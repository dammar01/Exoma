"""`.workflow/current/`: what the project is doing right now, in one place to watch.

Sessions live under `.workflow/data/sessions/<id>/`, one directory per main agent, and a
project that has seen a few weeks of work has dozens. Finding the one that is running —
and, during a browser run, which step it is on — meant opening them one by one. This is
a mirror the runtime writes as it goes, so there is exactly one place to look:

  current/session.json    session id, command, status, phase, timestamps, artifact dir,
                          and `other_active` — sessions that also hold a runtime lock
  current/progress.jsonl  one line per progress beat of the latest dispatch (reset on each)
  current/e2e/events.jsonl        a browser run's player events, per step, as they arrive
  current/e2e/last.png            the newest step screenshot the run kept
  current/e2e/report.json, verification.md, evidence.md   copied when the run ends

A mirror, never a source: nothing reads it back, sessions/ keeps the real record, and it
sits outside sessions/ so pruning and recurrence scans never see it. The LATEST dispatch
owns it; a second session dispatching on the same project takes it over, and a slower
first session stops writing into it (its events would otherwise interleave).

Everything here is best-effort. Observing a call must never be able to fail it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from exoma.core.workspace.workspace_paths import (
    atomic_write_bytes,
    atomic_write_text,
    now_iso,
    workflow_paths,
)

TASK_PREVIEW_CHARS = 200
MAX_PROGRESS_LINES = 2000


def _dir(project_root: Path) -> Path:
    return workflow_paths(Path(project_root))["current_dir"]


def _read_session(project_root: Path) -> dict:
    try:
        data = json.loads((_dir(project_root) / "session.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def owns(project_root: Path, session_id: str) -> bool:
    return _read_session(project_root).get("session_id") == session_id


def _other_active(project_root: Path, session_id: str) -> list[str]:
    sessions = workflow_paths(Path(project_root))["sessions_dir"]
    try:
        return sorted(
            p.name for p in sessions.iterdir()
            if p.is_dir() and p.name != session_id and (p / "runtime" / "lock").exists()
        )
    except OSError:
        return []


def _write_session(project_root: Path, payload: dict) -> None:
    atomic_write_text(_dir(project_root) / "session.json", json.dumps(payload, indent=2, ensure_ascii=False))


def start(project_root: Path, session_id: str, command: str, task: str) -> None:
    """A dispatch began: take the mirror over and reset its progress."""
    try:
        from exoma.utils.redact import redact

        preview, _ = redact(str(task or "")[:TASK_PREVIEW_CHARS])
        directory = _dir(project_root)
        directory.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(directory / "e2e", ignore_errors=True)
        (directory / "progress.jsonl").write_text("", encoding="utf-8")
        _write_session(
            project_root,
            {
                "session_id": session_id,
                "command": command,
                "task_preview": preview,
                "status": "running",
                "phase": "started",
                "started_at": now_iso(),
                "updated_at": now_iso(),
                "artifacts_dir": str(workflow_paths(Path(project_root), session_id)["session_dir"]),
                "other_active": _other_active(project_root, session_id),
            },
        )
    except Exception:
        pass


def progress(project_root: Path, session_id: str, beat: dict) -> None:
    """One progress beat: appended to progress.jsonl and reflected as the session's phase."""
    try:
        if not owns(project_root, session_id):
            return
        directory = _dir(project_root)
        path = directory / "progress.jsonl"
        try:
            lines = path.stat().st_size and sum(1 for _ in path.open("r", encoding="utf-8"))
        except OSError:
            lines = 0
        if lines and lines >= MAX_PROGRESS_LINES:
            return
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": now_iso(), **beat}, ensure_ascii=False, default=str) + "\n")
        session = _read_session(project_root)
        session.update({"phase": str(beat.get("phase") or session.get("phase")), "updated_at": now_iso()})
        _write_session(project_root, session)
    except Exception:
        pass


def finish(project_root: Path, session_id: str, result: dict) -> None:
    """The dispatch ended: status, error type or verdict, where its artifacts are."""
    try:
        if not owns(project_root, session_id):
            return
        meta = (result or {}).get("meta") or {}
        session = _read_session(project_root)
        session.update(
            {
                "status": "done" if result.get("ok") else "failed",
                "phase": "finished",
                "error_type": meta.get("error_type"),
                "verdict": meta.get("verdict"),
                "finished_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
        artifacts = (meta.get("e2e") or {}).get("artifacts")
        if artifacts:
            session["artifacts_dir"] = str(artifacts)
        _write_session(project_root, session)
    except Exception:
        pass


def e2e_event(project_root: Path, session_id: str, event: dict) -> None:
    """A browser player event, already scrubbed of resolved values by the caller."""
    try:
        if not owns(project_root, session_id):
            return
        directory = _dir(project_root) / "e2e"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        if event.get("type") == "progress":
            session = _read_session(project_root)
            session.update({"phase": f"e2e step {event.get('step')} ({event.get('step_id')}): {event.get('status')}", "updated_at": now_iso()})
            _write_session(project_root, session)
    except Exception:
        pass


def e2e_finish(project_root: Path, session_id: str, e2e_dir: Path, final_dir: Path) -> None:
    """Copy the run's readable results and its newest screenshot into current/e2e/."""
    try:
        if not owns(project_root, session_id):
            return
        target = _dir(project_root) / "e2e"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("report.json", "verification.md", "evidence.md"):
            source = Path(e2e_dir) / name
            if source.is_file():
                shutil.copyfile(source, target / name)
        shots = sorted(Path(final_dir).glob("step*.png"))
        if shots:
            atomic_write_bytes(target / "last.png", shots[-1].read_bytes())
    except Exception:
        pass
