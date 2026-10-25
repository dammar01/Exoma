"""The runtime half of /.plan -> /.execute -y -> /.verify.

Until v3.9.0 every rule of that chain lived in prompts only. `/.execute` had no Python
path at all, so "edit only with an approved plan", "only after the user typed -y" and
"never say done before verify" were things the main agent was asked to remember and
nothing could check. DEC-046 deferred hook enforcement because a hook reading prose
would have false positives; this module is what removes the need to read prose. The
main agent records its plan here, the user's `-y` arrives as a token the prompt hook
writes, and execution becomes a state the hooks can read:

    plan-record    validate a [PLAN] the main agent wrote to a JSON file; refuse a plan
                   that claims `proceed` while it still has open questions or an
                   unsure solution path; store it as the session's plan
    execute-begin  require that plan, decision `proceed`, and a fresh `/.execute -y`
                   token from the user's own prompt; consume the token; open an
                   execution whose scope is the plan's file list
    execute-end    close it as `implemented`; verification stays `not_run`
    (verify)       a finished /.verify writes its verdict onto the execution

Files live in the session's runtime directory, beside delegated.marker, because the
hooks that read them are standalone scripts that know that directory and nothing else.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath

from exoma.core.workspace.workspace_paths import atomic_write_text, now_iso, workflow_paths

PLAN_FILE = "plan.json"
EXECUTION_FILE = "execution.json"
# Written by the UserPromptSubmit hook when the user's own prompt is `/.execute -y`.
EXECUTE_TOKEN_FILE = "execute.token"
# How long a `-y` stays good. Long enough for the agent to read its plan back and call
# execute-begin; short enough that a `-y` typed an hour ago does not approve whatever
# plan is current now. Not measured.
TOKEN_TTL_SECONDS = 900

DECISIONS = ("proceed", "clarify", "re-explore")
LEVELS = ("low", "medium", "high")
CONFIDENCE_KEYS = ("problem_understanding", "root_cause", "solution_path")
MAX_OPTIONS = 3

STATUS_ACTIVE = "active"
STATUS_IMPLEMENTED = "implemented"
VERIFICATION_NOT_RUN = "not_run"


def _runtime_file(project_root: Path, session_id: str, name: str) -> Path:
    return workflow_paths(Path(project_root), session_id)["runtime_dir"] / name


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _scope_entry_error(entry) -> str | None:
    if not isinstance(entry, str) or not entry.strip():
        return "files: every entry must be a non-empty path string"
    pure = PurePosixPath(entry.strip().replace("\\", "/"))
    if pure.is_absolute() or (len(entry) > 1 and entry[1] == ":"):
        return f"files: {entry!r} must be relative to the project root"
    if ".." in pure.parts:
        return f"files: {entry!r} must not climb out of the project root"
    return None


def validate_plan(doc) -> list[str]:
    """Every reason this plan cannot be recorded. Empty means it can.

    The mechanical half of the plan contract — the part that is a property of the
    document, not of its reasoning. Whether the steps are right is still the main
    agent's and the user's judgment; whether a `proceed` sits on open questions is not.
    """
    if not isinstance(doc, dict):
        return ["plan: expected a JSON object"]
    errors: list[str] = []
    if not isinstance(doc.get("task"), str) or not doc["task"].strip():
        errors.append("task: required, a one-line restatement")
    decision = doc.get("decision")
    if decision not in DECISIONS:
        errors.append(f"decision: one of {', '.join(DECISIONS)}, got {decision!r}")

    confidence = doc.get("confidence")
    if not isinstance(confidence, dict):
        errors.append("confidence: object with " + ", ".join(CONFIDENCE_KEYS))
        confidence = {}
    for key in CONFIDENCE_KEYS:
        level = confidence.get(key)
        if level not in LEVELS:
            errors.append(f"confidence.{key}: one of {', '.join(LEVELS)}, got {level!r}")

    questions = doc.get("open_questions")
    if not isinstance(questions, list) or not all(isinstance(q, str) for q in questions):
        errors.append("open_questions: list of strings (empty when there are none)")
        questions = []

    options = doc.get("options")
    if not isinstance(options, list) or not 1 <= len(options) <= MAX_OPTIONS:
        errors.append(f"options: 1 to {MAX_OPTIONS} entries, the [OPTIONS] block")
        options = []
    recommended = 0
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            errors.append(f"options[{index}]: expected an object")
            continue
        if not str(option.get("name") or "").strip():
            errors.append(f"options[{index}].name: required")
        if not str(option.get("minus") or "").strip():
            errors.append(f"options[{index}].minus: required — an option without a cost was not thought through")
        if option.get("recommended") is True:
            recommended += 1
    if options and recommended != 1:
        errors.append(f"options: exactly one recommended, got {recommended}")

    files = doc.get("files")
    if not isinstance(files, list):
        errors.append("files: list of project-relative paths, the execution scope")
        files = []
    for entry in files:
        problem = _scope_entry_error(entry)
        if problem:
            errors.append(problem)

    if decision == "proceed":
        # The mechanical decision rule, enforced where it can no longer be talked past.
        if questions:
            errors.append(f"decision: proceed with {len(questions)} open question(s); answer them first")
        if confidence.get("solution_path") != "high":
            errors.append("decision: proceed needs confidence.solution_path high")
        if not files:
            errors.append("files: proceed needs a non-empty execution scope")
    return errors


def _plan_id(doc: dict) -> str:
    encoded = json.dumps(doc, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def record_plan(project_root: Path, session_id: str, doc) -> dict:
    """Validate and store the session's plan. A new plan closes no execution by itself."""
    errors = validate_plan(doc)
    if errors:
        return {"ok": False, "errors": errors}
    stored = {
        **doc,
        "plan_id": _plan_id(doc),
        "recorded_at": now_iso(),
        "files": [entry.strip().replace("\\", "/") for entry in doc["files"]],
    }
    _write(_runtime_file(project_root, session_id, PLAN_FILE), stored)
    return {"ok": True, "plan_id": stored["plan_id"], "decision": stored["decision"]}


