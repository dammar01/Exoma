# task-events.py - task telemetry hook.
# Registered on UserPromptSubmit (a local skill invoked as /.<name>), PostToolUse
# Skill|Edit|Write|MultiEdit|NotebookEdit (the Skill tool, a file edited) and Stop (commits
# made since the last look). Records what the runtime never sees into
# .workflow/data/tasks.jsonl; delegated skills are left out, the runtime records them.
# A commit is a HEAD that moved: the first event of a session snapshots HEAD under
# .workflow/data/task-hook/, and each Stop (or the next prompt) records the commits since
# and moves the snapshot. Only appends; task state is derived at report time
# (core/audit/task_telemetry.py). Never blocks, exit 0.
#
# Anchor reads (Stop only). The runtime proves a cited `file:line` exists; it cannot prove
# the primary agent looked at it. So at Stop, when the reply carries its own [PLAN] or
# [ANALYSIS RESULT] block (a header opening a line, outside code fences — never prose) and
# the session's newest delegated run is a plan or analyze, the hook reads `transcript_path`
# backward from its end, only as far as the newest `.workflow/run.sh plan|analyze` call: a
# Read tool call after it whose file and line range overlap an anchor that run cites counts
# as opened. None opened → a systemMessage says so, once per run (runtime/anchor-reads.json).
# No hook runs per Read, and no other Stop reads the transcript. Warning only: a Read through
# the shell is not seen, a Read proves a look, not a verified claim, and an unreadable
# transcript, or one with no runner call within TRANSCRIPT_SCAN_BYTES of its end, warns nothing.
# `policies.enforcement: off` turns the warning off.
import datetime, json, os, re, subprocess

