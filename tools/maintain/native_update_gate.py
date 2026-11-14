#!/usr/bin/env python3
"""Collect and validate native update-handoff receipts for a release candidate.

The strict artifact gate (`tests/run.py --update-artifacts handoff`) proves a real A-to-B
update on one host, one Python and one package manager. A release needs all three managers
under both supported Pythons on each native platform. This tool runs that matrix in one
command, writes uniquely named reports bound to the tested commit, and checks the result.

    python tools/maintain/native_update_gate.py matrix --python-311 <exe> --python-313 <exe>
    python tools/maintain/native_update_gate.py validate <dir> --commit <sha>

It does not authenticate the host: a receipt shows what the tested checkout reported about
itself, not who ran it. It also cannot enforce anything at tagging or publication; the
maintainer runs it before tagging (RELEASE.md). Stdlib only.
"""

import argparse
import datetime
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
KIND = "exoma-update-handoff"
SCHEMA_VERSION = 1
MANAGERS = ("uv", "pipx", "pip")
PYTHONS = ("3.11", "3.13")
PLATFORMS = ("win32", "linux", "darwin")
CELL_TIMEOUT = 1500


def cell_name(platform, python, manager):
    return f"{platform}-py{python}-{manager}"


def expected_cells(platform):
    return {(python, manager) for python in PYTHONS for manager in MANAGERS}


