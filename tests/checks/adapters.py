"""Every adapter normalises errors and redaction accounting the same way.

The check exists because they did not. opencode kept the redaction hits its own
`_sanitize_meta` collected; codex and agy carried a byte-similar function that discarded
them with `clean, _ = redact_value(...)`. Redaction still happened in all three — the
secret was scrubbed — but two of three providers then reported `redactions: 0` for the
call that scrubbed it, and `.workflow/audit.jsonl` is the record someone reads after an
incident.

Nothing caught it. The provider suite asserts the seam and the selection path, the
redaction suite asserts opencode's boundary, and no test ever put the three adapters
side by side and compared. A divergence between implementations of one contract is
invisible to any test that only ever looks at one of them.

So the assertions below are deliberately written as a loop over ALL registered adapters
rather than three near-copies: a fourth provider joins this check by existing, which is
the same reason the accounting itself now lives in adapters/redaction.py.
"""

import re
from pathlib import Path

from exoma.adapters.providers import agy_adapter, codex_adapter, opencode_adapter
from exoma.core.evidence.contract import ERROR_TYPES
from tests.checks.support import assert_true

# A key shaped like the real thing, kept obviously fake. Two distinct occurrences so a
# per-kind tally that silently collapses duplicates is visible as a wrong count.
_SECRET = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"

_ADAPTERS = (
    ("opencode", opencode_adapter),
    ("codex", codex_adapter),
    ("agy", agy_adapter),
)


def _redaction_count(payload: dict) -> int:
    return int((payload.get("meta") or {}).get("redaction_count") or 0)


def _test_adapter_error_normalization() -> None:
    shapes: dict[str, tuple] = {}

    for name, module in _ADAPTERS:
        # --- make_error: every string field must be scrubbed AND counted -------------
        error = module.make_error(
            "worker_died",
            f"worker died holding {_SECRET}",
            f"rotate {_SECRET} then retry",
            meta={"note": _SECRET},
            detail=_SECRET,
        )

        assert_true(
            _SECRET not in repr(error),
            f"{name}: make_error leaked the secret verbatim into the returned payload",
        )
        assert_true(
            _redaction_count(error) > 0,
            f"{name}: make_error scrubbed the secret but recorded redaction_count 0 — "
            "the adapter redacts before the executor sees the result, so an adapter that "
            "drops its own hits makes the audit trail understate exposure as nothing",
        )
        assert_true(
            error.get("ok") is False and error["meta"].get("error_type") in ERROR_TYPES,
            f"{name}: make_error must return ok=False with an error_type from the closed set",
        )

        # --- make_ok: same contract on the success path ------------------------------
        ok = module.make_ok(
            f"evidence mentioning {_SECRET}",
            meta={"note": _SECRET},
            digest={"summary": _SECRET},
        )
        assert_true(
            _SECRET not in repr(ok),
            f"{name}: make_ok leaked the secret verbatim into the returned payload",
        )
        assert_true(
            _redaction_count(ok) > 0,
            f"{name}: make_ok scrubbed the secret but recorded redaction_count 0",
        )

        # --- clean calls must not invent redactions ----------------------------------
        clean = module.make_ok("nothing sensitive here", meta={"note": "plain"})
        assert_true(
            _redaction_count(clean) == 0,
            f"{name}: a call with no secrets reported redactions — a false positive here "
            "is as bad as a miss, because it makes a clean history look like an incident",
        )

        shapes[name] = (tuple(sorted(error.keys())), tuple(sorted(ok.keys())))

    # --- the three must agree on shape, not merely each be self-consistent -----------
    reference = shapes["opencode"]
    for name in ("codex", "agy"):
        assert_true(
            shapes[name] == reference,
            f"{name} returns a different payload shape than opencode "
            f"({shapes[name]} vs {reference}) — the executor consumes all three through "
            "one code path, so a key present for one provider and absent for another is "
            "a branch nobody wrote",
        )
    _check_bootstrap_failures()


