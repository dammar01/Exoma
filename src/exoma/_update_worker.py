"""Standalone updater shared by Windows, Linux and macOS.

Only stdlib imports belong here: the environment containing Exoma can disappear while
this file is running. Native handles are deliberately local to this protocol; shared
worker/job cleanup helpers have different lifetime guarantees.
"""

import ctypes
from ctypes import wintypes as w
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import signal
import selectors

SCHEMA = 1
HANDSHAKE_SECONDS = 15
CALLER_SECONDS = 60
TERMINAL = {"succeeded", "failed", "interrupted"}


def canonical(path):
    return os.path.normcase(os.path.realpath(path))


def inside(path, root):
    return Path(canonical(path)).is_relative_to(Path(canonical(root)))


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def wait_for_token(path, token, timeout, on_wait):
    """Poll one handshake within one deadline, including Windows read contention.

    A successful atomic publication can still be temporarily unreadable. A missing
    file or PermissionError is retryable, not evidence that the message was accepted.
    Corrupt JSON and other errors remain fatal. Callbacks keep peer-exit checks live.
    """
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            message = read_json(path)
        except (FileNotFoundError, PermissionError) as exc:
            last_error = exc
        else:
            last_error = None
            if message.get("token") == token:
                return
        on_wait()
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.05, remaining))
    detail = f"; last read error: {last_error}" if last_error is not None else ""
    raise TimeoutError(f"handoff did not acknowledge {Path(path).name}{detail}") from last_error


def atomic_json(path, value):
    path = Path(path)
    staged = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with staged.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                os.replace(staged, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        staged.unlink(missing_ok=True)


class Win32:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows process protocol is unavailable on this platform")
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "GetCurrentProcess": (w.HANDLE, []),
            "CloseHandle": (w.BOOL, [w.HANDLE]),
            "OpenProcess": (w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
            "GetProcessTimes": (w.BOOL, [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]),
            "QueryFullProcessImageNameW": (w.BOOL, [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]),
            "WaitForSingleObject": (w.DWORD, [w.HANDLE, w.DWORD]),
            "IsProcessInJob": (w.BOOL, [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]),
            "CreateMutexW": (w.HANDLE, [ctypes.c_void_p, w.BOOL, w.LPCWSTR]),
            "ReleaseMutex": (w.BOOL, [w.HANDLE]),
            "CreateJobObjectW": (w.HANDLE, [ctypes.c_void_p, w.LPCWSTR]),
            "SetInformationJobObject": (w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]),
            "QueryInformationJobObject": (w.BOOL, [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]),
            "TerminateJobObject": (w.BOOL, [w.HANDLE, w.UINT]),
            "AssignProcessToJobObject": (w.BOOL, [w.HANDLE, w.HANDLE]),
            "TerminateProcess": (w.BOOL, [w.HANDLE, w.UINT]),
            "ResumeThread": (w.DWORD, [w.HANDLE]),
            "CreateToolhelp32Snapshot": (w.HANDLE, [w.DWORD, w.DWORD]),
            "InitializeProcThreadAttributeList": (w.BOOL, [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)]),
            "UpdateProcThreadAttribute": (w.BOOL, [ctypes.c_void_p, w.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                                   ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]),
            "DeleteProcThreadAttributeList": (None, [ctypes.c_void_p]),
        }
        for name, (result, args) in signatures.items():
            function = getattr(self.k, name)
            function.restype, function.argtypes = result, args

    def require(self, result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def close(self, handle):
        if handle:
            self.k.CloseHandle(handle)

    def in_job(self):
        answer = w.BOOL()
        self.require(self.k.IsProcessInJob(self.k.GetCurrentProcess(), None, ctypes.byref(answer)))
        return bool(answer.value)

    def job_snapshot(self):
        result = {"in_job": self.in_job(), "ancestor_coverage": "unknown"}
        if not result["in_job"]:
            return result
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ro", "wo", "oo", "rt", "wt", "ot")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("pm", ctypes.c_size_t),
                        ("jm", ctypes.c_size_t), ("peak_pm", ctypes.c_size_t), ("peak_jm", ctypes.c_size_t)]
        info = Extended()
        if self.k.QueryInformationJobObject(None, 9, ctypes.byref(info), ctypes.sizeof(info), None):
            flags = info.basic.flags
            result.update(immediate_flags=flags, breakaway=bool(flags & 0x800),
                          silent_breakaway=bool(flags & 0x1000), kill_on_close=bool(flags & 0x2000))
        else:
            result["query_error"] = ctypes.get_last_error()
        return result

    def identity(self, handle, pid):
        times = [w.FILETIME() for _ in range(4)]
        self.require(self.k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)))
        size = w.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        self.require(self.k.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)))
        return {"pid": pid, "created": times[0].dwHighDateTime << 32 | times[0].dwLowDateTime,
                "image": canonical(image.value)}

    def open_identity(self, pid, expected=None):
        handle = self.k.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87 and expected:
                return None  # Original process is already gone.
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            identity = self.identity(handle, pid)
            if expected and identity != {k: expected[k] for k in ("pid", "created", "image")}:
                self.close(handle)
                return None  # The recorded generation exited; never wait for its replacement.
            return handle
        except BaseException:
            self.close(handle)
            raise

    def process(self, pid):
        handle = self.open_identity(pid)
        try:
            return self.identity(handle, pid)
        finally:
            self.close(handle)

    def parents(self):
        class Entry(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                        ("heap", ctypes.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                        ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                        ("name", w.WCHAR * 260)]
        first, following = self.k.Process32FirstW, self.k.Process32NextW
        for function in (first, following):
            function.restype, function.argtypes = w.BOOL, [w.HANDLE, ctypes.POINTER(Entry)]
        snapshot = self.k.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            result, row = {}, Entry()
            row.size = ctypes.sizeof(row)
            present = first(snapshot, ctypes.byref(row))
            self.require(present)
            while present:
                result[row.pid] = row.parent
                present = following(snapshot, ctypes.byref(row))
            return result
        finally:
            self.close(snapshot)

    def user(self):
        # SID, rather than HOME/USERNAME, keeps the installation lock stable under overrides.
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
        adv.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             w.DWORD, ctypes.POINTER(w.DWORD)]
        adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
        self.k.LocalFree.argtypes, self.k.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
        token, size, text = w.HANDLE(), w.DWORD(), w.LPWSTR()
        self.require(adv.OpenProcessToken(self.k.GetCurrentProcess(), 8, ctypes.byref(token)))
        try:
            adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            data = ctypes.create_string_buffer(size.value)
            self.require(adv.GetTokenInformation(token, 1, data, size, ctypes.byref(size)))
            sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
            self.require(adv.ConvertSidToStringSidW(sid, ctypes.byref(text)))
            try:
                return text.value
            finally:
                self.k.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            self.close(token)

    def signaled(self, handle):
        result = self.k.WaitForSingleObject(handle, 0)
        if result not in (0, 258):
            raise OSError(f"unexpected process wait result: {result}")
        return result == 0