def checkout_identity(repo=REPO):
    """Full commit SHA and whether the working tree differs from it, or (None, None)."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                             check=True, timeout=30).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
                                    check=True, timeout=30).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None, None
    return sha, dirty


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too large to be a timestamp
        return False


def _independent(platform, state):
    """The same acceptance rule as tests/checks/self_update.py `_handoff_independence_verified`."""
    if not isinstance(state, dict):
        return False
    stages = state.get("stages") if isinstance(state.get("stages"), dict) else {}
    package = stages.get("package") if isinstance(stages.get("package"), dict) else {}
    host_job = state.get("host_job") if isinstance(state.get("host_job"), dict) else {}
    exited, started = state.get("callers_exited_at"), package.get("started_at")
    return (state.get("phase") == "succeeded" and state.get("cleanup_confirmed") is True
            and _number(exited) and _number(started) and exited <= started
            and (platform != "win32" or host_job.get("in_job") is False))


def _cell(report):
    """(python minor, manager) of a report, or None for a field of the wrong type."""
    version, manager = report.get("python_version"), report.get("manager")
    # platform.python_version(): 3.13.1, 3.13.0rc1, 3.14.0a1+ (development builds end in "+").
    found = re.fullmatch(r"(\d+)\.(\d+)\.\d+(?:[a-z]+\d*)?\+?", version) if isinstance(version, str) else None
    return (f"{found.group(1)}.{found.group(2)}" if found else None, manager if isinstance(manager, str) else None)


def check_report(report, commit, platform):
    """Problems with one report as a receipt; empty means it qualifies."""
    problems = []
    if not isinstance(report, dict):
        return ["not a JSON object"]
    schema = report.get("schema_version")
    if report.get("kind") != KIND or type(schema) is not int or schema != SCHEMA_VERSION:
        problems.append("not a current update-handoff report (kind/schema_version)")
    if report.get("mode") != "strict":
        problems.append(f"mode is {report.get('mode')!r}, only strict reports count")
    if report.get("commit") != commit:
        problems.append("commit does not match the candidate")
    if report.get("commit_dirty") is not False:
        problems.append("tested checkout was not clean")
    if report.get("platform") != platform:
        problems.append(f"platform is {report.get('platform')!r}, expected {platform!r}")
    if _cell(report)[0] not in PYTHONS:
        problems.append("unsupported or missing Python version")
    if _cell(report)[1] not in MANAGERS:
        problems.append("unsupported or missing manager")
    for key in ("passed", "actual_version_transition", "independent_worker_verified", "no_op_verified"):
        if report.get(key) is not True:
            problems.append(f"{key} is not true")
    if report.get("manual_recovery") is not False:
        problems.append("manual recovery was not ruled out")
    if report.get("error"):
        problems.append("report carries an error")
    if not _independent(platform, report.get("state")):
        problems.append("state does not prove an independent, cleanly completed handoff")
    return problems


def validate(directory, commit, platform=None):
    """Check every report in `directory` and the coverage of the required cells."""
    platform = platform or sys.platform
    problems, seen = [], {}
    paths = sorted(Path(directory).glob("*.json"))
    for path in paths:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{path.name}: unreadable ({type(exc).__name__})")
            continue
        for issue in check_report(report, commit, platform):
            problems.append(f"{path.name}: {issue}")
        if isinstance(report, dict):
            cell = _cell(report)
            if None not in cell:
                if cell in seen:
                    problems.append(f"{path.name}: duplicates the cell of {seen[cell]}")
                seen[cell] = path.name
    required = expected_cells(platform)
    for python, manager in sorted(required - set(seen)):
        problems.append(f"missing cell: {cell_name(platform, python, manager)}")
    for python, manager in sorted(set(seen) - required):
        problems.append(f"unexpected cell: python {python}, manager {manager} ({seen[(python, manager)]})")
    return problems


def _interpreter(value):
    """A path-bearing interpreter argument becomes absolute; a bare command name stays a PATH lookup."""
    value = str(value)
    if os.sep in value or (os.altsep and os.altsep in value):
        return str(Path(value).resolve())
    return value


def probe_interpreter(executable, want):
    """Return an error when `executable` is not a native CPython `want` on this platform."""
    try:
        out = subprocess.run([str(executable), "-c", "import sys;print('%d.%d' % sys.version_info[:2], sys.platform)"],
                             capture_output=True, text=True, check=True, timeout=60).stdout.split()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{executable}: cannot run ({type(exc).__name__})"
    if len(out) != 2 or out[0] != want:
        return f"{executable}: is Python {out[0] if out else '?'}, expected {want}"
    if out[1] != sys.platform:
        return f"{executable}: reports platform {out[1]!r}, host is {sys.platform!r} (no WSL or cross-OS substitution)"
    return None


def run_cell(executable, manager, report, log):
    """Run one strict handoff cell; the report is written by the test itself."""
    argv = [str(executable), "tests/run.py", "--only", "self-update", "--traceback",
            "--update-artifacts", "handoff", "--update-manager", manager, "--update-report", str(report)]
    with open(log, "wb") as stream:
        try:
            return subprocess.run(argv, cwd=REPO, stdout=stream, stderr=subprocess.STDOUT,
                                  timeout=CELL_TIMEOUT).returncode
        except subprocess.TimeoutExpired:
            return 124


def matrix(pythons, output):
    """Run every manager under each interpreter and validate the collected reports."""
    pythons = {python: _interpreter(executable) for python, executable in pythons.items()}
    for python, executable in pythons.items():
        error = probe_interpreter(executable, python)
        if error:
            print(f"native_update_gate: {error}", file=sys.stderr)
            return 2
    commit, dirty = checkout_identity()
    if commit is None or dirty:
        print("native_update_gate: needs a clean git checkout (commit or stash changes first)", file=sys.stderr)
        return 2
    attempt = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = Path(output).resolve() / commit / attempt
    directory.mkdir(parents=True, exist_ok=False)
    print(f"native_update_gate: {commit} on {sys.platform}, receipts in {directory}")
    failed = []
    for python, executable in pythons.items():
        for manager in MANAGERS:
            name = cell_name(sys.platform, python, manager)
            code = run_cell(executable, manager, directory / f"{name}.json", directory / f"{name}.out.txt")
            print(f"  {name}: {'ok' if code == 0 else 'exit ' + str(code)}", flush=True)
            if code != 0:
                failed.append(name)
    after, dirty_after = checkout_identity()
    problems = validate(directory, commit)
    if after != commit or dirty_after:
        problems.append("checkout changed while the matrix ran")
    for name in failed:
        problems.append(f"cell command failed: {name}")
    for problem in problems:
        print(f"  PROBLEM {problem}", file=sys.stderr)
    print("native_update_gate: " + ("every required cell qualifies" if not problems else f"{len(problems)} problem(s)"))
    return 0 if not problems else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("matrix", help="run the strict handoff gate for every manager and Python")
    run.add_argument("--python-311", required=True, help="native Python 3.11 executable")
    run.add_argument("--python-313", required=True, help="native Python 3.13 executable")
    run.add_argument("--output", default=str(REPO / ".workflow" / "release-evidence"),
                     help="directory for <commit>/<attempt>/ receipts (git-ignored by default)")
    check = commands.add_parser("validate", help="check a directory of receipts against a commit")
    check.add_argument("directory")
    check.add_argument("--commit", required=True, help="full SHA the receipts must name")
    check.add_argument("--platform", choices=PLATFORMS, help="platform the receipts must report (default: this host)")
    args = parser.parse_args(argv)
    if args.command == "matrix":
        return matrix({"3.11": args.python_311, "3.13": args.python_313}, args.output)
    problems = validate(args.directory, args.commit, args.platform)
    for problem in problems:
        print(f"PROBLEM {problem}", file=sys.stderr)
    print("every required cell qualifies" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
