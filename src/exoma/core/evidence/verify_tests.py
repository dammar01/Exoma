"""Tests for /.verify: chosen by main_agent, run by the runtime, judged beside the review.

The second agent verifies by reading and tracing only. When it also ran tests, providers
diverged: codex ran whole suites (CASE-010: verify medians up to 366 s), opencode could run
none and every such verify came back `incomplete` (CASE-009). Now main_agent picks the tests
that cover the change and its dependents and writes them, with its reason, to the session's
request file before calling verify:

    .workflow/data/sessions/<MAIN_SESSION_ID>/verify/tests.json
    {"commands": ["python tests/run.py --only installer"], "reason": "installer changed"}

`commands: []` with a reason says no test covers the change. The runtime runs a command only
when it starts with a prefix the project lists in `commands.verify_test_commands`, with no
shell (no `&&`, pipes, redirects, substitution or control characters), one at a time,
each under `commands.verify_test_timeout_seconds`. Results are written into the
[VERIFICATION] block the second agent returned, which never sees them: `checks_run` gets one
runtime line per command; a failing, timed-out or unrunnable command is a blocking finding
(origin `unknown`, so it fails closed); a refused command or a missing or unusable request
is a `not_verified` gap under the `tests:` prefix, which the validator reads as a
`runtime_gap` — never the gap-only exit 0 an agent's own declared gap earns. The verdict is
then derived from the block as usual, so `pass` needs green tests and a review with no
blocking finding. `commands: []` adds a visible `checks_run` line that is not counted as an
executed check: the verdict then rests on the checks the review itself ran.

The tests run after the provider has finished, when nothing else beats the job's heartbeat:
`apply` takes the delegated call's progress callback and beats it (phase `runtime_tests`)
before each command and every `HEARTBEAT_SECONDS` while one runs.

The request belongs to one verify job. When the job is created, `claim` moves it to
`tests.claimed.json` and drops whatever an earlier job left there; a recovered run of the
same job is not a new job and finds its request still claimed. It is consumed, renamed to
`tests.used.json`, once its results are written into the verify result. A job that ended
before that (the provider call failed, the run was refused) leaves its claim for the next
job to drop, so a later verify never reruns an old choice silently (CASE-023, DEC-062). Only
a job's own run reads a claim (`job`); a direct `main.run` reads tests.json alone.

A test main_agent already ran through the runtime is not run again. `run.sh test` runs
allowlisted commands the same way (`run_and_record`) and appends each outcome, with the
reuse identity (`reuse_identity`: the working tree's content, `.workflow/config.json`, the
listed environment variables and ignored input files), to `verify/test-runs.jsonl`. When
verify then requests the exact same command string and its latest recorded run passed
under the identity the project still has, its outcome is that run, marked `reused`, and
`checks_run` says so. A filtered command never stands in for another: the key is the
command text. A failed run, any change to those inputs, an unknown tree (no Git), an
unreadable record, or a command no longer allowlisted is run as usual.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from contextvars import ContextVar
from pathlib import Path

from exoma.core.evidence.contract import RUNTIME_GAP_PREFIX, RUNTIME_NO_TEST_ITEM
from exoma.core.runtime.config_defaults import verification_settings
from exoma.core.workspace.workspace_paths import read_json_file, workflow_paths
from exoma.utils import osutil
from exoma.utils.redact import redact

MAX_COMMANDS = 5
DEFAULT_TIMEOUT_SECONDS = 900
OUTPUT_TAIL_CHARS = 1200
_SHELL_SYNTAX = re.compile(r"[&|;<>`\n\r]|\$\(")
# NUL and the other C0 controls. Popen refuses an embedded NUL with ValueError — after the
# paid provider call, and with the request left in place so every retry crashed the same
# way — and the rest have no business in a test command line either.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # a tab is only whitespace
# How often a running test command beats the job's heartbeat. Well under the idle-stall
# threshold (config/settings.py), so a long suite never reads as a stalled worker.
HEARTBEAT_SECONDS = 20.0


def request_path(project_root, session_id: str) -> Path:
    return workflow_paths(Path(project_root), session_id)["session_dir"] / "verify" / "tests.json"


def runs_path(project_root, session_id: str) -> Path:
    return request_path(project_root, session_id).with_name("test-runs.jsonl")


def claimed_path(project_root, session_id: str) -> Path:
    return request_path(project_root, session_id).with_name("tests.claimed.json")


def claim(project_root, session_id: str) -> None:
    """Bind the request main_agent wrote to the verify job being created. An unconsumed
    claim is an earlier job's: the provider call failed or the run was refused before the
    tests ran. Before this, it stayed as tests.json and the next verify ran it (CASE-023)."""
    request = request_path(project_root, session_id)
    claimed = claimed_path(project_root, session_id)
    try:
        claimed.unlink(missing_ok=True)
        if request.is_file():
            request.replace(claimed)
    except OSError:
        pass


# The job whose worker is running this verify; None for a direct `main.run` (harnesses, sim).
_JOB: ContextVar[str | None] = ContextVar("verify_job", default=None)


@contextlib.contextmanager
def job(job_id: str):
    """Mark the calls inside as the run of `job_id`, so they read that job's claim."""
    token = _JOB.set(job_id)
    try:
        yield
    finally:
        _JOB.reset(token)


