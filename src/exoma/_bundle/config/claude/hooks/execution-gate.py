# execution-gate.py - PreToolUse hook (execution scope, project root, commit approval).
# Matcher (settings.json): Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell.
# The deterministic half of the plan -> execute chain (core/runtime/execution.py):
#   1. A file write outside the session's project root is blocked unless it lands in the
#      system temp dir, under ~/.claude, or under a path the user listed in
#      .workflow/config.json policies.write_allowed_outside_root.
#   2. While an execution is ACTIVE (runtime/execution.json, opened by execute-begin), a
#      write inside the root must fall in the plan's scope (`files`: exact path, directory
#      prefix, or glob). .workflow/ is always writable. With no active execution, writes
#      inside the root pass: memory notes and small fixes the user points to are legitimate.
#   3. `git commit` through Bash/PowerShell needs a runtime/commit.marker younger than 30
#      minutes, which intent-gate-set.py writes only for a user prompt that asks to commit.
# Paths are compared after realpath, so a symlink or junction cannot carry a write past
# the root or scope checks.
# Mode: policies.enforcement = hybrid (default: block) | warn (report, never block) | off.
# Shell commands are not parsed for file writes — a write through a script is outside what
# a PreToolUse hook can see reliably, and claiming otherwise would overstate the gate.
# Session or workspace absent -> allow (fail-open). exit 2 = block; every other path exits 0.
# Every refusal, blocked or warned, appends one line to runtime/gate-blocks.jsonl: when, which
# rule, tool, mode, exit code and plan id; never the path or command.
import datetime, fnmatch, json, os, re, sys, tempfile


def workflow_data_dir(root):
    # Same rule as core/workspace/workspace_paths.data_dir.
    wf = os.path.join(root, ".workflow")
    data = os.path.join(wf, "data")
    if os.path.isdir(data):
        return data
    for name in ("sessions", "provider-sessions", "reports", "audit.jsonl", "usage.jsonl", "quality.jsonl", "facts.jsonl", "evidence.jsonl", "redactions.jsonl"):
        if os.path.exists(os.path.join(wf, name)):
            return wf
    return data


def norm(path):
    # realpath, not abspath: a symlink or junction inside the project that points elsewhere
    # must be judged by where the write lands, or `project/link/x.py` escapes the root
    # while reading as inside it.
    return os.path.normcase(os.path.realpath(path))


def under(path, base):
    """True when `path` is `base` or inside it (both already absolute)."""
    p, b = norm(path), norm(base)
    try:
        return os.path.commonpath([p, b]) == b
    except ValueError:
        return False  # different drives


def in_scope(relative, scope):
    for entry in scope or []:
        entry = str(entry).strip().replace("\\", "/")
        if not entry:
            continue
        stem = entry.rstrip("/")
        if relative == stem or relative.startswith(stem + "/") or fnmatch.fnmatch(relative, entry):
            return True
    return False


# An option's value may be quoted and hold spaces: `git -C "repo with spaces" commit`. The
# executable may be named with its Windows suffix or by a quoted path (`git.exe commit`,
# `"C:/Program Files/Git/cmd/git.exe" commit`); without that, those spellings committed
# with no approval at all.
_ARG = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
_GIT_COMMIT = re.compile(
    r"\bgit(?:\.exe)?\b[\"']?(?:\s+(?:-[cC]\s+" + _ARG + r"|--?[\w-]+(?:=" + _ARG + r")?))*\s+commit\b",
    re.IGNORECASE,
)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# The commit marker is cleared by the next user prompt that does not ask to commit; this
# bound covers the turn that never gets a next prompt. Not measured.
COMMIT_MARKER_TTL_S = 1800


def marker_fresh(path):
    marker = read_json(path)
    if marker is None:
        return False
    try:
        set_at = datetime.datetime.fromisoformat(str(marker.get("set_at")))
    except ValueError:
        return False
    if set_at.tzinfo is None:
        set_at = set_at.replace(tzinfo=datetime.timezone.utc)
    age = (datetime.datetime.now(datetime.timezone.utc) - set_at).total_seconds()
    return 0 <= age <= COMMIT_MARKER_TTL_S


# Set once the session is known; refuse() reads it to log the refusal it is about to issue.
CONTEXT = {}


