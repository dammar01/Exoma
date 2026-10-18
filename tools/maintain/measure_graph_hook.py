#!/usr/bin/env python3
"""Measure what the graph-refresh Stop hook costs on one project (RQ-13, CASE-008).

The graph-refresh hook runs at the end of every implementing turn: it looks for the newest
source file outside the dependency and build directories, and runs `graphify update` when
the graph is older. Its cost was a perception until this measured it. CASE-008 was counted
with it, and it is kept so the figures can be re-counted on any project:

  1. project size: files inside the hook's skip directories vs source files outside them
  2. the hook's own scan (graph-refresh.py: os.walk, skip directories pruned before descent,
     matched case-insensitively), replicated here and timed in this process
  3. optionally the installed hook end to end, and `graphify update` alone

The scan is the same on every OS, because since DEC-055 every OS runs the same Python hook
through Bash. CASE-008's figures came from the retired PowerShell scans (v3.7.3); they were
timed by another method and are not comparable with `scan_ms`.

Read-only unless --run-hook or --run-graphify is given: both may regenerate graphify-out/.
--force-stale (with --run-hook) backdates graphify-out/graph.json before the hook runs, so
the hook takes its refresh path; source files are never touched, and the graph's own mtime
is put back when the hook did not rewrite it.
--wait-refresh (with --run-hook) waits for a detached refresh (DEC-012) to release
graphify-out/.refresh.lock, and reports how long it took after the hook returned.
Prints one JSON object of counts and timings, nothing from the project's files: the hook
is named by its repository path or as `~/...`, and graphify's last line has its paths
masked. The suite (metrics-contract) checks its scan and output shape; it never runs a
measurement on a real project.

  python tools/maintain/measure_graph_hook.py --project <root>
  python tools/maintain/measure_graph_hook.py --project <root> --run-hook --run-graphify
  python tools/maintain/measure_graph_hook.py --project <root> --run-hook --force-stale
  python tools/maintain/measure_graph_hook.py --project <root> --run-hook --force-stale --wait-refresh --hook dist/config/claude/hooks/graph-refresh.sh
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

# The same lists as dist/config/claude/hooks/graph-refresh.py (a test parses both).
SOURCE_EXTENSIONS = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".php", ".go", ".rs", ".java", ".rb")
SKIP_DIRS = frozenset({
    "node_modules", ".git", ".venv", "venv", "__pycache__", "vendor",
    "dist", "build", ".next", "coverage", "target", "graphify-out", ".workflow",
})


def hook_scan(root: Path) -> dict:
    """The hook's gate-2 scan, as graph-refresh.py runs it: milliseconds, source files seen,
    directories pruned."""
    started = time.perf_counter()
    visited = skipped = 0
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        kept = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        skipped += len(dirnames) - len(kept)
        dirnames[:] = kept
        for name in filenames:
            if name.lower().endswith(SOURCE_EXTENSIONS):
                visited += 1
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
                except OSError:
                    pass
    return {"ms": round((time.perf_counter() - started) * 1000), "files_visited": visited, "dirs_skipped": skipped}


def _hidden() -> dict:
    """No console window per child started (Windows): the hook and graphify."""
    if os.name != "nt":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return {"startupinfo": info, "creationflags": subprocess.CREATE_NO_WINDOW}


def project_size(root: Path) -> dict:
    """Files inside skip directories (by directory) and source files outside them."""
    in_skip: dict[str, int] = {}
    total = source = 0
    for current, dirs, files in os.walk(root):
        parts = Path(current).relative_to(root).parts
        hit = next((part for part in parts if part.lower() in SKIP_DIRS), None)
        total += len(files)
        if hit:
            in_skip[hit] = in_skip.get(hit, 0) + len(files)
        else:
            source += sum(1 for name in files if os.path.splitext(name)[1].lower() in SOURCE_EXTENSIONS)
    return {
        "files_total": total,
        "files_in_skip_dirs": sum(in_skip.values()),
        "files_in_skip_dirs_by_dir": dict(sorted(in_skip.items(), key=lambda item: -item[1])),
        "source_files_outside_skip": source,
    }


def _mtime(path: Path) -> float | None:
    return path.stat().st_mtime if path.is_file() else None


REPO_ROOT = Path(__file__).resolve().parents[2]
_PATHISH = re.compile(r"\S*[\\/]\S*")
LAST_LINE_CHARS = 200


def hook_label(hook: Path) -> str:
    """The hook as the output names it: inside this repository by its repository path, under
    the home directory as `~/...` (no user name), anywhere else by its file name only."""
    full = Path(os.path.abspath(hook))
    for base, prefix in ((REPO_ROOT, ""), (Path.home(), "~/")):
        try:
            return prefix + full.relative_to(base).as_posix()
        except ValueError:
            continue
    return full.name


def sanitize_line(line: str | None, root: Path) -> str | None:
    """graphify's last line with every path-like token replaced: it names files of the project."""
    if line is None:
        return None
    for form in {str(root), root.as_posix()}:
        line = line.replace(form, "<project>")
    line = _PATHISH.sub("<path>", line)
    return line[:LAST_LINE_CHARS]


def default_hook() -> Path:
    """The installed hook launcher, the same on every OS."""
    return Path.home() / ".claude" / "hooks" / "graph-refresh.sh"