def _request_file(project_root, session_id: str) -> Path:
    """Inside a job, its claimed request. A run without a job reads tests.json only: a
    claim it finds was left by a job that ended early, never written for this run."""
    claimed = claimed_path(project_root, session_id)
    if _JOB.get() is not None and claimed.is_file():
        return claimed
    return request_path(project_root, session_id)


def policy(project_root) -> tuple[dict, list[str]]:
    """Effective policy for both prepare and actual shell-free test execution."""
    settings, warnings = verification_settings(Path(project_root))
    prefixes = []
    for item in settings["verify_test_commands"]:
        if not item.strip():
            continue
        tokens = _split(item)
        if not tokens or _SHELL_SYNTAX.search(item) or _CONTROL_CHARS.search(item):
            warnings.append("commands.verify_test_commands: invalid prefix; no commands allowed")
            prefixes = []
            break
        prefixes.append(tokens)
    return {
        "verify_mode": settings["verify_mode"],
        "auto_verify_after_execute": settings["auto_verify_after_execute"],
        "test_command_prefixes": prefixes,
        "test_timeout_seconds": settings["verify_test_timeout_seconds"],
        "max_commands": MAX_COMMANDS,
    }, warnings


def _policy(project_root) -> tuple[list[list[str]], int]:
    effective, _ = policy(project_root)
    return effective["test_command_prefixes"], effective["test_timeout_seconds"]


def configured(project_root) -> bool:
    """True when the project lists at least one test command prefix the runtime may run."""
    return bool(_policy(project_root)[0])


def _one_line(value) -> str:
    """`value` on one line. Request text lands inside the [VERIFICATION] block, whose
    sections are found by header line: a newline in it could forge a section. Other control
    characters (a refused command may carry a NUL) are shown as `?`."""
    return " ".join(_CONTROL_CHARS.sub("?", str(value)).split())


def _split(command: str) -> list[str]:
    """Tokens of a command line. Windows keeps backslashes (non-POSIX rules), whose tokens
    keep their surrounding quotes; those are stripped, so `"C:\\Program Files\\x.exe" -k`
    names the executable it quotes."""
    try:
        tokens = osutil.split_command(command)
    except ValueError:
        return []
    return [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]


def read_request(project_root, session_id: str) -> dict | None:
    """The request as written, or None when there is none. Not consumed here: see `apply`."""
    return _read_request(_request_file(project_root, session_id))


def _read_request(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"invalid": "tests.json is not valid JSON"}
    if not isinstance(data, dict):
        return {"invalid": "tests.json must be an object"}
    commands = data.get("commands")
    if not isinstance(commands, list) or any(not isinstance(command, str) for command in commands):
        return {"invalid": "`commands` must be a list of command strings"}
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        return {"invalid": "`reason` must be a string"}
    if not commands and not (reason or "").strip():
        return {"invalid": "an explicit no-test request needs a non-empty `reason`"}
    if any(ch in str(data.get("reason") or "") for ch in "\r\n"):
        return {"invalid": "`reason` must be one line"}
    return data


def _consume(project_root, session_id: str) -> None:
    path = _request_file(project_root, session_id)
    try:
        path.replace(path.with_name("tests.used.json"))
    except OSError:
        pass


def _allowed(tokens: list[str], prefixes: list[list[str]]) -> bool:
    return any(tokens[: len(prefix)] == prefix for prefix in prefixes)


def _command_error(command: str, tokens: list[str], prefixes: list[list[str]]) -> str | None:
    if not command or not tokens:
        return "empty or unparseable"
    if _SHELL_SYNTAX.search(command):
        return "shell syntax is not run"
    if _CONTROL_CHARS.search(command):
        return "control characters are not run"
    if not _allowed(tokens, prefixes):
        return "not under commands.verify_test_commands in .workflow/config.json"
    return None


