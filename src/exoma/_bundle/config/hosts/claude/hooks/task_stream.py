"""Task telemetry file access shared by the standalone hook and the runtime."""

import os
import stat
from pathlib import Path


def task_stream_identity(path: Path):
    """Refuse a redirected or shared legacy stream before selecting/migrating its layout."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise OSError(f"Cannot use {path}: expected an unlinked regular task stream. Reconcile the entry before upgrade.")
    return info


def append_task_rows(path: Path, rows: bytes) -> None:
    """Check the opened file before reading its tail or appending the entire batch."""
    for attempt in range(2):
        identity = task_stream_identity(path)
        flags = os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        if identity is None:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            try:
                fd = os.open(path, flags | os.O_RDWR, 0o600)
                mode = "a+b"
            except PermissionError:
                fd = os.open(path, flags | os.O_WRONLY, 0o600)
                mode = "ab"
            break
        except FileExistsError:
            if attempt:
                raise
    with os.fdopen(fd, mode) as stream:
        opened = os.fstat(fd)
        current = task_stream_identity(path)
        if (current is None or not os.path.samestat(opened, current)
                or (identity is not None and not os.path.samestat(opened, identity))
                or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
            raise OSError(f"Task stream changed during open: {path}")
        if mode == "a+b" and stream.seek(0, os.SEEK_END):
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                rows = b"\n" + rows
        stream.write(rows)