LOCAL_SKILLS = {
    "execute", "init", "upgrade", "doctor", "sweep", "commit",
    "memory", "caveman", "local", "provider", "promote", "help",
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


def hidden_window():
    """(startupinfo, creationflags) that keep git's console hidden on Windows; (None, 0)
    elsewhere. Claude Code runs this hook without a console, and a console child (git)
    started from such a process opens a window of its own, once per event."""
    if os.name != "nt":
        return None, 0
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0  # SW_HIDE
    return info, 0x08000000  # CREATE_NO_WINDOW


def git_out(root, *args):
    info, flags = hidden_window()
    out = subprocess.run(
        ["git", "-C", root, *args], capture_output=True, text=True, timeout=5,
        startupinfo=info, creationflags=flags,
    )
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


# `path/to/file.ext:12` or `:12-40`, the shape every delegated citation takes.
ANCHOR = re.compile(r"([A-Za-z0-9_.\-]+(?:[/\\][A-Za-z0-9_.\-]+)*\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?")
ANCHOR_COMMANDS = {"plan", "analyze"}
ANCHOR_SCAN_LIMIT = 200
RUN_DIR = re.compile(r"_(explore|plan|analyze|verify|verify-browser)_[0-9a-f]+$")
SYNTHESIS_HEADER = re.compile(r"^[ \t#*_-]*\[(PLAN|ANALYSIS RESULT)\]", re.MULTILINE)
FENCE = re.compile(r"^[ \t]*(```|~~~).*?(?:^[ \t]*\1[^\n]*$|\Z)", re.DOTALL | re.MULTILINE)
# The delegated call in a Bash tool input; `plan-record` and the like are not a run.
RUNNER_CALL = re.compile(r"\.workflow[/\\]run\.sh['\"]?\s+['\"]?(plan|analyze)(?=['\"]?\s)")
# How far back from the transcript's end one Stop looks for that call, and the read size.
TRANSCRIPT_SCAN_BYTES = 32 * 1024 * 1024
TRANSCRIPT_CHUNK = 64 * 1024


def session_dir(root, main_id):
    return os.path.join(root, ".workflow", "data", "sessions", main_id) if main_id else None


def latest_run(sdir):
    """(run directory name, command) of the session's newest delegated run, or (None, None)."""
    logs = os.path.join(sdir, "logs")
    try:
        names = sorted(n for n in os.listdir(logs) if RUN_DIR.search(n))
    except OSError:
        return None, None
    if not names:
        return None, None
    return names[-1], RUN_DIR.search(names[-1]).group(1)


def run_anchors(root, sdir, run):
    """{relative path: [(start, end), ...]} cited in the run's output that exist in `root`."""
    try:
        with open(os.path.join(sdir, "logs", run, "output.raw.md"), encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}
    found = {}
    for match in ANCHOR.finditer(text):
        if sum(len(spans) for spans in found.values()) >= ANCHOR_SCAN_LIMIT:
            break
        rel = match.group(1).replace("\\", "/")
        if rel not in found and not os.path.isfile(os.path.join(root, rel)):
            continue
        start = int(match.group(2))
        found.setdefault(rel, []).append((start, int(match.group(3) or start)))
    return found


def read_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def write_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def lines_from_end(path, budget):
    """The file's complete lines, newest first, from at most `budget` bytes of its tail. A
    line cut by the budget is dropped; splitting on b"\\n" never cuts a UTF-8 character."""
    with open(path, "rb") as fh:
        pos = fh.seek(0, os.SEEK_END)
        tail = b""
        while pos > 0 and budget > 0:
            step = min(TRANSCRIPT_CHUNK, pos, budget)
            pos -= step
            budget -= step
            fh.seek(pos)
            parts = (fh.read(step) + tail).split(b"\n")
            tail = parts.pop(0)
            yield from reversed(parts)
        # the budget may end exactly at a line start; that line is whole
        if pos > 0:
            fh.seek(pos - 1)
        if pos == 0 or fh.read(1) == b"\n":
            yield tail


def read_span(root, ti):
    """(relative path, first line, last line) of one Read tool input, or None."""
    try:
        rel = inside(root, ti.get("file_path"))
        if not rel:
            return None
        first = int(ti.get("offset") or 1)
        limit = ti.get("limit")
        return rel, first, first + int(limit) - 1 if limit else float("inf")
    except (TypeError, ValueError):
        return None


def transcript_reads(path, root):
    """[(relative path, first line, last line)] of the Read calls made after the newest
    plan/analyze runner call, found by reading the transcript backward from its end; None
    when it cannot be read or no runner call lies within TRANSCRIPT_SCAN_BYTES of the end
    (unknown, not "nothing opened")."""
    reads = []
    try:
        for line in lines_from_end(path, TRANSCRIPT_SCAN_BYTES):
            # tool results and prose are most of the bytes; never parse them
            if b'"tool_use"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") if isinstance(entry, dict) and not entry.get("isSidechain") else None
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content")
            for block in reversed(content if isinstance(content, list) else []):
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                ti = block.get("input") if isinstance(block.get("input"), dict) else {}
                if block.get("name") == "Bash" and RUNNER_CALL.search(str(ti.get("command") or "")):
                    return reads
                if block.get("name") == "Read":
                    span = read_span(root, ti)
                    if span:
                        reads.append(span)
    except OSError:
        return None
    return None


def anchor_warning(payload, root):
    """The Stop-time systemMessage, or None."""
    policies = (read_state(os.path.join(root, ".workflow", "config.json")).get("policies") or {})
    if str(policies.get("enforcement") or "hybrid") == "off":
        return None
    text = FENCE.sub("", str(payload.get("last_assistant_message") or ""))
    if not SYNTHESIS_HEADER.search(text):
        return None
    sdir = session_dir(root, main_session(payload.get("session_id")))
    if not sdir:
        return None
    run, command = latest_run(sdir)
    anchors = run_anchors(root, sdir, run) if run and command in ANCHOR_COMMANDS else {}
    if not anchors:
        return None
    path = os.path.join(sdir, "runtime", "anchor-reads.json")
    state = read_state(path)
    if state.get("run") == run and state.get("warned"):
        return None
    reads = transcript_reads(payload.get("transcript_path"), root) if payload.get("transcript_path") else None
    if reads is None:
        return None
    if any(a <= last and b >= first for rel, first, last in reads for a, b in anchors.get(rel, ())):
        return None
    write_state(path, {"run": run, "warned": True})
    return (
        "[ANCHOR CHECK] this %s was written from the %s digest without opening any anchor it "
        "cites. The runtime proved those lines exist, not that the claims about them hold; "
        "read the anchor a decision rests on before acting on it." % (
            "[PLAN]" if "[PLAN]" in text else "[ANALYSIS RESULT]", command)
    )


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
    payload = json.loads(globals().get("HOOK_RAW") or os.environ.get("CLAUDE_HOOK_RAW", ""))
    root = project_root(payload.get("cwd"))
    if root and payload.get("hook_event_name") == "Stop":
        try:
            message = anchor_warning(payload, root)
        except Exception:
            message = None
        if message:
            print(json.dumps({"systemMessage": message}))
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