def prepare(project_root, session_id: str) -> dict:
    """Inspect pending inputs only: no claim, test, provider, state write or verdict."""
    effective, warnings = policy(project_root)
    path = request_path(project_root, session_id)
    data = _read_request(path)  # deliberately ignores this process's job/claimed context
    errors = []
    commands, reason = [], None
    if data is None:
        status = "missing"
    elif "invalid" in data:
        status = "invalid"
        errors.append(data["invalid"])
    else:
        commands, reason = data["commands"], data.get("reason")
        if len(commands) > MAX_COMMANDS:
            errors.append(f"at most {MAX_COMMANDS} commands are allowed")
        for index, raw in enumerate(commands[:MAX_COMMANDS]):
            command = raw.strip()
            error = _command_error(command, _split(command), effective["test_command_prefixes"])
            if error:
                errors.append(f"commands[{index}]: {error}")
        status = "invalid" if errors else "valid"
    action = ("verify" if effective["verify_mode"] == "syntax" or status == "valid" else
              "fix_request" if status == "invalid" else "select_tests")
    return {
        "ok": True,
        "content": f"verification prepared: mode {effective['verify_mode']}; pending request {status}",
        "meta": {
            "command": "verify-prepare", "session_id": session_id,
            "policy": effective, "config_warnings": warnings,
            "request": {"path": str(path), "status": status, "commands": commands,
                        "reason": reason, "validation_errors": errors},
            "next_step": {"action": action, "session_id": session_id, "automatic": False},
        },
    }


def _beat(on_progress, started: float, **fields) -> None:
    """One liveness beat in the shape the adapters send. A broken callback must not fail
    the tests it is reporting on."""
    if on_progress is None:
        return
    try:
        on_progress(
            {"phase": "runtime_tests", "elapsed_seconds": round(time.monotonic() - started, 1), **fields}
        )
    except Exception:
        pass


def run_tests(
    project_root, request: dict, on_progress=None, reuse: dict | None = None, identity=None,
) -> list[dict]:
    """One outcome per requested command: passed | failed | timeout | error | refused.

    `reuse` maps a command to its earlier passing run on this same tree (`reusable`): that
    command is not run again, and its outcome is the earlier one marked `reused`. The map
    is taken before the first command, so it holds only until a command actually runs: a
    test may write into the tree, and every later command then runs as well.

    `identity`, when given, is called before the first command that runs and after every
    command that runs; each such outcome gets `fingerprint`: the identity it ran on when the
    command left it unchanged, else None. A batch that changes an input, tests it and
    restores it thus binds the middle pass to the middle state, not to the state the batch
    started and ended in."""
    prefixes, timeout = _policy(project_root)
    outcomes: list[dict] = []
    commands = request.get("commands")
    if not isinstance(commands, list):
        return [{"command": "", "status": "refused", "detail": "`commands` must be a list"}]
    unsampled = object()
    current = unsampled
    run_started = time.monotonic()
    for index, raw in enumerate(commands[:MAX_COMMANDS]):
        command = str(raw).strip()
        tokens = _split(command)
        error = _command_error(command, tokens, prefixes)
        if error:
            outcomes.append({"command": command, "status": "refused", "detail": error})
            continue
        earlier = (reuse or {}).get(command)
        if earlier is not None:
            outcomes.append({
                "command": command, "status": "passed", "reused": True,
                "seconds": 0.0, "ran_at": earlier.get("at"), "ran_seconds": earlier.get("seconds"),
            })
            continue
        if identity is not None and current is unsampled:
            current = identity()
        started = time.monotonic()
        progress = {"command_index": index, "commands": min(len(commands), MAX_COMMANDS)}
        _beat(on_progress, run_started, **progress)
        try:
            # Bounded and tree-killed: a runner whose worker outlives it must not hang verify.
            code, stdout, stderr = osutil.run_bounded(
                [osutil.resolve_exe(tokens[0]), *tokens[1:]],
                timeout,
                cwd=str(project_root),
                on_tick=lambda _elapsed: _beat(on_progress, run_started, **progress),
                tick_seconds=HEARTBEAT_SECONDS,
            )
            if code is None:
                outcome = {"command": command, "status": "timeout", "detail": f"over {timeout} s"}
            else:
                status = "passed" if code == 0 else "failed"
                tail = (stdout + "\n" + stderr).strip()[-OUTPUT_TAIL_CHARS:]
                outcome = {"command": command, "status": status, "exit_code": code, "tail": tail}
        except (OSError, ValueError) as exc:  # ValueError: an argument Popen will not pass
            outcome = {"command": command, "status": "error", "detail": str(exc)[:200]}
        outcome["seconds"] = round(time.monotonic() - started, 1)
        if outcome.get("tail"):
            outcome["tail"] = redact(outcome["tail"])[0]
        if identity is not None:
            after = identity()
            outcome["fingerprint"] = current if current is not None and current == after else None
            if outcome["fingerprint"] is None:
                outcome["fingerprint_reason"] = ("identity_before_unavailable" if current is None else
                                                  "identity_after_unavailable" if after is None else
                                                  "inputs_changed_during_test")
            current = after
        outcomes.append(outcome)
        # The identity behind `reuse` was the tree before this command ran.
        reuse = None
    for raw in commands[MAX_COMMANDS:]:
        outcomes.append({"command": str(raw), "status": "refused", "detail": f"over {MAX_COMMANDS} commands"})
    return outcomes


