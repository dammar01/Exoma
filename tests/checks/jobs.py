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
    no_console,
)

def _test_submit_admission() -> None:
    """Initial dispatch and global capacity must admit exactly one worker."""
    _check_delegation_collection()
    _check_selected_checkpoint()
    _check_first_dispatch_recovery()
    _check_mutation_admission()
    _check_reboot_cleanup()
    _check_replaced_cleanup_owner()
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


def _check_mutation_admission() -> None:
    """Mutation needs proof of death and a durable fence against late startup."""
    from contextlib import contextmanager
    from unittest.mock import patch
    from exoma.utils import osutil

    base = {"status": "pending", "worker_pid": None, "created_at": "2020-01-01T00:00:00+00:00",
            "reservation_owner_pid": 42424242, "reservation_owner_identity": "native:132223104000000041"}
    with tempfile.TemporaryDirectory(prefix="mutation-admission-") as tmp:
        root = Path(tmp)
        manager = JobManager(root / "classify")
        cases = (
            ("dead owner", {}, "dead", None, "retire_startup"),
            ("reused owner", {}, "alive", "native:132223104000000042", "retire_startup"),
            ("live owner", {}, "alive", "native:132223104000000041", "block"),
            ("unknown owner", {}, "unknown", "replacement", "block"),
            ("inaccessible identity", {}, "alive", None, "block"),
            ("legacy without owner", {"reservation_owner_pid": None}, "dead", None, "block"),
            ("legacy dead PID", {"reservation_owner_identity": None}, "dead", None, "retire_startup"),
            ("legacy live PID", {"reservation_owner_identity": None}, "alive", None, "block"),
            ("bad timestamp", {"created_at": "invalid"}, "dead", None, "block"),
            ("future timestamp", {"created_at": "2999-01-01T00:00:00+00:00"}, "dead", None, "block"),
            ("registered without PID", {"worker_registered": True}, "dead", None, "block"),
            ("pending cancellation", {"cancellation": {"cleanup_confirmed": False}}, "dead", None, "block"),
            ("pending cleanup", {"ownership": {"cleanup_confirmed": False}}, "dead", None, "block"),
            ("terminal cleanup", {"status": "failed", "ownership": {"cleanup_confirmed": False}}, "dead", None, "block"),
            ("terminal", {"status": "failed"}, "unknown", None, "ignore"),
            ("dead worker", {"worker_pid": 777}, "dead", None, "ignore"),
            ("unknown worker", {"worker_pid": 777}, "unknown", None, "block"),
            ("live launcher", {"launcher_pid": 777, "launcher_identity": "native:132223104000000041"}, "alive", "native:132223104000000041", "block"),
        )
        for label, changes, state, identity, expected in cases:
            with patch.object(osutil, "process_state", return_value=state), \
                 patch.object(osutil, "process_identity", return_value=identity):
                assert_true(manager.mutation_admission({**base, **changes}) == expected, label)
        with patch.object(osutil, "process_state", return_value="dead"):
            for age, expected in ((29.999, "block"), (30.0, "retire_startup")):
                with patch.object(manager, "_age_seconds", return_value=age):
                    assert_true(manager.mutation_admission(base) == expected, "startup grace boundary")
        for current, expected in ((132223104000000041, "block"), (132223104000000042, "retire_startup"), (None, "block")):
            with patch.object(osutil, "process_state", return_value="alive"), \
                 patch.object(osutil, "pid_create_time", return_value=current):
                assert_true(manager.mutation_admission({**base, "reservation_owner_identity": None,
                            "reservation_owner_create_time": 132223104000000041}) == expected, "create-time proof fallback")
        boot_id = "991b0d3a-7f97-4d00-bc0d-9adc67a9acfd"
        proc = f"proc:{boot_id}:77"
        ps = "ps:Wed Nov 25 12:00:00 2026"
        for recorded, observed, expected in (
            (proc, proc, "alive"), (proc, f"proc:{boot_id}:78", "gone"),
            ("proc:unknown-boot:77", proc, "unknown"),
            ("proc:00000000-0000-0000-0000-000000000000:77", proc, "unknown"),
            ("native:42", "native:132223104000000042", "unknown"),
            (ps, ps, "alive"), (ps, "ps:Wed Nov 25 13:00:00 2026", "unknown"),
        ):
            with patch.object(osutil, "process_state", return_value="alive"), \
                 patch.object(osutil, "process_identity", return_value=observed):
                assert_true(manager.admission_process_state({"worker_pid": 42, "worker_identity": recorded},
                                                            "worker") == expected, "compare only proven generations")

        def fixture(name):
            store = JobManager(root / name)
            job = store.create_job("analyze", name, name, str(root), None)
            job.update(base)
            store._save(job)
            return store, job

        with patch.object(osutil, "process_state", return_value="dead"):
            store, job = fixture("retired")
            reservation = store.reserve_update(root / "operation", "token")
            current = store.get_job(job["job_id"])
            assert_true(reservation.is_file() and current["status"] == "failed" and current["reaped"]
                        and not store._lock_path(job["session_id"]).exists(), "orphan retires before update admission")
            reservation.unlink()
            store.set_launcher_pid(job["job_id"], os.getpid())
            store.set_worker_pid(job["job_id"], os.getpid())
            assert_true(store.mark_running(job["job_id"])["status"] == "failed"
                        and store.get_job(job["job_id"])["worker_pid"] is None,
                        "late startup cannot revive the terminal job even after the update ends")

            # Deterministically let registration/recovery/cancellation win the job lock.
            for winner in ("registration", "recovery", "cancellation"):
                store, job = fixture(winner)
                original = store._job_mutation
                changed = False

                @contextmanager
                def interleave(job_id):
                    nonlocal changed
                    if not changed:
                        changed = True
                        record = store.get_job(job_id)
                        if winner == "registration":
                            record.update(worker_pid=os.getpid(), worker_identity="live")
                        elif winner == "recovery":
                            record.update(reservation_owner_pid=os.getpid(), reservation_owner_identity="live")
                        else:
                            record["cancellation"] = {"cleanup_confirmed": False}
                        store._save(record)
                    with original(job_id):
                        yield

                def state(pid):
                    return "alive" if pid == os.getpid() else "dead"

                with patch.object(store, "_job_mutation", interleave), \
                     patch.object(osutil, "process_state", side_effect=state), \
                     patch.object(osutil, "process_identity", return_value="live"):
                    try:
                        store.reserve_update(root / "operation", "token")
                    except ValueError:
                        pass
                    else:
                        raise AssertionError(winner + " must prevent retirement/admission")
                assert_true(store.get_job(job["job_id"])["status"] == "pending"
                            and not (store.job_dir / ".update-reservation.json").exists(), winner + " wins safely")

            store, job = fixture("save-failure")
            with patch.object(store, "_save", side_effect=OSError("retirement persistence failed")):
                try:
                    store.reserve_update(root / "operation", "token")
                except OSError:
                    pass
                else:
                    raise AssertionError("failed persistence must refuse admission")
            assert_true(store.get_job(job["job_id"])["status"] == "pending"
                        and store._lock_path(job["session_id"]).exists()
                        and not (store.job_dir / ".update-reservation.json").exists(),
                        "save failure preserves ownership and prevents update reservation")

            store, job = fixture("release-failure")
            with patch.object(store, "_release_session_lock", return_value=None):
                for attempt in range(2):
                    try:
                        store.reserve_update(root / "operation", "token")
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("unreleased ownership must refuse admission, including retry")
            assert_true(not (store.job_dir / ".update-reservation.json").exists(), "lock release is confirmed")
            store, job = fixture("retained-lock")
            path = store._lock_path(job["session_id"])
            claim = json.loads(path.read_text(encoding="utf-8"))
            claim["cleanup_required"] = True
            path.write_text(json.dumps(claim), encoding="utf-8")
            try:
                store.reserve_update(root / "operation", "token")
            except ValueError:
                pass
            else:
                raise AssertionError("retained cleanup lock must override dead-owner proof")
            assert_true(store.get_job(job["job_id"])["status"] == "pending" and path.exists(),
                        "retained cleanup cannot be converted into startup retirement")