# Older than any source file, so the hook's mtime gate sees the graph as stale.
STALE_MTIME = 946684800.0  # 2000-01-01T00:00:00Z
# The detached worker's own bound (600 s) plus room to start and record.
REFRESH_WAIT_S = 660


def _wait_for_refresh(root: Path) -> int | None:
    """Milliseconds until the refresh lock is gone, or None when it outlived the wait."""
    lock = root / "graphify-out" / ".refresh.lock"
    started = time.perf_counter()
    while lock.exists():
        if time.perf_counter() - started > REFRESH_WAIT_S:
            return None
        time.sleep(0.2)
    return round((time.perf_counter() - started) * 1000)


def measure(root: Path, *, hook: Path, repeat: int, run_hook: bool, run_graphify: bool, force_stale: bool = False, wait_refresh: bool = False) -> dict:
    graph = root / "graphify-out" / "graph.json"
    result: dict = {
        "measured_on": _dt.date.today().isoformat(),
        "hook_measured": hook_label(hook),
        "graph_exists": graph.is_file(),
        **project_size(root),
    }
    scans = [hook_scan(root) for _ in range(repeat)]
    timings = [scan["ms"] for scan in scans]
    result.update(
        scan_ms=timings,
        scan_median_ms=statistics.median_low(timings),
        scan_files_visited=scans[-1]["files_visited"],
        scan_dirs_skipped=scans[-1]["dirs_skipped"],
    )

    if run_hook:
        if not hook.is_file():
            raise SystemExit(f"hook not found: {hook}")
        # Git Bash on Windows (osutil.find_bash), as the installed settings run it.
        sys.path.insert(0, str(REPO_ROOT))
        from utils.osutil import find_bash

        bash = find_bash()
        if not bash:
            # No bare `bash` fallback: on Windows it can resolve to WSL's, which cannot
            # read the Windows paths the hook carries, and the timing would be of nothing.
            raise SystemExit("no bash found (Git Bash on Windows): the hook cannot be measured")
        command = [bash, str(hook)]
        payload = json.dumps({"last_assistant_message": "[EXECUTION RESULT] measurement", "cwd": str(root)})
        original = graph.stat() if force_stale and graph.is_file() else None
        if original is not None:
            os.utime(graph, (STALE_MTIME, STALE_MTIME))
        result["hook_forced_stale"] = original is not None
        before = _mtime(graph)
        started = time.perf_counter()
        try:
            done = subprocess.run(command, input=payload, capture_output=True, text=True, errors="replace", **_hidden())
            result["hook_total_ms"] = round((time.perf_counter() - started) * 1000)
            # The hook always exits 0; anything else is its launcher failing, not a timing.
            result["hook_exit_code"] = done.returncode
            if wait_refresh:
                # The turn pays hook_total_ms; this is how long the graph stayed stale after it.
                result["refresh_after_hook_ms"] = _wait_for_refresh(root)
            result["hook_refreshed_graph"] = _mtime(graph) not in (None, before)
        finally:
            # A hook that did not rewrite the graph leaves it backdated; put its time back.
            if original is not None and _mtime(graph) == STALE_MTIME:
                os.utime(graph, (original.st_atime, original.st_mtime))

    if run_graphify:
        graphify = shutil.which("graphify")
        if graphify is None:
            result["graphify_update_ms"] = None
        else:
            before = _mtime(graph)
            started = time.perf_counter()
            done = subprocess.run([graphify, "update"], cwd=root, capture_output=True, text=True, errors="replace", **_hidden())
            result["graphify_update_ms"] = round((time.perf_counter() - started) * 1000)
            # graphify reports on stderr and exits 1 when only its HTML view fails; whether
            # graph.json was rewritten is recorded beside the exit code (CASE-008).
            result["graphify_exit_code"] = done.returncode
            lines = [line for line in (done.stdout + "\n" + done.stderr).splitlines() if line.strip()]
            result["graphify_last_line"] = sanitize_line(lines[-1], root) if lines else None
            result["graphify_rewrote_graph"] = _mtime(graph) not in (None, before)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True, help="project root to measure")
    parser.add_argument("--hook", type=Path, default=default_hook(), help="installed graph-refresh hook")
    parser.add_argument("--repeat", type=int, default=3, help="runs per scan (median reported)")
    parser.add_argument("--run-hook", action="store_true", help="also run the installed hook end to end")
    parser.add_argument("--run-graphify", action="store_true", help="also time `graphify update` alone")
    parser.add_argument("--force-stale", action="store_true", help="with --run-hook: backdate graph.json so the hook refreshes it")
    parser.add_argument("--wait-refresh", action="store_true", help="with --run-hook: wait for a detached refresh to release its lock")
    args = parser.parse_args(argv)
    if args.force_stale and not args.run_hook:
        parser.error("--force-stale needs --run-hook")
    if args.wait_refresh and not args.run_hook:
        parser.error("--wait-refresh needs --run-hook")
    root = Path(args.project).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    result = measure(
        root, hook=args.hook, repeat=max(1, args.repeat), run_hook=args.run_hook,
        run_graphify=args.run_graphify, force_stale=args.force_stale, wait_refresh=args.wait_refresh,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
