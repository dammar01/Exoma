"""Investigate Darwin enumeration on a native runner; never authorize cleanup.

    python tools/maintain/darwin_inventory_probe.py run --report <path>
    python tools/maintain/darwin_inventory_probe.py validate <path> --commit <sha>

The C helper is an independent SDK oracle for the standalone production reader.
No compiler dependency, process-table dump or package update is installed by this tool.
"""

import argparse
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import select
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[2]
KIND = "exoma-darwin-inventory-capability"
NATIVE_KIND = "exoma-darwin-inventory-native"
PRODUCTION_KIND = "exoma-darwin-inventory-production"
SCHEMA = 2
MAX_BYTES = 16 * 1024 * 1024
MAX_ATTEMPTS = 4
DEADLINE_NS = 5_000_000_000
MAX_REPORT_BYTES = 256 * 1024
FIELDS = ("pid", "parent", "status", "start_seconds", "start_micros")
NATIVE_FIELDS = ("created", "parent", "status", "uniqueid", "parent_uniqueid", "version", "parent_version")
SOURCES = ("tools/maintain/darwin_inventory_probe.c",
           "tools/maintain/darwin_inventory_probe.py", "src/exoma/_update_worker.py")
HOLD = "import signal,sys; signal.alarm(20); sys.stdin.buffer.read(1)"
PARENT = (
    "import os,signal,subprocess,sys\n"
    "signal.alarm(20)\n"
    "child=subprocess.Popen([sys.executable,'-I','-u','-c'," + repr(HOLD) + "],"
    "stdin=int(sys.argv[1]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
    "start_new_session=True,close_fds=True)\n"
    "os.close(int(sys.argv[1]))\n"
    "print(child.pid,flush=True)\n"
    "sys.stdin.buffer.read(1)\n"
)


class ProbeError(Exception):
    def __init__(self, site, code=None):
        self.site, self.code = site, code


def _int(value, low=0, high=2 ** 63 - 1):
    return type(value) is int and low <= value <= high


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and "\n" not in value


def _layout_valid(layout):
    if not isinstance(layout, dict):
        return False
    size, alignment = layout.get("record_size"), layout.get("alignment")
    if (layout.get("architecture") not in ("arm64", "x86_64")
            or not _int(size, 1, 65536) or not _int(alignment, 1, 16)
            or alignment & (alignment - 1) or size % alignment
            or layout.get("pointer_width") != 8 or layout.get("endianness") != "little"):
        return False
    fields = layout.get("fields")
    if not isinstance(fields, dict) or set(fields) != set(FIELDS):
        return False
    occupied = set()
    for field in fields.values():
        if not isinstance(field, dict):
            return False
        offset, width = field.get("offset"), field.get("width")
        if (not _int(offset, 0, size - 1) or type(width) is not int or width not in (1, 2, 4, 8)
                or offset + width > size or offset % width or type(field.get("signed")) is not bool):
            return False
        span = set(range(offset, offset + width))
        if span & occupied:
            return False
        occupied.update(span)
    return True


