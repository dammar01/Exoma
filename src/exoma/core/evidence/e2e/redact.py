"""Redaction for everything the e2e stages write down.

Three rules, in order of how often they matter:
  1. the resolved scenario (placeholders replaced by real values) is handed to the
     player's stdin/argv only and never to disk, a prompt, or a result;
  2. every artifact this package writes (spec, events, report, evidence block) goes
     through the shared secret scanner first, the same one delegated output goes
     through, so a value that leaks through a page title or an error message is
     caught by the same patterns everywhere;
  3. input values recorded by the player are dropped, not redacted — a `fill` step's
     evidence is that it ran, never what it typed;
  4. a resolved value is scrubbed in every form a page echoes it in (raw, URL-encoded,
     HTML- and JSON-escaped), in events and in player-written text files alike. Binary
     files cannot be scrubbed, so the player does not write them once values resolved.
"""

from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote, urlsplit

from exoma.core.workspace.workspace_paths import atomic_write_text
from exoma.utils.redact import redact_value

_VALUE_KEYS = frozenset({"value", "password", "secret", "token"})
# Shorter resolved values are not scrubbed by substring: replacing every "ab" in a
# report would corrupt URLs and selectors while protecting nothing a 2-character
# credential could not already be guessed from.
MIN_SCRUB_CHARS = 4
# Every text form a player could leave behind, not only the HTML it writes today: a file
# this list misses is stored with whatever resolved value it holds. Binaries (PNG, ZIP)
# stay out — a byte-level replace would corrupt them, so secret runs never capture them.
TEXT_ARTIFACT_SUFFIXES = (".html", ".htm", ".txt", ".log", ".json", ".jsonl", ".md")


# A path segment that is an identifier, not a route: a number, a UUID, a long hex or token
# string, a JWT, a mixed-case token with digits, or anything with an `@` (an address used
# as a key). A reset link's token lives exactly here, so the request ledger keeps the route
# and never the value. The JWT and mixed-case forms were missing: `eyJ....` carries dots,
# and a 16–23 character invite code slipped under the 24-character floor.
_ID_SEGMENT = re.compile(
    r"^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,}"
    # 24+ characters: a token when it carries a digit or is one unbroken run; a kebab or
    # snake word that long (`notification_preferences`) is still a route.
    r"|(?=[^/]*\d)[A-Za-z0-9_\-]{24,}|[A-Za-z0-9]{24,}"
    r"|[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    r"|(?=[^/]*[a-z])(?=[^/]*[A-Z])(?=[^/]*\d)[A-Za-z0-9_\-]{16,}"
    r"|.*@.*)$"
)
# Shorter secret shapes the pattern above lets through. A word the app routes on is one
# word, or kebab, snake or camel case; a token is a run of characters. 16+ letters and
# digits with no separator (`k3j4h5g6f7d8s9a0q1w2`), or 16+ characters whose letters are
# in both cases with each case at least a quarter of the segment (`AbCdEfGhIjKlMnOp`;
# `userSettingsPage` is a name and stays).
_TOKEN_RUN = re.compile(r"^[A-Za-z0-9]{16,}$")
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_\-]{16,}$")


def _secret_shaped(segment: str) -> bool:
    if _ID_SEGMENT.match(segment):
        return True
    if _TOKEN_RUN.match(segment) and any(c.isdigit() for c in segment) and any(c.isalpha() for c in segment):
        return True
    if not _TOKEN_CHARS.match(segment):
        return False
    upper = sum(c.isupper() for c in segment)
    lower = sum(c.islower() for c in segment)
    return min(upper, lower) * 4 >= len(segment)


def _segment(segment: str) -> str:
    # `;jsessionid=...` and other path parameters are dropped: a session id rides there.
    segment = segment.split(";", 1)[0]
    return ":id" if segment and _secret_shaped(unquote(segment)) else segment


_DEFAULT_PORTS = {"http": 80, "https": 443}


def sanitize_endpoint(url: str) -> str:
    """`scheme://host[:port]/route` for the request ledger: query, fragment and credentials in
    the authority dropped, `;` path parameters dropped, identifier- and token-shaped path
    segments replaced by `:id`."""
    try:
        parts = urlsplit(str(url))
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return "[unparseable url]"
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, _DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"
    segments = [_segment(segment) for segment in (parts.path or "/").split("/")]
    return f"{scheme}://{netloc}{'/'.join(segments) or '/'}"


