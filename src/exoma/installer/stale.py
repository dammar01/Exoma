"""Files an older install wrote that this release no longer ships.

`--apply` used to only add and overwrite. A skill, command, hook, or provider agent that a
release renamed or dropped stayed in ~/.claude forever, still loaded, still describing a
workflow that no longer exists — and a settings.json hook entry kept calling a script
nobody ships.

What counts as ours is decided by evidence, never by directory: ~/.claude/skills also
holds the user's own skills. A file is a removal candidate only when
  1. an earlier install recorded writing it (the ledger below, or any install receipt), AND
  2. its key sits in a family this installer manages file-by-file (skills, commands, hooks,
     provider agents), AND
  3. this release does not ship it, AND
  4. its content is still byte-for-byte what an install wrote.
A file that fails (4) was edited by the user after we wrote it: it is kept, and named.
Every removal is backed up and receipted, so `--rollback` restores it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from exoma.config.providers import PROVIDER_BUNDLES
from exoma.config.bundle_keys import canonical_key, provider_dir
from exoma.installer.base import (
    AGY_HOOKS_PREFIX,
    AGY_SKILLS_PREFIX,
    CLAUDE_HOOKS_PREFIX,
    CLAUDE_HOST,
    DIST_CONFIG,
    HOME,
    _RECEIPT,
    _flush_receipt,
    Plan,
    _backup,
    _file_sha256,
)

LEDGER = HOME / ".claude" / ".workflow-installed.json"
_LEDGER_SCHEMA = 1
_SCRIPT_STEM = re.compile(r"([\w.-]+)\.(?:ps1|sh)\b")


def managed_prefixes() -> tuple[str, ...]:
    """Manifest-key prefixes of the families installed one file at a time."""
    # The Agy host's skills and hook scripts too: a host the user stops choosing leaves its
    # files out of the current targets, and this prune is what takes them away.
    prefixes = [f"{CLAUDE_HOST}/skills/", f"{CLAUDE_HOST}/commands/", CLAUDE_HOOKS_PREFIX, AGY_SKILLS_PREFIX, AGY_HOOKS_PREFIX]
    for name, bundle in PROVIDER_BUNDLES.items():
        if bundle.get("agents_dir"):
            prefixes.append(f"{provider_dir(name)}/{bundle['agents_dir']}/")
        # Instructions copied whole (Agy's custom agent) have no block for --uninstall to cut:
        # the file itself is ours, and goes like any other managed file.
        if bundle.get("instructions_whole"):
            prefixes.append(f"{provider_dir(name)}/{bundle['instructions'][0]}")
    return tuple(prefixes)


def _receipts() -> list[dict]:
    from exoma.installer.rollback import _backup_dirs

    entries: list[dict] = []
    for directory in _backup_dirs():
        try:
            receipt = json.loads((directory / "install_receipt.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in receipt.get("entries") or []:
            if isinstance(item, dict):
                entries.append(item)
    return entries


def previously_installed() -> dict[str, dict]:
    """`{dest: {"key", "hashes"}}` for every managed file an earlier install recorded writing."""
    known: dict[str, dict] = {}
    prefixes = managed_prefixes()

    def remember(dest: str, key: str, sha: object) -> None:
        # Read in the current layout: an older install recorded `claude/...` and the like.
        key = canonical_key(key)
        if not isinstance(sha, str) or not key.startswith(prefixes):
            return
        known.setdefault(dest, {"key": key, "hashes": set()})["hashes"].add(sha)

    try:
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ledger = {}
    for dest, entry in (ledger.get("files") or {}).items():
        if isinstance(entry, dict):
            remember(dest, str(entry.get("key") or ""), entry.get("sha256"))
    for item in _receipts():
        if item.get("action") in ("create", "replace") and item.get("dest"):
            remember(str(item["dest"]), str(item.get("key") or ""), item.get("post_sha256"))
    return known


def remove_empty_skill_dir(path: Path, key: str) -> None:
    """An Agy skill is a directory (`.<name>/SKILL.md`): once its file goes, the directory
    goes too if nothing else is in it. A rollback recreates it with the file."""
    if key.split("/", 1)[-1].startswith(AGY_SKILLS_PREFIX) or key.startswith(AGY_SKILLS_PREFIX):
        try:
            path.parent.rmdir()
        except OSError:
            pass


def stale_files(current_dests: set[str]) -> tuple[list[tuple[Path, dict]], list[Path]]:
    """(removable, kept-because-edited) among recorded files this release does not ship."""
    current = {str(Path(d)) for d in current_dests}
    removable: list[tuple[Path, dict]] = []
    edited: list[Path] = []
    for dest, entry in sorted(previously_installed().items()):
        path = Path(dest)
        if str(path) in current or not path.is_file():
            continue
        if _file_sha256(path) in entry["hashes"]:
            removable.append((path, entry))
        else:
            edited.append(path)
    return removable, edited


def prune(
    current_dests: set[str], plan: Plan, apply: bool, backup_root: Path, keep_prefixes: tuple[str, ...] = ()
) -> list[str]:
    """Remove stale managed files. Returns the script stems of removed hooks.

    `keep_prefixes` keeps the files of those families: hook scripts a host still registers
    because its registration could not be taken out."""
    removable, edited = stale_files(current_dests)
    removed_stems: list[str] = []
    kept = 0
    for path, entry in removable:
        if keep_prefixes and entry["key"].startswith(keep_prefixes):
            kept += 1
            continue
        pre_sha256 = _file_sha256(path)
        key = f"stale/{entry['key']}"
        saved = _backup(path, backup_root, plan, apply, key)
        plan.add("remove", path, "installed by an earlier release, no longer shipped")
        if entry["key"].startswith(CLAUDE_HOOKS_PREFIX):
            match = _SCRIPT_STEM.search(path.name)
            if match:
                removed_stems.append(match.group(1).lower())
        if apply:
            path.unlink()
            remove_empty_skill_dir(path, entry["key"])
            _RECEIPT.append(
                {
                    "action": "remove",
                    "key": key,
                    "dest": str(path),
                    "backup": str(saved) if saved else None,
                    "pre_sha256": pre_sha256,
                    "post_sha256": None,
                }
            )
            _flush_receipt()
    if kept:
        plan.warn(
            f"kept {kept} hook script(s) of a host no longer chosen: its hook registration could "
            "not be read, so it still runs them; fix that file, then install again"
        )
    for path in edited:
        plan.warn(
            f"{path} was installed by an earlier release and is no longer shipped, but it "
            "changed since — kept. Delete it yourself if it is unused."
        )
    return removed_stems


def write_ledger(targets: list[tuple[Path, Path, str]], apply: bool) -> None:
    """Record what this install left in place, so the next one can tell ours from yours."""
    if not apply:
        return
    prefixes = managed_prefixes()
    files = {}
    for _source, dest, key in targets:
        sha = _file_sha256(dest)
        if sha and key.startswith(prefixes):
            files[str(dest)] = {"key": key, "sha256": sha}
    from exoma.core.workspace.workspace_paths import atomic_write_text

    atomic_write_text(
        LEDGER, json.dumps({"schema": _LEDGER_SCHEMA, "files": files}, indent=2) + "\n"
    )


def shipped_hook_stems() -> set[str]:
    """Every hook stem dist/ ships: one bash launcher (`<stem>.sh`) each. A `.ps1` an earlier
    release installed under the same stem is the same hook, so its settings entry is
    refreshed, not dropped, and the file itself goes as a stale file."""
    hooks = DIST_CONFIG / CLAUDE_HOST / "hooks"
    if not hooks.is_dir():
        return set()
    return {p.stem.lower() for p in hooks.iterdir() if p.suffix in (".ps1", ".sh")}


def retired_hook_stems() -> set[str]:
    """Hook stems an earlier install wrote that this release no longer ships."""
    shipped = shipped_hook_stems()
    stems = set()
    for entry in previously_installed().values():
        if entry["key"].startswith(CLAUDE_HOOKS_PREFIX):
            match = _SCRIPT_STEM.search(entry["key"].rsplit("/", 1)[-1])
            if match and match.group(1).lower() not in shipped:
                stems.add(match.group(1).lower())
    return stems
