#!/usr/bin/env bash
# status-guard.sh - launcher for status-guard.py, the same on every OS (Git Bash on Windows).
# Resolves an interpreter and hands this hook's stdin to workflow_hooks.py, which runs
# status-guard.py. The hook's own contract is described at the top of status-guard.py.
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
# The launcher's own directory, by bash alone (a hook must not need `dirname` on PATH).
# Backslashes become slashes first: Claude Code on Windows passes the path with them.
bs=$'\\'
DIR="${BASH_SOURCE[0]//"$bs"//}"
case "$DIR" in */*) DIR="${DIR%/*}" ;; *) DIR=. ;; esac
exec $PY "$DIR/workflow_hooks.py" status-guard