def _row_valid(row):
    return (isinstance(row, dict) and set(row) == set(FIELDS)
            and _int(row["pid"], 1, 2 ** 31 - 1) and _int(row["parent"], 0, 2 ** 31 - 1)
            and row["parent"] != row["pid"] and _int(row["status"], 1, 5)
            and _int(row["start_seconds"], 1, (2 ** 63 - 1) // 1_000_000)
            and _int(row["start_micros"], 0, 999999))


def validate_snapshot(value, selected, layout):
    """A whole returned buffer is transport evidence, never lifetime closure."""
    if not isinstance(value, dict) or not _layout_valid(layout):
        return ["invalid native snapshot/layout"]
    if (value.get("kind") != NATIVE_KIND or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("ok") is not True
            or value.get("transport_complete") is not True or "error" not in value or value["error"] is not None
            or value.get("layout") != layout):
        return ["native snapshot did not qualify"]
    limits = value.get("limits")
    if limits != {"max_bytes": MAX_BYTES, "max_attempts": MAX_ATTEMPTS, "deadline_ns": DEADLINE_NS}:
        return ["native bounds do not match"]
    attempts = value.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= MAX_ATTEMPTS:
        return ["invalid native attempts"]
    for index, item in enumerate(attempts):
        expected_errno = 0 if index == len(attempts) - 1 else errno.ENOMEM
        if (not isinstance(item, dict) or not _int(item.get("capacity"), 1, MAX_BYTES)
                or not _int(item.get("returned_bytes")) or type(item.get("errno")) is not int
                or item["errno"] != expected_errno or not _int(item.get("elapsed_ns"), 0, DEADLINE_NS)):
            return ["invalid native attempt evidence"]
        if index and item["capacity"] < min(MAX_BYTES, attempts[index - 1]["capacity"] * 2):
            return ["allocation did not grow after ENOMEM"]
    if sum(item["elapsed_ns"] for item in attempts) > DEADLINE_NS:
        return ["native deadline exceeded"]
    last, size = attempts[-1], layout["record_size"]
    if (not _int(value.get("total_records"), 1, MAX_BYTES // size)
            or last["returned_bytes"] > last["capacity"]
            or last["returned_bytes"] != value["total_records"] * size):
        return ["partial or malformed native buffer"]
    rows = value.get("records")
    if (not isinstance(rows, list) or len(rows) != len(selected) or len(rows) > 8
            or any(not _row_valid(row) for row in rows)
            or {row["pid"] for row in rows} != set(selected)):
        return ["missing, duplicate or malformed fixture records"]
    return []


def _native_record(value, pid):
    if not isinstance(value, dict) or value.get("ok") is not True:
        return None
    row = value.get("record")
    if (not isinstance(row, dict) or set(row) != set(NATIVE_FIELDS) or not _int(row.get("created"), 1)
            or not _int(row.get("uniqueid"), 1, 2 ** 64 - 1)
            or not _int(row.get("parent_uniqueid"), 0, 2 ** 64 - 1)
            or row["uniqueid"] == row["parent_uniqueid"]
            or not _int(row.get("parent"), 0, 2 ** 31 - 1)
            or row["parent"] == pid
            or not _int(row.get("status"), 1, 5)
            or not _int(row.get("version"), 0, 2 ** 32 - 1)
            or not _int(row.get("parent_version"), 0, 2 ** 32 - 1)
            or not _int(pid, 1, 2 ** 31 - 1)):
        return None
    return row


def _matches(snapshot, pid, native):
    row = next(row for row in snapshot["records"] if row["pid"] == pid)
    return (row["start_seconds"] * 1_000_000 + row["start_micros"] == native["created"]
            and row["parent"] == native["parent"])


def _missing(value):
    return (isinstance(value, dict) and value.get("ok") is False
            and isinstance(value.get("error"), dict)
            and value["error"].get("type") in ("ProcessLookupError", "OSError")
            and type(value["error"].get("errno")) is int and value["error"]["errno"] == errno.ESRCH)


def _validate_production(value, selected, layout):
    if not isinstance(value, dict) or value.get("kind") != PRODUCTION_KIND:
        return ["production enumeration missing"]
    problems = validate_snapshot({**value, "kind": NATIVE_KIND}, selected, layout)
    if problems:
        return problems
    queries = value.get("queries")
    if (not isinstance(queries, dict) or set(queries) != {str(pid) for pid in selected}
            or any(not isinstance(query, dict) or query.get("generation_source") != "native"
                   or not {"query_error", "lineage_error"} <= set(query) for query in queries.values())):
        return ["production lookup provenance missing"]
    binding = value.get("lineage_binding")
    if (not isinstance(binding, dict) or set(binding) != {"queried", "attached", "unbound", "followup"}
            or any(not _int(binding.get(key), 0, value["total_records"])
                   for key in ("queried", "attached", "unbound"))
            or binding["attached"] + binding["unbound"] != binding["queried"]):
        return ["production lineage binding evidence invalid"]
    if binding["queried"]:
        followup = binding["followup"]
        if not isinstance(followup, dict):
            return ["production binding follow-up missing"]
        problems.extend(validate_snapshot({**followup, "kind": NATIVE_KIND, "schema_version": 1,
                                           "ok": True, "transport_complete": True,
                                           "layout": layout, "error": None, "records": []}, [], layout))
    elif binding["followup"] is not None:
        problems.append("unexpected production binding follow-up")
    return problems


def validate_report(report, commit=None):
    if not isinstance(report, dict):
        return ["report is not an object"]
    problems = []
    if (report.get("kind") != KIND or type(report.get("schema_version")) is not int
            or report["schema_version"] != SCHEMA):
        problems.append("wrong capability kind/schema")
    sha = report.get("commit")
    if (not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None
            or commit is not None and sha != commit or report.get("commit_dirty") is not False):
        problems.append("commit must match a clean checkout")
    if (report.get("capability_verified") is not True or report.get("release_acceptance") is not False
            or report.get("global_atomic") is not False or report.get("cleanup_confirmed") is not True
            or report.get("errors") != []):
        problems.append("capability/cleanup evidence incomplete or acceptance overstated")
    environment = report.get("environment", {})
    if (not isinstance(environment, dict) or environment.get("platform") != "darwin"
            or environment.get("architecture") not in ("arm64", "x86_64")
            or any(not _text(environment.get(key)) for key in ("os_version", "os_build", "kernel", "python"))):
        problems.append("native environment identity missing")
    sdk = report.get("sdk", {})
    if not isinstance(sdk, dict) or any(not _text(sdk.get(key)) for key in ("version", "compiler")):
        problems.append("SDK/compiler identity missing")
    hashes = report.get("source_sha256")
    if (not isinstance(hashes, dict) or set(hashes) != set(SOURCES)
            or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
                   for value in hashes.values())):
        problems.append("probe/runtime source hashes missing")
    layout, pids = report.get("layout"), report.get("fixture_pids")
    if not _layout_valid(layout):
        problems.append("unvalidated SDK layout")
    elif isinstance(environment, dict) and layout["architecture"] != environment.get("architecture"):
        problems.append("helper/Python architectures differ")
    production = report.get("production")
    if (not isinstance(production, dict) or production.get("abi_verified") is not True
            or production.get("layout") != layout):
        problems.append("production/SDK ABI agreement missing")
    if (not isinstance(pids, dict) or set(pids) != {"self", "foreign", "parent", "escaped"}
            or any(not _int(pid, 1, 2 ** 31 - 1) for pid in pids.values())
            or len(set(pids.values())) != 4):
        problems.append("fixture identities missing")
    if problems:
        return problems
    cases = report.get("cases")
    if not isinstance(cases, dict) or set(cases) != {"live_identity", "retained_after_exit", "escaped_child"}:
        return ["capability cases missing"]
    selections = {"live_identity": [pids["self"], pids["foreign"]],
                  "retained_after_exit": [pids["foreign"]],
                  "escaped_child": [pids["parent"], pids["escaped"]]}
    for name, selected in selections.items():
        case = cases[name]
        if not isinstance(case, dict):
            return ["malformed capability case"]
        problems.extend(name + ": " + error for error in validate_snapshot(case.get("snapshot"), selected, layout))
        problems.extend(name + ": " + error for error in _validate_production(case.get("production"), selected, layout))
    if problems:
        return problems
    for name, selected in selections.items():
        oracle = {row["pid"]: row for row in cases[name]["snapshot"]["records"]}
        for row in cases[name]["production"]["records"]:
            if any(row[key] != oracle[row["pid"]][key] for key in ("parent", "start_seconds", "start_micros")):
                problems.append("production/SDK lifetime mismatch")
    live, retained, escaped = (cases[name] for name in selections)
    identities = live.get("native", {})
    for pid in selections["live_identity"]:
        native = _native_record(identities.get(str(pid)) if isinstance(identities, dict) else None, pid)
        if native is None or not _matches(live["snapshot"], pid, native):
            problems.append("live enumeration/native generation or parent mismatch")
    baseline = _native_record(retained.get("baseline"), pids["foreign"])
    after = retained.get("after", {})
    if (baseline is None or not _matches(retained["snapshot"], pids["foreign"], baseline)
            or retained.get("reaped") is not True or not isinstance(after, dict)
            or not all(_missing(after.get(key)) for key in ("inventory", "lineage"))
            or not all(_missing({"ok": False, "error": retained["production"]["queries"][str(pids["foreign"])][key]})
                       for key in ("query_error", "lineage_error"))):
        problems.append("retained generation after reap/ESRCH not established")
    lineage_probe = production.get("lineage_probe", {})
    if (not isinstance(lineage_probe, dict)
            or type(lineage_probe.get("forced_combined_denial")) is not int
            or lineage_probe["forced_combined_denial"] != pids["foreign"]):
        problems.append("forced lineage-only case missing")
    else:
        binding_snapshot = lineage_probe.get("snapshot")
        binding_errors = _validate_production(binding_snapshot, [pids["foreign"]], layout)
        problems.extend(binding_errors)
        bound = _native_record({"ok": True, "record": lineage_probe.get("record")}, pids["foreign"])
        if (bound is None or baseline is None or binding_errors
                or any(bound[key] != baseline[key] for key in NATIVE_FIELDS if key != "status")
                or bound["status"] == 5 or not _matches(binding_snapshot, pids["foreign"], bound)
                or not _int(binding_snapshot["lineage_binding"]["attached"], 1)
                or binding_snapshot["queries"][str(pids["foreign"])]["query_error"] != {
                    "site": "inventory", "type": "PermissionError", "errno": errno.EPERM}
                or binding_snapshot["queries"][str(pids["foreign"])]["lineage_error"] is not None):
            problems.append("lineage-only enrichment not bound to native lifetime")
    parent = _native_record(escaped.get("parent_baseline"), pids["parent"])
    child = _native_record(escaped.get("child_baseline"), pids["escaped"])
    survivor = _native_record(escaped.get("child_after"), pids["escaped"])
    parent_after = escaped.get("parent_after", {})
    if (parent is None or child is None or survivor is None
            or not _matches(escaped["snapshot"], pids["parent"], parent)
            or not _matches(escaped["snapshot"], pids["escaped"], child)
            or child["parent"] != pids["parent"] or child["created"] != survivor["created"]
            or child["uniqueid"] != survivor["uniqueid"] or survivor["status"] == 5
            or not _int(escaped.get("child_session"), 1, 2 ** 31 - 1)
            or escaped["child_session"] != pids["escaped"]
            or escaped.get("parent_reaped") is not True or not isinstance(parent_after, dict)
            or not all(_missing(parent_after.get(key)) for key in ("inventory", "lineage"))):
        problems.append("vanished parent/surviving escaped child not established")
    return problems


def _command(argv, site, timeout=10):
    result = subprocess.run([str(arg) for arg in argv], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        if site == "compile":
            # Diagnostics from our own source only; never persist process argv/env.
            print(result.stderr[:4096].replace(str(REPO), "<repo>"), file=sys.stderr)
        raise ProbeError(site, result.returncode)
    return result.stdout.strip()


def _read(api, pid, lineage=False):
    try:
        record = (api._darwin_lineage_record(pid, include_terminal=True) if lineage
                  else api._darwin_inventory_record(pid))
        return {"ok": True, "record": {key: value if type(value) is int else None
                                      for key, value in record.items() if key in NATIVE_FIELDS}}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": {"type": type(exc).__name__, "errno": getattr(exc, "errno", None)}}


def _after(api, pid):
    return {"inventory": _read(api, pid), "lineage": _read(api, pid, True)}


def _stop_direct(proc):
    if proc.stdin and not proc.stdin.closed:
        proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()  # Direct, unreaped child: Popen retains wait/identity ownership.
        proc.wait(timeout=5)
    for stream in (proc.stdout, proc.stderr):
        if stream and not stream.closed:
            stream.close()


def _escaped_stopped(api, pid, identity):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = _read(api, pid)
        if _missing(value):
            return True
        current = _native_record(value, pid)
        if current is not None and (current["status"] == 5 or identity is not None
                                   and current["created"] != identity["created"]):
            return True
        time.sleep(0.05)
    return False


def _production_snapshot(report, api, pids):
    observed = api._darwin_enriched_inventory()  # Decodes/validates the full returned table.
    rows = []
    for pid in pids:
        if pid not in observed:
            raise ProbeError("production_fixture_missing")
        row = observed[pid]
        rows.append({"pid": pid, "parent": row["parent"], "status": row["status"],
                     "start_seconds": row["created"] // 1000000, "start_micros": row["created"] % 1000000})
    return {**observed.enumeration, "kind": PRODUCTION_KIND, "schema_version": 1, "ok": True,
            "transport_complete": True, "layout": report["production"]["layout"], "error": None,
            "records": rows, "lineage_binding": observed.lineage_binding,
            "queries": {str(pid): observed.query_status[pid] for pid in pids}}, observed


def _collect(report, helper, api):
    direct, pipe_fds = [], []
    escaped_pid, escaped_identity = None, None
    parent_started = False
    def snapshot(pids):
        result = subprocess.run([str(helper), "snapshot", *(str(pid) for pid in pids)],
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        if len(result.stdout.encode("utf-8")) > 65536:
            raise ProbeError("enumeration_output_bounds")
        value = json.loads(result.stdout)
        if result.returncode or validate_snapshot(value, pids, report["layout"]):
            report.setdefault("enumeration_failures", []).append(value)
            raise ProbeError("enumeration_contract", result.returncode)
        return value
    try:
        foreign = subprocess.Popen([sys.executable, "-I", "-u", "-c", HOLD], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        direct.append(foreign)
        report["fixture_pids"] = {"self": os.getpid(), "foreign": foreign.pid}
        # Both processes are held alive while their two native observations are compared.
        live = snapshot([os.getpid(), foreign.pid])
        production_live, _ = _production_snapshot(report, api, [os.getpid(), foreign.pid])
        report["cases"]["live_identity"] = {"snapshot": live, "native": {
            str(pid): _read(api, pid) for pid in (os.getpid(), foreign.pid)}, "production": production_live}
        # Force only the owned fixture's combined query to fail. The actual
        # native lineage call and bracketing table reads still execute unchanged.
        class Restricted(type(api)):
            def _darwin_inventory_record(self, pid):
                if pid == foreign.pid:
                    raise PermissionError(errno.EPERM, "forced probe fixture denial")
                return super()._darwin_inventory_record(pid)
        binding_snapshot, binding_rows = _production_snapshot(report, Restricted(), [foreign.pid])
        report["production"]["lineage_probe"] = {
            "forced_combined_denial": foreign.pid, "snapshot": binding_snapshot,
            "record": {key: value for key, value in binding_rows[foreign.pid].items() if key in NATIVE_FIELDS}}
        baseline = _read(api, foreign.pid)
        retained = snapshot([foreign.pid])
        class Exiting(type(api)):
            def _darwin_inventory_record(self, pid):
                if pid == foreign.pid:
                    _stop_direct(foreign)  # After production capture, before either native enrichment call.
                return super()._darwin_inventory_record(pid)
        production_retained, _ = _production_snapshot(report, Exiting(), [foreign.pid])
        _stop_direct(foreign)
        report["cases"]["retained_after_exit"] = {
            "snapshot": retained, "baseline": baseline, "reaped": foreign.returncode == 0,
            "after": _after(api, foreign.pid), "production": production_retained}

        read_fd, write_fd = os.pipe()
        pipe_fds.extend((read_fd, write_fd))
        parent = subprocess.Popen([sys.executable, "-I", "-u", "-c", PARENT, str(read_fd)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  pass_fds=(read_fd,))
        direct.append(parent)
        parent_started = True
        os.close(read_fd)
        pipe_fds.remove(read_fd)
        if not select.select([parent.stdout], [], [], 5)[0]:
            raise ProbeError("fixture_start")
        line = parent.stdout.readline(64)
        if not line.endswith(b"\n") or not line.strip().isdigit():
            raise ProbeError("fixture_identity")
        escaped_pid = int(line)
        if not _int(escaped_pid, 1, 2 ** 31 - 1):
            raise ProbeError("fixture_identity")
        report["fixture_pids"].update(parent=parent.pid, escaped=escaped_pid)
        observations = snapshot([parent.pid, escaped_pid])
        production_escaped, _ = _production_snapshot(report, api, [parent.pid, escaped_pid])
        parent_native, child_native = _read(api, parent.pid), _read(api, escaped_pid)
        escaped_identity = _native_record(child_native, escaped_pid)
        child_session = os.getsid(escaped_pid)
        _stop_direct(parent)
        report["cases"]["escaped_child"] = {
            "snapshot": observations, "parent_baseline": parent_native, "child_baseline": child_native,
            "child_session": child_session, "parent_reaped": parent.returncode == 0,
            "parent_after": _after(api, parent.pid), "child_after": _read(api, escaped_pid),
            "production": production_escaped}
    finally:
        # Only the coordinator owns the escaped child's write end. Closing it
        # releases that child even if its parent has exited; alarms bound a hang.
        for fd in pipe_fds:
            os.close(fd)
        for proc in direct:
            _stop_direct(proc)
        stopped = (not parent_started or escaped_pid is not None
                   and _escaped_stopped(api, escaped_pid, escaped_identity))
        report["cleanup_confirmed"] = stopped
        if not stopped:
            report["errors"].append({"site": "fixture_cleanup", "type": "unconfirmed"})


def _checkout():
    sha = _command(["git", "-C", REPO, "rev-parse", "HEAD"], "checkout")
    dirty = bool(_command(["git", "-C", REPO, "status", "--porcelain"], "checkout"))
    return sha, dirty


def _source_hashes():
    return {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in SOURCES}


def _write(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if len(data.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ProbeError("report_bounds")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(data, encoding="utf-8")
    os.replace(temporary, path)


def _load_native():
    spec = importlib.util.spec_from_file_location("darwin_probe_identity", REPO / SOURCES[2])
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    return native


def run(report_path):
    report = {"kind": KIND, "schema_version": SCHEMA, "capability_verified": False,
              "release_acceptance": False, "global_atomic": False, "cleanup_confirmed": False,
              "errors": [], "cases": {}, "environment": {"platform": sys.platform,
                  "architecture": platform.machine(), "python": platform.python_version(),
                  "os_version": platform.mac_ver()[0], "kernel": platform.release()}}
    _write(report_path, report)  # A later failure must still leave an artifact.
    try:
        report["commit"], report["commit_dirty"] = _checkout()
        report["source_sha256"] = _source_hashes()
        if sys.platform != "darwin":
            raise ProbeError("unsupported_platform")
        if report["commit_dirty"]:
            raise ProbeError("dirty_checkout")
        report["environment"]["os_build"] = _command(["/usr/bin/sw_vers", "-buildVersion"], "environment")
        xcrun = ["/usr/bin/xcrun", "--sdk", "macosx"]
        report["sdk"] = {"version": _command([*xcrun, "--show-sdk-version"], "sdk"),
                         "compiler": _command([*xcrun, "clang", "--version"], "compiler").splitlines()[0]}
        with tempfile.TemporaryDirectory(prefix="exoma-darwin-capability-") as tmp:
            helper = Path(tmp) / "inventory"
            _command([*xcrun, "clang", "-std=c11", "-Wall", "-Wextra", "-Werror",
                      REPO / SOURCES[0], "-o", helper], "compile", timeout=45)
            report["layout"] = json.loads(_command([helper, "layout"], "layout")).get("layout")
            if not _layout_valid(report["layout"]):
                raise ProbeError("layout_contract")
            native = _load_native()
            report["production"] = {"layout": native.DARWIN_KINFO_LAYOUT,
                                    "abi_verified": native.Posix._darwin_kinfo_supported()}
            if not report["production"]["abi_verified"] or native.DARWIN_KINFO_LAYOUT != report["layout"]:
                raise ProbeError("production_abi_contract")
            _collect(report, helper, native.Posix())
        if (_checkout() != (report["commit"], False)
                or _source_hashes() != report["source_sha256"]):
            raise ProbeError("checkout_changed")
        report["capability_verified"] = True
    except Exception as exc:
        report["errors"].append({"site": exc.site if isinstance(exc, ProbeError) else "probe",
                                 "type": type(exc).__name__, "code": getattr(exc, "code", None),
                                 "errno": getattr(exc, "errno", None)})
    problems = validate_report(report, report.get("commit"))
    if problems:
        report["capability_verified"] = False
    report["validation_errors"] = problems
    _write(report_path, report)
    print("capability_verified=" + str(report["capability_verified"]).lower()
          + "; release_acceptance=false; report=" + str(report_path))
    return 0 if report["capability_verified"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--report", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("report")
    validate.add_argument("--commit", required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.report)
    try:
        path = Path(args.report)
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise ProbeError("report_bounds")
        problems = validate_report(json.loads(path.read_text(encoding="utf-8")), args.commit)
    except (OSError, ValueError, ProbeError):
        problems = ["missing, oversized or unreadable capability report"]
    for problem in problems:
        print("PROBLEM " + problem)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