def _variants(value: str) -> set[str]:
    """The forms a page is likely to echo a typed value back in: raw, URL-encoded (path,
    component and form style, either hex case), HTML-escaped, JSON-escaped."""
    forms = {
        value,
        quote(value, safe=""),
        quote(value),
        quote_plus(value),
        escape(value, quote=True),
        json.dumps(value)[1:-1],
    }
    for form in list(forms):
        if "%" in form:
            forms.add(re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), form))
    return {form for form in forms if len(form) >= MIN_SCRUB_CHARS}


def scrub_resolved(payload: object, env_values: dict[str, str]) -> object:
    """Put `${NAME}` back wherever a resolved placeholder value came back out.

    The player receives real values and the page can echo them — into a URL, a title,
    an assertion's `actual`. The secret scanner only catches values that LOOK like
    secrets; an email address used as a login does not. Anything the scenario resolved
    is therefore mapped back to its placeholder by value before the payload is written,
    shown to a reviewer, or returned. Longest values first, so one value that contains
    another is restored whole.
    """
    pairs = sorted(
        (
            (form, f"${{{name}}}")
            for name, value in env_values.items()
            if isinstance(value, str) and len(value) >= MIN_SCRUB_CHARS
            for form in _variants(value)
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    if not pairs:
        return payload

    def visit(item, key=None):
        if isinstance(item, str):
            # A step id or a claim id comes from the placeholder scenario, never from the
            # page, and a credential that is an ordinary word would otherwise turn the step
            # `fill-password` into `fill-${E2E_PASS}` in every event, report and signature.
            if key in _RESOLVED_STRUCTURAL_KEYS:
                return item
            for value, placeholder in pairs:
                if value in item:
                    item = item.replace(value, placeholder)
            return item
        if isinstance(item, dict):
            return {visit(k) if isinstance(k, str) else k: visit(v, k) for k, v in item.items()}
        if isinstance(item, list):
            return [visit(child, key) for child in item]
        if isinstance(item, tuple):
            return tuple(visit(child, key) for child in item)
        return item

    return visit(payload)


# Below this length a typed-out value is matched only as a WHOLE string, never inside one.
# Agent-written input is not a page echo: `user` as a credential would otherwise rewrite
# the claim id `login-valid-user` into a placeholder and break the scenario that holds it.
MIN_LITERAL_SUBSTRING = 8
# Fields that name structure, never carry input: a credential that happens to be an ordinary
# word (`password`) must not turn the step id `fill-password` into `fill-${E2E_PASS}` — which
# is exactly what the first real draft after this rule did.
_STRUCTURAL_KEYS = frozenset({"id", "claim_id", "cleans", "action", "side_effect", "severity", "version", "covers", "kind", "type"})
# A value found inside a longer string counts only as a whole token: not glued to letters,
# digits, `-` or `_` on either side. `fill-password` is an identifier that contains a word;
# `login as a@b.test` or `/users/a@b.test` carries the value itself.
_TOKEN_EDGE = r"(?<![A-Za-z0-9_-]){}(?![A-Za-z0-9_-])"
# Selector fields name the page. There a credential that is a plain word (letters only, like
# a weak `password`) is matched only as the whole string: inside a selector it is far more
# likely the word — `{"css": "#password"}` became `#${E2E_PASS}`, a false leak report. Any
# other value (an email, one with digits or symbols) is still found as a token inside a
# longer selector: `{"text": "Welcome user@example.test"}` carries the credential itself.
_SELECTOR_KEYS = frozenset({"css", "text", "label", "name", "role", "e2e"})


def scrub_literals(payload: object, env_values: dict[str, str]) -> tuple[object, list[str]]:
    """(payload with typed-out secret values put back to `${NAME}`, names found).

    For input an agent wrote — the task, the draft, the request — rather than output a page
    echoed. A value that should have travelled as `${NAME}` sometimes arrives typed out,
    and the generic scanner cannot see an email or a plain password; only the lookup knows
    what the real values are. A string that IS the value is replaced whatever its length
    (down to MIN_SCRUB_CHARS); a string that merely contains it only when the value is long
    enough not to be an ordinary word. Names only — never what the value was.
    """
    pairs = sorted(
        (
            (form, name)
            for name, value in env_values.items()
            if isinstance(value, str) and len(value) >= MIN_SCRUB_CHARS
            for form in _variants(value)
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    # Values below MIN_SCRUB_CHARS: matched only as the whole of a `value` field — what a
    # `fill` types. Anywhere else a one- to three-character string is as likely an enum
    # (`none`), a count or an id as a credential, and rewriting it would break the scenario.
    short = {
        value: name
        for name, value in env_values.items()
        if isinstance(value, str) and 0 < len(value) < MIN_SCRUB_CHARS
    }
    if not pairs and not short:
        return payload, []
    found: set[str] = set()

    # A letters-only value inside a longer string needs MIN_LITERAL_SUBSTRING: short words are
    # everywhere. Any other value (digits, `@`, symbols) is not a word anyone writes by chance,
    # so it is found as a token from MIN_SCRUB_CHARS up.
    token_patterns = {
        form: re.compile(_TOKEN_EDGE.format(re.escape(form)))
        for form, _ in pairs
        if len(form) >= (MIN_LITERAL_SUBSTRING if form.isalpha() else MIN_SCRUB_CHARS)
    }

    def visit(item, key=None):
        if isinstance(item, str):
            if key in _STRUCTURAL_KEYS:
                return item
            if key in _VALUE_KEYS and item in short:
                found.add(short[item])
                return f"${{{short[item]}}}"
            for form, name in pairs:
                if item == form:
                    found.add(name)
                    return f"${{{name}}}"
            in_selector = key in _SELECTOR_KEYS
            for form, name in pairs:
                if in_selector and form.isalpha():
                    continue
                pattern = token_patterns.get(form)
                if pattern is not None and pattern.search(item):
                    found.add(name)
                    item = pattern.sub(lambda _m, n=name: f"${{{n}}}", item)
            return item
        if isinstance(item, dict):
            # Keys are structure, never rewritten.
            return {k: visit(v, k) for k, v in item.items()}
        if isinstance(item, list):
            return [visit(child, key) for child in item]
        if isinstance(item, tuple):
            return tuple(visit(child, key) for child in item)
        return item

    clean = visit(payload)
    return clean, sorted(found)


_RESOLVED_STRUCTURAL_KEYS = frozenset({"step_id", "id", "claim_id", "cleans", "action", "type", "kind", "after_step"})


def scrub_text_files(
    directory: Path, env_values: dict[str, str], failures: list[dict] | None = None
) -> list[dict]:
    """Scrub player-written text artifacts in place: resolved values back to placeholders,
    then the shared secret scanner. Returns the scanner's hits for the audit trail.

    Recursive: a subdirectory is not a place a secret can hide from this pass.

    Fail-closed. A file that cannot be scrubbed is deleted, because an unscrubbed copy is
    exactly what this pass exists to prevent; it used to be skipped and left on disk. What
    could not be scrubbed — removed or not — is appended to `failures` as
    `{"path", "removed", "error"}` so the caller can keep it out of the report."""
    failed = failures if failures is not None else []
    try:
        paths = sorted(p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in TEXT_ARTIFACT_SUFFIXES)
    except OSError as exc:
        failed.append({"path": str(directory), "removed": False, "error": type(exc).__name__})
        return []
    hits_all: list[dict] = []
    for path in paths:
        try:
            text = path.read_bytes().decode("utf-8", errors="ignore")  # a bounded capture may cut a character
            clean, hits = redact_value(scrub_resolved(text, env_values))
            atomic_write_text(path, str(clean))
        except OSError as exc:
            # One unreadable file must not leave the rest unscrubbed, nor survive itself.
            removed = True
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                removed = False
            failed.append({"path": str(path), "removed": removed, "error": type(exc).__name__})
            continue
        hits_all.extend(hits)
    return hits_all


def redact_text(text: str) -> tuple[str, list[dict]]:
    clean, hits = redact_value(text)
    return str(clean), hits


def strip_input_values(event: object) -> object:
    """Drop typed values from a player event before it is stored or shown."""
    if isinstance(event, dict):
        return {
            key: ("[omitted]" if key in _VALUE_KEYS and isinstance(child, str) else strip_input_values(child))
            for key, child in event.items()
        }
    if isinstance(event, list):
        return [strip_input_values(child) for child in event]
    return event


def redact_payload(payload: object) -> tuple[object, list[dict]]:
    """Secret-scan a JSON-like payload after dropping input values."""
    return redact_value(strip_input_values(payload))


def write_json(path: Path, payload: object) -> list[dict]:
    """Write a redacted JSON artifact; returns the redaction hits for the audit trail."""
    clean, hits = redact_payload(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(clean, indent=2, ensure_ascii=False, default=str))
    return hits


def write_jsonl(path: Path, events: list[dict]) -> list[dict]:
    hits_all: list[dict] = []
    lines: list[str] = []
    for event in events:
        clean, hits = redact_payload(event)
        hits_all.extend(hits)
        lines.append(json.dumps(clean, ensure_ascii=False, default=str))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
    return hits_all


def write_text(path: Path, text: str) -> list[dict]:
    clean, hits = redact_value(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, str(clean))
    return hits
