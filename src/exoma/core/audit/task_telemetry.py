"""Tasks as sequences of skill calls, closed by a verify the runtime derives as pass
(DEC-015, revised by DEC-020).

Events come from two writers into `tasks.jsonl`: the runtime records each delegated
command it runs, and the `task-events` hook records what never passes through it — a
local skill invoked from the prompt, a file edited, a commit made. Since event v2 the hook
reads a commit as a HEAD that moved between turns (UserPromptSubmit/Stop) rather than from
every Bash call, so a commit is recorded at the end of the turn that made it, after that
turn's verify. Nothing is derived at write time; the hook stays a few lines of append, and a
better reading of the same history needs no migration.

A task is the run of events in one MAIN_SESSION_ID from its first event until a verify
whose derived verdict is `pass` after the task edited something. A commit is the
recommended next step, not the closing one: a commit that includes a file of a task just
completed joins that task's sequence, and a commit on an open task leaves it open. Its state:

- `completed`: closed by a derived `pass` with no edit since.
- `open`: edits not yet verified `pass`, committed or not.
- `read_only`: skills ran, nothing was edited; nothing for a verify to close.
- `unknown`: events without a session identity, which cannot be put in a task.

The derived verdict is read, not the declared one (DEC-020): a DONE the runtime turns into
`incomplete` — a gap under not_verified included — does not close a task. Events written
before DEC-020 carry no derived verdict, and their tasks stay open.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from exoma.core.evidence.contracts import TASK_EVENT_VERSION, TASK_STREAM_NAME
from exoma.core.evidence.runtime_io import write_task_event
from exoma.core.workspace.workspace_paths import data_dir, now_iso

RECENT = 10
TASK_HOOK_DIAGNOSTICS_REL = Path("task-hook") / "diagnostics.jsonl"
DIAGNOSTICS_WINDOW_DAYS = 7
_DIAGNOSTIC_STAGES = frozenset({"git_rev_parse", "git_log", "append", "snapshot"})
_DIAGNOSTIC_REASONS = frozenset({
    "git_timeout", "git_deadline_exceeded", "git_spawn_error",
    "git_nonzero", "io_error", "lock_contention", "invalid_snapshot",
})


def _parse_iso_aware(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def record_skill(project_root, session_id: str | None, skill: str, *, verdict: str | None = None,
                 derived: str | None = None, next_action: str | None = None,
                 prompt_id: str | None = None) -> None:
    """The runtime's event for one delegated command. Never raises.

    `verdict` is what the verify reply declared; `derived` is the runtime's verdict on it
    (pass | incomplete | fail), the one that closes a task."""
    try:
        write_task_event(Path(project_root), {
            "v": TASK_EVENT_VERSION,
            "at": now_iso(),
            "source": "runtime",
            "session_id": session_id,
            "kind": "skill",
            "skill": skill,
            "verdict": verdict,
            "derived": derived,
            "next_action": next_action,
            "prompt_id": prompt_id,
        })
    except Exception:
        pass


def _valid_event(event) -> bool:
    if not isinstance(event, dict) or not isinstance(event.get("kind"), str) or event["kind"] not in ("skill", "edit", "commit"):
        return False
    for key in ("session_id", "at", "committed_at", "skill", "path", "commit", "verdict", "derived", "prompt_id"):
        if event.get(key) is not None and not isinstance(event[key], str):
            return False
    if event.get("delayed") is not None and not isinstance(event["delayed"], bool):
        return False
    paths = event.get("paths")
    return paths is None or isinstance(paths, str) or isinstance(paths, list) and all(isinstance(p, str) for p in paths)


def load_events(project_root, *, diagnostics: dict | None = None) -> list[dict]:
    """Every event on disk, oldest first; a torn or foreign line is skipped."""
    diagnostics = {} if diagnostics is None else diagnostics
    diagnostics.update(status="complete", invalid_rows=0)
    try:
        text = (data_dir(Path(project_root)) / TASK_STREAM_NAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        diagnostics["status"] = "unavailable"
        return []
    events = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            diagnostics["invalid_rows"] += 1
            diagnostics["status"] = "partial"
            continue
        if _valid_event(event):
            events.append(event)
        else:
            diagnostics["invalid_rows"] += 1
            diagnostics["status"] = "partial"
    return events


def _new_task(session_id, at) -> dict:
    return {"session_id": session_id, "started_at": at, "ended_at": at, "sequence": [],
            "edited": set(), "verdict": None, "commit": None, "state": None}


def derive_tasks(events: list[dict]) -> tuple[list[dict], int]:
    """Tasks in the order they started, and the number of commits no task claimed."""
    tasks: list[dict] = []
    current: dict[str | None, dict] = {}
    # Completed tasks per session in completion order: normal commits consider only the most
    # recent closed task, while delayed retry commits can match earlier completed tasks whose
    # start precedes `committed_at`.
    closed: dict[str | None, list[dict]] = {}
    # A commit is recorded once per session whose turn saw HEAD move, so several copies of it
    # can arrive. Only the copy a task claims marks it taken: a copy from another session that
    # arrives first must not hide it from the session that made it. A commit is unclaimed when
    # no copy of it was ever claimed, counted once.
    claimed_commits: set[str] = set()
    unclaimed_commits: set[str] = set()
    for event in events:
        if not _valid_event(event):
            continue
        session = event.get("session_id") or None
        at = event.get("at")
        kind = event["kind"]
        if kind == "commit":
            commit = str(event.get("commit") or "")
            if not commit or commit in claimed_commits:
                continue
            listed = event.get("paths") or []
            paths = {str(p) for p in ([listed] if isinstance(listed, str) else listed)}
            owner = None
            is_delayed = event.get("delayed") is True
            if session is not None:
                cur = current.get(session)
                prev_list = closed.get(session) or []
                committed_ts = _parse_iso_aware(event.get("committed_at")) if is_delayed else None
                candidates = []
                if is_delayed and committed_ts is not None:
                    if cur is not None:
                        cur_started = _parse_iso_aware(cur.get("started_at"))
                        if cur_started is None or committed_ts >= cur_started.replace(microsecond=0):
                            candidates.append(cur)
                    for prev in reversed(prev_list):
                        prev_started = _parse_iso_aware(prev.get("started_at"))
                        if prev_started is None or committed_ts >= prev_started.replace(microsecond=0):
                            candidates.append(prev)
                else:
                    if cur is not None:
                        candidates.append(cur)
                    if prev_list:
                        candidates.append(prev_list[-1])
                for candidate in candidates:
                    if candidate["edited"] & paths:
                        owner = candidate
                        break
            if owner is None:
                unclaimed_commits.add(commit)
                continue
            claimed_commits.add(commit)
            owner["sequence"].append("commit")
            owner["commit"] = commit
            commit_at = (event.get("committed_at") if is_delayed else None) or at
            owner_end_ts = _parse_iso_aware(owner.get("ended_at"))
            commit_end_ts = _parse_iso_aware(commit_at)
            if not is_delayed or owner_end_ts is None or commit_end_ts is None or commit_end_ts >= owner_end_ts:
                owner["ended_at"] = commit_at
            continue
        task = current.get(session)
        if task is None:
            task = current[session] = _new_task(session, at)
            tasks.append(task)
        task["ended_at"] = at
        if kind == "edit":
            task["edited"].add(str(event.get("path") or ""))
            # A pass vouches for the tree it saw; an edit after it is unverified work.
            task["verdict"] = None
            if not task["sequence"] or task["sequence"][-1] != "edit":
                task["sequence"].append("edit")
            continue
        skill = str(event.get("skill") or "unknown")
        task["sequence"].append(skill)
        if skill == "verify":
            verdict = str(event.get("verdict") or "").strip().upper().replace("_", " ")
            task["verdict"] = verdict or None
            if str(event.get("derived") or "").strip().lower() == "pass" and task["edited"] and session is not None:
                task["state"] = "completed"
                closed.setdefault(session, []).append(task)
                del current[session]
    for task in current.values():
        if task["session_id"] is None:
            task["state"] = "unknown"
        elif not task["edited"]:
            task["state"] = "read_only"
        else:
            task["state"] = "open"
    return tasks, len(unclaimed_commits - claimed_commits)


def load_hook_diagnostics(project_root, *, window_days: int = DIAGNOSTICS_WINDOW_DAYS) -> dict:
    """Bounded privacy-safe summary of recent task-events hook failures."""
    path = data_dir(Path(project_root)) / TASK_HOOK_DIAGNOSTICS_REL
    base = {
        "status": "none",
        "total": 0,
        "window_days": window_days,
        "by_stage": {},
        "by_reason": {},
        "last_at": None,
        "invalid_rows": 0,
    }
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return base
    except OSError:
        return {**base, "status": "unavailable"}
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    by_stage: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    total = 0
    invalid = 0
    last_at: str | None = None
    last_ts: datetime | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            invalid += 1
            continue
        if not isinstance(row, dict):
            invalid += 1
            continue
        stage = row.get("stage")
        reason = row.get("reason")
        if (
            not isinstance(stage, str)
            or not isinstance(reason, str)
            or stage not in _DIAGNOSTIC_STAGES
            or reason not in _DIAGNOSTIC_REASONS
        ):
            invalid += 1
            continue
        at = row.get("at")
        ts = _parse_iso_aware(at)
        if ts is None:
            invalid += 1
            continue
        if ts < cutoff:
            continue
        if last_ts is None or ts > last_ts:
            last_ts = ts
            last_at = at
        stage = row["stage"]
        reason = row["reason"]
        by_stage[stage] = by_stage.get(stage, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        total += 1
    return {
        "status": "failures_recorded" if total else ("partial" if invalid else "none"),
        "total": total,
        "window_days": window_days,
        "by_stage": dict(sorted(by_stage.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "last_at": last_at,
        "invalid_rows": invalid,
    }


def report(project_root) -> dict:
    diagnostics: dict = {}
    tasks, unclaimed = derive_tasks(load_events(project_root, diagnostics=diagnostics))
    by_state: dict[str, int] = {}
    skill_counts: dict[str, int] = {}
    for task in tasks:
        by_state[task["state"]] = by_state.get(task["state"], 0) + 1
        for step in task["sequence"]:
            skill_counts[step] = skill_counts.get(step, 0) + 1
    return {
        "tasks": len(tasks),
        "event_stream": diagnostics,
        "hook_diagnostics": load_hook_diagnostics(project_root),
        "by_state": dict(sorted(by_state.items())),
        "skill_counts": dict(sorted(skill_counts.items())),
        "unclaimed_commits": unclaimed,
        "recent": [
            {
                "session_id": task["session_id"],
                "started_at": task["started_at"],
                "ended_at": task["ended_at"],
                "state": task["state"],
                "sequence": task["sequence"],
                "files_edited": len(task["edited"]),
                "commit": task["commit"],
            }
            for task in tasks[-RECENT:]
        ],
    }
