"""Standalone updater shared by Windows, Linux and macOS.

Only stdlib imports belong here: the environment containing Exoma can disappear while
this file is running. Native handles are deliberately local to this protocol; shared
worker/job cleanup helpers have different lifetime guarantees.
"""

import ctypes
from ctypes import wintypes as w
import errno
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
GUARDIAN_RECEIPT_BYTES = 7168
DIAGNOSTIC_PIDS = 8
DIAGNOSTIC_RECORDS = 32
DIAGNOSTIC_PARENT_HOPS = 8
DIAGNOSTIC_STRING_CHARS = 256
# Measured by the SDK oracle in native run 44284715093 (clean 9902ec3).
# Keep one measured layout; verify its observed fields before enabling this process ABI.
DARWIN_KINFO_LAYOUT = {
    "architecture": "arm64", "record_size": 648, "alignment": 8,
    "pointer_width": 8, "endianness": "little",
    "fields": {"pid": {"offset": 40, "width": 4, "signed": True},
               "parent": {"offset": 560, "width": 4, "signed": True},
               "status": {"offset": 36, "width": 1, "signed": True},
               "start_seconds": {"offset": 0, "width": 8, "signed": True},
               "start_micros": {"offset": 8, "width": 4, "signed": True}}}
DARWIN_INVENTORY_BYTES = 16 * 1024 * 1024
DARWIN_INVENTORY_ATTEMPTS = 4
DARWIN_INVENTORY_SECONDS = 5
DIAGNOSTIC_SITES = {"owner_init", "spawn", "register", "release", "wait", "child_cleanup",
                    "owner_cleanup", "inventory", "exit_observation", "signal_term", "signal_kill",
                    "child_reap", "monitor_close", "signal_probe", "receipt_wait", "receipt_read",
                    "receipt_parse", "receipt_validate"}
DIAGNOSTIC_TYPES = {"OSError", "ProcessLookupError", "PermissionError", "TimeoutError", "ValueError",
                    "SubprocessError", "TimeoutExpired", "BrokenPipeError", "JSONDecodeError"}


def _diagnostic_error(site, exc):
    # Never persist str/repr(exc): subprocess exceptions can include argv/secrets.
    number = getattr(exc, "errno", None)
    return {"site": site if site in DIAGNOSTIC_SITES else "unknown",
            "type": type(exc).__name__ if type(exc).__name__ in DIAGNOSTIC_TYPES else "Exception",
            "errno": number if type(number) is int and abs(number) < 2 ** 31 else None}


def cleanup_diagnostic_projection(receipt):
    """Allowlisted evidence only; never copy legacy error strings, requests or argv."""
    def number(value):
        return value if type(value) is int and abs(value) < 2 ** 64 else None
    def error(value):
        if not isinstance(value, dict):
            return None
        return {"site": value.get("site") if isinstance(value.get("site"), str) and value["site"] in DIAGNOSTIC_SITES else "unknown",
                "type": value.get("type") if isinstance(value.get("type"), str) and value["type"] in DIAGNOSTIC_TYPES else "Exception",
                "errno": number(value.get("errno"))}
    if not isinstance(receipt, dict):
        return {"malformed": True}
    result = {"cleanup_confirmed": receipt.get("cleanup_confirmed") is True,
              "exit_code": number(receipt.get("exit_code"))}
    diagnostic = receipt.get("diagnostics")
    if not isinstance(diagnostic, dict):
        return result
    result["stage_error"] = error(diagnostic.get("stage_error"))
    errors = diagnostic.get("cleanup_errors")
    result["cleanup_errors"] = [error(value) for value in errors[:8]] if isinstance(errors, list) else []
    child = diagnostic.get("child")
    if isinstance(child, dict):
        lifecycle = {key: child.get(key) is True for key in ("exit_observed", "synthetic_exit", "release_requested")}
        lifecycle["child_returncode"] = number(child.get("child_returncode"))
        lifecycle["reap_outcome"] = child.get("reap_outcome") if child.get("reap_outcome") in (
            "no_child", "not_attempted", "pending", "reaped", "failed") else "unknown"
        signals = child.get("signals_attempted")
        lifecycle["signals_attempted"] = [s for s in signals[:4] if s in ("SIGTERM", "SIGKILL")] if isinstance(signals, list) else []
        probe = child.get("group_probe")
        if isinstance(probe, dict):
            lifecycle["group_probe"] = {
                "status": probe.get("status") if probe.get("status") in ("not_attempted", "present", "absent", "error") else "unknown",
                "error": error(probe.get("error"))}
        result["child"] = lifecycle
    ancestry = diagnostic.get("ancestry")
    if isinstance(ancestry, dict):
        projected = {key: number(ancestry.get(key)) for key in ("rejected_count", "omitted_pids", "omitted_records")}
        records = ancestry.get("records")
        projected["records"] = []
        for row in records[:8] if isinstance(records, list) else []:
            if not isinstance(row, dict):
                continue
            projected["records"].append({
                "snapshot": row.get("snapshot") if row.get("snapshot") in ("baseline", "current") else "unknown",
                **{key: number(row.get(key)) for key in ("pid", "parent", "created", "uniqueid", "parent_uniqueid", "version", "parent_version", "native_status")},
                "generation_source": row.get("generation_source") if row.get("generation_source") in ("native", "ps_fallback") else "unknown",
                "query_error": error(row.get("query_error")),
                "lineage_error": error(row.get("lineage_error"))})
        result["ancestry"] = projected
    if len(json.dumps(result, ensure_ascii=True)) > 4096:
        result.pop("ancestry", None)
        result["truncated"] = True
    return result


