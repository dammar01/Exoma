#!/usr/bin/env python3
"""Regenerate src/exoma/_bundle/manifest.json from the dist/ tree.

dist/ is the canonical, Git-reviewed bundle; ~/.claude is an install target downstream of
it. tools/maintain/extract_config.py still exists to *bootstrap* dist/ from a maintainer's ~/.claude,
but once dist/ is edited in-repo the manifest must be rebuilt from dist/ itself — not from
~/.claude, which would drag stale home-dir content back in. This tool does exactly that,
hashing the same way extract_config.py does so the two stay interchangeable:

  sha256(read_text().encode("utf-8"))   # read_text normalises CRLF->LF (universal newlines)

Usage:
  python tools/maintain/gen_manifest.py            # rewrite src/exoma/_bundle/manifest.json
  python tools/maintain/gen_manifest.py --check    # exit 1 if manifest is stale vs dist/
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from exoma.config.providers import PROVIDER_BUNDLES  # noqa: E402

DIST_DIR = REPO_ROOT / "src" / "exoma" / "_bundle"
DIST_CONFIG = DIST_DIR / "config"
MANIFEST = DIST_DIR / "manifest.json"

# Same one-time target map extract_config.py writes, so a rebuilt manifest is byte-identical
# in structure to a bootstrapped one.
def _provider_targets() -> dict:
    """Destination map for every declared provider bundle (config/providers.py)."""
    targets = {}
    for name, bundle in PROVIDER_BUNDLES.items():
        home = f"{{{{HOME}}}}/{bundle['home_dir']}"
        dist_name, dest_name = bundle["instructions"]
        targets[f"{name}/{dist_name}"] = f"{home}/{dest_name}"
        if bundle.get("agents_dir"):
            targets[f"{name}/{bundle['agents_dir']}"] = f"{home}/{bundle['agents_dir']}"
        if bundle.get("global_config"):
            dist_name, dest_name = bundle["global_config"]
            targets[f"{name}/{dist_name}"] = f"{home}/{dest_name}"
        if bundle.get("project_config"):
            dist_name, dest_name = bundle["project_config"]
            targets[f"{name}/{dist_name}"] = f"{{{{PROJECT_ROOT}}}}/{dest_name}"
    return targets


TARGETS = {
    "claude/CLAUDE.md": "{{HOME}}/.claude/CLAUDE.md",
    "claude/skills": "{{HOME}}/.claude/skills",
    "claude/commands": "{{HOME}}/.claude/commands",
    "claude/hooks": "{{HOME}}/.claude/hooks",
    "claude/settings.template.json": "{{HOME}}/.claude/settings.json",
    **_provider_targets(),
}


def _dist_files() -> list[str]:
    """dist-relative paths in the same order extract_config.py emits them."""
    paths: list[str] = ["claude/CLAUDE.md"]
    skills = DIST_CONFIG / "claude" / "skills"
    if skills.is_dir():
        paths += [
            f"claude/skills/{p.name}"
            for p in sorted(skills.glob("*.md"))
            if p.is_file()
        ]
    commands = DIST_CONFIG / "claude" / "commands"
    if commands.is_dir():
        paths += [
            f"claude/commands/{p.name}"
            for p in sorted(commands.glob("*.md"))
            if p.is_file()
        ]
    hooks = DIST_CONFIG / "claude" / "hooks"
    if hooks.is_dir():
        found = sorted(p for p in hooks.iterdir() if p.is_file())
        paths += [f"claude/hooks/{p.name}" for p in found]
    for name, bundle in PROVIDER_BUNDLES.items():
        dist_name, _ = bundle["instructions"]
        if (DIST_CONFIG / name / dist_name).is_file():
            paths.append(f"{name}/{dist_name}")
        agents_dir = bundle.get("agents_dir")
        agents = DIST_CONFIG / name / agents_dir if agents_dir else None
        if agents is not None and agents.is_dir():
            paths += [
                f"{name}/{agents_dir}/{p.name}"
                for p in sorted(agents.glob("*.md"))
                if p.is_file()
            ]
    if (DIST_CONFIG / "claude" / "settings.template.json").is_file():
        paths.append("claude/settings.template.json")
    for name, bundle in PROVIDER_BUNDLES.items():
        for key in ("global_config", "project_config"):
            entry = bundle.get(key)
            if entry and (DIST_CONFIG / name / entry[0]).is_file():
                paths.append(f"{name}/{entry[0]}")
    return paths


def _merge_kind(rel: str) -> str:
    return (
        "replace"
        if "/skills/" in rel
        or "/commands/" in rel
        or "/agents/" in rel
        or "/hooks/" in rel
        else "merge"
    )


def _component(rel: str) -> str:
    """Which upgrade component a shipped file belongs to.

    prompt_bundle = the LLM-facing contract (CLAUDE.md, skills, AGENTS.md); runtime =
    everything that is machine wiring (hooks, settings template).
    """
    # Each provider's instruction file is prompt_bundle for the same reason CLAUDE.md is:
    # it is contract text an LLM reads. Derived from the bundles so a second provider is
    # classified correctly instead of silently landing in `runtime`.
    instructions = {
        f"{name}/{bundle['instructions'][0]}"
        for name, bundle in PROVIDER_BUNDLES.items()
    }
    if (
        "/skills/" in rel
        or "/commands/" in rel
        or "/agents/" in rel
        or rel == "claude/CLAUDE.md"
        or rel in instructions
    ):
        return "prompt_bundle"
    return "runtime"


def _build() -> dict:
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from exoma.config.settings import COMPONENT_VERSIONS, TOOL_VERSION

    files_meta = []
    for rel in _dist_files():
        text = (DIST_CONFIG / rel).read_text(encoding="utf-8")
        blob = text.encode("utf-8")
        files_meta.append(
            {
                "path": rel,
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob),
                "merge": _merge_kind(rel),
                "component": _component(rel),
            }
        )
    return {
        "version": TOOL_VERSION,
        "versions": dict(COMPONENT_VERSIONS),
        "files": files_meta,
        "targets": TARGETS,
    }


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    built = json.dumps(_build(), indent=2) + "\n"

    if check_only:
        current = MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""
        if current == built:
            print("[gen_manifest] OK — manifest in sync with the bundle")
            return 0
        print(
            "[gen_manifest] STALE — manifest does not match the bundle. Run: python tools/maintain/gen_manifest.py"
        )
        return 1

    MANIFEST.write_text(built, encoding="utf-8", newline="\n")
    print(f"[gen_manifest] wrote {MANIFEST.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
