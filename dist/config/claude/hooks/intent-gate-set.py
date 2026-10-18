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


try:
    raw = os.environ.get("CLAUDE_HOOK_RAW", "")
    if not raw.strip():
        sys.exit(0)
    payload = json.loads(raw)
    # Claude Code sends the prompt as `prompt`; `user_prompt` alone was never set, so the
    # runtime gate never armed.
    prompt = str(payload.get("prompt") or payload.get("user_prompt") or "")
    # A background task's completion reaches this hook as a prompt, but the user did not
    # write it: its task names and output excerpts matched the NL map and armed the gate
    # (DEC-045). It neither arms nor clears a marker; the user's own prompt decides.
    if prompt.lstrip().startswith("<task-notification>"):
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
