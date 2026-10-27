"""Portable filesystem path components for session and job directories."""
import hashlib
import re

_WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_HASHED_COMPONENT_SUFFIX = re.compile(r"--[0-9a-f]{12}$")


def safe_path_component(value: str) -> str:
    """Map an identifier to one portable, collision-resistant path component."""
    raw = str(value or "")
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)
    device_stem = raw.casefold().split(".", 1)[0]
    portable_plain = (
        cleaned == raw
        and cleaned not in {"", ".", ".."}
        and len(cleaned) <= 120
        and raw == raw.casefold()
        and not raw.endswith((".", " "))
        and device_stem not in _WINDOWS_RESERVED_NAMES
        and not _HASHED_COMPONENT_SUFFIX.search(raw)
    )
    if portable_plain:
        return cleaned
    stem = cleaned.strip(". ")[:96] or "session"
    if stem.casefold().split(".", 1)[0] in _WINDOWS_RESERVED_NAMES:
        stem = f"id_{stem}"
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{stem}--{digest}"

# `validate_scope` used to live here: it scanned the TASK TEXT for sensitive filenames
# and out-of-root paths before dispatch. It was removed in v3.4.1 because it guarded the
# wrong thing. Naming a file is not reading one, so it rejected an audit whose
# instructions merely mentioned the files it was auditing, while a run that never spelled
# the name out reached exactly the same data. Its own docstring admitted the real boundary
# was elsewhere ("opencode enforces the hard boundary") — that boundary now exists and is
# enforced on the tool call itself: `permission.read` / `permission.grep` deny rules in
# _bundle/config/providers/opencode/opencode.project.json, plus `external_directory: deny` for
# out-of-root access. Enforcement belongs where the file is opened, not where it is typed.
