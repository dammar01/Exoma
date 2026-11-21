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
import datetime, json, os, re, shutil, signal, subprocess, tempfile, time
if os.name == "nt":
    import msvcrt
else:
    import fcntl

LOCAL_SKILLS = {
    "execute", "init", "upgrade", "doctor", "sweep", "commit",
    "memory", "caveman", "local", "provider", "promote", "help",
}
SKILL_PROMPT = re.compile(r"^\s*/\.([\w-]+)")
EVENT_VERSION = 2
GIT_DEADLINE_SECONDS = 2.0
DIAGNOSTICS_VERSION = 1
DIAGNOSTICS_MAX_BYTES = 256 * 1024
SNAPSHOT_LOCK_WAIT_SECONDS = 0.25
OID_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
REBASELINE_SNAPSHOT = "__REBASELINE__"


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


def atomic_write_text(path, text):
    """Stage in the target directory and replace atomically; never leave a truncated target."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), time.time_ns())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
        for attempt in range(4):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.01 * (attempt + 1))
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def acquire_lock(lock_path, wait_seconds=SNAPSHOT_LOCK_WAIT_SECONDS):
    """OS-backed non-blocking file lock with bounded wait. Released by the kernel on crash."""
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fh = open(lock_path, "a+b")
    except OSError:
        return None
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            if os.name == "nt":
                fh.seek(0)
                if fh.seek(0, os.SEEK_END) == 0:
                    fh.write(b"\0")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.monotonic() >= deadline:
                try:
                    fh.close()
                except OSError:
                    pass
                return None
            time.sleep(0.01)


def release_lock(fh):
    if fh is None:
        return
    try:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass


def needs_leading_newline(path):
    """Best-effort tail probe decoupled from write-only append."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                return False
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) != b"\n"
    except OSError:
        return False


def diagnostics_path(root):
    return os.path.join(root, ".workflow", "data", "task-hook", "diagnostics.jsonl")


