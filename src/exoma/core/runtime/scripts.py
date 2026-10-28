"""Generation and drift-detection for the .workflow/ run scripts."""

from exoma.core.workspace.workspace_paths import _LEGACY_MARKERS, WORKFLOW_DIRNAME
from exoma.core.workspace.workspace_paths import atomic_write_text
import os
from exoma.utils import osutil
from pathlib import Path

# Commands that write one session's state: delegated calls (lock, logs, evidence), the sweep
# report, and the plan -> execute chain. Without an explicit session they would land in a
# session nobody bound, so run.sh and the CLI refuse `default` for them. doctor, init,
# upgrade, clean and provider work on the project; inspect only reads (inspect.sh runs it
# with no session at all).
SESSION_REQUIRED = (
    "explore", "plan", "analyze", "verify", "verify-browser",
    "sweep", "plan-record", "execute-begin", "execute-end",
)


def _build_run_scripts(project_root: Path) -> list[tuple[Path, str]]:
    """Compose run/inspect/check scripts so main_agent calls one script.

    Building is separated from writing so doctor can compare what is on disk against what
    this function would produce — a script that drifted out of step with the generator is
    invisible to a check that only asks whether the file exists.

    One flavour on every OS: bash scripts (Git Bash on Windows) that exec the interpreter
    running this generator, by absolute path, as `-m exoma`. The directory holding the
    package goes first on PYTHONPATH, so the scripts reach this build whether it is
    installed into that interpreter or run from a source checkout. Paths are written with
    forward slashes: Windows python and Git Bash both read `C:/x`, and a forward-slash path
    holds nothing bash treats as special inside double quotes.
    """
    from exoma.core.workspace.workspace_paths import _tool_paths

    tool = _tool_paths()
    sh_py = Path(tool["python"]).as_posix()
    package_root = Path(tool["package_root"]).as_posix()
    # The interpreter's separator, not bash's: Windows python splits PYTHONPATH on `;`.
    python_path = f'export PYTHONPATH="{package_root}${{PYTHONPATH:+{os.pathsep}$PYTHONPATH}}"\n'
    # Resolved, like the CLI resolves --work-dir: one directory reached through two
    # spellings (macOS /var -> /private/var) must generate one text, or every caller that
    # passes the other spelling sees drift that is not there (CASE-029).
    root = project_root.resolve().as_posix()
    workflow_dir = project_root / WORKFLOW_DIRNAME
    sh_markers = " ".join(_LEGACY_MARKERS)

    session_required = " ".join(SESSION_REQUIRED)
    usage = "usage: run.sh <command> --session <id> [--task <text> | --task-file <path>]"
    # Background (job) commands go through await+job-command; the rest run directly.
    run_sh = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'COMMAND="${1:?command required}"\n'
        "shift\n"
        'TASK=""; TASK_FILE=""; SESSION="${MAIN_SESSION_ID:-}"\n'
        # Named options. Positions shift when a shell drops an empty argument — Windows
        # PowerShell 5.1 passes '' to a native program as nothing at all, so the session id
        # of `run.sh execute-begin '' '<id>'` arrived as the task and the call ran on
        # `default` (CASE-033). A missing option cannot move another one.
        'case "${1:-}" in\n'
        "  --*)\n"
        "    while [ $# -gt 0 ]; do\n"
        '      case "$1" in\n'
        "        --session|--task|--task-file)\n"
        # A value that is itself an option name is the shape a dropped empty value leaves
        # (`--task '' --session x` arriving as `--task --session x`): refused, not taken.
        f'          case "${{2-}}" in --session|--task|--task-file) set -- "$1" ;; esac\n'
        f'          if [ $# -lt 2 ]; then echo "[workflow] ERROR: $1 needs a value; {usage}" >&2; exit 2; fi\n'
        '          case "$1" in --session) SESSION="$2" ;; --task) TASK="$2" ;; --task-file) TASK_FILE="$2" ;; esac\n'
        "          shift 2 ;;\n"
        f'        *) echo "[workflow] ERROR: unknown option \'$1\'; {usage}" >&2; exit 2 ;;\n'
        "      esac\n"
        "    done ;;\n"
        # The positional form every release up to 0.3.x documented: run.sh <command>
        # [<task>] [<session>]. Kept so a caller written for it still works; a task that
        # itself opens with `--` needs the named form.
        "  *)\n"
        '    TASK="${1:-}"; SESSION="${2:-$SESSION}" ;;\n'
        "esac\n"
        'if [ -n "$TASK" ] && [ -n "$TASK_FILE" ]; then\n'
        f'  echo "[workflow] ERROR: pass --task or --task-file, not both; {usage}" >&2; exit 2\n'
        "fi\n"
        'SESSION="${SESSION:-default}"\n'
        # session=default on a command that reads or writes session state is not a
        # warning-shaped problem: two main agents on one project silently share a lock,
        # state and logs, and a plan or an execution lands in a session nobody bound.
        # Printing to stderr and proceeding was invisible exactly when it fired — the caller
        # runs this as a background task and never reads that stream — so the script
        # refuses. The CLI refuses the local ones of the same list itself (cli.main).
        'if [ "$SESSION" = "default" ] && [ -z "${EXOMA_ALLOW_DEFAULT_SESSION:-${AI_PROXY_ALLOW_DEFAULT_SESSION:-}}" ]; then\n'
        f'  case " {session_required} " in\n'
        '    *" $COMMAND "*)\n'
        '      echo "[workflow] ERROR: no session for \'$COMMAND\'. It reads or writes session state, and without --session it would land in a session nobody bound; concurrent main agents would share one lock, state and log directory. Pass --session with MAIN_SESSION_ID (the value from the [SESSION BINDING] block). To override deliberately: set EXOMA_ALLOW_DEFAULT_SESSION=1." >&2\n'
        "      exit 2 ;;\n"
        "  esac\n"
        "fi\n"
        'case " explore plan analyze verify verify-browser " in\n'
        '  *" $COMMAND "*)\n'
        # Pre-flight gate: dispatching a delegated run satisfies the gate -> clear the marker
        # so the PreToolUse hook stops blocking gather tools for the rest of this turn. The
        # data directory is decided when the script RUNS, by the rule workspace_paths.data_dir
        # uses, so a workspace migrated after generation clears the marker where hooks write.
        f'    WF="{root}/.workflow"; DD="$WF/data"\n'
        f'    if [ ! -d "$DD" ]; then for n in {sh_markers}; do if [ -e "$WF/$n" ]; then DD="$WF"; break; fi; done; fi\n'
        '    MK="$DD/sessions/$SESSION/runtime/delegated.marker"\n'
        '    [ -f "$MK" ] && rm -f "$MK"\n'
        '    ARGS=(-m exoma --command await --job-command "$COMMAND") ;;\n'
        "  *)\n"
        '    ARGS=(-m exoma --command "$COMMAND") ;;\n'
        "esac\n"
        # No task, no --prompt: local commands do not take one, and an empty value reaches
        # argparse as a bare flag. A task file keeps the task out of every shell's quoting:
        # the CLI reads it as UTF-8 (--prompt-file). One `--opt=value` word each: a separate
        # value that opens with `-` (a task like `--help` or `-x`) is read by argparse as an
        # option, and the call failed with `expected one argument`.
        'if [ -n "$TASK" ]; then ARGS+=("--prompt=$TASK"); fi\n'
        'if [ -n "$TASK_FILE" ]; then ARGS+=("--prompt-file=$TASK_FILE"); fi\n'
        f'ARGS+=(--session "$SESSION" --work-dir "{root}" --pretty)\n'
        + python_path
        + f'exec "{sh_py}" "${{ARGS[@]}}"\n'
    )
    inspect_sh = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        + python_path
        + f'exec "{sh_py}" -m exoma --command inspect --work-dir "{root}" --pretty\n'
    )
    # Attach to an existing job by id (recovery after a foreground timeout). Passes through
    # flags like --wait --result to `exoma check`, which polls without spawning a new run.
    check_sh = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        + python_path
        + f'exec "{sh_py}" -m exoma check "$@"\n'
    )

    return [
        (workflow_dir / "run.sh", run_sh),
        (workflow_dir / "inspect.sh", inspect_sh),
        (workflow_dir / "check.sh", check_sh),
    ]

