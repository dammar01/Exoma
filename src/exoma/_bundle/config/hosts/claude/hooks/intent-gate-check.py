# intent-gate-check.py - PreToolUse hook (Pre-flight gate: CHECK side).
# Matcher (settings.json): mcp__.*|Read|Grep|Glob|Bash|PowerShell|Write|Edit|MultiEdit|
# NotebookEdit. If a DELEGATED marker is pending (set by intent-gate-set.py, not yet cleared
# by .workflow/run.sh) -> HARD-block: exit 2 with the reason on stderr.
# The decision is workflow_policy.preflight_refusal, shared with the Agy host wrapper; this
# hook maps Claude Code's tools onto its kinds and keeps the exit-2 contract:
#   Write/Edit/MultiEdit/NotebookEdit -> write: passes, except a write of the runner scripts
#     themselves. That one is refused whether or not a marker is pending: the runner is the
#     one command the gate lets through, so rewriting it would make that an arbitrary command.
#   Bash/PowerShell -> shell: one tokeniser for both, one plain call `[bash] <root>/.workflow/
#     {run,check,inspect}.sh ...` whose every word is bare or '...' (single quotes: literal in
#     both shells), so `$`, backtick, ; | & < > and newline pass only inside single quotes. A
#     command may open with `cd <dir inside the root> && `. Under verify, a clean
#     `git [-C <root>] diff --name-only|--name-status|--stat ...` passes too.
#   Read -> read: a skill definition (an existing .md whose real path lies inside
#     ~/.claude/skills) or a background task's output (<temp>/claude.../tasks/<id>.output);
#     under verify, .workflow/config.json and this session's verify/tests.json; and a
#     precision read: a project file outside .workflow, at most 200 lines (`limit`, or a file
#     that short), two per marker, counted in runtime/delegated.reads. `cat <file>`
#     (Get-Content) of a skill or task output passes like its Read; `~` or /c/... spellings
#     of a path are read the way the shell reads them.
#   anything else (Grep, Glob, mcp__*) -> other: blocked while armed.
# Escapes: WORKFLOW_LOCAL_MODE=1 / local_mode.flag. Marker/session absent -> allow (fail-open).
# exit 2 = block ; every other path exits 0. Exit code flows from python (no trailing exit).
import os, sys, json, re, datetime, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import workflow_policy as wp  # noqa: E402
except Exception:
    # A partial install without the shared helper: fail open like every other state this
    # hook cannot read, rather than crash on every tool call.
    sys.exit(0)


def workflow_data_dir(root):
    # Same rule as core/workspace/workspace_paths.data_dir: .workflow/data once it exists,
    # the .workflow root while a v3.5.x-layout workspace still keeps its data there, data/ otherwise.
    wf = os.path.join(root, ".workflow")
    data = os.path.join(wf, "data")
    if os.path.isdir(data):
        return data
    for name in ("sessions", "provider-sessions", "reports", "audit.jsonl", "usage.jsonl", "quality.jsonl", "facts.jsonl", "evidence.jsonl", "redactions.jsonl"):
        if os.path.exists(os.path.join(wf, name)):
            return wf
    return data


# Fail-open leaves no trace, and that is the problem: a hook that dies on a malformed
# registry exits 0 exactly like a hook that found nothing to block, so the enforcement
# layer can be dead for an entire session with nothing to show for it. Record the fault
# and still exit 0 — non-wedging, just no longer silent. Written ONLY on real faults,
# never on the normal allow/block paths, and overwritten rather than appended.
runtime_dir = None


