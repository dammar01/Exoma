"""Job admission: attach, recover, and the limits on both."""

import json
import os
import shutil
import tempfile
import threading
import time
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

def _test_submit_admission() -> None:
    """Initial dispatch and global capacity must admit exactly one worker."""
    import shutil as _shutil

    root = Path(tempfile.mkdtemp(prefix="submit-admission-"))
    original_manager = main.JOB_MANAGER
    original_check_manager = check.JOB_MANAGER
    original_spawn = main._spawn_worker
    manager = JobManager(root / "jobs", max_global_workers=1)
    main.JOB_MANAGER = manager
    check.JOB_MANAGER = manager
    calls: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    def blocking_spawn(job_id: str, work_dir: str | None = None) -> dict:
        calls.append(job_id)
        entered.set()
        release.wait(timeout=5)
        return {"ok": True, "content": "worker started", "meta": {"pid": os.getpid()}}

    try:
        assert_true(
            manager.request_hash("analyze", "same", "hash-session", str(root), True, "m1")
            != manager.request_hash("analyze", "same", "hash-session", str(root), False, "m1")
            and manager.request_hash(
                "analyze", "same", "hash-session", str(root), True, "m1"
            )
            != manager.request_hash(
                "analyze", "same", "hash-session", str(root), True, "m2"
            ),
            "job idempotency must distinguish fresh-session reuse policy and model override",
        )
        main._spawn_worker = blocking_spawn
        first_result: list[dict] = []
        first = threading.Thread(
            target=lambda: first_result.append(
                main.submit("analyze", "same", "same-session", str(root), None)
            )
        )
        first.start()
        assert_true(entered.wait(timeout=5), "first submit never reached worker spawn")
        attached = main.submit("analyze", "same", "same-session", str(root), None)
        assert_true(
            attached.get("ok")
            and attached.get("meta", {}).get("reused")
            and not attached.get("meta", {}).get("recovery")
            and len(calls) == 1,
            f"a fresh pending dispatch must attach, never recover/spawn twice: {attached}",
        )
        release.set()
        first.join(timeout=5)
        assert_true(first_result and first_result[0].get("ok"), "first submit failed")
        manager.fail_job(first_result[0]["job_id"], "cleanup")

        calls.clear()
        main._spawn_worker = lambda job_id, work_dir=None: (
            calls.append(job_id)
            or {"ok": True, "content": "worker started", "meta": {"pid": os.getpid()}}
        )
        results: list[dict] = []
        start = threading.Barrier(3)

        def submit_distinct(index: int) -> None:
            start.wait()
            results.append(
                main.submit(
                    "analyze", f"task-{index}", f"capacity-{index}", str(root), None
                )
            )

        threads = [threading.Thread(target=submit_distinct, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=5)
        assert_true(
            sum(bool(result.get("ok")) for result in results) == 1
            and sum(
                result.get("meta", {}).get("error_type") == "worker_capacity"
                for result in results
            )
            == 1
            and len(calls) == 1,
            f"capacity=1 must admit one submit and reject one: {results}, calls={calls}",
        )
        for result in results:
            if result.get("ok"):
                manager.fail_job(result["job_id"], "cleanup")

        live = manager.create_job("analyze", "live", "live-capacity", str(root), None)
        manager.set_worker_pid(live["job_id"], os.getpid())
        manager.mark_running(live["job_id"])
        dead = manager.create_job("analyze", "dead", "dead-capacity", str(root), None)
        manager.set_worker_pid(dead["job_id"], 999999999)
        manager.mark_running(dead["job_id"])
        blocked_recovery = main.submit(
            "analyze", "dead", "dead-capacity", str(root), None
        )
        assert_true(
            not blocked_recovery.get("ok")
            and blocked_recovery.get("meta", {}).get("error_type")
            == "worker_capacity"
            and (manager.get_job(dead["job_id"]) or {}).get("recovery_attempt") == 0,
            f"recovery must obey the global capacity limit: {blocked_recovery}",
        )
        manager.fail_job(live["job_id"], "cleanup")
        manager.fail_job(dead["job_id"], "cleanup")

        startup_crash = manager.create_job(
            "analyze", "startup-crash", "startup-crash-session", str(root), None
        )
        manager.set_worker_pid(startup_crash["job_id"], 999999999)
        calls.clear()
        recovered_startup = main.submit(
            "analyze", "startup-crash", "startup-crash-session", str(root), None
        )
        recovered_record = manager.get_job(startup_crash["job_id"]) or {}
        assert_true(
            recovered_startup.get("ok")
            and recovered_startup.get("meta", {}).get("recovery")
            and recovered_record.get("recovery_attempt") == 1
            and calls == [startup_crash["job_id"]],
            f"a pending dead worker must not consume the slot needed for its recovery: {recovered_startup}",
        )
        manager.fail_job(startup_crash["job_id"], "cleanup")

        reused_worker = manager.create_job(
            "analyze", "reused-worker", "reused-worker-session", str(root), None
        )
        manager.set_worker_pid(reused_worker["job_id"], os.getpid())
        manager.mark_running(reused_worker["job_id"])
        reused_worker_record = manager.get_job(reused_worker["job_id"])
        reused_worker_record["worker_identity"] = "different-generation"
        manager._save(reused_worker_record)
        calls.clear()
        recovered_reused_worker = main.submit(
            "analyze", "reused-worker", "reused-worker-session", str(root), None
        )
        assert_true(
            recovered_reused_worker.get("ok")
            and recovered_reused_worker.get("meta", {}).get("recovery")
            and calls == [reused_worker["job_id"]],
            f"a reused worker PID must reach recovery instead of attaching forever: {recovered_reused_worker}",
        )
        manager.fail_job(reused_worker["job_id"], "cleanup")

        owned = manager.create_job(
            "analyze", "slow-spawn", "slow-owner-session", str(root), None
        )
        owned_record = manager.get_job(owned["job_id"])
        owned_record["created_at"] = "2020-01-01T00:00:00+00:00"
        manager._save(owned_record)
        calls.clear()
        still_owned = main.submit(
            "analyze", "slow-spawn", "slow-owner-session", str(root), None
        )
        assert_true(
            still_owned.get("ok")
            and still_owned.get("meta", {}).get("reused")
            and not calls
            and (manager.get_job(owned["job_id"]) or {}).get("recovery_attempt") == 0,
            f"a live submit owner must protect a slow pre-spawn reservation: {still_owned}",
        )
        manager.fail_job(owned["job_id"], "cleanup")

        reused_owner = manager.create_job(
            "analyze", "reused-owner", "reused-owner-session", str(root), None
        )
        reused_record = manager.get_job(reused_owner["job_id"])
        reused_record["reservation_owner_pid"] = os.getpid()
        reused_record["reservation_owner_create_time"] = None
        reused_record["reservation_owner_identity"] = "different-generation"
        reused_record["created_at"] = "2020-01-01T00:00:00+00:00"
        manager._save(reused_record)
        reused_claim = manager.claim_recovery(
            reused_owner["job_id"], stale_after_seconds=0
        )
        manager.release_recovery_claim(reused_owner["job_id"])
        assert_true(
            reused_claim.get("action") == "recover",
            f"a reused reservation-owner PID must not block recovery: {reused_claim}",
        )
        manager.fail_job(reused_owner["job_id"], "cleanup")

        legacy_owner = manager.create_job(
            "analyze", "legacy-owner", "legacy-owner-session", str(root), None
        )
        legacy_record = manager.get_job(legacy_owner["job_id"])
        legacy_record["reservation_owner_pid"] = os.getpid()
        legacy_record["reservation_owner_create_time"] = None
        legacy_record.pop("reservation_owner_identity", None)
        legacy_record["created_at"] = "2020-01-01T00:00:00+00:00"
        manager._save(legacy_record)
        legacy_claim = manager.claim_recovery(
            legacy_owner["job_id"], stale_after_seconds=0
        )
        manager.release_recovery_claim(legacy_owner["job_id"])
        assert_true(
            legacy_claim.get("action") == "recover",
            f"an unverifiable legacy reservation must become reclaimable after grace: {legacy_claim}",
        )
        manager.fail_job(legacy_owner["job_id"], "cleanup")

        orphan = manager.create_job(
            "analyze", "orphan", "orphan-session", str(root), None
        )
        orphan_record = manager.get_job(orphan["job_id"])
        orphan_record["reservation_owner_pid"] = 999999999
        orphan_record["reservation_owner_create_time"] = None
        manager._save(orphan_record)
        original_claim = manager.claim_recovery
        claim_calls = 0

        def staged_claim(job_id: str, max_attempts: int = 1, stale_after_seconds: float = 30.0):
            nonlocal claim_calls
            claim_calls += 1
            effective_stale = 999.0 if claim_calls == 1 else 0.0
            return original_claim(job_id, max_attempts, effective_stale)

        manager.claim_recovery = staged_claim  # type: ignore[method-assign]

        def complete_spawn(job_id: str, work_dir: str | None = None) -> dict:
            calls.append(job_id)
            manager.complete_job(
                job_id,
                {"ok": True, "content": "recovered orphan", "meta": {}},
            )
            return {"ok": True, "content": "worker started", "meta": {"pid": os.getpid()}}

        calls.clear()
        main._spawn_worker = complete_spawn
        # The deadline only bounds a failure: a passing run returns as soon as the stubbed
        # spawn completes the job. 1 s was eaten on a slow Windows CI runner by the resumed
        # submits' file guards and fsyncs alone, failing with the job still pending.
        recovered = main.await_job(
            "analyze",
            "orphan",
            "orphan-session",
            str(root),
            None,
            poll_interval=0.01,
            poll_timeout=30,
        )
        assert_true(
            recovered.get("ok")
            and recovered.get("content") == "recovered orphan"
            and claim_calls >= 2
            and calls == [orphan["job_id"]]
            and (manager.get_job(orphan["job_id"]) or {}).get("recovery_attempt") == 0,
            f"await must reclaim an orphaned pre-spawn reservation after grace: {recovered}; "
            f"claim_calls={claim_calls} spawns={calls} job={manager.get_job(orphan['job_id'])}",
        )
        manager.claim_recovery = original_claim  # type: ignore[method-assign]

        attach_orphan = manager.create_job(
            "analyze", "attach-orphan", "attach-orphan-session", str(root), None
        )
        from datetime import datetime, timedelta, timezone

        stale = manager.get_job(attach_orphan["job_id"])
        stale["reservation_owner_pid"] = 999999999
        stale["reservation_owner_create_time"] = None
        stale["created_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=31)
        ).isoformat()
        manager._save(stale)
        calls.clear()
        attached_status = check._wait_for_status(
            attach_orphan["job_id"], poll_interval=0.01, poll_timeout=30
        )
        assert_true(
            attached_status.get("status") == "completed"
            and calls == [attach_orphan["job_id"]],
            f"check --wait must reclaim an orphaned pre-spawn reservation: {attached_status}; "
            f"spawns={calls} job={manager.get_job(attach_orphan['job_id'])}",
        )
    finally:
        release.set()
        main._spawn_worker = original_spawn
        main.JOB_MANAGER = original_manager
        check.JOB_MANAGER = original_check_manager
        _shutil.rmtree(root, ignore_errors=True)


def _test_worker_log_preservation() -> None:
    """Two spawns of the same job preserve both output segments and the public path."""
    from unittest.mock import patch
    from exoma.core.jobs import job_lifecycle

    with tempfile.TemporaryDirectory(prefix="worker-log-") as directory:
        root = Path(directory)
        manager = JobManager(root / "jobs")
        counter = []

        def spawn(args, **kwargs):
            stream = kwargs["stdout"]
            counter.append(len(counter) + 1)
            stream.write(f"attempt-output-{counter[-1]}\n")
            stream.flush()
            return type("Process", (), {"pid": os.getpid()})()

        with patch.object(main, "JOB_MANAGER", manager), patch.object(main.subprocess, "Popen", spawn):
            first = job_lifecycle._spawn_worker("same-job", str(root))
            second = job_lifecycle._spawn_worker("same-job", str(root))
        log = manager.log_path("same-job")
        content = log.read_text(encoding="utf-8")
        assert_true("attempt-output-1" in content and "attempt-output-2" in content,
                    f"respawn preserves earlier output: {content}")
        assert_true(content.count("--- worker spawn ") == 2, "spawn boundaries are visible")
        assert_true(first["meta"]["log"] == second["meta"]["log"] == str(log), "log path remains compatible")
        from unittest.mock import Mock
        broken = Mock()
        broken.write.side_effect = OSError("header unavailable")
        with patch.object(main, "JOB_MANAGER", manager), \
                patch.object(job_lifecycle, "open", return_value=broken, create=True), \
                patch.object(main.subprocess, "Popen", return_value=type("Process", (), {"pid": os.getpid()})()):
            result = job_lifecycle._spawn_worker("same-job", str(root))
        assert_true(result["ok"] and broken.close.call_count == 1,
                    "a failed log header closes its handle and still permits spawning")


def _test_job_store_not_writable() -> None:
    """A sandbox that refuses the global job store gets the exact fix, not a shrug (CASE-034).

    Codex as host on a `workspace-write` sandbox could not write the job store under the
    per-user data directory, and every delegated call ended in `job_submit_error` saying
    only "check that the job store is writable". Submit and doctor must both name the root
    to grant and the snippet that grants it.
    """
    import contextlib

    from exoma.config import settings
    from exoma.core.audit.diagnostics import run_doctor
    from exoma.core.jobs.job_lifecycle import job_store_fix
    from exoma.core.runtime.state import ensure_workflow_workspace

    # Under the storage directory that directory is the root, never the data directory
    # above it: the data directory holds `.env`, which a sandbox must not be able to write.
    fix = job_store_fix(settings.JOB_DIR)
    assert_true(
        fix["writable_root"] == str(settings.STORAGE_DIR)
        and f"writable_roots = ['{settings.STORAGE_DIR}']" in fix["snippet"]
        and "[sandbox_workspace_write]" in fix["snippet"]
        and "EXOMA_HOME" in fix["next_action"],
        f"the storage directory is the root to grant: {fix}",
    )
    granted = Path(fix["writable_root"])
    env_file = settings.DATA_DIR / ".env"
    assert_true(
        granted != settings.DATA_DIR and granted not in env_file.parents,
        f"the granted root must not cover the data directory's .env: {granted}",
    )
    assert_true(
        fix["next_action"].startswith(f"The job store {settings.JOB_DIR} is not writable")
        and "Grant the host's sandbox write access to" in fix["next_action"]
        and "(Codex: run `exoma install` again with codex" in fix["next_action"]
        and "`writable_roots`" in fix["next_action"],
        f"the fix names any host's sandbox first, Codex only as the example: {fix['next_action']}",
    )
    odd = Path(tempfile.gettempdir()) / "it's jobs"
    quoted = job_store_fix(odd)["snippet"]
    assert_true(
        "writable_roots = [\"" in quoted and "it's jobs" in quoted,
        f"a path with an apostrophe falls back to a basic TOML string: {quoted!r}",
    )

    root = Path(tempfile.mkdtemp(prefix="job-store-denied-"))
    original_manager = main.JOB_MANAGER
    original_spawn = main._spawn_worker
    manager = JobManager(root / "jobs", max_global_workers=1)

    # A sandbox refusal is a PermissionError on one platform and a plain OSError (EROFS,
    # EACCES, a Windows access error) on another; both get the fix. The generic "check that
    # the job store is writable" a Codex user saw came from the second kind.
    refusals = (
        PermissionError(13, "Permission denied", str(manager.job_dir / ".capacity.guard")),
        OSError(30, "Read-only file system", str(manager.job_dir / ".capacity.guard")),
    )
    main.JOB_MANAGER = manager
    main._spawn_worker = lambda *a, **k: {"ok": True, "content": "worker started", "meta": {}}
    try:
        for refusal in refusals:
            @contextlib.contextmanager
            def denied(exc=refusal):
                raise exc
                yield  # pragma: no cover

            manager.capacity_guard = denied
            result = main.submit("analyze", "task", "denied-session", str(root), None)
            meta = result.get("meta", {})
            assert_true(
                not result.get("ok")
                and meta.get("error_type") == "job_submit_error"
                and meta.get("reason") == "job_store_not_writable"
                and meta.get("error") == type(refusal).__name__
                and meta.get("job_store") == str(manager.job_dir)
                and str(manager.job_dir) in meta.get("next_action", "")
                and "writable_roots" in meta.get("next_action", "")
                and "Check that the job store is writable" not in meta.get("next_action", "")
                and "[sandbox_workspace_write]" in meta.get("fix_snippet", ""),
                f"a denied job store ({type(refusal).__name__}) names the root and the snippet: {result}",
            )

        # A refusal naming a path outside the store is that path's, not the store's: no
        # sandbox-grant advice for a write the grant would not fix.
        elsewhere = root / "project" / ".workflow" / "data" / "verify" / "tests.json"

        @contextlib.contextmanager
        def denied_elsewhere():
            raise PermissionError(13, "Permission denied", str(elsewhere))
            yield  # pragma: no cover

        manager.capacity_guard = denied_elsewhere
        result = main.submit("analyze", "task", "denied-session", str(root), None)
        meta = result.get("meta", {})
        assert_true(
            not result.get("ok")
            and meta.get("reason") == "write_refused"
            and str(elsewhere) in meta.get("next_action", "")
            and "writable_roots" not in meta.get("next_action", ""),
            f"a refused write outside the job store names that path, not the grant: {result}",
        )

        # `write`/`fsync` raise without a filename. A permission errno is still the store's
        # refusal; a full disk or an I/O error is not, and a sandbox grant would not fix it.
        for unnamed, reason in (
            (PermissionError(13, "Permission denied"), "job_store_not_writable"),
            (OSError(28, "No space left on device"), "job_store_write_failed"),
            (OSError(5, "Input/output error"), "job_store_write_failed"),
        ):
            @contextlib.contextmanager
            def failing(exc=unnamed):
                raise exc
                yield  # pragma: no cover

            manager.capacity_guard = failing
            result = main.submit("analyze", "task", "denied-session", str(root), None)
            meta = result.get("meta", {})
            granted = "writable_roots" in meta.get("next_action", "") or "fix_snippet" in meta
            assert_true(
                not result.get("ok")
                and meta.get("reason") == reason
                and meta.get("job_store") == str(manager.job_dir)
                and granted == (reason == "job_store_not_writable"),
                f"a filename-less errno {unnamed.errno} reads as {reason}: {result}",
            )
    finally:
        main.JOB_MANAGER = original_manager
        main._spawn_worker = original_spawn

    # Doctor probes the same store. A file where the store's parent should be makes the
    # probe fail on every OS without changing permissions.
    blocker = root / "blocker"
    blocker.write_text("", encoding="utf-8")
    workspace = root / "project"
    workspace.mkdir()
    ensure_workflow_workspace(workspace, os.getenv("AGENT_PATH"))
    original_job_dir = settings.JOB_DIR
    settings.JOB_DIR = blocker / "jobs"
    try:
        meta = run_doctor(workspace, "does-not-exist", "job-store-denied")["meta"]
        report = json.loads(Path(meta["doctor_report"]).read_text(encoding="utf-8"))
        assert_true(
            report["checks"].get("job_store_writable") is False
            and any(issue.startswith("job store not writable") for issue in meta["issues"])
            and any("[sandbox_workspace_write]" in fix_text and "exoma install --check" in fix_text
                    for fix_text in meta["recommended_fixes"]),
            f"doctor reports the unwritable job store with the snippet and the grant check: {meta}",
        )
    finally:
        settings.JOB_DIR = original_job_dir

    meta = run_doctor(workspace, "does-not-exist", "job-store-ok")["meta"]
    report = json.loads(Path(meta["doctor_report"]).read_text(encoding="utf-8"))
    assert_true(
        report["checks"].get("job_store_writable") is True
        and not any(issue.startswith("job store") for issue in meta["issues"])
        and not (settings.JOB_DIR / ".doctor-probe").exists(),
        f"a writable job store passes and leaves no probe behind: {meta['issues']}",
    )
    shutil.rmtree(root, ignore_errors=True)


def _test_job_cancellation_and_diagnostics():
    from unittest.mock import patch
    from exoma.core.jobs.diagnostics import snapshot
    from exoma.core.evidence.result_shaping import _verify_exit_code
    with tempfile.TemporaryDirectory(prefix="job-cancel-") as tmp:
        root = Path(tmp).resolve()
        with patch("os.getcwd", return_value=str(root)):
            relative_manager = JobManager(Path("relative-jobs"))
        assert_true(relative_manager.job_dir == root / "relative-jobs",
                    "cross-process reservation paths are absolute even with relative storage")
        manager = JobManager(root / "jobs", idle_stall_seconds=10)
        job = manager.create_job("analyze", "task", "cancel-session", str(root), None)
        manager.set_worker_pid(job["job_id"], 987654321)
        manager.mark_running(job["job_id"])
        manager.touch_heartbeat(job["job_id"], {"progress_schema": 1, "semantic_progress_supported": True,
                                               "work_idle_seconds": 30, "idle_seconds": 0})
        with patch("exoma.core.jobs.job_manager.osutil.process_generation", return_value="matched"):
            assert_true(manager.liveness(manager.get_job(job["job_id"])) == "alive-stalled",
                        "fresh output/heartbeat cannot disguise semantic stagnation")
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
        result = snapshot(root, "cancel-session", job["job_id"], manager.job_dir)
        assert_true(result["ok"] and result["jobs"][0]["heartbeat"]["progress"]["work_idle_seconds"] == 30,
                    "snapshot reports the live diagnostic beat")
        assert_true(not snapshot(root, "another-session", job["job_id"], manager.job_dir)["ok"],
                    "snapshot rejects another session's job")
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
        assert_true(before == after, "populated-store snapshot does not write or touch state")
        missing = root / "missing"
        assert_true(snapshot(missing, "cancel-session", job_dir=missing / "jobs")["ok"] and not missing.exists(),
                    "missing-store snapshot creates nothing")
        import subprocess
        import sys
        from tests.checks.support import no_console
        for entry in (["-m", "exoma.core.jobs.diagnostics"], ["-m", "exoma", "inspect"],
                      ["-m", "exoma", "-c", "inspect"], ["-m", "exoma", "--command=inspect"]):
            env = {**os.environ, "EXOMA_HOME": str(missing)}
            observed = subprocess.run([sys.executable, *entry, "--work-dir", str(root), "--session", "cancel-session"],
                                      env=env, capture_output=True, text=True, timeout=10, **no_console())
            assert_true(observed.returncode == 0 and json.loads(observed.stdout)["meta"]["read_only"]
                        and not missing.exists(), "public and standalone inspect bypass eager managers")
        with patch.object(manager, "_kill_worker", return_value={"ok": False, "reason": "identity_unverifiable"}):
            refused = manager.cancel(job["job_id"], "cancel-session", str(root))
        assert_true(refused["meta"]["error_type"] == "cancellation_pending" and
                    manager.get_job(job["job_id"])["status"] == "running",
                    "cleanup-unconfirmed cancellation remains active")
        manager.complete_job(job["job_id"], {"ok": True, "content": "late"})
        assert_true(manager.get_job(job["job_id"])["status"] == "running" and
                    manager.get_job(job["job_id"])["late_output"]["content"] == "late",
                    "late completion cannot erase cancellation intent or release ownership")
        assert_true(not manager.release_dead_session_lock("cancel-session", job["job_id"]),
                    "different request cannot reclaim cleanup-unconfirmed cancellation")
        assert_true(manager.release_stale_session_locks()["kept"] == 1,
                    "clean retains cancellation ownership after the worker disappears")
        reaped = manager.reap_stalled(job["job_id"], "worker_stalled", "old probe", "inspect",
                                     observed=job)
        assert_true(reaped["meta"]["error_type"] == "cancellation_pending" and
                    manager.get_job(job["job_id"])["status"] == "running" and
                    manager.active_job_for_session("cancel-session")["job_id"] == job["job_id"],
                    "in-flight probe cannot overwrite cancellation or release the session")
        manager.touch_heartbeat(job["job_id"], {"progress_schema": 1, "semantic_progress_supported": False,
                                               "diagnostic": "retry_or_transport_error",
                                               "work_idle_seconds": 50, "idle_seconds": 0})
        assert_true(manager._idle_seconds(job) < 2,
                    "unknown event schema keeps output-idle fallback after a diagnostic")
        manager.set_worker_pid(job["job_id"], os.getpid())
        cancelled = manager.acknowledge_cancellation(job["job_id"], {"ok": True, "reason": "fixture_worker_cleanup"})
        assert_true(_verify_exit_code("cancel", cancelled) == 130 and
                    manager.get_job(job["job_id"])["status"] == "failed",
                    "confirmed explicit cancellation has a typed terminal result and exit 130")
        assert_true(manager.claim_recovery(job["job_id"])["action"] == "attach", "cancelled job never auto-recovers")
        assert_true(manager.cancel(job["job_id"], "cancel-session", str(root))["ok"], "repeat cancellation is idempotent")
        completed = manager.create_job("analyze", "done", "completed-session", str(root), None)
        manager.complete_job(completed["job_id"], {"ok": True, "content": "original"})
        manager.cancel(completed["job_id"], "completed-session", str(root))
        assert_true(manager.get_job(completed["job_id"])["output"]["content"] == "original",
                    "completion winning the mutation race stays completed")
        reservation = manager.reserve_update(root / "operation", "owner-token")
        try:
            manager.assert_update_admission()
        except ValueError:
            pass
        else:
            raise AssertionError("new provider admission ignored an update reservation")
        reservation.unlink()
        import io
        from contextlib import redirect_stderr
        from exoma.core.jobs import job_lifecycle
        manager.touch_heartbeat(job["job_id"], {"diagnostic": "network_denied", "retry_count": 1})
        notices = io.StringIO()
        with patch.object(main, "JOB_MANAGER", manager), \
             patch.object(main, "_warn_if_workspace_stale"), \
             patch.object(job_lifecycle, "submit", return_value={"ok": True, "job_id": job["job_id"], "meta": {}}) as submit, \
             patch.object(job_lifecycle, "get_result", side_effect=[{"status": "running"}, {"status": "completed", "output": {"ok": True}}]), \
             redirect_stderr(notices):
            job_lifecycle.await_job("analyze", "task", "cancel-session", str(root), poll_interval=0.01)
        assert_true("[JOB]" in notices.getvalue() and "network_denied" in notices.getvalue()
                    and submit.call_count == 1, "early diagnosis is visible while collecting one provider job")
        for pending_type in ("cleanup_pending", "cancellation_pending"):
            with patch.object(main, "JOB_MANAGER", manager), patch.object(main, "_warn_if_workspace_stale"), \
                 patch.object(job_lifecycle, "submit", return_value={"ok": True, "job_id": job["job_id"], "meta": {}}), \
                 patch.object(job_lifecycle, "get_result", return_value={"status": "running", "content": "cleanup unconfirmed",
                     "meta": {"error_type": pending_type, "recoverable": False}}) as read, redirect_stderr(io.StringIO()):
                pending = job_lifecycle.await_job("analyze", "task", "cancel-session", str(root), poll_interval=0.01)
            assert_true(pending["meta"]["error_type"] == pending_type and read.call_count == 1,
                        "unconfirmed cleanup is reported immediately without waiting or probing again")
        from exoma.utils import osutil
        ready = root / "real-child.json"
        child_code = ("import json,os,pathlib,subprocess,sys,time; "
                      "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                      "pathlib.Path(" + repr(str(ready)) + ").write_text(json.dumps([os.getpid(),p.pid])); time.sleep(60)")
        process = subprocess.Popen([sys.executable, "-c", child_code], **osutil.detached_popen_kwargs())
        waiter = threading.Thread(target=process.wait, daemon=True)
        waiter.start()
        try:
            until = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < until:
                time.sleep(0.02)
            assert_true(ready.exists(), "real cancellable process tree starts")
            identities = [(pid, osutil.pid_create_time(pid), osutil.process_identity(pid)) for pid in json.loads(ready.read_text())]
            live = manager.create_job("analyze", "real cancellation", "real-cancel-session", str(root), None)
            manager.set_worker_pid(live["job_id"], process.pid)
            manager.mark_running(live["job_id"])
            with patch("exoma.core.jobs.job_manager._CANCEL_WAIT_SECONDS", 0.05):
                outcome = manager.cancel(live["job_id"], "real-cancel-session", str(root))
            deadline = time.monotonic() + 5
            while any(osutil.process_generation(pid, created, identity) not in {"dead", "reused"}
                      for pid, created, identity in identities) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert_true(outcome["meta"]["error_type"] == "cancellation_pending" and
                        all(osutil.process_generation(pid, created, identity) in {"dead", "reused"}
                            for pid, created, identity in identities),
                        "forced cleanup stops known generations but cannot exclude an unsnapshotted spawn")
        finally:
            if process.poll() is None:
                osutil.terminate_tree(process)
            waiter.join(timeout=5)
        _check_worker_provider_cancellation(root)


def _check_worker_provider_cancellation(root):
    """Real run_worker + production adapter: provider has a separate process session."""
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from tests.checks.support import no_console
    from exoma.utils import osutil
    manager = JobManager(root / "provider-jobs")
    for racing, failed_spawn in ((False, False), (True, False), (False, True)):
        name = "exception" if failed_spawn else "race" if racing else "normal"
        ready = root / ("provider-" + name + ".json")
        before_spawn, release = ready.with_suffix(".before"), ready.with_suffix(".release")
        job = manager.create_job("analyze", "task", "provider-" + name, str(root), None)
        provider_code = (
            "import json,os,pathlib,subprocess,sys,time\n"
            "from exoma.utils import osutil\n"
            "options=osutil.hidden_run_kwargs(); options.pop('start_new_session',None)\n"
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],**options)\n"
            "identities=[(pid,osutil.pid_create_time(pid),osutil.process_identity(pid)) for pid in (os.getpid(),p.pid)]\n"
            "pathlib.Path(" + repr(str(ready)) + ").write_text(json.dumps(identities))\n"
            "time.sleep(60)\n")
        helper = root / (name + "-worker.py")
        helper.write_text(
            "import json,os,pathlib,subprocess,sys,time\nfrom unittest.mock import patch\n"
            "from exoma import cli\nfrom exoma.core.jobs.job_manager import JobManager\n"
            "from exoma.core.jobs.job_lifecycle import run_worker\n"
            "from exoma.adapters.providers.codex_adapter import CodexAdapter\n"
            "from exoma.utils import osutil\n"
            "cli.JOB_MANAGER=JobManager(" + repr(str(manager.job_dir)) + ")\n"
            "real_popen=subprocess.Popen\n"
            "def spawn(*args,**kwargs):\n"
            "    pathlib.Path(" + repr(str(before_spawn)) + ").touch()\n"
            "    while not pathlib.Path(" + repr(str(release)) + ").exists(): time.sleep(.01)\n"
            "    p=real_popen(*args,**kwargs)\n"
            "    while not pathlib.Path(" + repr(str(ready)) + ").exists() and p.poll() is None: time.sleep(.01)\n"
            "    return p\n"
            "def execute(*args,on_progress=None,**kwargs):\n"
            "    adapter=CodexAdapter(on_progress=on_progress); adapter.poll_interval=.05\n"
            "    argv=[sys.executable,'-c'," + repr(provider_code) + "]\n"
            "    if " + repr(failed_spawn) + ":\n"
            "        p=real_popen(argv,**osutil.hidden_run_kwargs())\n"
            "        while not pathlib.Path(" + repr(str(ready)) + ").exists() and p.poll() is None: time.sleep(.01)\n"
            "        raise RuntimeError('fixture exception after spawning a provider')\n"
            "    if " + repr(racing) + ":\n"
            "        with patch('exoma.adapters.providers.codex_adapter.subprocess.Popen',side_effect=spawn):\n"
            "            adapter._popen_capture(argv,'',None,30,'agent',None)\n"
            "    else: adapter._popen_capture(argv,'',None,30,'agent',None)\n"
            "    return {'ok':True}\n"
            "cli.run=execute\n"
            "result=run_worker(" + repr(job["job_id"]) + ")\n"
            "print(json.dumps(result),flush=True)\n"
            "raise SystemExit(cli._verify_exit_code('worker',result))\n", encoding="utf-8")
        env = {**os.environ, "EXOMA_HOME": str(root / "worker-home"), "WORKFLOW_JOB_WORKER": job["job_id"],
               "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(main.__file__).resolve().parents[1]), os.environ.get("PYTHONPATH")]))}
        interpreter = sys.executable
        if os.name == "nt" and racing:
            venv = root / "redirector-venv"
            made = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)],
                                  capture_output=True, text=True, timeout=30, **no_console())
            assert_true(made.returncode == 0, "real Windows venv redirector fixture: " + made.stderr)
            interpreter = str(venv / "Scripts" / "python.exe")
        with (root / "worker-output.log").open("ab") as log:
            process = subprocess.Popen([interpreter, str(helper)], env=env, stdout=log, stderr=log,
                                       **osutil.detached_popen_kwargs())
        try:
            marker = before_spawn if racing else ready
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert_true(marker.exists(), "worker reaches production provider boundary: " + (root / "worker-output.log").read_text(errors="replace"))
            authoritative = manager.get_job(job["job_id"])["worker_pid"]
            if not failed_spawn:
                manager.set_launcher_pid(job["job_id"], process.pid)  # Deliberately late parent registration.
                assert_true(manager.get_job(job["job_id"])["worker_pid"] == authoritative,
                            "late launcher registration preserves the authoritative worker identity")
                if os.name == "nt" and racing:
                    assert_true(authoritative != process.pid, "the real venv redirector and interpreter have distinct PIDs")
            if failed_spawn:
                process.wait(timeout=10)
                identities = json.loads(ready.read_text())
                result = manager.get_result(job["job_id"])
                assert_true(not manager.get_job(job["job_id"]).get("cancellation"),
                            "ordinary cleanup never invents explicit cancellation provenance")
                if sys.platform != "darwin":
                    assert_true(result["status"] == "failed" and
                                all(osutil.process_generation(pid, created, identity) in {"dead", "reused"}
                                    for pid, created, identity in identities) and
                                manager.active_job_for_session(job["session_id"]) is None,
                                "exception after spawn cleans native ownership before releasing the session")
                else:
                    assert_true(result["meta"]["error_type"] == "cleanup_pending",
                                "unattributable exception descendants retain native ownership")
                    for pid, created, identity in identities:
                        if osutil.process_generation(pid, created, identity) == "matched":
                            osutil.terminate_tree(None, pid=pid, expected_create_time=created)
                continue
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(manager.cancel, job["job_id"], job["session_id"], str(root))
                if racing:
                    deadline = time.monotonic() + 5
                    while not manager.get_job(job["job_id"]).get("cancellation") and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert_true(manager.get_job(job["job_id"]).get("cancellation"), "intent is durable before racing spawn continues")
                    release.touch()
                outcome = pending.result(timeout=20)
            identities = json.loads(ready.read_text())
            assert_true(outcome["meta"]["error_type"] == "cancelled" and process.wait(timeout=5) == 130
                        and all(osutil.process_generation(pid, created, identity) in {"dead", "reused"}
                                for pid, created, identity in identities)
                        and manager.active_job_for_session(job["session_id"]) is None,
                        "worker acknowledges cleanup across provider sessions and spawn/cancel races: " +
                        repr({"racing": racing, "outcome": outcome, "status": process.poll(),
                              "generations": [osutil.process_generation(pid, created, identity) for pid, created, identity in identities],
                              "active": manager.active_job_for_session(job["session_id"]),
                              "log": (root / "worker-output.log").read_text(errors="replace")[-2000:]}))
        finally:
            release.touch()
            if process.poll() is None:
                osutil.terminate_tree(process)
    _check_worker_cleanup_uncertainty(root)


