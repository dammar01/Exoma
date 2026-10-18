"""workflow_hooks.py - the one entry point every hook launcher execs.

    <python> workflow_hooks.py <hook>      (stdin: the hook's JSON from Claude Code)

Runs <hook>.py from this directory as __main__, handing it the payload the way the hooks
have always read it: CLAUDE_HOOK_RAW (CLAUDE_STATUSLINE_RAW for the statusline) and
HOOK_DIR. The hook's exit code is this process's exit code, so intent-gate-check's exit 2
still blocks. An empty payload exits 0 without running anything, as the launchers did.

Standalone on purpose: installed into ~/.claude/hooks, it must run with no repository on
disk, so it imports nothing beyond the standard library. One file per hook rather than one
module holding six: the hooks keep module-level names (lock constants, parsers) that
core/ and the tests read by name.
"""
import os
import runpy
import sys

HOOKS = (
    "graph-refresh",
    "intent-gate-check",
    "intent-gate-set",
    "session-bind",
    "task-events",
    "workflow-statusline",
)
_PAYLOAD_ENV = {"workflow-statusline": "CLAUDE_STATUSLINE_RAW"}


def main(argv: list) -> int:
    if len(argv) != 2 or argv[1] not in HOOKS:
        # A launcher naming no known hook is a broken install, not a reason to block a
        # prompt or a tool call: say so on stderr and fail open.
        print(f"workflow_hooks: unknown hook {argv[1:]!r}; expected one of {', '.join(HOOKS)}", file=sys.stderr)
        return 0
    hook = argv[1]
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    here = os.path.dirname(os.path.abspath(__file__))
    os.environ[_PAYLOAD_ENV.get(hook, "CLAUDE_HOOK_RAW")] = raw
    os.environ["HOOK_DIR"] = here
    try:
        runpy.run_path(os.path.join(here, f"{hook}.py"), run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
