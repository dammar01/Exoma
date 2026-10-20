"""workflow_hooks.py - the one entry point every hook launcher execs.

    <python> workflow_hooks.py <hook>      (stdin: the hook's JSON from Claude Code)

Runs <hook>.py from this directory as __main__ with the payload as the module global
HOOK_RAW and this directory as HOOK_DIR (DEC-068). The payload never goes through the
environment: Windows refuses a variable longer than 32767 characters, so every Edit/Write
or prompt past that size crashed the hook and the gates failed open. stdin is read as bytes
and decoded as UTF-8, the encoding Claude Code writes, whatever the locale code page is.

A hook's exit 2 is this process's exit 2, so it still blocks. Anything else a hook raises,
and any exit other than 0 or 2, is a broken hook, not a decision: it is logged to
~/.claude/hook-errors.log and the hook exits 0, the fail-open every hook already uses for a
missing registry or marker. An empty payload exits 0 without running anything, as the launchers did.

Standalone on purpose: installed into ~/.claude/hooks, it must run with no repository on
disk, so it imports nothing beyond the standard library. One file per hook rather than one
module holding eight: the hooks keep module-level names (lock constants, parsers) that
core/ and the tests read by name.
"""
import datetime
import os
import runpy
import sys
import traceback

HOOKS = (
    "execution-gate",
    "graph-refresh",
    "intent-gate-check",
    "intent-gate-set",
    "session-bind",
    "status-guard",
    "task-events",
    "workflow-statusline",
)
ERROR_LOG = "hook-errors.log"
# One log, kept small: past this size it is cut to its newest half before the next append.
_ERROR_LOG_MAX_BYTES = 256 * 1024


def error_log_path() -> str:
    """~/.claude/hook-errors.log: beside the registry, never beside the hook scripts, so a
    hook run from a checkout (the tests run dist/) writes nothing into the checkout."""
    return os.path.join(os.path.expanduser("~"), ".claude", ERROR_LOG)


def log_error(hook: str) -> None:
    """Append the current exception to hook-errors.log; never raise."""
    try:
        path = error_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > _ERROR_LOG_MAX_BYTES:
            with open(path, "rb") as f:
                f.seek(-_ERROR_LOG_MAX_BYTES // 2, os.SEEK_END)
                tail = f.read()
            with open(path, "wb") as f:
                f.write(tail)
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"--- {stamp} {hook}\n{traceback.format_exc()}\n")
    except Exception:
        pass


def main(argv: list) -> int:
    # Claude Code reads hook output as UTF-8; a cp1252 console must not turn a non-ASCII
    # character in a message into an encode error.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if len(argv) != 2 or argv[1] not in HOOKS:
        # A launcher naming no known hook is a broken install, not a reason to block a
        # prompt or a tool call: say so on stderr and fail open.
        print(f"workflow_hooks: unknown hook {argv[1:]!r}; expected one of {', '.join(HOOKS)}", file=sys.stderr)
        return 0
    hook = argv[1]
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace").lstrip("﻿")
    if not raw.strip():
        return 0
    here = os.path.dirname(os.path.abspath(__file__))
    # HOOK_DIR is a short path; the environment copy stays for hooks run outside this file.
    os.environ["HOOK_DIR"] = here
    try:
        runpy.run_path(os.path.join(here, f"{hook}.py"), init_globals={"HOOK_RAW": raw, "HOOK_DIR": here},
                       run_name="__main__")
    except SystemExit as exc:
        code = exc.code
        if code is None or code in (0, 2):
            return code or 0
        # Any other exit (1, 3, a message string) is a broken hook, not a decision, and
        # Claude Code shows it as a hook error: logged and failed open like an exception.
        log_error(hook)
        print(f"workflow_hooks: {hook} exited {code!r}; see {error_log_path()}", file=sys.stderr)
        return 0
    except Exception:
        log_error(hook)
        print(f"workflow_hooks: {hook} failed; see {error_log_path()}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