def _findings(request: dict | None, outcomes: list[dict]) -> dict[str, list[str]]:
    """The lines to add, per [VERIFICATION] section."""
    add: dict[str, list[str]] = {"blocking_findings": [], "checks_run": [], "not_verified": []}
    if request is None:
        add["not_verified"].append(
            f"{RUNTIME_GAP_PREFIX} main_agent wrote no test request for this verify (verify/tests.json); no test ran"
        )
        return add
    if request.get("invalid"):
        add["not_verified"].append(f"{RUNTIME_GAP_PREFIX} the test request was not usable ({request['invalid']}); no test ran")
        return add
    reason = _one_line(request.get("reason") or "") or "no reason given"
    if not outcomes:
        # Visible, but not counted as a check by the validator: the review's own checks decide.
        add["checks_run"].append(f"{RUNTIME_NO_TEST_ITEM} — {reason}")
        return add
    for outcome in outcomes:
        command = _one_line(outcome["command"])
        status = outcome["status"]
        if status == "passed" and outcome.get("reused"):
            add["checks_run"].append(
                f"runtime: `{command}` passed in {outcome.get('ran_seconds')} s at "
                f"{_one_line(outcome.get('ran_at') or 'an earlier run')} on this same tree "
                "(nothing edited since) — reused, not rerun"
            )
        elif status == "passed":
            add["checks_run"].append(f"runtime: `{command}` passed in {outcome['seconds']} s")
        elif status == "refused":
            add["not_verified"].append(f"{RUNTIME_GAP_PREFIX} `{command}` was not run — {_one_line(outcome['detail'])}")
        else:
            detail = _one_line(
                f"exited {outcome['exit_code']}" if status == "failed" else outcome.get("detail") or status
            )
            tail = " ".join((outcome.get("tail") or "").split())[-300:]
            add["checks_run"].append(f"runtime: `{command}` {status} ({detail})")
            add["blocking_findings"].append(
                f"severity: high | origin: unknown | scope_relation: in_scope — runtime test "
                f"`{command}` {detail}" + (f" — tail: {tail}" if tail else "")
                + " — fix the failure or show it is unrelated, then re-verify"
            )
    return add


def _is_none(item: str) -> bool:
    from exoma.core.evidence.contract import _NONE_ITEM

    return _NONE_ITEM.fullmatch(item.lstrip("-").strip())


def _insert(lines: list[str], section: str, items: list[str]) -> list[str]:
    head = re.compile(rf"^\s*{re.escape(section)}\s*:(.*)$", re.IGNORECASE)
    start = next((i for i, line in enumerate(lines) if head.match(line)), None)
    new = [f"- {item}" for item in items]
    if start is None:
        return lines + ["", f"{section}:", *new]
    inline = head.match(lines[start]).group(1).strip()
    if inline and _is_none(inline):
        lines[start] = f"{section}:"
    out = lines[: start + 1] + new
    rest = lines[start + 1 :]
    # Drop "- none" entries of this section: it is no longer empty.
    i = 0
    while i < len(rest):
        stripped = rest[i].strip()
        if re.match(r"^[a-z_]+\s*:", stripped, re.IGNORECASE) or (stripped.startswith("[") and stripped.endswith("]")):
            break
        if stripped.startswith("-") and _is_none(stripped):
            rest.pop(i)
            continue
        i += 1
    return out + rest


def merge(content: str, request: dict | None, outcomes: list[dict]) -> str:
    """`content` with the runtime's test results written into its [VERIFICATION] block."""
    return _merge_lines(content, _findings(request, outcomes))


def merge_gap(content: str, line: str) -> str:
    """`content` with one runtime gap added under not_verified (the executor's crash path)."""
    return _merge_lines(
        content, {"blocking_findings": [], "checks_run": [], "not_verified": [_one_line(line)]}
    )


