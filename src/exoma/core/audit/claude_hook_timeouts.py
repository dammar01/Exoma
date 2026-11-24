"""Bounded observations from Claude's project-bound transcript sample."""
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from exoma.utils import osutil


_MAX_REGISTRY = 256 * 1024
_MAX_FILES = 8
_TAIL_BYTES = 256 * 1024
_MAX_LINES = 8192
_CONFIRMED_VERSIONS = ("2.1.330",)


def _regular_under(path: Path, root: Path) -> bool:
    """Restrict observations to plain files under an unredirected host directory."""
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(root):
        return False
    node = path
    while True:
        info = node.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return False
        if node == path and not stat.S_ISREG(info.st_mode):
            return False
        if node == root:
            return True
        node = node.parent


def _timestamp(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def claude_hook_timeouts(project_root: Path) -> dict:
    home = osutil.agent_home() / ".claude"
    project = project_root.resolve()
    cwd_states = {}
    repository = (project / ".git").exists()

    def project_cwd(raw):
        """Classify launch directories before opening a transcript. None means unresolved."""
        if not isinstance(raw, str) or not raw or len(raw) > 4096:
            return None
        if raw in cwd_states:
            return cwd_states[raw]
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts:
            return None
        if not path.is_relative_to(project):
            return False
        if len(cwd_states) >= 512 or len(path.relative_to(project).parts) > 64:
            return None
        try:
            if path != project and not repository:
                return False
            state = True
            node = project
            for part in path.relative_to(project).parts:
                node = node / part
                info = node.lstat()
                if (not stat.S_ISDIR(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    state = None
                    break
                try:
                    (node / ".git").lstat()
                except FileNotFoundError:
                    continue
                state = False
                break
        except (OSError, ValueError):
            state = None
        cwd_states[raw] = state
        return state
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    report = {"status": "unavailable", "window_days": 7, "files_scanned": 0, "count": None,
              "latest": None, "invalid_rows": 0, "unavailable_files": 0, "truncated": False}
    registry = home / "session_registry.json"
    try:
        if not _regular_under(registry, home):
            return report
        with registry.open("rb") as stream:
            data = stream.read(_MAX_REGISTRY + 1)
        if len(data) > _MAX_REGISTRY:
            return {**report, "truncated": True}
        bindings = json.loads(data)
    except (OSError, ValueError, RecursionError):
        return report
    if not isinstance(bindings, dict) or len(bindings) > 256:
        return report

    matches = []
    for sid, binding in bindings.items():
        if not isinstance(binding, dict) or not isinstance(binding.get("cwd"), str):
            report["unavailable_files"] += 1
            continue
        scope = project_cwd(binding["cwd"])
        if scope is None:
            report["unavailable_files"] += 1
        elif scope:
            matches.append((str(binding.get("bound_at") or ""), sid, binding.get("transcript_path")))
    if not matches:
        return report
    matches.sort(key=lambda item: item[:2], reverse=True)
    report["truncated"] = len(matches) > _MAX_FILES
    events = set()
    latest = None
    recognized = False
    unknown_schema = False
    for _, sid, raw_path in matches[:_MAX_FILES]:
        try:
            path = Path(raw_path) if isinstance(raw_path, str) else None
            if (path is None or path.name != sid + ".jsonl" or path.parent.parent != home / "projects"
                    or not _regular_under(path, home)):
                report["unavailable_files"] += 1
                continue
            with path.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - _TAIL_BYTES))
                raw = stream.read(_TAIL_BYTES)
            if size > _TAIL_BYTES:
                report["truncated"] = True
                raw = raw.partition(b"\n")[2]
        except (ValueError, OSError):
            report["unavailable_files"] += 1
            continue
        report["files_scanned"] += 1
        if raw and not raw.endswith(b"\n"):
            report["truncated"] = True
            raw = raw.rpartition(b"\n")[0]
        lines = raw.splitlines()
        if len(lines) > _MAX_LINES:
            report["truncated"] = True
        file_recognized = False
        for line in lines[-_MAX_LINES:]:
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                report["invalid_rows"] += 1
                continue
            if not isinstance(row, dict):
                report["invalid_rows"] += 1
                continue
            if row.get("sessionId") != sid:
                continue
            scope = project_cwd(row.get("cwd"))
            if scope is not True:
                unknown_schema |= scope is None
                continue
            if row.get("version") not in _CONFIRMED_VERSIONS:
                unknown_schema = True
                continue
            kind = row.get("type")
            attachment = row.get("attachment")
            if kind != "attachment":
                if kind in ("user", "assistant", "system", "progress", "summary", "result"):
                    file_recognized = recognized = True
                else:
                    unknown_schema = True
                continue
            attachment_type = attachment.get("type") if isinstance(attachment, dict) else None
            if not isinstance(attachment_type, str) or not attachment_type.strip():
                report["invalid_rows"] += 1
                continue
            if attachment_type != "hook_cancelled":
                file_recognized = recognized = True
                continue
            timed_out = attachment.get("timedOut")
            if not isinstance(timed_out, bool):
                report["invalid_rows"] += 1
                continue
            if not timed_out:
                file_recognized = recognized = True
                continue
            at = _timestamp(row.get("timestamp"))
            uid = row.get("uuid")
            if at is None or at > now or not isinstance(uid, str) or not uid:
                report["invalid_rows"] += 1
                continue
            file_recognized = recognized = True
            if at >= cutoff:
                events.add((sid, uid))
                latest = max(latest, at) if latest is not None else at
        unknown_schema |= not file_recognized
    if recognized:
        report["count"] = len(events)
        report["latest"] = latest.isoformat() if latest else None
        incomplete = unknown_schema or report["invalid_rows"] or report["unavailable_files"] or report["truncated"]
        report["status"] = "partial" if incomplete else "complete"
    elif report["files_scanned"]:
        report["status"] = "unknown"
    return report
