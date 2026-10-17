#!/usr/bin/env bash
# graph-refresh.sh - Stop hook (POSIX parity of graph-refresh.ps1).
# Regenerates graphify-out/ after main_agent actually changed code. Two gates:
#   1. Did this turn implement? ([EXECUTION RESULT] / [REFACTOR RESULT] in last message)
#   2. Is the graph older than the sources? (mtime compare, skip dirs pruned before descent)
# Both must pass. The refresh is detached (DEC-012): the hook starts a worker that runs
# `graphify update` only (never init/build/watch) with no time limit (DEC-042), beats the
# lock while it runs, records one row in the quality stream, and removes
# graphify-out/.refresh.lock. Readers treat the graph as stale while the lock is held,
# because graphify rewrites graph.json in place.
# Never blocks the response (always exit 0).
RAW="$(cat)"
[ -z "$RAW" ] && exit 0
# Interpreter: the first of python3, python, `py -3` that RUNS and is >= 3.10 (utils/osutil.py
# MIN_PYTHON). A name on PATH is not enough: Windows' WindowsApps aliases open the Store
# instead of running, and an old python3 can shadow a newer python. None found: fail open.
PY=""
for cand in "python3" "python" "py -3"; do
  set -- $cand
  command -v "$1" >/dev/null 2>&1 || continue
  if "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done
[ -z "$PY" ] && exit 0
CLAUDE_HOOK_RAW="$RAW" $PY <<'PY'
import os, sys, json, shutil, subprocess, time

SOURCE_EXT = (
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".php", ".go", ".rs", ".java", ".rb",
)
SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", "vendor",
    "dist", "build", ".next", "coverage", "target", "graphify-out", ".workflow",
}
# graphify runs without a time limit (DEC-042): a large graph takes as long as it takes, and
# the worker never kills it. The lock is held while the worker keeps rewriting `beat`, every
# BEAT_INTERVAL_S; a lock whose beat is BEAT_STALE_S old has no worker behind it. A lock
# without `beat` was written before this rule and keeps the old one: LEGACY_MAX_AGE_S and a
# live pid. core.graph.graph_index and graph-refresh.ps1 use the same numbers (a test parses
# all three). GRAPH_REFRESH_BEAT_S shortens the interval for tests.
BEAT_INTERVAL_S = 15
BEAT_STALE_S = 120
LEGACY_MAX_AGE_S = 900


def beat_interval():
    try:
        value = float(str(os.environ.get("GRAPH_REFRESH_BEAT_S", "")).strip())
    except ValueError:
        return BEAT_INTERVAL_S
    return value if 0 < value < BEAT_STALE_S else BEAT_INTERVAL_S

