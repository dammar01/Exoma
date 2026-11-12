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

Each step also snapshots the working tree as a Git tree id (core/evidence/tree_fingerprint):
`base_tree` on the plan, `begin_tree` and `end_tree` on the execution. Comparing two
snapshots isolates what changed between them from what the tree already carried, so an
execution on a tree with uncommitted edits still knows which files it changed
(`changed_files`), which of those fall outside the plan's scope (`outside_scope`, written
through any tool, Bash included, which the edit hook never sees), what changed between
the plan and execute-begin (`changed_since_plan`), and, at verify, what changed after
execute-end (`changed_since_execute`). A snapshot that cannot be taken or compared (no
Git, a tree `git gc` pruned) is `null`: unknown, never "nothing changed".
"""

from __future__ import annotations

import datetime
import contextlib
import functools
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

from exoma.core.workspace.workspace_paths import atomic_write_text, now_iso, workflow_paths
from exoma.utils import osutil

_TRANSACTIONS = threading.local()
_UNSPECIFIED = object()


@contextlib.contextmanager
def _execution_transaction(project_root, session_id):
    """Same byte-lock protocol as standalone workflow_policy.execution_transaction."""
    path = _runtime_file(project_root, session_id, "execution.state.guard")
    key = os.path.normcase(os.path.realpath(path))
    held = getattr(_TRANSACTIONS, "held", set())
    if key in held:
        yield
        return
    deadline = time.monotonic() + 10
    handle = None
    locked = False
    try:
        while True:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = osutil.open_lock_file(path)
            try:
                osutil.lock_file(handle)
                locked = True
                named, opened = path.stat(), os.fstat(handle.fileno())
                if osutil.IS_WINDOWS or (named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino):
                    break
            except OSError:
                pass
            if locked:
                osutil.unlock_file(handle)
                locked = False
            handle.close()
            handle = None
            if time.monotonic() >= deadline:
                raise TimeoutError("execution state transaction is busy; retry after the active mutation")
            time.sleep(0.02)
        _TRANSACTIONS.held = held | {key}
        yield
    finally:
        _TRANSACTIONS.held = held
        if handle is not None:
            try:
                if locked:
                    osutil.unlock_file(handle)
            finally:
                handle.close()


def _transactional(function):
    @functools.wraps(function)
    def guarded(project_root, session_id, *args, **kwargs):
        if function.__name__ in ("end_execution", "record_verification") and "expected" not in kwargs:
            kwargs["expected"] = execution_identity(current_execution(project_root, session_id))
        with _execution_transaction(project_root, session_id):
            return function(project_root, session_id, *args, **kwargs)
    return guarded

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


def _tree(project_root: Path) -> str | None:
    from exoma.core.evidence.tree_fingerprint import tree_fingerprint

    try:
        return tree_fingerprint(project_root)
    except (OSError, ValueError):
        return None


def _changed(project_root: Path, old, new) -> list[dict] | None:
    from exoma.core.evidence.tree_fingerprint import changed_paths

    try:
        return changed_paths(project_root, old, new)
    except (OSError, ValueError):
        return None


def in_scope(relative: str, scope, casefold: bool | None = None) -> bool:
    """The execution gate's rule (hooks/workflow_policy.in_scope): an entry names the file,
    a directory above it, or a glob matching it. On Windows (`casefold` defaults to it)
    both sides are lowercased first, as the gate does, so `SRC/` covers `src/x.py`."""
    import fnmatch
    import os

    if casefold is None:
        casefold = os.name == "nt"
    if casefold:
        relative = relative.lower()
    for entry in scope or []:
        entry = str(entry).strip().replace("\\", "/")
        if casefold:
            entry = entry.lower()
        if not entry:
            continue
        stem = entry.rstrip("/")
        if relative == stem or relative.startswith(stem + "/") or fnmatch.fnmatch(relative, entry):
            return True
    return False


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


@_transactional
def record_plan(project_root: Path, session_id: str, doc) -> dict:
    """Validate and store the session's plan. A new plan closes no execution by itself."""
    errors = validate_plan(doc)
    if errors:
        return {"ok": False, "errors": errors}
    stored = {
        **doc,
        "plan_id": _plan_id(doc),
        "recorded_at": now_iso(),
        "revision": uuid.uuid4().hex,
        "files": [entry.strip().replace("\\", "/") for entry in doc["files"]],
        "base_tree": _tree(project_root),
    }
    try:
        _runtime_file(project_root, session_id, EXECUTE_TOKEN_FILE).unlink(missing_ok=True)
    except OSError as exc:
        return {"ok": False, "errors": [f"cannot revoke the preceding approval: {exc}"]}
    _write(_runtime_file(project_root, session_id, PLAN_FILE), stored)
    return {"ok": True, "plan_id": stored["plan_id"], "decision": stored["decision"]}


def current_plan(project_root: Path, session_id: str) -> dict | None:
    return _read(_runtime_file(project_root, session_id, PLAN_FILE))


def approval_binding(project_root: Path, session_id: str, plan: dict | None = None) -> dict | None:
    """Execution-relevant snapshot, also produced by the standalone shared host hook.

    Include the runtime location and revision as well as the document ID: identical
    re-records and copied approvals in another session must not authorize this plan.
    """
    plan = current_plan(project_root, session_id) if plan is None else plan
    if not isinstance(plan, dict) or plan.get("decision") != "proceed":
        return None
    if not all(isinstance(plan.get(key), str) and plan[key] for key in ("plan_id", "recorded_at", "task")):
        return None
    files = plan.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(p, str) and p for p in files):
        return None
    return {
        "plan_id": plan["plan_id"], "recorded_at": plan["recorded_at"],
        "revision": plan.get("revision") or plan["recorded_at"],
        "task": plan["task"], "files": files,
        "runtime": os.path.normcase(os.path.realpath(_runtime_file(project_root, session_id, PLAN_FILE).parent)),
    }