def _read_script(path: Path) -> str | None:
    """Current on-disk text, or None when it is missing or unreadable.

    utf-8-sig strips a BOM if present and is harmless when absent, so every comparison
    against generated content is about the content, never about how a previous writer
    (or an editor) chose to encode it.
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None

def _retired_scripts(project_root: Path) -> list[Path]:
    """`.ps1` entry scripts an earlier release generated, still sitting in .workflow/.

    The runner is bash on every OS now. A leftover run.ps1 looks usable and is not: no
    generator maintains it, and the gate no longer passes a call to it. Left for the
    caller to delete rather than silently repaired here.
    """
    workflow_dir = project_root / WORKFLOW_DIRNAME
    return [
        path
        for path in (workflow_dir / f"{stem}.ps1" for stem in ("run", "inspect", "check"))
        if path.exists()
    ]

def script_drift(project_root: Path) -> list[dict]:
    """Scripts on disk that no longer match what the generator produces.

    Each entry is {'script', 'state'} with state 'missing', 'content_differs', or
    'retired_leftover' (a .ps1 an earlier release generated). A drifted script keeps
    working right up until the CLI it calls changes shape underneath it — the on-disk run.sh routed `sweep` through `--job-command`
    for a whole release cycle after the generator stopped doing so, because nothing
    compared the two.
    """
    drifted: list[dict] = []
    for path, content in _build_run_scripts(project_root):
        current = _read_script(path)
        if current is None:
            drifted.append({"script": path.name, "state": "missing"})
        elif current != content:
            drifted.append({"script": path.name, "state": "content_differs"})
    drifted.extend(
        {"script": path.name, "state": "retired_leftover"}
        for path in _retired_scripts(project_root)
    )
    return drifted

def _generate_run_scripts(project_root: Path) -> list[str]:
    """Write the scripts _build_run_scripts composes; return the paths actually rewritten.

    Also deletes the .ps1 scripts an earlier release generated: keeping a script no
    generator maintains is worse than not having one, because it looks usable.
    """
    written: list[str] = []
    for path in _retired_scripts(project_root):
        try:
            path.unlink()
            written.append(f"removed {path}")
        except OSError:
            pass  # not ours to force; doctor keeps reporting it
    for path, content in _build_run_scripts(project_root):
        if _read_script(path) == content:
            continue
        atomic_write_text(path, content)  # plain UTF-8: a BOM breaks the shebang
        osutil.make_executable(path)
        written.append(str(path))
    return written
