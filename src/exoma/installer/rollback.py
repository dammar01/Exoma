"""Undoing an install: backup discovery, receipt replay, install-mode memory.

Every restore verifies destination and backup hashes before touching anything, so a
destination edited since the install is refused rather than silently overwritten.
"""

import json
import os
import shutil
import uuid
from pathlib import Path

from exoma.core.workspace.workspace_paths import atomic_write_text
from exoma.utils import osutil
from exoma.installer.base import (
    AGY_HOST_HOOK_NAME,
    CLAUDE_HOST,
    HOME,
    HOSTS,
    agy_hooks_dest,
    agy_hooks_state,
    codex_hook_events_present,
    codex_hooks_dest,
    codex_hooks_state,
    Plan,
    _RECEIPT_SCHEMA_VERSION,
    _backup,
    _file_sha256,
    _record,
    _save_displaced_conflict,
)

_MODE_FILE = HOME / ".claude" / ".workflow-install-mode.json"


def _stored_mode() -> dict:
    """The install mode file as written by the last install, `{}` when there is none."""
    try:
        loaded = json.loads(_MODE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _stored_only_command() -> bool:
    """The persisted intent mode used when an upgrade supplies no mode flag."""
    return bool(_stored_mode().get("only_command"))


def _stored_hosts() -> tuple[str, ...] | None:
    """The main-agent hosts the last install chose, or None when it recorded none (no
    install yet, or one from before the choice existed — see install._migrated_hosts)."""
    hosts = _stored_mode().get("hosts")
    if not isinstance(hosts, list):
        return None
    chosen = tuple(h for h in HOSTS if h in hosts)
    return chosen or None


def _migrated_hosts() -> tuple[str, ...] | None:
    """The hosts an install from before the choice existed left, read from what it wrote:
    Claude always (every earlier install took it), Agy when its hooks.json still carries
    Exoma's hook. None on a machine with no earlier install. Codex postdates the choice, so
    it appears here only when the mode file was lost: added when ~/.codex/hooks.json
    registers Exoma's lifecycle hooks, the same test doctor uses."""
    from exoma.installer.stale import LEDGER

    if not (_MODE_FILE.exists() or LEDGER.exists()):
        return None
    current, _refusal = agy_hooks_state(agy_hooks_dest())
    hosts = ("claude", "agy") if current and AGY_HOST_HOOK_NAME in current else ("claude",)
    codex_current, _codex_refusal = codex_hooks_state(codex_hooks_dest())
    # Registered at all, not launchable: a pre-release command without the call operator is
    # still Exoma's, and this install repairs it rather than withdrawing the host.
    if codex_current and codex_hook_events_present(codex_current.get("hooks"), launchable=False):
        hosts += ("codex",)
    from exoma.installer.base import opencode_home

    # OpenCode's only registration is the plugin file itself.
    if (opencode_home() / "plugins" / "exoma.js").is_file():
        hosts += ("opencode",)
    return hosts


def _installed_hosts() -> tuple[str, ...]:
    """The hosts the installed state belongs to, for --check: the recorded choice, else the
    migration, else Claude."""
    return _stored_hosts() or _migrated_hosts() or ("claude",)


def _stored_agy_permissions() -> tuple[bool | None, list[str]]:
    """(the opt-in as last chosen — None when never asked, the rules Exoma added)."""
    mode = _stored_mode()
    enabled = mode.get("agy_permissions")
    rules = mode.get("agy_permission_rules")
    return (
        enabled if isinstance(enabled, bool) else None,
        [r for r in rules if isinstance(r, str)] if isinstance(rules, list) else [],
    )


def _stored_agy_opt_out() -> bool:
    """Whether the user turned the Agy permission rules off (--no-agy-permissions). Only an
    explicit opt-out is kept: an `agy_permissions: false` without it was a default, from a
    build where the rules were opt-in, and does not stop them now."""
    return _stored_mode().get("agy_permissions_opt_out") is True


def _stored_codex_auto_review_context(hosts: tuple[str, ...] | None = None) -> bool:
    from exoma.installer.codex_policy import resolve_context

    return resolve_context(_stored_mode(), _installed_hosts() if hosts is None else hosts)[0]


def _store_install_mode(
    only_command: bool,
    hosts: tuple[str, ...],
    agy_permissions: bool,
    agy_rules: list[str],
    agy_opt_out: bool,
    plan: Plan,
    apply: bool,
    backup_root: Path,
    codex_auto_review_context: bool | None = None,
    codex_auto_review_context_opt_out: bool | None = None,
) -> None:
    """Persist every choice an upgrade without flags must keep: the intent mode, the hosts,
    the Agy permission rules' state, explicit Agy/Codex opt-outs, and the rules Exoma added
    (what --uninstall may remove). Legacy Codex false is not an explicit opt-out."""
    if codex_auto_review_context_opt_out is None:
        codex_auto_review_context_opt_out = _stored_mode().get("codex_auto_review_context_opt_out") is True
    content = json.dumps(
        {
            "only_command": bool(only_command),
            "hosts": list(hosts),
            "agy_permissions": bool(agy_permissions),
            "agy_permissions_opt_out": bool(agy_opt_out),
            "agy_permission_rules": list(agy_rules),
            **({"codex_auto_review_context": codex_auto_review_context}
               if codex_auto_review_context is not None else {}),
            "codex_auto_review_context_opt_out": codex_auto_review_context_opt_out,
        },
        indent=2,
    ) + "\n"
    detail = (
        f"{'command-only (prefix /. required)' if only_command else 'auto-intent'}; "
        f"hosts: {', '.join(hosts)}"
        + (f"; agy permissions: {'on' if agy_permissions else 'off'}" if "agy" in hosts else "")
    )
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
            f"{CLAUDE_HOST}/workflow-install-mode.json",
        )
        if _MODE_FILE.exists()
        else None
    )
    plan.add("mode", _MODE_FILE, detail)
    if apply:
        _MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MODE_FILE.write_text(content, encoding="utf-8")
        _record("mode", _MODE_FILE, f"{CLAUDE_HOST}/workflow-install-mode.json", saved, pre_sha256)


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


