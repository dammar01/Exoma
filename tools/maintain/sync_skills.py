#!/usr/bin/env python3
"""Validate that the skill files, the command wrappers, and CLAUDE.md's registry agree.

`src/exoma/_bundle/config/hosts/claude/skills/<name>.md` is the single source of truth for every skill body.
CLAUDE.md carries only the orchestrator contract plus a command registry, so there is
nothing to generate. What can still drift is the set of skills: a new skills/<name>.md
added without registering `/.name` in CLAUDE.md, or a registry entry with no file.

Three artifacts have to agree, not two. `commands/.<name>.md` is the wrapper that makes
`/.name` typeable at all, and it is the leg this tool used to skip: /.promote shipped with
a registry entry and a skill body and no wrapper, so the command existed everywhere except
where a user could invoke it, and this check stayed green through the whole release.

The wrapper's frontmatter is the fourth leg. Claude Code lists a command by its wrapper's
`description` and shows its `argument-hint` while typing; nineteen wrappers carried a generic
"Run the <name> workflow skill" while the skill header two lines into its body already said
what the command does. The skill header is now the source: line 2 `description:` and an
optional line 3 `argument-hint:`. `--write` renders every registered wrapper from it, and
`--check` fails when a wrapper differs from what would be rendered.

  python tools/maintain/sync_skills.py            # report mismatches
  python tools/maintain/sync_skills.py --check    # exit 1 on any mismatch (for CI / doctor)
  python tools/maintain/sync_skills.py --write    # regenerate commands/.<name>.md, then check
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = REPO_ROOT / "src" / "exoma" / "_bundle" / "config" / "hosts" / "claude" / "CLAUDE.md"
SKILLS_DIR = REPO_ROOT / "src" / "exoma" / "_bundle" / "config" / "hosts" / "claude" / "skills"
COMMANDS_DIR = REPO_ROOT / "src" / "exoma" / "_bundle" / "config" / "hosts" / "claude" / "commands"

# Registry lines list every command as `/.name` (with an optional trailing flag like -y).
_REGISTRY_RE = re.compile(r"^(?:LOCAL|DELEGATED):", re.MULTILINE)
_COMMAND_RE = re.compile(r"/\.([a-z][a-z0-9_-]*)")


def _registry_commands(claude_text: str) -> set[str]:
    """Command names drawn from the `LOCAL:` / `DELEGATED:` registry lines only."""
    commands: set[str] = set()
    for line in claude_text.splitlines():
        if line.startswith(("LOCAL:", "DELEGATED:")):
            commands.update(_COMMAND_RE.findall(line))
    return commands


def _skill_files() -> set[str]:
    return {p.stem for p in SKILLS_DIR.glob("*.md") if p.is_file()}


def _skill_header(name: str) -> dict[str, str]:
    """`description` (required) and `argument-hint` (optional) from the skill's header lines."""
    meta: dict[str, str] = {}
    for line in (SKILLS_DIR / f"{name}.md").read_text(encoding="utf-8").splitlines()[1:3]:
        key, sep, value = line.partition(":")
        if sep and key in ("description", "argument-hint") and value.strip():
            meta[key] = value.strip()
    return meta


def render_wrapper(name: str) -> str:
    """The wrapper `commands/.<name>.md` exactly as the skill header implies it.

    Values are JSON-quoted, which YAML reads as a double-quoted string: several descriptions
    contain `: ` ("Fallback: /.local."), which a plain YAML scalar would not survive.
    """
    meta = _skill_header(name)
    if "description" not in meta:
        raise SystemExit(f"[sync_skills] skills/{name}.md has no `description:` on line 2")
    front = [f"description: {json.dumps(meta['description'], ensure_ascii=False)}"]
    if "argument-hint" in meta:
        front.append(f"argument-hint: {json.dumps(meta['argument-hint'], ensure_ascii=False)}")
    return (
        "---\n" + "\n".join(front) + "\n---\n"
        f"Read ~/.claude/skills/{name}.md and follow it exactly. "
        "Pass along any arguments I gave: $ARGUMENTS\n"
    )


def _wrapper_path(name: str) -> Path:
    return COMMANDS_DIR / f".{name}.md"


def _command_files() -> set[str]:
    """Wrapper names, without the leading dot the filename carries (`.plan.md` -> `plan`)."""
    return {
        p.stem.lstrip(".")
        for p in COMMANDS_DIR.glob(".*.md")
        if p.is_file() and p.stem.lstrip(".")
    }


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    if "--write" in sys.argv[1:]:
        for name in sorted(_registry_commands(CLAUDE_MD.read_text(encoding="utf-8")) & _skill_files()):
            _wrapper_path(name).write_text(render_wrapper(name), encoding="utf-8", newline="\n")
    claude_text = CLAUDE_MD.read_text(encoding="utf-8")
    commands = _registry_commands(claude_text)
    files = _skill_files()
    wrappers = _command_files()
    if not commands:
        raise SystemExit(
            "[sync_skills] no LOCAL:/DELEGATED: registry lines found in CLAUDE.md"
        )
    if not files:
        raise SystemExit("[sync_skills] no skills/*.md files found")
    if not wrappers:
        raise SystemExit("[sync_skills] no commands/.*.md wrapper files found")

    missing_files = sorted(commands - files)      # registered command, no skill file
    unregistered = sorted(files - commands)       # skill file, not in the registry
    missing_wrappers = sorted(commands - wrappers)   # registered command, not typeable
    orphan_wrappers = sorted(wrappers - commands)    # wrapper for nothing in the registry

    stale_wrappers = sorted(
        name for name in commands & files & wrappers
        if _wrapper_path(name).read_text(encoding="utf-8") != render_wrapper(name)
    )

    if not (missing_files or unregistered or missing_wrappers or orphan_wrappers or stale_wrappers):
        print(
            f"[sync_skills] OK — {len(files)} skills and {len(wrappers)} command wrappers "
            "match the command registry"
        )
        return 0

    print("[sync_skills] MISMATCH between skills/, commands/ and CLAUDE.md registry:")
    for name in missing_files:
        print(f"  - /.{name} is in the registry but skills/{name}.md is missing")
    for name in unregistered:
        print(f"  - skills/{name}.md exists but /.{name} is not in the registry")
    for name in missing_wrappers:
        print(
            f"  - /.{name} is in the registry but commands/.{name}.md is missing "
            "— the command cannot be invoked"
        )
    for name in orphan_wrappers:
        print(f"  - commands/.{name}.md exists but /.{name} is not in the registry")
    for name in stale_wrappers:
        print(f"  - commands/.{name}.md differs from the header of skills/{name}.md")
    if check_only:
        return 1
    print(
        "Fix: add the missing skill file or command wrapper, "
        "or register/unregister the command in CLAUDE.md; "
        "for a stale wrapper run with --write"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
