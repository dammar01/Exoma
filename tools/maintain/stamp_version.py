#!/usr/bin/env python3
"""Stamp the shipped docs with the one version number the code actually uses.

`config.settings.TOOL_VERSION` is the single source. Everything else — README headings,
the CLAUDE.md / AGENTS.md banners, the skill files that quote a version back at the user —
is a copy, and copies drift. v3.4.2 shipped with thirteen places still saying v3.4.1,
including the `/.help` output a new teammate reads first.

Only versions on anchored lines are touched. A bare semver anywhere else is left alone:
`utils/path_guard.py` legitimately says "removed in v3.4.1", and a changelog written for
a past release must keep saying what it said.

  python tools/maintain/stamp_version.py            # rewrite the stale ones
  python tools/maintain/stamp_version.py --check    # exit 1 if any are stale (CI / e2e)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "src"))

from exoma.config.providers import PROVIDER_BUNDLES  # noqa: E402
from exoma.config.settings import TOOL_VERSION  # noqa: E402

# path -> substrings that mark a line as carrying the CURRENT version. A line is rewritten
# only when it contains one of them, so adding a new mention means adding it here too —
# deliberate: silent stamping of an unrelated version is how the drift started.
TARGETS: dict[str, tuple[str, ...]] = {
    # The README carries the version only in its shields.io badge; the prose deliberately
    # does not, so a release bump touches one line here instead of five.
    "README.md": ("img.shields.io/badge/version-",),
    # uv_build reads only a static version, so the package metadata is one more copy.
    "pyproject.toml": ('version = "',),
    "docs/reference.md": (
        "# Exoma v",
        "## Install (v",
        "Reliability keys (v",
        "### Liveness worker (v",
    ),
    "src/exoma/_bundle/config/hosts/claude/CLAUDE.md": (
        "# Claude Code — Personal Global Config (v",
        "WORKFLOW-MAIN-AGENT:START",
        "## Workflow Main Agent —",
    ),
    "src/exoma/_bundle/config/hosts/claude/skills/doctor.md": (".workflow/config.json : v",),
    "src/exoma/_bundle/config/hosts/claude/skills/help.md": (
        "description: Command reference v",
        "[COMMAND GUIDE — v",
    ),
    "src/exoma/_bundle/config/hosts/claude/skills/init.md": ("generated: run/inspect/check",),
    "src/exoma/_bundle/config/hosts/claude/commands/.help.md": ('description: "Command reference v',),
}

# Each provider's instruction file carries the same banner. Derived rather than listed so
# a second provider is stamped by existing, not by someone remembering to add a line here.
# The title anchor stays loose ("Second Agent —" matches "# OpenCode Second Agent — v…")
# because the provider's own name is in it.
for _name, _bundle in PROVIDER_BUNDLES.items():
    TARGETS[f"src/exoma/_bundle/config/providers/{_name}/{_bundle['instructions'][0]}"] = (
        "WORKFLOW-SECOND-AGENT:START",
        "Second Agent —",
    )

# Where `--check` looks for version mentions NOBODY registered. TARGETS answers "what do I
# rewrite"; this answers "what did someone forget to tell me about" — the failure mode the
# allowlist alone cannot see, because a line outside it reports clean forever.
#
# Writing stays TARGETS-only on purpose. A scan that also rewrote would, the first time an
# exemption was forgotten, edit a sentence about the past into a sentence about the present
# — and the files most exposed to that are precisely the migration notes and changelog
# entries someone reads to find out what changed. A noisy check is the cheaper failure.
SCAN_PATHS: tuple[str, ...] = ("README.md", "docs/reference.md", "src/exoma/_bundle/config")

# Lines inside SCAN_PATHS allowed to carry a semver that is NOT TOOL_VERSION, keyed the
# same way TARGETS is: path -> substrings. Two legitimate kinds live here.
#   - history: a sentence about a past release must keep saying what it said.
#   - somebody else's version: a third-party tool's release is not ours to stamp.
EXEMPT: dict[str, tuple[str, ...]] = {
    # Exoma is forked from agent-workflow; a sentence about an agent-workflow release names
    # that project's version, not ours.
    "README.md": ("agent-workflow 3.",),
    "docs/reference.md": (
        "agent-workflow 3.",
        # The v3.4.2 -> v3.4.3 provider-key migration, described in the past tense.
        "migrates the v3.4.2 keys",
        "since v3.4.3 the second_agent provider",
        # codex-cli's version, not ours.
        "codex-cli",
        # The benchmark's frozen SUT, stated in prose. Same freeze as the TARGETS note.
        "System under test dibekukan",
        # The v3.6.0 `data/` layout upgrade, described as history.
        "The upgrade to the `data/` layout",
        # The v3.7.1 trusted-provider stance for codex/agy, described as history.
        "Since v3.7.1 this position",
        # Provider CLI releases (config/providers.py stable_version), not ours.
        "(`stable_version`): opencode",
        "Codex releases 0.155.0 through",
        # DEC-058: the probe that moved the pins, naming provider releases.
        "passed the live probe: codex",
        "(writes refused on a new and a resumed thread) and opencode",
        # v3.8.0 behavior described as history once 3.8.1 shipped.
        "and since 3.8.0 the template",
        "from the 3.8.0 draft)",
        "Up to 3.8.0, `intent-gate-set`",
        "In 3.8.0 this was observed",
        "Before 3.8.0 this hook ran",
    ),
    "src/exoma/_bundle/config/hosts/claude/settings.template.json": (
        # Marketplace pins: the plugin repos' own release tags, not ours.
        '"ref": "v',
    ),
}

# Bounded on both sides: no digit or dot before, no further `.digit` or digit after. The
# unbounded `\d+\.\d+\.\d+` read `127.0.0.1` in a shipped skill as version "127.0.0" and
# failed `--check`; a lookahead for `.digit` alone still let the last group backtrack, so
# `10.10.10.10` matched as "10.10.1". An IPv4 address is nobody's version.
_SEMVER = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?!\.?\d)")


def _restamp(text: str, anchors: tuple[str, ...]) -> tuple[str, int]:
    """Rewrite the semver on every anchored line. Returns (text, lines_changed)."""
    changed = 0
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not any(anchor in line for anchor in anchors):
            continue
        stamped = _SEMVER.sub(TOOL_VERSION, line)
        if stamped != line:
            lines[index] = stamped
            changed += 1
    return "".join(lines), changed


def _scanned_files() -> list[Path]:
    """Every file under SCAN_PATHS, files and directories alike.

    Not restricted to `*.md`: a version can be written into a hook or a settings template
    as easily as into prose, and "only markdown was checked" is the kind of boundary that
    reads as coverage without being it. `src/exoma/_bundle/manifest.json` sits outside SCAN_PATHS
    because `tools/maintain/gen_manifest.py --check` already owns it — two gates over one file
    disagree eventually, and that one hashes the whole bundle rather than guessing.
    """
    found: list[Path] = []
    for relative in SCAN_PATHS:
        path = REPO_ROOT / relative
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found += sorted(p for p in path.rglob("*") if p.is_file())
    return found


def _unreviewed(path: Path) -> list[str]:
    """Lines carrying a semver that is neither TOOL_VERSION, a TARGETS line, nor EXEMPT.

    A registered line is skipped rather than reported: after a bump it holds the previous
    version by definition, and it is already reported as `stale` with a fix that works.
    What is left is a version mention no list knows about — the drift class the allowlist
    is structurally blind to.
    """
    relative = path.relative_to(REPO_ROOT).as_posix()
    anchors = TARGETS.get(relative, ()) + EXEMPT.get(relative, ())
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Not text, or unreadable. A binary file carries no version mention to review, and
        # failing the gate on one would report a problem the maintainer cannot act on.
        return []
    hits: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        found = [v for v in _SEMVER.findall(line) if v != TOOL_VERSION]
        if not found:
            continue
        if any(anchor in line for anchor in anchors):
            continue
        hits.append(f"{relative}:{number}: {line.strip()[:100]}")
    return hits


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    stale: list[str] = []
    missing: list[str] = []

    for relative, anchors in TARGETS.items():
        path = REPO_ROOT / relative
        if not path.exists():
            missing.append(relative)
            continue
        original = path.read_text(encoding="utf-8")
        stamped, changed = _restamp(original, anchors)
        if not changed:
            continue
        stale.append(f"{relative} ({changed} line(s))")
        if not check_only:
            path.write_text(stamped, encoding="utf-8", newline="\n")

    if missing:
        print("[stamp_version] target file(s) missing:")
        for relative in missing:
            print(f"  - {relative}")
        return 1

    unreviewed: list[str] = []
    for path in _scanned_files():
        unreviewed += _unreviewed(path)

    if stale:
        verb = "stale" if check_only else "restamped"
        print(f"[stamp_version] {verb} at v{TOOL_VERSION}:")
        for entry in stale:
            print(f"  - {entry}")

    if unreviewed:
        # Reported apart from `stale` because the fix is different in kind: `stale` is
        # mechanical and the tool does it, while these need a human to decide whether the
        # line should follow the version or stay where it is. Guessing is what the two
        # lists exist to prevent.
        print(f"[stamp_version] unreviewed version mention(s) — not in TARGETS or EXEMPT:")
        for entry in unreviewed:
            print(f"  - {entry}")
        print(
            "Fix: add the line's anchor to TARGETS (it should follow TOOL_VERSION) "
            "or to EXEMPT (it is history, or someone else's version)."
        )
        return 1

    if not stale:
        print(
            f"[stamp_version] OK — every target says v{TOOL_VERSION} and no unreviewed "
            "version mention remains"
        )
        return 0
    if check_only:
        print("Fix: python tools/maintain/stamp_version.py")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
