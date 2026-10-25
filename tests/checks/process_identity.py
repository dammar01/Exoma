"""Process identity, the fail-closed worker kill, and the byte-0 lock all live in osutil."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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
            matched = dict(job, worker_identity=identity)
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