def _merge_lines(content: str, add: dict[str, list[str]]) -> str:
    marker = "[VERIFICATION]"
    before, sep, body = (content or "").partition(marker)
    if not sep:
        before, body = "", content or ""
    # Keep [DIGEST] and anything after it out of the section edits.
    body, digest_sep, tail = body.partition("[DIGEST]")
    lines = body.split("\n")
    for section in ("blocking_findings", "checks_run", "not_verified"):
        if add[section]:
            lines = _insert(lines, section, add[section])
    if add["blocking_findings"]:
        lines = [
            re.sub(r"^(\s*verdict\s*:).*$", r"\1 NEEDS FIX", line, flags=re.IGNORECASE)
            for line in lines
        ]
    merged = "\n".join(lines)
    return before + sep + merged + (digest_sep + tail if digest_sep else "")


def apply(project_root, session_id: str, result: dict, on_progress=None) -> dict:
    """Run the requested tests and write their results into a delegated verify result.

    `on_progress` is the delegated call's progress callback: the provider has finished, so
    these beats are the only thing keeping the job from reading as stalled while tests run.
    """
    request = read_request(project_root, session_id)
    outcomes = (
        run_tests(project_root, request, on_progress, reusable(project_root, session_id))
        if request and not request.get("invalid")
        else []
    )
    record_error = _record_verify_failures(project_root, session_id, outcomes)
    result["content"] = merge(result.get("content") or "", request, outcomes)
    # Consumed only now that its results are in the result. Renaming on read lost the
    # request when the worker died while the tests ran: the recovered run of the same job
    # found none and reported "no test request". A death between here and the result being
    # saved still loses it; that recovered run reports the gap (incomplete), never a pass.
    if request is not None:
        _consume(project_root, session_id)
    meta = result.setdefault("meta", {})
    meta["runtime_tests"] = {
        "requested": request is not None and not request.get("invalid"),
        "reason": (request or {}).get("reason"),
        "outcomes": [{k: v for k, v in o.items() if k != "tail"} for o in outcomes],
        "reused": sum(1 for o in outcomes if o.get("reused")),
        "seconds": round(sum(o.get("seconds") or 0 for o in outcomes), 1),
    }
    if record_error:
        meta["runtime_tests"]["record_error"] = record_error
    return result


# Outcomes of a command that ran and did not pass.
_NOT_PASSED = ("failed", "timeout", "error")
# A marker of this kind records a failure verify already saw, not a run still going: a
# later `run.sh test` that recorded its commands retires it even while its owner lives.
VERIFY_FAILURE_MARKER = "verify_failure"


