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

import json
from pathlib import Path

from exoma.core.evidence.contracts import TASK_EVENT_VERSION, TASK_STREAM_NAME
from exoma.core.evidence.runtime_io import write_task_event
from exoma.core.workspace.workspace_paths import data_dir, now_iso

RECENT = 10


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


def load_events(project_root) -> list[dict]:
    """Every event on disk, oldest first; a torn or foreign line is skipped."""
    try:
        text = (data_dir(Path(project_root)) / TASK_STREAM_NAME).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("kind") in ("skill", "edit", "commit"):
            events.append(event)
    return events


def _new_task(session_id, at) -> dict:
    return {"session_id": session_id, "started_at": at, "ended_at": at, "sequence": [],
            "edited": set(), "verdict": None, "commit": None, "state": None}


def derive_tasks(events: list[dict]) -> tuple[list[dict], int]:
    """Tasks in the order they started, and the number of commits no task claimed."""
    tasks: list[dict] = []
    current: dict[str | None, dict] = {}
    # The task each session last completed: the commit recommended after its pass joins it.
    closed: dict[str | None, dict] = {}
    # A commit is recorded once per session whose turn saw HEAD move, so several copies of it
    # can arrive. Only the copy a task claims marks it taken: a copy from another session that
    # arrives first must not hide it from the session that made it. A commit is unclaimed when
    # no copy of it was ever claimed, counted once.
    claimed_commits: set[str] = set()
    unclaimed_commits: set[str] = set()
    for event in events:
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
            if session is not None:
                for candidate in (current.get(session), closed.get(session)):
                    if candidate is not None and candidate["edited"] & paths:
                        owner = candidate
                        break
            if owner is None:
                unclaimed_commits.add(commit)
                continue
            claimed_commits.add(commit)
            owner["sequence"].append("commit")
            owner["commit"] = commit
            owner["ended_at"] = at
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
                closed[session] = task
                del current[session]
    for task in current.values():
        if task["session_id"] is None:
            task["state"] = "unknown"
        elif not task["edited"]:
            task["state"] = "read_only"
        else:
            task["state"] = "open"
    return tasks, len(unclaimed_commits - claimed_commits)


def report(project_root) -> dict:
    tasks, unclaimed = derive_tasks(load_events(project_root))
    by_state: dict[str, int] = {}
    skill_counts: dict[str, int] = {}
    for task in tasks:
        by_state[task["state"]] = by_state.get(task["state"], 0) + 1
        for step in task["sequence"]:
            skill_counts[step] = skill_counts.get(step, 0) + 1
    return {
        "tasks": len(tasks),
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
