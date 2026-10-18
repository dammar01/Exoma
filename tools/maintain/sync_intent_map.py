#!/usr/bin/env python3
"""Validate that the runtime intent NL-map and CLAUDE.md's NL-map agree.

Two hand-maintained copies of the same delegated intent logic exist:
  - dist/config/claude/hooks/intent-map.json  (consumed by intent-gate-set.py at runtime)
  - dist/config/claude/CLAUDE.md               (the LLM-facing NL-map + command registry)

`intent-map.json` is the CANONICAL source; CLAUDE.md must cover everything it lists. When
the two drift, the runtime gate and the prompt disagree about what counts as DELEGATED —
exactly the class of bug this check exists to catch. It writes nothing.

  python tools/maintain/sync_intent_map.py            # report drift
  python tools/maintain/sync_intent_map.py --check    # exit 1 if they disagree (for CI / doctor)

Checks (all hard under --check):
  1. delegated command SET in JSON == the DELEGATED registry set in CLAUDE.md
  2. prefix_regex commands == the JSON delegated set
  3. every JSON pattern's keyword (its longest word) appears in CLAUDE.md — so a pattern
     added to the JSON without adding the trigger phrase to the prompt fails the check
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = REPO_ROOT / "dist" / "config" / "claude" / "CLAUDE.md"
INTENT_MAP = REPO_ROOT / "dist" / "config" / "claude" / "hooks" / "intent-map.json"

_COMMAND_RE = re.compile(r"/\.([a-z][a-z0-9_-]*)")
_WORD_RE = re.compile(r"[a-z]{4,}")


def _registry_delegated(claude_text: str) -> set[str]:
    """Delegated command names from the `DELEGATED:` registry line."""
    commands: set[str] = set()
    for line in claude_text.splitlines():
        if line.startswith("DELEGATED:"):
            commands.update(_COMMAND_RE.findall(line))
    return commands


def _keyword(pattern: str) -> str | None:
    """The longest alphabetic run in a regex pattern — a stable, regex-noise-free token to
    look for in the prompt's NL-map (e.g. `struktur.{0,12}gimana` -> `struktur`). Word-
    boundary escapes are stripped first so `\\bdampak\\b` yields `dampak`, not `bdampak`."""
    cleaned = pattern.replace("\\b", " ").replace("\\", " ")
    words = _WORD_RE.findall(cleaned.lower())
    return max(words, key=len) if words else None


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    claude_text = CLAUDE_MD.read_text(encoding="utf-8")
    claude_lower = claude_text.lower()
    try:
        intent = json.loads(INTENT_MAP.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[sync_intent_map] cannot read intent-map.json: {exc}")

    json_delegated = set(intent.get("delegated", []))
    prefix_regex = str(intent.get("prefix_regex", ""))
    prefix_cmds = set(re.findall(r"[a-z][a-z0-9-]*", prefix_regex.split("(", 1)[-1].split(")", 1)[0]))
    registry = _registry_delegated(claude_text)

    problems: list[str] = []

    if json_delegated != registry:
        problems.append(
            f"intent-map delegated commands {sorted(json_delegated)} != "
            f"CLAUDE.md DELEGATED registry {sorted(registry)}"
        )
    if json_delegated != prefix_cmds:
        problems.append(
            f"prefix_regex commands {sorted(prefix_cmds)} != delegated set {sorted(json_delegated)}"
        )

    # pattern keyword coverage: each JSON trigger must be reflected in the prompt NL-map
    for cmd, patterns in (intent.get("patterns") or {}).items():
        for pat in patterns:
            kw = _keyword(pat)
            if kw and kw not in claude_lower:
                problems.append(
                    f"pattern '{pat}' (cmd={cmd}) keyword '{kw}' not found in CLAUDE.md NL-map"
                )

    if not problems:
        print(
            f"[sync_intent_map] OK — {len(json_delegated)} delegated commands, "
            f"all patterns reflected in CLAUDE.md"
        )
        return 0

    print("[sync_intent_map] DRIFT between intent-map.json and CLAUDE.md:")
    for p in problems:
        print(f"  - {p}")
    if check_only:
        return 1
    print("Fix: update CLAUDE.md's NL-map/registry to cover intent-map.json (canonical source)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
