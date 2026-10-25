"""Redaction accounting shared by every adapter.

An adapter redacts before the executor ever sees the result, which means the executor's
own sanitiser finds nothing left to clean and reports zero hits. Whoever redacted first
is therefore the only party that can still say how much was redacted — and if it drops
that number on the floor, `UsageRecord.redactions` reads 0 for a call that scrubbed a
private key.

That is not hypothetical. opencode carried the accounting; codex and agy carried a copy
of the same function with `clean, _ = redact_value(...)` and threw the hits away, so two
of three providers wrote an audit trail that understated secret exposure as exactly
nothing. The shapes matched, the behaviour did not, and nothing compared them.

So the accounting lives here once. `adapters/contract/base.py` is a Protocol on purpose — it
records the shape an adapter answers to without demanding inheritance — and concrete
helpers do not belong in it. A fourth provider gets this right by importing it, not by
its author remembering the failure mode above.

Redaction itself is unchanged: `utils.redact` decides WHAT is a secret. This module only
decides that the count survives the trip.
"""

from exoma.core.evidence.contract import make_error as _contract_make_error
from exoma.core.evidence.contract import make_ok as _contract_make_ok
from exoma.utils.redact import redact, redact_value


def attach_redactions(meta: dict, hits: list[dict]) -> None:
    """Fold `hits` into `meta["redactions"]`, summing per kind.

    Additive rather than overwriting: meta may already carry hits from an earlier pass
    over the same payload, and replacing them would make the second pass hide the first.
    """
    if not hits:
        return
    counts: dict[str, int] = {}
    for hit in [*(meta.get("redactions") or []), *hits]:
        if not isinstance(hit, dict) or not hit.get("kind"):
            continue
        kind = str(hit["kind"])
        counts[kind] = counts.get(kind, 0) + int(hit.get("count") or 0)
    meta["redactions"] = [
        {"kind": kind, "count": count} for kind, count in counts.items()
    ]
    meta["redaction_count"] = sum(counts.values())


def sanitize_meta(meta: dict | None, extra_hits: list[dict] | None = None) -> dict:
    """Redact `meta` in place of the caller, keeping the tally of what was removed.

    `extra_hits` is how a caller reports redactions it performed on OTHER fields —
    message, next_action, content, digest — so the whole call's total lands in one place
    the audit trail can read.
    """
    clean, hits = redact_value(meta or {})
    if not isinstance(clean, dict):
        clean = {}
    attach_redactions(clean, [*(extra_hits or []), *hits])
    return clean


def make_error(
    error_type: str,
    message: str,
    next_action: str,
    meta: dict | None = None,
    **fields,
) -> dict:
    """`core.evidence.contract.make_error` with every string scrubbed and the count preserved."""
    clean_message, message_hits = redact(str(message or ""))
    clean_next, next_hits = redact(str(next_action or ""))
    clean_fields, field_hits = redact_value(fields)
    safe_meta = sanitize_meta(meta, [*message_hits, *next_hits, *field_hits])
    return _contract_make_error(
        error_type, clean_message, clean_next, meta=safe_meta, **clean_fields
    )


def make_ok(content: str, meta: dict | None = None, digest: dict | None = None) -> dict:
    """`core.evidence.contract.make_ok` with every string scrubbed and the count preserved."""
    clean_content, content_hits = redact(content or "")
    clean_digest, digest_hits = redact_value(digest)
    safe_meta = sanitize_meta(meta, [*content_hits, *digest_hits])
    return _contract_make_ok(clean_content, safe_meta, clean_digest)
