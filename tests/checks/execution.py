"""The plan -> execute -y -> verify chain as runtime state (core/runtime/execution.py).

Every rule here used to be prompt-only. What these checks pin is the part that is now a
property of files a hook can read: a plan cannot be recorded as `proceed` while it has
open questions, an execution cannot open without the user's own `-y`, and the verdict a
verify returns is what the execution carries — never `pass` for a syntax-only check.
"""

import datetime
import json
import shutil
import tempfile
from pathlib import Path

import main
from core.runtime import execution
from core.workspace.workspace_paths import workflow_paths
from tests.checks.support import assert_true


def _plan(**overrides) -> dict:
    doc = {
        "task": "fix the anchor count",
        "decision": "proceed",
        "confidence": {"problem_understanding": "high", "root_cause": "high", "solution_path": "high"},
        "open_questions": [],
        "options": [{"name": "A", "plus": "small", "minus": "touches the executor", "recommended": True}],
        "files": ["core/evidence/evidence_store.py", "tests/checks/"],
    }
    doc.update(overrides)
    return doc


def _token(root: Path, session: str, age_seconds: float = 0) -> None:
    runtime = workflow_paths(root, session)["runtime_dir"]
    runtime.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age_seconds)
    (runtime / execution.EXECUTE_TOKEN_FILE).write_text(json.dumps({"set_at": stamp.isoformat()}), encoding="utf-8")


def _plan_is_checked_mechanically() -> None:
    assert_true(execution.validate_plan(_plan()) == [], f"a complete plan validates: {execution.validate_plan(_plan())}")
    cases = {
        "open questions": _plan(open_questions=["1. which rollout?"]),
        "unsure path": _plan(confidence={"problem_understanding": "high", "root_cause": "high", "solution_path": "medium"}),
        "no scope": _plan(files=[]),
        "two recommendations": _plan(options=[
            {"name": "A", "minus": "x", "recommended": True}, {"name": "B", "minus": "y", "recommended": True}]),
        "an option with no cost": _plan(options=[{"name": "A", "minus": "", "recommended": True}]),
        "scope outside the root": _plan(files=["../other/x.py"]),
        "absolute scope": _plan(files=["E:/elsewhere/x.py"]),
        "no decision": _plan(decision="maybe"),
    }
    for label, doc in cases.items():
        assert_true(execution.validate_plan(doc), f"refused: {label}")
    clarify = _plan(decision="clarify", open_questions=["1. which?"], files=[])
    assert_true(execution.validate_plan(clarify) == [], "a clarify plan may carry its questions and no scope")


def _execution_needs_the_users_yes() -> None:
    root = Path(tempfile.mkdtemp(prefix="execution-chain-"))
    session = "exec-session"
    try:
        (root / ".workflow").mkdir()
        refused = execution.begin_execution(root, session)
        assert_true(refused["reason"] == "no_plan", f"no plan, no execution: {refused}")

        execution.record_plan(root, session, _plan(decision="clarify", open_questions=["1. x?"], files=[]))
        refused = execution.begin_execution(root, session)
        assert_true(refused["reason"] == "plan_not_proceed", f"a clarify plan does not execute: {refused}")

        recorded = execution.record_plan(root, session, _plan())
        assert_true(recorded["ok"], f"a proceed plan is recorded: {recorded}")
        refused = execution.begin_execution(root, session)
        assert_true(refused["reason"] == "not_approved", f"proceed alone is not approval: {refused}")
        _token(root, session, age_seconds=execution.TOKEN_TTL_SECONDS + 60)
        refused = execution.begin_execution(root, session)
        assert_true(refused["reason"] == "not_approved", f"a stale -y approves nothing: {refused}")

        _token(root, session)
        begun = execution.begin_execution(root, session)
        assert_true(
            begun["ok"] and begun["execution"]["status"] == "active"
            and begun["execution"]["verification"] == "not_run"
            and begun["execution"]["scope"] == ["core/evidence/evidence_store.py", "tests/checks/"],
            f"plan + fresh -y opens an execution scoped to the plan: {begun}",
        )
        again = execution.begin_execution(root, session)
        assert_true(again["reason"] == "not_approved", "one -y approves one execution")

        ended = execution.end_execution(root, session)
        assert_true(
            ended["ok"] and ended["execution"]["status"] == "implemented"
            and ended["execution"]["verification"] == "not_run",
            f"closing an execution says implemented, not verified: {ended}",
        )
        assert_true(not execution.end_execution(root, session)["ok"], "nothing left to close")

        execution.record_verification(root, session, {"ok": True, "meta": {"mode": "quick", "verdict": "pass"}})
        assert_true(
            execution.current_execution(root, session)["verification"] == "syntax_only",
            "a parse check is recorded as syntax_only, never as pass",
        )
        execution.record_verification(root, session, {"ok": True, "meta": {"verdict": "pass"}})
        assert_true(execution.current_execution(root, session)["verification"] == "pass", "a passing verify is recorded")
        execution.record_verification(root, session, {"ok": False, "meta": {}})
        assert_true(execution.current_execution(root, session)["verification"] == "incomplete",
                    "a failed call is incomplete, not a verdict")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _cli_speaks_the_error_contract() -> None:
    root = Path(tempfile.mkdtemp(prefix="execution-cli-"))
    try:
        (root / ".workflow").mkdir()
        plan_file = root / "plan.json"
        plan_file.write_text(json.dumps(_plan(open_questions=["1. x?"])), encoding="utf-8")
        result = main.run("plan-record", str(plan_file), "cli-session", str(root))
        assert_true(
            result["ok"] is False and result["meta"]["error_type"] == "plan_invalid"
            and "open question" in result["content"],
            f"plan-record refuses a proceed with open questions, as JSON: {result}",
        )
        plan_file.write_text(json.dumps(_plan()), encoding="utf-8")
        assert_true(main.run("plan-record", str(plan_file), "cli-session", str(root))["ok"], "and records a sound one")
        result = main.run("execute-begin", "", "cli-session", str(root))
        assert_true(result["meta"].get("error_type") == "execute_not_approved",
                    f"execute-begin names the missing -y: {result}")
        _token(root, "cli-session")
        assert_true(main.run("execute-begin", "", "cli-session", str(root))["ok"], "and opens with it")
        assert_true(main.run("execute-end", "", "cli-session", str(root))["ok"], "execute-end closes it")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_execution_chain() -> None:
    _plan_is_checked_mechanically()
    _execution_needs_the_users_yes()
    _cli_speaks_the_error_contract()