class _ProcessInventory(dict):
    """Keep generation lookup provenance beside the unchanged classifier records."""
    def __init__(self):
        super().__init__()
        self.query_status = {}
        self.diagnostics_unavailable = False
        self.enumeration = None
        self.lineage_binding = None


def _diagnostic_generation(value):
    if type(value) is int and abs(value) < 2 ** 64:
        return value
    if isinstance(value, str):
        return value[:DIAGNOSTIC_STRING_CHARS]
    return None


def _ancestry_diagnostics(baseline, current, remaining):
    rejected = sorted(remaining)[:DIAGNOSTIC_PIDS]
    selected, seen, strings_truncated, depth_truncated = [], set(), False, False
    # Keep both snapshots of rejected generations before spending the budget on
    # ancestors; a large baseline must not crowd out the current query failure.
    walks = [[name, inventory, pid, set()] for pid in rejected
             for name, inventory in (("current", current), ("baseline", baseline))]
    for _ in range(DIAGNOSTIC_PARENT_HOPS):
        for walk in walks:
            name, inventory, pid, chain = walk
            if pid not in inventory or pid in chain:
                continue
            chain.add(pid)
            key = (name, pid)
            if key not in seen:
                seen.add(key)
                row = inventory[pid]
                generation = row.get("created")
                strings_truncated |= isinstance(generation, str) and len(generation) > DIAGNOSTIC_STRING_CHARS
                record = {"snapshot": name, "pid": pid, "parent": row["parent"],
                          "created": _diagnostic_generation(generation)}
                for field in ("uniqueid", "parent_uniqueid", "version", "parent_version"):
                    if type(row.get(field)) is int and 0 <= row[field] < 2 ** 64:
                        record[field] = row[field]
                if type(row.get("status")) is int and 1 <= row["status"] <= 6:
                    record["native_status"] = row["status"]
                status = getattr(inventory, "query_status", {}).get(pid)
                record.update(status or {"generation_source": "unknown"})
                selected.append(record)
            walk[2] = inventory[pid]["parent"]
    depth_truncated = any(pid in inventory and pid not in chain for _, inventory, pid, chain in walks)
    return {"rejected_count": len(remaining), "rejected_pids": rejected,
            "omitted_pids": len(remaining) - len(rejected),
            "records": selected[:DIAGNOSTIC_RECORDS],
            "omitted_records": max(0, len(selected) - DIAGNOSTIC_RECORDS),
            "strings_truncated": strings_truncated, "depth_truncated": depth_truncated,
            "query_diagnostics_unavailable": any(getattr(s, "diagnostics_unavailable", False)
                                                 for s in (baseline, current))}


def _guardian_diagnostics(stage_error, cleanup_errors, lifecycle, owner):
    return {"schema_version": 1,
            "stage_error": _diagnostic_error(*stage_error) if stage_error else None,
            "cleanup_errors": [_diagnostic_error(*error) for error in cleanup_errors],
            "child": lifecycle,
            "ancestry": getattr(owner, "diagnostics", None)}


