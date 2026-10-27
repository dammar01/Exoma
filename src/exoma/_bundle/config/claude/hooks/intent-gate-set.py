# intent-gate-set.py - UserPromptSubmit hook (Pre-flight gate: SET side).
# A DELEGATED command the user named (`/.analyze ...` typed, or the slash-command payload)
# -> write .workflow/data/sessions/<MAIN_SESSION_ID>/runtime/delegated.marker ; else delete
# stale. A prompt that only resembles one under the NL map (intent-map.json) arms nothing:
# it gets an [INTENT HINT] as additionalContext. Pasted content is not classified.
# Also records the user's own approvals beside it: execute.token for `/.execute -y`
# (consumed by `exoma --command execute-begin`) and commit.marker for a turn that asks
# to commit (read by execution-gate.py).
# MAIN_SESSION_ID resolved from $HOME/.claude/session_registry.json. Never blocks (exit 0).
import os, sys, json, datetime

# Approvals, intent classification and the marker live in workflow_policy.py beside this
# file, shared with the Agy host wrapper; this hook keeps what is Claude Code's own: the
# prompt `source` labels, host-generated turns, and additionalContext.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from workflow_policy import (  # noqa: E402
        explicit_command, intent_hint, is_second_agent_turn, load_intent_map, nl_command, record_approvals,
        runtime_dir as _runtime_dir, set_delegated_marker, user_text, without_reminders,
    )
except Exception:
    # A partial install without the shared helper: arm nothing, never block the prompt.
    sys.exit(0)


# Prompt `source` values Claude Code uses for turns the user did not type. `user` and `sdk`
# are someone's own words and stay classified. Neither list is documented by Claude Code, so
# a project extends both with policies.prompt_sources {"user": [...], "host": [...]} in
# .workflow/config.json, and a label in neither is counted in ~/.claude/prompt-sources.json
# An unknown label is treated as the user's, with one limit: an approval
# (execute.token, commit.marker) then needs the command at the start of the prompt.
_HOST_SOURCES = ("system", "loop_wakeup", "schedule_wakeup", "poll_event")
_USER_SOURCES = ("user", "sdk")
# How a host-generated turn opens once any leading <system-reminder> blocks are removed: a
# background task's completion, and a native subagent's hand-back to the main agent.
_HOST_OPENINGS = ("<task-notification>", "<agent-message", "Another Claude session sent a message")


def prompt_sources(root):
    """(user, host) `source` labels: the shipped ones plus policies.prompt_sources."""
    user, host = set(_USER_SOURCES), set(_HOST_SOURCES)
    try:
        with open(os.path.join(root, ".workflow", "config.json"), "r", encoding="utf-8-sig") as f:
            extra = ((json.load(f) or {}).get("policies") or {}).get("prompt_sources") or {}
        user |= {str(s) for s in extra.get("user") or []}
        host |= {str(s) for s in extra.get("host") or []}
    except Exception:
        pass
    return user - host, host


def note_unknown_source(home, label):
    """Count a `source` label in neither list, so the next host version shows up."""
    path = os.path.join(home, ".claude", "prompt-sources.json")
    try:
        try:
            with open(path, "r", encoding="utf-8") as f:
                seen = json.load(f) or {}
        except Exception:
            seen = {}
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        row = seen.get(label) or {"count": 0, "first_seen": now}
        row.update(count=int(row.get("count") or 0) + 1, last_seen=now)
        seen[label] = row
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(seen, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def not_user_prompt(payload, prompt, host_sources=_HOST_SOURCES):
    """True for a turn the user did not write.

    Such a turn quotes task names, agent reports and output excerpts, which match the NL map
    as easily as a request does: a subagent's hand-back armed `verify`, then `analyze`, in the
    middle of an /.execute and blocked the main agent and its subagents alike. The payload's
    own fields decide first; the text shapes cover a host that sends neither field.
    """
    if str(payload.get("source") or "") in host_sources:
        return True
    if payload.get("agent_id"):
        return True
    text = without_reminders(prompt)
    # Nothing but reminders: the notification sat inside one. Treating that as an empty user
    # prompt would clear the marker the user's own prompt armed.
    if text != prompt.lstrip() and not text:
        return True
    # A notification can also follow the reminder that announces it, inside the same turn.
    if text.startswith("[SYSTEM NOTIFICATION"):
        return True
    return text.startswith(_HOST_OPENINGS)


try:
    raw = globals().get("HOOK_RAW") or os.environ.get("CLAUDE_HOOK_RAW", "")
    if not raw.strip():
        sys.exit(0)
    payload = json.loads(raw)
    # Claude Code sends the prompt as `prompt`; `user_prompt` alone was never set, so the
    # runtime gate never armed.
    prompt = str(payload.get("prompt") or payload.get("user_prompt") or "")
    claude_sid = payload.get("session_id")
    cwd = payload.get("cwd")
    if not claude_sid:
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

    # A background task's completion or a subagent's hand-back reaches this hook as a prompt,
    # but the user did not write it. It neither arms nor clears a marker; the user's own
    # prompt decides.
    user_sources, host_sources = prompt_sources(root)
    if not_user_prompt(payload, prompt, host_sources):
        sys.exit(0)
    # Claude Code running as some host's second agent: the runtime's call, not the user's turn.
    if is_second_agent_turn():
        sys.exit(0)
    source = str(payload.get("source") or "")
    unknown_source = bool(source) and source not in user_sources
    if unknown_source:
        note_unknown_source(home, source)
    text = user_text(prompt)
    runtime_dir = _runtime_dir(root, main_id)
    record_approvals(text, runtime_dir, unknown_source,
                     {"claude_sid": claude_sid, "prompt_id": payload.get("prompt_id")})

    mp = load_intent_map(globals().get("HOOK_DIR") or os.environ.get("HOOK_DIR", ""))
    if mp is None:
        sys.exit(0)

    # Only a command the user named arms the gate. A sentence that resembles one under the NL
    # map ("saat saya analisa...", a question that mentions playwright) is not a request to
    # delegate; reading it as one blocked the main agent on its own reply. It gets
    # a hint instead, which the main agent weighs like any other context.
    resolved = explicit_command(text, mp.get("prefix_regex"))
    hinted = None if resolved else nl_command(text, mp)
    if hinted:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": intent_hint(hinted, root, main_id),
            }
        }))
    set_delegated_marker(resolved, runtime_dir, {
        "claude_sid": claude_sid,
        "prompt_excerpt": prompt[:160],
        # Provenance, so the next false arming names its source instead of leaving
        # only an excerpt.
        "source": payload.get("source"),
        "has_agent_id": bool(payload.get("agent_id")),
        "prompt_id": payload.get("prompt_id"),
    })
    sys.exit(0)
except SystemExit:
    raise
except Exception:
    sys.exit(0)