def installation_key(prefix, user):
    return hashlib.sha256((user + "\0" + canonical(prefix)).encode()).hexdigest()


class Posix:
    """Native lifecycle adapter; no imports from the installation being replaced."""
    def user(self):
        return str(os.getuid())

    def in_job(self):
        return False  # POSIX sessions do not imply escape from cgroups/containers.

    def process(self, pid):
        if sys.platform == "darwin":
            # Apple's libproc ABI: proc_info.h / libproc.h. Use the native image,
            # not ps argv[0], and a microsecond start identity for PID reuse checks.
            class BSDInfo(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint32) for name in (
                    "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
                _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
                _fields_ += [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
                _fields_ += [("nice", ctypes.c_int32), ("seconds", ctypes.c_uint64), ("micros", ctypes.c_uint64)]
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            library.proc_pidinfo.restype = ctypes.c_int
            library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            library.proc_pidpath.restype = ctypes.c_int
            info, image = BSDInfo(), ctypes.create_string_buffer(4096)
            if library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
                error = ctypes.get_errno()
                if error in {2, 3}:
                    raise ProcessLookupError(pid)
                raise OSError(error, "cannot verify process generation")
            if info.status == 5:
                raise ProcessLookupError(pid)
            if library.proc_pidpath(pid, image, len(image)) <= 0:
                raise OSError(ctypes.get_errno(), "cannot verify process image")
            return {"pid": pid, "created": info.seconds * 1000000 + info.micros,
                    "image": canonical(os.fsdecode(image.value))}
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return {"pid": pid, "created": int(stat[19]),
                    "image": canonical(os.readlink(f"/proc/{pid}/exe"))}
        except FileNotFoundError:
            if Path("/proc").is_dir():
                raise ProcessLookupError(pid) from None
        # macOS/BSD: fail closed if the start identity cannot be read.
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
                                capture_output=True, text=True, timeout=2, check=False)
        text = result.stdout.strip()
        if result.returncode or len(text) < 25:
            raise ProcessLookupError(pid)
        return {"pid": pid, "created": text[:24], "image": canonical(text[24:].strip())}

    def parents(self):
        return {os.getpid(): os.getppid()}

    def open_identity(self, pid, expected=None):
        try:
            identity = self.process(pid)
        except ProcessLookupError:
            return None
        if expected and identity != {key: expected[key] for key in ("pid", "created", "image")}:
            return None
        return identity

    def signaled(self, handle):
        return self.open_identity(handle["pid"], handle) is None

    def close(self, handle):
        pass


def platform_api():
    return Win32() if os.name == "nt" else Posix()


class PosixLock:
    def __init__(self, api, key, *, recovery=False):
        import fcntl
        import pwd
        root = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache" / "exoma" / "update-locks"
        root.mkdir(parents=True, exist_ok=True)
        self.handle = (root / (key + ".lock")).open("a+b")
        self.claim_path = root / (key + ".claim.json")
        self.claim = None
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.claim_path.exists() and not recovery:
                raise OSError(f"installation cleanup is unconfirmed; inspect {self.claim_path}")
        except BaseException:
            self.handle.close()
            raise
        self.abandoned = False

    def reserve(self, operation, helper):
        self.claim = {"schema_version": SCHEMA, "operation": str(operation), "helper": helper,
                      "boot": boot_identity(), "stage": None}
        atomic_json(self.claim_path, self.claim)

    def stage(self, guardian, receipt):
        self.claim.update(stage={"guardian": guardian, "receipt": str(receipt)})
        atomic_json(self.claim_path, self.claim)

    def confirm_cleanup(self):
        if self.claim is not None:
            if read_json(self.claim_path) != self.claim:
                raise OSError("installation cleanup claim changed ownership")
            self.claim_path.unlink()
            self.claim = None

    def close(self):
        self.handle.close()


def boot_identity():
    if sys.platform.startswith("linux"):
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if sys.platform == "darwin":
        value = subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.boottime"], text=True)
        fields = dict(re.findall(r"\b(sec|usec)\s*=\s*(\d+)", value))
        if set(fields) != {"sec", "usec"}:
            raise ValueError("unverifiable native boot generation")
        # sysctl appends a date in the caller's timezone; that presentation must
        # never be mistaken for a reboot and authorize uncertain cleanup recovery.
        return "darwin:" + fields["sec"] + ":" + fields["usec"]
    raise OSError("no native boot identity available for update ownership")


class PosixChildJob:
    """A guardian holds installation ownership and cleans the stage on pipe EOF."""
    def __init__(self, api, key, lock):
        self.api, self.lock, self.proc = api, lock, None
        self.cleanup_unconfirmed, self.result, self.early_reply = False, None, None

    def run(self, argv, cwd, log, timeout, tick):
        if self.cleanup_unconfirmed:
            raise OSError("previous guardian cleanup is unconfirmed")
        self.result, self.early_reply = None, None
        self.proc = subprocess.Popen([sys.executable, "-I", "-S", "-B", __file__, "--guardian"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, pass_fds=(self.lock.handle.fileno(),), text=True)
        receipt = Path(log).with_name(Path(log).name + ".cleanup-" + uuid.uuid4().hex + ".json")
        request = {"argv": argv, "cwd": str(cwd), "log": str(log), "timeout": timeout,
                   "receipt": str(receipt)}
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self.proc.stdout, selectors.EVENT_READ)
                if not selector.select(HANDSHAKE_SECONDS):
                    raise TimeoutError("guardian did not register a child")
                identity = json.loads(self.proc.stdout.readline())
            if not isinstance(identity, dict) or not all(key in identity for key in ("pid", "created", "image")):
                self.early_reply = identity if isinstance(identity, dict) else None
                raise OSError("guardian failed before child registration")
            tick(identity)
            if hasattr(self.lock, "stage"):
                self.lock.stage(self.api.process(self.proc.pid), receipt)
            self.proc.stdin.write("go\n")
            self.proc.stdin.flush()
            deadline, next_beat = time.monotonic() + timeout, 0
            while self.proc.poll() is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"stage exceeded {timeout:g}s")
                if time.monotonic() >= next_beat:
                    tick(None)
                    next_beat = time.monotonic() + 1
                time.sleep(0.05)
            self.cleanup()
            return self.result["exit_code"]
        finally:
            self.cleanup()

    def cleanup(self):
        if self.proc:
            try:
                if not self.proc.stdin.closed:
                    try:
                        self.proc.stdin.close()  # EOF is also sent on supervisor death.
                    except BrokenPipeError:
                        pass
                self.proc.wait(timeout=15)
                reply = self.early_reply or json.loads(self.proc.stdout.readline(8192))
                if not isinstance(reply, dict) or reply.get("cleanup_confirmed") is not True or not isinstance(reply.get("exit_code"), int):
                    raise OSError("guardian did not confirm stage cleanup")
                self.result = reply
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self.cleanup_unconfirmed = True
            finally:
                self.proc.stdout.close()
                self.proc = None
        if self.cleanup_unconfirmed:
            raise OSError("guardian cleanup unconfirmed; inspect child group before retrying")

    def close(self):
        self.cleanup()


