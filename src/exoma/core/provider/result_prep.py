"""Shaping and sanitising a delegated call result before it is returned."""

import hashlib
import json
import re

from exoma.core.workspace.workspace_paths import CONFIG_VERSION
from exoma.utils.redact import redact_value

# What a `scope_not_covered` bullet can be about. Only the first two lower confidence:
# a requested surface the run could not reach is a hole in THIS answer, and an untagged
# bullet is treated the same because nothing says otherwise. Live state (a running
# provider, a browser, a database) and external systems are outside what any read of the
# repository can cover — capping on them capped every honest run, since nearly every run
# lists one, and a ceiling that always fires tells the reader nothing.
SCOPE_CAPPING = ("in_scope_unreachable", "unknown")
SCOPE_CATEGORIES = ("in_scope_unreachable", "live_state", "external", "out_of_scope")
_SCOPE_TAG = re.compile(r"^\[([a-z_]+)\]\s*", re.IGNORECASE)


def scope_gaps(content: str) -> list[tuple[str, str]]:
    """Every `scope_not_covered` bullet as (category, text).

    The category comes from a leading `[tag]`; a missing or unrecognised tag is `unknown`,
    so output from a provider that predates the tags keeps the cap it always had.
    """
    gaps: list[tuple[str, str]] = []
    collecting = False
    for line in (content or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("scope_not_covered"):
            collecting = True
            continue
        if collecting:
            if not stripped:
                continue
            if not stripped.startswith("-"):
                break
            body = stripped.lstrip("-").strip()
            if not body or body.lower() in {"none", "(none)", "n/a"}:
                continue
            category = "unknown"
            tag = _SCOPE_TAG.match(body)
            if tag and tag.group(1).lower() in SCOPE_CATEGORIES:
                category = tag.group(1).lower()
                body = body[tag.end():].strip()
            gaps.append((category, body))
    return gaps


def _scope_incomplete(content: str) -> bool:
    """True when the run reported ground it did not cover THAT THIS ANSWER NEEDED.

    Read off the agent's own `scope_not_covered` section: it is the one admission of
    incompleteness that arrives without lowering the confidence line beside it. Only the
    capping categories count; the rest are reported through `scope_warnings`.
    """
    return any(category in SCOPE_CAPPING for category, _ in scope_gaps(content))


def scope_warnings(content: str) -> list[dict]:
    """The non-capping gaps, kept visible instead of silently dropped with the cap."""
    return [
        {"category": category, "detail": text}
        for category, text in scope_gaps(content)
        if category not in SCOPE_CAPPING
    ]

def _attach_redactions(meta: dict, hits: list[dict]) -> None:
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

def _without_raw_args(value):
    """Remove argv payloads from injected/legacy adapter data."""
    if isinstance(value, dict):
        raw_args = value.get("args")
        clean = {
            key: _without_raw_args(child)
            for key, child in value.items()
            if key != "args"
        }
        if isinstance(raw_args, (list, tuple)):
            clean.setdefault("argv_count", len(raw_args))
            clean.setdefault("argv_chars", sum(len(str(arg)) for arg in raw_args))
        return clean
    if isinstance(value, list):
        return [_without_raw_args(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_without_raw_args(child) for child in value)
    return value

def _sanitize_result(result):
    clean, hits = redact_value(_without_raw_args(result))
    if isinstance(clean, dict):
        meta = clean.setdefault("meta", {})
        if isinstance(meta, dict):
            _attach_redactions(meta, hits)
    return clean, hits

def knowledge_fingerprint(known_knowledge: list[dict] | None) -> str | None:
    """A stable identity for the promoted knowledge a call was shown, or None for none.

    The digest of the injected entries themselves — ids, applicability, claims — rather
    than of the files: what can change the answer is what the second agent READ, and a
    document excluded for this branch was never read. A hash, not the text, so the
    evidence context stays small and an index row never carries the knowledge twice.
    """
    if not known_knowledge:
        return None
    encoded = json.dumps(
        known_knowledge, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _evidence_context(
    route: dict,
    fanout: bool,
    graph_leads: dict | None,
    known_facts: list[str] | None,
    known_knowledge: list[dict] | None = None,
) -> dict:
    """Inputs that can materially change an otherwise identical delegated answer.

    Schema 3 adds promoted knowledge. It rode the same prompt as facts but stayed out of
    this hash, so promoting a correction left the old answer — reasoned without it —
    served as a fresh hit for the identical task. Rows written under schema 2 hash
    differently and therefore miss once; that is the intended cost of the fix.
    """
    return {
        "schema": 3,
        "runtime_config_version": CONFIG_VERSION,
        "route": dict(route),
        "fanout": bool(fanout),
        "graph_leads": graph_leads,
        "known_facts": known_facts or [],
        "known_knowledge": knowledge_fingerprint(known_knowledge),
    }