def hook_warning(kind, message):
    try:
        # Session dir when it is known. The fault most worth recording — an unparseable
        # registry — happens BEFORE that dir can be resolved, so a session-only location
        # would miss exactly the case this exists for; ~/.claude is the fallback.
        target = runtime_dir or os.path.join(os.path.expanduser("~"), ".claude")
        if not target:
            return
        os.makedirs(target, exist_ok=True)
        payload = {
            "hook": "intent-gate-check",
            "kind": kind,
            "message": str(message),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with open(
            os.path.join(target, "hook-warning.json"), "w", encoding="utf-8"
        ) as fh:
            json.dump(payload, fh, indent=2)
    except Exception:
        pass


def task_output(candidate, home):
    # True when a Read names the output file of one of Claude Code's own background tasks:
    # <temp>/claude.../<...>/tasks/<id>.output. That is how a delegated run started in the
    # background hands back its result, so blocking it blocked reading the delegation itself.
    # Undocumented layout: if Claude Code moves it, this stops matching and the read is gated
    # again, as before.
    candidate = wp.local_path(candidate, home)
    if not candidate.strip() or not os.path.isabs(candidate):
        return False
    try:
        full = os.path.realpath(candidate)
        if not os.path.isfile(full) or not re.fullmatch(r"[\w.-]+\.output", os.path.basename(full)):
            return False
        parts = re.split(r"[\\/]", os.path.normcase(full))
        if len(parts) < 3 or parts[-2] != "tasks" or not any(p.startswith("claude") for p in parts[:-2]):
            return False
        roots = {os.path.normcase(os.path.realpath(tempfile.gettempdir()))}
        if os.name != "nt":
            roots.add(os.path.normcase(os.path.realpath("/tmp")))
        return any(os.path.normcase(full).startswith(r.rstrip("\\/") + os.sep) for r in roots)
    except Exception:
        return False


WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")


def kind_of(tool_name):
    if tool_name in WRITE_TOOLS:
        return "write"
    if tool_name in SHELL_TOOLS:
        return "shell"
    if tool_name == "Read":
        return "read"
    return "other"


def runner_write_message(cmd, tool_name, target, main_id, local_flag, marker):
    return (
        "[PRE-FLIGHT GATE] '%s' targets a runner script: %s\n"
        "The runner is the one command the pre-flight gate lets through, so it is never rewritten by hand.\n"
        "Do this instead: run bash .workflow/run.sh %s --session '%s' as shipped; /.upgrade regenerates it.\n"
        "False positive? Escapes: export WORKFLOW_LOCAL_MODE=1, create %s, or delete %s.\n"
    ) % (tool_name, target, cmd, main_id, local_flag, marker)


try:
    raw = globals().get("HOOK_RAW") or os.environ.get("CLAUDE_HOOK_RAW", "")
    if not raw.strip():
        sys.exit(0)
    payload = json.loads(raw)
    claude_sid = payload.get("session_id")
    tool_name = str(payload.get("tool_name") or "")
    cwd = payload.get("cwd")
    if not claude_sid:
        sys.exit(0)

    # global env escape
    if os.environ.get("WORKFLOW_LOCAL_MODE") == "1":
        sys.exit(0)

    home = os.path.expanduser("~")
    registry_path = os.path.join(home, ".claude", "session_registry.json")
    if not os.path.isfile(registry_path):
        sys.exit(0)
    with open(registry_path, "r", encoding="utf-8") as f:
        reg = json.load(f) or {}
    entry = reg.get(claude_sid)
    if not entry:
        sys.exit(0)
    main_id = str(entry.get("main_session_id") or "")
    root = str(entry.get("cwd") or "") or str(cwd or "")
    if not main_id or not root:
        sys.exit(0)

    runtime_dir = os.path.join(workflow_data_dir(root), "sessions", main_id, "runtime")
    marker = os.path.join(runtime_dir, "delegated.marker")
    local_flag = os.path.join(runtime_dir, "local_mode.flag")

    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        ti = {}
    kind = kind_of(tool_name)
    target = str(ti.get("file_path") or ti.get("notebook_path") or "")

    # The runner scripts are never written by hand, marker or not: a runner rewritten outside
    # a delegation would pass the gate on the next one as an arbitrary command.
    if kind == "write" and wp.runner_file(target, root):
        sys.stderr.write(runner_write_message("<command>", tool_name, target, main_id, local_flag, marker))
        sys.exit(2)

    if os.path.isfile(local_flag):
        sys.exit(0)
    if not os.path.isfile(marker):
        sys.exit(0)

    cmd = "?"
    marker_obj = {}
    try:
        with open(marker, "r", encoding="utf-8") as f:
            marker_obj = json.load(f) or {}
        cmd = str(marker_obj.get("command") or "?")
    except Exception as exc:
        marker_obj = {}
        hook_warning("marker_unreadable", exc)

    ctx = {
        "root": root,
        "home": home,
        "rt_dir": runtime_dir,
        "is_bash": lambda word: word in ("bash", "bash.exe"),
        "host_read": lambda path: (wp.skill_read(path, home, [os.path.join(home, ".claude", "skills")])
                                   or task_output(path, home)),
        "limit": ti.get("limit"),
    }
    rule = wp.preflight_refusal(kind, target, str(ti.get("command") or ""), marker_obj, ctx)
    if rule is None:
        sys.exit(0)

    what = (
        "a shell read (cat/rg/grep/git show) -- reading the codebase is second_agent's job"
        if kind == "shell"
        else "a bulk-gather tool"
    )
    reason = (
        "[PRE-FLIGHT GATE] intent=DELEGATED (%s) but .workflow/run has NOT run this turn.\n"
        "Tool '%s' is %s -- FORBIDDEN before delegation (Division of Labor: gather = second_agent).\n"
        "%s"
        "Do this instead: bash .workflow/run.sh %s --session '%s' --task '<task>'\n"
        "Single quotes, so $ ; | & < > and brackets in the task are text; no apostrophe inside.\n"
        "That routes evidence to second_agent AND clears this gate.\n"
        "False positive? Escapes: export WORKFLOW_LOCAL_MODE=1, create %s, or delete %s."
    ) % (
        cmd, tool_name, what,
        ("The %d precision reads this delegation allows are used.\n" % wp.PRECISION_READS_PER_MARKER)
        if rule == "preflight_quota"
        else ("A precision read passes: a project file, at most %d lines (`limit`, or a file that short), %d per delegation.\n"
              % (wp.PRECISION_READ_LINES, wp.PRECISION_READS_PER_MARKER)) if kind == "read" else "",
        cmd, main_id, local_flag, marker,
    )
    sys.stderr.write(reason + "\n")
    sys.exit(2)
except SystemExit:
    raise
except Exception as exc:
    # on any hook error, fail-open (never wedge the agent) — but leave the reason behind
    hook_warning("hook_error", exc)
    sys.exit(0)