class ChildExit:
    """Observe exit without reaping: the stage PID/PGID cannot be reused before cleanup."""
    def __init__(self, pid):
        self.pid, self.seen, self.queue = pid, False, None
        if not hasattr(os, "waitid"):
            import select
            self.queue = select.kqueue()
            event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                                  fflags=select.KQ_NOTE_EXIT)
            self.queue.control([event], 0, 0)

    def exited(self):
        if not self.seen:
            self.seen = (bool(self.queue.control(None, 1, 0)) if self.queue else
                         os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None)
        return self.seen

    def close(self):
        if self.queue:
            self.queue.close()


class DescendantOwner:
    """Linux adopts escaped descendants; other POSIX hosts fail closed on ambiguity."""
    def __init__(self):
        self.subreaper = sys.platform.startswith("linux")
        if self.subreaper:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
            libc.prctl.restype = ctypes.c_int
            if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER, before stage fork
                raise OSError(ctypes.get_errno(), "cannot adopt orphaned update descendants")
            self.baseline = None
        else:
            self.baseline = self.inventory()

    @staticmethod
    def inventory():
        # Inventory includes unrelated processes, deliberately. A new process whose
        # ancestry was lost cannot safely be declared unrelated or killed. ps start
        # times plus native generations prevent PID reuse from masking that gap.
        proc = subprocess.Popen(["/bin/ps", "-axo", "pid=,ppid=,lstart="],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            stdout, _ = proc.communicate(timeout=5)
            if proc.returncode:
                raise OSError("cannot enumerate native process ancestry")
            rows = stdout.splitlines()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        result, api = {}, Posix()
        for row in rows:
            pid_text, parent, stamp = row.strip().split(None, 2)
            pid = int(pid_text)
            if pid == proc.pid:
                continue  # This waitable inventory child is already reaped.
            try:
                created = api.process(pid)["created"]
            except ProcessLookupError:
                # A disappearing new parent may already have orphaned a child
                # after ps enumerated it. Keep that uncertainty instead of hiding
                # the fork/exit race behind an empty final snapshot.
                created = stamp
            except OSError:
                created = stamp
            result[pid] = {"created": created, "parent": int(parent)}
        return result

    def cleanup(self):
        if not self.subreaper:
            current = self.inventory()
            def unchanged(pid):
                return pid in current and pid in self.baseline and (
                    current[pid]["created"] == self.baseline[pid]["created"])
            def foreign(pid):
                seen = set()
                while pid in current and pid not in seen:
                    # A surviving parent that predated our stage and is outside
                    # our guardian proves foreign ancestry. PID 1 is excluded:
                    # escaped descendants may already have been reparented to it.
                    if pid not in {0, 1, os.getpid()} and unchanged(pid):
                        return True
                    if pid == os.getpid():
                        return False
                    seen.add(pid)
                    pid = current[pid]["parent"]
                return False
            remaining = [pid for pid in current if pid != os.getpid() and not unchanged(pid) and not foreign(pid)]
            if remaining:
                raise OSError("descendant ancestry cannot be confirmed: " + repr(sorted(remaining)))
            return
        # Every orphan, including setsid/double-fork children, is adopted by this
        # guardian. Do not reap a live generation before signalling it: waitable
        # direct children pin PIDs, unlike a process-tree snapshot followed by kill.
        deadline = time.monotonic() + 5
        while True:
            try:
                while True:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                    if pid == 0:
                        break
            except ChildProcessError:
                return  # Kernel confirms there are no remaining owned descendants.
            children = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
            for pid in children:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if time.monotonic() >= deadline:
                raise OSError("adopted update descendants did not terminate")
            time.sleep(0.01)


def guardian():
    """No manager instruction runs until group registration and the owner GO message."""
    request = json.loads(sys.stdin.readline())
    child, monitor, owner, failure, cleanup_error, code = None, None, None, None, None, 1
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        owner = DescendantOwner()
        with Path(request["log"]).open("ab", buffering=0) as log:
            child = subprocess.Popen([sys.executable, "-I", "-S", "-B", __file__, "--exec-stage",
                                      json.dumps(request["argv"])], cwd=request["cwd"],
                                     stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True)
            monitor = ChildExit(child.pid)
            print(json.dumps(Posix().process(child.pid)), flush=True)
            with selectors.DefaultSelector() as selector:
                selector.register(sys.stdin, selectors.EVENT_READ)
                if not selector.select(HANDSHAKE_SECONDS) or sys.stdin.readline().strip() != "go":
                    raise OSError("supervisor disappeared before stage registration")
                child.stdin.write(b"1")
                child.stdin.close()
                deadline = time.monotonic() + request["timeout"]
                while not monitor.exited():
                    if selector.select(0.05):
                        if not sys.stdin.readline():
                            raise OSError("supervisor control pipe closed")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("stage deadline")
    except Exception as exc:
        failure = str(exc)[:512]
    finally:
        if child:
            try:
                if not monitor or not monitor.exited():
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    until = time.monotonic() + 2
                    while monitor and not monitor.exited() and time.monotonic() < until:
                        time.sleep(0.05)
                # Reap ONLY after the last group signal. Holding a waitable child pins
                # the group leader's PID even when it already exited successfully.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                code = child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as exc:
                cleanup_error = str(exc)[:512]
        if owner:
            try:
                owner.cleanup()
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                cleanup_error = str(exc)[:512]
        if monitor:
            monitor.close()
    result = {"cleanup_confirmed": cleanup_error is None,
              "exit_code": code if failure is None else 1, "error": cleanup_error or failure}
    if request.get("receipt"):
        atomic_json(request["receipt"], result)
    try:
        print(json.dumps(result), flush=True)
    except BrokenPipeError:
        pass  # Owner died; cleanup has still completed before the lease closes.
    return result["exit_code"] if result["cleanup_confirmed"] else 4


def outcome(operation, expected_key=None):
    operation = Path(operation)
    state = read_json(operation / "state.json")
    phase = state.get("phase")
    if state.get("schema_version") != SCHEMA or state.get("operation_id") != operation.name or (
            expected_key is not None and state.get("installation_key") != expected_key):
        raise ValueError("unsupported or mismatched update state")
    if phase == "cleanup_pending" or state.get("cleanup_confirmed") is False:
        code = 4
    elif phase == "succeeded":
        code = 0 if all(state.get("stages", {}).get(name, {}).get("exit_code") == 0
                       for name in ("package", "probe", "install", "check")) else 4
    elif phase in TERMINAL:
        code = 130 if phase == "interrupted" and state.get("cancellation") else 1
    elif phase in {"preparing", "accepted", "waiting_callers", "package", "probe", "install", "check"}:
        api = platform_api()
        handle = api.open_identity(state["helper"]["pid"], state["helper"])
        try:
            code = 3 if handle and not api.signaled(handle) and time.time() - float(state["heartbeat"]) < 30 else 4
        finally:
            api.close(handle)
    else:
        raise ValueError("unknown journal phase")
    return {**state, "completed": code in {0, 1, 130}, "exit_code": code}, code


def observe(operation, wait=False, cancel=False):
    """Read a copied operation without importing/restarting the target installation."""
    operation = Path(operation).resolve()
    request = read_json(operation / "request.json")
    if cancel:
        atomic_json(operation / "cancel.json", {"token": request["token"], "source": "explicit_observer", "at": time.time()})
    while True:
        state, code = outcome(operation, request["installation_key"])
        if not wait or code != 3:
            print(json.dumps(state, indent=2))
            return code
        time.sleep(0.2)


def callers(api, interpreter, entrypoints):
    """Only the current interpreter, its tool proxy, and its proven Exoma launcher."""
    rows = api.parents()
    current = api.process(os.getpid())
    if os.name != "nt":
        return [{**current, "parent": os.getppid()}]
    result = [current]
    for _ in range(8):
        parent = rows.get(current["pid"])
        if not parent:
            break
        candidate = api.process(parent)
        if candidate["created"] > current["created"]:
            raise ValueError("caller parent generation changed during discovery")
        image = candidate["image"]
        launcher = image in entrypoints
        if image != canonical(interpreter) and not launcher:
            if Path(image).name.lower() == "exoma.exe":
                raise ValueError("cannot verify the invoking Exoma launcher")
            break
        result.append(candidate)
        if launcher:
            break  # A Python host above the launcher is not a tool proxy.
        current = candidate
    else:
        raise ValueError("unexpectedly deep launcher chain")
    for index, identity in enumerate(result):
        identity["parent"] = rows.get(identity["pid"], 0)
        if index and result[index - 1]["parent"] != identity["pid"]:
            raise ValueError("launcher ancestry changed")
    return result


class InstallationLock:
    def __init__(self, api, key):
        self.api, self.handle, self.owned = api, None, False
        self.handle = api.require(api.k.CreateMutexW(None, False, "Global\\ExomaUpdate-" + key))
        result = api.k.WaitForSingleObject(self.handle, 0)
        self.owned = result in (0, 128)
        if not self.owned:
            api.close(self.handle)
            self.handle = None
            raise OSError("another updater owns this installation (busy)")
        self.abandoned = result == 128

    def close(self):
        if self.owned:
            self.api.k.ReleaseMutex(self.handle)
            self.owned = False
        self.api.close(self.handle)
        self.handle = None


class ChildJob:
    """A worker-owned job contains each mutative child before its first instruction."""
    def __init__(self, api, key):
        self.api, self.handle = api, None
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ro", "wo", "oo", "rt", "wt", "ot")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("pm", ctypes.c_size_t),
                        ("jm", ctypes.c_size_t), ("peak_pm", ctypes.c_size_t), ("peak_jm", ctypes.c_size_t)]
        self.handle = api.require(api.k.CreateJobObjectW(None, "Global\\ExomaUpdateChild-" + key))
        try:
            if self.active():
                raise OSError("previous update children are still running; package state is uncertain")
            info = Extended()
            info.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; the handle is never inheritable.
            api.require(api.k.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)))
        except BaseException:
            api.close(self.handle)
            self.handle = None
            raise

    def active(self):
        class Accounting(ctypes.Structure):
            _fields_ = [("ut", ctypes.c_int64), ("kt", ctypes.c_int64),
                        ("put", ctypes.c_int64), ("pkt", ctypes.c_int64),
                        ("faults", w.DWORD), ("total", w.DWORD), ("active", w.DWORD), ("ended", w.DWORD)]
        value = Accounting()
        self.api.require(self.api.k.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None))
        return value.active

    def cleanup(self):
        if self.active():
            self.api.require(self.api.k.TerminateJobObject(self.handle, 1))
        until = time.monotonic() + 15
        while self.active():
            if time.monotonic() >= until:
                raise OSError("child cleanup is not confirmed; do not retry the package command")
            time.sleep(0.05)

    def close(self):
        self.api.close(self.handle)
        self.handle = None

    def run(self, argv, cwd, log, timeout, tick):
        from ctypes import wintypes
        import msvcrt
        class Startup(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR),
                        ("title", w.LPWSTR), *[(name, w.DWORD) for name in
                         ("x", "y", "xs", "ys", "xc", "yc", "fill", "flags")],
                        ("show", w.WORD), ("reserved2_size", w.WORD),
                        ("reserved2", ctypes.POINTER(w.BYTE)), ("stdin", w.HANDLE),
                        ("stdout", w.HANDLE), ("stderr", w.HANDLE)]
        class ProcessInfo(ctypes.Structure):
            _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]
        class ExtendedStartup(ctypes.Structure):
            _fields_ = [("startup", Startup), ("attributes", ctypes.c_void_p)]
        create = self.api.k.CreateProcessW
        create.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL,
                           w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ProcessInfo)]
        create.restype = w.BOOL
        self.api.k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        info, extended = ProcessInfo(), ExtendedStartup()
        startup = extended.startup
        startup.cb, startup.flags, startup.show = ctypes.sizeof(extended), 0x101, 0
        # JOB_LIST is applied by CreateProcess itself. Merely creating suspended and
        # assigning afterward leaks an uncontained suspended child if the owner crashes
        # between those calls. Windows 10+ supports atomic job assignment at creation.
        size = ctypes.c_size_t()
        self.api.k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        self.api.require(self.api.k.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)))
        jobs = (w.HANDLE * 1)(self.handle)
        try:
            self.api.require(self.api.k.UpdateProcThreadAttribute(
                attributes, 0, 0x0002000D, jobs, ctypes.sizeof(jobs), None, None))
        except BaseException:
            self.api.k.DeleteProcThreadAttributeList(attributes)
            raise
        extended.attributes = ctypes.cast(attributes, ctypes.c_void_p)
        with open(os.devnull, "rb") as null, Path(log).open("ab", buffering=0) as output:
            hin, hout = msvcrt.get_osfhandle(null.fileno()), msvcrt.get_osfhandle(output.fileno())
            os.set_handle_inheritable(hin, True)
            os.set_handle_inheritable(hout, True)
            startup.stdin, startup.stdout, startup.stderr = hin, hout, hout
            try:
                self.api.require(create(str(argv[0]), ctypes.create_unicode_buffer(subprocess.list2cmdline(argv)),
                                        None, None, True, 0x4 | 0x08000000 | 0x200 | 0x80000, None, str(cwd),
                                        ctypes.byref(extended), ctypes.byref(info)))
            finally:
                os.set_handle_inheritable(hin, False)
                os.set_handle_inheritable(hout, False)
                self.api.k.DeleteProcThreadAttributeList(attributes)
            try:
                try:
                    member = w.BOOL()
                    self.api.require(self.api.k.IsProcessInJob(info.process, self.handle, ctypes.byref(member)))
                    if not member.value:
                        raise OSError("child did not enter the update job")
                    tick(self.api.identity(info.process, info.pid))
                    if self.api.k.ResumeThread(info.thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                except BaseException:
                    self.api.k.TerminateProcess(info.process, 1)
                    self.api.k.WaitForSingleObject(info.process, 15000)
                    raise
                deadline, next_beat = time.monotonic() + timeout, 0
                while not self.api.signaled(info.process):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"stage exceeded {timeout:g}s")
                    if time.monotonic() >= next_beat:
                        tick(None)
                        next_beat = time.monotonic() + 1
                    time.sleep(0.05)
                code = wintypes.DWORD()
                self.api.require(self.api.k.GetExitCodeProcess(info.process, ctypes.byref(code)))
                return code.value
            finally:
                try:
                    self.cleanup()
                finally:
                    self.api.close(info.thread)
                    self.api.close(info.process)


