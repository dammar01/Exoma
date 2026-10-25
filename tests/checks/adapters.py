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


_REPO_ROOT = Path(__file__).resolve().parents[2]
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