def _check_reboot_cleanup():
    import io
    import subprocess
    import sys
    from contextlib import redirect_stdout
    from unittest.mock import patch

    from exoma.core.jobs import clean_stale
    from exoma.utils import osutil

    old = {"machine": "a" * 64, "boot_id": "1" * 32, "boot_us": 1_500_000_000_000_000}
    new = {**old, "boot_id": "2" * 32, "boot_us": 1_700_000_000_000_000}
    with tempfile.TemporaryDirectory(prefix="jobs-reboot-") as tmp:
        root = Path(tmp)
        empty = root / "empty"
        env = {**os.environ, "EXOMA_HOME": str(empty),
               "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
        for flags, role, expected in (([], "", 0), ([], "second_agent", 1),
                                      (["--dry-run"], "second_agent", 0), (["--apply"], " SECOND_AGENT ", 1)):
            proc = subprocess.run([sys.executable, "-B", "-m", "exoma", "jobs", "clean-stale", *flags],
                                  cwd=root, env={**env, "WORKFLOW_AGENT_ROLE": role},
                                  timeout=15, capture_output=True, text=True, **no_console())
            assert_true(proc.returncode == expected and not empty.exists(),
                        f"public CLI respects read-only store and role: {proc.stdout} {proc.stderr}")

        # Exercise the real Windows probe in a separate CLI process. Only the prior
        # boot is synthetic; a firmware/OS without machine evidence cannot run this case.
        observed = osutil.windows_boot_evidence()
        if observed:
            from datetime import datetime, timezone
            import uuid
            isolated = JobManager(empty / "storage/jobs")
            earlier = {**observed, "boot_id": uuid.uuid4().hex, "boot_us": observed["boot_us"] - 120_000_000}
            stamp = datetime.fromtimestamp((earlier["boot_us"] + 60_000_000) / 1e6, timezone.utc).isoformat()
            birth = 116444736000000000 + 10 * earlier["boot_us"]
            with patch.object(osutil, "windows_boot_evidence", return_value=earlier), \
                 patch.object(osutil, "pid_create_time", return_value=birth), \
                 patch.object(isolated, "_now", return_value=stamp):
                seeded = isolated.create_job("plan", "", "native-cli", str(root), None)
                seeded.update(status="running", started_at=stamp, reservation_owner_pid=None)
                isolated._save(seeded)
            for flags, expected in ((["--dry-run"], "would_clean=1"), ([], "cleaned=1"),
                                    (["--apply"], "cleaned=0")):
                proc = subprocess.run([sys.executable, "-B", "-m", "exoma", "jobs", "clean-stale", *flags],
                                      cwd=root, env={**env, "WORKFLOW_AGENT_ROLE": ""},
                                      timeout=15, capture_output=True, text=True, **no_console())
                assert_true(proc.returncode == 0 and expected in proc.stdout, proc.stdout + proc.stderr)
            assert_true(isolated._load(seeded["job_id"])["reboot_cleanup"]["observed_boot"]["boot_id"]
                        == observed["boot_id"], "native CLI records the current kernel boot GUID")

        store = JobManager(root / "jobs")
        birth = 116444736000000000 + 10 * old["boot_us"]
        identity = f"native:{birth}"

        def fixture(session):
            with patch.object(osutil, "windows_boot_evidence", return_value=old), \
                 patch.object(osutil, "pid_create_time", return_value=birth), \
                 patch.object(osutil, "process_identity", return_value=identity), \
                 patch.object(store, "_now", return_value="2020-01-01T00:00:00+00:00"):
                job = store.create_job("plan", "private task", session, str(root / session), None)
                store.set_worker_pid(job["job_id"], os.getpid())
                store.mark_running(job["job_id"])
                store.begin_worker_ownership(job["job_id"])
            return store._load(job["job_id"])

        def results(apply=False):
            return {row[0]: row[2:] for row in clean_stale.inspect_jobs(store, apply=apply)}

        missing = fixture("cancelled-no-lock")
        mid = missing["job_id"]
        missing["cancellation"] = {"targets": [], "cleanup_confirmed": False}
        store._save(missing)
        store._lock_path(missing["session_id"]).unlink()
        failed_missing = fixture("failed-cancel-no-lock")
        fid = failed_missing["job_id"]
        failed_missing.update(status="failed", error="original failure", completed_at="2020-01-02T00:00:00+00:00",
                              output={"reason": "original"}, cancellation={"targets": [], "cleanup_confirmed": False})
        store._save(failed_missing)
        store._lock_path(failed_missing["session_id"]).unlink()

        def cold_event(number, stamp):
            return (f"<Event><System><TimeCreated SystemTime='{stamp}'/><EventRecordID>{number}</EventRecordID>"
                    "</System><EventData><Data Name='BootType'>0</Data></EventData></Event>")

        previous = cold_event(7, "2017-07-14T02:40:01Z")
        latest = cold_event(8, "2023-11-14T22:13:21Z")
        with patch.object(osutil, "process_state", return_value="dead"), \
             patch.object(osutil, "windows_boot_evidence", return_value={**new, "boot_id": old["boot_id"]}), \
             patch.dict(os.environ, {"SystemRoot": str(root)}):
            with patch.object(osutil, "run_bounded", return_value=(0, f"<Events>{previous}</Events>", "")):
                assert_true(not clean_stale._distinct_cold_starts(old["boot_us"], old["boot_us"] + 250_000,
                                                                  old["boot_us"] + 500_000),
                            "a clock adjustment cannot reuse the original startup event")
            for events in (latest, previous, latest + latest, latest + previous.replace(">7<", ">9<")):
                with patch.object(osutil, "run_bounded", return_value=(0, f"<Events>{events}</Events>", "")):
                    assert_true(results()[mid][0] == "skipped", "a single or unordered event cannot prove two boots")
            with patch.object(osutil, "run_bounded", return_value=(0, f"<Events>{latest}{previous}</Events>", "")):
                assert_true(mid in store.update_job_blockers() and results()[mid][0] == "eligible"
                            and results(True)[mid][0] == "recovered" and mid not in store.update_job_blockers(),
                            "cancelled old job without a lock clears admission after a confirmed cold start")
                terminal = store._load(fid)
                assert_true(terminal["reboot_cleanup"]["prior_status"] == "failed" and fid not in store.update_job_blockers()
                            and all(terminal[k] == failed_missing[k] for k in ("status", "completed_at", "error", "output")),
                            "lockless failed cancellation is confirmed without overwriting terminal history")
                assert_true(results(True)[mid][0] == results(True)[fid][0] == "skipped" and store._load(fid) == terminal,
                            "terminal history is stable")

        ghost = fixture("pid-cleanup")
        gid = ghost["job_id"]
        ghost.pop("attempt_origin", None)
        ghost["ownership"]["cleanup_confirmed"] = True
        store._save(ghost)
        pid_lock = store._lock_path(ghost["session_id"])
        pid_lock.unlink()
        with patch.object(osutil, "process_state", return_value="unknown"):
            assert_true(results(True)[gid][0] == "skipped", "unknown PID is retained")
        with patch.object(osutil, "process_state", return_value="alive"):
            with patch.object(osutil, "process_identity", return_value=identity):
                assert_true(results(True)[gid][0] == "active", "original live process is retained")
            proc = "proc:c6d5955d-bd76-42eb-aa9f-076e9437e053:51"
            ps = "ps:Wed Nov 25 12:00:00 2026"
            for recorded, observed in (("unknown", proc), ("proc:x:7", proc), (proc, "proc:bad"),
                                       ("ps:not a date", ps), (ps, proc), (identity, "native:invalid")):
                store._save({**ghost, "worker_identity": recorded, "worker_create_time": None})
                with patch.object(osutil, "process_identity", return_value=observed):
                    assert_true(results(True)[gid][0] == "skipped" and store._load(gid)["status"] == "running",
                                "invalid or incomparable generations retain a live PID")
            store._save(ghost)
            oversized = fixture("oversized-pid")
            oid = oversized["job_id"]
            oversized.update(worker_pid=2**80)
            store._save(oversized)
            def pid_state(pid):
                if pid == oversized["worker_pid"]:
                    raise AssertionError("out-of-range PID reached native API")
                return "alive"
            with patch.object(osutil, "process_identity", return_value="native:132223104000000042"):
                pid_lock.write_text("{", encoding="utf-8")
                assert_true(results(True)[gid][0] == "skipped" and store._load(gid) == ghost,
                            "unreadable lock cannot act as an absent lock")
                pid_lock.unlink()
                with patch.object(osutil, "process_state", side_effect=pid_state):
                    batch = results(True)
                    assert_true(batch[gid][0] == "recovered" and batch[oid][0] == "skipped"
                                and store._load(gid)["status"] == "failed" and store._load(oid) == oversized,
                                "oversized PID stays unresolved while a verified reused PID is retired")
            # This test record intentionally remains a blocker; keep later fixtures independent.
            store._path(oid).unlink()
            store._lock_path(oversized["session_id"]).unlink()

        interrupted = fixture("pid-write-interrupted")
        iid = interrupted["job_id"]
        with patch.object(osutil, "process_identity", return_value=identity):
            store.confirm_worker_cleanup(iid)
        with patch.object(osutil, "process_state", return_value="dead"), \
             patch.object(osutil, "windows_boot_evidence", return_value=old):
            with patch.object(store, "_save", side_effect=OSError("interrupted")):
                assert_true(results(True)[iid][0] == "error" and iid in store.update_job_blockers(),
                            "failed PID cleanup write retains admission")
            assert_true(results(True)[iid][0] == "recovered" and iid not in store.update_job_blockers()
                        and store._load(iid)["pid_cleanup"]["reason"] == "worker_process_gone",
                        "retry commits terminal history and releases only its claim")

        job = fixture("running")
        failed = fixture("failed")
        failed.update(status="failed", error="original error", output={"typed": "original"},
                      completed_at="2020-01-02T00:00:00+00:00")
        failed["cancellation"] = {"reason": "original request", "cleanup_confirmed": False}
        store._save(failed)
        jid, fid = job["job_id"], failed["job_id"]
        lock = store._lock_path(job["session_id"])
        owner = store._read_lock(lock)
        stalled = fixture("reaped-running")
        with patch.object(store, "_kill_worker", return_value={"ok": False}), \
             patch.object(store, "_now", return_value="2020-01-02T00:00:00+00:00"):
            store.reap_stalled(stalled["job_id"], "worker_died", "stalled while still owning children", "inspect")
        before = store._load(stalled["job_id"])
        assert_true(before["status"] == "running" and before["reaped"], "stall reaper retained ownership")
        with patch.object(osutil, "process_state", return_value="dead"):
            for boot in (None, old, {**new, "boot_id": "0" * 32}, {**new, "machine": "f" * 64},
                         {**new, "boot_us": old["boot_us"]}):
                with patch.object(osutil, "windows_boot_evidence", return_value=boot):
                    assert_true(results(True)[jid][0] == "skipped" and lock.exists(),
                                "missing, same, foreign or chronologically conflicting boot retains descendants")
            with patch.object(osutil, "windows_boot_evidence", return_value=new):
                assert_true(results()[jid][0] == "eligible" and store._load(jid) == job,
                            "preview recognizes prior boot and preserves history")
                with patch.object(osutil, "process_state", return_value="alive"), \
                     patch.object(osutil, "process_identity", return_value=identity):
                    assert_true(results(True)[jid][0] == "active", "a matching live worker wins")
                too_late = 116444736000000000 + 10 * new["boot_us"]
                store._save({**job, "worker_create_time": too_late, "worker_identity": f"native:{too_late}"})
                assert_true(results(True)[jid][0] == "skipped" and lock.exists(),
                            "an absent PID created on the current boot contradicts the recorded origin")
                store._save({**job, "cancellation": {"targets": [{"worker_pid": 73, "worker_identity": identity}]}})
                with patch.object(osutil, "process_state", side_effect=lambda pid: "alive" if pid == 73 else "dead"), \
                     patch.object(osutil, "process_identity", return_value=identity):
                    assert_true(results()[jid][0] == "skipped", "conflicting live descendant evidence retains ownership")
                store._save({**job, "attempt_origin": None})
                assert_true(results()[jid][0] == "skipped", "legacy record retains its claim")
                store._save(job)
                lock.write_text(json.dumps({**owner, "token": "replacement"}), encoding="utf-8")
                assert_true(clean_stale._repair(store, job, owner)[0] == "skipped"
                            and store._read_lock(lock)["token"] == "replacement", "replacement lock survives")
                lock.write_text(json.dumps(owner), encoding="utf-8")
                with patch.object(store, "_save", side_effect=OSError("disk full")):
                    assert_true(results(True)[jid][0] == "error" and lock.exists()
                                and store._load(jid) == job, "persistence failure retains ownership")

                original_unlink = Path.unlink
                def interrupt_release(path, *args, **kwargs):
                    if path == lock:
                        raise OSError("interrupted release")
                    return original_unlink(path, *args, **kwargs)

                with patch.object(Path, "unlink", interrupt_release):
                    assert_true(results(True)[jid][0] == "error", "interrupted after audit publication")
                terminal = store._load(jid)
                saved = store._load(fid)
                fenced = store._load(stalled["job_id"])
                assert_true(fenced["status"] == "failed" and "reboot_cleanup" in fenced
                            and all(fenced[k] == before[k] for k in ("error", "completed_at", "output"))
                            and store.get_result(stalled["job_id"])["content"] == before["error"],
                            "reboot recovery preserves the stall reaper's original failure")
                assert_true(all(saved[k] == failed[k] for k in ("error", "output", "completed_at"))
                            and saved["cancellation"]["cleanup_confirmed"]
                            and saved["ownership"]["cleanup_confirmed"], "failed history survives")
                assert_true(terminal["status"] == "failed" and terminal["reboot_cleanup"]["prior_status"] == "running"
                            and jid in store.update_job_blockers(), "retained lock blocks update until retry")
                snapshot = osutil.open_snapshot
                def unreadable_claim(path, *args, **kwargs):
                    if path == lock:
                        raise PermissionError("fixture transient claim read failure")
                    return snapshot(path, *args, **kwargs)
                with patch.object(osutil, "open_snapshot", side_effect=unreadable_claim):
                    store.prune_jobs(ttl_days=-1, keep_last=0)
                assert_true(store._load(jid) == terminal, "pruning preserves retained recovery history")
                mutation = store._mutation_path(jid)
                pending = mutation.with_suffix(".claim.pending")
                crash_writer = """
import json, os, sys
from exoma.core.jobs.job_manager import JobManager
def interrupt(payload, stream):
    stream.write("{")
    stream.flush()
    os._exit(29)
json.dump = interrupt
with JobManager(sys.argv[1], initialize=False)._job_mutation(sys.argv[2], reclaim=False):
    raise AssertionError("unpublished claim entered critical section")
"""
                crash = subprocess.run([sys.executable, "-B", "-c", crash_writer, str(store.job_dir), jid],
                                       cwd=root, env=env, timeout=15, capture_output=True, **no_console())
                assert_true(crash.returncode == 29 and pending.exists() and not mutation.exists() and lock.exists(),
                            "process exit while staging leaves no partial published claim")
                with store._job_mutation(jid, reclaim=False):
                    assert_true(store._read_side(mutation)["guarded_mutation"] and not pending.exists(),
                                "next writer acquires a complete claim after interruption")
                # Abrupt process exit releases the kernel guard but leaves the mutation claim.
                store._mutation_path(jid).write_text(json.dumps({"guarded_mutation": True, "token": "old"}),
                                                     encoding="utf-8")
                assert_true(results(True)[jid][0] == "recovered" and store._load(jid) == terminal
                            and not lock.exists(), "retry releases exact owner and keeps original audit")
                assert_true(results(True)[jid][0] == "skipped" and not store.update_job_blockers(),
                            "repeated cleanup has no further action")
                orphan = store.lock_dir / "orphan.lock"
                orphan.write_text(json.dumps({"job_id": "lost", "cleanup_required": True}), encoding="utf-8")
                (store.job_dir / "job_bad.json").write_text("{", encoding="utf-8")
                (root / ".workflow").mkdir(exist_ok=True)
                (root / ".workflow/config.json").write_text("{}", encoding="utf-8")
                output = io.StringIO()
                with patch.object(clean_stale, "JobManager", return_value=store), \
                     patch.object(Path, "cwd", return_value=root), redirect_stdout(output):
                    code = clean_stale.main(["clean-stale", "--apply"])
                assert_true(code == 1 and orphan.exists() and "update_blockers=" in output.getvalue()
                            and int(output.getvalue().split("update_blockers=")[1]) == 2
                            and "job_bad.json" not in output.getvalue() and jid not in output.getvalue()
                            and "private task" not in output.getvalue(), "reports all remaining blockers")

        retry = fixture("recovery")
        rid = retry["job_id"]
        # Recovery only begins when prior ownership has already been confirmed.
        with patch.object(osutil, "process_identity", return_value=identity):
            store.confirm_worker_cleanup(rid)
        with patch.object(osutil, "windows_boot_evidence", return_value=new), \
             patch.object(osutil, "process_generation", return_value=osutil.PROCESS_DEAD):
            selected = store.claim_recovery(rid)
            store.release_recovery_claim(rid)
            assert_true(selected["action"] == "recover", "fixture recovered the same job")
            origin = selected["job"]["attempt_origin"]
            assert_true(origin["boot"] == new and origin["attempt"] != retry["attempt_origin"]["attempt"]
                        and store._read_lock(store._lock_path("recovery"))["attempt_origin"] == origin,
                        "recovery replaces both copies of the latest attempt before launch")
            store.mark_running(rid)
            assert_true(results(True)[rid][0] == "skipped", "first creation before reboot cannot retire a new attempt")


def _check_replaced_cleanup_owner():
    """An old failed job with a busy guard must not affect its session's next owner."""
    import io
    from contextlib import redirect_stdout
    from unittest.mock import patch

    from exoma.core.jobs import clean_stale
    from exoma.utils import osutil

    with tempfile.TemporaryDirectory(prefix="jobs-replaced-owner-") as tmp:
        root = Path(tmp)
        store = JobManager(root / "jobs")
        old = store.create_job("plan", "first", "shared", str(root), None)
        old = store.fail_job(old["job_id"], "original failure")
        new = store.create_job("plan", "second", "shared", str(root), None)
        store.set_launcher_pid(new["job_id"], os.getpid())
        lock = store._lock_path("shared")
        owner = store._read_lock(lock)
        # Exercise a real session takeover and each independent ownership mismatch.
        claims = (owner, {**owner, "token": old["lock_token"]},
                  {**owner, "job_id": old["job_id"]},
                  {**owner, "job_id": old["job_id"], "token": old["lock_token"], "session_id": "other"})
        with store._job_mutation(old["job_id"]), \
             patch.object(store, "_job_mutation", side_effect=AssertionError("unexpected mutation guard")), \
             patch.object(osutil, "process_state", return_value="dead"), \
             patch.object(clean_stale, "JobManager", return_value=store):
            for claim in claims:
                lock.write_text(json.dumps(claim), encoding="utf-8")
                output = io.StringIO()
                with redirect_stdout(output):
                    code = clean_stale.main(["clean-stale", "--apply"])
                assert_true(code == 0 and "cleaned=0" in output.getvalue() and old["job_id"] not in output.getvalue(),
                            "obsolete owner skips without failing an unblocked batch or printing job IDs")
                assert_true(store._load(old["job_id"]) == old and store._read_lock(lock) == claim,
                            "failed history and session claim survive")


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
        _check_job_snapshot_sharing(root)
        original_cwd = Path.cwd()
        try:
            os.chdir(root)
            relative_manager = JobManager(Path("relative-jobs"))
        finally:
            os.chdir(original_cwd)
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


def _check_job_snapshot_sharing(root):
    """Readers share publisher access and release complete snapshots promptly."""
    from unittest.mock import patch
    from concurrent.futures import ThreadPoolExecutor
    from exoma.core.jobs.diagnostics import _read
    from exoma.utils import osutil

    manager = JobManager(root / "snapshot-jobs")
    job = manager.create_job("analyze", "old ü", "snapshot-session", str(root), None)
    path = manager._path(job["job_id"])
    blocked, release = threading.Event(), threading.Event()
    real_replace = os.replace

    def replace(source, target):
        try:
            return real_replace(source, target)
        except PermissionError:
            blocked.set()
            assert_true(release.wait(5), "reader releases its snapshot before the writer retries")
            raise

    with ThreadPoolExecutor(max_workers=1) as pool, \
            patch("exoma.core.workspace.workspace_paths.os.replace", side_effect=replace):
        old = osutil.open_snapshot(path)
        try:
            assert_true(not os.get_inheritable(old.fileno()), "snapshot descriptors are not inherited by workers")
            pending = pool.submit(manager._save, {**job, "task": "new ü"})
            if os.name == "nt":
                # MoveFileEx can still require the destination reader to close.
                assert_true(blocked.wait(5) or pending.done(), "publisher reaches replacement")
            else:
                pending.result(timeout=5)
            assert_true(json.load(old)["task"] == "old ü", "a reader sees one complete old snapshot")
        finally:
            old.close()
            release.set()
        pending.result(timeout=10)
    assert_true(manager.get_job(job["job_id"])["task"] == "new ü",
                "publisher replaces the record after reader release within its existing retry policy")
    with osutil.open_snapshot(path, binary=True) as stream:
        assert_true(isinstance(stream.read(1), bytes), "bounded diagnostics can read binary snapshots")
    try:
        with osutil.open_snapshot(path.with_name("missing.json")):
            raise AssertionError("opening a snapshot must not create a missing file")
    except FileNotFoundError:
        pass

    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = osutil._windows_kernel32()
        info = kernel32.GetHandleInformation
        info.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        info.restype = wintypes.BOOL

        def assert_closed(handle):
            flags = wintypes.DWORD()
            assert_true(not info(handle, ctypes.byref(flags)) and ctypes.get_last_error() == 6,
                        "snapshot ownership closes the native handle exactly once")

        # Deterministic sharing conflict: no sleeps, thread scheduling or retry needed.
        delete_handle = kernel32.CreateFileW(str(path), 0x10000, 7, None, 3, 0x80, None)
        assert_true(delete_handle != wintypes.HANDLE(-1).value, "fixture holds DELETE access for a publisher")
        try:
            try:
                with path.open("r", encoding="utf-8"):
                    raise AssertionError("plain open unexpectedly shared the publisher's DELETE access")
            except PermissionError:
                pass
            assert_true(manager.get_job(job["job_id"])["task"] == "new ü" and _read(path)["task"] == "new ü",
                        "production job and diagnostic readers coexist with publisher DELETE access")
        finally:
            kernel32.CloseHandle(delete_handle)

        exclusive = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        assert_true(exclusive != wintypes.HANDLE(-1).value, "fixture holds an incompatible exclusive reader")
        try:
            try:
                manager.get_job(job["job_id"])
            except PermissionError as error:
                assert_true(error.winerror == 32 and error.filename == str(path),
                            "real sharing denial retains native error and filename")
            else:
                raise AssertionError("a denied read must not become a missing or successful job")
            try:
                manager.retained_cleanup_claims(root)
            except ValueError as error:
                assert_true(isinstance(error.__cause__, PermissionError), "admission retains the read failure cause")
            else:
                raise AssertionError("unreadable jobs must not authorize cleanup admission")
            assert_true(manager._read_side(path) is None and manager._read_lock(path) is None
                        and _read(path).get("unavailable"), "best-effort readers retain their unavailable policy")
        finally:
            kernel32.CloseHandle(exclusive)

        with osutil.open_snapshot(path) as stream:
            native = msvcrt.get_osfhandle(stream.fileno())
        assert_closed(native)

        real_create, real_transfer = kernel32.CreateFileW, msvcrt.open_osfhandle
        for fail_transfer in (True, False):
            handles, descriptors = [], []
            failure = OSError("fixture stream ownership failure")

            def create(*args):
                handle = real_create(*args)
                handles.append(handle)
                return handle

            def transfer(*args):
                if fail_transfer:
                    raise failure
                fd = real_transfer(*args)
                descriptors.append(fd)
                return fd

            with patch.object(kernel32, "CreateFileW", side_effect=create), \
                    patch("msvcrt.open_osfhandle", side_effect=transfer), \
                    patch("os.fdopen", side_effect=failure):
                try:
                    osutil.open_snapshot(path)
                except OSError as error:
                    assert_true(error is failure, "snapshot setup propagates its original failure")
                else:
                    raise AssertionError("snapshot setup failure was suppressed")
            assert_true(len(handles) == 1, "failed setup opens only one native handle")
            assert_closed(handles[0])
            for fd in descriptors:
                try:
                    os.fstat(fd)
                except OSError:
                    pass
                else:
                    raise AssertionError("failed stream construction leaked a descriptor")

    path.write_text("{", encoding="utf-8")
    try:
        manager.get_job(job["job_id"])
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("snapshot opening must not hide malformed job JSON")


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


def _check_worker_terminal_inventory(root):
    from types import SimpleNamespace
    from unittest.mock import patch
    from exoma import _update_worker as native
    from exoma.core.jobs import job_lifecycle, job_manager

    guardian, foreign = os.getpid(), os.getpid() + 1000000
    baseline = {1: {"created": 1, "parent": 0, "uniqueid": 11, "parent_uniqueid": 0,
                    "version": 101, "parent_version": 0},
                guardian: {"created": 2, "parent": foreign, "uniqueid": 22, "parent_uniqueid": 33,
                           "version": 202, "parent_version": 303},
                foreign: {"created": 3, "parent": 1, "status": 2, "uniqueid": 33,
                          "parent_uniqueid": 11, "version": 303, "parent_version": 101}}
    cases = (
        ("foreign-terminal", {**baseline, foreign: {**baseline[foreign], "status": 5}}, True),
        ("foreign-enumerated-then-esrch", {**baseline, foreign: {
            "created": 3, "parent": 1, "status": 2}}, True),
        ("unknown-terminal", {**baseline, foreign: {"created": "ps timestamp", "parent": 1}}, False),
        ("reused-enumeration", {**baseline, foreign: {"created": 4, "parent": 1, "status": 2}}, False),
        ("escaped-after-parent-exit", {**baseline,
            foreign + 1: {"created": 4, "parent": guardian, "status": 5, "uniqueid": 55,
                          "parent_uniqueid": 22, "version": 505, "parent_version": 202},
            foreign + 2: {"created": 5, "parent": 1, "status": 2, "uniqueid": 66,
                          "parent_uniqueid": 55, "version": 606, "parent_version": 505}}, False),
        ("reused-owned", {**baseline, foreign: {**baseline[foreign], "uniqueid": 44,
                                               "parent_uniqueid": 22, "parent_version": 202}}, False),
    )
    for label, current, accepted in cases:
        manager = JobManager(root / ("native-cleanup-" + label))
        job = manager.create_job("analyze", "task", "native-" + label, str(root), None)
        observations = iter([baseline])
        def inventory():
            return next(observations, current)
        # Exercise the real POSIX ownership wrapper/classifier on every CI OS.
        # Replace the module's os reference, never the host-wide os.name/Path ABI.
        posix_os = SimpleNamespace(**{**vars(os), "name": "posix"})
        with patch.object(main, "JOB_MANAGER", manager), \
             patch.object(main, "run", return_value={"ok": True, "content": "ordinary completion"}), \
             patch.object(job_manager, "os", posix_os), \
             patch.object(native.sys, "platform", "darwin"), \
             patch.object(native.DescendantOwner, "inventory", side_effect=inventory), \
             patch.dict(os.environ, {"WORKFLOW_JOB_WORKER": job["job_id"]}):
            result = job_lifecycle.run_worker(job["job_id"])
        stored = manager.get_job(job["job_id"])
        if accepted:
            assert_true(result["ok"] and stored["status"] == "completed"
                        and stored["ownership"]["cleanup_confirmed"]
                        and manager.active_job_for_session(job["session_id"]) is None,
                        "a terminal foreign native lifetime permits real worker completion and lock release")
        else:
            assert_true(not result["ok"] and result["meta"]["error_type"] == "cleanup_pending"
                        and stored["status"] == "running" and not stored["ownership"]["cleanup_confirmed"],
                        "unknown or reused-owned inventory retains cleanup ownership")
            try:
                manager.reserve_update(root / ("update-" + label), "fixture")
            except ValueError:
                pass
            else:
                raise AssertionError("uncertain native inventory allowed package mutation")


def _check_worker_cleanup_uncertainty(root):
    _check_worker_terminal_inventory(root)
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


def _check_selected_checkpoint():
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from exoma.core.runtime.scripts import _generate_run_scripts
    from exoma.utils.osutil import find_bash
    from tools.maintain.verify_followup_eval import REVIEW
    from unittest.mock import patch
    from exoma.core.jobs import job_lifecycle as lifecycle, clean_stale
    from exoma.core.jobs.job_manager import DEAD
    from exoma.core.runtime import execution
    from exoma.core.runtime.upgrade import active_jobs_for_workspace

    for entry in ("redispatch", "result"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home = root / "home"
            manager = JobManager(home / "storage/jobs")
            _generate_run_scripts(root)
            session = "selected-verify"

            def runner(command, *options):
                proc = subprocess.run([find_bash() or "bash", str(root / ".workflow/run.sh"), command,
                                       "--session", session, *options], env={**os.environ, "EXOMA_HOME": str(home)},
                                      cwd=root, capture_output=True, text=True, timeout=30, **no_console())
                assert proc.returncode == 0, (proc.stdout, proc.stderr)
                return json.loads(proc.stdout)
            state = execution._runtime_file(root, session, execution.EXECUTION_FILE)
            # An existing open finding from a version without reason codes.
            execution._write(state, {"execution_id": "original", "status": "active", "finding_history": {
                "version": 1, "revision": 1, "rounds": {"old": {"events": [{"id": "F-old", "status": "new"}]}},
                "findings": {"F-old": {"id": "F-old", "disposition": "open", "last_round": "old"}}}})
            def control(action, token):
                return lifecycle.delegation_control("delegate-" + action, token, session, str(root))

            with patch.object(main, "JOB_MANAGER", manager), patch.object(main, "_spawn_worker",
                    return_value={"ok": True, "meta": {"pid": os.getpid()}}) as spawn:
                generation = control("select", "verify")["meta"]["generation"]
                args = ("verify", "check", session, str(root), None, True, generation)
                jid = lifecycle._submit_selected(*args)["job_id"]
                manager.mark_running(jid)
                with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                      stdin=subprocess.PIPE, **no_console()) as worker:
                    manager.set_worker_pid(jid, worker.pid)
                    worker.communicate(timeout=10)
                payload = {"ok": True, "content": REVIEW.format(verdict="DONE", finding="none"), "meta": {
                    "verdict": "pass", "review_observations": {"blockers": []}}}
                save = manager._save
                def lose_terminal(job, **kwargs):
                    if job["status"] == "completed":
                        raise OSError("lost terminal write")
                    return save(job, **kwargs)
                fault = (patch.object(execution, "record_verification", side_effect=OSError("lost execution write"))
                         if entry == "result" else patch.object(manager, "_save", side_effect=lose_terminal))
                with fault:
                    try:
                        manager.complete_verify_round(jid, 0, payload)
                    except OSError:
                        pass
                    else:
                        raise AssertionError("missing publication fault")
                with patch.object(manager, "liveness", return_value=DEAD), patch.object(
                        manager, "_admission_process_gone", return_value=True):
                    if entry == "redispatch":
                        delivered = runner("verify", "--request", generation, "--task", "check")
                        assert delivered["meta"]["job_id"] == jid
                    else:
                        assert jid in manager.update_job_blockers()
                        assert active_jobs_for_workspace(root, manager=manager)
                        assert not active_jobs_for_workspace(root / "elsewhere", manager=manager)
                        for apply in (False, True):
                            rows = clean_stale.inspect_jobs(manager, apply=apply)
                            assert any(row[0] == jid and "publication" in row[-1] for row in rows), rows
                        stored = manager.get_job(jid)
                        assert stored["status"] == "running"
                        with patch.dict(os.environ, {"WORKFLOW_AGENT_ROLE": "second_agent"}):
                            assert manager.get_result(jid)["meta"]["reason"] == "verify_checkpoint_pending"
                        with patch.object(manager, "_read_lock", return_value={"job_id": "replacement"}):
                            assert manager.get_result(jid)["meta"]["reason"] == "verify_checkpoint_pending"
                        assert manager.get_job(jid) == stored
                        from exoma.utils.osutil import run_bounded
                        attach = [find_bash() or "bash", str(root / ".workflow/check.sh"), jid, "--wait"]
                        env = {**os.environ, "EXOMA_HOME": str(home)}
                        lock = manager._lock_path(session)
                        original_claim = lock.read_bytes()
                        try:
                            for options in ([], ["--result"]):
                                if options:
                                    lock.write_text(json.dumps({"job_id": "replacement"}), encoding="utf-8")
                                else:
                                    lock.unlink()
                                code, stdout, stderr = run_bounded(attach + options, 10, cwd=str(root), env=env)
                                assert code == 2, (options, code, stdout, stderr)
                                diagnostic = json.loads(stdout)
                                assert diagnostic["job_id"] == jid and diagnostic["status"] == "running"
                                assert diagnostic["done"] and not diagnostic["ok"]
                                assert diagnostic["reason"] == "verify_checkpoint_pending"
                                assert diagnostic["error_type"] == "runtime_lock" and diagnostic["next_action"]
                                assert manager.get_job(jid) == stored
                                if options:
                                    assert json.loads(lock.read_bytes()) == {"job_id": "replacement"}
                                else:
                                    assert not lock.exists()
                        finally:
                            lock.write_bytes(original_claim)
                        try:
                            manager.reserve_update(root, "reservation")
                        except ValueError:
                            pass
                        else:
                            raise AssertionError("package update admitted a pending checkpoint")
                        code, stdout, stderr = run_bounded(attach + ["--result"], 10, cwd=str(root), env=env)
                        assert code == 0 and stdout.strip() == payload["content"].strip(), (code, stdout, stderr)
                    with ThreadPoolExecutor(2) as pool:
                        deliveries = list(pool.map(lambda _: runner("delegate-result", "--task", generation), range(2)))
                    receipts = {r["meta"]["delegation"]["receipt"] for r in deliveries}
                    assert len(receipts) == 1 and spawn.call_count == 1
                    ledger = execution.current_execution(root, session)["finding_history"]
                    assert ledger["revision"] == 2 and "reason_code" not in ledger["rounds"]["old"]["events"][0]
                    for delivery in deliveries:
                        assert delivery["meta"]["verify_followup"] == ledger["rounds"][jid]
                        event, = delivery["meta"]["verify_followup"]["events"]
                        assert (event["id"], event["reason_code"]) == ("F-old", "followup_missing")
                    receipt = receipts.pop()
                    def advance_selection(job_id):
                        assert control("ack", receipt)["ok"] and control("ack", receipt)["ok"]
                        assert control("select", "verify")["meta"]["generation"] != generation
                        return manager.result_snapshot(manager.get_job(job_id))

                    with patch.object(lifecycle, "get_result", side_effect=advance_selection):
                        stale = (control("result", generation) if entry == "result"
                                 else lifecycle._submit_selected(*args))
                    assert not stale["ok"] and "delegation" not in stale["meta"] and spawn.call_count == 1
                    assert not control("result", generation)["ok"]
                    assert jid not in manager.update_job_blockers()
                    assert not active_jobs_for_workspace(root, manager=manager)
                    assert manager.reserve_update(root, "reservation").is_file()


def _check_delegation_collection():
    import subprocess
    from unittest.mock import patch
    from exoma.core.runtime.scripts import _generate_run_scripts
    from exoma.utils.osutil import find_bash
    from exoma.core.jobs import job_lifecycle as jobs
    from exoma._bundle.config.hosts.claude.hooks import workflow_policy as policy

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        isolated_home = root / "runtime-home"
        manager = JobManager(isolated_home / "storage" / "jobs")
        session = "main_a"
        rt = policy.runtime_dir(root, session)

        def control(action, argument):
            return jobs.delegation_control("delegate-" + action, argument, session, str(root))

        with patch.object(main, "JOB_MANAGER", manager), patch.object(main, "_spawn_worker",
                return_value={"ok": True, "meta": {"pid": os.getpid()}}) as launch:
            generation = control("select", "analyze")["meta"]["generation"]
            admitted = jobs._submit_selected("analyze", "task", session, str(root), None, True, generation)
            job_id = admitted["job_id"]
            assert_true(not control("abort", generation)["ok"], "cannot abort an admitted selection")
            assert_true(not control("ack", generation)["ok"], "selection is not a result receipt")
            manager.complete_job(job_id, {"ok": True, "content": "evidence", "meta": {}})
            assert_true(policy.delegation_marker(rt)["phase"] == "running", "terminal publication preserves gate")
            ordinary = jobs.await_job("analyze", "task", session, str(root), generation=generation)
            receipt = ordinary["meta"]["delegation"]["receipt"]
            for command, task, model in (("analyze", "revised", None), ("plan", "task", None),
                                         ("analyze", "task", "different-model")):
                rejected = jobs.await_job(command, task, session, str(root), model, generation=generation)
                assert_true(not rejected["ok"] and "delegation" not in rejected["meta"]
                            and rejected["content"] != ordinary["content"] and launch.call_count == 1,
                            "a rejected dispatch cannot inherit earlier evidence or its receipt")
            _generate_run_scripts(root)
            cli = [find_bash() or "bash", str(root / ".workflow/run.sh"), "analyze", "--session", session,
                   "--request", generation, "--task", "task", "--main-host", "codex"]
            env = {**os.environ, "EXOMA_HOME": str(isolated_home)}
            process = subprocess.run(cli, cwd=root, env=env, capture_output=True, text=True,
                                     encoding="utf-8", timeout=30, **no_console())
            assert_true(process.returncode == 0 and json.loads(process.stdout)["meta"]["delegation"]["receipt"] == receipt,
                        f"generated runner forwards the generation and returns the original receipt: {process.stderr}")
            recovered = control("result", generation)
            assert_true(recovered["content"] == ordinary["content"] and
                        recovered["meta"]["delegation"]["receipt"] == receipt and launch.call_count == 1,
                        "recovery returns the same evidence and receipt without spawning")
            assert_true(not jobs.delegation_control("delegate-ack", receipt, "main_b", str(root))["ok"],
                        "a fork cannot acknowledge another session")
            assert_true(policy.delegation_marker(rt)["phase"] == "returned", "wait for main acknowledgement")
            assert_true(control("ack", receipt)["ok"] and control("ack", receipt)["ok"] and
                        not policy.delegation_marker(rt), "acknowledgement releases exactly once")
            next_gen = control("select", "verify-browser")["meta"]["generation"]
            assert_true(next_gen != generation and not control("ack", receipt)["ok"],
                        "late acknowledgement cannot clear a new generation")
            assert_true(control("abort", next_gen)["ok"], "a prelaunch failure can be reported")
            failed_gen = control("select", "analyze")["meta"]["generation"]
            failed_job = jobs._submit_selected("analyze", "retry", session, str(root), None, True, failed_gen)["job_id"]
            manager.fail_job(failed_job, "fixture provider unavailable")
            failure = control("result", failed_gen)
            assert_true(not failure["ok"] and failure["meta"]["job_id"] == failed_job and
                        policy.delegation_marker(rt)["phase"] == "returned",
                        "terminal failure is returned under its own receipt and remains gated")
            assert_true(control("ack", failure["meta"]["delegation"]["receipt"])["ok"] and
                        not (root / ".workflow/local_mode.flag").exists(), "collecting failure grants no local mode")

            failed_launch = control("select", "analyze")["meta"]["generation"]
            with patch.object(main, "_spawn_worker", return_value={"ok": False, "content": "launch interrupted"}):
                terminal = jobs.await_job("analyze", "same request", session, str(root), generation=failed_launch)
            assert_true(not terminal["ok"] and terminal["content"] == "launch interrupted"
                        and control("ack", terminal["meta"]["delegation"]["receipt"])["ok"],
                        "an admitted same-request launch failure retains its terminal receipt")

            # Fail the session-state write after the job reservation has been created,
            # while a prior job's unused claim and this job's request both exist.
            verify_gen = control("select", "verify")["meta"]["generation"]
            request = jobs.verify_tests.request_path(root, session)
            claimed = jobs.verify_tests.claimed_path(root, session)
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text("current test selection", encoding="utf-8")
            claimed.write_text("previous test selection", encoding="utf-8")
            calls = launch.call_count
            with patch.object(jobs, "atomic_write_json", side_effect=OSError("binding write failed")):
                refused = jobs.await_job("verify", "review", session, str(root), generation=verify_gen)
            assert_true(not refused["ok"] and launch.call_count == calls and not manager.active_job_for_session(session)
                        and request.is_file(), "failed admission releases its reservation and preserves the new request")
            retried = jobs._submit_selected("verify", "review", session, str(root), None, True, verify_gen)
            assert_true(retried["ok"] and launch.call_count == calls + 1 and not request.exists()
                        and claimed.read_text(encoding="utf-8") == "current test selection",
                        "same-generation retry launches with its own test claim")
            manager.fail_job(retried["job_id"], "end of verify fixture")
            assert_true(control("ack", control("result", verify_gen)["meta"]["delegation"]["receipt"])["ok"],
                        "verify fixture collected")

            # Drive an ordinary bound dispatch through the generated runner in a separate
            # process. Attach to a live fixture job so no provider is needed.
            policy.set_delegated_marker(None, rt, {"host": "codex"})
            pending = jobs.submit("analyze", "dispatch first", session, str(root))
            runner = [find_bash() or "bash", str(root / ".workflow/run.sh"), "analyze", "--session",
                      session, "--task", "dispatch first"]
            with subprocess.Popen(runner, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", **no_console()) as child:
                try:
                    deadline = time.monotonic() + 12
                    while policy.delegation_marker(rt).get("job_id") != pending["job_id"]:
                        if child.poll() is not None or time.monotonic() >= deadline:
                            raise AssertionError("runner did not bind its first dispatch while waiting")
                        time.sleep(0.05)
                    assert_true(policy.delegation_marker(rt)["phase"] == "running",
                                "the bound job is gated before evidence exists")
                finally:
                    manager.complete_job(pending["job_id"], {"ok": True, "content": "CLI evidence", "meta": {}})
                stdout, stderr = child.communicate(timeout=30)
            assert_true(child.returncode == 0 and json.loads(stdout)["content"] == "CLI evidence",
                        f"bound runner returns its own evidence: {stderr}")
            # Recover using only information known before dispatch, as if the
            # caller had received none of the runner's stdout.
            selection = control("select", "analyze")["meta"]
            assert_true(selection["phase"] == "returned", "reselect returns the active terminal generation")
            recovered = control("result", selection["generation"])
            cli_receipt = recovered["meta"]["delegation"]["receipt"]
            assert_true(recovered["meta"]["job_id"] == pending["job_id"]
                        and cli_receipt == json.loads(stdout)["meta"]["delegation"]["receipt"],
                        "lost output is recovered from the original job")
            assert_true(policy.delegation_marker(rt)["phase"] == "returned" and control("ack", cli_receipt)["ok"],
                        "runner return requires a subsequent main-host acknowledgement")

            def finish(job_id, work_dir):
                manager.complete_job(job_id, {"ok": True, "content": "fresh evidence", "meta": {}})
                return {"ok": True, "meta": {"pid": os.getpid()}}

            launch.side_effect = finish
            for bound_session in ("main_host", "legacy_session"):
                runtime = policy.runtime_dir(root, bound_session)
                if bound_session == "main_host":
                    policy.set_delegated_marker(None, runtime, {"host": "codex"})
                assert_true(not policy.delegation_marker(runtime), "a natural-language prompt alone is advisory")
                result = jobs.await_job("analyze", "first dispatch", bound_session, str(root))
                assert_true(result["ok"], "dispatch without an explicit selection returns evidence")
                if bound_session == "main_host":
                    proof = result["meta"]["delegation"]
                    assert_true(policy.delegation_marker(runtime)["phase"] == "returned"
                                and jobs.delegation_control("delegate-ack", proof["receipt"], bound_session, str(root))["ok"],
                                "a bound dispatch creates a collection obligation through acknowledgement")
                else:
                    assert_true("delegation" not in result["meta"] and not policy.delegation_marker(runtime),
                                "unbound legacy dispatch preserves its original result format")


def _check_first_dispatch_recovery():
    from unittest.mock import patch
    from exoma._bundle.config.hosts.claude.hooks import workflow_policy as policy
    from exoma.core.jobs import job_lifecycle as jobs

    with tempfile.TemporaryDirectory() as tmp:
        root = str(Path(tmp).resolve())
        manager = JobManager(Path(root) / "jobs")
        session = "interrupted"
        rt = policy.runtime_dir(root, session)
        policy.set_delegated_marker(None, rt, {"host": "codex"})

        def control(action, value):
            return jobs.delegation_control("delegate-" + action, value, session, root)

        write = jobs.atomic_write_json

        def fail_binding(path, state):
            if state.get("phase") == "running":
                raise OSError("fixture: cannot persist admitted job")
            return write(path, state)

        with patch.object(main, "JOB_MANAGER", manager), patch.object(main, "_spawn_worker",
                return_value={"ok": True, "meta": {"pid": os.getpid()}}) as launch:
            with patch.object(jobs, "atomic_write_json", side_effect=fail_binding):
                refused = jobs.await_job("analyze", "unchanged", session, root)
            generation = refused["meta"]["generation"]
            assert_true(not refused["ok"] and launch.call_count == 0 and
                        control("select", "analyze")["meta"]["generation"] == generation,
                        "first dispatch returns its selected generation after a binding refusal")
            assert_true(control("abort", generation)["ok"], "pre-admission request can be disposed")

            policy.set_delegated_marker("analyze", rt, {"host": "codex"})
            unlink = Path.unlink
            def busy_marker(path, *args, **kwargs):
                if path.name == "delegated.marker":
                    raise PermissionError("fixture: marker still open")
                return unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", busy_marker), patch.object(jobs, "REPLACE_RETRY_DELAYS", (0,)):
                busy = jobs.await_job("analyze", "unchanged", session, root)
            selected = control("select", "analyze")["meta"]
            assert_true(not busy["ok"] and selected["phase"] == "selected"
                        and busy["meta"]["generation"] == selected["generation"] and launch.call_count == 0,
                        "a busy armed marker preserves the selected generation without admitting a worker")
            assert_true(control("abort", selected["generation"])["ok"] and not policy.delegation_marker(rt),
                        "recovered selection leaves no stale armed marker after disposal")

            for interrupted_by in ("timeout", "worker_died"):
                if interrupted_by == "worker_died":
                    with patch.object(jobs, "get_result", return_value={"status": "recovering",
                            "meta": {"error_type": "worker_died", "recoverable": True}}):
                        response = jobs.await_job("analyze", "unchanged", session, root)
                else:
                    response = jobs.await_job("analyze", "unchanged", session, root,
                                              poll_interval=0.01, poll_timeout=0.1)
                selection = control("select", "analyze")["meta"]
                assert_true(not response["ok"] and "receipt" not in response["meta"]
                            and response["meta"]["generation"] == selection["generation"]
                            and selection["phase"] == "running", "recover the generation after " + interrupted_by)
                manager.complete_job(response["meta"]["job_id"], {"ok": True, "content": "original", "meta": {}})
                original = jobs.await_job("analyze", "unchanged", session, root, generation=selection["generation"])
                assert_true(original["content"] == "original" and original["meta"]["generation"] == selection["generation"]
                            and control("ack", original["meta"]["delegation"]["receipt"])["ok"],
                            "same-session retry collects the original job after " + interrupted_by)

            actual_result = jobs.get_result
            def finish_before_reattach(job_id):
                manager.complete_job(job_id, {"ok": True, "content": "finished during reattach", "meta": {}})
                delayed_snapshot.side_effect = actual_result
                return {"status": "pending", "meta": {}}

            with patch.object(manager, "set_launcher_pid"), patch.object(jobs, "get_result",
                    side_effect=finish_before_reattach) as delayed_snapshot:
                result = jobs.await_job("analyze", "race", session, root)
            assert_true(result["ok"] and result["content"] == "finished during reattach"
                        and control("ack", result["meta"]["delegation"]["receipt"])["ok"] and launch.call_count == 3,
                        "a terminal job racing pending recovery is collected without another launch")
