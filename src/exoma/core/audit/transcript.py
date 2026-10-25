"""Claude Code session transcripts -> turn counts and context token totals.

Reads history, never writes it. The runtime's own `.workflow/usage.jsonl` covers the
second agent; nothing covers the premium side, because the premium side is Claude Code
itself and it does not report into this project. Its transcript does, and this module is
the only reader of it.

Three things shape the parsing.

**The schema belongs to the vendor.** Every field name here was read off a real transcript
under `~/.claude/projects/`, not inferred from documentation, and a vendor is free to
change any of them. So every access is defensive and every unknown record type is ignored
rather than rejected: a transcript with one new record shape must still yield turn counts.

**Sum per message, never take the last.** `message.usage` is per-assistant-message, so the
totals are additions across the stream — the same rule the provider adapters follow for
their own event streams. Taking the final message's usage would report one turn's context
as the whole session's.

**A user turn is a human typing.** `type: "user"` also covers tool results, task
notifications and injected system text, all of which arrive in the user role and none of
which are someone asking for something. Counting them would make "how many turns did this
take" a measure of how many tools the model chose to call. The filter is in
`is_human_turn`, stated as a list so a miss is visible rather than silent.

Sidechain records (`isSidechain: true`) are the native-subagent transcript interleaved
into the parent session. They are counted separately rather than dropped, because arm B is
precisely the arm that produces them: folding them into the main totals would hide arm B's
cost, and dropping them would understate it.
"""

import json
import re
from pathlib import Path

# Record types carrying a conversation message. Everything else in a transcript
# (`mode`, `attachment`, `file-history-snapshot`, `ai-title`, ...) is session bookkeeping.
USER_TYPE = "user"
ASSISTANT_TYPE = "assistant"

# Text injected into the user role by the harness rather than typed by a person. Matched
# on the leading tag, which is how each one is delimited.
INJECTED_PREFIXES = (
    "<task-notification>",
    "<local-command-caveat>",
    "<system-reminder>",
    "<command-output>",
    "<local-command-stdout>",
)

# Tools whose use means the model has started changing the tree. `turns_to_first_edit`
# counts human turns up to the first of these.
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")

# A path already absolute in either platform's flavour: a Windows drive root
# (`E:\\...`, `E:/...`) or a POSIX/UNC root. Both flavours are matched whatever the
# host is, because a transcript directory is named for the machine that wrote it,
# which is not always the machine reading it.
ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])")


