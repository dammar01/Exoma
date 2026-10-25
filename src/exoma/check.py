import argparse
import json
import sys
import time

# Before any import that the newer syntax would break: the oldest supported Python is
# utils/osutil.py MIN_PYTHON, repeated here because importing it would fail on the very
# interpreter this names.
if sys.version_info < (3, 10):
    sys.exit(
        f"Exoma needs Python 3.10 or newer; this is {sys.version.split()[0]} "
        f"({sys.executable}). Run it with a newer python."
    )

from exoma.config.settings import (  # noqa: E402
    DEFAULT_JOB_POLL_INTERVAL_SECONDS,
    DEFAULT_JOB_POLL_TIMEOUT_SECONDS,
    DEFAULT_MAX_PROBES,
    DEFAULT_PROBE_RECHECK_SECONDS,
)
from exoma.core.evidence.contract import validate_verification_contract, verify_exit_status
from exoma.core.jobs.job_manager import JobManager

JOB_MANAGER = JobManager()
# Attach-path cadence for the stall checks. Fixed rather than per-project: check.py is
# handed a job id and nothing else, so there is no config to read before the first poll.
_PROBE_RECHECK_SECONDS = float(DEFAULT_PROBE_RECHECK_SECONDS)
# Same probe budget as the await path: cap the number of quota-spending probes for one
# stalled job, and back off between them. Past the budget, stop probing — job_max_runtime
# still reaps a job that never returns.
_MAX_PROBES = DEFAULT_MAX_PROBES
# A record can still read "running" while its worker is gone: get_result keeps the first
# dead worker recoverable instead of failing it. Polling must treat that as finished
# anyway, the same way the await loop does in core/jobs/job_lifecycle.py.
_TERMINAL_ERROR_TYPES = {"worker_died"}


def _terminal_meta(result: dict) -> dict:
    """Fields that end polling for a record get_result left in a non-final status."""
    meta = result.get("meta") or {}
    error_type = meta.get("error_type")
    if not error_type:
        return {}
    fields = {"error_type": error_type}
    if error_type in _TERMINAL_ERROR_TYPES:
        fields["done"] = True
        if meta.get("next_action"):
            fields["next_action"] = meta["next_action"]
    return fields


def _status_payload(job_id: str) -> dict:
    # Use get_result so attach polling also runs liveness checks and reaping.
    result = JOB_MANAGER.get_result(job_id)
    status = result.get("status") or "unknown"
    payload = {
        "ok": status == "completed",
        "job_id": job_id,
        "status": status,
        # not_found is terminal because no record can later complete.
        "done": status in {"completed", "failed", "not_found"},
    }
    payload.update(_terminal_meta(result))
    if result.get("content") and status != "completed":
        payload["content"] = result["content"]
    return payload


def _reap_if_stalled(job_id: str, payload: dict) -> dict | None:
    """Run the same PID + fresh-session probe checks `await` runs, and reap on failure.

    Imported lazily: check.py is the attach path and must stay usable even when the
    heavier main module cannot be imported for an unrelated reason.
    """
    if payload.get("error_type") != "worker_stalled":
        return None
    job = JOB_MANAGER.get_job(job_id) or {}
    try:
        from exoma.cli import check_stalled_job
    except Exception:
        return None
    reaped = check_stalled_job(job_id, job.get("work_dir"), job.get("model"))
    if reaped is None:
        return None
    return _status_payload(job_id)


def _resume_pending_reservation(job_id: str, payload: dict) -> dict:
    """Let attach polling reclaim a submit that died before spawning its worker."""
    if payload.get("status") != "pending":
        return payload
    job = JOB_MANAGER.get_job(job_id) or {}
    if job.get("worker_pid") is not None:
        return payload
    try:
        from exoma.cli import submit

        submit(
            str(job.get("command") or ""),
            str(job.get("task") or ""),
            str(job.get("session_id") or ""),
            job.get("work_dir"),
            job.get("model"),
            bool(job.get("allow_reuse", True)),
        )
    except Exception:
        return payload
    return _status_payload(job_id)


def _result_payload(job_id: str):
    result = JOB_MANAGER.get_result(job_id)
    if result.get("status") == "completed":
        output = result.get("output") or {}
        return True, output.get("content") or ""

    status = result.get("status", "not_found")
    payload = {
        "ok": False,
        "job_id": job_id,
        "status": status,
        "done": status in {"completed", "failed", "not_found"},
    }
    # Same terminal rule as _status_payload — the two paths diverging here is what made
    # an attach on a killed worker poll until timeout.
    payload.update(_terminal_meta(result))
    if result.get("content"):
        payload["content"] = result["content"]
    return False, payload


