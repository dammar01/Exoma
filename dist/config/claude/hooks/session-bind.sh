#!/usr/bin/env bash
# session-bind.sh - SessionStart hook (POSIX parity of session-bind.ps1)
# Maps Claude Code session lifecycle -> second_agent (opencode) MAIN_SESSION_ID.
#   startup | clear | compact  -> NEW  MAIN_SESSION_ID
#   resume                     -> REUSE MAIN_SESSION_ID
# Registry: $HOME/.claude/session_registry.json (key = claude session_id)
# Output: JSON hookSpecificOutput.additionalContext -> injects MAIN_SESSION_ID.
# Never blocks session start (always exit 0). JSON via python (bash 3.2 safe).
RAW="$(cat)"
[ -z "$RAW" ] && exit 0
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
CLAUDE_HOOK_RAW="$RAW" $PY <<'PY'
import os, sys, json, datetime, random

try:
    raw = os.environ.get("CLAUDE_HOOK_RAW", "")
    if not raw.strip():
        sys.exit(0)
    payload = json.loads(raw)
    source = payload.get("source")
    claude_sid = payload.get("session_id")
    cwd = payload.get("cwd") or os.getcwd()
    # Recorded, not derived. The benchmark harvester needs to find this session's
    # transcript after the fact, and reconstructing the path means guessing how Claude
    # Code names its project directories. The hook is handed the real one.
    transcript_path = payload.get("transcript_path")
    try:
        root = os.path.realpath(cwd)
    except Exception:
        root = cwd
    slug = os.path.basename(root.rstrip("/")) or "root"

    home = os.path.expanduser("~")
    reg_dir = os.path.join(home, ".claude")
    registry_path = os.path.join(reg_dir, "session_registry.json")

    registry = {}
    if os.path.isfile(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f) or {}
        except Exception:
            registry = {}

    reuse = False
    main_id = None
    if source == "resume" and claude_sid and claude_sid in registry:
        main_id = registry[claude_sid].get("main_session_id")
        reuse = True
    if not main_id:
        now = datetime.datetime.now()
        ts = now.strftime("%Y%m%d_%H%M%S") + "%03d" % (now.microsecond // 1000)
        rand = "".join(random.choice("0123456789abcdef") for _ in range(4))
        main_id = "main_%s_%s_%s" % (slug, ts, rand)
        reuse = False

    bound_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if claude_sid:
        registry[claude_sid] = {
            "main_session_id": main_id,
            "cwd": root,
            "bound_at": bound_at,
            "source": source,
            "transcript_path": transcript_path,
        }

    # prune to newest 50 by bound_at
    try:
        items = sorted(
            registry.items(),
            key=lambda kv: (kv[1] or {}).get("bound_at", ""),
            reverse=True,
        )[:50]
        registry = dict(items)
    except Exception:
        pass

    try:
        os.makedirs(reg_dir, exist_ok=True)
        with open(registry_path, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2)
    except Exception:
        pass

    verb = "REUSE (continue)" if reuse else "NEW"
    ctx = (
        "[SESSION BINDING - authoritative]\n"
        "MAIN_SESSION_ID=%s\n"
        "MAIN_SESSION_PROJECT_ROOT=%s\n"
        "source=%s\n"
        "second_agent_thread=%s\n"
        "Use this MAIN_SESSION_ID for all /.explore /.plan /.analyze /.verify /.sweep "
        "invocations. Overrides .workflow/state.json session.id."
    ) % (main_id, root, source, verb)

    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }
    sys.stdout.write(json.dumps(out))
    sys.exit(0)
except SystemExit:
    raise
except Exception:
    sys.exit(0)
PY
exit 0