def run_request(operation):
    operation = Path(operation).resolve()
    request = read_json(operation / "request.json")
    api = platform_api()
    lock, job, handles = None, None, []
    final_phase, exit_code = "failed", 1
    state = {"schema_version": SCHEMA, "operation_id": operation.name,
             "installation_key": request["installation_key"], "phase": "preparing",
             "started_at": time.time(), "helper": api.process(os.getpid()), "stages": {},
             "version_before": request.get("version_before"), "version_after": None}

    def save(phase=None, **fields):
        state.update(fields)
        if phase:
            state["phase"] = phase
        state["heartbeat"] = time.time()
        atomic_json(operation / "state.json", state)

    try:
        if request.get("schema_version") != SCHEMA or request.get("operation_id") != operation.name:
            raise ValueError("unsupported update request")
        state["host_job"] = api.job_snapshot() if os.name == "nt" else {"session": os.getsid(0), "cgroup_escape": False}
        if api.in_job():
            raise OSError("worker still belongs to a host job; run the manager directly from a terminal")
        target = request["prefix"]
        if inside(operation, target) or inside(sys.executable, target):
            raise ValueError("update worker/runtime must be outside the target environment")
        if installation_key(target, api.user()) != request["installation_key"]:
            raise ValueError("installation identity mismatch")
        timeout = request["timeout"]
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid stage timeout")
        snapshot = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        if snapshot != request["worker_sha256"]:
            raise ValueError("worker snapshot mismatch")
        lock = InstallationLock(api, request["installation_key"]) if os.name == "nt" else PosixLock(api, request["installation_key"])
        if os.name != "nt":
            lock.reserve(operation, state["helper"])
        job = ChildJob(api, request["installation_key"]) if os.name == "nt" else PosixChildJob(api, request["installation_key"], lock)
        rows = api.parents()
        for identity in request["callers"]:
            handle = api.open_identity(identity["pid"], identity)
            if handle:
                handles.append(handle)
                if os.name == "nt" and not api.signaled(handle) and rows.get(identity["pid"]) != identity["parent"]:
                    raise ValueError("caller ancestry changed before acknowledgment")
        if not handles:
            raise ValueError("caller exited before handoff")
        save()
        reservation = request.get("reservation")
        if reservation:
            claim = read_json(reservation)
            if claim.get("token") != request["token"]:
                raise ValueError("update reservation ownership changed")
            atomic_json(reservation, {**claim, "helper": state["helper"]})
        atomic_json(operation.parent / "latest.json", {"operation_id": operation.name})
        atomic_json(operation / "ready.json", {"token": request["token"]})
        def check_caller():
            if all(api.signaled(h) for h in handles):
                raise TimeoutError("caller did not commit the handoff; package unchanged")
        wait_for_token(operation / "commit.json", request["token"], HANDSHAKE_SECONDS, check_caller)
        save("accepted", accepted_at=time.time())
        atomic_json(operation / "accepted.json", {"token": request["token"]})
        save("waiting_callers")
        deadline = time.monotonic() + CALLER_SECONDS
        while not all(api.signaled(h) for h in handles):
            if time.monotonic() >= deadline:
                raise TimeoutError("caller/launcher did not exit; package unchanged")
            save()
            time.sleep(0.2)
        save(callers_exited_at=time.time())
        for handle in handles:
            api.close(handle)
        handles.clear()

        def step(name, argv):
            state["stages"][name] = {"started_at": time.time(), "exit_code": None}
            save(name, child=None)
            def tick(child):
                try:
                    cancellation = read_json(operation / "cancel.json")
                except FileNotFoundError:
                    cancellation = None
                if cancellation and cancellation.get("token") == request["token"]:
                    state["cancellation"] = {key: cancellation.get(key) for key in ("source", "at")}
                    raise KeyboardInterrupt("explicit update cancellation")
                if child:
                    state["child"] = child
                save()
            try:
                code = job.run(argv, request["cwd"], operation / (name + ".log"), timeout, tick)
            except BaseException:
                raise
            state["stages"][name]["exit_code"] = code
            save(child=None)
            if code:
                raise RuntimeError(f"{name} failed (exit {code}); later stages skipped")

        # Admission remains reserved while the manager changes the installed package.
        os.environ["WORKFLOW_UPDATE_OWNER"] = request["token"]
        step("package", request["package"])
        interpreter = Path(target) / request["python_relative"]
        if not interpreter.is_file():
            raise OSError("target interpreter unavailable after package upgrade")
        # Probe the installed distribution from a fresh isolated target interpreter.
        probe = ("import importlib.metadata as m,json,sys,exoma; "
                 "print(json.dumps(dict(version=m.version('exoma'),module=exoma.__file__,prefix=sys.prefix)))")
        step("probe", [str(interpreter), "-I", "-B", "-c", probe])
        observed = json.loads((operation / "probe.log").read_text(encoding="utf-8-sig").strip())
        if canonical(observed["prefix"]) != canonical(target) or not inside(observed["module"], target):
            raise ValueError("fresh interpreter does not import Exoma from the target installation")
        save(version_after=observed["version"])
        fresh = [str(interpreter), "-I", "-B", "-m", "exoma"]
        step("install", [*fresh, "install", "--no-input"])
        step("check", [*fresh, "install", "--check"])
        final_phase, exit_code = "succeeded", 0
    except (Exception, KeyboardInterrupt) as exc:
        # Package rollback is never inferred from a manager error or process crash.
        recovery = "Inspect package.log and installed version; after all update children exit, run the manager directly."
        if "package" in state["stages"] and state["stages"]["package"]["exit_code"] == 0:
            recovery = "Package changed; repair the reported stage, then run exoma install --no-input and exoma install --check. Inspect new install receipts before rollback."
            receipts = []
            try:
                before = set(request.get("receipts_before", []))
                for path in Path(request["receipt_root"]).glob("install_*/install_receipt.json"):
                    if path.parent.name not in before and re.fullmatch(r"install_[A-Za-z0-9_-]+", path.parent.name):
                        receipts.append({"receipt": str(path), "rollback_argv": ["exoma", "install", "--rollback", path.parent.name, "--apply"]})
            except (OSError, KeyError, TypeError):
                pass
            state["new_install_receipts"] = receipts
            if request.get("workspace"):
                state["workspace_recovery_argv"] = ["exoma", "upgrade", "--work-dir", request["workspace"]]
        final_phase = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        state.update(error=str(exc), recovery=recovery)
        exit_code = 130 if state.get("cancellation") else 1
    finally:
        cleanup_error = None
        try:
            for handle in handles:
                api.close(handle)
            if job:
                try:
                    job.cleanup()
                finally:
                    job.close()
            if request.get("reservation"):
                reservation = Path(request["reservation"])
                try:
                    if read_json(reservation).get("token") != request["token"]:
                        raise OSError("update reservation ownership changed during cleanup")
                    reservation.unlink()
                except FileNotFoundError:
                    pass
            if lock and os.name != "nt":
                lock.confirm_cleanup()
        except Exception as exc:
            cleanup_error = str(exc)
        finally:
            if lock:
                try:
                    lock.close()
                except Exception as exc:
                    cleanup_error = str(exc)
        if cleanup_error:
            final_phase, exit_code = "cleanup_pending", 4
            state.update(cleanup_error=cleanup_error, recovery="Confirm updater children and reservation ownership before repair; do not replay the package stage.")
            if os.name != "nt":
                state["recovery_argv"] = [sys.executable, "-I", "-S", "-B", __file__, "--recover", str(operation)]
        # Terminal publication follows BOTH process cleanup and admission release.
        save(final_phase, finished_at=time.time(), cleanup_confirmed=cleanup_error is None,
             completed=cleanup_error is None)
    return exit_code


