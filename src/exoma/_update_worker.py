"""Standalone Windows updater. Its snapshot runs without importing the target package.

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


def callers(api, interpreter, entrypoints):
    """Only the current interpreter, its tool proxy, and its proven Exoma launcher."""
    rows = api.parents()
    current = api.process(os.getpid())
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
    api = Win32()
    lock, job, handles = None, None, []
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
        lock = InstallationLock(api, request["installation_key"])
        job = ChildJob(api, request["installation_key"])
        rows = api.parents()
        for identity in request["callers"]:
            handle = api.open_identity(identity["pid"], identity)
            if handle:
                handles.append(handle)
                if not api.signaled(handle) and rows.get(identity["pid"]) != identity["parent"]:
                    raise ValueError("caller ancestry changed before acknowledgment")
        if not handles:
            raise ValueError("caller exited before handoff")
        save()
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
        save("succeeded", finished_at=time.time())
        return 0
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
        save("interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
             error=str(exc), recovery=recovery, finished_at=time.time())
        return 1
    finally:
        for handle in handles:
            api.close(handle)
        if job:
            try:
                job.cleanup()
            finally:
                job.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("expected an operation directory")
    raise SystemExit(run_request(sys.argv[1]))
