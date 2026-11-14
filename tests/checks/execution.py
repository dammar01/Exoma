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
    (runtime / execution.EXECUTE_TOKEN_FILE).write_text(json.dumps({
        "set_at": stamp.isoformat(), "plan_binding": execution.approval_binding(root, session)
    }), encoding="utf-8")


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
        _token(root, session, age_seconds=-60)
        assert_true(execution.begin_execution(root, session)["reason"] == "not_approved",
                    "future-dated approval is not fresh approval")

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
        config = root / ".workflow" / "config.json"
        config.write_text('{"commands":{"auto_verify_after_execute":true,"verify_mode":"syntax"}}', encoding="utf-8")
        result = main.run("execute-end", "", "cli-session", str(root))
        closed = execution.current_execution(root, "cli-session")
        assert_true(result["ok"] and all(result["meta"][key] == value for key, value in closed.items()),
                    "execute-end preserves every existing execution field")
        assert_true(result["meta"]["policy"] == {"auto_verify_after_execute": True, "verify_mode": "syntax"}
                    and result["meta"]["next_step"] == {
                        "action": "verify_prepare", "required": True, "session_id": "cli-session",
                        "execution_id": closed["execution_id"], "automatic": False}
                    and closed["verification"] == "not_run", "fresh policy directs the agent without dispatching verify")
        assert_true("policy" not in closed and "next_step" not in closed, "response policy is not persisted as execution state")
        _token(root, "cli-session")
        assert_true(main.run("execute-begin", "", "cli-session", str(root))["ok"], "another execution opens")
        config.write_text('{"commands":{"auto_verify_after_execute":false}}', encoding="utf-8")
        result = main.run("execute-end", "", "cli-session", str(root))
        assert_true(result["meta"]["next_step"]["action"] == "offer_verify"
                    and result["meta"]["next_step"]["required"] is False, "policy is reread for each close")
        _token(root, "cli-session")
        main.run("execute-begin", "", "cli-session", str(root))
        config.write_text("{", encoding="utf-8")
        result = main.run("execute-end", "", "cli-session", str(root))
        assert_true(result["ok"] and result["meta"]["config_warnings"]
                    and result["meta"]["next_step"]["action"] == "offer_verify",
                    "broken config cannot turn a successful close into a close failure")
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
    _approval_cannot_follow_a_replaced_plan()
    _approval_runtime_identity()
    _cli_speaks_the_error_contract()
    _execution_tracks_its_own_edits()
    _concurrent_execution_publication()
    _stale_approval_preserves_new_token()
    _two_sessions_do_not_share_the_guard()
    _two_active_executions_keep_their_own_scope()