def _wait_for_status(job_id: str, poll_interval: float, poll_timeout: int) -> dict:
    started_at = time.monotonic()
    interval = poll_interval if poll_interval > 0 else DEFAULT_JOB_POLL_INTERVAL_SECONDS
    last_probe_at: float | None = None
    probe_count = 0
    recheck = _PROBE_RECHECK_SECONDS

    while True:
        payload = _status_payload(job_id)
        payload = _resume_pending_reservation(job_id, payload)
        # `done` can be set while the status still reads running: a dead worker's record
        # is left recoverable rather than failed, so status alone never ends the loop.
        if payload.get("done") or payload["status"] not in {"pending", "running"}:
            return payload

        now = time.monotonic()
        if probe_count < _MAX_PROBES and (
            last_probe_at is None or (now - last_probe_at) >= recheck
        ):
            last_probe_at = now
            probe_count += 1
            reaped = _reap_if_stalled(job_id, payload)
            if reaped is not None:
                return reaped
            recheck = min(recheck * 2, _PROBE_RECHECK_SECONDS * 8)

        if poll_timeout > 0 and (time.monotonic() - started_at) >= poll_timeout:
            return {
                "ok": False,
                "job_id": job_id,
                "status": payload["status"],
                "done": False,
                "timed_out": True,
            }

        time.sleep(interval)


def _wait_for_result(job_id: str, poll_interval: float, poll_timeout: int):
    started_at = time.monotonic()
    interval = poll_interval if poll_interval > 0 else DEFAULT_JOB_POLL_INTERVAL_SECONDS
    last_probe_at: float | None = None
    probe_count = 0
    recheck = _PROBE_RECHECK_SECONDS

    while True:
        ok, payload = _result_payload(job_id)
        if not ok and payload.get("status") == "pending":
            status = _resume_pending_reservation(job_id, _status_payload(job_id))
            if status.get("status") != "pending":
                ok, payload = _result_payload(job_id)
        if ok:
            return True, payload
        if payload.get("done") or payload["status"] not in {"pending", "running"}:
            return False, payload

        now = time.monotonic()
        if probe_count < _MAX_PROBES and (
            last_probe_at is None or (now - last_probe_at) >= recheck
        ):
            last_probe_at = now
            probe_count += 1
            if _reap_if_stalled(job_id, _status_payload(job_id)) is not None:
                return False, _status_payload(job_id)
            recheck = min(recheck * 2, _PROBE_RECHECK_SECONDS * 8)

        if poll_timeout > 0 and (time.monotonic() - started_at) >= poll_timeout:
            payload["timed_out"] = True
            return False, payload

        time.sleep(interval)


def _write_text(payload: str) -> None:
    try:
        sys.stdout.write(payload)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(payload.encode("utf-8", errors="replace"))


def _exit_code_for_status(status: str) -> int:
    if status == "completed":
        return 0
    if status == "failed":
        return 1
    if status in {"pending", "running"}:
        return 2
    if status == "not_found":
        return 3
    return 1


def _exit_code_for_result(job_id: str) -> int:
    job = JOB_MANAGER.get_job(job_id) or {}
    if str(job.get("command") or "").strip().lower() != "verify":
        return 0
    output = job.get("output")
    if not isinstance(output, dict) or not output.get("ok"):
        return 2
    meta = output.get("meta") or {}
    verdict = meta.get("verdict")
    assessment = meta.get("verify_contract")
    if verdict is None:
        assessment = validate_verification_contract(output.get("content") or "")
        verdict = assessment["verdict"]
    return verify_exit_status(verdict, assessment)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check workflow job status or result")
    parser.add_argument("job_id")
    parser.add_argument(
        "--result",
        action="store_true",
        help="return final cleaned output only when completed",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="poll internally until completion or timeout",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_JOB_POLL_INTERVAL_SECONDS,
        help="seconds between job status polls while waiting",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=DEFAULT_JOB_POLL_TIMEOUT_SECONDS,
        help="max seconds to wait; 0 means wait forever",
    )
    args = parser.parse_args()

    if args.result:
        ok, payload = (
            _wait_for_result(args.job_id, args.poll_interval, args.poll_timeout)
            if args.wait
            else _result_payload(args.job_id)
        )
        if ok:
            _write_text(payload)
            return _exit_code_for_result(args.job_id)
        print(json.dumps(payload))
        return _exit_code_for_status(payload.get("status"))

    payload = (
        _wait_for_status(args.job_id, args.poll_interval, args.poll_timeout)
        if args.wait
        else _status_payload(args.job_id)
    )
    print(json.dumps(payload))
    return _exit_code_for_status(payload.get("status"))


if __name__ == "__main__":
    raise SystemExit(main())