# The worker runs as its own python process, started in a new session so it outlives the
# hook, with no handle shared with it. argv: root hook_ms scan_ms visited skipped token beat_s
WORKER = r'''
import os, sys, json, shutil, subprocess, time
root, hook_ms, scan_ms, visited, skipped, token = sys.argv[1], *map(int, sys.argv[2:6]), sys.argv[6]
interval = float(sys.argv[7])
graph = os.path.join(root, "graphify-out", "graph.json")
lock = os.path.join(root, "graphify-out", ".refresh.lock")


def beat(graphify_pid):
    # Rewrite `beat` while the lock is still this worker's, beside it and renamed over it
    # so a reader never sees half a lock. The pid stays whatever the lock names.
    staged = lock + ".beat." + token
    try:
        with open(lock, encoding="utf-8") as fh:
            current = json.load(fh)
        if current.get("token") != token:
            return
        current["beat"] = int(time.time())
        current["graphify_pid"] = graphify_pid
        with open(staged, "w", encoding="utf-8") as fh:
            json.dump(current, fh)
        os.replace(staged, lock)
    except Exception:
        pass
    finally:
        try:
            os.remove(staged)
        except OSError:
            pass


hide = {}
if os.name == "nt":
    # This worker has no console; without SW_HIDE a console child (graphify.cmd) would
    # open a visible window of its own.
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    hide = {"startupinfo": info, "creationflags": 0x08000000}  # CREATE_NO_WINDOW
outcome, exit_code, graphify_ms, rewritten = "error", None, 0, False
try:
    before = os.path.getmtime(graph)
    started = time.monotonic()
    proc = subprocess.Popen(
        [shutil.which("graphify") or "graphify", "update"], cwd=root,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **hide,
    )
    beat(proc.pid)
    # No limit: wait as long as graphify runs, beating so readers know a writer is alive.
    while True:
        try:
            exit_code = proc.wait(timeout=interval)
            break
        except subprocess.TimeoutExpired:
            beat(proc.pid)
    graphify_ms = int((time.monotonic() - started) * 1000)
    rewritten = os.path.getmtime(graph) != before
    # graphify exits 1 on a large graph when only its HTML view fails, having written
    # graph.json (CASE-008): a rewritten graph is the success signal, not the exit code.
    # A rewrite that does not parse is "corrupt", never a refresh.
    if rewritten:
        try:
            with open(graph, encoding="utf-8") as fh:
                json.load(fh)
            outcome = "refreshed"
        except Exception:
            outcome = "corrupt"
except Exception:
    pass
finally:
    data = os.path.join(root, ".workflow", "data")
    if os.path.isdir(data):
        row = {"kind": "graph_refresh", "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "hook": "sh", "outcome": outcome, "hook_ms": hook_ms, "scan_ms": scan_ms,
               "files_visited": visited, "dirs_skipped": skipped, "graphify_ms": graphify_ms,
               "graphify_exit": exit_code, "graph_rewritten": rewritten}
        try:
            with open(os.path.join(data, "quality.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except Exception:
            pass
    try:
        with open(lock, encoding="utf-8") as fh:
            owned = json.load(fh).get("token") == token
        if owned:
            os.remove(lock)
    except Exception:
        pass
'''


def pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) sends CTRL_C_EVENT on Windows; ask the kernel instead.
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_lock(path):
    # The lock as written, or None when there is none (or it cannot be read).
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def lock_held(path):
    # Same rule as core.graph.graph_index.refresh_lock_state.
    try:
        with open(path, encoding="utf-8") as fh:
            lock = json.load(fh)
        if "beat" in lock:
            return time.time() - int(lock["beat"]) < BEAT_STALE_S
        return time.time() - int(lock["started"]) < LEGACY_MAX_AGE_S and pid_alive(int(lock["pid"]))
    except FileNotFoundError:
        return False
    except Exception:
        # Present but unreadable: held while its mtime is inside the beat window; every
        # writer replaces the file whole, so a live one keeps it young.
        try:
            return time.time() - os.path.getmtime(path) < BEAT_STALE_S
        except OSError:
            return False


def lock_token(path):
    # The token of the lock as written, or None when there is none (or it cannot be read).
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("token")
    except Exception:
        return None


def hand_off(lock_path, token, worker):
    # Replace the hook's lock with one naming the worker, but only while the lock is still
    # this hook's: a worker that already finished has removed it, and a lock written back
    # then would name a dead pid with no one left to remove it. Written beside the lock and
    # renamed over it, so a reader never sees an empty or half lock.
    staged = lock_path + "." + token
    try:
        with open(staged, "w", encoding="utf-8") as fh:
            now = int(time.time())
            json.dump({"pid": worker.pid, "token": token, "started": now, "beat": now}, fh)
        if lock_token(lock_path) != token:
            return
        os.replace(staged, lock_path)
        # POSIX has no rename that requires its target to exist (graph-refresh.ps1 gets one
        # from File.Replace): a worker that finished between the check and the rename has
        # just had its lock written back, so take it out again.
        if worker.poll() is not None and lock_token(lock_path) == token:
            os.remove(lock_path)
    finally:
        if os.path.exists(staged):
            os.remove(staged)


