"""Workspace release guards and per-project session isolation."""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from exoma import check
from exoma import cli as main
from exoma.adapters.providers.opencode_adapter import OpenCodeAdapter
from exoma.core.provider.executor import Executor
from exoma.core.jobs.job_manager import JobManager
from exoma.core.prompt.prompt_builder import build_prompt
from exoma.core.runtime.state import ensure_workflow_workspace

from tests.checks.support import (
    FakeJobProcess,
    FakeOpenCodeAdapter,
    RecordingOpenCodeAdapter,
    assert_true,
    clean_output,
    extract_session_id,
)
from tests.checks.support import no_console

def _test_workspace_release_guards() -> None:
    """Runtime generations, upgrade corruption, and unborn staged diffs fail safely."""
    import shutil as _shutil

    from exoma.core.audit.diagnostics import run_sweep
    from exoma.core.evidence.runtime_io import write_evidence_sidecars, write_prompt_handoff
    from exoma.core.workspace.runtime_lock import acquire_runtime_lock, release_runtime_lock
    from exoma.core.runtime.upgrade import upgrade_workflow_workspace
    from exoma.core.workspace.workspace_paths import workflow_paths

    root = Path(tempfile.mkdtemp(prefix="workspace-release-"))
    try:
        subprocess_result = main.subprocess.run(
            ["git", "init", "-q", str(root)], capture_output=True, text=True
        , **no_console())
        assert_true(subprocess_result.returncode == 0, "workspace fixture git init failed")
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
        paths = workflow_paths(root, "lock-session")
        paths["runtime_dir"].mkdir(parents=True, exist_ok=True)

        old_lock = acquire_runtime_lock(paths["lock"], "analyze", "lock-session")
        new_token = "new-generation-token"
        paths["lock"].write_text(
            json.dumps(
                {
                    "command": "analyze",
                    "session_id": "lock-session",
                    "token": new_token,
                    "created_at": "2026-08-02T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        release_runtime_lock(
            paths["lock"], "lock-session", old_lock.get("token")
        )
        assert_true(
            paths["lock"].exists(),
            "a late runtime finalizer must not remove a newer lock generation",
        )
        release_runtime_lock(paths["lock"], "lock-session", new_token)

        live_owner = acquire_runtime_lock(paths["lock"], "analyze", "lock-session")
        live_payload = json.loads(paths["lock"].read_text(encoding="utf-8"))
        live_payload["created_at"] = "2020-01-01T00:00:00+00:00"
        paths["lock"].write_text(json.dumps(live_payload), encoding="utf-8")
        blocked_live_owner = acquire_runtime_lock(
            paths["lock"], "analyze", "lock-session"
        )
        assert_true(
            live_owner.get("ok") and not blocked_live_owner.get("ok"),
            "a living lock owner must remain active after the legacy TTL",
        )
        release_runtime_lock(paths["lock"], "lock-session", live_owner.get("token"))

        reused_owner = acquire_runtime_lock(paths["lock"], "analyze", "lock-session")
        reused_payload = json.loads(paths["lock"].read_text(encoding="utf-8"))
        reused_payload["process_identity"] = "different-process-generation"
        paths["lock"].write_text(json.dumps(reused_payload), encoding="utf-8")
        replacement = acquire_runtime_lock(paths["lock"], "analyze", "lock-session")
        assert_true(
            reused_owner.get("ok") and replacement.get("ok"),
            "a live but identity-mismatched PID must be treated as reused",
        )
        release_runtime_lock(paths["lock"], "lock-session", replacement.get("token"))

        legacy_owner = acquire_runtime_lock(paths["lock"], "analyze", "lock-session")
        legacy_payload = json.loads(paths["lock"].read_text(encoding="utf-8"))
        legacy_payload.pop("process_identity", None)
        legacy_payload["pid_create_time"] = None
        legacy_payload["created_at"] = "2020-01-01T00:00:00+00:00"
        paths["lock"].write_text(json.dumps(legacy_payload), encoding="utf-8")
        legacy_replacement = acquire_runtime_lock(
            paths["lock"], "analyze", "lock-session"
        )
        assert_true(
            legacy_owner.get("ok") and legacy_replacement.get("ok"),
            "legacy POSIX locks without process identity must remain TTL-recoverable",
        )
        release_runtime_lock(
            paths["lock"], "lock-session", legacy_replacement.get("token")
        )

        contention_results: list[dict] = []
        contention_guard = threading.Lock()
        contention_start = threading.Barrier(9)
        release_owner = threading.Event()

        def contend() -> None:
            contention_start.wait()
            claim = acquire_runtime_lock(paths["lock"], "analyze", "lock-session")
            with contention_guard:
                contention_results.append(claim)
            if claim.get("ok"):
                release_owner.wait(timeout=5)
                release_runtime_lock(
                    paths["lock"], "lock-session", claim.get("token")
                )

        contenders = [threading.Thread(target=contend) for _ in range(8)]
        for contender in contenders:
            contender.start()
        contention_start.wait()
        deadline = time.time() + 5
        while len(contention_results) < len(contenders) and time.time() < deadline:
            time.sleep(0.01)
        assert_true(
            sum(bool(result.get("ok")) for result in contention_results) == 1,
            f"runtime lock contention must produce exactly one owner: {contention_results}",
        )
        release_owner.set()
        for contender in contenders:
            contender.join(timeout=5)

        unsafe_a = workflow_paths(root, "a/b")["session_dir"]
        unsafe_b = workflow_paths(root, "a?b")["session_dir"]
        dot_session = workflow_paths(root, "..")["session_dir"]
        sessions_root = workflow_paths(root)["data_dir"] / "sessions"
        assert_true(
            unsafe_a != unsafe_b
            and dot_session.resolve().parent == sessions_root.resolve(),
            "workflow session paths must resist traversal and sanitizer collisions",
        )

        write_evidence_sidecars(
            root,
            "sidecar-lock",
            {"files": ["owner.py"]},
            ["owner fact"],
        )
        sidecar_paths = workflow_paths(root, "sidecar-lock")
        before_leads = sidecar_paths["leads"].read_text(encoding="utf-8")
        sidecar_owner = acquire_runtime_lock(
            sidecar_paths["lock"], "analyze", "sidecar-lock"
        )
        blocked = Executor(adapter=FakeOpenCodeAdapter()).execute(
            "analyze",
            "different task",
            {"session_id": "sidecar-lock", "provider_session_id": None},
            str(root),
        )
        assert_true(
            not blocked.get("ok")
            and blocked.get("meta", {}).get("error_type") == "runtime_lock"
            and sidecar_paths["leads"].read_text(encoding="utf-8") == before_leads,
            f"a denied caller must not mutate the active owner's sidecars: {blocked}",
        )
        release_runtime_lock(
            sidecar_paths["lock"], "sidecar-lock", sidecar_owner.get("token")
        )

        first = write_prompt_handoff(root, "analyze", "lock-session", "one")
        release_runtime_lock(
            first["paths"]["lock"],
            "lock-session",
            first["meta"]["lock_token"],
        )
        second = write_prompt_handoff(root, "analyze", "lock-session", "two")
        release_runtime_lock(
            second["paths"]["lock"],
            "lock-session",
            second["meta"]["lock_token"],
        )
        assert_true(
            first["meta"]["prompt_id"] != second["meta"]["prompt_id"],
            "same-second prompt archives must remain immutable and unique",
        )

        config_path = workflow_paths(root)["config"]
        config_path.write_text("{not-json", encoding="utf-8")
        try:
            upgrade_workflow_workspace(root, os.getenv("AGENT_PATH"))
        except ValueError:
            pass
        else:
            raise AssertionError("upgrade must refuse corrupt JSON instead of overwriting it")
        assert_true(
            config_path.read_text(encoding="utf-8") == "{not-json",
            "refused upgrade must preserve corrupt config for manual recovery",
        )
        config_path.write_text(json.dumps({"version": "3.4.0", "runtime": "bad"}), encoding="utf-8")
        try:
            upgrade_workflow_workspace(root, os.getenv("AGENT_PATH"))
        except ValueError:
            pass
        else:
            raise AssertionError("upgrade must reject a non-object runtime section")

        root2 = Path(tempfile.mkdtemp(prefix="sweep-unborn-"))
        try:
            init = main.subprocess.run(
                ["git", "init", "-q", str(root2)], capture_output=True, text=True
            , **no_console())
            assert_true(init.returncode == 0, "unborn sweep fixture git init failed")
            ensure_workflow_workspace(root2, os.getenv("AGENT_PATH"))
            (root2 / "staged.py").write_text("value = 1\n", encoding="utf-8")
            added = main.subprocess.run(
                ["git", "add", "staged.py"], cwd=root2, capture_output=True, text=True
            , **no_console())
            assert_true(added.returncode == 0, f"git add failed: {added.stderr}")
            swept = run_sweep(root2, "unborn")
            assert_true(
                swept.get("ok")
                and "staged.py" in swept.get("meta", {}).get("changed_files", []),
                f"sweep must include staged files before the first commit: {swept}",
            )
        finally:
            _shutil.rmtree(root2, ignore_errors=True)

        not_repo = Path(tempfile.mkdtemp(prefix="sweep-not-repo-"))
        try:
            failed = run_sweep(not_repo, "bad-git")
            assert_true(
                not failed.get("ok")
                and failed.get("meta", {}).get("error_type") == "sweep_git_error"
                and failed.get("meta", {}).get("next_action"),
                f"Git failures must use the structured sweep contract: {failed}",
            )
        finally:
            _shutil.rmtree(not_repo, ignore_errors=True)
    finally:
        _shutil.rmtree(root, ignore_errors=True)


def _test_project_session_isolation() -> None:
    """Default and provider sessions must not cross project or sanitized-ID boundaries."""
    import shutil as _shutil

    from exoma.config import settings

    root = Path(tempfile.mkdtemp(prefix="session-isolation-"))
    project_a = root / "a"
    project_b = root / "b"
    project_a.mkdir()
    project_b.mkdir()
    initialized = main.subprocess.run(
        ["git", "init", "-q", str(project_a)], capture_output=True, text=True
    , **no_console())
    assert_true(initialized.returncode == 0, "session isolation git init failed")
    project_a_subdir = project_a / "src"
    project_a_subdir.mkdir()
    original_cache = settings.CACHE_FILE
    original_manager = main.SESSION_MANAGER
    original_executor = main.EXECUTOR
    try:
        settings.CACHE_FILE = root / "cache.json"
        session_a = main.resolve_session_id("default", project_root=project_a)
        session_a_again = main.resolve_session_id("default", project_root=project_a)
        session_a_subdir = main.resolve_session_id(
            "default", project_root=project_a_subdir
        )
        session_b = main.resolve_session_id("default", project_root=project_b)
        fresh_explicit = main.resolve_session_id(
            "explicit-session", fresh=True, project_root=project_a
        )
        assert_true(
            session_a == session_a_again == session_a_subdir
            and session_a != session_b
            and fresh_explicit != "explicit-session"
            and main.resolve_session_id("default", project_root=project_a) == session_a,
            "session cache must use canonical project root and fresh must replace explicit IDs",
        )

        # An abandoned session must not keep capturing "default" calls forever. Age the
        # stamp past the TTL with no active job: the next resolve hands out a new ID and
        # rewrites the cache, so later calls follow the new session, not the first one.
        cache_file = settings._main_session_cache_path(project_a)
        aged = json.loads(cache_file.read_text(encoding="utf-8"))
        aged["updated_at"] = (
            datetime.now(timezone.utc)
            - timedelta(seconds=settings.MAIN_SESSION_CACHE_TTL_SECONDS + 60)
        ).isoformat()
        cache_file.write_text(json.dumps(aged), encoding="utf-8")
        session_after_ttl = main.resolve_session_id("default", project_root=project_a)
        assert_true(
            session_after_ttl != session_a
            and main.resolve_session_id("default", project_root=project_a)
            == session_after_ttl,
            "an idle session past the cache TTL must be replaced, then cached in its place",
        )

        main.SESSION_MANAGER = main._DEFAULT_SESSION_MANAGER
        manager_a = main._session_manager_for(project_a)
        manager_b = main._session_manager_for(project_b)
        logical_id = "same/session"
        storage_id = main._session_storage_id(logical_id)
        provider_a = manager_a.load_or_create(storage_id)
        manager_a.update_provider_session_id(provider_a, "provider-project-a")
        provider_b = manager_b.load_or_create(storage_id)
        assert_true(
            manager_a.session_dir != manager_b.session_dir
            and provider_b.get("provider_session_id") is None,
            "provider session IDs must not leak across project roots",
        )
        unsafe_ids = [
            "a/b",
            "a?b",
            "..",
            "foo",
            "Foo",
            "foo.",
            "con",
            "con.txt",
            "NUL",
        ]
        safe_ids = [main._session_storage_id(value) for value in unsafe_ids]
        encoded_id = main._session_storage_id("a/b")
        assert_true(
            len(set(safe_ids)) == len(safe_ids)
            and all(
                value not in {"", ".", "..", "con", "con.txt", "nul"}
                for value in safe_ids
            )
            and main._session_storage_id(encoded_id) != encoded_id,
            "sanitized logical session IDs must remain collision-resistant",
        )

        safe_manager = JobManager(root / "safe-jobs")
        assert_true(
            len({safe_manager._safe(value) for value in unsafe_ids}) == len(unsafe_ids),
            "job/session lock filenames must remain collision-resistant",
        )

        ensure_workflow_workspace(project_a, os.getenv("AGENT_PATH"))
        entered = threading.Event()
        release = threading.Event()

        class BlockingAdapter(FakeOpenCodeAdapter):
            def run(self, *args, **kwargs):
                entered.set()
                release.wait(timeout=5)
                return super().run(*args, **kwargs)

        main.EXECUTOR = Executor(adapter=BlockingAdapter())
        first_result: list[dict] = []
        first = threading.Thread(
            target=lambda: first_result.append(
                main.run(
                    "analyze",
                    "trace provider session isolation",
                    "provider-race-session",
                    str(project_a),
                )
            )
        )
        first.start()
        assert_true(entered.wait(timeout=5), "provider race fixture did not enter adapter")
        blocked = main.run(
            "analyze",
            "competing provider session call",
            "provider-race-session",
            str(project_a),
        )
        release.set()
        first.join(timeout=5)
        persisted = manager_a.load_or_create("provider-race-session")
        assert_true(
            first_result
            and first_result[0].get("ok")
            and blocked.get("meta", {}).get("error_type") == "runtime_lock"
            and persisted.get("provider_session_id") == "ses_test123"
            and len((persisted.get("history") or {}).get("runs") or []) == 1,
            f"provider session load/update/history must remain inside the runtime lock: {blocked}",
        )
    finally:
        if "release" in locals():
            release.set()
        settings.CACHE_FILE = original_cache
        main.SESSION_MANAGER = original_manager
        main.EXECUTOR = original_executor
        _shutil.rmtree(root, ignore_errors=True)


def _test_init_upgrade_and_session_guard() -> None:
    """init must not hand back a stale workspace, and the entry script must refuse
    a default session on a delegated command.

    Both used to be reported rather than enforced: `upgrade_needed` was a return field
    and session=default was a stderr line. The caller reads neither — the JSON result is
    consumed programmatically and the run script is dispatched as a background task whose
    stderr nobody opens. A safeguard that only fires where nobody looks is not one.
    """
    from exoma.core.runtime.upgrade import needs_upgrade, workspace_versions
    from exoma.core.workspace.workspace_paths import workflow_paths
    from exoma.utils import osutil

    root = Path(tempfile.mkdtemp(prefix="init-upgrade-"))
    try:
        agent_path = os.getenv("AGENT_PATH") or str(
            Path(__file__).resolve().parents[2] / "main.py"
        )
        fresh = ensure_workflow_workspace(root, agent_path)
        assert_true(
            fresh["auto_upgrade"] is None and not fresh["upgrade_needed"],
            f"a workspace created right now has nothing to upgrade: {fresh['auto_upgrade']!r}",
        )

        # Stamp it with an older build, exactly as a workspace scaffolded before an update
        # would be, then run init again — the path a user takes without knowing to upgrade.
        config_path = workflow_paths(root)["config"]
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["runtime"]["tool_version"] = "0.0.1-old"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        assert_true(
            needs_upgrade(root), "test fixture failed to make the workspace look stale"
        )

        again = ensure_workflow_workspace(root, agent_path)
        assert_true(
            isinstance(again["auto_upgrade"], dict),
            f"init on a stale workspace must upgrade it, not just report it: {again['auto_upgrade']!r}",
        )
        assert_true(
            not again["upgrade_needed"],
            "upgrade_needed must describe the state the caller is LEFT in, not the one it walked into",
        )
        assert_true(
            workspace_versions(root)["installed_tool_version"]
            == workspace_versions(root)["current_tool_version"],
            "the version stamp must be current after init auto-upgrades",
        )

        # A workspace agent-workflow wrote: its runtime block names a checkout's main.py.
        # Upgrade takes it over — the old paths go, the scripts call `-m exoma`.
        from exoma.core.runtime.upgrade import upgrade_workflow_workspace
        from exoma.core.workspace.workspace_paths import LEGACY_RUNTIME_KEYS

        config = json.loads(config_path.read_text(encoding="utf-8"))
        old_clone = str(root / "agent-workflow" / "main.py")
        config["version"] = "3.9.0"
        config["runtime"] = {
            "agent_workflow_path": old_clone,
            "main_py_path": old_clone,
            "check_py_path": str(root / "agent-workflow" / "check.py"),
            "tool_dir": str(root / "agent-workflow"),
            "tool_version": "3.9.0",
            "runtime_version": "3.9.0",
            "second_agent": config["runtime"]["second_agent"],
        }
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        upgrade_workflow_workspace(root)
        runtime = json.loads(config_path.read_text(encoding="utf-8"))["runtime"]
        assert_true(
            not any(key in runtime for key in LEGACY_RUNTIME_KEYS)
            and runtime.get("python") and runtime.get("package_root"),
            f"upgrade replaces agent-workflow's checkout paths with the running build: {runtime}",
        )
        run_sh = (workflow_paths(root)["workflow_dir"] / "run.sh").read_text(encoding="utf-8")
        assert_true(
            "-m exoma" in run_sh and "main.py" not in run_sh,
            "the regenerated runner calls `-m exoma`, never a checkout's main.py",
        )
        inspected = subprocess.run(
            [osutil.find_bash() or "bash", str(workflow_paths(root)["workflow_dir"] / "inspect.sh")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **no_console(),
        )
        assert_true(
            inspected.returncode == 0 and json.loads(inspected.stdout).get("ok") is not None,
            f"the generated inspect.sh reaches the runtime through -m exoma: rc={inspected.returncode} {inspected.stderr[-300:]!r}",
        )

        # The generated entry script, run for real. Only the refusal path is exercised:
        # on success it would dispatch an actual delegated call.
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("MAIN_SESSION_ID", "AI_PROXY_ALLOW_DEFAULT_SESSION")
        }
        workflow_dir = workflow_paths(root)["workflow_dir"]
        # The runner is bash on every OS (Git Bash on Windows).
        argv = [osutil.find_bash() or "bash", str(workflow_dir / "run.sh")]
        refused = subprocess.run(
            argv + ["analyze", "probe task"],
            env=env,
            capture_output=True,
            text=True,
            **no_console(),
        )
        assert_true(
            refused.returncode == 2,
            f"a delegated call with no session must be refused, not dispatched: rc={refused.returncode} {refused.stdout!r}",
        )
        assert_true(
            "MAIN_SESSION_ID" in refused.stderr,
            f"the refusal must name what to pass instead: {refused.stderr!r}",
        )

        allowed = subprocess.run(
            argv + ["doctor"],
            env=env,
            capture_output=True,
            text=True,
            **no_console(),
        )
        assert_true(
            allowed.returncode != 2,
            f"a local command must keep working without a session: rc={allowed.returncode} {allowed.stderr!r}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
