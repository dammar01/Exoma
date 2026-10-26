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
import json, os, re, sys


def workflow_data_dir(root):
    # Same rule as core/workspace/workspace_paths.data_dir.
    wf = os.path.join(root, ".workflow")
    data = os.path.join(wf, "data")
    if os.path.isdir(data):
        return data
    for name in ("sessions", "provider-sessions", "reports", "audit.jsonl", "usage.jsonl", "quality.jsonl", "facts.jsonl", "evidence.jsonl", "redactions.jsonl"):
        if os.path.exists(os.path.join(wf, name)):
            return wf
    return data


# The values skills/execute.md allows while verification is not_run. Anything else in the
# status field ("done") claims what only /.verify can establish.
UNVERIFIED_STATUSES = {"implemented", "partial", "blocked"}
# The reply's own block: the header opens a line (heading or emphasis marks allowed). One
# quoted (`> [EXECUTION RESULT]`), mentioned mid-sentence, or inside a code fence is someone
# else's result being relayed, not this reply's claim.
_RESULT_HEADER = re.compile(r"^[ \t#*_-]*\[EXECUTION RESULT\]", re.MULTILINE)
# A fence runs to its closing fence, or to the end of the reply when it is never closed.
_FENCE = re.compile(r"^[ \t]*(```|~~~).*?(?:^[ \t]*\1[^\n]*$|\Z)", re.DOTALL | re.MULTILINE)
# A block ends at the next [SECTION] header. A field line may carry list or emphasis marks
# around the key (`- **status:** implemented`); a quoted line is not a field.
_NEXT_HEADER = re.compile(r"^\s*\[[A-Z][A-Z _-]+\]", re.MULTILINE)
_FIELD = re.compile(r"^[ \t*_`|-]*(status|verification)[*_`\s]*:[*_`\s]*([A-Za-z_-]+)", re.IGNORECASE | re.MULTILINE)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


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


def result_fields(text):
    """{field: value} from the last [EXECUTION RESULT] block in `text`, lower-cased."""
    text = _FENCE.sub("", text or "")
    headers = list(_RESULT_HEADER.finditer(text))
    if not headers:
        return {}
    body = text[headers[-1].end():]
    nxt = _NEXT_HEADER.search(body)
    body = body[: nxt.start()] if nxt else body
    return {m.group(1).lower(): m.group(2).lower() for m in _FIELD.finditer(body)}


def claims_completion(text):
    """`field: value` where the reply's own [EXECUTION RESULT] claims more than not_run
    allows, or None."""
    fields = result_fields(text)
    status, verification = fields.get("status"), fields.get("verification")
    if status and status not in UNVERIFIED_STATUSES:
        return "status: " + status
    if verification and verification != "not_run":
        return "verification: " + verification
    return None


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
