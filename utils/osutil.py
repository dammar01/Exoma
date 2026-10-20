"""Cross-OS primitives. All platform-specific branches live here — nowhere else.

Windows + macOS + Linux. Stdlib only (no psutil).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# The oldest Python the code runs on (`X | None` in signatures is evaluated at def time).
# main.py, install.py and check.py repeat it inline in their version guard, because a guard
# that imported this module would fail on the very interpreter it is meant to name;
# tests/checks/os_resolvers.py keeps the copies equal.
MIN_PYTHON = (3, 10)

# PATH names tried for an interpreter, in order. `python3` first: on Linux and macOS it is
# often the only one; `python` is the usual Windows name; `py -3` is the Windows launcher.
_PYTHON_CANDIDATES: tuple[tuple[str, ...], ...] = (("python3",), ("python",), ("py", "-3"))
_VERSION_PROBE = "import sys; print('%d.%d' % sys.version_info[:2])"


# Where install.py records the interpreter it ran under (the one it installed the
# requirements into), so the hook launchers and the generated .workflow scripts run the
# same Python instead of each picking its own from PATH (DEC-068).
PINNED_PYTHON_FILE = "python.path"


def pinned_python_path(home: Path | None = None) -> Path:
    """~/.claude/hooks/python.path, beside the launchers that read it."""
    return Path(home or os.path.expanduser("~")) / ".claude" / "hooks" / PINNED_PYTHON_FILE


def pinned_python(home: Path | None = None) -> str | None:
    """The interpreter install.py pinned, while that file still exists; else None."""
    try:
        value = pinned_python_path(home).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if value and os.path.isfile(value) else None


def python_exe() -> str:
    """Resolve the python interpreter for generated scripts: the pinned one, else this one."""
    return pinned_python() or sys.executable or ("python" if IS_WINDOWS else "python3")


def _probe_python(argv: list[str]) -> tuple[int, int] | None:
    """The version an interpreter reports by RUNNING, or None. A name on PATH proves
    nothing: Windows ships `python3`/`python` aliases under WindowsApps that open the
    Store instead of running, and an old python3 can shadow a newer python."""
    try:
        # 5 s each, three candidates: a hung interpreter still leaves the caller inside the
        # shortest hook timeout (10 s) for the first two.
        done = subprocess.run(
            [*argv, "-c", _VERSION_PROBE], capture_output=True, text=True, timeout=5,
            **hidden_run_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        major, minor = done.stdout.strip().split(".")
        return int(major), int(minor)
    except ValueError:
        return None


def find_python(min_version: tuple[int, int] = MIN_PYTHON) -> list[str] | None:
    """argv of the first interpreter on PATH that runs and is at least `min_version`, or
    None. Tried as `python3`, `python`, `py -3`, each by running it."""
    for candidate in _PYTHON_CANDIDATES:
        exe = shutil.which(candidate[0])
        if not exe:
            continue
        argv = [exe, *candidate[1:]]
        version = _probe_python(argv)
        if version is not None and version >= min_version:
            return argv
    return None


def find_bash() -> str | None:
    """The bash that runs the hook and runner scripts, or None.

    POSIX: `bash` on PATH. Windows: Git Bash, found next to `git` — PATH there can also
    resolve `bash` to WSL's System32\\bash.exe or a WindowsApps alias, and neither runs
    these scripts against Windows paths. git.exe lives in `<Git>\\cmd`, or in
    `<Git>\\mingw64\\bin` when PATH comes from Git Bash itself, so the parents are walked.
    """
    if not IS_WINDOWS:
        return shutil.which("bash")
    git = shutil.which("git")
    if not git:
        return None
    for root in Path(git).parents:
        for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


def user_cache_dir() -> Path | None:
    """The per-user cache root: %LOCALAPPDATA% (else %APPDATA%) on Windows,
    $XDG_CACHE_HOME (else ~/.cache) elsewhere. None when Windows names neither."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) if base else None
    xdg = os.environ.get("XDG_CACHE_HOME")
    return Path(xdg) if xdg else Path.home() / ".cache"