def _check_bootstrap_failures() -> None:
    import tempfile
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix="bootstrap-failure-") as tmp:
        for failure, expected in ((FileNotFoundError("missing executable"), "command_not_found"),
                                  ("quota exhausted", "rate_limited"),
                                  ("unauthorized: authentication failed", "permission_denied"),
                                  ("connect EACCES", "network_denied")):
            adapter = opencode_adapter.OpenCodeAdapter()
            outcome = {"returncode": 1, "stdout": "", "stderr": failure, "duration_seconds": 0,
                       "timed_out": False, "kill": None, "output_complete": True}
            with patch.object(adapter, "_resolve_command", return_value="opencode"), \
                    patch.object(adapter, "_child_env", return_value={}), \
                    patch.object(adapter, "_popen_capture", **({"side_effect": failure} if isinstance(failure, Exception) else {"return_value": outcome})) as capture:
                result = adapter.run("task", {"session_id": "s"}, model="test-model", work_dir=tmp)
            assert_true(result["meta"]["error_type"] == expected and capture.call_count == 1,
                        f"bootstrap must classify permanent failure before retry: {result}")
        adapter = opencode_adapter.OpenCodeAdapter()
        outcome = {"returncode": 0, "stdout": "a partial answer", "stderr": "", "duration_seconds": 0,
                   "timed_out": False, "kill": None, "output_complete": False}
        with patch.object(adapter, "_child_env", return_value={}), \
                patch.object(adapter, "_popen_capture", return_value=outcome):
            result = adapter._run_args(["opencode", "run"], tmp)
        assert_true(result["meta"]["error_type"] == "streaming_failed",
                    "a partial capture cannot be accepted as complete evidence")


def _test_adapter_redaction_is_shared() -> None:
    """All three must resolve to the SAME function object, not three equal ones.

    Asserting behaviour alone would pass again the moment someone re-copies the helper
    and then edits one copy — which is exactly how the original divergence appeared.
    Identity is the property that actually prevents a recurrence.
    """
    from exoma.adapters.shared import redaction

    for name, module in _ADAPTERS:
        assert_true(
            module.make_error is redaction.make_error,
            f"{name}.make_error is a local copy rather than adapters.shared.redaction.make_error; "
            "three copies of one rule is the shape the divergence grew in",
        )
        assert_true(
            module.make_ok is redaction.make_ok,
            f"{name}.make_ok is a local copy rather than adapters.shared.redaction.make_ok",
        )


def _test_stdin_failure_reaches_call_meta() -> None:
    """A prompt codex could not hand over must say so in the run meta.

    stdin is the one transport where the handover can fail while the process is already
    running: the child closes its end — typically because it rejected the input — and the
    write lands on a broken pipe. The adapter then waits out a process that was never given
    its question, and the call returns an empty answer with no cause attached. "Prompt too
    long" has been guesswork for exactly that reason.

    The assertion is about the meta a LATER reader gets, not about the moment the failure is
    detected, because those came apart once already: `last_call_meta` is REBOUND wholesale
    after the process ends, so recording the error when it happened wrote it onto an object
    that was about to be discarded. The instrumentation read as correct and reached nothing.
    """
    import sys

    adapter = codex_adapter.CodexAdapter()

    # Exits without reading stdin, so a large write hits a closed pipe. Nothing about the
    # size is special to codex — this reproduces the shape, not a specific provider limit.
    adapter._popen_capture(
        [sys.executable, "-c", "import sys; sys.exit(0)"],
        "x" * 5_000_000,
        None,
        30,
        "probe",
        None,
    )
    meta = adapter.last_call_meta
    assert_true(
        meta.get("prompt_chars") == 5_000_000,
        f"the size of the prompt that failed to send was not recorded: {meta.get('prompt_chars')}",
    )
    assert_true(
        "BrokenPipe" in str(meta.get("stdin_write_failed") or ""),
        "a failed stdin handover left no cause in the call meta, which is the silence this "
        f"instrumentation exists to end: {meta.get('stdin_write_failed')!r}",
    )

    adapter._popen_capture(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        "hello",
        None,
        30,
        "probe",
        None,
    )
    meta = adapter.last_call_meta
    assert_true(
        meta.get("prompt_chars") == 5 and meta.get("stdin_write_failed") is None,
        f"a successful handover reported a failure, which would make the signal noise: {meta}",
    )
    _check_transport_boundaries()


