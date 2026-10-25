"""Detection and redaction of credential-shaped runtime data."""
import re

SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Anchored on the left: `task-events-at-turn-boundaries` contains "sk-" plus 16 key-class
    # characters, so research file names and kebab-case identifiers were scrubbed as keys and
    # the evidence anchors pointing at them were lost (DEC-047).
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{16,}"), "openai-style key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "github token"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "github token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "slack token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws access key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{20,}"), "bearer token"),
    # A value named by its key. The 12-character floor let a short real password through
    # (`password: "hunter2x"`); quoting is intent enough to lower it to 6. An unquoted
    # value keeps the 12 floor, or 8 when it carries a digit — `password: required` is
    # prose, `password: s3cretpw` is not.
    (
        re.compile(
            r"(?i)\"?(api[_-]?key|secret|password|passwd|access[_-]?token)\"?\s*[:=]\s*"
            r"(?:\"[^\"]{6,}\"|'[^']{6,}'"
            r"|(?!\[REDACTED:)(?:[^\s,;]{12,}|(?=[^\s,;]*\d)[^\s,;]{8,}))"
        ),
        "inline credential",
    ),
    (
        re.compile(
            r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^@\s]+@"
        ),
        "credential-bearing URL",
    ),
    # Any other scheme carrying `user:password@` — https, amqp, ftp, smtp. Only the form
    # with a password: a bare `user@host` is an address, not a credential.
    (
        re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s:@\[\]]+:[^/\s@]+@"),
        "credential-bearing URL",
    ),
]


def scan(text: str, label: str = "") -> list[str]:
    """Names of the credential shapes present. Empty when the text is clean."""
    prefix = f"{label}: " if label else ""
    return [f"{prefix}{why}" for pattern, why in SECRET_PATTERNS if pattern.search(text or "")]


def redact(text: str) -> tuple[str, list[dict]]:
    """Replace credential-shaped runs with a labelled marker.

    Returns (clean_text, hits) where each hit is {'kind', 'count'} — the KIND of thing
    found and how often, never the value. The whole point is that the value stops here;
    recording it in the audit trail would move the leak rather than close it.

    The marker keeps the surrounding evidence readable: a claim about where a token is
    configured is still useful once the token itself is gone.
    """
    if not text:
        return text or "", []
    hits: list[dict] = []
    out = text
    for pattern, why in SECRET_PATTERNS:
        out, count = pattern.subn(f"[REDACTED:{why}]", out)
        if count:
            hits.append({"kind": why, "count": count})
    return out, hits


def redact_value(value):
    """Recursively redact JSON-like data without retaining matched values."""
    counts: dict[str, int] = {}

    def visit(item):
        if isinstance(item, str):
            clean, hits = redact(item)
            for hit in hits:
                kind = str(hit["kind"])
                counts[kind] = counts.get(kind, 0) + int(hit["count"])
            return clean
        if isinstance(item, dict):
            return {
                visit(key) if isinstance(key, str) else key: visit(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, tuple):
            return tuple(visit(child) for child in item)
        return item

    clean = visit(value)
    hits = [{"kind": kind, "count": count} for kind, count in counts.items()]
    return clean, hits