def has_display() -> bool:
    """Whether a headed window has a screen to open on. Only Linux can lack one in the
    normal case (CI, a container, SSH without X11 or Wayland); Windows and macOS always
    have a session."""
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def split_command(command: str) -> list[str]:
    """Tokens of a command line by this OS's rules: POSIX quoting elsewhere; on Windows
    backslashes are kept (non-POSIX shlex), so a path like C:\\x\\y.exe survives. Raises
    ValueError on unbalanced quotes, as shlex does."""
    import shlex

    return shlex.split(command, posix=not IS_WINDOWS)


def user_env_location(home: Path) -> tuple[str, str]:
    """Where a persistent per-user environment variable is written: ("registry",
    "HKCU\\Environment") on Windows, ("profile", <~/.zshrc or ~/.bashrc>) elsewhere."""
    if IS_WINDOWS:
        return "registry", r"HKCU\Environment"
    shell = os.environ.get("SHELL", "")
    return "profile", str(home / (".zshrc" if shell.endswith("zsh") else ".bashrc"))


def set_registry_user_env(name: str, value: str) -> str | None:
    """Write a user environment variable to HKCU\\Environment and broadcast the change.
    Windows only. Returns None, or why the broadcast was skipped (the value is written
    either way; without the broadcast it is live only after the next logon)."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    try:
        import ctypes

        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
    except Exception as exc:  # pragma: no cover - cosmetic refresh only
        return str(exc)
    return None


def resolve_exe(name: str) -> str:
    """Resolve an executable, honoring the Windows `.cmd` shim."""
    if not IS_WINDOWS or os.path.splitext(name)[1]:
        return shutil.which(name) or name
    return shutil.which(f"{name}.cmd") or shutil.which(name) or name


def provider_callable(command_name: str) -> tuple[bool, str]:
    """Is a provider CLI on PATH? Returns (ok, resolved path or reason).

    Presence check ONLY — never invoked. `<cli> --help` can resolve to a native .exe shim
    that false-negatives ("not compatible with this Windows version") while the `.cmd` the
    workflow actually spawns works fine.

    Lives here, not in a provider's install module. The check is `shutil.which` and nothing
    else — there is nothing opencode-shaped about it — but importing it from
    `adapters.install.opencode_install` meant core/ reached into one provider's module to test
    every provider's binary.
    """
    resolved = shutil.which(f"{command_name}.cmd") or shutil.which(command_name)
    if resolved:
        return True, resolved
    return False, f"{command_name} not found in PATH"


def make_executable(path: Path) -> None:
    """chmod +x on POSIX; no-op on Windows."""
    if IS_WINDOWS:
        return
    try:
        mode = os.stat(path).st_mode
        os.chmod(path, mode | 0o111)
    except OSError:
        pass


def detached_popen_kwargs() -> dict:
    """Kwargs to spawn a fully detached background worker, per OS."""
    if IS_WINDOWS:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
        return {
            "startupinfo": startupinfo,
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            ),
        }
    # POSIX: new session detaches from the controlling terminal/parent group.
    return {"start_new_session": True}


def hidden_run_kwargs() -> dict:
    """Kwargs to run a foreground subprocess without flashing a console (Windows).

    On Windows the child is also placed in its own process group so the whole tree
    can be terminated later — `opencode` is a `.cmd` shim that spawns node, and
    killing only the shim leaves the node process orphaned (RAM leak per timeout).
    """
    if IS_WINDOWS:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return {
            "startupinfo": startupinfo,
            "creationflags": subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    # POSIX: own process group so killpg reaches grandchildren too.
    return {"start_new_session": True}


def process_tree(pid: int | None) -> list[dict]:
    """Descendants of `pid` as [{'pid': int, 'name': str}]. Diagnostics only.

    Windows: CIM query. POSIX: `ps`. Returns [] when the platform tool is missing
    or errors — callers treat an empty list as "unknown", never as "no children".
    """
    if not pid or pid <= 0:
        return []
    try:
        if IS_WINDOWS:
            script = (
                "$ErrorActionPreference='SilentlyContinue';"
                "Get-CimInstance Win32_Process |"
                " Select-Object ProcessId,ParentProcessId,Name |"
                " ConvertTo-Json -Compress"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                **hidden_run_kwargs(),
            ).stdout
            import json as _json

            rows = _json.loads(out or "[]")
            if isinstance(rows, dict):
                rows = [rows]
            children: dict[int, list[int]] = {}
            names: dict[int, str] = {}
            for row in rows:
                child = int(row.get("ProcessId") or 0)
                parent = int(row.get("ParentProcessId") or 0)
                if not child:
                    continue
                names[child] = str(row.get("Name") or "")
                children.setdefault(parent, []).append(child)
        else:
            out = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,comm="],
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout
            children = {}
            names = {}
            for line in (out or "").splitlines():
                parts = line.split(None, 2)
                if len(parts) < 2:
                    continue
                child, parent = int(parts[0]), int(parts[1])
                names[child] = parts[2] if len(parts) > 2 else ""
                children.setdefault(parent, []).append(child)
    except Exception:
        return []

    found: list[dict] = []
    stack = [pid]
    seen = {pid}
    while stack:
        for child in children.get(stack.pop(), []):
            if child in seen:
                continue
            seen.add(child)
            found.append({"pid": child, "name": names.get(child, "")})
            stack.append(child)
    return found


def terminate_tree(proc: "subprocess.Popen | None", pid: int | None = None) -> dict:
    """Kill a process AND every descendant. Returns {'method', 'ok'}.

    Plain `proc.kill()` is not enough: on Windows it terminates the `.cmd` shim
    while node keeps running; on POSIX it misses grandchildren. Best-effort —
    a failure here must never mask the original error the caller is handling.
    """
    target = pid if pid is not None else (proc.pid if proc else None)
    if not target:
        return {"method": "none", "ok": False}

    method, ok = "none", False
    try:
        if IS_WINDOWS:
            method = "taskkill"
            ok = (
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(target)],
                    capture_output=True,
                    timeout=20,
                    **hidden_run_kwargs(),
                ).returncode
                == 0
            )
        else:
            import signal

            method = "killpg"
            try:
                os.killpg(os.getpgid(target), signal.SIGKILL)
                ok = True
            except (ProcessLookupError, PermissionError, OSError):
                os.kill(target, signal.SIGKILL)
                ok = True
    except Exception:
        ok = False

    if proc is not None:
        try:  # reap the direct child so it does not linger as a zombie
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    return {"method": method, "ok": ok}


class _WinJob:
    """A Windows job object holding a child and everything it spawns afterwards.

    `taskkill /T` walks the tree from a live parent; once the direct child has exited, its
    orphans are out of reach by pid. The job still holds them. Best-effort and fail-open:
    any failure leaves `handle` None and the caller falls back to taskkill. A descendant
    spawned in the instant between Popen and the assignment is not in the job.
    """

    def __init__(self, proc: "subprocess.Popen"):
        self.handle = None
        if not IS_WINDOWS:
            return
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            if not kernel32.AssignProcessToJobObject(ctypes.c_void_p(handle), ctypes.c_void_p(int(proc._handle))):
                kernel32.CloseHandle(ctypes.c_void_p(handle))
                return
            self.handle = handle
        except Exception:
            self.handle = None

    def terminate(self) -> bool:
        if not self.handle:
            return False
        try:
            import ctypes

            return bool(ctypes.windll.kernel32.TerminateJobObject(ctypes.c_void_p(self.handle), 1))
        except Exception:
            return False

    def close(self) -> None:
        if not self.handle:
            return
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
        except Exception:
            pass
        self.handle = None


def _kill_leftovers(proc: "subprocess.Popen", job: _WinJob) -> None:
    """Kill what is left of an EXITED child's tree: descendants still holding its pipes."""
    if IS_WINDOWS:
        job.terminate()
        return
    import signal

    try:  # the child led its own group (start_new_session); the group outlives the leader
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _pipe_reader(stream, chunks: list[bytes]):
    """Drain `stream` into `chunks` on a daemon thread, so a full pipe never blocks the
    child and a pipe held open by an orphan never blocks the caller. Bytes, read as they
    arrive (`read1`), so output already written is kept even when the reader is given up."""
    import threading

    def read() -> None:
        try:
            for chunk in iter(lambda: stream.read1(65536), b""):
                chunks.append(chunk)
        except Exception:
            pass

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread


def _join_readers(readers: list, seconds: float) -> bool:
    """Wait at most `seconds` in total for the pipe readers; True when both reached EOF."""
    import time

    deadline = time.monotonic() + max(0.0, seconds)
    for thread in readers:
        thread.join(max(0.0, deadline - time.monotonic()))
    return not any(thread.is_alive() for thread in readers)


def run_bounded(
    args: list[str],
    timeout: float,
    cwd: str | None = None,
    drain_seconds: float = 5,
    on_tick=None,
    tick_seconds: float = 20.0,
) -> tuple[int | None, str, str]:
    """Run `args` to completion or `timeout`; (returncode, stdout, stderr). None = timed out.

    `subprocess.run(timeout=...)` is not bounded: on timeout it kills only the direct child
    and then waits on the pipes with no limit, so a grandchild still holding stdout (a test
    runner's worker, a `.cmd` shim's node) hangs the caller on Windows. Here the child runs in
    its own process group, the whole tree is killed on timeout, and the output already
    written is collected for at most `drain_seconds` before giving up on it.

    The direct child's exit is what ends the run, not EOF on its pipes. A runner that exits
    0 but leaves a dev server or watcher holding stdout used to wait out the whole timeout
    and come back as a timeout (a false fail), leaking the orphan. Now, once the child has
    exited, the pipes get `drain_seconds`; whatever still holds them is killed — its process
    group on POSIX, its job object on Windows — and the child's real returncode is returned.

    `on_tick(elapsed_seconds)`, when given, is called about every `tick_seconds` while the
    child runs, so a caller can keep a heartbeat alive; an exception from it is ignored.
    """
    import time

    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **hidden_run_kwargs(),
    )
    job = _WinJob(proc)
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []

    def text(chunks: list[bytes]) -> str:  # what text=True gave: utf-8, universal newlines
        decoded = b"".join(list(chunks)).decode("utf-8", errors="replace")
        return decoded.replace("\r\n", "\n").replace("\r", "\n")

    try:
        readers = [_pipe_reader(proc.stdout, out_chunks), _pipe_reader(proc.stderr, err_chunks)]
        started = time.monotonic()
        deadline = started + timeout
        next_tick = started + tick_seconds
        timed_out = False
        while True:
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                break
            wake = min(deadline, next_tick) if on_tick is not None else deadline
            try:
                proc.wait(timeout=max(0.01, wake - now))
                break
            except subprocess.TimeoutExpired:
                pass
            if on_tick is not None and time.monotonic() >= next_tick:
                try:
                    on_tick(round(time.monotonic() - started, 1))
                except Exception:
                    pass
                next_tick += tick_seconds
        if timed_out:
            job.terminate()
            terminate_tree(proc)
            _join_readers(readers, drain_seconds)
            return None, text(out_chunks), text(err_chunks)
        if not _join_readers(readers, drain_seconds):
            # Exited, but something it started still holds a pipe: kill it, then give the
            # readers a moment to see EOF. One outside the tree keeps its pipe; given up on.
            _kill_leftovers(proc, job)
            _join_readers(readers, drain_seconds)
        return proc.returncode, text(out_chunks), text(err_chunks)
    except BaseException:
        job.terminate()
        terminate_tree(proc)
        raise
    finally:
        job.close()


