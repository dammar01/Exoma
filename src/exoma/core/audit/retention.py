"""Read-only logical storage inventory and conservative session retention preview."""
import json
import os
import stat
import time
from pathlib import Path

from exoma.core.evidence.contracts import TASK_STREAM_NAME
from exoma.core.workspace.workspace_paths import workflow_paths


def _category(name: str) -> str:
    if name in {"sessions", "provider-sessions", "reports", "exports", "backups"}:
        return name
    if name in {"audit.jsonl", "usage.jsonl", "quality.jsonl", "redactions.jsonl", TASK_STREAM_NAME}:
        return "telemetry"
    if name in {"evidence.jsonl", "facts.jsonl", "e2e-knowledge.jsonl"}:
        return "evidence"
    return "other"


def _totals():
    return {"files": 0, "logical_bytes": 0, "oldest_mtime": None, "newest_mtime": None}


def _redirect(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _observe_references(paths, sessions):
    """Add positive reasons only. An absent or unreadable index never releases a session."""
    observations = {}
    sources = (("current", paths["current_dir"] / "session.json"),
               ("evidence", paths["evidence_store"]))
    for kind, path in sources:
        try:
            parent = path.parent.lstat()
            if _redirect(parent) or _redirect(path.lstat()) or not path.is_file():
                observations[kind] = "unavailable"
                continue
            with path.open("rb") as stream:
                raw = stream.read(256 * 1024 + 1)
            clipped = len(raw) > 256 * 1024
            if clipped and kind == "current":
                observations[kind] = "partial"
                continue
            if clipped:
                raw = raw[:256 * 1024].rpartition(b"\n")[0]
            observations[kind] = "partial" if clipped else "complete"
            if kind == "current":
                row = json.loads(raw)
                if not isinstance(row, dict):
                    observations[kind] = "unavailable"
                else:
                    sid = row.get("session_id")
                    if isinstance(sid, str) and sid in sessions:
                        sessions[sid]["protected_by"].add("current_session")
                continue
            for line in raw.splitlines():
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    observations[kind] = "partial"
                    continue
                if not isinstance(row, dict):
                    observations[kind] = "partial"
                    continue
                sid = row.get("session")
                if isinstance(sid, str) and sid in sessions:
                    sessions[sid]["protected_by"].add("indexed_evidence")
        except FileNotFoundError:
            observations[kind] = "absent"
        except (OSError, ValueError, RecursionError):
            observations[kind] = "unavailable"
    return observations


def retention_report(project_root: Path, *, preview=False, max_entries=10000, budget_seconds=0.5) -> dict:
    try:
        paths = workflow_paths(project_root)
    except OSError as exc:
        result = {"status": "unavailable", "layout": "unknown", "categories": {}, "entries_seen": 0,
                  "skipped": 1, "totals_are_lower_bounds": True, "reason": str(exc)}
        if preview:
            result.update(sessions=[], targets=[], target_count=0, target_bytes=0)
        return result
    data = paths["data_dir"]
    legacy = data == paths["workflow_dir"]
    report = {"status": "complete", "layout": "legacy" if legacy else "data", "categories": {},
              "entries_seen": 0, "skipped": 0, "totals_are_lower_bounds": False}
    sessions = {}
    if preview:
        report.update({"policy": {"review_age_days": 7, "keep_newest": 20,
                                  "retirement_proof": "unavailable"},
                       "sessions": [], "targets": [], "target_count": 0, "target_bytes": 0})
    try:
        for parent in (paths["workflow_dir"], data):
            info = parent.lstat()
            if _redirect(info) or not stat.S_ISDIR(info.st_mode) or os.path.ismount(parent):
                raise OSError("redirected storage")
    except FileNotFoundError:
        report["status"] = "absent"
        return report
    except OSError:
        return {**report, "status": "unavailable", "totals_are_lower_bounds": True}

    if not legacy and os.path.lexists(paths["workflow_dir"] / TASK_STREAM_NAME):
        report["legacy_task_stream"] = "A separate tasks.jsonl remains at the workspace root; compare it with data/tasks.jsonl before moving it."
        report["totals_are_lower_bounds"] = True

    legacy_names = {p.name for p in paths.values() if p.parent == data
                    and p not in (paths["config"], paths["provider_config"], paths["gitignore"], paths["current_dir"])}
    legacy_names.update({"exports", TASK_STREAM_NAME})
    deadline = time.monotonic() + budget_seconds

    def walk(directory, depth, category=None, group=None):
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if report["entries_seen"] >= max_entries or time.monotonic() >= deadline:
                        report["totals_are_lower_bounds"] = True
                        return
                    report["entries_seen"] += 1
                    if depth == 0 and legacy and entry.name not in legacy_names:
                        continue
                    kind = category or _category(entry.name)
                    counts = report["categories"].setdefault(kind, _totals())
                    try:
                        info = entry.stat(follow_symlinks=False)
                        is_dir = stat.S_ISDIR(info.st_mode)
                        if _redirect(info) or (is_dir and os.path.ismount(entry.path)):
                            report["skipped"] += 1
                            continue
                        member = group
                        if preview and depth == 1 and kind in ("sessions", "provider-sessions"):
                            sid = None
                            if kind == "provider-sessions" and entry.name.endswith(".json") and stat.S_ISREG(info.st_mode):
                                sid = entry.name[:-5]
                            elif kind == "sessions" and is_dir:
                                sid = entry.name
                            if sid:
                                member = sessions.setdefault(sid, {"id": sid, "paths": [], "logical_bytes": 0,
                                                                   "protected_by": set(), "newest_mtime": info.st_mtime})
                                member["paths"].append(f"{kind}/{entry.name}")
                        if member is not None and kind == "sessions":
                            relative = Path(entry.path).relative_to(paths["sessions_dir"] / member["id"]).parts
                            if relative in (("logs",), ("e2e",), ("artifacts",)):
                                member["protected_by"].add("session_artifacts")
                            if relative == ("runtime", "lock"):
                                member["protected_by"].add("runtime_lock_present")
                        if member is not None:
                            member["newest_mtime"] = max(member["newest_mtime"], info.st_mtime)
                        if is_dir:
                            if depth < 8:
                                walk(entry.path, depth + 1, kind, member)
                            else:
                                report["skipped"] += 1
                        elif stat.S_ISREG(info.st_mode):
                            counts["files"] += 1
                            counts["logical_bytes"] += info.st_size
                            for field, compare in (("oldest_mtime", min), ("newest_mtime", max)):
                                previous = counts[field]
                                counts[field] = info.st_mtime if previous is None else compare(previous, info.st_mtime)
                            if member is not None:
                                member["logical_bytes"] += info.st_size
                        else:
                            report["skipped"] += 1
                    except OSError:
                        report["skipped"] += 1
        except OSError:
            report["skipped"] += 1

    walk(data, 0)
    if report["skipped"] or report["totals_are_lower_bounds"]:
        report.update(status="partial", totals_are_lower_bounds=True)
    if preview:
        report["reference_observations"] = _observe_references(paths, sessions)
        cutoff = time.time() - 7 * 86400
        for rank, session in enumerate(sorted(sessions.values(), key=lambda s: (-s["newest_mtime"], s["id"]))):
            session["protected_by"] = sorted(session["protected_by"])
            if session["protected_by"]:
                reason = "protected"
            elif report["status"] == "partial":
                reason = "incomplete_inventory"
            elif rank < 20:
                reason = "keep_newest"
            elif session["newest_mtime"] >= cutoff:
                reason = "recent_activity"
            else:
                reason = "retirement_unproven"
            report["sessions"].append({**session, "paths": sorted(session["paths"]), "retained_reason": reason})
    return report
