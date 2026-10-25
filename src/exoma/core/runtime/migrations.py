"""Versioned workspace migrations, run by `upgrade` (and by init's auto-upgrade).

A migration is a step from one `LAYOUT_VERSION` to the next. Each is idempotent: it
decides from what is on disk whether there is anything to do, so a second upgrade is a
no-op and a half-finished one is finished or rolled back, never repeated on top of itself.

Layout 1 (v3.5.x) → 2 (v3.6.0): everything internal moves from the `.workflow` root into
`.workflow/data/`, leaving the root to the files a person edits. In order:

  1. back the whole `.workflow` tree up to `data/backups/<stamp>/` (last BACKUPS_KEPT kept);
  2. take the fact, evidence, browser-knowledge and promote locks, so no writer is
     mid-rewrite while its file moves;
  3. move every internal item into `.workflow/data.migrating/`, then rename that to
     `data/` in one step — the moment `data/` exists every reader (runtime, hooks, run
     scripts) switches layout, so it must never exist half-filled;
  4. point the evidence index's archived artifact paths at their new location;
  5. delete the pre-session root leftovers (`state.json`, `scope.json`,
     `command-cache.json`, `runtime/`, `logs/`) and the old lock files;
  6. strip config.json to overrides (values equal to a default, unknown or retired keys);
  7. convert a `secrets.json` still in the pre-list shape, keeping its values.

A failure before step 3's rename moves everything back and removes the temporary
directory; the backup is kept either way and its path reported.

Steps 4-7 run after the rename, where moving back is no longer the safe direction: every
reader has already switched to data/. They roll forward instead. The rename carries a
`.migration-pending.json` into data/ listing them; each one removes itself once done, and
a later upgrade finishes whatever is still listed. Each step is idempotent, so one that
completed before its entry was cleared simply runs again as a no-op.

A leftover `data.migrating/` whose entries also exist at the root is refused, not merged:
neither copy is provably the newer one, and `shutil.move` onto an existing name nests a
directory or silently replaces a file.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from exoma.core.workspace.workspace_paths import (
    DATA_DIRNAME,
    WORKFLOW_DIRNAME,
    atomic_write_json,
    atomic_write_text,
    is_legacy_layout,
    read_json_file,
)

LAYOUT_VERSION = 2
BACKUPS_KEPT = 3
# Internal items a layout-1 workspace kept at the root.
_INTERNAL = (
    "sessions",
    "provider-sessions",
    "reports",
    "audit.jsonl",
    "usage.jsonl",
    "quality.jsonl",
    "redactions.jsonl",
    "evidence.jsonl",
    "facts.jsonl",
    "recurrence-cache.json",
    "capabilities.json",
    "graph-meta.json",
    "graph-stale.json",
    "e2e-knowledge.jsonl",
)
# Written before sessions existed and read by nothing since; kept only in the backup.
_OBSOLETE = ("state.json", "scope.json", "command-cache.json", "runtime", "logs")
_LOCK_FILES = ("facts.jsonl.lock", "evidence.jsonl.lock", "e2e-knowledge.jsonl.lock", "promote.lock")
# What a person edits, plus what the tooling regenerates in place.
_EDITABLE = ("config.json", "second_agent.json", "opencode.json", "e2e", ".gitignore", "current", DATA_DIRNAME)
_SCRIPT_STEMS = ("run", "check", "inspect")
# Rides inside data/ from the rename on; present only while post-rename steps remain.
PENDING_FILENAME = ".migration-pending.json"
_POST_STEPS = ("rewrite_evidence", "remove_obsolete", "strip_config", "convert_secrets", "prune_backups")


class MigrationConflict(ValueError):
    """A leftover data.migrating/ holds entries that also exist at the root. Nothing moved."""


class MigrationIncomplete(RuntimeError):
    """The layout moved to data/, but a post-rename step failed. A rerun resumes it."""

    def __init__(self, step: str, cause: BaseException, backup: str | None) -> None:
        super().__init__(f"step {step!r} failed: {type(cause).__name__}: {cause}")
        self.step = step
        self.backup = backup


class MigrationRollbackIncomplete(RuntimeError):
    """A pre-rename step failed, and putting the moved entries back failed too.

    Distinct from a clean rollback on purpose. Reporting "rolled back, nothing else changed"
    here would send the user to a .workflow/ root that is missing files which are in fact
    still sitting in data.migrating/ — the one place they would not think to look.
    """

    def __init__(self, cause: BaseException, failures: list[str], staging: Path) -> None:
        super().__init__(
            f"{type(cause).__name__}: {cause}; rollback left {len(failures)} item(s) behind: "
            + "; ".join(failures)
        )
        self.failures = failures
        self.staging = str(staging)


_STAMP_PATTERN = re.compile(r"\d{8}_\d{6}_\d+")


def _stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}"


def _backup(workflow_dir: Path, target: Path) -> None:
    """Copy the whole workspace except a data directory (which is where backups live)."""
    target.mkdir(parents=True, exist_ok=True)
    for entry in workflow_dir.iterdir():
        if entry.name in (DATA_DIRNAME, f"{DATA_DIRNAME}.migrating"):
            continue
        destination = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination)
        else:
            shutil.copy2(entry, destination)


def _prune_backups(backups: Path) -> None:
    try:
        stamps = sorted((p for p in backups.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return
    for old in stamps[:-BACKUPS_KEPT]:
        shutil.rmtree(old, ignore_errors=True)


@contextlib.contextmanager
def _store_locks(project_root: Path):
    """Every store writer's lock, taken at the paths the running layout uses."""
    from exoma.core.evidence.e2e.knowledge import _Lock as KnowledgeLock
    from exoma.core.evidence.evidence_store import _EvidenceLock
    from exoma.core.evidence.fact_store import _FactLock
    from exoma.core.knowledge.store import _KnowledgeLock

    with contextlib.ExitStack() as stack:
        for lock in (_FactLock, _EvidenceLock, KnowledgeLock, _KnowledgeLock):
            stack.enter_context(lock(project_root))
        yield


