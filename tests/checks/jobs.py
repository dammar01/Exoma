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
        recovered = main.await_job(
            "analyze",
            "orphan",
            "orphan-session",
            str(root),
            None,
            poll_interval=0.01,
            poll_timeout=1,
        )
        assert_true(
            recovered.get("ok")
            and recovered.get("content") == "recovered orphan"
            and claim_calls >= 2
            and calls == [orphan["job_id"]]
            and (manager.get_job(orphan["job_id"]) or {}).get("recovery_attempt") == 0,
            f"await must reclaim an orphaned pre-spawn reservation after grace: {recovered}",
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
            attach_orphan["job_id"], poll_interval=0.01, poll_timeout=1
        )
        assert_true(
            attached_status.get("status") == "completed"
            and calls == [attach_orphan["job_id"]],
            f"check --wait must reclaim an orphaned pre-spawn reservation: {attached_status}",
        )
    finally:
        release.set()
        main._spawn_worker = original_spawn
        main.JOB_MANAGER = original_manager
        check.JOB_MANAGER = original_check_manager
        _shutil.rmtree(root, ignore_errors=True)