def _rollback_paths(item, transaction, index):
    dest = Path(item["dest"])
    sibling = f"{dest.name}.{transaction}.{index}"
    return {
        "dest": str(dest), "action": item.get("action"),
        "pre_sha256": item.get("pre_sha256"), "post_sha256": item.get("post_sha256"),
        "staging": str(dest.with_name(sibling + ".rollback")),
        "capture": str(dest.with_name(sibling + (".displaced" if item.get("backup") else ".rollback-delete"))),
    }


def _remove_staging(path, failures):
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        failures.append(f"cleanup of {path} failed: {exc}")


def _run_rollback(which: str | None, apply: bool) -> int:
    """Undo one install from its receipt. Dry run unless `--apply` (install itself writes by default)."""
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
        if not apply:
            return _replay_receipt(chosen, receipt_path, False)
        with osutil.open_lock_file(chosen / ".rollback.lock") as lock:
            osutil.lock_file(lock)
            return _replay_receipt(chosen, receipt_path, True)
    except OSError as exc:
        print(f"[ROLLBACK] unable to complete rollback of {chosen}: {exc}")
        return 2


def _replay_receipt(chosen: Path, receipt_path: Path, apply: bool) -> int:
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
    pending_noop_indices: set[int] = set()
    journal = receipt.get("rollback")
    try:
        transaction = uuid.UUID(journal["transaction"]).hex if journal is not None else uuid.uuid4().hex
        operations = [_rollback_paths(item, transaction, idx) for idx, item in enumerate(entries)]
        if journal is not None and journal["operations"] != operations:
            raise ValueError("operation list differs from the install receipt")
    except (KeyError, ValueError, AttributeError, TypeError) as exc:
        print(f"  ABORTED: invalid rollback receipt: {exc}")
        return 2
    completed = journal.get("completed", []) if journal is not None else []
    if not isinstance(completed, list) or any(type(i) is not int or not 0 <= i < len(entries) for i in completed):
        print("  ABORTED: invalid completed entries in rollback receipt.")
        return 2
    completed_files = []
    for idx, item in enumerate(entries):
        if not isinstance(item, dict) or not item.get("dest"):
            conflicts.append("receipt contains an invalid entry")
            continue
        dest = Path(item["dest"])
        backup = Path(item["backup"]) if item.get("backup") else None
        expected_post = item.get("post_sha256")
        expected_pre = item.get("pre_sha256")
        actual_post = _file_sha256(dest)
        if journal is not None:
            operation = operations[idx]
            staging = Path(operation["staging"])
            capture = Path(operation["capture"])
            at_goal = (isinstance(expected_pre, str) and actual_post == expected_pre
                       if backup else not os.path.lexists(dest))
            if idx in completed and not at_goal:
                conflicts.append(f"{dest}: changed after its rollback completed; retained at destination")
                continue
            if at_goal:
                # The mutation landed. Never remove an unrecognized concurrent edit.
                capture_hash = expected_pre if item.get("action") == "remove" else expected_post
                for leftover, allowed in ((capture, (capture_hash,)), (staging, (expected_pre, expected_post))):
                    if os.path.lexists(leftover):
                        digest = _file_sha256(leftover)
                        if not digest or digest not in allowed or leftover.is_symlink():
                            conflicts.append(f"{dest}: interrupted rollback; inspect retained edit at {leftover}")
                        else:
                            completed_files.append((leftover, digest))
                pending_noop_indices.add(idx)
                print(f"  already restored  {dest}")
                continue
            if os.path.lexists(staging) and (staging.is_symlink() or _file_sha256(staging) != expected_pre):
                conflicts.append(f"{dest}: unrecognized staging contents retained at {staging}")
                continue
            if os.path.lexists(staging.with_suffix(".rollback.partial")):
                print(f"  interrupted copy will be retained and rebuilt for {dest}")
            if os.path.lexists(capture):
                conflicts.append(f"{dest}: interrupted rollback; inspect retained file at {capture}")
                continue
        if item.get("pending") and actual_post == expected_post:
            recovery = [item.get(field) for field in ("capture_path", "staging_path")]
            retained = [path for path in recovery if isinstance(path, str) and Path(path).exists()]
            if any(_file_sha256(Path(path)) != expected_pre for path in retained):
                conflicts.append(f"{dest}: interrupted replacement; inspect retained bytes at {retained}")
                continue
        if item.get("action") == "remove":
            # A stale file the install deleted: undoing it means the path is still free.
            if actual_post is not None:
                conflicts.append(f"{dest}: removed by that install, but a file is there again")
        elif item.get("pending") is True and isinstance(expected_pre, str) and actual_post == expected_pre:
            # A pending atomic replacement interrupted before rename never changed `dest`:
            # already at the original hash, so skip restoring it while still flagging any
            # unexpected third hash as a conflict.
            pending_noop_indices.add(idx)
            continue
        elif not isinstance(expected_post, str) or actual_post != expected_post:
            conflicts.append(
                f"{dest}: destination changed "
                f"(expected {expected_post or 'missing hash'}, found {actual_post or 'missing'})"
            )
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

    active_entries = [item for idx, item in enumerate(entries) if idx not in pending_noop_indices]
    restores = [i for i in active_entries if i.get("backup")]
    deletes = [i for i in active_entries if not i.get("backup")]
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
    failures: list[str] = []
    if journal is None:
        receipt["rollback"] = {"transaction": transaction, "operations": operations}
        atomic_write_text(receipt_path, json.dumps(receipt, indent=2) + "\n", fsync=True)
    indices = {id(item): i for i, item in enumerate(entries)}

    def mark_completed(index):
        if index not in completed:
            completed.append(index)
            receipt["rollback"]["completed"] = completed
            atomic_write_text(receipt_path, json.dumps(receipt, indent=2) + "\n", fsync=True)

    # A retry can observe the native mutation before the preceding process recorded it.
    for index in sorted(pending_noop_indices):
        mark_completed(index)
    staged: list[tuple[Path, Path, dict]] = []
    try:
        for item in restores:
            dest = Path(item["dest"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(operations[indices[id(item)]]["staging"])
            if journal is None or not os.path.lexists(tmp):
                partial = tmp.with_suffix(".rollback.partial")
                if journal is not None and os.path.lexists(partial):
                    # A terminated copy never became publishable. Preserve its bytes and
                    # restart from the validated backup, using the reserved staging name.
                    retained = partial.with_name(f"{partial.name}.{uuid.uuid4().hex}.retained")
                    partial.rename(retained)
                    print(f"  retained interrupted copy at {retained}")
                with partial.open("xb") as target:
                    with Path(item["backup"]).open("rb") as source:
                        shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
                if _file_sha256(partial) != item["pre_sha256"]:
                    raise OSError(f"staged copy differs from receipt for {dest}; retained at {partial}")
                shutil.copystat(item["backup"], partial)
                os.replace(partial, tmp)
                staged.append((tmp, dest, item))
            elif tmp.is_symlink() or _file_sha256(tmp) != item["pre_sha256"]:
                raise OSError(f"unrecognized staging contents retained at {tmp}")
            else:
                staged.append((tmp, dest, item))
            if _file_sha256(tmp) != item["pre_sha256"]:
                raise OSError(f"staged copy differs from receipt for {dest}")
    except OSError as exc:
        for tmp, _, _ in staged:
            try:
                tmp.unlink()
            except OSError:
                pass
        print(f"  ABORTED while staging: {exc}")
        print("  nothing was changed.")
        return 2

    for path, digest in completed_files:
        if _file_sha256(path) == digest:
            _remove_staging(path, failures)
        else:
            failures.append(f"retained changed recovery file at {path}")
    restored = deleted = 0
    for tmp, dest, item in staged:
        displaced = Path(operations[indices[id(item)]]["capture"])
        keep_staging = False
        try:
            if tmp.is_symlink() or _file_sha256(tmp) != item["pre_sha256"]:
                keep_staging = True
                raise OSError(f"staged copy changed before restore; retained at {tmp}")
            if item.get("action") == "remove":
                # Claim the journaled capture name before linking. If the receipt
                # write fails, retry can see the attempted restore even when the
                # user subsequently removes the destination.
                tmp.rename(displaced)
                try:
                    os.link(displaced, dest)
                except (FileExistsError, PermissionError):
                    # The filesystem refused creation. Release this attempt so a
                    # later retry can proceed after the conflict or sharing lock clears.
                    displaced.unlink()
                    raise
                mark_completed(indices[id(item)])
                displaced.unlink()
            else:
                keep_staging = True
                try:
                    osutil.replace_preserving_displaced(tmp, dest, displaced)
                except osutil.ReplacementInterrupted:
                    raise
                except OSError:
                    keep_staging = False
                    raise
                keep_staging = False
                if _file_sha256(displaced) != item.get("post_sha256"):
                    rel_key = item.get("key") or dest.name
                    conflict_path = chosen / f"{rel_key}.rollback-displaced"
                    if conflict_path.exists():
                        conflict_path = conflict_path.with_name(f"{conflict_path.name}.{uuid.uuid4().hex}")
                    _save_displaced_conflict(displaced, conflict_path)
                    failures.append(f"{dest}: concurrent edit preserved at {conflict_path}")
                mark_completed(indices[id(item)])
                displaced.unlink()
            restored += 1
        except osutil.ReplacementInterrupted as exc:
            keep_staging = exc.recovery_path == tmp
            failures.append(f"{dest}: {exc}")
        except OSError as exc:
            failures.append(f"{dest}: {exc}")
            if displaced.exists():
                failures.append(f"{dest}: retained displaced file at {displaced}")
        finally:
            if not keep_staging:
                _remove_staging(tmp, failures)
    for item in deletes:
        dest = Path(item["dest"])
        captured = Path(operations[indices[id(item)]]["capture"])
        try:
            dest.rename(captured)
            if _file_sha256(captured) == item["post_sha256"]:
                mark_completed(indices[id(item)])
                captured.unlink()
                deleted += 1
            else:
                # Restore the concurrent edit only if nobody re-created the path.
                try:
                    os.link(captured, dest, follow_symlinks=False)
                except OSError:
                    failures.append(f"{dest}: concurrent edit during delete, retained at {captured}")
                else:
                    captured.unlink()
                    failures.append(f"{dest}: concurrent edit during delete, preserved at destination")
        except OSError as exc:
            failures.append(f"{dest}: {exc}; inspect {captured} if present")

    print(f"\n  restore {restored}/{len(staged)}, delete {deleted}/{len(deletes)}")
    if failures:
        # Say exactly what is still in place. A partial rollback that reports success is
        # worse than one that fails loudly: the next install builds on an unknown state.
        print("  PARTIAL — these were not rolled back:")
        for failure in failures:
            print(f"  !! {failure}")
        print(f"  backups remain at {chosen}")
        return 2
    receipt.pop("rollback")
    atomic_write_text(receipt_path, json.dumps(receipt, indent=2) + "\n", fsync=True)
    return 0
