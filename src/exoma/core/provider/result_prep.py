"""Shaping and sanitising a delegated call result before it is returned."""

import hashlib
import json
import os
import re
from pathlib import Path

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


_PATH_TOKEN_RE = re.compile(
    r"""(?:`([^`\r\n]+)`|"([^"\r\n]+)"|'([^'\r\n]+)'|((?:[A-Za-z]:[\\/]|~[\\/]|(?:\.\.?[\\/])+)[^\s,;:)\]]+|(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10}|[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10}))"""
)
_BINARY_ATTACHMENT_EXTS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".zip", ".7z", ".tar", ".gz", ".rar", ".exe", ".dll", ".bin",
})


def _extract_path_candidates(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _PATH_TOKEN_RE.finditer(text or ""):
        raw = next((g for g in match.groups() if g), "").strip()
        if not raw or "://" in raw or raw in seen:
            continue
        seen.add(raw)
        found.append(raw)
    return found


def unreachable_readable_paths(project_root: Path, content: str) -> list[str]:
    """Project-root text files claimed `in_scope_unreachable` that are actually readable."""
    from exoma.core.evidence.fact_store import resolve_project_file

    root = Path(project_root).resolve()
    readable: list[str] = []
    seen: set[str] = set()
    for category, detail in scope_gaps(content):
        if category not in SCOPE_CAPPING:
            continue
        for token in _extract_path_candidates(detail):
            rel = resolve_project_file(root, token)
            if not rel or rel in seen:
                continue
            target = (root / rel).resolve()
            if target.suffix.lower() in _BINARY_ATTACHMENT_EXTS:
                continue
            try:
                with target.open("rb") as fh:
                    sample = fh.read(4096)
                if b"\x00" in sample:
                    continue
                sample.decode("utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            seen.add(rel)
            readable.append(rel)
    return readable


def find_unreadable_attachments(project_root: Path, task: str) -> list[dict]:
    """Detect explicit binary/PDF or outside-root file attachments referenced in `task`."""
    root = Path(project_root).resolve()
    issues: list[dict] = []
    seen: set[str] = set()
    for token in _extract_path_candidates(task):
        expanded = token
        if token == "~" or token.startswith(("~/", "~\\")):
            expanded = str(Path.home() / token[2:])
        p = Path(expanded)
        has_explicit_prefix = p.is_absolute() or token.startswith(("~/", "~\\", "../", "..\\", "./", ".\\"))
        has_dir_sep = "/" in token or "\\" in token
        ext = p.suffix.lower()
        if not has_explicit_prefix and not has_dir_sep and ext not in _BINARY_ATTACHMENT_EXTS:
            continue
        try:
            candidate = p.resolve() if p.is_absolute() else (root / p).resolve()
        except (OSError, ValueError):
            continue
        inside = candidate == root or root in candidate.parents
        exists = candidate.is_file()
        key = str(candidate)
        if key in seen:
            continue
        if ext in _BINARY_ATTACHMENT_EXTS and (exists or has_explicit_prefix or has_dir_sep):
            seen.add(key)
            issues.append({
                "path": token,
                "reason": "binary_attachment",
                "inside_root": inside,
                "exists": exists,
            })
        elif not inside and (exists or (has_explicit_prefix and ext)):
            seen.add(key)
            issues.append({
                "path": token,
                "reason": "outside_project_root",
                "inside_root": False,
                "exists": exists,
            })
    return issues

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
