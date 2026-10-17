"""O_EXCL lock file that knows who holds it.

The fact store, the knowledge store, and the browser-knowledge store each used to carry a
private copy of the same lock: create the file with O_EXCL, and when it already exists,
steal it once its mtime was older than a TTL or once a deadline passed. Two failures hid
in that shape, and both were POSIX-only because Windows refuses to unlink a file whose
handle is open:

* A live holder that ran past the TTL — a swapped-out process, a slow disk — had its lock
  taken from under it, and two writers then ran the read-modify-write at the same time.
* The holder's exit unlinked the path unconditionally. Once its lock had been stolen, the
  path belonged to the next writer, so the first writer's exit deleted a lock it no longer
  owned and let a third writer in beside the second.

This lock writes `{pid, token}` into the file. A lock is taken over only when its owner
is provably gone: the recorded pid is not running, or the file is unreadable and older
than the TTL (a writer that crashed between create and write). A live owner is waited
for up to the deadline and then reported as `TimeoutError` — waiting longer would hang
the caller, and taking the lock would bring back the concurrent write this exists to
stop. Release removes the file only while it still carries this holder's token.
"""

import json
import os
import time
import uuid
from pathlib import Path

from utils import osutil


def _try_os_lock(path: Path):
    """A non-blocking exclusive OS lock on byte 0 of `path`, or None when someone holds it.

    Per open handle on both platforms (LockFileEx on Windows, flock on POSIX), so two
    threads of one process exclude each other as two processes do.
    """
    try:
        handle = osutil.open_lock_file(path)
    except OSError:
        return None
    try:
        osutil.lock_file(handle)
        return handle
    except OSError:
        handle.close()
        return None


def _release_os_lock(handle) -> None:
    try:
        osutil.unlock_file(handle)
    except OSError:
        pass
    finally:
        handle.close()


class OwnedFileLock:
    def __init__(self, path: Path, ttl_seconds: float = 30.0, wait_seconds: float | None = None):
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self.wait_seconds = ttl_seconds if wait_seconds is None else wait_seconds
        self.token = uuid.uuid4().hex
        self.fd: int | None = None

    def __enter__(self) -> "OwnedFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.wait_seconds
        payload = json.dumps(
            {"pid": os.getpid(), "token": self.token, "at": time.time()}
        ).encode("utf-8")
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except PermissionError:
                # Windows: the previous holder's file is still delete-pending (a reader had
                # it open when it was unlinked), and creating over it is refused as access
                # denied rather than "exists". It is gone in moments; wait like for a holder.
                # On POSIX this is a real permissions problem and is raised.
                if not osutil.IS_WINDOWS or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
                continue
            except FileExistsError:
                # A reclaim that happened is retried at once; one that could not run (the
                # guard busy, the owner judged alive after all) counts against the wait
                # like any live holder, so a stuck reclaimer cannot hang this writer.
                if self._owner_is_gone() and self._remove_abandoned():
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"lock {self.path.name} is still held by a running process"
                    )
                time.sleep(0.05)
                continue
            try:
                os.write(self.fd, payload)
            except OSError:
                self._close()
                self._unlink()
                raise
            return self

    def __exit__(self, *exc) -> None:
        # Read before closing: once closed, a writer that judged us gone could replace
        # the file, and the comparison must see the file this holder created.
        owned = self._read_owner().get("token") == self.token
        self._close()
        if not owned:
            return
        # Retried: on Windows the unlink fails while a waiter has the file open to read its
        # owner, and a lock left behind by a holder in a still-running process is one no
        # waiter may take over — it would stand until every waiter timed out.
        deadline = time.monotonic() + 5.0
        while True:
            if self._unlink():
                return
            if self._read_owner().get("token") not in (self.token, None):
                return  # no longer ours (or gone and re-taken); nothing to remove
            if not self.path.exists() or time.monotonic() >= deadline:
                return
            time.sleep(0.01)

    def _owner_is_gone(self) -> bool:
        owner = self._read_owner()
        pid = owner.get("pid")
        if isinstance(pid, int) and pid > 0:
            if pid == os.getpid() and owner.get("token") != self.token:
                # Our own process, another holder object: alive by definition.
                return False
            return not osutil.process_alive(pid)
        # No readable owner: either a writer between create and write (young file) or a
        # crash in that window (old file). Only the age can tell them apart.
        try:
            return time.time() - self.path.stat().st_mtime > self.ttl_seconds
        except OSError:
            return True  # vanished between EEXIST and stat — retry the create

    def _read_owner(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _remove_abandoned(self) -> bool:
        """Remove a dead owner's lock — only while no other reclaimer can be doing the same.

        Two writers can both see the same dead pid. Unlinking straight away let the slower
        one delete the lock the faster one had just created in its place, and both then
        ran the read-modify-write. Reclaim is therefore serialised through a guard, and the
        owner is judged again inside it: by then the faster writer's live token is what the
        file holds, and it is left alone. Returns whether a dead owner's file was removed.
        """
        # The guard is an OS advisory lock on a file that is never deleted, not another
        # O_EXCL file: the kernel releases it when its holder dies and never hands it to
        # anyone while the holder lives, however long that holder is paused. An O_EXCL
        # guard would need an age rule to recover from a crash, and an age rule is exactly
        # what let a paused reclaimer lose its guard and then delete a live lock.
        guard = _try_os_lock(self.path.with_name(f"{self.path.name}.reclaim"))
        if guard is None:
            return False  # another reclaimer is inside; judge again next round
        try:
            # Judged again from scratch. A file that vanished meanwhile is NOT abandoned
            # here: another writer may be creating its replacement, and unlinking "nothing"
            # a moment late would delete that. A dead owner's file cannot change under the
            # guard — its owner is gone and every other remover needs this guard.
            owner = self._read_owner()
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            pid = owner.get("pid")
            if isinstance(pid, int) and pid > 0:
                gone = not (pid == os.getpid() and owner.get("token") != self.token) and not osutil.process_alive(pid)
            else:
                gone = age > self.ttl_seconds
            return gone and self._unlink()
        finally:
            _release_os_lock(guard)

    def _unlink(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False  # (Windows) still open — by a live holder, or a waiter reading it

    def _close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
