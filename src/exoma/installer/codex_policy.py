"""Owned additive Codex reviewer context; preserve unrelated TOML and user policy text.

Only ordinary bare-key/table layouts are edited. Unsupported valid TOML is reported,
never reformatted wholesale. Ownership metadata lives in a TOML comment, not in the
reviewer's prompt; it retains the original string literal for an exact clean removal.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path

TEMPLATE_KEY = "hosts/codex/auto-review-policy.md"
TESTED_CLI_VERSIONS = frozenset({"0.163.0"})
START = "<!-- EXOMA-CODEX-AUTO-REVIEW:START"
END = "<!-- EXOMA-CODEX-AUTO-REVIEW:END -->"
META = "# EXOMA-CODEX-AUTO-REVIEW:OWNERSHIP "
_BLOCK = re.compile(re.escape(START) + r" sha256=([0-9a-f]{64}) -->\n(.*?)\n" + re.escape(END), re.S)


def _parse(text):
    try:
        import tomllib
    except ImportError as exc:
        raise ValueError("support-unverified: automatic policy merge needs Python 3.11+ tomllib") from exc
    return tomllib.loads(text.lstrip("\ufeff"))


def _string_end(text, start):
    quote = text[start]
    triple = text.startswith(quote * 3, start)
    i = start + (3 if triple else 1)
    while i < len(text):
        if quote == '"' and text[i] == "\\":
            i += 2
        elif text[i] == quote:
            if not triple:
                return i + 1
            j = i
            while j < len(text) and text[j] == quote:
                j += 1
            if j - i >= 3:
                return j
            i = j
        else:
            i += 1
    raise ValueError("unterminated TOML string")


def _layout(text):
    masked, strings = list(text), {}
    i = 0
    while i < len(text):
        if text[i] in "\"'":
            end = _string_end(text, i)
            strings[i] = end
        elif text[i] == "#":
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
        else:
            i += 1
            continue
        for j in range(i, end):
            if text[j] not in "\r\n":
                masked[j] = " "
        i = end
    code = "".join(masked).lstrip("\ufeff") if text.startswith("\ufeff") else "".join(masked)
    if text.startswith("\ufeff"):
        code = " " + code  # keep offsets unchanged
    headers = list(re.finditer(r"(?m)^[ \t]*\[([^\]\n]+)\][ \t\r]*(?:\n|$)", code))
    table = None
    for n, header in enumerate(headers):
        if header.group(1).strip() == "auto_review":
            table = (header.start(), header.end(), headers[n + 1].start() if n + 1 < len(headers) else len(text))
    span = None
    for match in re.finditer(r"(?m)^[ \t]*(extra_policy|auto_review[ \t]*\.[ \t]*extra_policy)[ \t]*=", code):
        prior = [h for h in headers if h.start() < match.start()]
        section = prior[-1].group(1).strip() if prior else ""
        key = re.sub(r"\s", "", match.group(1))
        if (key == "extra_policy" and section == "auto_review") or (key == "auto_review.extra_policy" and not section):
            start = match.end()
            while start < len(text) and text[start] in " \t":
                start += 1
            if start not in strings or span is not None:
                raise ValueError("unsupported or ambiguous extra_policy layout")
            span = (start, strings[start], match.start())
    metadata = []
    for match in re.finditer(r"(?m)^" + re.escape(META) + r"([^\r\n]*)\r?\n?", text):
        if not any(start <= match.start() < end for start, end in strings.items()):
            metadata.append(match)
    if len(metadata) > 1:
        raise ValueError("duplicate policy ownership metadata")
    return span, table, metadata[0] if metadata else None


def _without_extra(data):
    out = copy.deepcopy(data)
    if isinstance(out.get("auto_review"), dict):
        out["auto_review"].pop("extra_policy", None)
        if not out["auto_review"]:
            out.pop("auto_review")
    return out


def _metadata_hash(metadata):
    payload = {key: value for key, value in metadata.items() if key != "integrity"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def merge(text: str, body: str, enabled: bool) -> dict:
    """Return text/status/error; conflicts never mutate the supplied text."""
    if not enabled and START not in text and META not in text:
        return {"text": text, "status": "disabled", "error": None}
    try:
        before = _parse(text)
        section = before.get("auto_review", {})
        if not isinstance(section, dict):
            raise ValueError("auto_review is not a table")
        value = section.get("extra_policy", "")
        if not isinstance(value, str):
            raise ValueError("extra_policy is not a string")
        span, table, meta_match = _layout(text)
        if "extra_policy" in section and span is None:
            raise ValueError("unsupported extra_policy key/layout; preserved")
        blocks = list(_BLOCK.finditer(value))
        owned = meta_match is not None
        if value.count(START) != len(blocks) or value.count(END) != len(blocks) or len(blocks) > 1:
            raise ValueError("broken or duplicate owned policy markers")
        if bool(blocks) != owned:
            raise ValueError("policy block has no verifiable ownership metadata")
        metadata = json.loads(meta_match.group(1)) if owned else None
        if owned:
            old = blocks[0]
            digest = hashlib.sha256(old.group(2).encode("utf-8")).hexdigest()
            if (not isinstance(metadata, dict) or digest != old.group(1)
                    or metadata.get("sha256") != digest
                    or metadata.get("integrity") != _metadata_hash(metadata)):
                raise ValueError("owned policy was edited; preserved")
            if (metadata.get("joiner") not in ("", "\n\n")
                    or metadata.get("separator") not in ("", "\n", "\n\n")
                    or type(metadata.get("new_key")) is not bool
                    or type(metadata.get("new_table")) is not bool):
                raise ValueError("invalid policy ownership metadata; preserved")
        if enabled:
            body = body.strip()
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            block = f"{START} sha256={digest} -->\n{body}\n{END}"
            if owned:
                updated = value[:old.start()] + block + value[old.end():]
                if updated == value:
                    return {"text": text, "status": "installed", "error": None}
                metadata["sha256"] = digest
            else:
                separator = "\n\n" if value else ""
                updated = value + separator + block
                metadata = {"sha256": digest, "original": text[span[0]:span[1]] if span else None,
                            "joiner": separator, "new_table": "auto_review" not in before,
                            "new_key": "extra_policy" not in section, "separator": ""}
            literal = json.dumps(updated, ensure_ascii=False)
            if span:
                candidate = text[:span[0]] + literal + text[span[1]:]
                insertion = span[2]
            elif "auto_review" not in before:
                separator = "" if not text or text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
                metadata["separator"] = separator
                candidate = text + separator + "[auto_review]\nextra_policy = " + literal + "\n"
                insertion = len(text + separator + "[auto_review]\n")
            elif table:
                # Insert after the header, not before the next header: comments just
                # before that next header may open another owner's managed block.
                position = table[1]
                separator = "\n" if position and not text[:position].endswith("\n") else ""
                metadata["separator"] = separator
                insertion = position + len(separator)
                candidate = text[:position] + separator + "extra_policy = " + literal + "\n" + text[position:]
            else:
                raise ValueError("unsupported auto_review table layout; preserved")
            metadata["integrity"] = _metadata_hash(metadata)
            meta_line = META + json.dumps(metadata, ensure_ascii=True, separators=(",", ":")) + "\n"
            if owned:
                # The metadata precedes the owned assignment; find offsets again after replacement.
                found = _layout(candidate)[2]
                candidate = candidate[:found.start()] + meta_line + candidate[found.end():]
            else:
                candidate = candidate[:insertion] + meta_line + candidate[insertion:]
            status = "outdated" if owned else "missing"
        elif not owned:
            return {"text": text, "status": "disabled", "error": None}
        else:
            start = old.start()
            joiner = metadata.get("joiner", "")
            if joiner and value[:start].endswith(joiner):
                start -= len(joiner)
            remaining = value[:start] + value[old.end():]
            original = metadata.get("original")
            literal = json.dumps(remaining, ensure_ascii=False)
            if isinstance(original, str):
                decoded = _parse("v = " + original)
                if set(decoded) != {"v"} or not isinstance(decoded["v"], str):
                    raise ValueError("invalid original policy ownership")
                if remaining == decoded["v"]:
                    literal = original
            line_end = text.find("\n", span[1])
            line_end = len(text) if line_end < 0 else line_end + 1
            if metadata.get("new_key") and not remaining and not text[span[1]:line_end].strip():
                candidate = text[:span[2]] + text[line_end:]
            else:
                candidate = text[:span[0]] + literal + text[span[1]:]
            found = _layout(candidate)[2]
            candidate = candidate[:found.start()] + candidate[found.end():]
            table_now = _layout(candidate)[1]
            if metadata.get("new_table") and table_now and not candidate[table_now[1]:table_now[2]].strip():
                prefix, suffix = candidate[:table_now[0]], candidate[table_now[2]:]
                separator = metadata.get("separator", "")
                if separator and not suffix and prefix.endswith(separator):
                    prefix = prefix[:-len(separator)]
                candidate = prefix + suffix
            elif metadata.get("new_key") and metadata.get("separator") and candidate.endswith(metadata["separator"]):
                candidate = candidate[:-len(metadata["separator"])]
            status = "removed"
        after = _parse(candidate)
        if _without_extra(before) != _without_extra(after):
            raise ValueError("unrelated configuration would change")
        return {"text": candidate, "status": status, "error": None}
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        message = str(exc)
        return {"text": text, "status": "support-unverified" if message.startswith("support-unverified") else "conflict", "error": message}


def cli_supported() -> bool:
    """Conservative tested baseline, not a claim about the upstream introduction version."""
    from exoma.core.provider.versions import read_version
    banner = read_version("codex", fresh=True) or ""
    match = re.fullmatch(r"(?:codex(?:-cli)?\s+)?(\d+\.\d+\.\d+)", banner.strip())
    return bool(match and match.group(1) in TESTED_CLI_VERSIONS)


def installation_status(config_text: str, template: Path, enabled: bool, home: Path) -> str:
    """Read-only installation state of the owned block, shared by `install --check` and doctor.

    One classifier so the two reports cannot disagree: installed | disabled (clean),
    missing, outdated, removed (disabled, block still present), conflict,
    support-unverified, template-unreadable. Nothing is written.
    """
    if enabled and (not location_supported(home) or not cli_supported()):
        return "support-unverified"
    try:
        body = template.read_text(encoding="utf-8") if enabled else ""
    except (OSError, UnicodeError):
        return "template-unreadable"
    return merge(config_text, body, enabled)["status"]


def location_supported(home: Path) -> bool:
    selected = os.environ.get("CODEX_HOME")
    return not selected or Path(selected).expanduser().resolve() == (home / ".codex").resolve()
