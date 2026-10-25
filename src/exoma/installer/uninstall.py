"""`install.py --uninstall`: take out what the workflow installed, leave what the user wrote.

An install is additive: CLAUDE.md and every provider AGENTS.md get one marker-delimited
block, settings.json gets hook entries and a statusLine, and skills/commands/hooks/agents
are files the installer recorded writing. Uninstall reverses exactly that much:

- the managed block is cut out of each instruction file; a file left with nothing else in
  it is removed, a file with the user's own text keeps that text;
- a managed file goes only when its content is still what an install wrote (the ledger or a
  receipt says so); an edited one is kept and named;
- settings.json loses the commands that run a shipped (or retired) workflow script and a
  workflow statusLine. Every other key stays, including ones an older release seeded
  (`model`, plugins): nothing records whether the user has come to rely on them.

Everything goes through the same backup + receipt an install uses, so `--rollback` undoes an
uninstall. Dry run unless `--apply`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from exoma.installer.base import (
    HOME,
    MARKERS,
    _RECEIPT,
    Plan,
    _backup,
    _begin_receipt,
    _discard_empty_receipt,
    _file_sha256,
    _flush_receipt,
    _managed_block,
    _marker_problem,
    _read_text_lenient,
    _record,
    _targets,
)
from exoma.installer.settings import _hook_script_ids
from exoma.installer.stale import (
    LEDGER,
    managed_prefixes,
    previously_installed,
    retired_hook_stems,
    shipped_hook_stems,
)

MODE_FILE = HOME / ".claude" / ".workflow-install-mode.json"


def _without_block(text: str, start: str, end: str) -> str | None:
    """`text` with its managed block cut out, or None when it has none."""
    block = _managed_block(text, start, end)
    if block is None:
        return None
    at = text.index(block)
    before, after = text[:at], text[at + len(block):]
    if not (before + after).strip():
        return ""
    # The install separated its block from the user's text with one blank line. Only that
    # seam is normalised: blank runs anywhere else in the user's text are theirs, byte for byte.
    before, after = before.rstrip("\n"), after.lstrip("\n")
    if not before.strip():
        return after
    if not after:
        return before + "\n"
    return before + "\n\n" + after


def _remove_receipted(path: Path, key: str, plan: Plan, apply: bool, backup_root: Path, why: str) -> None:
    pre_sha256 = _file_sha256(path)
    saved = _backup(path, backup_root, plan, apply, f"uninstall/{key}")
    plan.add("remove", path, why)
    if apply:
        path.unlink()
        _RECEIPT.append(
            {
                "action": "remove",
                "key": f"uninstall/{key}",
                "dest": str(path),
                "backup": str(saved) if saved else None,
                "pre_sha256": pre_sha256,
                "post_sha256": None,
            }
        )
        _flush_receipt()


def _uninstall_text(dest: Path, key: str, plan: Plan, apply: bool, backup_root: Path) -> None:
    if not dest.is_file():
        return
    start, end = MARKERS[key]
    text = _read_text_lenient(dest)
    problem = _marker_problem(text, start, end)
    if problem:
        plan.warn(f"{dest}: workflow markers do not pair up ({problem}) — left as is; remove the block by hand")
        return
    remaining = _without_block(text, start, end)
    if remaining is None:
        plan.add("unchanged", dest, "no workflow block")
        return
    if not remaining:
        _remove_receipted(dest, key, plan, apply, backup_root, "held only the workflow block")
        return
    pre_sha256 = _file_sha256(dest)
    saved = _backup(dest, backup_root, plan, apply, f"uninstall/{key}")
    plan.add("merge", dest, "removed the workflow block, kept your text")
    if apply:
        dest.write_text(remaining, encoding="utf-8")
        _record("merge", dest, f"uninstall/{key}", saved, pre_sha256)


HOOKS_PREFIX = "claude/hooks/"


def _uninstall_files(plan: Plan, apply: bool, backup_root: Path, keep_hooks: bool = False) -> None:
    """Remove the managed files an install wrote. `keep_hooks` keeps every hook script:
    settings.json could not be cleaned, so it still runs them, and a deleted script would
    turn each event it is wired to into a hook error."""
    prefixes = managed_prefixes()
    known = previously_installed()
    candidates = {str(dest): key for _src, dest, key in _targets() if key.startswith(prefixes)}
    candidates.update({dest: entry["key"] for dest, entry in known.items()})
    kept_hooks = 0
    for dest, key in sorted(candidates.items()):
        path = Path(dest)
        if not path.is_file():
            continue
        if keep_hooks and key.startswith(HOOKS_PREFIX):
            kept_hooks += 1
            continue
        hashes = (known.get(dest) or {}).get("hashes") or set()
        if _file_sha256(path) in hashes:
            _remove_receipted(path, key, plan, apply, backup_root, "installed by the workflow")
        else:
            plan.warn(f"{path} looks like a workflow file but is not what an install wrote — kept")
    if kept_hooks:
        plan.warn(
            f"kept {kept_hooks} hook script(s) under ~/.claude/hooks: settings.json still runs them; "
            "remove the workflow hooks from it, then run --uninstall again"
        )


def _strip_settings(settings: dict, stems: set[str]) -> tuple[dict, list[str]]:
    out = json.loads(json.dumps(settings))
    changes: list[str] = []
    hooks = out.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            entries = hooks[event]
            if not isinstance(entries, list):
                continue
            kept_entries = []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                    kept_entries.append(entry)
                    continue
                kept_hooks = []
                for hook in entry["hooks"]:
                    ids = _hook_script_ids({"hooks": [hook]})
                    if ids and ids <= stems:
                        changes.append(f"hooks.{event}: {', '.join(sorted(ids))}")
                    else:
                        kept_hooks.append(hook)
                if kept_hooks:
                    kept_entries.append({**entry, "hooks": kept_hooks})
            if kept_entries:
                hooks[event] = kept_entries
            else:
                del hooks[event]
        if not hooks:
            del out["hooks"]
    status = out.get("statusLine")
    if isinstance(status, dict):
        ids = _hook_script_ids({"hooks": [status]})
        if ids and ids <= stems:
            del out["statusLine"]
            changes.append("statusLine")
    return out, changes


def _uninstall_settings(plan: Plan, apply: bool, backup_root: Path) -> bool:
    """Strip the workflow hooks from settings.json. False when it could not be read, which
    leaves it still running them: the caller then keeps the hook scripts."""
    dest = HOME / ".claude" / "settings.json"
    if not dest.is_file():
        return True
    try:
        # Lenient like the install's own read: a BOM or a cp1252 byte left by an editor is no
        # reason to leave every workflow hook registered.
        current = json.loads(_read_text_lenient(dest))
    except (OSError, ValueError):
        plan.warn(f"{dest} is not readable JSON — left as is; remove the workflow hooks by hand")
        return False
    if not isinstance(current, dict):
        plan.warn(f"{dest} is not a JSON object — left as is; remove the workflow hooks by hand")
        return False
    stripped, changes = _strip_settings(current, shipped_hook_stems() | retired_hook_stems())
    if not changes:
        plan.add("unchanged", dest, "no workflow hooks")
        return True
    pre_sha256 = _file_sha256(dest)
    saved = _backup(dest, backup_root, plan, apply, "uninstall/claude/settings.json")
    plan.add("merge", dest, "removed " + "; ".join(changes))
    if apply:
        dest.write_text(json.dumps(stripped, indent=2) + "\n", encoding="utf-8")
        _record("merge", dest, "uninstall/claude/settings.json", saved, pre_sha256)
    return True


def run_uninstall(apply: bool) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    backup_root = HOME / ".claude" / "backups" / f"install_{stamp}"
    plan = Plan()
    print(f"[UNINSTALL] Exoma ({'APPLY' if apply else 'DRY RUN'})")
    print(f"  home:   {HOME}")
    print(f"  backup: {backup_root}")
    print()
    if apply:
        _begin_receipt(
            backup_root / "install_receipt.json",
            {"installed_at": datetime.now(timezone.utc).isoformat(), "uninstall": True},
        )
    for _src, dest, key in _targets():
        if key in MARKERS:
            _uninstall_text(dest, key, plan, apply, backup_root)
    # Settings first: a hook script deleted while settings.json still registers it fails on
    # every event it is wired to, so the scripts go only once nothing runs them.
    settings_clean = _uninstall_settings(plan, apply, backup_root)
    _uninstall_files(plan, apply, backup_root, keep_hooks=not settings_clean)
    # Receipted like everything else: a rollback that restored the files but not the mode
    # would reinstall the other intent mode, and one without the ledger could no longer tell
    # the workflow's files from the user's.
    for path, key in ((LEDGER, "claude/.workflow-installed.json"), (MODE_FILE, "claude/.workflow-install-mode.json")):
        if path.is_file():
            _remove_receipted(path, key, plan, apply, backup_root, "installer bookkeeping")
    plan.warn(
        "not touched: config/second_agent.seed.json, an AGENT_PATH persisted by --set-env, "
        "and every project's .workflow/ — remove those yourself if you want them gone"
    )
    if apply and _RECEIPT:
        _flush_receipt(complete=True)
    elif apply:
        _discard_empty_receipt()

    counts: dict[str, int] = {}
    for verb, target, detail in plan.actions:
        counts[verb] = counts.get(verb, 0) + 1
        suffix = f"  — {detail}" if detail else ""
        print(f"  {verb:9} {target}{suffix}")
    print()
    print("[UNINSTALL REPORT]")
    print("  " + (" | ".join(f"{verb}: {n}" for verb, n in sorted(counts.items())) or "nothing to do"))
    for warning in plan.warnings:
        print(f"    ! {warning}")
    print()
    if apply:
        print("  undo with: python install.py --rollback --apply")
    else:
        print("  DRY RUN — nothing was written. Re-run with --apply to uninstall.")
    return 0