def _rewrite_evidence_paths(index: Path, old_root: Path, new_root: Path) -> int:
    """Point archived artifact paths at the data directory. Returns rows rewritten."""
    if not index.is_file():
        return 0
    # Compared normalised (separators, and case where the platform ignores it), so a row
    # written as C:/x or c:\x still matches; normcase and normpath keep the length, so the
    # remainder is sliced from the row's own spelling.
    old_prefix = os.path.normcase(os.path.normpath(str(old_root / "sessions")))
    new_prefix = str(new_root / "sessions")
    rewritten = 0
    lines = []
    for raw in index.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except ValueError:
            lines.append(raw)
            continue
        path = row.get("artifact_path") if isinstance(row, dict) else None
        if isinstance(path, str):
            spelled = os.path.normpath(path)
            folded = os.path.normcase(spelled)
            if folded == old_prefix or folded.startswith(old_prefix + os.sep):
                row["artifact_path"] = new_prefix + spelled[len(old_prefix):]
                rewritten += 1
        lines.append(json.dumps(row, ensure_ascii=False))
    if not rewritten:
        return 0  # nothing to change: no rewrite, so a resumed run cannot trip on a locked file
    atomic_write_text(index, "".join(line + "\n" for line in lines))
    return rewritten


def _convert_secrets(path: Path, backup_dir: Path) -> bool:
    """Rewrite a pre-list `secrets.json` (`profiles` as an object) in the list shape.

    The one time the workflow writes this file, and only to carry the user's own values
    across a format change it made; the original is copied to the backup first.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        return False
    (backup_dir / "e2e").mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_dir / "e2e" / "secrets.pre-list.json")
    data["profiles"] = [
        {"name": name, "credentials": values if isinstance(values, dict) else {}}
        for name, values in data["profiles"].items()
    ]
    atomic_write_json(path, data)
    return True


def migrate_to_data_layout(project_root: Path) -> dict:
    """Layout 1 → 2. Returns a report; raises only after putting everything back."""
    project_root = Path(project_root)
    workflow_dir = project_root / WORKFLOW_DIRNAME
    report: dict = {"from": 1, "to": 2, "moved": [], "removed": [], "stray": [], "config_stripped": [], "secrets_converted": False}
    staging = workflow_dir / f"{DATA_DIRNAME}.migrating"
    data = workflow_dir / DATA_DIRNAME
    if _is_redirect(staging):
        # This module only ever creates data.migrating/ as a real directory; a link there is
        # not ours, and recovering through it would move or delete another directory's files.
        raise MigrationConflict(
            f"{staging} is a link to another directory, not a staging directory this migration "
            "made. Nothing was read, moved or removed. Remove the link, then rerun upgrade."
        )
    redirected = _is_redirect(data)
    if staging.is_dir() and redirected:
        # A data/ that points elsewhere is the user's choice of where history lives. Nothing
        # here walks into it, empties it or moves backups through it.
        raise MigrationConflict(
            f"{staging} is left from an interrupted migration, and {data} is a link to another "
            "directory. Nothing was moved or removed. Reconcile the staging directory with the "
            "link's target by hand, delete it, then rerun upgrade."
        )
    if staging.is_dir() and data.is_dir():
        # Only empty directories — init's scaffolding (reports/, sessions/), created because
        # the stranded workspace looked new — holds no history, so it yields to the staged one.
        _remove_empty_tree(data)
    data_live = data.exists()
    # A crash after the layout markers moved but before the rename leaves no marker at the
    # root and no data/ — which reads as "not legacy". The staging directory is the only
    # trace of that history, so it is recovered here rather than skipped for good.
    stranded = staging.is_dir() and not data_live
    if staging.is_dir() and data_live:
        _settle_staging_beside_live_data(staging, workflow_dir / DATA_DIRNAME)
    if not is_legacy_layout(project_root) and not stranded:
        state = pending_migration(project_root)
        if state is None:
            report["skipped"] = "already on the data layout"
            return report
        report["resumed"] = True
        report["backup"] = str(workflow_dir / DATA_DIRNAME / "backups" / state["stamp"])
        return _finish(workflow_dir, workflow_dir / DATA_DIRNAME, state, report)

    if staging.exists():
        # A previous attempt died after staging and before its rename: put it back first —
        # unless the root has the same name again, where either copy could be the live one.
        leftovers = [e for e in staging.iterdir() if e.name not in ("backups", PENDING_FILENAME)]
        conflicts = sorted(e.name for e in leftovers if (workflow_dir / e.name).exists())
        if conflicts:
            raise MigrationConflict(
                f"{staging} is left from an interrupted migration and holds "
                f"{', '.join(conflicts)}, which also exist at the .workflow root. Nothing was "
                "moved. Keep one copy of each (compare them first), remove the other, then "
                "rerun upgrade."
            )
        for entry in leftovers:
            shutil.move(str(entry), str(workflow_dir / entry.name))
        with contextlib.suppress(FileNotFoundError):
            (staging / PENDING_FILENAME).unlink()

    stamp = _stamp()
    backup_dir = staging / "backups" / stamp
    report["backup"] = str(workflow_dir / DATA_DIRNAME / "backups" / stamp)

    moved: list[str] = []
    backed_up = False
    try:
        (staging / "backups").mkdir(parents=True, exist_ok=True)
        _backup(workflow_dir, backup_dir)
        backed_up = True
        with _store_locks(project_root):
            # Layout markers last: while any is still at the root, every reader keeps the
            # old layout, so nothing can start writing into a data/ that does not exist yet.
            from exoma.core.workspace.workspace_paths import _LEGACY_MARKERS

            order = [n for n in _INTERNAL if n not in _LEGACY_MARKERS] + [n for n in _INTERNAL if n in _LEGACY_MARKERS]
            for name in order:
                source = workflow_dir / name
                if source.exists():
                    shutil.move(str(source), str(staging / name))
                    moved.append(name)
            # Written into staging, so it arrives in data/ by the same rename: there is no
            # moment where data/ exists and the unfinished steps are not recorded.
            atomic_write_json(staging / PENDING_FILENAME, {"stamp": stamp, "pending": list(_POST_STEPS)})
            os.replace(staging, workflow_dir / DATA_DIRNAME)
    except Exception as exc:
        # Every rollback step is attempted and every failure kept. One unguarded move used
        # to raise out of this block, replacing the original error and abandoning the
        # remaining entries, while upgrade still reported a clean rollback.
        failures: list[str] = []
        for name in moved:
            if (staging / name).exists():
                try:
                    shutil.move(str(staging / name), str(workflow_dir / name))
                except Exception as move_exc:  # noqa: BLE001 — collected, reported below
                    failures.append(f"{name}: {type(move_exc).__name__}: {move_exc}")
        if staging.exists():
            # Backups are the one thing worth keeping from a failed attempt — every complete
            # one, including any an earlier interrupted attempt left; a half-copied one is not.
            backups = staging / "backups"
            try:
                kept_backups = sorted(backups.iterdir()) if backups.is_dir() else []
            except OSError as list_exc:
                kept_backups = []
                failures.append(f"backups: {type(list_exc).__name__}: {list_exc}")
            for kept in kept_backups:
                if kept == backup_dir and not backed_up:
                    continue
                try:
                    shutil.move(str(kept), str(workflow_dir / f"migration-backup-{kept.name}"))
                except Exception as move_exc:  # noqa: BLE001 — collected, reported below
                    failures.append(f"backup {kept.name}: {type(move_exc).__name__}: {move_exc}")
            if not failures:
                shutil.rmtree(staging, ignore_errors=True)
                if staging.exists():
                    # Everything moved back, but the empty staging tree would not go (a
                    # handle held open). Reported, so "rolled back" never hides a leftover.
                    failures.append(f"{staging.name}: could not be removed after rollback")
            # With failures, staging holds the only copy of what could not move back; it
            # stays for the user (or the next run's leftover recovery) to reconcile.
        if failures:
            raise MigrationRollbackIncomplete(exc, failures, staging) from exc
        raise
    report["moved"] = moved
    data = workflow_dir / DATA_DIRNAME
    return _finish(workflow_dir, data, {"stamp": stamp, "pending": list(_POST_STEPS)}, report)


def _is_redirect(path: Path) -> bool:
    """A symlink, or on Windows a junction — anything whose contents live somewhere else."""
    is_junction = getattr(path, "is_junction", None)  # Python 3.12+
    return path.is_symlink() or bool(is_junction and is_junction())


def _remove_empty_tree(root: Path) -> bool:
    """Remove `root` if it holds directories only. Never deletes a file.

    Bottom-up `os.rmdir`, not scan-then-`rmtree`: rmdir refuses a non-empty directory
    itself, so a file a hook or writer creates mid-walk makes the removal stop there
    instead of being deleted with the rest. Whatever is left simply counts as live data.
    """
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        if filenames:
            return False
        try:
            os.rmdir(dirpath)
        except OSError:
            return False
    return True


def _settle_staging_beside_live_data(staging: Path, data: Path) -> None:
    """A data.migrating/ left while data/ is already live: keep its backups, refuse the rest.

    data/ appearing beside a stranded staging directory means something wrote after the
    crash, so data/ may hold new history while staging holds the old. Neither may be
    dropped and nothing can merge them safely; only the backups, which never overlap by
    stamp, are carried over.
    """
    history = sorted(e.name for e in staging.iterdir() if e.name not in ("backups", PENDING_FILENAME))
    if history:
        raise MigrationConflict(
            f"{staging} is left from an interrupted migration and still holds {', '.join(history)}, "
            f"but {data} is already live and may hold newer records. Nothing was moved. Compare "
            "the two, move what data/ is missing into it, delete the staging directory, then "
            "rerun upgrade."
        )
    backups = staging / "backups"
    try:
        if backups.is_dir():
            (data / "backups").mkdir(parents=True, exist_ok=True)
            for kept in sorted(backups.iterdir()):
                target = data / "backups" / kept.name
                suffix = 0
                while target.exists():
                    suffix += 1
                    target = data / "backups" / f"{kept.name}-staged{suffix}"
                shutil.move(str(kept), str(target))
        shutil.rmtree(staging)
    except Exception as exc:  # shutil.move raises its own Error too; none of them is a rollback
        # Each backup moved so far is whole; the rest are still in staging, and the next
        # upgrade carries them over the same way. Not a rollback, so not reported as one.
        raise MigrationIncomplete("settle_staging_backups", exc, str(data / "backups")) from exc


def pending_migration(project_root: Path) -> dict | None:
    """The post-rename steps a data-layout workspace has still to run, or None.

    An unreadable marker still means unfinished: every step is idempotent, so running all
    of them again is safe, while ignoring the marker would leave the work undone for good.
    """
    marker = Path(project_root) / WORKFLOW_DIRNAME / DATA_DIRNAME / PENDING_FILENAME
    if not marker.is_file():
        return None
    try:
        state = read_json_file(marker)
    except (OSError, ValueError):
        state = None
    state = state if isinstance(state, dict) else {}
    listed = state.get("pending")
    # Only a list of known steps is trusted. Anything else — a non-list, or a name this
    # build does not know — means the record cannot say what is done, so all of them run
    # again; filtering the unknown out would read as "nothing left" and finalize unrun work.
    if isinstance(listed, list) and all(s in _POST_STEPS for s in listed):
        pending = [s for s in _POST_STEPS if s in listed]
    else:
        pending = list(_POST_STEPS)
    stamp = state.get("stamp")
    # The stamp becomes a directory under data/backups/, so it has to be one this module
    # could have produced — never a path that climbs out of it.
    if not (isinstance(stamp, str) and _STAMP_PATTERN.fullmatch(stamp)):
        stamp = _stamp()
    return {"stamp": stamp, "pending": pending}


def _remove_obsolete(workflow_dir: Path) -> list[str]:
    removed = []
    for name in (*_OBSOLETE, *_LOCK_FILES):
        target = workflow_dir / name
        if not target.exists():
            continue
        try:
            shutil.rmtree(target) if target.is_dir() else target.unlink()
            removed.append(name)
        except OSError:
            pass  # reported as stray below and by doctor; not worth stopping the migration
    return removed


def _strip_config(workflow_dir: Path) -> list:
    config_path = workflow_dir / "config.json"
    if not config_path.is_file():
        return []
    from exoma.core.runtime.config_defaults import strip_to_overrides

    try:
        config = read_json_file(config_path)
    except (OSError, ValueError):
        return []  # an unreadable config is doctor's to report, not the migration's to rewrite
    if not isinstance(config, dict):
        return []
    stripped = strip_to_overrides(config)
    runtime = config.get("runtime") if isinstance(config.get("runtime"), dict) else {}
    runtime["layout_version"] = LAYOUT_VERSION
    runtime["sessions_dir"] = f"{WORKFLOW_DIRNAME}/{DATA_DIRNAME}/sessions"
    config["runtime"] = runtime
    atomic_write_json(config_path, config)
    return stripped


def _finish(workflow_dir: Path, data: Path, state: dict, report: dict) -> dict:
    """Run the post-rename steps still listed, clearing each from the marker as it lands."""
    marker = data / PENDING_FILENAME
    stamp = state["stamp"]
    for step in [s for s in _POST_STEPS if s in state["pending"]]:
        try:
            if step == "rewrite_evidence":
                report["evidence_paths_rewritten"] = _rewrite_evidence_paths(data / "evidence.jsonl", workflow_dir, data)
            elif step == "remove_obsolete":
                report["removed"] = _remove_obsolete(workflow_dir)
            elif step == "strip_config":
                report["config_stripped"] = _strip_config(workflow_dir)
            elif step == "convert_secrets":
                secrets = workflow_dir / "e2e" / "secrets.json"
                if secrets.is_file():
                    report["secrets_converted"] = _convert_secrets(secrets, data / "backups" / stamp)
            elif step == "prune_backups":
                _prune_backups(data / "backups")
            state["pending"].remove(step)
            atomic_write_json(marker, state)
        except Exception as exc:
            raise MigrationIncomplete(step, exc, report.get("backup")) from exc
    try:
        marker.unlink(missing_ok=True)
    except OSError as exc:
        # Every step landed; a marker that stays behind costs one no-op resume.
        raise MigrationIncomplete("finalize", exc, report.get("backup")) from exc

    for entry in workflow_dir.iterdir():
        stem, _, suffix = entry.name.partition(".")
        if entry.name in _EDITABLE or (stem in _SCRIPT_STEMS and suffix in ("ps1", "sh")):
            continue
        report["stray"].append(entry.name)
    return report


_STEPS = ((2, migrate_to_data_layout),)


def run(project_root: Path) -> list[dict]:
    """Apply every step the workspace still needs, in order. Returns their reports."""
    reports = []
    for version, step in _STEPS:
        result = step(project_root)
        if not result.get("skipped"):
            reports.append(result)
    return reports