def log_refusal(rule, mode):
    runtime_dir = CONTEXT.get("runtime_dir")
    if not runtime_dir:
        return
    row = {
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "hook": "execution-gate",
        "rule": rule,
        "tool": CONTEXT.get("tool"),
        "mode": mode,
        "exit": 0 if mode == "warn" else 2,
        "plan_id": CONTEXT.get("plan_id"),
    }
    try:
        os.makedirs(runtime_dir, exist_ok=True)
        with open(os.path.join(runtime_dir, "gate-blocks.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass  # measuring the gate must never change what it decides


def refuse(mode, rule, message):
    log_refusal(rule, mode)
    if mode == "warn":
        sys.stdout.write(json.dumps({"systemMessage": "[EXECUTION GATE: warn] " + message}))
        sys.exit(0)
    sys.stderr.write("[EXECUTION GATE] " + message + "\n")
    sys.exit(2)


try:
    raw = globals().get("HOOK_RAW") or os.environ.get("CLAUDE_HOOK_RAW", "")
    if not raw.strip():
        sys.exit(0)
    payload = json.loads(raw)
    claude_sid = payload.get("session_id")
    tool_name = str(payload.get("tool_name") or "")
    if not claude_sid:
        sys.exit(0)

    home = os.path.expanduser("~")
    reg = read_json(os.path.join(home, ".claude", "session_registry.json")) or {}
    entry = reg.get(claude_sid) or {}
    main_id = str(entry.get("main_session_id") or "")
    root = str(entry.get("cwd") or "") or str(payload.get("cwd") or "")
    if not main_id or not root or not os.path.isdir(os.path.join(root, ".workflow")):
        sys.exit(0)

    policies = (read_json(os.path.join(root, ".workflow", "config.json")) or {}).get("policies") or {}
    mode = str(policies.get("enforcement") or "hybrid")
    if mode == "off":
        sys.exit(0)
    runtime_dir = os.path.join(workflow_data_dir(root), "sessions", main_id, "runtime")
    CONTEXT.update(runtime_dir=runtime_dir, tool=tool_name,
                   plan_id=(read_json(os.path.join(runtime_dir, "execution.json")) or {}).get("plan_id"))
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        ti = {}

    if tool_name in ("Bash", "PowerShell"):
        command = str(ti.get("command") or "")
        if _GIT_COMMIT.search(command) and not marker_fresh(os.path.join(runtime_dir, "commit.marker")):
            refuse(mode, "commit_unapproved", (
                "`git commit` without the user asking for a commit this turn.\n"
                "A commit is approved by the user's own prompt (e.g. /.commit, or asking to commit); "
                "show the proposed message and wait for that prompt.\n"
                "Turn the gate off for this project: policies.enforcement = \"off\" in .workflow/config.json."
            ))
        sys.exit(0)

    if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        sys.exit(0)
    target = str(ti.get("file_path") or ti.get("notebook_path") or "")
    if not target:
        sys.exit(0)
    target = target if os.path.isabs(target) else os.path.join(str(payload.get("cwd") or root), target)

    if not under(target, root):
        allowed = [tempfile.gettempdir(), os.environ.get("TEMP") or "", os.environ.get("TMP") or "",
                   os.path.join(home, ".claude")]
        extra = policies.get("write_allowed_outside_root")
        if isinstance(extra, list):
            allowed += [os.path.expanduser(str(p)) for p in extra if str(p).strip()]
        if any(base and under(target, base) for base in allowed):
            sys.exit(0)
        refuse(mode, "outside_root", (
            "'%s' writes outside the session's project root (%s): %s\n"
            "Writing into another project needs the user's explicit approval for that path.\n"
            "Once given, the user adds it to policies.write_allowed_outside_root in .workflow/config.json."
        ) % (tool_name, root, target))

    relative = os.path.relpath(norm(target), norm(root)).replace("\\", "/")
    if relative == ".workflow" or relative.startswith(".workflow/"):
        sys.exit(0)
    execution = read_json(os.path.join(runtime_dir, "execution.json"))
    if not execution or execution.get("status") != "active":
        sys.exit(0)
    scope = execution.get("scope") or []
    scope_keys = [s.lower() if os.name == "nt" else s for s in scope]
    # The plan names paths as the user sees them. Containment above is decided on the
    # resolved path; scope accepts either spelling, so `linked/x.py` in a plan still
    # matches after `linked/` resolves to another in-root directory.
    lexical = os.path.relpath(
        os.path.normcase(os.path.abspath(target)), os.path.normcase(os.path.abspath(root))
    ).replace("\\", "/")
    if in_scope(relative, scope_keys) or in_scope(lexical, scope_keys):
        sys.exit(0)
    refuse(mode, "outside_scope", (
        "'%s' targets %s, which is outside the active execution's scope (plan %s).\n"
        "Scope: %s\n"
        "Needing it means the plan was incomplete: stop, report the conflict, and ask the user; "
        "a revised plan recorded with plan-record and a new /.execute -y opens the wider scope."
    ) % (tool_name, relative, execution.get("plan_id"), ", ".join(scope) or "(empty)"))
except SystemExit:
    raise
except Exception:
    sys.exit(0)
