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
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from contextvars import ContextVar
from pathlib import Path

from exoma.core.evidence.contract import RUNTIME_GAP_PREFIX, RUNTIME_NO_TEST_ITEM
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


def _policy(project_root) -> tuple[list[list[str]], int]:
    try:
        config = read_json_file(workflow_paths(Path(project_root))["config"])
    except (OSError, ValueError):
        config = {}
    commands = config.get("commands") if isinstance(config.get("commands"), dict) else {}
    prefixes = [
        _split(str(item)) for item in commands.get("verify_test_commands") or [] if str(item).strip()
    ]
    timeout = commands.get("verify_test_timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        timeout = DEFAULT_TIMEOUT_SECONDS
    return [p for p in prefixes if p], timeout


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
    path = _request_file(project_root, session_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"invalid": "tests.json is not valid JSON"}
    if not isinstance(data, dict):
        return {"invalid": "tests.json must be an object"}
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


def run_tests(project_root, request: dict, on_progress=None) -> list[dict]:
    """One outcome per requested command: passed | failed | timeout | error | refused."""
    prefixes, timeout = _policy(project_root)
    outcomes: list[dict] = []
    commands = request.get("commands")
    if not isinstance(commands, list):
        return [{"command": "", "status": "refused", "detail": "`commands` must be a list"}]
    run_started = time.monotonic()
    for index, raw in enumerate(commands[:MAX_COMMANDS]):
        command = str(raw).strip()
        tokens = _split(command)
        if not command or not tokens:
            outcomes.append({"command": command, "status": "refused", "detail": "empty or unparseable"})
            continue
        if _SHELL_SYNTAX.search(command):
            outcomes.append({"command": command, "status": "refused", "detail": "shell syntax is not run"})
            continue
        if _CONTROL_CHARS.search(command):
            outcomes.append({"command": command, "status": "refused", "detail": "control characters are not run"})
            continue
        if not _allowed(tokens, prefixes):
            outcomes.append({
                "command": command,
                "status": "refused",
                "detail": "not under commands.verify_test_commands in .workflow/config.json",
            })
            continue
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
        outcomes.append(outcome)
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
        if status == "passed":
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
        run_tests(project_root, request, on_progress)
        if request and not request.get("invalid")
        else []
    )
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
        "seconds": round(sum(o.get("seconds") or 0 for o in outcomes), 1),
    }
    return result
