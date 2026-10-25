"""`clean` removes the runtime-lock guards of dead sessions and keeps every other (DEC-013)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from exoma.core.workspace.runtime_lock import (
    _RuntimeTransitionGuard,
    acquire_runtime_lock,
    prune_runtime_guards,
    release_runtime_lock,
    remove_orphan_guard,
)
from exoma.core.workspace.workspace_paths import workflow_paths

from tests.checks.support import assert_true

_HOLD_GUARD = """
import sys, time
from pathlib import Path
from exoma.core.workspace.runtime_lock import _RuntimeTransitionGuard
with _RuntimeTransitionGuard(Path(sys.argv[1])):
    print("held", flush=True)
    sys.stdin.readline()
"""


def _guarded_session(root: Path, session_id: str) -> Path:
    lock = workflow_paths(root, session_id)["lock"]
    with _RuntimeTransitionGuard(lock):
        pass
    return lock


def _test_runtime_guard_cleanup() -> None:
    repo = Path(__file__).resolve().parents[2]
    root = Path(tempfile.mkdtemp(prefix="runtime-guards-"))
    sessions = workflow_paths(root)["sessions_dir"]
    try:
        dead = _guarded_session(root, "dead")
        guard = dead.with_name("lock.guard")
        assert_true(guard.exists(), "taking the transition guard leaves its one-byte file behind")

        # A runtime lock whose owner is gone does not keep its guard.
        stale = _guarded_session(root, "stale")
        stale.write_text(json.dumps({"pid": 2_000_000_000, "created_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")

        # A live runtime lock keeps its guard.
        live = workflow_paths(root, "live")["lock"]
        live.parent.mkdir(parents=True, exist_ok=True)
        taken = acquire_runtime_lock(live, "verify", "live")
        assert_true(taken["ok"], f"the live session took its runtime lock: {taken}")

        counts = prune_runtime_guards(sessions)
        assert_true(
            counts == {"removed": 2, "kept": 1},
            f"dead and stale guards go, the live one stays: {counts}",
        )
        assert_true(not guard.exists() and live.with_name("lock.guard").exists(), "the right files went")
        assert_true(
            prune_runtime_guards(sessions) == {"removed": 0, "kept": 1},
            "a second clean removes nothing more",
        )
        assert_true(
            remove_orphan_guard(dead) == "absent" and not guard.exists(),
            "a missing guard is reported absent, never created",
        )
        release_runtime_lock(live, "live", taken["token"])

        # A guard another process holds is kept, whatever the runtime lock says.
        held = _guarded_session(root, "held")
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLD_GUARD, str(held)],
            cwd=repo,
            env={**os.environ, "PYTHONPATH": str(repo / "src")},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert_true(holder.stdout.readline().strip() == "held", "the holder process took the guard")
            outcome = remove_orphan_guard(held)
            assert_true(
                outcome == "kept" and held.with_name("lock.guard").exists(),
                f"a guard held by another process is kept: {outcome}",
            )
        finally:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=10)

        # The guard still works after a clean removed it: the next use recreates it.
        with _RuntimeTransitionGuard(dead):
            assert_true(guard.exists(), "a removed guard is recreated on the next use")
    finally:
        shutil.rmtree(root, ignore_errors=True)
