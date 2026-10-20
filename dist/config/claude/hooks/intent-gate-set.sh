#!/usr/bin/env bash
# intent-gate-set.sh - launcher for intent-gate-set.py, the same on every OS (Git Bash on Windows).
# Resolves an interpreter and hands this hook's stdin to workflow_hooks.py, which runs
# intent-gate-set.py. The hook's own contract is described at the top of intent-gate-set.py.
# Interpreter: the one install.py pinned in python.path beside this file, the Python the
# requirements went into and the one .workflow/run.sh names (DEC-068). Without that file:
# the first of python3, python, `py -3` that RUNS and is >= 3.10 (utils/osutil.py
# MIN_PYTHON). A name on PATH is not enough: Windows' WindowsApps aliases open the Store
# instead of running, and an old python3 can shadow a newer python. None found: fail open.
# The launcher's own directory, by bash alone (a hook must not need `dirname` on PATH).
# Backslashes become slashes first: Claude Code on Windows passes the path with them.
bs=$'\\'
DIR="${BASH_SOURCE[0]//"$bs"//}"
case "$DIR" in */*) DIR="${DIR%/*}" ;; *) DIR=. ;; esac
PY=()
if [ -f "$DIR/python.path" ]; then
  IFS= read -r pinned < "$DIR/python.path" || true
  cr=$'\r'; pinned="${pinned//"$cr"/}"
  if [ -n "$pinned" ] && [ -f "$pinned" ]; then PY=("$pinned"); fi
fi
if [ "${#PY[@]}" -eq 0 ]; then
  for cand in "python3" "python" "py -3"; do
    set -- $cand
    command -v "$1" >/dev/null 2>&1 || continue
    if "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      PY=("$@"); break
    fi
  done
fi
[ "${#PY[@]}" -eq 0 ] && exit 0
exec "${PY[@]}" "$DIR/workflow_hooks.py" intent-gate-set
