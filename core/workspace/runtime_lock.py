"""Cross-process runtime lock: acquisition, liveness, and release."""

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.workspace.workspace_paths import (
    JSON_INDENT,
    LOCK_TTL_SECONDS,
    now_iso,
    read_json_file,
)
from utils import osutil

_RUNTIME_TRANSITION_THREAD_LOCK = threading.Lock()


class _RuntimeTransitionGuard:
    """Serialize runtime-lock creation and removal across threads and processes."""

    def __init__(self, lock_path: Path):
        self.path = lock_path.with_name(f"{lock_path.name}.guard")
        self.handle = None

    def __enter__(self):
        _RUNTIME_TRANSITION_THREAD_LOCK.acquire()
        try:
            while True:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.handle = osutil.open_lock_file(self.path)
                osutil.lock_file(self.handle, blocking=True)
                # `clean` may have unlinked the guard while this process waited on it
                # (DEC-013). The lock is then on a file no one else will open: start over
                # on whatever the path names now. Windows refuses to unlink an open file,
                # so there the path never changes under a holder.
                if osutil.IS_WINDOWS or _same_file(self.handle, self.path):
                    return self
                self.handle.close()
                self.handle = None
        except Exception:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            _RUNTIME_TRANSITION_THREAD_LOCK.release()
            raise

    def __exit__(self, *exc) -> None:
        try:
            if self.handle is not None:
                osutil.unlock_file(self.handle)
        finally:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            _RUNTIME_TRANSITION_THREAD_LOCK.release()


def _same_file(handle, path: Path) -> bool:
    try:
        named = os.stat(path)
    except FileNotFoundError:
        return False
    held = os.fstat(handle.fileno())
    return (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino)


def remove_orphan_guard(lock_path: Path) -> str:
    """Remove a session's transition guard when no process can be using it (DEC-013).

    Returns `removed`, `kept` (taken by someone, or the session's runtime lock is live) or
    `absent`. The guard is locked without waiting, so a holder keeps it. On POSIX it is
    unlinked while held; a process that was waiting on it sees the path changed and opens
    the new file (`_RuntimeTransitionGuard`). Windows refuses to delete a file another
    process has open, so the guard is unlinked after release and a refusal keeps it.
    """
    guard = lock_path.with_name(f"{lock_path.name}.guard")
    with _RUNTIME_TRANSITION_THREAD_LOCK:
        try:
            handle = guard.open("r+b")
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "kept"
        try:
            try:
                osutil.lock_file(handle)
            except OSError:
                return "kept"
            if osutil.IS_WINDOWS:
                try:
                    if _runtime_lock_is_live(lock_path):
                        return "kept"
                finally:
                    osutil.unlock_file(handle)
            else:
                if not _same_file(handle, guard) or _runtime_lock_is_live(lock_path):
                    return "kept"
                try:
                    guard.unlink()
                except FileNotFoundError:
                    return "absent"
                except OSError:
                    return "kept"
                return "removed"
        finally:
            handle.close()
        try:
            guard.unlink()
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "kept"
        return "removed"


def _runtime_lock_is_live(lock_path: Path) -> bool:
    if not lock_path.exists():
        return False
    return _runtime_lock_is_active(lock_path, _runtime_lock_payload(lock_path))


def prune_runtime_guards(sessions_dir: Path) -> dict:
    """`clean`: drop the transition guards of sessions with no live runtime lock."""
    counts = {"removed": 0, "kept": 0}
    if not sessions_dir.is_dir():
        return counts
    for guard in sorted(sessions_dir.glob("*/runtime/lock.guard")):
        outcome = remove_orphan_guard(guard.with_name("lock"))
        if outcome in counts:
            counts[outcome] += 1
    return counts


def _runtime_lock_payload(lock_path: Path) -> dict | None:
    try:
        payload = read_json_file(lock_path)
    except (ValueError, FileNotFoundError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _runtime_lock_age_seconds(lock_path: Path, payload: dict | None) -> float | None:
    created = None
    if payload:
        try:
            created = datetime.fromisoformat(str(payload.get("created_at") or ""))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            created = None
    if created is not None:
        return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
    try:
        modified = datetime.fromtimestamp(lock_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - modified).total_seconds())


def _runtime_lock_is_active(lock_path: Path, payload: dict | None) -> bool:
    if payload:
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            expected_identity = payload.get("process_identity") or None
            expected_create_time = payload.get("pid_create_time")
            generation = osutil.process_generation(
                pid, expected_create_time, expected_identity
            )
            if generation == osutil.PROCESS_DEAD:
                return False
            # Keeping a lock is the safe side here, the opposite of a kill: an identity
            # that cannot be read keeps a create-time lock, never an identity lock.
            if expected_identity:
                return generation == osutil.PROCESS_MATCHED
            if expected_create_time is not None:
                return generation != osutil.PROCESS_REUSED
            age = _runtime_lock_age_seconds(lock_path, payload)
            return age is None or age <= LOCK_TTL_SECONDS
    age = _runtime_lock_age_seconds(lock_path, payload)
    return age is None or age <= LOCK_TTL_SECONDS


def runtime_lock_owned(lock_path: Path, session_id: str, lock_token: str) -> bool:
    """True only while the exact runtime-lock generation is still installed."""
    with _RuntimeTransitionGuard(lock_path):
        payload = _runtime_lock_payload(lock_path)
        return bool(
            payload
            and payload.get("session_id") == session_id
            and payload.get("token") == lock_token
        )


def acquire_runtime_lock(lock_path: Path, command: str, session_id: str) -> dict:
    stale_payload = None
    stale_replaced = False
    with _RuntimeTransitionGuard(lock_path):
        if lock_path.exists():
            payload = _runtime_lock_payload(lock_path)
            if _runtime_lock_is_active(lock_path, payload):
                return {
                    "ok": False,
                    "stale": False,
                    "payload": payload or {"invalid": True},
                }
            stale_payload = payload or {"invalid": True}
            try:
                lock_path.unlink()
                stale_replaced = True
            except FileNotFoundError:
                pass

        token = secrets.token_hex(16)
        payload = {
            "command": command,
            "session_id": session_id,
            "token": token,
            "pid": os.getpid(),
            "pid_create_time": osutil.pid_create_time(os.getpid()),
            "process_identity": osutil.process_identity(os.getpid()),
            "created_at": now_iso(),
        }
        encoded = json.dumps(payload, indent=JSON_INDENT).encode("utf-8")
        fd = None
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "wb") as handle:
                fd = None
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            current = _runtime_lock_payload(lock_path)
            return {
                "ok": False,
                "stale": False,
                "payload": current or {"invalid": True},
            }
        except Exception:
            if fd is not None:
                os.close(fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            raise

    return {
        "ok": True,
        "stale": stale_replaced,
        "payload": stale_payload,
        "token": token,
    }


def release_runtime_lock(
    lock_path: Path,
    session_id: str | None = None,
    lock_token: str | None = None,
) -> None:
    """Release only the exact runtime-lock generation owned by this call."""
    with _RuntimeTransitionGuard(lock_path):
        if not lock_path.exists():
            return
        if session_id is not None or lock_token is not None:
            payload = _runtime_lock_payload(lock_path)
            if not isinstance(payload, dict):
                return
            if session_id is not None and payload.get("session_id") != session_id:
                return
            if lock_token is not None and payload.get("token") != lock_token:
                return
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


