"""Read-only, bounded job snapshots. Importing this module creates no managers."""
import argparse
import json
from pathlib import Path
import re

from exoma.config.settings import JOB_DIR
from exoma.core.workspace.workspace_paths import workflow_paths
from exoma.utils.redact import redact_value


def _read(path, limit=262144):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            return {"unavailable": "size_limit"}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"unavailable": "not_object"}
    except (OSError, ValueError, RecursionError):
        return {"unavailable": "missing_or_concurrent_write"}


def snapshot(project_root, session_id, job_id=None, job_dir=None):
    root = Path(project_root).resolve()
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return {"ok": False, "content": "A bound session is required", "meta": {"error_type": "session_state_error"}}
    if job_id and not re.fullmatch(r"job_[A-Za-z0-9_-]+", job_id):
        return {"ok": False, "content": "Invalid job ID", "meta": {"error_type": "job_submit_error"}}
    paths = workflow_paths(root, session_id)
    store = Path(job_dir) if job_dir is not None else JOB_DIR
    candidates = [store / (job_id + ".json")] if job_id else sorted(store.glob("job_*.json"), reverse=True)[:256]
    jobs = []
    for path in candidates:
        job = _read(path)
        if job.get("session_id") != session_id or not job.get("work_dir"):
            continue
        try:
            owned = Path(job["work_dir"]).resolve() == root
        except (TypeError, ValueError, OSError):
            owned = False
        if not owned:
            continue
        selected = {key: job.get(key) for key in (
            "job_id", "command", "status", "worker_pid", "created_at", "started_at",
            "completed_at", "recovery_attempt", "cancellation", "error")}
        selected["heartbeat"] = _read(store / "beats" / (path.stem + ".beat.json"))
        # Probe metadata is read as stored, never refreshed or executed.
        selected["probe"] = _read(store / "beats" / (path.stem + ".probe.json"))
        jobs.append(selected)
        if len(jobs) == 5:
            break
    if job_id and not jobs:
        return {"ok": False, "content": "Job missing or not owned by this project/session",
                "meta": {"error_type": "job_submit_error", "read_only": True}}
    result = {"ok": True, "content": f"Read-only snapshot: {len(jobs)} matching job(s)",
              "meta": {"read_only": True, "project_root": str(root), "session_id": session_id},
              "state": _read(paths["state"]), "jobs": jobs}
    return redact_value(result)[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--job-id")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--command", "-c", choices=["inspect"])
    parser.add_argument("--main-host")
    args = parser.parse_args(argv)
    result = snapshot(args.work_dir, args.session, args.job_id)
    print(json.dumps(result, indent=2 if args.pretty else None))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
