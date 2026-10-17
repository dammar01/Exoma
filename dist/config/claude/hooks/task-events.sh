#!/usr/bin/env bash
# task-events.sh - task telemetry hook (DEC-015). POSIX parity with task-events.ps1.
# Registered on UserPromptSubmit (a local skill invoked as /.<name>), PostToolUse
# Skill|Edit|Write|MultiEdit|NotebookEdit (the Skill tool, a file edited) and Stop (commits
# made since the last look). Records what the runtime never sees into
# .workflow/data/tasks.jsonl; delegated skills are left out, the runtime records them.
# A commit is a HEAD that moved: the first event of a session snapshots HEAD under
# .workflow/data/task-hook/, and each Stop (or the next prompt) records the commits since
# and moves the snapshot. Only appends; task state is derived at report time
# (core/audit/task_telemetry.py). Never blocks, exit 0.
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
import datetime, json, os, re, subprocess

LOCAL_SKILLS = {
    "execute", "init", "upgrade", "doctor", "sweep", "refactor", "commit", "review",
    "compress", "memory", "caveman", "local", "provider", "promote", "help",
}
SKILL_PROMPT = re.compile(r"^\s*/\.([\w-]+)")
EVENT_VERSION = 2


def project_root(start):
    current = os.path.abspath(start or ".")
    while True:
        if os.path.isdir(os.path.join(current, ".workflow", "data")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def main_session(claude_sid):
    try:
        path = os.path.join(os.path.expanduser("~"), ".claude", "session_registry.json")
        with open(path, encoding="utf-8") as fh:
            entry = (json.load(fh) or {}).get(claude_sid) or {}
        return entry.get("main_session_id") or None
    except Exception:
        return None


def inside(root, path):
    if not path:
        return None
    full = os.path.abspath(path if os.path.isabs(path) else os.path.join(root, path))
    try:
        rel = os.path.relpath(full, root)
    except ValueError:
        return None
    rel = rel.replace("\\", "/")
    if rel.startswith("../") or rel == ".." or rel.split("/")[0] in (".workflow", ".git"):
        return None
    return rel


def git_out(root, *args):
    out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=5)
    return out.stdout if out.returncode == 0 else None


def snapshot_path(root, payload):
    key = re.sub(r"[^\w-]", "_", str(payload.get("session_id") or ""))
    return os.path.join(root, ".workflow", "data", "task-hook", key + ".head") if key else None


def write_snapshot(path, head):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(head)


def commits_since_snapshot(payload, root):
    path = snapshot_path(root, payload)
    if not path:
        return []
    now = (git_out(root, "rev-parse", "--verify", "-q", "HEAD") or "").strip()
    if not os.path.isfile(path):
        if now:
            write_snapshot(path, now)
        return []
    with open(path, encoding="utf-8") as fh:
        before = fh.read().strip()
    if not now or now == before:
        return []
    write_snapshot(path, now)
    span = f"{before}..{now}" if before else now
    out = git_out(root, "log", "--reverse", "--first-parent", "-m", "--format=commit:%H",
                  "--name-only", "--relative", span)
    if out is None:
        return []
    commits, current = [], None
    for line in out.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("commit:"):
            if current:
                commits.append(current)
            current = {"kind": "commit", "commit": text[len("commit:"):], "paths": []}
        elif current:
            current["paths"].append(text)
    if current:
        commits.append(current)
    return commits


def events_for(payload, root):
    hook = payload.get("hook_event_name")
    if hook == "UserPromptSubmit":
        found = commits_since_snapshot(payload, root)
        match = SKILL_PROMPT.match(str(payload.get("prompt") or ""))
        if match and match.group(1) in LOCAL_SKILLS:
            found.append({"kind": "skill", "skill": match.group(1)})
        return found
    if hook == "Stop":
        return commits_since_snapshot(payload, root)
    if hook != "PostToolUse":
        return []
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    if tool == "Skill":
        name = str(ti.get("skill") or "").lstrip(".")
        return [{"kind": "skill", "skill": name}] if name in LOCAL_SKILLS else []
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        rel = inside(root, ti.get("file_path") or ti.get("notebook_path"))
        return [{"kind": "edit", "path": rel}] if rel else []
    return []


try:
    payload = json.loads(os.environ.get("CLAUDE_HOOK_RAW", ""))
    root = project_root(payload.get("cwd"))
    events = events_for(payload, root) if root else []
    if events:
        session = main_session(payload.get("session_id"))
        with open(os.path.join(root, ".workflow", "data", "tasks.jsonl"), "a", encoding="utf-8") as fh:
            for event in events:
                row = {
                    "v": EVENT_VERSION,
                    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "source": "hook",
                    "session_id": session,
                    **event,
                }
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
except Exception:
    pass
PY
exit 0
