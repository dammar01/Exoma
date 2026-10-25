"""Undoing an install: backup discovery, receipt replay, install-mode memory.

Every restore verifies destination and backup hashes before touching anything, so a
destination edited since the install is refused rather than silently overwritten.
"""

import json
import os
import shutil
from pathlib import Path

from exoma.installer.base import (
    HOME,
    Plan,
    _RECEIPT_SCHEMA_VERSION,
    _backup,
    _file_sha256,
    _record,
)

_MODE_FILE = HOME / ".claude" / ".workflow-install-mode.json"


def _stored_only_command() -> bool:
    """The persisted intent mode used when an upgrade supplies no mode flag."""
    try:
        return bool(
            json.loads(_MODE_FILE.read_text(encoding="utf-8")).get("only_command")
        )
    except (OSError, ValueError):
        return False


def _store_only_command(
    value: bool, plan: Plan, apply: bool, backup_root: Path
) -> None:
    content = json.dumps({"only_command": bool(value)}, indent=2) + "\n"
    detail = "command-only (prefix /. required)" if value else "auto-intent"
    if _MODE_FILE.is_file() and _MODE_FILE.read_bytes() == content.encode("utf-8"):
        plan.add("unchanged", _MODE_FILE, detail)
        return

    pre_sha256 = _file_sha256(_MODE_FILE)
    saved = (
        _backup(
            _MODE_FILE,
            backup_root,
            plan,
            apply,
            "claude/workflow-install-mode.json",
        )
        if _MODE_FILE.exists()
        else None
    )
    plan.add("mode", _MODE_FILE, detail)
    if apply:
        _MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MODE_FILE.write_text(content, encoding="utf-8")
        _record("mode", _MODE_FILE, "claude/workflow-install-mode.json", saved, pre_sha256)


def _backup_dirs() -> list[Path]:
    """Timestamped install backup dirs, newest first."""
    root = HOME / ".claude" / "backups"
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith("install_")),
        key=lambda p: p.name,
        reverse=True,
    )


def _run_rollback(which: str | None, apply: bool) -> int:
    """Undo one install from its receipt. Dry run by default, like install itself."""
    dirs = _backup_dirs()
    if not dirs:
        print("[ROLLBACK] no install backups found under ~/.claude/backups/")
        return 1
    if which:
        chosen = next(
            (d for d in dirs if d.name == which or d.name == f"install_{which}"), None
        )
        if chosen is None:
            print(f"[ROLLBACK] no backup named {which}. Available:")
            for d in dirs[:10]:
                print(f"  {d.name}")
            return 1
    else:
        chosen = dirs[0]

    receipt_path = chosen / "install_receipt.json"
    if not receipt_path.is_file():
        # Pre-receipt installs left only the backup files. Restoring those blindly would
        # not undo the files the install CREATED, leaving a half-rolled-back config that
        # looks complete — refuse instead, and say what can be done by hand.
        print(f"[ROLLBACK] {chosen.name} has no install_receipt.json (installed by an")
        print("  older build). Its backups are still there and can be copied back by hand:")
        print(f"  {chosen}")
        return 1

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[ROLLBACK] invalid receipt: {exc}")
        return 1
    entries = receipt.get("entries", [])
    if (
        receipt.get("schema_version") != _RECEIPT_SCHEMA_VERSION
        or not isinstance(entries, list)
    ):
        print(f"[ROLLBACK] {chosen.name} uses an unsupported receipt schema.")
        print("  Refusing an unverified rollback; restore its backups manually if needed.")
        return 1

    print(f"[ROLLBACK] {chosen.name} ({'APPLY' if apply else 'DRY RUN'})")
    if receipt.get("complete") is False:
        # Written step by step, and this install never reached its final write. Every entry
        # is a step that finished (the entry is recorded after its file), so undoing them is
        # sound; a step that died mid-write has no entry and its backup is copied by hand.
        print("  NOTE: that install was interrupted; undoing the steps it recorded.")
        print(f"  Any step it did not record still has its backup under {chosen}.")
    conflicts: list[str] = []
    for item in entries:
        if not isinstance(item, dict) or not item.get("dest"):
            conflicts.append("receipt contains an invalid entry")
            continue
        dest = Path(item["dest"])
        backup = Path(item["backup"]) if item.get("backup") else None
        expected_post = item.get("post_sha256")
        actual_post = _file_sha256(dest)
        if item.get("action") == "remove":
            # A stale file the install deleted: undoing it means the path is still free.
            if actual_post is not None:
                conflicts.append(f"{dest}: removed by that install, but a file is there again")
        elif not isinstance(expected_post, str) or actual_post != expected_post:
            conflicts.append(
                f"{dest}: destination changed "
                f"(expected {expected_post or 'missing hash'}, found {actual_post or 'missing'})"
            )
        expected_pre = item.get("pre_sha256")
        if backup is not None:
            backup_hash = _file_sha256(backup)
            if not isinstance(expected_pre, str) or backup_hash != expected_pre:
                conflicts.append(
                    f"{backup}: backup changed or missing "
                    f"(expected {expected_pre or 'missing hash'}, found {backup_hash or 'missing'})"
                )
        elif expected_pre is not None:
            conflicts.append(f"{dest}: receipt has a pre-install hash but no backup")

    if conflicts:
        print("  ABORTED: rollback preflight found conflicts; nothing was changed.")
        for conflict in conflicts:
            print(f"  !! {conflict}")
        return 2

    restores = [i for i in entries if i.get("backup")]
    deletes = [i for i in entries if not i.get("backup")]
    for item in restores:
        print(f"  restore  {item['dest']}")
    for item in deletes:
        print(f"  delete   {item['dest']} — created by that install")

    if not apply:
        print(f"\n  restore {len(restores)}, delete {len(deletes)}")
        print("  dry run — rerun with --apply to write")
        return 0

    # The preflight above is all-or-nothing, but the writes were not: a copy that failed
    # halfway left some files restored and some not, with nothing recording where it
    # stopped — and a second attempt then aborted, because the files already restored no
    # longer matched their post-install hash. So copy everything to a sibling temp FIRST.
    # A staging failure changes nothing; only same-directory renames run after that, which
    # is the cheapest step that can fail.
    staged: list[tuple[Path, Path]] = []
    try:
        for item in restores:
            dest = Path(item["dest"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f"{dest.name}.{os.getpid()}.rollback")
            shutil.copy2(Path(item["backup"]), tmp)
            staged.append((tmp, dest))
    except OSError as exc:
        for tmp, _ in staged:
            try:
                tmp.unlink()
            except OSError:
                pass
        print(f"  ABORTED while staging: {exc}")
        print("  nothing was changed.")
        return 2

    restored = deleted = 0
    failures: list[str] = []
    for tmp, dest in staged:
        try:
            os.replace(tmp, dest)
            restored += 1
        except OSError as exc:
            failures.append(f"{dest}: {exc}")
            try:
                tmp.unlink()  # the staged copy is litter once its rename failed
            except OSError:
                pass
    for item in deletes:
        dest = Path(item["dest"])
        try:
            dest.unlink(missing_ok=True)
            deleted += 1
        except OSError as exc:
            failures.append(f"{dest}: {exc}")

    print(f"\n  restore {restored}/{len(staged)}, delete {deleted}/{len(deletes)}")
    if failures:
        # Say exactly what is still in place. A partial rollback that reports success is
        # worse than one that fails loudly: the next install builds on an unknown state.
        print("  PARTIAL — these were not rolled back:")
        for failure in failures:
            print(f"  !! {failure}")
        print(f"  backups remain at {chosen}")
        return 2
    return 0