def transcripts_root(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".claude" / "projects"


def project_slug(project_root) -> str:
    """`E:\\Work\\project\\x` -> `E--Work-project-x`, the directory Claude Code writes to.

    Claude Code names the directory after the root it was handed, so the slug must
    not depend on where this process happens to be running. An already-absolute path
    is slugified as written: resolving one spelled in the other platform's flavour --
    a Windows root read on Linux, say -- reads it as relative and silently prefixes
    the current directory, yielding a slug that names no transcript directory at all.
    """
    text = str(project_root)
    if not ABSOLUTE_PATH.match(text):
        text = str(Path(project_root).resolve())
    out = []
    for char in text:
        out.append(char if (char.isalnum() or char == "-") else "-")
    return "".join(out).strip("-")


def find_transcript(session_id: str, project_root, home: Path | None = None) -> Path | None:
    """The transcript for a Claude session id, or None.

    The file is named for the session id, so this is a lookup rather than a search — no
    mtime heuristic, which would pick the wrong file whenever two sessions run at once.
    """
    if not session_id:
        return None
    path = transcripts_root(home) / project_slug(project_root) / f"{session_id}.jsonl"
    return path if path.is_file() else None


def load_transcript(path) -> list[dict]:
    """Every record in a transcript, oldest first. Unreadable lines are skipped.

    A transcript is append-only and written by a live process, so a torn final line is a
    realistic state. Dropping that one line is right; refusing to report anything because
    of it is not.
    """
    records: list[dict] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _content_text(record: dict) -> str | None:
    """The message body as text, or None if it is not a plain text message."""
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # A list of tool_result blocks is a tool returning, not a person typing. A list of
        # text blocks is a person typing, and is treated as such.
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        if len(parts) == len(content) and parts:
            return "".join(parts)
        return None
    return None


def is_human_turn(record: dict) -> bool:
    """True for a record that represents a person submitting a prompt.

    A slash command counts. It arrives wrapped in `<command-name>` / `<command-message>`
    tags rather than as bare prose, but a person typed it and it is one of the times they
    had to ask for something — which is the question `message_turns` answers. The expanded
    instruction text that follows a slash command arrives separately with `isMeta` set and
    is excluded there, so the command is counted once, not twice.
    """
    if record.get("type") != USER_TYPE:
        return False
    if record.get("isMeta"):
        # Harness-injected context that arrives in the user role.
        return False
    if record.get("isSidechain"):
        # The parent's prompt to a subagent, not a person's prompt to the session.
        return False
    text = _content_text(record)
    if text is None:
        return False
    stripped = text.lstrip()
    if not stripped:
        return False
    return not stripped.startswith(INJECTED_PREFIXES)


def _usage_of(record: dict) -> dict | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    return usage if isinstance(usage, dict) else None


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _context_of(usage: dict) -> int:
    """Everything the model was fed for one message.

    Cache reads and cache writes are part of the context window, not a discount on it: a
    turn that read 200k cached tokens still carried 200k tokens of context. Output is
    excluded — it is what came back, not what went in.
    """
    return (
        _int(usage.get("input_tokens"))
        + _int(usage.get("cache_read_input_tokens"))
        + _int(usage.get("cache_creation_input_tokens"))
    )


def count_turns(records: list[dict]) -> dict:
    """Turn counts, with the sidechain kept separate from the main conversation."""
    human = sum(1 for record in records if is_human_turn(record))
    assistant = sum(
        1
        for record in records
        if record.get("type") == ASSISTANT_TYPE and not record.get("isSidechain")
    )
    sidechain = sum(
        1
        for record in records
        if record.get("type") == ASSISTANT_TYPE and record.get("isSidechain")
    )
    return {
        "message_turns": human,
        "assistant_turns": assistant,
        "sidechain_assistant_turns": sidechain,
    }


def turns_to_first_edit(records: list[dict]) -> int | None:
    """Human turns submitted before the model first touched a file. None if it never did.

    "Message turn until execution" in the brief: how much conversation it took to get from
    the ask to a change on disk.
    """
    turns = 0
    for record in records:
        if is_human_turn(record):
            turns += 1
            continue
        if record.get("type") != ASSISTANT_TYPE:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") in EDIT_TOOLS:
                return turns
    return None


def context_tokens(records: list[dict]) -> dict:
    """Context fed to the model, summed across turns and at its single largest turn.

    Two numbers because they answer different questions. The sum is the cumulative load
    the session placed on the premium model; the peak is how close a single turn came to
    filling the window. An arm can be cheap on one and alarming on the other.
    """
    total = peak = 0
    sidechain_total = 0
    output = reasoning = 0
    measured = 0
    for record in records:
        if record.get("type") != ASSISTANT_TYPE:
            continue
        usage = _usage_of(record)
        if usage is None:
            continue
        context = _context_of(usage)
        if record.get("isSidechain"):
            sidechain_total += context
            continue
        measured += 1
        total += context
        peak = max(peak, context)
        output += _int(usage.get("output_tokens"))
        details = usage.get("output_tokens_details")
        if isinstance(details, dict):
            reasoning += _int(details.get("thinking_tokens"))
    return {
        "context_input_tokens": total,
        "context_peak_tokens": peak,
        "context_sidechain_tokens": sidechain_total,
        "transcript_output_tokens": output,
        "transcript_reasoning_tokens": reasoning,
        # Its own denominator: totals over three measured messages and over three hundred
        # are different claims.
        "context_measured_messages": measured,
    }


def summarize(path) -> dict:
    """Everything the benchmark ledger wants from one transcript, in one pass."""
    records = load_transcript(path)
    if not records:
        return {
            "message_turns": None,
            "assistant_turns": None,
            "sidechain_assistant_turns": None,
            "turns_to_first_edit": None,
            "context_input_tokens": None,
            "context_peak_tokens": None,
            "context_sidechain_tokens": None,
            "transcript_output_tokens": None,
            "transcript_reasoning_tokens": None,
            "context_measured_messages": 0,
            "transcript_source": "unreadable_or_empty",
        }
    out = count_turns(records)
    out.update(context_tokens(records))
    out["turns_to_first_edit"] = turns_to_first_edit(records)
    out["transcript_source"] = Path(path).name
    return out