def _guardian_receipt(base, stage_error, cleanup_errors, lifecycle, owner):
    """Project evidence best-effort; legacy authority survives diagnostic failures."""
    try:
        diagnostics = _guardian_diagnostics(stage_error, cleanup_errors, lifecycle, owner)
        result = {**base, "diagnostics": diagnostics}
        # atomic_json uses this pretty ASCII encoding; stdout is no larger.
        def size():
            return len((json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False) + "\n").encode("ascii"))
        ancestry = diagnostics.get("ancestry")
        while size() > GUARDIAN_RECEIPT_BYTES and ancestry and ancestry.get("records"):
            ancestry["records"].pop()
            ancestry["omitted_records"] += 1
            diagnostics["truncated"] = True
        if size() > GUARDIAN_RECEIPT_BYTES:
            # Retain causal and lifecycle evidence ahead of ancillary ancestry.
            diagnostics["ancestry"] = {"unavailable": True, "reason": "receipt_budget"}
            diagnostics["truncated"] = True
        if size() > GUARDIAN_RECEIPT_BYTES:
            result["diagnostics"] = {"schema_version": 1, "unavailable": True, "reason": "receipt_budget"}
        if size() > GUARDIAN_RECEIPT_BYTES:
            raise ValueError("legacy receipt exceeds diagnostic envelope")
        return result
    except Exception:
        # The bounded legacy error is at most 512 characters (including Unicode).
        return {**base, "diagnostics": {"schema_version": 1, "unavailable": True,
                                        "reason": "projection_failed"}}


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


class _DarwinBSDInfo(ctypes.Structure):
    # Apple proc_info.h: struct proc_bsdinfo (136 bytes on supported Darwin ABIs).
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("seconds", ctypes.c_uint64), ("micros", ctypes.c_uint64)]


class _DarwinUniqueInfo(ctypes.Structure):
    # proc_info_private.h: the 56-byte proc_uniqidentifierinfo layout.
    _fields_ = [("uuid", ctypes.c_ubyte * 16), ("uniqueid", ctypes.c_uint64),
                ("parent_uniqueid", ctypes.c_uint64), ("version", ctypes.c_uint32),
                ("parent_version", ctypes.c_uint32), ("reserved2", ctypes.c_uint64),
                ("reserved3", ctypes.c_uint64)]