def _record_verify_failures(project_root, session_id: str, outcomes: list[dict]) -> str | None:
    """Append a row for every command verify ran that did not pass, so an earlier `run.sh
    test` pass for it stops being the latest row and is no longer reused. A pass inside
    verify is still not recorded: only `run.sh test` makes a command reusable. The rows
    carry no fingerprint, so they can never be reused themselves. Returns an error, or None.

    When the rows cannot be appended the earlier pass would stay the latest row, so the
    failure is kept another way: a pending marker owned by this process names the failed
    commands, which `reusable` withholds until a `run.sh test` started after this process
    started records them again (`run_and_record` retires it then, owner alive or not: the
    failure is over, and that run's rows are newer than it). When even the marker
    cannot be written, the record is removed: no pass is reused rather than a stale one."""
    failed = [o for o in outcomes if o.get("status") in _NOT_PASSED and not o.get("reused")]
    if not failed:
        return None
    rows = "".join(
        json.dumps({
            "command": o["command"], "status": o["status"], "seconds": o.get("seconds"),
            "fingerprint": None, "at": _now(), "source": "verify",
            "fingerprint_reason": "verification_test_failed",
        }) + "\n"
        for o in failed
    )
    path = runs_path(project_root, session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(rows)
    except OSError as exc:
        error = f"verify failure not recorded: {type(exc).__name__}: {exc}"
    else:
        return None
    marker = _marker_path(project_root, session_id)
    partial = marker.with_name(marker.name + ".tmp")
    try:
        pid = os.getpid()
        partial.write_text(json.dumps({
            "pid": pid, "identity": osutil.process_identity(pid),
            "commands": [o["command"] for o in failed], "at": _now(),
            "kind": VERIFY_FAILURE_MARKER,
        }), encoding="utf-8")
        os.replace(partial, marker)
    except OSError as exc:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError as unlink_exc:
            return (f"{error}; marker not written ({type(exc).__name__}) and record not removed "
                    f"({type(unlink_exc).__name__}): an earlier pass may still be reused")
        return f"{error}; marker not written ({type(exc).__name__}): record removed, nothing is reused"
    return f"{error}; the earlier pass is withheld by {marker.name} until the command is recorded again"


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULT_REUSE_ENV = ("WORKFLOW_*", "EXOMA_*", "PYTEST_*", "CI", "NODE_ENV", "APP_ENV")
DEFAULT_REUSE_FILES = (".env", ".env.*")


def _reuse_policy(project_root) -> tuple[list[str], list[str]]:
    """`commands.verify_reuse_env` (variable-name patterns) and `commands.verify_reuse_files`
    (globs under the project root), each falling back to its default when absent or not a
    list of strings."""
    try:
        config = read_json_file(workflow_paths(Path(project_root))["config"])
    except (OSError, ValueError):
        config = {}
    commands = config.get("commands") if isinstance(config.get("commands"), dict) else {}

    def strings(key, default):
        value = commands.get(key)
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return [v for v in value if v.strip()]
        return list(default)

    return strings("verify_reuse_env", DEFAULT_REUSE_ENV), strings("verify_reuse_files", DEFAULT_REUSE_FILES)


def reuse_identity(project_root, environ=None) -> str | None:
    """What a test run is reused under: the tree's content plus the inputs Git does not see.

    sha256 over the Git tree id of the working tree (core/evidence/tree_fingerprint.py),
    the bytes of `.workflow/config.json`, every environment variable whose name matches
    `commands.verify_reuse_env`, and every file matching `commands.verify_reuse_files`
    (ignored inputs such as `.env`). Only the hash is stored, never a value. None when the
    tree cannot be fingerprinted (no Git): then nothing is reusable.

    Not covered, and stated in the docs: installed dependencies and virtualenvs, the
    contents of submodules, PATH/PYTHONPATH (they differ between `run.sh test` and the
    verify worker, so hashing them would end every reuse), and any input nobody listed.
    """
    import fnmatch
    import hashlib
    import os

    from exoma.core.evidence.tree_fingerprint import tree_fingerprint

    tree = tree_fingerprint(project_root)
    if tree is None:
        return None
    root = Path(project_root)
    env_patterns, file_globs = _reuse_policy(root)
    digest = hashlib.sha256()
    digest.update(b"tree\0" + tree.encode())
    try:
        config = workflow_paths(root)["config"].read_bytes()
    except OSError:
        config = b"\0missing"
    digest.update(b"\0config\0" + hashlib.sha256(config).digest())
    source = os.environ if environ is None else environ
    for name in sorted(source):
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in env_patterns):
            digest.update(b"\0env\0" + name.encode("utf-8", "surrogateescape") + b"=")
            digest.update(hashlib.sha256(str(source[name]).encode("utf-8", "surrogateescape")).digest())
    files = set()
    for pattern in file_globs:
        try:
            files.update(p for p in root.glob(pattern) if p.is_file())
        except (OSError, ValueError):
            continue
    for path in sorted(files):
        try:
            content = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            content = b"\0unreadable"
        # A POSIX name that is not UTF-8 decodes to lone surrogates; `surrogatepass` hashes
        # it as it is instead of raising after the run's marker is already written.
        name = path.relative_to(root).as_posix().encode("utf-8", "surrogatepass")
        digest.update(b"\0file\0" + name + content)
    return digest.hexdigest()[:40]


def _identity(project_root) -> str | None:
    """`reuse_identity`, or None when computing it fails: an identity that cannot be taken
    makes nothing reusable, it never stops a run from being recorded."""
    try:
        return reuse_identity(project_root)
    except (OSError, ValueError):  # ValueError covers UnicodeError
        return None


# A torn marker older than this cannot belong to a run still going: every command is
# bounded by the timeout and tree-killed past it, and there are at most MAX_COMMANDS.
TORN_MARKER_GRACE_SECONDS = 300


def _torn_marker_limit(project_root) -> float:
    return MAX_COMMANDS * _policy(project_root)[1] + TORN_MARKER_GRACE_SECONDS


def _retire_torn(project_root, session_id: str, torn: list[Path]) -> str | None:
    """Retire torn markers too old to be a live run's. The run that wrote one may have
    ended without recording a newer outcome of any command, so a barrier row goes into the
    record first and every row before it stops being reusable; only then is the marker set
    aside as `.corrupt` (kept for inspection, no longer read). Returns an error, or None."""
    if not torn:
        return None
    path = runs_path(project_root, session_id)
    barrier = json.dumps({
        "barrier": "torn pending marker retired: " + ", ".join(p.name for p in torn), "at": _now(),
    }) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(barrier)
    except OSError as exc:
        return f"torn pending marker kept, barrier not written: {type(exc).__name__}"
    for marker in torn:
        try:
            marker.replace(marker.with_name(marker.name + ".corrupt"))
        except OSError:
            pass  # still withholds everything: safe
    return None


