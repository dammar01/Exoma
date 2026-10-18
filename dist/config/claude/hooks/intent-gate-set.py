# intent-gate-set.py - UserPromptSubmit hook (Pre-flight gate: SET side).
# Classifies the raw prompt against the DELEGATED NL-map (intent-map.json). Delegated ->
# write .workflow/data/sessions/<MAIN_SESSION_ID>/runtime/delegated.marker ; else delete stale.
# MAIN_SESSION_ID resolved from $HOME/.claude/session_registry.json. Never blocks (exit 0).
import os, sys, json, re, datetime


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


# Prompt `source` values Claude Code uses for turns the user did not type. `user` and `sdk`
# are someone's own words and stay classified.
_HOST_SOURCES = ("system", "loop_wakeup", "schedule_wakeup", "poll_event")
# How a host-generated turn opens once any leading <system-reminder> blocks are removed: a
# background task's completion, and a native subagent's hand-back to the main agent.
_HOST_OPENINGS = ("<task-notification>", "<agent-message", "Another Claude session sent a message")
# Both tags may carry attributes (`<system-reminder id="...">`, and the same on the close).
_REMINDER = re.compile(r"\A\s*<system-reminder\b[^>]*>.*?</system-reminder\b[^>]*>", re.DOTALL)


def not_user_prompt(payload, prompt):
    """True for a turn the user did not write (DEC-045, DEC-059).

    Such a turn quotes task names, agent reports and output excerpts, which match the NL map
    as easily as a request does: a subagent's hand-back armed `verify`, then `analyze`, in the
    middle of an /.execute and blocked the main agent and its subagents alike. The payload's
    own fields decide first; the text shapes cover a host that sends neither field.
    """
    if str(payload.get("source") or "") in _HOST_SOURCES:
        return True
    if payload.get("agent_id"):
        return True
    text = prompt
    while True:
        stripped = _REMINDER.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    text = text.lstrip()
    # Nothing but reminders: the notification sat inside one. Treating that as an empty user
    # prompt would clear the marker the user's own prompt armed.
    if text != prompt.lstrip() and not text:
        return True
    # A notification can also follow the reminder that announces it, inside the same turn.
    if text.startswith("[SYSTEM NOTIFICATION"):
        return True
    return text.startswith(_HOST_OPENINGS)


try:
    raw = os.environ.get("CLAUDE_HOOK_RAW", "")
    if not raw.strip():
        sys.exit(0)
    payload = json.loads(raw)
    # Claude Code sends the prompt as `prompt`; `user_prompt` alone was never set, so the
    # runtime gate never armed.
    prompt = str(payload.get("prompt") or payload.get("user_prompt") or "")
    # A background task's completion or a subagent's hand-back reaches this hook as a prompt,
    # but the user did not write it. It neither arms nor clears a marker; the user's own
    # prompt decides (DEC-045, DEC-059).
    if not_user_prompt(payload, prompt):
        sys.exit(0)
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

    runtime_dir = os.path.join(workflow_data_dir(root), "sessions", main_id, "runtime")
    marker = os.path.join(runtime_dir, "delegated.marker")

    map_path = os.path.join(os.environ.get("HOOK_DIR", ""), "intent-map.json")
    if not os.path.isfile(map_path):
        sys.exit(0)
    with open(map_path, "r", encoding="utf-8") as f:
        mp = json.load(f)

    resolved = None
    prefix_regex = mp.get("prefix_regex")
    m = re.search(prefix_regex, prompt) if prefix_regex else None
    if m:
        resolved = m.group(1)
    else:
        for cmd in mp.get("delegated", []):
            for pat in mp.get("patterns", {}).get(cmd, []):
                if re.search(pat, prompt, re.IGNORECASE):
                    resolved = cmd
                    break
            if resolved:
                break

    if resolved:
        os.makedirs(runtime_dir, exist_ok=True)
        obj = {
            "command": resolved,
            "set_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "claude_sid": claude_sid,
            "prompt_excerpt": prompt[:160],
            # Provenance, so the next false arming names its source instead of leaving
            # only an excerpt (DEC-059).
            "source": payload.get("source"),
            "has_agent_id": bool(payload.get("agent_id")),
            "prompt_id": payload.get("prompt_id"),
        }
        with open(marker, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj))
    else:
        if os.path.isfile(marker):
            try:
                os.remove(marker)
            except Exception:
                pass
    sys.exit(0)
except SystemExit:
    raise
except Exception:
    sys.exit(0)
