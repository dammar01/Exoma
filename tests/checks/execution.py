"""The plan -> execute -y -> verify chain as runtime state (core/runtime/execution.py).

Every rule here used to be prompt-only. What these checks pin is the part that is now a
property of files a hook can read: a plan cannot be recorded as `proceed` while it has
open questions, an execution cannot open without the user's own `-y`, and the verdict a
verify returns is what the execution carries — never `pass` for a syntax-only check.
"""

import datetime
import json
import os
import shutil
import tempfile
from pathlib import Path

from exoma import cli as main
from exoma.core.runtime import execution
from exoma.core.workspace.workspace_paths import workflow_paths
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


def _execution_tracks_its_own_edits() -> None:
    """Tree snapshots on plan, begin and end name the files an execution changed on a tree
    that already carried uncommitted edits, flag those outside the scope (whatever tool
    wrote them), and name edits made after execute-end at verify. No Git: unknown."""
    import subprocess
    import unicodedata

    from tests.checks.support import no_console

    root = Path(tempfile.mkdtemp(prefix="execution-tree-"))
    session = "tree-session"
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    try:
        (root / ".workflow").mkdir()
        subprocess.run([*git, "init", "-q"], check=True, **no_console())
        (root / "src").mkdir()
        (root / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
        (root / "dirty.py").write_text("d = 1\n", encoding="utf-8")
        subprocess.run([*git, "add", "-A"], check=True, **no_console())
        subprocess.run([*git, "commit", "-qm", "base"], check=True, **no_console())
        (root / "dirty.py").write_text("d = 2\n", encoding="utf-8")  # uncommitted before the plan

        execution.record_plan(root, session, _plan(files=["src/"]))
        assert_true(execution.current_plan(root, session).get("base_tree"), "the plan records its tree")
        (root / "between.py").write_text("b = 1\n", encoding="utf-8")
        _token(root, session)
        begun = execution.begin_execution(root, session)["execution"]
        assert_true(begun["begin_tree"] and [c["path"] for c in begun["changed_since_plan"]] == ["between.py"],
                    f"an edit between plan and execute-begin is named: {begun}")

        (root / "src" / "x.py").write_text("x = 2\n", encoding="utf-8")
        (root / "src" / "new.py").write_text("n = 1\n", encoding="utf-8")
        (root / "src" / "café.py").write_text("c = 1\n", encoding="utf-8")  # non-ASCII, in scope
        (root / "outside.py").write_text("o = 1\n", encoding="utf-8")  # as a Bash write would
        ended = execution.end_execution(root, session)["execution"]
        changed = {unicodedata.normalize("NFC", c["path"]): c["status"] for c in ended["changed_files"]}
        assert_true(changed == {"src/x.py": "M", "src/new.py": "A", "src/café.py": "A", "outside.py": "A"},
                    f"only the execution's own edits, not the tree's earlier ones: {changed}")
        assert_true(ended["outside_scope"] == ["outside.py"],
                    f"outside the scope is flagged, a non-ASCII name inside it is not: {ended}")
        assert_true(execution.in_scope("src/x.py", ["SRC/"], casefold=True)
                    and not execution.in_scope("src/x.py", ["SRC/"], casefold=False),
                    "scope case: ignored where the gate ignores it (Windows), kept elsewhere")
        assert_true(execution.in_scope("src/x.py", ["SRC/"]) == (os.name == "nt"),
                    "the default follows the platform, as the execution gate does")

        (root / "late.py").write_text("l = 1\n", encoding="utf-8")
        result = {"ok": True, "meta": {"verdict": "pass"}}
        execution.record_verification(root, session, result)
        late = [c["path"] for c in result["meta"].get("changed_since_execute") or []]
        assert_true(late == ["late.py"]
                    and [c["path"] for c in execution.current_execution(root, session)["changed_since_execute"]] == late,
                    f"an edit after execute-end is named at verify: {result}")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    bare = Path(tempfile.mkdtemp(prefix="execution-nogit-"))
    try:
        (bare / ".workflow").mkdir()
        execution.record_plan(bare, session, _plan())
        _token(bare, session)
        execution.begin_execution(bare, session)
        ended = execution.end_execution(bare, session)["execution"]
        assert_true(ended["changed_files"] is None and ended["outside_scope"] is None,
                    f"no Git: unknown, never an empty change list: {ended}")
    finally:
        shutil.rmtree(bare, ignore_errors=True)


def _test_execution_chain() -> None:
    _plan_is_checked_mechanically()
    _execution_needs_the_users_yes()
    _cli_speaks_the_error_contract()
    _execution_tracks_its_own_edits()