def run_and_record(project_root, session_id: str, commands: list[str]) -> dict:
    """`run.sh test`: run `commands` under the verify policy, record each outcome with the
    reuse identity, and return `{outcomes, fingerprint, reusable, record_error}`;
    `reusable` lists the passing commands a later verify may reuse.

    The identity is taken before and after every command, and each row carries the one
    its command ran on. A command that writes into the tree (a build artifact Git does not
    ignore) or a listed input is recorded with no identity, so it is never reused, and a
    later command in the batch is bound to the state that command left. `fingerprint` in
    the result is the batch's starting identity, None when any command changed it.

    Fails closed. Each run first writes its own marker, `test-runs.<pid>-<token>.pending`,
    naming its owner (pid + process identity) and its commands; `reusable` withholds every
    command a marker names, whoever owns it. When the marker cannot be written, nothing
    runs (`outcomes` is empty, `record_error` says why): a run that cannot be recorded would
    leave an older pass in charge. The marker is written whole (a temporary name, then a
    rename), so a live run's marker is never torn. After the append the run removes its own
    marker and those of owners that were already dead before it ran whose commands it has
    just recorded, never a live run's, nor one that appeared while it ran (its rows are
    then older than that run's lost outcome). A torn marker older than any run can last is
    retired before the run, behind a barrier row (`_retire_torn`). On an append failure the
    marker stays and the record is removed. Every failure is reported in `record_error`.
    """
    clean = [str(command).strip() for command in commands]
    marker = _marker_path(project_root, session_id)
    partial = marker.with_name(marker.name + ".tmp")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        partial.write_text(json.dumps({
            "pid": pid, "identity": osutil.process_identity(pid), "commands": clean, "at": _now(),
        }), encoding="utf-8")
        os.replace(partial, marker)
    except OSError as exc:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        return {
            "outcomes": [], "fingerprint": None, "reusable": [],
            "record_error": f"pending marker not written, nothing was run: {type(exc).__name__}: {exc}",
        }
    # Taken before the run: only these may be cleared after it (see the docstring).
    retire, torn = [], []
    limit = _torn_marker_limit(project_root)
    for other, owner in _markers(project_root, session_id):
        if other == marker:
            continue
        if owner is None:
            try:
                if time.time() - other.stat().st_mtime > limit:
                    torn.append(other)
            except OSError:
                pass
        elif owner.get("kind") == VERIFY_FAILURE_MARKER or not _owner_alive(owner):
            retire.append((other, owner))
    record_error = _retire_torn(project_root, session_id, torn)
    # Sampled around every command, not once around the batch: each row carries the
    # identity its own command ran on, None when that command changed it (`run_tests`).
    start = _identity(project_root)
    first = [start]

    def identity():
        return first.pop() if first else _identity(project_root)

    outcomes = run_tests(project_root, {"commands": commands}, identity=identity)
    ran = [o for o in outcomes if o["status"] != "refused"]
    bound = {id(o): o.pop("fingerprint", None) for o in ran}
    # The batch's own identity only when no command changed it: nothing it proved is
    # reusable on inputs that no longer exist.
    fingerprint = start if all(bound[id(o)] == start for o in ran) else None
    trusted = True  # False once a failure below leaves the rows unreadable or withheld
    path = runs_path(project_root, session_id)
    rows = "".join(
        json.dumps({
            "command": outcome["command"], "status": outcome["status"],
            "seconds": outcome.get("seconds"), "fingerprint": bound[id(outcome)], "at": _now(),
            "fingerprint_reason": outcome.get("fingerprint_reason"),
        }) + "\n"
        for outcome in ran
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(rows)
    except OSError as exc:
        # The marker stays: it is what keeps an older pass from being reused.
        record_error = "; ".join(filter(None, [record_error, f"record not written: {type(exc).__name__}: {exc}"]))
        fingerprint, trusted = None, False
        try:
            path.unlink(missing_ok=True)
        except OSError as unlink_exc:
            record_error += f"; stale record not removed: {type(unlink_exc).__name__}"
    else:
        recorded = {o["command"] for o in outcomes if o["status"] != "refused"}
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:
            # Left behind, it withholds these commands until a later run records them: safe.
            record_error = "; ".join(filter(None, [record_error, f"pending marker not removed: {type(exc).__name__}"]))
            fingerprint, trusted = None, False
        # A run that was dead before this one started, or a verify failure recorded as a
        # marker before it, is cleared once every command it named has a row from this run,
        # which is then provably the newer outcome.
        for other, owner in retire:
            if set(owner["commands"]) <= recorded:
                try:
                    other.unlink(missing_ok=True)
                except OSError:
                    pass
    if record_error and not os.access(path, os.W_OK):
        fingerprint, trusted = None, False
    return {"outcomes": outcomes, "fingerprint": fingerprint,
            "reusable": _reported_reusable(project_root, session_id, ran, bound) if trusted else [],
            "record_error": record_error}


def _reported_reusable(project_root, session_id: str, ran: list[dict], bound: dict) -> list[str]:
    """What a later verify may reuse from this run, by the rules `reusable` applies: the
    command's latest outcome passed, bound to the state it ran on, and no marker left
    after this run withholds it (an unreadable one withholds all). Whether the project is
    still in that state when verify asks is not known here."""
    latest = {}
    for outcome in ran:
        latest[outcome["command"]] = outcome
    withheld: set[str] = set()
    for _marker, owner in _markers(project_root, session_id):
        if owner is None:
            return []
        withheld.update(owner["commands"])
    return [
        command for command, o in latest.items()
        if command not in withheld and o["status"] == "passed" and bound[id(o)]
    ]


def _marker_path(project_root, session_id: str) -> Path:
    import secrets

    return runs_path(project_root, session_id).with_name(
        f"test-runs.{os.getpid()}-{secrets.token_hex(4)}.pending"
    )


def _markers(project_root, session_id: str) -> list[tuple[Path, dict | None]]:
    """Every pending marker with its content; None for one that cannot be read as a
    marker (torn, foreign, or from an earlier build)."""
    found = []
    for path in sorted(runs_path(project_root, session_id).parent.glob("test-runs*.pending")):
        try:
            owner = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            owner = None
        if not (
            isinstance(owner, dict)
            and isinstance(owner.get("pid"), int)
            and isinstance(owner.get("commands"), list)
            and all(isinstance(c, str) for c in owner["commands"])
        ):
            owner = None
        found.append((path, owner))
    return found


def _owner_alive(owner: dict) -> bool:
    """False only when the marker's run is provably over: its pid is dead, or now hosts a
    different process than the recorded identity (a reused pid). A pid the OS cannot
    answer for (Windows access denied, any other query failure) counts as alive, so another
    run never clears a marker whose owner may still be running (osutil.process_state)."""
    pid = owner["pid"]
    if osutil.process_state(pid) == "dead":
        return False
    recorded = owner.get("identity")
    return recorded is None or osutil.process_identity(pid) in (None, recorded)


def _record_stamp(path: Path) -> tuple[int, int] | None:
    """The record's size and mtime: an append grows the size, so a row written between
    two stamps always shows. None when the record cannot be stat'ed."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def reusable(project_root, session_id: str) -> dict:
    """Command -> its latest recorded run, for each command whose latest run passed under
    the reuse identity the project has now. Empty when the identity cannot be computed, or
    when any line of the record cannot be read: a torn or foreign line may be the newer
    outcome of some command, so no row is trusted over it. A command named by any pending
    marker (`run_and_record`) is withheld, live run or crashed one: its newest outcome may
    not be in the record. A marker that cannot be read withholds everything, and so does a
    record that cannot be written (a run since then may have failed to append to it). A
    barrier row (`_retire_torn`) drops every row before it.

    Nothing is locked. Instead the markers and the record's size and mtime are taken again
    once the identity is in: a run that started (new marker) or recorded (the record grew)
    while this read went on returns nothing rather than a snapshot it may contradict. A run
    that starts after this returns is not seen; see the reference."""
    path = runs_path(project_root, session_id)
    if not path.is_file() or not os.access(path, os.W_OK):
        return {}
    markers = _markers(project_root, session_id)
    seen = _record_stamp(path)
    if seen is None:
        return {}
    withheld: set[str] = set()
    for _marker, owner in markers:
        if owner is None:
            return {}
        withheld.update(owner["commands"])
    latest: dict = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                return {}
            if isinstance(row, dict) and row.get("barrier"):
                latest.clear()
                continue
            if not isinstance(row, dict) or not isinstance(row.get("command"), str):
                return {}
            latest[row["command"]] = row
    except (OSError, UnicodeDecodeError):
        return {}
    if not any(row.get("status") == "passed" and row.get("fingerprint") for row in latest.values()):
        return {}
    current = _identity(project_root)
    if current is None:
        return {}
    if _markers(project_root, session_id) != markers or _record_stamp(path) != seen:
        return {}
    return {
        command: row for command, row in latest.items()
        if command not in withheld
        and row.get("status") == "passed" and row.get("fingerprint") == current
    }
