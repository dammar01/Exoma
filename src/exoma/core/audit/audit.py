"""Reading side of the governance trail.

`.workflow/data/audit.jsonl` (`.workflow/audit.jsonl` on a v3.5.x layout) has been written
on every delegated call since the trail existed, and read by nothing. A record nobody can open is a record only in the sense that the
bytes are on disk: the whole reason audit is a stream separate from usage is that someone
consults it after an incident, and until now that someone had to `cat` a JSONL file and
count by eye.

Kept out of `telemetry.py` on purpose, for the same reason the streams are separate.
Telemetry measures and may be recomputed under a better definition; audit answers "what
was done, by which provider, and did anything get scrubbed on the way". Merging the two
readers would eventually let a change to a metric reshape the record.

Deliberately NOT a rate. Every figure here is a count over rows that exist, because the
question an audit answers is "what happened", and a percentage invites the reader to
treat an empty trail as a clean one.
"""

import json
from pathlib import Path

from exoma.core.evidence.contracts import AUDIT_STREAM_NAME
from exoma.core.workspace.workspace_paths import data_dir


def load_audit(project_root) -> list[dict]:
    """Every audit row on disk, oldest first. Torn rows are skipped, not fatal.

    Same tolerance as `telemetry.load_usage`, and for the same reason: the stream is
    written fail-open, so a process killed mid-write leaves a partial final line. Refusing
    to open the trail because its last line is short would make the record useless exactly
    when something has gone wrong.
    """
    path = data_dir(Path(project_root)) / AUDIT_STREAM_NAME
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _tally(rows: list[dict], field: str, unknown: str = "unknown") -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(field)
        key = str(value) if value else unknown
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def report(project_root) -> dict:
    """The trail summarised, over the whole recorded history of this project."""
    rows = load_audit(project_root)
    failures = [row for row in rows if row.get("ok") is False]
    redacting = [row for row in rows if int(row.get("redactions") or 0) > 0]

    return {
        "calls": len(rows),
        "first_recorded": rows[0].get("at") if rows else None,
        "last_recorded": rows[-1].get("at") if rows else None,
        "by_command": _tally(rows, "command"),
        # `provider` is null on rows written before the field was populated, and the
        # tally says so rather than folding them into whichever provider ran most.
        "by_provider": _tally(rows, "provider"),
        "by_model": _tally(rows, "model"),
        "failures": {
            "calls": len(failures),
            "by_error_type": _tally(failures, "error_type", unknown="unclassified"),
        },
        "redactions": {
            # The number this stream exists to answer: how often a delegated call had
            # something scrubbed out of it. Until v3.4.6 two of three adapters discarded
            # their own hit counts, so a codex or agy row from before then reads 0 whether
            # or not anything was redacted. Rows are not rewritten to say otherwise.
            "calls_with_redactions": len(redacting),
            "total": sum(int(row.get("redactions") or 0) for row in redacting),
            "by_command": _tally(redacting, "command"),
        },
        "scope": (
            "delegated calls only — edits made by the main agent never pass through this "
            "runtime and are not audited here"
        ),
    }