class _DarwinInventoryInfo(ctypes.Structure):
    _fields_ = [("bsd", _DarwinBSDInfo), ("lineage", _DarwinUniqueInfo)]


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
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            library.proc_pidinfo.restype = ctypes.c_int
            library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            library.proc_pidpath.restype = ctypes.c_int
            info, image = _DarwinBSDInfo(), ctypes.create_string_buffer(4096)
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

    def lineage(self, pid):
        """Darwin libproc lifetime IDs and original-parent exec version.

        proc_info.h: PROC_PIDUNIQIDENTIFIERINFO (17), 56-byte ABI. This private
        flavor is optional: an unavailable/short record cannot prove ancestry.
        """
        if sys.platform != "darwin":
            return None
        return self._darwin_lineage_record(pid)

    def _darwin_lineage_record(self, pid, *, include_terminal=False):
        # Flavor 17 is available without the same-user permission required by
        # flavor 18. Keep this independent evidence when the combined query is
        # unavailable. A nonzero argument also permits querying zombie records.
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        library.proc_pidinfo.restype = ctypes.c_int
        info = ctypes.create_string_buffer(56)
        ctypes.set_errno(0)
        size = library.proc_pidinfo(pid, 17, int(include_terminal), info, len(info))
        if size != len(info):
            raise OSError(ctypes.get_errno() or errno.EIO, "cannot verify process birth lineage")
        uniqueid = ctypes.c_uint64.from_buffer(info, 16).value
        parent_uniqueid = ctypes.c_uint64.from_buffer(info, 24).value
        if not uniqueid or uniqueid == parent_uniqueid:
            raise ValueError("invalid native process lineage")
        # p_puniqueid survives reparenting, but exec's replacement proc may
        # acquire its adoptive parent's ID. p_orig_ppidversion is copied across
        # exec and must match a sampled version of that parent before we trust
        # the edge. Older kernels reserve this field as zero: no new proof.
        return {"uniqueid": uniqueid, "parent_uniqueid": parent_uniqueid,
                "version": ctypes.c_uint32.from_buffer(info, 32).value,
                "parent_version": ctypes.c_uint32.from_buffer(info, 36).value}

    @staticmethod
    def _darwin_kinfo_supported():
        if sys.platform != "darwin" or sys.byteorder != "little" or ctypes.sizeof(ctypes.c_void_p) != 8:
            return False
        if os.uname().machine != "arm64":
            return False
        api, pid = Posix(), os.getpid()
        before = api._darwin_inventory_record(pid)
        observed = api._darwin_kinfo_snapshot(pid=pid)[pid]
        after = api._darwin_inventory_record(pid)
        if (any(before[key] != observed[key] or after[key] != observed[key]
                for key in ("created", "parent", "status"))
                or any(before[key] != after[key]
                       for key in ("uniqueid", "parent_uniqueid", "version", "parent_version"))):
            raise OSError("native process ABI cannot be verified")
        # This validates the observed fixed layout, not future kernel semantics
        # or complete fork/exit history. Attempted validation failures propagate.
        return True

    @staticmethod
    def _darwin_kinfo_decode(data):
        size = DARWIN_KINFO_LAYOUT["record_size"]
        if not data or len(data) > DARWIN_INVENTORY_BYTES or len(data) % size:
            raise ValueError("invalid native process table length")
        result, seen = _ProcessInventory(), set()
        view = memoryview(data)
        for start in range(0, len(data), size):
            row = {name: int.from_bytes(view[start + field["offset"]:start + field["offset"] + field["width"]],
                                        "little", signed=field["signed"])
                   for name, field in DARWIN_KINFO_LAYOUT["fields"].items()}
            pid, parent = row["pid"], row["parent"]
            if pid < 0 or pid in seen:
                raise ValueError("duplicate or invalid native process PID")
            seen.add(pid)
            if pid == 0:
                continue  # Apple ps excludes the kernel record; positive PIDs never get this exemption.
            created = row["start_seconds"] * 1000000 + row["start_micros"]
            if (parent < 0 or parent == pid or row["status"] not in (1, 2, 3, 4, 5, 6)
                    or row["start_seconds"] <= 0 or not 0 <= row["start_micros"] < 1000000
                    or not 0 < created < 2 ** 64):
                raise ValueError("invalid native process record")
            result[pid] = {"created": created, "parent": parent, "status": row["status"]}
        return result

    def _darwin_kinfo_snapshot(self, *, pid=None):
        if pid is not None and (type(pid) is not int or not 0 < pid < 2 ** 31):
            raise ValueError("invalid native process selection")
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        library.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        library.sysctl.restype = ctypes.c_int
        # CTL_KERN, KERN_PROC: the same caller-ABI record writer serves ALL/PID.
        mib = (ctypes.c_int * 4)(1, 14, 0 if pid is None else 1, 0 if pid is None else pid)
        started, next_capacity, attempts = time.monotonic(), 0, []
        def deadline():
            if time.monotonic() - started >= DARWIN_INVENTORY_SECONDS:
                raise TimeoutError("native process enumeration deadline")
        for _ in range(DARWIN_INVENTORY_ATTEMPTS):
            deadline()
            before, needed = time.monotonic(), ctypes.c_size_t()
            ctypes.set_errno(0)
            if library.sysctl(mib, 4, None, ctypes.byref(needed), None, 0) != 0:
                raise OSError(ctypes.get_errno() or errno.EIO, "cannot size native process table")
            deadline()
            if not DARWIN_KINFO_LAYOUT["record_size"] <= needed.value <= DARWIN_INVENTORY_BYTES:
                raise ValueError("native process table exceeds bounds")
            capacity = max(next_capacity, min(DARWIN_INVENTORY_BYTES,
                           needed.value + max(needed.value // 4, DARWIN_KINFO_LAYOUT["record_size"] * 16)))
            data, returned = ctypes.create_string_buffer(capacity), ctypes.c_size_t(capacity)
            ctypes.set_errno(0)
            failed = library.sysctl(mib, 4, data, ctypes.byref(returned), None, 0) != 0
            error = (ctypes.get_errno() or errno.EIO) if failed else 0
            attempts.append({"capacity": capacity, "returned_bytes": returned.value, "errno": error,
                             "elapsed_ns": max(0, int((time.monotonic() - before) * 1000000000))})
            deadline()
            if failed:
                if error == errno.ENOMEM and capacity < DARWIN_INVENTORY_BYTES:
                    next_capacity = min(DARWIN_INVENTORY_BYTES, capacity * 2)
                    continue  # Never decode failed/partial fills.
                raise OSError(error, "cannot fill native process table")
            if returned.value > capacity:
                raise ValueError("native process table overran capacity")
            # A size-only query includes spare capacity. Only copied fill bytes
            # can establish the stride of a selected process record.
            if pid is not None and returned.value != DARWIN_KINFO_LAYOUT["record_size"]:
                raise ValueError("native selected record size does not match")
            result = self._darwin_kinfo_decode(data.raw[:returned.value])
            if pid is not None and set(result) != {pid}:
                raise ValueError("native selected record PID does not match")
            deadline()
            result.enumeration = {"total_records": returned.value // DARWIN_KINFO_LAYOUT["record_size"],
                                  "attempts": attempts,
                                  "limits": {"max_bytes": DARWIN_INVENTORY_BYTES,
                                             "max_attempts": DARWIN_INVENTORY_ATTEMPTS,
                                             "deadline_ns": DARWIN_INVENTORY_SECONDS * 1000000000}}
            return result
        raise OSError(errno.ENOMEM, "native process enumeration exhausted")

    def _darwin_enriched_inventory(self):
        result, pending = self._darwin_kinfo_snapshot(), {}
        fields = ("uniqueid", "parent_uniqueid", "version", "parent_version")
        for pid, row in result.items():
            query_error, lineage_error = None, None
            try:
                native = self._darwin_inventory_record(pid)
            except (OSError, ValueError) as exc:
                query_error = exc
                try:
                    pending[pid] = self._darwin_lineage_record(pid, include_terminal=True)
                except (OSError, ValueError) as exc:
                    lineage_error = exc
            else:
                if native["created"] != row["created"]:
                    raise OSError("native inventory generation conflict")
                row.update({key: native[key] for key in fields})
            result.query_status[pid] = {"generation_source": "native",
                                       "query_error": _diagnostic_error("inventory", query_error) if query_error else None,
                                       "lineage_error": _diagnostic_error("inventory", lineage_error) if lineage_error else None}
        # A lineage-only result has no generation. Bracket all such queries with
        # native generation observations, using one follow-up table for the batch.
        binding = {"queried": len(pending), "attached": 0, "unbound": 0, "followup": None}
        if pending:
            followup = self._darwin_kinfo_snapshot()
            binding["followup"] = followup.enumeration
            for pid, lineage in pending.items():
                if pid in followup and followup[pid]["created"] != result[pid]["created"]:
                    raise OSError("native lineage generation conflict")
                if pid in followup:
                    result[pid].update(lineage)
                    binding["attached"] += 1
                else:
                    binding["unbound"] += 1
                    result.query_status[pid]["lineage_error"] = _diagnostic_error(
                        "inventory", ProcessLookupError(errno.ESRCH, "unbound native lineage"))
        result.lineage_binding = binding
        return result

    def _darwin_inventory_record(self, pid):
        """One native lifetime/status/parent snapshot, including terminal processes.

        Apple proc_info_private.h and kern/proc_info.c: flavor 18 combines BSD
        and unique-ID records from the same referenced process; arg=1 includes
        zombies. Inventory needs no executable path and must retain terminal
        ancestry: an exited parent may have surviving escaped descendants.
        """
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        library.proc_pidinfo.restype = ctypes.c_int
        info = _DarwinInventoryInfo()
        ctypes.set_errno(0)
        size = library.proc_pidinfo(pid, 18, 1, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info):
            raise OSError(ctypes.get_errno() or errno.EIO, "cannot read native process inventory")
        bsd, lineage = info.bsd, info.lineage
        if (bsd.pid != pid or bsd.ppid == pid or bsd.status not in (1, 2, 3, 4, 5, 6)
                or not bsd.seconds or bsd.micros >= 1000000 or bsd.seconds * 1000000 + bsd.micros >= 2 ** 64
                or not lineage.uniqueid or lineage.uniqueid == lineage.parent_uniqueid):
            raise ValueError("invalid native process inventory")
        return {"created": bsd.seconds * 1000000 + bsd.micros, "parent": bsd.ppid,
                "status": bsd.status, "uniqueid": lineage.uniqueid,
                "parent_uniqueid": lineage.parent_uniqueid, "version": lineage.version,
                "parent_version": lineage.parent_version}

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
        self.diagnostics = {}

    def run(self, argv, cwd, log, timeout, tick):
        if self.cleanup_unconfirmed:
            raise OSError("previous guardian cleanup is unconfirmed")
        self.result, self.early_reply = None, None
        receipt = Path(log).with_name(Path(log).name + ".cleanup-" + uuid.uuid4().hex + ".json")
        self.diagnostics = {"receipt": receipt.name, "status": "spawning"}
        try:
            self.proc = subprocess.Popen([sys.executable, "-I", "-S", "-B", __file__, "--guardian"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True, pass_fds=(self.lock.handle.fileno(),), text=True)
        except Exception as exc:
            self.diagnostics.update(status="spawn_failed", failure=_diagnostic_error("spawn", exc))
            raise
        self.diagnostics["status"] = "pending"
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
            site = "receipt_wait"
            try:
                if not self.proc.stdin.closed:
                    try:
                        self.proc.stdin.close()  # EOF is also sent on supervisor death.
                    except BrokenPipeError:
                        pass
                returned = self.proc.wait(timeout=15)
                if type(returned) is int:
                    self.diagnostics["guardian_exit_code"] = returned
                site = "receipt_read"
                raw = self.proc.stdout.readline(8192) if self.early_reply is None else None
                site = "receipt_parse"
                reply = self.early_reply if self.early_reply is not None else json.loads(raw)
                try:
                    self.diagnostics.update(status="received", reply=cleanup_diagnostic_projection(reply))
                except Exception:
                    self.diagnostics.update(status="received", projection_unavailable=True)
                site = "receipt_validate"
                if not isinstance(reply, dict) or reply.get("cleanup_confirmed") is not True or not isinstance(reply.get("exit_code"), int):
                    raise OSError("guardian did not confirm stage cleanup")
                self.result = reply
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                self.cleanup_unconfirmed = True
                self.diagnostics.update(status="unconfirmed", failure=_diagnostic_error(site, exc))
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
            for attempt in range(3):
                try:
                    self.baseline = self.inventory()
                    break
                except OSError as exc:
                    if "generation conflict" not in str(exc) or attempt == 2:
                        raise
                    time.sleep(0.02)

    @staticmethod
    def inventory():
        # Inventory includes unrelated processes, deliberately. A new process whose
        # ancestry was lost cannot safely be declared unrelated or killed. ps start
        # times plus native generations prevent PID reuse from masking that gap.
        api = Posix()
        if api._darwin_kinfo_supported():
            # A recognized ABI must fail closed on enumeration/decoding errors.
            return api._darwin_enriched_inventory()
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
        result = _ProcessInventory()
        for row in rows:
            pid_text, parent, stamp = row.strip().split(None, 2)
            pid = int(pid_text)
            if pid == proc.pid:
                continue  # This waitable inventory child is already reaped.
            source, query_error = "native", None
            lineage_error = None
            try:
                if sys.platform == "darwin":
                    result[pid] = api._darwin_inventory_record(pid)
                else:
                    result[pid] = {"created": api.process(pid)["created"], "parent": int(parent)}
            except (OSError, ValueError) as exc:
                # A disappearing new parent may already have orphaned a child
                # after ps enumerated it. Keep that uncertainty instead of hiding
                # the fork/exit race behind an empty final snapshot.
                result[pid] = {"created": stamp, "parent": int(parent)}
                source, query_error = "ps_fallback", exc
            if sys.platform != "darwin" or query_error is not None:
                try:
                    lineage = (api._darwin_lineage_record(pid, include_terminal=True) if sys.platform == "darwin"
                               else api.lineage(pid))
                    if lineage is not None:
                        result[pid].update(lineage)
                except (OSError, ValueError) as exc:
                    lineage_error = exc
            try:
                result.query_status[pid] = {"generation_source": source,
                                            "query_error": _diagnostic_error("inventory", query_error)
                                             if query_error else None,
                                             "lineage_error": _diagnostic_error("inventory", lineage_error)
                                             if lineage_error else None}
            except Exception:
                result.diagnostics_unavailable = True
        return result

    def _unconfirmed_descendants(self, current):
        def lineage(row):
            return (isinstance(row, dict) and type(row.get("uniqueid")) is int
                    and 0 < row["uniqueid"] < 2 ** 64 and type(row.get("parent_uniqueid")) is int
                    and 0 <= row["parent_uniqueid"] < 2 ** 64 and row["uniqueid"] != row["parent_uniqueid"])
        def unchanged(pid):
            if lineage(current.get(pid)) and lineage(self.baseline.get(pid)):
                return current[pid]["uniqueid"] == self.baseline[pid]["uniqueid"]
            if sys.platform == "darwin" and not all(
                    type(snapshot.get(pid, {}).get("created")) is int and 0 < snapshot[pid]["created"] < 2 ** 64
                    for snapshot in (current, self.baseline)):
                return False  # Coarse ps timestamps cannot prove a Darwin lifetime.
            return pid in current and pid in self.baseline and (
                current[pid]["created"] == self.baseline[pid]["created"])
        def outside_baseline(pid):
            seen = set()
            while pid in self.baseline and pid not in seen:
                if pid == os.getpid():
                    return False
                if pid == 1:
                    return True
                seen.add(pid)
                pid = self.baseline[pid]["parent"]
            return False
        # A birth edge to launchd is proof; an adoptive PPID of 1 is not.
        root = self.baseline.get(os.getpid())
        foreign_ids = {row["uniqueid"] for pid, row in self.baseline.items()
                       if lineage(row) and outside_baseline(pid)} if lineage(root) else set()
        edges, versions, conflicts, possible_owned = {}, {}, set(), set()
        root_sample = root if lineage(root) else current.get(os.getpid())
        if lineage(root_sample):
            possible_owned.add(root_sample["uniqueid"])
        raw_edges = []
        for snapshot in (self.baseline, current):
            for pid, row in snapshot.items():
                if lineage(row):
                    raw_edges.append((row["uniqueid"], row["parent_uniqueid"]))
                    version, parent_version = row.get("version"), row.get("parent_version")
                    if type(version) is int and 0 < version < 2 ** 32:
                        versions.setdefault(row["uniqueid"], set()).add(version)
                    if type(parent_version) is not int or not 0 < parent_version < 2 ** 32:
                        continue
                    edge = (pid, row["parent_uniqueid"], parent_version)
                    previous = edges.get(row["uniqueid"])
                    if previous and (previous[0] != pid or previous[2] != parent_version):
                        conflicts.add(row["uniqueid"])
                    edges[row["uniqueid"]] = edge
        # Native evidence of possible ownership vetoes the PPID fallback
        # even when an old kernel/query cannot provide a parent version.
        while True:
            children = {child for child, parent in raw_edges if parent in possible_owned}
            if children <= possible_owned:
                break
            possible_owned.update(children)
        for uniqueid in conflicts:
            edges.pop(uniqueid, None)
            foreign_ids.discard(uniqueid)
        def birth_foreign(pid):
            row = current.get(pid)
            if not lineage(row):
                return None
            if row["uniqueid"] in possible_owned:
                return False
            parent = current.get(row.get("parent")) if type(row.get("parent")) is int else None
            contradiction = lineage(parent) and row["parent_uniqueid"] != parent["uniqueid"]
            if not foreign_ids:
                return False if contradiction else None
            uniqueid, seen = row["uniqueid"], set()
            if uniqueid in conflicts:
                return False
            if uniqueid in foreign_ids:
                return True  # This lifetime already existed outside the guardian.
            if uniqueid not in edges:
                return False if contradiction else None
            while uniqueid not in seen:
                if uniqueid == root["uniqueid"]:
                    return False
                if uniqueid in foreign_ids:
                    return True
                if uniqueid not in edges:
                    return False
                seen.add(uniqueid)
                _, parent, parent_version = edges[uniqueid]
                if parent == root["uniqueid"]:
                    return False
                if parent_version not in versions.get(parent, set()):
                    return False  # An adoptive parent after exec is not birth proof.
                uniqueid = parent
            return False
        def foreign(pid):
            proof = birth_foreign(pid)
            if proof is not None:
                return proof
            seen = set()
            while pid in current and pid not in seen:
                if pid in {0, 1, os.getpid()}:
                    return False  # An adoptive root is never fallback foreign proof.
                proof = birth_foreign(pid)
                if proof is not None:
                    return proof
                # A surviving parent that predated our stage and is outside
                # our guardian proves foreign ancestry. PID 1 is excluded:
                # escaped descendants may already have been reparented to it.
                if unchanged(pid):
                    return True
                seen.add(pid)
                pid = current[pid]["parent"]
            return False
        return [pid for pid in current if pid != os.getpid() and not unchanged(pid) and not foreign(pid)]

    def cleanup(self):
        if not self.subreaper:
            previous = None
            for attempt in range(4):
                try:
                    current = self.inventory()
                except OSError as exc:
                    if "generation conflict" not in str(exc) or attempt == 3:
                        raise
                    time.sleep(0.05)
                    continue
                remaining = self._unconfirmed_descendants(current)
                if not remaining:
                    return
                if attempt == 3 or current is previous:
                    try:
                        self.diagnostics = _ancestry_diagnostics(self.baseline, current, remaining)
                    except Exception:
                        self.diagnostics = {"unavailable": True, "reason": "projection_failed"}
                    raise OSError("descendant ancestry cannot be confirmed: " + repr(sorted(remaining)))
                previous = current
                time.sleep(0.05)
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
    stage_error, cleanup_errors, site = None, [], "owner_init"
    lifecycle = {"registered_child": None, "release_requested": False,
                 "exit_observed": False, "signals_attempted": [],
                 "child_returncode": None, "reap_outcome": "no_child", "synthetic_exit": False,
                 "group_probe": {"status": "not_attempted"}}

    def observed_exit():
        exited = monitor.exited()
        lifecycle["exit_observed"] |= exited
        return exited

    def cleanup_failed(where, exc):
        nonlocal cleanup_error
        cleanup_error = str(exc)[:512]
        cleanup_errors.append((where, exc))

    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        owner = DescendantOwner()
        site = "spawn"
        with Path(request["log"]).open("ab", buffering=0) as log:
            child = subprocess.Popen([sys.executable, "-I", "-S", "-B", __file__, "--exec-stage",
                                      json.dumps(request["argv"])], cwd=request["cwd"],
                                     stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True)
            lifecycle["reap_outcome"] = "not_attempted"
            site = "register"
            monitor = ChildExit(child.pid)
            identity = Posix().process(child.pid)
            lifecycle["registered_child"] = {"pid": identity["pid"], "created": identity["created"]}
            print(json.dumps(identity), flush=True)
            with selectors.DefaultSelector() as selector:
                selector.register(sys.stdin, selectors.EVENT_READ)
                if not selector.select(HANDSHAKE_SECONDS) or sys.stdin.readline().strip() != "go":
                    raise OSError("supervisor disappeared before stage registration")
                site = "release"
                child.stdin.write(b"1")
                lifecycle["release_requested"] = True
                child.stdin.close()
                site = "wait"
                deadline = time.monotonic() + request["timeout"]
                while not observed_exit():
                    if selector.select(0.05):
                        if not sys.stdin.readline():
                            raise OSError("supervisor control pipe closed")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("stage deadline")
    except Exception as exc:
        failure = str(exc)[:512]
        stage_error = (site, exc)
    finally:
        if child:
            try:
                running = not monitor or not observed_exit()
            except (OSError, ValueError) as exc:
                # Missing observation is not evidence of exit. Still attempt both
                # group signals and a bounded reap while the child pins its PGID.
                running = True
                cleanup_failed("exit_observation", exc)
            if running:
                try:
                    lifecycle["signals_attempted"].append("SIGTERM")
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    cleanup_failed("signal_term", exc)
                try:
                    until = time.monotonic() + 2
                    while monitor and not observed_exit() and time.monotonic() < until:
                        time.sleep(0.05)
                except (OSError, ValueError) as exc:
                    cleanup_failed("exit_observation", exc)
            # Reap ONLY after the last group signal. Holding a waitable child pins
            # the group leader's PID even when it already exited successfully.
            # Only EPERM is deferred: Darwin reports it for a group holding just the
            # already-exited, unreaped leader. It is discarded below solely when exit
            # was observed before this signal, the reap succeeded, and a signal-0
            # probe proves the group gone. Any other outcome keeps the failure.
            kill_denied = None
            exit_before_kill = bool(lifecycle["exit_observed"])
            try:
                lifecycle["signals_attempted"].append("SIGKILL")
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                # EACCES and every other errno are plain failures, never deferred.
                if exc.errno == errno.EPERM:
                    kill_denied = exc
                else:
                    cleanup_failed("signal_kill", exc)
            lifecycle["reap_outcome"] = "pending"
            reap_error = None
            try:
                code = child.wait(timeout=5)
                lifecycle.update(child_returncode=code, reap_outcome="reaped")
            except (OSError, subprocess.TimeoutExpired) as exc:
                reap_error = exc
                lifecycle["reap_outcome"] = "failed"
            if kill_denied is not None:
                group_absent = False
                if reap_error is None and exit_before_kill:
                    try:
                        os.killpg(child.pid, 0)  # signal 0: never destructive
                        lifecycle["group_probe"] = {"status": "present"}
                    except ProcessLookupError:
                        group_absent = True
                        lifecycle["group_probe"] = {"status": "absent"}
                    except OSError as exc:
                        lifecycle["group_probe"] = {"status": "error", "error": _diagnostic_error("signal_probe", exc)}
                if not group_absent:
                    cleanup_failed("signal_kill", kill_denied)
            if reap_error is not None:
                cleanup_failed("child_reap", reap_error)
        if owner:
            try:
                owner.cleanup()
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                cleanup_failed("owner_cleanup", exc)
        if monitor:
            try:
                monitor.close()
            except (OSError, ValueError) as exc:
                cleanup_failed("monitor_close", exc)
    lifecycle["synthetic_exit"] = failure is not None or lifecycle["child_returncode"] is None
    result = _guardian_receipt({"cleanup_confirmed": cleanup_error is None,
                               "exit_code": code if failure is None else 1, "error": cleanup_error or failure},
                              stage_error, cleanup_errors, lifecycle, owner)
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
        try:
            diagnostic = getattr(job, "diagnostics", None)
            if isinstance(diagnostic, dict) and diagnostic:
                state["cleanup_diagnostics"] = diagnostic
        except Exception:
            state["cleanup_diagnostics"] = {"unavailable": True}
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
