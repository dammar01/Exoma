# intent-gate-set.py - UserPromptSubmit hook (Pre-flight gate: SET side).
# A DELEGATED command the user named (`/.analyze ...` typed, or the slash-command payload)
# -> write .workflow/data/sessions/<MAIN_SESSION_ID>/runtime/delegated.marker ; else delete
# stale. A prompt that only resembles one under the NL map (intent-map.json) arms nothing:
# it gets an [INTENT HINT] as additionalContext. Pasted content is not classified.
# Also records the user's own approvals beside it: execute.token for `/.execute -y`
# (consumed by `exoma --command execute-begin`) and commit.marker for a turn that asks
# to commit (read by execution-gate.py).
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
# Both tags may carry attributes (`<system-reminder id="...">`, and the same on the close).
_REMINDER = re.compile(r"\A\s*<system-reminder\b[^>]*>.*?</system-reminder\b[^>]*>", re.DOTALL)


# Pasted text arrives inside the prompt. It is material the user brought, not their words:
# a pasted log that says "commit" or "analisa" asks for nothing.
_PASTED = re.compile(r"<pasted_content\b[^>]*>.*?</pasted_content\b[^>]*>", re.DOTALL)


def without_reminders(prompt):
    """The prompt with its leading <system-reminder> blocks removed, left-stripped."""
    text = prompt
    while True:
        stripped = _REMINDER.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return text.lstrip()


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


# `/.execute -y` typed as text, or as the expanded slash command Claude Code submits.
_EXECUTE_YES = re.compile(
    r"(?:^|\s)/\.execute\s+-y\b|<command-name>/?\.execute</command-name>\s*<command-args>\s*-y\b",
    re.IGNORECASE,
)
_COMMIT = re.compile(r"(?:^|\s)/\.commit\b|\bcommit\w*\b", re.IGNORECASE)
# "jangan commit", "don't commit", "tanpa commit": naming the act is not asking for it.
_COMMIT_NEGATED = re.compile(
    r"\b(?:jangan|tanpa|belum|tidak|bukan|don'?t|do\s+not|no|never|without)\b[^.\n]{0,20}\bcommit",
    re.IGNORECASE,
)


# The skill asks "Jalankan? (yes/no)" after proposing the message, so the commit itself
# happens a turn after the request. A bare yes keeps the approval that turn already holds;
# it never creates one.
_AFFIRM = re.compile(
    r"^\s*(?:yes|ya|y|iya|ok|oke|okay|gas|lanjut|boleh|setuju|jalankan|commit)\b[\s.!]*$",
    re.IGNORECASE,
)


# The same two approvals, held to the start of the prompt: what a turn from an unknown
# `source` must look like to approve anything. The slash-command payload counts wherever it
# sits, since Claude Code, not the text, puts the tag there.
_EXECUTE_AT_START = re.compile(r"/\.execute\s+-y\b", re.IGNORECASE)
_COMMIT_AT_START = re.compile(r"(?:/\.commit\b|commit\w*\b)", re.IGNORECASE)
_EXECUTE_TAG = re.compile(r"<command-name>/?\.execute</command-name>\s*<command-args>\s*-y\b", re.IGNORECASE)
_COMMIT_TAG = re.compile(r"<command-name>/?\.commit</command-name>", re.IGNORECASE)

# A delegated command the user named: typed with its prefix, or the slash-command payload.
# Only these arm the gate. A sentence that merely matches the NL map gets a hint.
_DELEGATED = "explore|plan|analyze|verify-browser|verify"
_COMMAND_TAG = re.compile(r"<command-name>/?\.(%s)</command-name>" % _DELEGATED, re.IGNORECASE)


def execute_approved(prompt, anchored=False):
    if anchored:
        return bool(_EXECUTE_AT_START.match(prompt or "") or _EXECUTE_TAG.search(prompt or ""))
    return bool(_EXECUTE_YES.search(prompt or ""))


def commit_requested(prompt, anchored=False):
    text = prompt or ""
    asked = (_COMMIT_AT_START.match(text) or _COMMIT_TAG.search(text)) if anchored else _COMMIT.search(text)
    return bool(asked) and not _COMMIT_NEGATED.search(text)


def explicit_command(text, prefix_regex):
    """The delegated command the user named explicitly, or None."""
    m = re.search(prefix_regex, text) if prefix_regex else None
    if m:
        return m.group(1)
    m = _COMMAND_TAG.search(text)
    return m.group(1).lower() if m else None


def nl_command(text, mp):
    """The delegated command a sentence resembles under the NL map, or None."""
    for cmd in mp.get("delegated", []):
        for pat in mp.get("patterns", {}).get(cmd, []):
            if re.search(pat, text, re.IGNORECASE):
                return cmd
    return None


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
    source = str(payload.get("source") or "")
    unknown_source = bool(source) and source not in user_sources
    if unknown_source:
        note_unknown_source(home, source)
    # What the user wrote: pasted material out, leading reminders out.
    text = without_reminders(_PASTED.sub(" ", prompt))

    runtime_dir = os.path.join(workflow_data_dir(root), "sessions", main_id, "runtime")
    marker = os.path.join(runtime_dir, "delegated.marker")

    # The user's own approvals, as files the runtime and execution-gate can check
    # (core/runtime/execution.py). Written before the NL map is read: approval does not
    # depend on intent classification, and a missing map must not drop a `-y`.
    approvals = (
        ("execute.token", execute_approved(text, anchored=unknown_source)),
        ("commit.marker", commit_requested(text, anchored=unknown_source)),
    )
    for name, given in approvals:
        path = os.path.join(runtime_dir, name)
        if name == "commit.marker" and not given and os.path.isfile(path) and _AFFIRM.match(text):
            continue  # the yes to a commit this session just proposed
        if given:
            os.makedirs(runtime_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "set_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "claude_sid": claude_sid,
                    "prompt_id": payload.get("prompt_id"),
                }))
        elif name == "commit.marker" and os.path.isfile(path):
            # A commit is approved for the turn the user asked for it, not for the rest of
            # the session. The execute token is consumed by execute-begin instead, and
            # expires on its own.
            try:
                os.remove(path)
            except Exception:
                pass

    map_path = os.path.join(globals().get("HOOK_DIR") or os.environ.get("HOOK_DIR", ""), "intent-map.json")
    if not os.path.isfile(map_path):
        sys.exit(0)
    with open(map_path, "r", encoding="utf-8") as f:
        mp = json.load(f)

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
                "additionalContext": (
                    "[INTENT HINT] This prompt resembles /.%s under the workflow NL map. If it "
                    "is that request, delegate first: bash '%s/.workflow/run.sh' %s '<task>' '%s'. "
                    "If it is a question or a remark, answer it. Nothing is blocked: the gate "
                    "arms only on an explicit /.%s."
                ) % (hinted, root.replace("\\", "/"), hinted, main_id, hinted),
            }
        }))

    if resolved:
        os.makedirs(runtime_dir, exist_ok=True)
        obj = {
            "command": resolved,
            "set_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "claude_sid": claude_sid,
            "prompt_excerpt": prompt[:160],
            # Provenance, so the next false arming names its source instead of leaving
            # only an excerpt.
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