def _token_age(token: dict) -> float | None:
    try:
        set_at = datetime.datetime.fromisoformat(str(token.get("set_at")))
    except ValueError:
        return None
    if set_at.tzinfo is None:
        set_at = set_at.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - set_at).total_seconds()


@_transactional
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
    binding = approval_binding(project_root, session_id, plan)
    if (token is None or age is None or age < 0 or age > TOKEN_TTL_SECONDS
            or binding is None or token.get("plan_binding") != binding):
        return {
            "ok": False,
            "reason": "not_approved",
            "error": (
                "no fresh user approval bound to this plan revision (invoke the execute skill with -y)"
                + (f" (the last one is {int(age)}s old)" if age is not None and age > 0 else "")
            ),
        }
    # Atomically claim the token: two begin calls cannot both consume the same approval.
    claimed = token_path.with_name(f".execute-token-{uuid.uuid4().hex}.used")
    try:
        token_path.replace(claimed)
    except OSError:
        return {"ok": False, "reason": "not_approved", "error": "approval could not be consumed"}
    try:
        if _read(claimed) != token:
            return {"ok": False, "reason": "not_approved", "error": "approval changed before consumption"}
        begin_tree = _tree(project_root)
        if approval_binding(project_root, session_id) != binding:
            return {"ok": False, "reason": "not_approved", "error": "plan changed while opening execution"}
    finally:
        try:
            claimed.unlink(missing_ok=True)
        except OSError:
            pass  # A consumed private file is never accepted as execute.token.
    execution = {
        "execution_id": uuid.uuid4().hex,
        "plan_revision": binding["revision"],
        "plan_id": plan.get("plan_id"),
        "task": plan.get("task"),
        "scope": list(plan.get("files") or []),
        "status": STATUS_ACTIVE,
        "verification": VERIFICATION_NOT_RUN,
        "started_at": now_iso(),
        "approved_at": token.get("set_at"),
        "begin_tree": begin_tree,
        "changed_since_plan": _changed(project_root, plan.get("base_tree"), begin_tree),
    }
    _write(_runtime_file(project_root, session_id, EXECUTION_FILE), execution)
    return {"ok": True, "execution": execution}


@_transactional
def end_execution(project_root: Path, session_id: str, *, expected=_UNSPECIFIED) -> dict:
    path = _runtime_file(project_root, session_id, EXECUTION_FILE)
    execution = _read(path)
    if expected is not _UNSPECIFIED and execution_identity(execution) != expected:
        return {"ok": False, "reason": "not_active", "error": "execution was replaced while closing it"}
    if execution is None or execution.get("status") != STATUS_ACTIVE:
        return {"ok": False, "reason": "not_active", "error": "no active execution for this session"}
    end_tree = _tree(project_root)
    changed = _changed(project_root, execution.get("begin_tree"), end_tree)
    scope = execution.get("scope") or []
    execution.update(
        status=STATUS_IMPLEMENTED,
        ended_at=now_iso(),
        end_tree=end_tree,
        changed_files=changed,
        outside_scope=None if changed is None else [c["path"] for c in changed if not in_scope(c["path"], scope)],
    )
    _write(path, execution)
    return {"ok": True, "execution": execution}


def current_execution(project_root: Path, session_id: str) -> dict | None:
    return _read(_runtime_file(project_root, session_id, EXECUTION_FILE))


def execution_identity(execution: dict | None):
    if not isinstance(execution, dict):
        return None
    return execution.get("execution_id") or (execution.get("plan_id"), execution.get("started_at"), execution.get("approved_at"))


@_transactional
def record_verification(project_root: Path, session_id: str, result: dict, *, expected=_UNSPECIFIED) -> None:
    """Write a finished /.verify's verdict onto the session's execution, if there is one.

    The verdict is the runtime's (`meta.verdict`: pass | fail | incomplete), never the
    second agent's own line. A syntax-mode verify records `syntax_only`: a parse check
    is not evidence the change works, and the guard must not read it as `pass`.
    """
    path = _runtime_file(project_root, session_id, EXECUTION_FILE)
    execution = _read(path)
    if expected is not _UNSPECIFIED and execution_identity(execution) != expected:
        if isinstance(result, dict):
            result.setdefault("meta", {})["execution_state_error"] = "stale verification: execution was replaced; verdict not attached"
        return
    if execution is None or not isinstance(result, dict):
        return
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    if meta.get("mode") == "quick" or "quick_verify" in meta:
        verdict = "syntax_only"
    else:
        verdict = meta.get("verdict")
    if not result.get("ok") or verdict not in ("pass", "fail", "incomplete", "syntax_only"):
        verdict = "incomplete"
    # Edits after execute-end are not the execution's: name them beside the verdict.
    changed = (
        _changed(project_root, execution.get("end_tree"), _tree(project_root))
        if execution.get("status") == STATUS_IMPLEMENTED else None
    )
    execution.update(verification=verdict, verified_at=now_iso(), changed_since_execute=changed)
    _write(path, execution)
    if changed and isinstance(result.get("meta"), dict):
        result["meta"]["changed_since_execute"] = changed