def current_plan(project_root: Path, session_id: str) -> dict | None:
    return _read(_runtime_file(project_root, session_id, PLAN_FILE))


def _token_age(token: dict) -> float | None:
    try:
        set_at = datetime.datetime.fromisoformat(str(token.get("set_at")))
    except ValueError:
        return None
    if set_at.tzinfo is None:
        set_at = set_at.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - set_at).total_seconds()


def begin_execution(project_root: Path, session_id: str) -> dict:
    """Open an execution, or say exactly which precondition is missing.

    Returns {"ok": True, "execution": {...}} or {"ok": False, "reason": <kind>, "error"}.
    The token is removed on success so one `-y` approves one execution.
    """
    plan = current_plan(project_root, session_id)
    if plan is None:
        return {"ok": False, "reason": "no_plan", "error": "no recorded plan for this session"}
    if plan.get("decision") != "proceed":
        return {
            "ok": False,
            "reason": "plan_not_proceed",
            "error": f"the recorded plan's decision is {plan.get('decision')!r}, not proceed",
        }
    token_path = _runtime_file(project_root, session_id, EXECUTE_TOKEN_FILE)
    token = _read(token_path)
    age = _token_age(token) if token else None
    if token is None or age is None or age < 0 or age > TOKEN_TTL_SECONDS:
        return {
            "ok": False,
            "reason": "not_approved",
            "error": (
                "no fresh `/.execute -y` from the user"
                + (f" (the last one is {int(age)}s old)" if age is not None and age > 0 else "")
            ),
        }
    execution = {
        "plan_id": plan.get("plan_id"),
        "task": plan.get("task"),
        "scope": list(plan.get("files") or []),
        "status": STATUS_ACTIVE,
        "verification": VERIFICATION_NOT_RUN,
        "started_at": now_iso(),
        "approved_at": token.get("set_at"),
    }
    _write(_runtime_file(project_root, session_id, EXECUTION_FILE), execution)
    try:
        token_path.unlink()
    except OSError:
        pass
    return {"ok": True, "execution": execution}


def end_execution(project_root: Path, session_id: str) -> dict:
    path = _runtime_file(project_root, session_id, EXECUTION_FILE)
    execution = _read(path)
    if execution is None or execution.get("status") != STATUS_ACTIVE:
        return {"ok": False, "reason": "not_active", "error": "no active execution for this session"}
    execution.update(status=STATUS_IMPLEMENTED, ended_at=now_iso())
    _write(path, execution)
    return {"ok": True, "execution": execution}


def current_execution(project_root: Path, session_id: str) -> dict | None:
    return _read(_runtime_file(project_root, session_id, EXECUTION_FILE))


def record_verification(project_root: Path, session_id: str, result: dict) -> None:
    """Write a finished /.verify's verdict onto the session's execution, if there is one.

    The verdict is the runtime's (`meta.verdict`: pass | fail | incomplete), never the
    second agent's own line. A syntax-mode verify records `syntax_only`: a parse check
    is not evidence the change works, and the guard must not read it as `pass`.
    """
    path = _runtime_file(project_root, session_id, EXECUTION_FILE)
    execution = _read(path)
    if execution is None or not isinstance(result, dict):
        return
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    if meta.get("mode") == "quick" or "quick_verify" in meta:
        verdict = "syntax_only"
    else:
        verdict = meta.get("verdict")
    if not result.get("ok") or verdict not in ("pass", "fail", "incomplete", "syntax_only"):
        verdict = "incomplete"
    execution.update(verification=verdict, verified_at=now_iso())
    _write(path, execution)