def log_diagnostic(root, event, session_id, stage, reason):
    """Append one privacy-safe failure row outside tasks.jsonl; trim above 256 KiB."""
    if not root:
        return
    path = diagnostics_path(root)
    row = {
        "v": DIAGNOSTICS_VERSION,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "hook": "task-events",
        "event": str(event or ""),
        "session_id": session_id,
        "stage": stage,
        "reason": reason,
    }
    lock_fh = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lock_fh = acquire_lock(path + ".lock", wait_seconds=0.1)
        if lock_fh is None:
            # Avoid appending without the lock while another hook may be compacting.
            return
        if os.path.isfile(path) and os.path.getsize(path) > DIAGNOSTICS_MAX_BYTES:
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
                half = raw[len(raw) // 2:]
                cut = half.find(b"\n")
                trimmed = half[cut + 1:] if cut >= 0 else b""
                atomic_write_text(path, trimmed.decode("utf-8", errors="replace"))
            except Exception:
                pass
        prefix = b"\n" if needs_leading_newline(path) else b""
        line = prefix + (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        with open(path, "ab") as fh:
            fh.write(line)
            fh.flush()
    except Exception:
        pass
    finally:
        release_lock(lock_fh)


def kill_process_tree(proc):
    """Best-effort process-tree termination without blocking on grandchild pipes."""
    try:
        if os.name == "nt":
            info, flags = hidden_window()
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=0.5, startupinfo=info, creationflags=flags,
                )
            except Exception:
                pass
            try:
                proc.kill()
            except OSError:
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
    finally:
        try:
            proc.wait(timeout=0.2)
        except Exception:
            pass


def is_ordinary_no_head(rc, stderr_text):
    """True for an unborn repo (rc=1, empty stderr) or canonical C-locale non-repo error."""
    first = (stderr_text or "").lstrip().splitlines()[:1]
    head_line = first[0].lower() if first else ""
    if rc == 128 and head_line.startswith("fatal: not a git repository"):
        return True
    return rc == 1 and not head_line


def is_invalid_revision_error(rc, stderr_text):
    """True for C-locale git log errors indicating an invalid or missing snapshot revision."""
    if rc != 128:
        return False
    first = (stderr_text or "").lstrip().splitlines()[:1]
    head_line = first[0].lower() if first else ""
    return head_line.startswith((
        "fatal: invalid revision range",
        "fatal: bad revision",
        "fatal: bad object",
    ))


def commit_object_state(root, oid, deadline):
    """Verify an OID against Git's object store: 'commit', 'missing', or 'error'."""
    if not oid or not OID_RE.fullmatch(oid):
        return "missing"
    out, err, rc, stderr = git_out(
        root, "cat-file", "-t", oid, deadline=deadline, capture_err=True,
    )
    if not err:
        return "commit" if (out or "").strip() == "commit" else "missing"
    if err == "git_nonzero" and rc == 128:
        first = (stderr or "").lstrip().splitlines()[:1]
        head_line = first[0].lower() if first else ""
        if head_line.startswith((
            "fatal: not a valid object name",
            "fatal: git cat-file: could not get object info",
        )):
            return "missing"
    return "error"


def git_out(root, *args, deadline=None, capture_err=False):
    remaining = GIT_DEADLINE_SECONDS if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        return (None, "git_deadline_exceeded", None, "") if capture_err else (None, "git_deadline_exceeded")
    git_bin = shutil.which("git") or "git"
    info, flags = hidden_window()
    git_env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    try:
        with tempfile.TemporaryFile() as out_fh, tempfile.TemporaryFile() as err_fh:
            proc = subprocess.Popen(
                [git_bin, "-C", root, *args],
                stdin=subprocess.DEVNULL,
                stdout=out_fh,
                stderr=err_fh if capture_err else subprocess.DEVNULL,
                env=git_env,
                startupinfo=info, creationflags=flags,
                start_new_session=(os.name != "nt"),
            )
            wait_remaining = GIT_DEADLINE_SECONDS if deadline is None else deadline - time.monotonic()
            if wait_remaining <= 0:
                kill_process_tree(proc)
                return (None, "git_deadline_exceeded", None, "") if capture_err else (None, "git_deadline_exceeded")
            try:
                rc = proc.wait(timeout=wait_remaining)
            except subprocess.TimeoutExpired:
                kill_process_tree(proc)
                return (None, "git_timeout", None, "") if capture_err else (None, "git_timeout")
            if rc != 0:
                err_snippet = ""
                if capture_err:
                    err_fh.seek(0)
                    err_snippet = err_fh.read(512).decode("utf-8", errors="replace")
                return (None, "git_nonzero", rc, err_snippet) if capture_err else (None, "git_nonzero")
            out_fh.seek(0)
            stdout_text = out_fh.read().decode("utf-8", errors="replace")
            return (stdout_text, None, 0, "") if capture_err else (stdout_text, None)
    except OSError:
        return (None, "git_spawn_error", None, "") if capture_err else (None, "git_spawn_error")


def snapshot_path(root, payload):
    key = re.sub(r"[^\w-]", "_", str(payload.get("session_id") or ""))
    return os.path.join(root, ".workflow", "data", "task-hook", key + ".head") if key else None


def write_snapshot(path, head):
    atomic_write_text(path, head)


def retry_marker_path(snap):
    return snap + ".retry" if snap else None


def mark_retry(snap, bound_oid):
    """Record the highest known failed HEAD OID under the snapshot lock."""
    path = retry_marker_path(snap)
    if not path or not bound_oid or not OID_RE.fullmatch(bound_oid):
        return
    try:
        atomic_write_text(path, bound_oid + "\n")
    except OSError:
        pass


def read_retry_bound(snap):
    path = retry_marker_path(snap)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            bound = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return bound if OID_RE.fullmatch(bound) else None


def clear_retry(snap):
    path = retry_marker_path(snap)
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def collect_commits(payload, root, deadline, session):
    """Return (commit_events, pending_snapshot) without advancing .head on disk.

    pending_snapshot is (path, expected_before_or_sentinel, new_head). The caller appends the
    commit batch first and advances the snapshot only after the append succeeds."""
    path = snapshot_path(root, payload)
    if not path:
        return [], None
    hook_event = payload.get("hook_event_name")
    exists = os.path.isfile(path)
    raw_now, err, rc, err_snippet = git_out(
        root, "rev-parse", "--verify", "-q", "HEAD", deadline=deadline, capture_err=True,
    )
    if err:
        # Quiet only for ordinary non-repo or unborn repo before a baseline exists;
        # operational errors (e.g. dubious ownership) are still recorded without stderr text.
        if err != "git_nonzero" or exists or not is_ordinary_no_head(rc, err_snippet):
            log_diagnostic(root, hook_event, session, "git_rev_parse", err)
        return [], None
    now = (raw_now or "").strip()
    if not exists:
        return ([], (path, None, now)) if now else ([], None)
    try:
        with open(path, encoding="utf-8") as fh:
            before = fh.read().strip()
    except UnicodeDecodeError:
        if now and commit_object_state(root, now, deadline) == "commit":
            log_diagnostic(root, hook_event, session, "snapshot", "invalid_snapshot")
            return [], (path, REBASELINE_SNAPSHOT, now)
        mark_retry(path, now)
        log_diagnostic(root, hook_event, session, "git_rev_parse", "git_nonzero")
        return [], None
    except OSError:
        mark_retry(path, now)
        log_diagnostic(root, hook_event, session, "snapshot", "io_error")
        return [], None
    if not now:
        return [], None
    if not OID_RE.fullmatch(before):
        if commit_object_state(root, now, deadline) == "commit":
            log_diagnostic(root, hook_event, session, "snapshot", "invalid_snapshot")
            return [], (path, REBASELINE_SNAPSHOT, now)
        mark_retry(path, now)
        log_diagnostic(root, hook_event, session, "git_rev_parse", "git_nonzero")
        return [], None
    if now == before:
        clear_retry(path)
        return [], None
    span = f"{before}..{now}"
    out, err, log_rc, log_err = git_out(
        root, "log", "--reverse", "--first-parent", "-m", "--format=commit:%H%x09%cI",
        "--name-only", "--relative", span, deadline=deadline, capture_err=True,
    )
    if err or out is None:
        if (
            err == "git_nonzero"
            and is_invalid_revision_error(log_rc, log_err)
            and commit_object_state(root, now, deadline) == "commit"
            and commit_object_state(root, before, deadline) == "missing"
        ):
            log_diagnostic(root, hook_event, session, "snapshot", "invalid_snapshot")
            return [], (path, REBASELINE_SNAPSHOT, now)
        mark_retry(path, now)
        log_diagnostic(root, hook_event, session, "git_log", err or "git_nonzero")
        return [], None
    commits, current = [], None
    for line in out.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("commit:"):
            if current:
                commits.append(current)
            body = text[len("commit:"):]
            oid, _, committed_at = body.partition("\t")
            current = {"kind": "commit", "commit": oid, "paths": []}
            if committed_at:
                current["committed_at"] = committed_at
        elif current:
            current["paths"].append(text)
    if current:
        commits.append(current)
    retry_bound = read_retry_bound(path)
    if retry_bound:
        bound_idx = next((i for i, c in enumerate(commits) if c["commit"] == retry_bound), None)
        if bound_idx is not None:
            for idx in range(bound_idx + 1):
                commits[idx]["delayed"] = True
    return commits, (path, before, now)


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
    atomic_write_text(path, json.dumps(state))


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


def non_git_events_for(payload, root):
    hook = payload.get("hook_event_name")
    if hook == "UserPromptSubmit":
        match = SKILL_PROMPT.match(str(payload.get("prompt") or ""))
        if match and match.group(1) in LOCAL_SKILLS:
            return [{"kind": "skill", "skill": match.group(1)}]
        return []
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


def append_events(root, session, events):
    if not events:
        return
    stream_path = os.path.join(root, ".workflow", "data", "tasks.jsonl")
    os.makedirs(os.path.dirname(stream_path), exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    lines = []
    for event in events:
        row = {
            "v": EVENT_VERSION,
            "at": now,
            "source": "hook",
            "session_id": session,
            **event,
        }
        lines.append(json.dumps(row, ensure_ascii=False) + "\n")
    prefix = b"\n" if needs_leading_newline(stream_path) else b""
    buf = prefix + "".join(lines).encode("utf-8")
    with open(stream_path, "ab") as fh:
        fh.write(buf)
        fh.flush()


def commit_snapshot(pending):
    """Advance .head atomically only if the on-disk cursor still matches expected_before."""
    path, expected_before, new_head = pending
    if expected_before == REBASELINE_SNAPSHOT:
        write_snapshot(path, new_head)
        return
    if expected_before is None:
        if os.path.isfile(path):
            return
    else:
        try:
            with open(path, encoding="utf-8") as fh:
                current = fh.read().strip()
        except (OSError, UnicodeDecodeError):
            return
        if current != expected_before:
            return
    write_snapshot(path, new_head)


try:
    payload = json.loads(globals().get("HOOK_RAW") or os.environ.get("CLAUDE_HOOK_RAW", ""))
    root = project_root(payload.get("cwd"))
    if root:
        hook_event = payload.get("hook_event_name")
        session = main_session(payload.get("session_id"))
        if hook_event == "Stop":
            try:
                message = anchor_warning(payload, root)
            except Exception:
                message = None
            if message:
                print(json.dumps({"systemMessage": message}))
        other_events = non_git_events_for(payload, root)
        if hook_event in ("UserPromptSubmit", "Stop"):
            snap = snapshot_path(root, payload)
            lock_fh = acquire_lock(snap + ".lock") if snap else None
            if snap and lock_fh is None:
                # Contention: preserve independent events and defer Git observation
                # rather than risking concurrent cursor or retry-marker races.
                log_diagnostic(root, hook_event, session, "snapshot", "lock_contention")
                if other_events:
                    try:
                        append_events(root, session, other_events)
                    except OSError:
                        log_diagnostic(root, hook_event, session, "append", "io_error")
            else:
                try:
                    deadline = time.monotonic() + GIT_DEADLINE_SECONDS
                    commit_events, pending_snapshot = collect_commits(payload, root, deadline, session)
                    if pending_snapshot is not None and pending_snapshot[1] in (None, REBASELINE_SNAPSHOT):
                        # Initial baseline or invalid-snapshot recovery has no commit rows;
                        # update .head independently of whether a local skill append succeeds.
                        try:
                            commit_snapshot(pending_snapshot)
                            clear_retry(snap)
                        except OSError:
                            log_diagnostic(root, hook_event, session, "snapshot", "io_error")
                        if other_events:
                            try:
                                append_events(root, session, other_events)
                            except OSError:
                                log_diagnostic(root, hook_event, session, "append", "io_error")
                    else:
                        ordered_events = commit_events + other_events
                        append_ok = True
                        if ordered_events:
                            try:
                                append_events(root, session, ordered_events)
                            except OSError:
                                append_ok = False
                                log_diagnostic(root, hook_event, session, "append", "io_error")
                        if append_ok and pending_snapshot is not None:
                            try:
                                commit_snapshot(pending_snapshot)
                                clear_retry(snap)
                            except OSError:
                                mark_retry(snap, pending_snapshot[2])
                                log_diagnostic(root, hook_event, session, "snapshot", "io_error")
                        elif not append_ok and pending_snapshot is not None:
                            mark_retry(snap, pending_snapshot[2])
                finally:
                    release_lock(lock_fh)
        elif other_events:
            try:
                append_events(root, session, other_events)
            except OSError:
                log_diagnostic(root, hook_event, session, "append", "io_error")
except Exception:
    pass
