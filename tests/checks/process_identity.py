"""Process identity, the fail-closed worker kill, and the byte-0 lock all live in osutil."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from exoma.core.jobs.job_manager import JobManager
from exoma.utils import osutil

from tests.checks.support import assert_true

_HOLD_LOCK = """
import sys
from pathlib import Path
from exoma.utils import osutil
handle = osutil.open_lock_file(Path(sys.argv[1]))
osutil.lock_file(handle)
print("held", flush=True)
sys.stdin.readline()
"""


def _sleeper() -> subprocess.Popen:
    # Own process group, so a killpg that reaches it cannot reach this test runner.
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        **osutil.hidden_run_kwargs(),
    )


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"], **osutil.hidden_run_kwargs())
    proc.wait(timeout=30)
    return proc.pid


def _test_process_generation_outcomes() -> None:
    me = os.getpid()
    assert_true(
        osutil.process_generation(_dead_pid(), None, "anything") == osutil.PROCESS_DEAD,
        "an exited PID is dead, whatever was recorded for it",
    )
    assert_true(
        osutil.process_generation(me) == osutil.PROCESS_UNVERIFIABLE,
        "a live PID with nothing recorded is unverifiable, never matched",
    )
    # A host that exposes no process generation has only the unverifiable outcome to
    # check; the matched/reused branches run where an identity is readable.
    identity = osutil.process_identity(me)
    if identity is not None:
        assert_true(
            osutil.process_generation(me, None, "not-this-process") == osutil.PROCESS_REUSED,
            "a live PID whose identity differs from the recorded one is reused",
        )
        assert_true(
            osutil.process_identity(me) == identity,
            "the identity of one running process is stable between reads",
        )
        assert_true(
            osutil.process_generation(me, None, identity) == osutil.PROCESS_MATCHED,
            "the recorded identity of a running process matches it",
        )
    create_time = osutil.pid_create_time(me)
    if create_time is not None:
        assert_true(
            osutil.process_generation(me, create_time + 1, None) == osutil.PROCESS_REUSED,
            "a create time that differs proves reuse when no identity was recorded",
        )

    original = osutil.process_identity
    osutil.process_identity = lambda pid: None
    try:
        assert_true(
            osutil.process_generation(me, None, "recorded") == osutil.PROCESS_UNVERIFIABLE,
            "an identity that cannot be read now is unverifiable, not a match and not reuse",
        )
    finally:
        osutil.process_identity = original


def _test_worker_kill_is_fail_closed() -> None:
    root = Path(tempfile.mkdtemp(prefix="wf-kill-"))
    proc = _sleeper()
    try:
        if osutil.IS_WINDOWS:
            assert_true(any(child["pid"] == proc.pid for child in osutil.process_tree(os.getpid())),
                        "the native Windows process snapshot sees a spawned child")
        manager = JobManager(root / "jobs")
        job = {"worker_pid": proc.pid, "worker_create_time": None, "worker_identity": None}
        result = manager._kill_worker(job)
        assert_true(
            result.get("reason") == "identity_unverifiable" and proc.poll() is None,
            f"a worker with no recorded identity is not killed: {result}",
        )

        reused = dict(job, worker_identity="a-different-process")
        if osutil.process_identity(proc.pid) is not None:
            result = manager._kill_worker(reused)
            assert_true(
                result.get("reason") == "pid_reuse_detected" and proc.poll() is None,
                f"a worker whose PID now hosts another process is not killed: {result}",
            )

        original = osutil.process_identity
        osutil.process_identity = lambda pid: None
        try:
            result = manager._kill_worker(dict(job, worker_identity="recorded"))
        finally:
            osutil.process_identity = original
        assert_true(
            result.get("reason") == "identity_unverifiable" and proc.poll() is None,
            f"a recorded identity that cannot be read now does not license a kill: {result}",
        )

        assert_true(
            manager._kill_worker({"worker_pid": os.getpid()}).get("reason") == "self_pid",
            "the runtime never kills its own PID",
        )
        assert_true(
            manager._kill_worker({"worker_pid": _dead_pid(), "worker_identity": "x"}).get("reason")
            == "worker_dead",
            "a dead worker has nothing left to kill",
        )

        identity = osutil.process_identity(proc.pid)
        if identity is not None:
            # As set_worker_pid records them: both, so terminate_tree can recheck on Windows.
            matched = dict(job, worker_identity=identity,
                           worker_create_time=osutil.pid_create_time(proc.pid))
            result = manager._kill_worker(matched)
            deadline = time.monotonic() + 20
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert_true(
                result.get("method") != "skipped" and proc.poll() is not None,
                f"a worker whose identity matches is killed: {result}",
            )
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=30)
        shutil.rmtree(root, ignore_errors=True)

    if osutil.IS_WINDOWS:
        denied = _sleeper()
        try:
            with patch.object(osutil.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["taskkill"], 1, stderr=b"ERROR: Access denied")):
                result = osutil.terminate_tree(denied)
            assert_true(result == {"method": "winapi", "ok": True} and denied.poll() is not None,
                        f"a denied taskkill falls back to a process handle: {result}")
        finally:
            if denied.poll() is None:
                denied.kill()
            denied.wait(timeout=30)

        recycled = _sleeper()
        try:
            created = osutil.pid_create_time(recycled.pid)
            assert_true(created is not None, "a live sleeper has a readable creation time")
            assert_true(osutil._windows_terminate_pid(recycled.pid, created + 1)
                        and recycled.poll() is None,
                        "a PID whose creation time changed is treated as gone and left running")
            assert_true(osutil._windows_terminate_pid(recycled.pid, created),
                        "a PID whose creation time matches is terminated")
            recycled.wait(timeout=30)
        finally:
            if recycled.poll() is None:
                recycled.kill()
            recycled.wait(timeout=30)

        # By PID only (given the Popen, terminate_tree also reaps it with proc.kill()): the
        # caller's recorded creation time is required and is checked before taskkill runs.
        byid = _sleeper()
        try:
            created = osutil.pid_create_time(byid.pid)
            with patch.object(osutil.subprocess, "run", side_effect=AssertionError("taskkill ran")):
                unrecorded = osutil.terminate_tree(None, byid.pid)
                reused = osutil.terminate_tree(None, byid.pid, expected_create_time=created + 1)
            assert_true(unrecorded == {"method": "skipped", "ok": False, "reason": "identity_unverifiable"}
                        and byid.poll() is None,
                        f"a PID-only kill without a recorded creation time touches nothing: {unrecorded}")
            assert_true(reused == {"method": "skipped", "ok": False, "reason": "pid_reuse_detected"}
                        and byid.poll() is None,
                        f"a PID now hosting another process is not killed, not even by taskkill: {reused}")
            with patch.object(osutil.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["taskkill"], 1, stderr=b"ERROR: Access denied")):
                result = osutil.terminate_tree(None, byid.pid, expected_create_time=created)
            byid.wait(timeout=30)
            assert_true(result == {"method": "winapi", "ok": True},
                        f"a PID-only kill with a matching creation time ends it through its pinned handle: {result}")
            # Not _dead_pid(): Windows may hand a freed PID straight to another process.
            # This one sits far above any PID Windows assigns, so it never names a process.
            assert_true(osutil.terminate_tree(None, 0xFFFFFFFC, expected_create_time=created)
                        == {"method": "skipped", "ok": True, "reason": "gone"},
                        "a PID that hosts no process has nothing left to kill")
        finally:
            if byid.poll() is None:
                byid.kill()
            byid.wait(timeout=30)

        parent_code = (
            "import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(child.pid, flush=True); time.sleep(60)"
        )
        parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE,
                                  text=True, **osutil.hidden_run_kwargs())
        child_pid = None
        try:
            child_pid = int(parent.stdout.readline().strip())
            assert_true(any(row["pid"] == child_pid for row in osutil.process_tree(parent.pid)),
                        "the native snapshot finds a worker's descendant")
            with patch.object(osutil.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["taskkill"], 1, stderr=b"ERROR: Access denied")):
                result = osutil.terminate_tree(parent)
            assert_true(result == {"method": "winapi", "ok": True}
                        and parent.poll() is not None and not osutil.process_alive(child_pid),
                        f"the fallback terminates the worker and its descendant: {result}")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=30)
            if child_pid and osutil.process_alive(child_pid):
                osutil._windows_terminate_pid(child_pid)

        # A live descendant whose identity cannot be read is left alone and reported.
        parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE,
                                  text=True, **osutil.hidden_run_kwargs())
        child_pid = None
        try:
            child_pid = int(parent.stdout.readline().strip())
            real_create_time = osutil.pid_create_time
            with patch.object(osutil.subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["taskkill"], 1, stderr=b"ERROR: Access denied")), \
                    patch.object(osutil, "pid_create_time",
                                 side_effect=lambda pid: None if pid == child_pid else real_create_time(pid)):
                result = osutil.terminate_tree(parent)
            assert_true(result == {"method": "winapi", "ok": False}
                        and parent.poll() is not None and osutil.process_alive(child_pid),
                        f"an unidentifiable live descendant makes the kill incomplete: {result}")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=30)
            if child_pid and osutil.process_alive(child_pid):
                osutil._windows_terminate_pid(child_pid)


def _test_byte_lock_excludes_handles_and_processes() -> None:
    root = Path(tempfile.mkdtemp(prefix="wf-bytelock-"))
    path = root / "guard"
    holder = None
    try:
        first = osutil.open_lock_file(path)
        assert_true(path.stat().st_size == 1, "an empty lock file is given the byte it locks")
        osutil.lock_file(first)
        second = osutil.open_lock_file(path)
        try:
            refused = False
            try:
                osutil.lock_file(second)
            except OSError:
                refused = True
            assert_true(refused, "a second handle cannot take a held lock")
            osutil.unlock_file(first)
            osutil.lock_file(second)
            osutil.unlock_file(second)
        finally:
            second.close()
            first.close()
        assert_true(path.stat().st_size == 1, "reopening a lock file does not grow it")

        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLD_LOCK, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=env,
            **osutil.hidden_run_kwargs(),
        )
        assert_true(holder.stdout.readline().strip() == "held", "the holder took the lock")
        probe = osutil.open_lock_file(path)
        try:
            refused = False
            try:
                osutil.lock_file(probe)
            except OSError:
                refused = True
            assert_true(refused, "another process's lock excludes this one")
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=30)
            osutil.lock_file(probe)  # the kernel freed it when the holder exited
            osutil.unlock_file(probe)
        finally:
            probe.close()
    finally:
        if holder is not None and holder.poll() is None:
            holder.kill()
            holder.wait(timeout=30)
        shutil.rmtree(root, ignore_errors=True)
