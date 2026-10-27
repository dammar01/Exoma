# status-guard.py - Stop hook (the "never say done before verify" rule).
# Reads the session's runtime/execution.json (core/runtime/execution.py). When an execution
# exists whose verification is still `not_run`, and the assistant message that just ended
# the turn carries an [EXECUTION RESULT] block whose own fields claim more than that — a
# `status` outside implemented|partial|blocked (skills/execute.md), or a `verification`
# other than not_run — the hook reports it as a systemMessage the user sees. Warning only.
# It reads the structured fields the execute skill defines, never prose: a word
# list fired on "begitu selesai, saya lanjut" and on every quoted digest, in any language,
# for any model. A reply with no [EXECUTION RESULT] is not judged.
# The message is `last_assistant_message` when the host sends it, otherwise the last
# assistant entry of `transcript_path`. Anything missing -> exit 0 silently. Never blocks.
import json, os, sys

# The [EXECUTION RESULT] reading lives in workflow_policy.py beside this file, shared with the
# Agy host wrapper; this hook keeps Claude Code's transcript format and systemMessage.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from workflow_policy import claims_completion, read_json, workflow_data_dir  # noqa: E402


def last_assistant_text(payload):
    text = payload.get("last_assistant_message")
    if isinstance(text, str) and text.strip():
        return text
    path = payload.get("transcript_path")
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-200:]
    except Exception:
        return ""
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("type") != "assistant":
            continue
        content = (row.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if any(parts):
                return "\n".join(parts)
    return ""


try:
    raw = globals().get("HOOK_RAW") or os.environ.get("CLAUDE_HOOK_RAW", "")
    if not raw.strip():
        sys.exit(0)
    payload = json.loads(raw)
    claude_sid = payload.get("session_id")
    if not claude_sid:
        sys.exit(0)
    reg = read_json(os.path.join(os.path.expanduser("~"), ".claude", "session_registry.json")) or {}
    entry = reg.get(claude_sid) or {}
    main_id = str(entry.get("main_session_id") or "")
    root = str(entry.get("cwd") or "") or str(payload.get("cwd") or "")
    if not main_id or not root or not os.path.isdir(os.path.join(root, ".workflow")):
        sys.exit(0)
    policies = (read_json(os.path.join(root, ".workflow", "config.json")) or {}).get("policies") or {}
    if str(policies.get("enforcement") or "hybrid") == "off":
        sys.exit(0)
    execution = read_json(os.path.join(workflow_data_dir(root), "sessions", main_id, "runtime", "execution.json"))
    if not execution or execution.get("verification") != "not_run":
        sys.exit(0)
    word = claims_completion(last_assistant_text(payload))
    if word:
        sys.stdout.write(json.dumps({
            "systemMessage": (
                "[STATUS GUARD] the [EXECUTION RESULT] says %r, but plan %s has verification: not_run. "
                "Implemented is not verified — run /.verify before treating this as done."
            ) % (word, execution.get("plan_id")),
        }))
    sys.exit(0)
except SystemExit:
    raise
except Exception:
    sys.exit(0)