def _check_transport_boundaries() -> None:
    import os
    import subprocess
    import sys
    import time
    from unittest.mock import patch

    adapter = codex_adapter.CodexAdapter()
    adapter.poll_interval = 0.05
    started = time.monotonic()
    out = adapter._popen_capture([sys.executable, "-c", "import time; time.sleep(60)"],
                                 "x" * 5_000_000, None, 0.3, "probe", None)
    assert_true(out["timed_out"] and time.monotonic() - started < 10,
                "stdin write/close must not bypass the supervising deadline")

    for module, cls in ((codex_adapter, codex_adapter.CodexAdapter),
                        (opencode_adapter, opencode_adapter.OpenCodeAdapter),
                        (agy_adapter, agy_adapter.AgyAdapter)):
        adapter = cls()
        adapter.poll_interval = 0.05
        def capture(args, timeout=5):
            if module is codex_adapter:
                return adapter._popen_capture(args, "", None, timeout, "probe", None)
            if module is opencode_adapter:
                return adapter._popen_capture(args, os.environ.copy(), None, timeout, "probe")
            return adapter._popen_capture(args, None, timeout, "probe", None)
        with patch.object(module, "MAX_CAPTURE_CHARS", 4096):
            out = capture([sys.executable, "-c", "print('x' * 50000, end='')"])
        assert_true(len(out["stdout"]) < 4300 and adapter.last_call_meta["capture_truncated"],
                    f"{module.__name__}: a single line must obey the capture bound")
        children = []
        original_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child
        with patch.object(module.subprocess, "Popen", side_effect=spawn), \
                patch.object(adapter, "_tick", side_effect=KeyboardInterrupt):
            try:
                capture([sys.executable, "-c", "import time; time.sleep(60)"])
                raise AssertionError("interrupt must propagate")
            except KeyboardInterrupt:
                pass
        assert_true(children and children[0].poll() is not None,
                    f"{module.__name__}: interrupt left its child running")


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _test_stream_observations():
    import json
    import tempfile
    import time
    from exoma.adapters.shared.stream import StreamTracker, MAX_EVENT
    now = [0.0]
    with tempfile.TemporaryDirectory(prefix="provider-stream-") as tmp:
        for provider, start, work in (
            ("codex", {"type": "thread.started"}, {"type": "item.completed", "item": {"type": "command_execution"}}),
            ("claude", {"type": "system"}, {"type": "assistant", "message": {"content": [{"text": "answer"}]}}),
            ("opencode", {"type": "step_start"}, {"type": "step_finish"}),
            ("agy", {"step_update": {}}, {"step_update": {"step_type": "agent_response", "text_delta": "answer"}}),
        ):
            now[0] = 0
            tracker = StreamTracker(provider, tmp, clock=lambda: now[0])
            tracker.feed("stdout", json.dumps(start) + "\n")
            now[0] = 50
            tracker.feed("stderr", "WebSocket disconnected; retrying\n")
            snap = tracker.snapshot()
            assert_true(snap["work_idle_seconds"] == 50 and snap["retry_count"] == 1,
                        f"{provider}: retry output is not work progress")
            tracker.feed("stdout", json.dumps(work) + "\n")
            assert_true(tracker.snapshot()["work_idle_seconds"] == 0 and tracker.snapshot()["work_events"] == 1,
                        f"{provider}: a recognized work event advances activity")
            tracker.feed("stdout", json.dumps({"type": "error", "api_key": "tiny", "text": _SECRET}) + "\n")
            tracker.feed("stdout", "x" * (MAX_EVENT + 1))
            tracker.feed("stdout", _SECRET + "\n")
            tracker.feed("stdout", '{"type":"unknown","password":"split')
            tracker.feed("stdout", 'credential"}\n')
            until = time.monotonic() + 2
            while not list(tracker.sink.path.glob("*.jsonl")) and time.monotonic() < until:
                time.sleep(0.01)
            assert_true(bool(list(tracker.sink.path.glob("*.jsonl"))), "diagnostics are on disk before provider exit")
            tracker.close()
            text = "".join(p.read_text(encoding="utf-8") for p in tracker.sink.path.glob("*.jsonl"))
            assert_true(_SECRET not in text and "splitcredential" not in text and '"tiny"' not in text,
                        "decoded, short named and fragmented credentials are redacted before persistence")
            assert_true(tracker.snapshot()["diagnostics"]["oversized_events"] > 0,
                        "oversized fragments are discarded with an explicit count")
        bad = Path(tmp) / "not-a-directory"
        bad.write_text("occupied", encoding="utf-8")
        tracker = StreamTracker("codex", bad)
        tracker.feed("stderr", "retry\n")
        tracker.close()
        assert_true(tracker.snapshot()["diagnostics"]["error"] is not None,
                    "sink failure is visible without failing provider capture")
        from exoma.core.evidence.provider_stream import DiagnosticSink
        flood = DiagnosticSink(tmp, byte_limit=65536)
        for sequence in range(1000):
            flood.emit({"sequence": sequence, "payload": "x" * 4096})
        flood.close()
        assert_true(sum(path.stat().st_size for path in flood.path.glob("*.jsonl")) <= 65536
                    and (flood.dropped or flood.truncated), "flood capture stays bounded with visible loss")
        tracker = StreamTracker("codex")
        tracker.feed("stdout", json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "aggregated_output": 'turn.failed WebSocket error'}}) + "\n")
        assert_true(not tracker.snapshot()["terminal_error"] and tracker.snapshot()["diagnostic"] is None,
                    "quoted tool errors do not become terminal provider errors")
        tracker.close()
        import sys
        adapter = codex_adapter.CodexAdapter()
        adapter.poll_interval = 0.05
        started = time.monotonic()
        out = adapter._popen_capture([sys.executable, "-c",
            "import json,time; print(json.dumps({'type':'turn.failed','error':{'message':'provider failed'}}),flush=True); time.sleep(60)"],
            "prompt", None, 60, "run", None)
        assert_true(time.monotonic() - started < 15 and adapter.last_call_meta["terminal_error"]
                    and out["returncode"] != 0, "terminal provider event ends waiting and cleans the process before its deadline")
_MAKE_ERROR_LITERAL = re.compile(r'make_error\(\s*"([a-z_]+)"')


def _test_error_types_are_registered() -> None:
    """Every literal error_type handed to make_error is in the closed set.

    make_error raises on an unregistered type, so a typo there is not a wrong label: it is
    a traceback on exactly the failure path the error was written for. Two shipped that
    way — main's `session_state_error` and agy's `stream_failed` — and no test reached
    either path. Scanning the source catches the next one without having to provoke it.
    """
    unregistered = []
    for path in sorted(_REPO_ROOT.rglob("*.py")):
        parts = path.relative_to(_REPO_ROOT).parts
        if parts[0] in (".workflow", "tests", ".git") or "__pycache__" in parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _MAKE_ERROR_LITERAL.finditer(text):
            if match.group(1) not in ERROR_TYPES:
                line = text.count("\n", 0, match.start()) + 1
                unregistered.append(f"{path.relative_to(_REPO_ROOT).as_posix()}:{line} {match.group(1)}")
    assert_true(
        not unregistered,
        f"make_error called with an unregistered error_type (it would raise): {unregistered}",
    )