def process_alive(pid: int | None) -> bool:
    """True if `pid` is a live process. Cross-OS, stdlib only.

    POSIX: signal 0 probe. Windows: OpenProcess via ctypes.
    Conservative: unknown/None -> False (treat as dead so the reaper can act).
    """
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        return _win_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _win_process_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_create_time(pid: int | None) -> int | None:
    """Process creation time as an opaque comparable integer, or None if unknowable.

    Windows: the 64-bit creation FILETIME (100ns ticks) via GetProcessTimes — unique per
    (pid, launch) so it survives PID recycling. Non-Windows or any failure: None. This is
    NOT a wall-clock value; its only use is proving a PID still hosts the process we
    recorded before we kill it.
    """
    if not pid or pid <= 0 or not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_t = wintypes.FILETIME()
        kernel_t = wintypes.FILETIME()
        user_t = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_t),
            ctypes.byref(kernel_t),
            ctypes.byref(user_t),
        )
        if not ok:
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    except Exception:
        return None
    finally:
        kernel32.CloseHandle(handle)


def process_identity(pid: int | None) -> str | None:
    """Process start identity, stable across PID reuse, or None if unknowable.

    Windows: the native creation FILETIME. Linux: boot id plus start ticks from `/proc`.
    Other POSIX (macOS, BSD): the start time `ps -o lstart=` prints. The value is opaque;
    its only use is comparing it with one recorded earlier for the same PID.
    """
    if not pid or pid <= 0:
        return None
    native = pid_create_time(pid)
    if native is not None:
        return f"native:{native!r}"
    if IS_WINDOWS:
        return None
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        start_ticks = stat_text.rsplit(")", 1)[1].split()[19]
        try:
            boot_id = (
                Path("/proc/sys/kernel/random/boot_id")
                .read_text(encoding="ascii")
                .strip()
            )
        except OSError:
            boot_id = "unknown-boot"
        return f"proc:{boot_id}:{start_ticks}"
    except (OSError, IndexError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            **hidden_run_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    started = (result.stdout or "").strip()
    return f"ps:{started}" if result.returncode == 0 and started else None


PROCESS_MATCHED = "matched"  # the PID still hosts the process we recorded
PROCESS_REUSED = "reused"  # the PID is live but proven to host a different process
PROCESS_DEAD = "dead"  # no process has the PID
PROCESS_UNVERIFIABLE = "unverifiable"  # live, but nothing recorded or readable to compare


def process_generation(
    pid: int | None,
    expected_create_time: int | None = None,
    expected_identity: str | None = None,
) -> str:
    """Which process a recorded PID names now: one of the PROCESS_* values.

    The recorded identity is compared when there is one, else the recorded create time.
    Anything that cannot be compared is `unverifiable`, never `matched`: a caller that is
    about to kill must treat it as a different process (see JobManager._kill_worker).
    """
    if not process_alive(pid):
        return PROCESS_DEAD
    if expected_identity is not None:
        current = process_identity(pid)
    elif expected_create_time is not None:
        current, expected_identity = pid_create_time(pid), expected_create_time
    else:
        return PROCESS_UNVERIFIABLE
    if current is None:
        return PROCESS_UNVERIFIABLE
    return PROCESS_MATCHED if current == expected_identity else PROCESS_REUSED


def open_lock_file(path: Path):
    """Open `path` for a byte-0 advisory lock, giving an empty file the byte to lock."""
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
    except OSError:
        handle.close()
        raise
    return handle


def lock_file(handle, blocking: bool = False) -> None:
    """Exclusive advisory lock on byte 0 of an open file. Raises OSError while held.

    Per open handle on both platforms (LockFileEx on Windows, flock on POSIX), so two
    threads of one process exclude each other as two processes do. Blocking on Windows
    is msvcrt's: it retries for about ten seconds, then raises OSError.
    """
    handle.seek(0)
    if IS_WINDOWS:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_file(handle) -> None:
    """Release the lock `lock_file` took on `handle`."""
    handle.seek(0)
    if IS_WINDOWS:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