def _approval_runtime_identity() -> None:
    import threading
    from unittest.mock import patch

    with tempfile.TemporaryDirectory(prefix="execution-alias-") as tmp:
        root, session = Path(tmp), "alias"
        execution.record_plan(root, session, _plan())
        runtime = workflow_paths(root, session)["runtime_dir"]
        binding = execution.approval_binding(root, session)
        # Keep '..' in the token's text: both names really stat to one directory,
        # without needing symlink privileges or a case-insensitive test machine.
        alias = str(runtime / ".." / runtime.name)
        assert_true(alias != binding["runtime"] and Path(alias).samefile(runtime),
                    "the regression fixture supplies different text for a real directory alias")
        assert_true(execution._approval_matches({**binding, "runtime": alias}, binding),
                    "physical directory identity accepts a real path alias")

        for field, replacement in (
                ("plan_id", "other"), ("recorded_at", "other"), ("revision", "other"),
                ("task", "other"), ("files", ["other.py"]), ("extra", True)):
            with patch.object(Path, "stat", side_effect=AssertionError("metadata mismatch must not query identity")):
                assert_true(not execution._approval_matches({**binding, field: replacement}, binding),
                            "non-path binding mismatch is rejected before filesystem queries: " + field)
        for invalid in (None, [], {}, {key: value for key, value in binding.items() if key != "revision"}):
            assert_true(not execution._approval_matches(invalid, binding), "malformed binding remains unapproved")

        other, file = root / "Runtime", root / "not-directory"
        other.mkdir()
        file.write_text("fixture", encoding="utf-8")
        token = runtime / execution.EXECUTE_TOKEN_FILE
        for location in (str(other), str(root / "missing"), str(file), "runtime", "", None, 42, str(root) + "/\0"):
            _token(root, session)
            payload = json.loads(token.read_text())
            payload["plan_binding"]["runtime"] = location
            token.write_text(json.dumps(payload), encoding="utf-8")
            before = token.read_bytes()
            assert_true(execution.begin_execution(root, session).get("reason") == "not_approved"
                        and token.read_bytes() == before,
                        "unverifiable/different runtime rejects without consuming approval: " + repr(location))
        for error in (PermissionError("fixture identity denied"), OSError("fixture identity unknown"),
                      ValueError("fixture invalid path")):
            _token(root, session)
            before = token.read_bytes()
            with patch.object(Path, "samefile", side_effect=error):
                assert_true(execution.begin_execution(root, session).get("reason") == "not_approved",
                            "a failed identity query cannot authorize execution")
            assert_true(token.read_bytes() == before, "identity error leaves the initial approval untouched")

        # Hermetic case-only alias simulation: acceptance still needs physical
        # proof. It is not permission to lowercase two POSIX directory names.
        folded = str(runtime.parent / "RUNTIME")
        for matches in (True, False):
            with patch.object(Path, "is_dir", return_value=True), patch.object(Path, "samefile", return_value=matches) as probe:
                assert_true(execution._approval_matches({**binding, "runtime": folded}, binding) is matches,
                            "case-only alias follows actual identity proof, never text similarity")
                probe.assert_called_once()

        _token(root, session)
        payload = json.loads(token.read_text())
        payload["plan_binding"]["runtime"] = alias
        token.write_text(json.dumps(payload), encoding="utf-8")
        plan_bytes = (runtime / execution.PLAN_FILE).read_bytes()
        begun = execution.begin_execution(root, session)
        assert_true(begun["ok"] and not token.exists() and (runtime / execution.PLAN_FILE).read_bytes() == plan_bytes,
                    "real alias consumes one approval without rewriting the recorded plan")
        assert_true(execution.begin_execution(root, session).get("reason") == "not_approved",
                    "accepted alias does not make approval replayable")

        # Both callers target the same transaction file, including via an alias.
        _token(root, session)
        payload = json.loads(token.read_text())
        payload["plan_binding"]["runtime"] = alias
        token.write_text(json.dumps(payload), encoding="utf-8")
        barrier, results, errors = threading.Barrier(2), [], []
        def consume(project):
            try:
                barrier.wait(timeout=3)
                results.append(execution.begin_execution(project, session))
            except BaseException as exc:
                errors.append(exc)
        workers = [threading.Thread(target=consume, args=(project,))
                   for project in (root, root / ".." / root.name)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(8)
        assert_true(not errors and len(results) == 2 and sum(result["ok"] for result in results) == 1,
                    "concurrent alias consumers can claim one approval only once")

    # Supplemental native case-only proof on the checkout's filesystem (WSL's
    # /mnt drive, Windows or a case-insensitive macOS volume). Core regressions
    # above always run, including on case-sensitive Linux.
    with tempfile.TemporaryDirectory(prefix=".execution-case-", dir=Path(__file__).resolve().parents[2]) as tmp:
        root = Path(tmp) / "CaseProject"
        root.mkdir()
        alias_root = root.with_name("caseproject")
        if alias_root.is_dir() and root.samefile(alias_root):
            with patch.object(execution, "_tree", return_value=None):
                execution.record_plan(root, "case", _plan())
                _token(root, "case")
                token = workflow_paths(root, "case")["runtime_dir"] / execution.EXECUTE_TOKEN_FILE
                plan_bytes = (token.parent / execution.PLAN_FILE).read_bytes()
                assert_true(execution.begin_execution(alias_root, "case")["ok"] and not token.exists()
                            and (token.parent / execution.PLAN_FILE).read_bytes() == plan_bytes,
                            "native filesystem case-only alias accepts the same revision exactly once")


def _concurrent_execution_publication() -> None:
    import threading
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix="execution-concurrent-") as tmp:
        root, session = Path(tmp), "concurrent"
        execution.record_plan(root, session, _plan(files=["a.py"]))
        _token(root, session)
        publishing, release, second_started, second_done = (threading.Event() for _ in range(4))
        original_write = execution._write
        results, errors = [], []
        def write(path, value):
            if path.name == execution.EXECUTION_FILE and value.get("scope") == ["a.py"]:
                publishing.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release publication")
            original_write(path, value)
        def first():
            try:
                results.append(execution.begin_execution(root, session))
            except BaseException as exc:
                errors.append(exc)
        def second():
            second_started.set()
            try:
                execution.record_plan(root, session, _plan(files=["b.py"]))
                _token(root, session)
                results.append(execution.begin_execution(root, session))
            except BaseException as exc:
                errors.append(exc)
            finally:
                second_done.set()
        with patch.object(execution, "_write", side_effect=write):
            a, b = threading.Thread(target=first), threading.Thread(target=second)
            a.start()
            try:
                assert_true(publishing.wait(5), "first begin reached publication")
                b.start()
                assert_true(second_started.wait(2) and not second_done.wait(0.1),
                            "the second mutation cannot publish through the first transaction")
            finally:
                release.set()
                a.join(8)
                if b.ident is not None:
                    b.join(8)
        current = execution.current_execution(root, session)
        assert_true(not errors and len(results) == 2 and all(r["ok"] for r in results)
                    and current["scope"] == ["b.py"], "an older publication cannot overwrite the newer begin")
        old_id = next(r["execution"]["execution_id"] for r in results if r["execution"]["scope"] == ["a.py"])
        result = {"ok": True, "meta": {"verdict": "pass"}}
        execution.record_verification(root, session, result, expected=old_id)
        stale_end = execution.end_execution(root, session, expected=old_id)
        assert_true(result["meta"].get("execution_state_error") and not stale_end["ok"]
                    and execution.current_execution(root, session)["verification"] == "not_run",
                    "stale end/verify cannot modify the replacement execution")
        _token(root, session)
        barrier = threading.Barrier(2)
        results.clear()
        def consume():
            barrier.wait(timeout=3)
            results.append(execution.begin_execution(root, session))
        workers = [threading.Thread(target=consume) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(8)
        assert_true(len(results) == 2 and sum(r["ok"] for r in results) == 1,
                    "parallel begin consumers can use one approval only once")


def _stale_approval_preserves_new_token() -> None:
    import importlib.util
    from unittest.mock import patch
    source = Path(__file__).resolve().parents[2] / "src/exoma/_bundle/config/hosts/claude/hooks/workflow_policy.py"
    spec = importlib.util.spec_from_file_location("execution_policy_check", source)
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    with tempfile.TemporaryDirectory(prefix="approval-stale-") as tmp:
        token = Path(tmp) / "execute.token"
        newer = '{"newer": true}'
        with patch.object(policy, "approval_plan_binding", side_effect=[{"revision": "old"}, {"revision": "new"}]):
            token.write_text(newer, encoding="utf-8")
            policy.record_approvals("/.execute -y", tmp, True, {})
        assert_true(token.read_text(encoding="utf-8") == newer,
                    "a stale writer may discard its staging file, never somebody else's new approval")
        # Standalone hooks and the runtime must agree on the same OS lock, not just
        # each exclude their own threads independently.
        import threading
        acquired = threading.Event()
        root = Path(tmp) / "project"
        runtime = workflow_paths(root, "s")["runtime_dir"]
        runtime.mkdir(parents=True)
        with execution._execution_transaction(root, "s"):
            def host_mutation_at(path):
                with policy.execution_transaction(str(path)):
                    acquired.set()
            worker = threading.Thread(target=host_mutation_at, args=(runtime,))
            worker.start()
            assert_true(not acquired.wait(0.1), "standalone and runtime transaction locks must exclude each other")
        worker.join(3)
        assert_true(acquired.is_set(), "standalone mutation proceeds after runtime releases its transaction")


def _load_standalone_policy(name: str):
    import importlib.util
    source = Path(__file__).resolve().parents[2] / "src/exoma/_bundle/config/hosts/claude/hooks/workflow_policy.py"
    spec = importlib.util.spec_from_file_location(name, source)
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    return policy


def _two_sessions_do_not_share_the_guard() -> None:
    """A guard held by one session does not stall another session on the same root.

    Session B mutates from a separate thread on purpose: the guard is reentrant per
    thread, so B driven from the thread holding A would pass even if the guard were
    project-wide. B must finish while A still holds its guard, well inside the guard's
    own ten-second acquisition limit. The Git snapshot is stubbed so that limit measures
    the lock alone, not how fast Git starts; other checks cover the snapshot itself.
    """
    import threading
    from unittest.mock import patch
    with tempfile.TemporaryDirectory(prefix="execution-two-sessions-", ignore_cleanup_errors=True) as tmp, \
            patch.object(execution, "_tree", return_value=None):
        root = Path(tmp)
        done, errors, results = threading.Event(), [], {}
        def mutate_b():
            try:
                execution.record_plan(root, "sess-b", _plan(files=["b.py"]))
                _token(root, "sess-b")
                results["begin"] = execution.begin_execution(root, "sess-b")
                results["end"] = execution.end_execution(root, "sess-b")
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()
        worker = threading.Thread(target=mutate_b)
        with execution._execution_transaction(root, "sess-a"):
            worker.start()
            finished_under_a = done.wait(5)
        # B waits at most the guard's ten seconds once A is released; it must be gone
        # before the directory holding its guard file is removed.
        worker.join(15)
        assert_true(not worker.is_alive(), "session B's worker stopped before its runtime directory is removed")
        assert_true(not errors, f"session B mutates without error while A holds its guard: {errors}")
        assert_true(finished_under_a, "session B's plan, begin and end complete while A still holds its guard")
        assert_true(results.get("begin", {}).get("ok") and results.get("end", {}).get("ok"),
                    f"session B opens and closes its own execution: {results}")
        assert_true(execution.current_plan(root, "sess-a") is None and execution.current_execution(root, "sess-a") is None,
                    "session B's mutations leave session A's runtime untouched")


def _two_active_executions_keep_their_own_scope() -> None:
    """Two executions open at once: each session's gate and verdict are its own."""
    policy = _load_standalone_policy("execution_policy_two_scopes")
    with tempfile.TemporaryDirectory(prefix="execution-two-scopes-") as tmp:
        root = Path(tmp)
        sessions = (("sess-a", "a.py", "b.py"), ("sess-b", "b.py", "a.py"))
        opened = {}
        for session, own, _other in sessions:
            execution.record_plan(root, session, _plan(files=[own]))
            _token(root, session)
            begun = execution.begin_execution(root, session)
            assert_true(begun["ok"], f"{session} opens its own execution: {begun}")
            opened[session] = begun["execution"]["execution_id"]
        assert_true(opened["sess-a"] != opened["sess-b"], "each execution has its own identity")
        for session, own, other in sessions:
            runtime = str(workflow_paths(root, session)["runtime_dir"])
            allowed = policy.write_refusal("Edit", str(root / own), str(root), str(root), {}, runtime)
            refused = policy.write_refusal("Edit", str(root / other), str(root), str(root), {}, runtime)
            assert_true(allowed is None, f"{session} may write its own scope ({own}): {allowed}")
            assert_true(refused is not None and refused[0] == "outside_scope",
                        f"{session} is refused the other session's scope ({other}): {refused}")
        before_b = execution.current_execution(root, "sess-b")
        execution.record_verification(root, "sess-a", {"ok": True, "meta": {"verdict": "pass"}},
                                      expected=opened["sess-a"])
        assert_true(execution.current_execution(root, "sess-b") == before_b,
                    "a verdict recorded for A leaves B's execution unchanged")
        execution.record_verification(root, "sess-b", {"ok": True, "meta": {"verdict": "fail"}},
                                      expected=opened["sess-b"])
        a, b = execution.current_execution(root, "sess-a"), execution.current_execution(root, "sess-b")
        assert_true(a["execution_id"] == opened["sess-a"] and a["scope"] == ["a.py"] and a["verification"] == "pass",
                    f"A keeps its identity, scope and verdict: {a}")
        assert_true(b["execution_id"] == opened["sess-b"] and b["scope"] == ["b.py"] and b["verification"] == "fail",
                    f"B keeps its identity, scope and verdict: {b}")


def _approval_cannot_follow_a_replaced_plan() -> None:
    with tempfile.TemporaryDirectory(prefix="execution-plan-binding-") as tmp:
        root, session = Path(tmp), "binding-session"
        execution.record_plan(root, session, _plan())
        _token(root, session)
        old_token = (workflow_paths(root, session)["runtime_dir"] / execution.EXECUTE_TOKEN_FILE).read_bytes()
        execution.record_plan(root, session, _plan(files=["different.py"]))
        assert_true(execution.begin_execution(root, session).get("reason") == "not_approved",
                    "approval for A cannot open replacement plan B")
        # Even if a stale writer restores the old token, binding must reject it.
        token_path = workflow_paths(root, session)["runtime_dir"] / execution.EXECUTE_TOKEN_FILE
        token_path.write_bytes(old_token)
        assert_true(execution.begin_execution(root, session).get("reason") == "not_approved",
                    "restoring an old token cannot approve a newer plan")
        _token(root, session)
        assert_true(execution.begin_execution(root, session)["ok"], "new B approval opens B")
        execution.record_plan(root, session, _plan())
        _token(root, session)
        old = token_path.read_bytes()
        execution.record_plan(root, session, _plan())
        token_path.write_bytes(old)
        assert_true(execution.begin_execution(root, session).get("reason") == "not_approved",
                    "even an identical plan re-record has a new approval revision")
        token_path.write_text(json.dumps({"set_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}),
                              encoding="utf-8")
        assert_true(execution.begin_execution(root, session).get("reason") == "not_approved",
                    "legacy unbound approvals are not a fallback")
        _token(root, session)
        other = "other-session"
        execution.record_plan(root, other, _plan())
        other_token = workflow_paths(root, other)["runtime_dir"] / execution.EXECUTE_TOKEN_FILE
        other_token.write_bytes(token_path.read_bytes())
        assert_true(execution.begin_execution(root, other).get("reason") == "not_approved",
                    "copied approval cannot cross runtime sessions")
        from unittest.mock import patch
        _token(root, session)
        def replace_during_begin(_root):
            with patch.object(execution, "_tree", return_value=None):
                execution.record_plan(root, session, _plan(files=["interleaved.py"]))
            return None
        with patch.object(execution, "_tree", side_effect=replace_during_begin):
            assert_true(execution.begin_execution(root, session).get("reason") == "not_approved",
                        "plan replacement during begin snapshot is rejected")