def _check_worker_cleanup_uncertainty(root):
    from exoma.core.jobs import job_lifecycle
    from exoma.core.evidence.contract import make_error
    from unittest.mock import MagicMock, patch
    from exoma.core.runtime import upgrade
    (root / ".workflow").mkdir(exist_ok=True)
    manager = JobManager(root / "cleanup-jobs")
    for crashes in (False, True):
        session = "cleanup-crash" if crashes else "cleanup-normal"
        job = manager.create_job("analyze", "task", session, str(root), None)
        owner = MagicMock()
        owner.cleanup.side_effect = OSError("fixture native cleanup unconfirmed")
        run_options = {"side_effect": RuntimeError("fixture provider exception")} if crashes else {
            "return_value": {"ok": True, "content": "ordinary completion"}}
        with patch.object(main, "JOB_MANAGER", manager), patch.object(main, "run", **run_options), \
             patch.object(job_lifecycle, "WorkerOwnership", return_value=owner), \
             patch.dict(os.environ, {"WORKFLOW_JOB_WORKER": job["job_id"]}):
            result = job_lifecycle.run_worker(job["job_id"])
        current = manager.get_job(job["job_id"])
        assert_true(result["meta"]["error_type"] == "cleanup_pending" and current["status"] == "running"
                    and not current.get("cancellation") and owner.cleanup.called,
                    "normal and exception cleanup failures retain ownership without cancellation provenance")
        with patch.object(manager, "liveness", return_value="dead"):
            assert_true(not manager.release_dead_session_lock(session, job["job_id"])
                        and manager.claim_recovery(job["job_id"])["action"] == "attach",
                        "unconfirmed native cleanup blocks both reclaim and recovery")
            with patch("exoma.core.jobs.job_manager.JobManager", return_value=manager), \
                 patch("exoma.core.runtime.migrations.run") as migrate:
                assert_true(any(row["job_id"] == job["job_id"] for row in upgrade.active_jobs_for_workspace(root)),
                            "workspace admission retains cleanup uncertainty before dead-worker filtering")
                try:
                    upgrade.upgrade_workflow_workspace(root)
                except ValueError:
                    pass
                else:
                    raise AssertionError("workspace upgrade ignored dead-worker cleanup uncertainty")
                assert_true(not migrate.called, "retained cleanup prevents workspace migration")
        manager.complete_job(job["job_id"], {"ok": True})
        manager.fail_job(job["job_id"], "late failure", output=make_error("worker_crashed", "late", "inspect"))
        assert_true(manager.get_job(job["job_id"])["status"] == "running" and
                    manager.active_job_for_session(session)["job_id"] == job["job_id"],
                    "late terminal writes cannot erase ownership uncertainty")
        assert_true(manager.release_stale_session_locks()["kept"] >= 1,
                    "clean retains ordinary cleanup uncertainty")
        with patch.object(manager, "_kill_worker", return_value={"ok": False, "reason": "fixture"}):
            reaped = manager.reap_stalled(job["job_id"], "worker_stalled", "old probe", "inspect")
        assert_true(reaped["status"] == "running" and manager._lock_path(session).exists(),
                    "stall reaping cannot publish terminal completion before native cleanup")
        stored = manager._path(job["job_id"]).read_bytes()
        manager._path(job["job_id"]).write_text("{broken", encoding="utf-8")
        manager._release_after_finish(job["job_id"], None)
        assert_true(manager._lock_path(session).exists() and
                    not manager._release_lock_path(manager._lock_path(session), force=True),
                    "lock retains cleanup proof even when the job record cannot be read")
        manager._path(job["job_id"]).write_bytes(stored)
        for missing in (False, True):
            if missing:
                manager._path(job["job_id"]).unlink()
            else:
                manager._path(job["job_id"]).write_text("{broken", encoding="utf-8")
            try:
                try:
                    manager.reserve_update(root / "missing-record-update", "fixture")
                except ValueError:
                    pass
                else:
                    raise AssertionError("package update ignored retained cleanup after job-record loss")
                with patch("exoma.core.jobs.job_manager.JobManager", return_value=manager), \
                     patch("exoma.core.runtime.migrations.run") as migrate:
                    try:
                        upgrade.upgrade_workflow_workspace(root)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("workspace update ignored retained cleanup after job-record loss")
                    assert_true(not migrate.called, "missing/unreadable records never authorize migration")
            finally:
                manager._path(job["job_id"]).write_bytes(stored)
        assert_true(not manager.retained_cleanup_claims(root.with_name(root.name + "-unrelated")),
                    "known workspace cleanup scope does not block an unrelated workspace")
        claim_path = manager._lock_path(session)
        claim = manager._read_lock(claim_path)
        try:
            claim_path.write_text(json.dumps({**claim, "work_dir": "."}), encoding="utf-8")
            manager._path(job["job_id"]).unlink()
            assert_true(manager.retained_cleanup_claims(root.with_name(root.name + "-another-cwd")),
                        "stored relative workspace scope cannot be resolved against a later caller's cwd")
        finally:
            claim_path.write_text(json.dumps(claim), encoding="utf-8")
            manager._path(job["job_id"]).write_bytes(stored)
        try:
            manager.reserve_update(root / "blocked-update", "fixture")
        except ValueError:
            pass
        else:
            raise AssertionError("uncertain normal cleanup admitted an update")