def write_row(root, fields):
    # Fail-open: a workspace without .workflow/data gets no row.
    data = os.path.join(root, ".workflow", "data")
    if not os.path.isdir(data):
        return
    row = {"kind": "graph_refresh", "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "hook": "sh"}
    row.update(fields)
    try:
        with open(os.path.join(data, "quality.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


try:
    started = time.monotonic()
    hook_ms = lambda: int((time.monotonic() - started) * 1000)
    raw = os.environ.get("CLAUDE_HOOK_RAW", "")
    if not raw.strip():
        sys.exit(0)
    payload = json.loads(raw)
    message = str(payload.get("last_assistant_message") or "")
    cwd = payload.get("cwd") or os.getcwd()

    # Gate 1: did this turn implement anything?
    if "[EXECUTION RESULT]" not in message and "[REFACTOR RESULT]" not in message:
        sys.exit(0)

    try:
        root = os.path.realpath(cwd)
    except Exception:
        root = cwd

    graph_path = os.path.join(root, "graphify-out", "graph.json")
    if not os.path.isfile(graph_path):
        # No graph in this project. `graphify init` is never run automatically.
        sys.exit(0)

    lock_path = os.path.join(root, "graphify-out", ".refresh.lock")
    seen_lock = read_lock(lock_path)
    # A lock that changed since it was judged belongs to a hook that just took it: deleting
    # it as stale would start a second graphify beside the first. Re-read right before the
    # delete, and only the lock judged stale is removed.
    if lock_held(lock_path) or (seen_lock is not None and read_lock(lock_path) != seen_lock):
        write_row(root, {"outcome": "skipped_running", "hook_ms": hook_ms()})
        sys.exit(0)
    if seen_lock is not None:
        try:
            os.remove(lock_path)
        except OSError:
            pass

    # Gate 2: is the graph behind the sources?
    graph_time = os.path.getmtime(graph_path)
    scan_started = time.monotonic()
    newest, visited, skipped = 0.0, 0, 0
    for dirpath, dirnames, filenames in os.walk(root):
        # Case-insensitive, as the .ps1 flavour matches: Vendor/ is vendor/ on Windows and macOS.
        kept = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        skipped += len(dirnames) - len(kept)
        dirnames[:] = kept
        for fn in filenames:
            if fn.lower().endswith(SOURCE_EXT):
                visited += 1
                try:
                    mt = os.path.getmtime(os.path.join(dirpath, fn))
                    if mt > newest:
                        newest = mt
                except Exception:
                    pass
    scan_ms = int((time.monotonic() - scan_started) * 1000)
    scan = {"scan_ms": scan_ms, "files_visited": visited, "dirs_skipped": skipped}
    if newest <= graph_time:
        write_row(root, {"outcome": "skipped_fresh", "hook_ms": hook_ms(), **scan})
        sys.exit(0)  # graph is current

    if not shutil.which("graphify"):
        write_row(root, {"outcome": "no_graphify", "hook_ms": hook_ms(), **scan})
        sys.exit(0)

    # Exclusive create: of two hooks that found the graph stale, one starts the worker.
    # The token, not the pid, says who owns the lock: the pid Popen reports can belong to
    # a launcher in front of the interpreter that actually runs.
    token = os.urandom(8).hex()
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        now = int(time.time())
        os.write(fd, json.dumps({"pid": os.getpid(), "token": token, "started": now, "beat": now}).encode())
        os.close(fd)
    except OSError:
        sys.exit(0)
    flags = {}
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
        flags["startupinfo"] = info
        flags["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        flags["start_new_session"] = True
    worker = subprocess.Popen(
        [sys.executable, "-c", WORKER, root, str(hook_ms()), str(scan_ms), str(visited), str(skipped), token,
         str(beat_interval())],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, **flags,
    )
    # Hand the lock to the worker before this hook exits: a lock naming the hook's own pid
    # would read as abandoned the moment it returns, while graphify is still writing.
    try:
        hand_off(lock_path, token, worker)
    except Exception:
        pass
    sys.exit(0)
except SystemExit:
    raise
except Exception:
    sys.exit(0)
PY
exit 0
