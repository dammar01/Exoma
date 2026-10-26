# session-bind.py - SessionStart hook
# Maps Claude Code session lifecycle -> second_agent (opencode) MAIN_SESSION_ID.
#   startup | clear | compact  -> NEW  MAIN_SESSION_ID
#   fork                       -> NEW  MAIN_SESSION_ID (/branch, /fork, --fork-session: a new
#                                 conversation id with its own runtime state)
#   resume                     -> REUSE MAIN_SESSION_ID
# Registry: $HOME/.claude/session_registry.json (key = claude session_id)
# Output: JSON hookSpecificOutput.additionalContext -> injects MAIN_SESSION_ID.
# Never blocks session start (always exit 0). JSON via python.
import os, sys, json, datetime, random, time

# The registry is read, changed and replaced as one step under a sidecar lock file, so two
# sessions starting together cannot each write back a registry missing the other's binding
# O_CREAT|O_EXCL behaves the same on every OS and needs nothing beyond the
# standard library. The lock holds its owner's token: a holder removes only its own lock,
# and a lock older than LOCK_STALE_S (its holder died) is removed only while it still holds
# the token judged stale. What is left is the instant between that last check and the
# removal; a lock file cannot close it, only an OS lock could (the choice is deliberate).
# After LOCK_WAIT_S the bind goes ahead unlocked rather than delay the session start.
LOCK_WAIT_S = 2.0
LOCK_STALE_S = 10.0


def read_token(lock_path):
    try:
        with open(lock_path, "r", encoding="ascii", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def acquire(lock_path):
    """The token of the lock this process now holds, or None after LOCK_WAIT_S."""
    token = "%d-%s" % (os.getpid(), "".join(random.choice("0123456789abcdef") for _ in range(12)))
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            seen = read_token(lock_path)
            try:
                stale = time.time() - os.path.getmtime(lock_path) > LOCK_STALE_S
            except OSError:
                stale = False
            if stale and seen is not None and read_token(lock_path) == seen:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
                continue
        except OSError:
            # Windows refuses to create a file whose previous copy is still being deleted
            # (PermissionError): the lock is busy, not broken, so wait like for any other.
            pass
        else:
            try:
                try:
                    os.write(fd, token.encode("ascii"))
                finally:
                    os.close(fd)
            except OSError:
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
                return None
            return token
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.02)


def release(lock_path, token):
    """Remove the lock only while it is still this process's own."""
    if token and read_token(lock_path) == token:
        try:
            os.remove(lock_path)
        except OSError:
            pass


lock_path = None
lock_token = None
try:
    raw = globals().get("HOOK_RAW") or os.environ.get("CLAUDE_HOOK_RAW", "")
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

    try:
        os.makedirs(reg_dir, exist_ok=True)
    except Exception:
        pass
    lock_path = registry_path + ".lock"
    lock_token = acquire(lock_path)
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
        # Replaced, not rewritten in place: two sessions starting together must not leave
        # a half-written registry that every other hook then reads as empty.
        tmp = "%s.%d.tmp" % (registry_path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(registry, f, indent=2)
        os.replace(tmp, registry_path)
    except Exception:
        pass
    finally:
        if lock_token:
            release(lock_path, lock_token)
            lock_token = None

    verb ="REUSE (continue)" if reuse else "NEW"
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
finally:
    if lock_token and lock_path:
        release(lock_path, lock_token)
