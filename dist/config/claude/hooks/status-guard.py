# status-guard.py - Stop hook (the "never say done before verify" rule, DEC-067).
# Reads the session's runtime/execution.json (core/runtime/execution.py). When an execution
# exists whose verification is still `not_run`, and the assistant message that just ended
# the turn claims completion ("done", "selesai", "berhasil", "fixed", "test lolos", ...),
# the hook reports it as a systemMessage the user sees. Warning only, by design: a word
# list reads prose, and prose is where false positives live — "belum selesai" and a quoted
# rule both contain the word. The deterministic gates are in execution-gate.py.
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


# Completion claims, in the two languages the skills are written in. A claim negated in
# the words just before it ("belum selesai", "not done") is not a claim.
_CLAIM = re.compile(
    r"\b(done|selesai|berhasil|sudah jalan|fixed|works now|aman sekarang|test lolos|tests? pass(?:ed|es)?)\b|✅",
    re.IGNORECASE,
)
_NEGATION = re.compile(r"\b(belum|tidak|bukan|jangan|dilarang|not|never|no|isn't|aren't|without)\b[\s\w,\"'`-]{0,20}$", re.IGNORECASE)
# The sentence that names the rule, quoted in a status line, is not a claim either.
_RULE_QUOTE = re.compile(r"implemented,?\s+belum diverifikasi|verification:\s*not_run", re.IGNORECASE)


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


def claims_completion(text):
    """The first unnegated completion claim in `text`, or None."""
    for line in (text or "").splitlines():
        # Drop the quoted rule, keep the rest: a line that names the status and then
        # claims completion anyway is still a claim.
        line = _RULE_QUOTE.sub(" ", line)
        for m in _CLAIM.finditer(line):
            if _NEGATION.search(line[: m.start()]):
                continue
            return m.group(0)
    return None


try:
    raw = os.environ.get("CLAUDE_HOOK_RAW", "")
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
                "[STATUS GUARD] the reply says %r, but plan %s has verification: not_run. "
                "Implemented is not verified — run /.verify before treating this as done."
            ) % (word, execution.get("plan_id")),
        }))
    sys.exit(0)
except SystemExit:
    raise
except Exception:
    sys.exit(0)