def recover_posix(operation):
    """Explicit recovery; never infer descendant cleanup merely from dead leaders."""
    if os.name == "nt":
        raise OSError("POSIX ownership recovery is unavailable on Windows")
    operation = Path(operation).resolve()
    request = read_json(operation / "request.json")
    api = Posix()
    key = installation_key(request["prefix"], api.user())
    if key != request["installation_key"]:
        raise ValueError("installation identity mismatch")
    lock = PosixLock(api, key, recovery=True)
    try:
        claim = read_json(lock.claim_path)
        if claim.get("schema_version") != SCHEMA or not isinstance(claim.get("boot"), str) or not claim["boot"]:
            raise ValueError("unverifiable installation cleanup claim")
        if claim.get("operation") != str(operation):
            raise OSError("another operation owns installation recovery")
        if claim.get("boot") == boot_identity():
            if not api.signaled(claim["helper"]):
                raise OSError("update supervisor is still alive")
            stage = claim.get("stage")
            if stage:
                if not api.signaled(stage["guardian"]):
                    raise OSError("stage guardian is still alive")
                if read_json(stage["receipt"]).get("cleanup_confirmed") is not True:
                    raise OSError("descendant cleanup is unconfirmed; recovery requires a reboot")
        # A changed native boot generation proves every old process exited. On the
        # same boot, only a guardian's durable cleanup receipt provides that proof.
        if request.get("reservation"):
            reservation = Path(request["reservation"])
            try:
                if read_json(reservation).get("token") != request["token"]:
                    raise OSError("update reservation ownership changed")
                reservation.unlink()
            except FileNotFoundError:
                pass
        lock.claim = claim
        lock.confirm_cleanup()
        state = read_json(operation / "state.json")
        state.update(phase="failed", completed=True, cleanup_confirmed=True,
                     recovered_at=time.time(), recovery="Cleanup confirmed; inspect stage outcomes before repair.")
        atomic_json(operation / "state.json", state)
        print(json.dumps(state, indent=2))
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--recover"]:
        raise SystemExit(recover_posix(sys.argv[2]))
    if sys.argv[1:2] == ["--guardian"]:
        raise SystemExit(guardian())
    if sys.argv[1:2] == ["--exec-stage"]:
        if sys.stdin.buffer.read(1) != b"1":
            raise SystemExit("stage not admitted")
        argv = json.loads(sys.argv[2])
        os.execvpe(argv[0], argv, os.environ)
    if sys.argv[1:2] == ["--observe"]:
        try:
            code = observe(sys.argv[2], wait="--wait" in sys.argv[3:], cancel="--cancel" in sys.argv[3:])
        except KeyboardInterrupt:
            state, code = outcome(sys.argv[2])
            print(json.dumps({**state, "observer_detached": True}, indent=2))
        raise SystemExit(code)
    if len(sys.argv) != 2:
        raise SystemExit("expected an operation directory")
    raise SystemExit(run_request(sys.argv[1]))
