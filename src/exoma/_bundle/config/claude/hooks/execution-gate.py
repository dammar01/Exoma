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
import json, os, sys

# The decisions live in workflow_policy.py beside this file, shared with the Agy host
# wrapper; this hook keeps Claude Code's payload parsing and its exit-2 contract.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from workflow_policy import (  # noqa: E402
        commit_refusal, log_refusal as _log_refusal, read_json, runtime_dir as _runtime_dir, write_refusal,
    )
except Exception:
    # A partial install without the shared helper: fail open like every other state this
    # hook cannot read, rather than crash on every tool call.
    sys.exit(0)

# Set once the session is known; refuse() reads it to log the refusal it is about to issue.
CONTEXT = {}


def refuse(mode, rule, message):
    _log_refusal(CONTEXT.get("runtime_dir"), "execution-gate", rule, CONTEXT.get("tool"), mode,
                 mode != "warn", CONTEXT.get("plan_id"))
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
    runtime_dir = _runtime_dir(root, main_id)
    CONTEXT.update(runtime_dir=runtime_dir, tool=tool_name,
                   plan_id=(read_json(os.path.join(runtime_dir, "execution.json")) or {}).get("plan_id"))
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        ti = {}

    if tool_name in ("Bash", "PowerShell"):
        refusal = commit_refusal(str(ti.get("command") or ""), runtime_dir)
        if refusal:
            refuse(mode, *refusal)
        sys.exit(0)

    if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        sys.exit(0)
    target = str(ti.get("file_path") or ti.get("notebook_path") or "")
    if not target:
        sys.exit(0)
    refusal = write_refusal(tool_name, target, payload.get("cwd"), root, policies, runtime_dir,
                            host_dirs=(os.path.join(home, ".claude"),))
    if refusal:
        refuse(mode, *refusal)
    sys.exit(0)
except SystemExit:
    raise
except Exception:
    sys.exit(0)
