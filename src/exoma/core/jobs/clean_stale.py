"""Inspect and recover job ownership after a verified Windows reboot."""

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from exoma.core.jobs.job_manager import JobManager
from exoma.core.workspace.workspace_paths import atomic_write_json
from exoma.utils import osutil


def _timestamp_us(value):
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("timestamp has no timezone")
    delta = stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _valid_boot(boot):
    return (isinstance(boot, dict) and isinstance(boot.get("machine"), str)
            and re.fullmatch("[0-9a-f]{64}", boot["machine"])
            and isinstance(boot.get("boot_id"), str) and re.fullmatch("[0-9a-f]{32}", boot["boot_id"])
            and boot["boot_id"] not in ("0" * 32, "f" * 32)
            and type(boot.get("boot_us")) is int and boot["boot_us"] > 0)


def _predates_boot(record, prefix, boot_us):
    """Cross-check any persisted Windows creation FILETIME, including a reused PID."""
    birth = record.get(prefix + "_create_time")
    identity = record.get(prefix + "_identity")
    if identity is not None:
        match = re.fullmatch(r"native:([0-9]{1,20})", identity) if isinstance(identity, str) else None
        if not match:
            return False
        ticks = int(match[1])
        if birth is not None and (type(birth) is not int or birth != ticks):
            return False
        birth = ticks
    epoch_ticks = 116444736000000000
    return birth is None or (type(birth) is int and epoch_ticks < birth < epoch_ticks + 10 * boot_us)


def _owns_session_claim(job, claim):
    token = job.get("lock_token")
    return (claim is not None and isinstance(token, str) and bool(token)
            and claim.get("job_id") == job["job_id"] and claim.get("session_id") == job["session_id"]
            and claim.get("token") == token)


def _skip_reason(manager, job, claim, boot):
    if job.get("status") not in ("running", "failed"):
        return "status is outside reboot recovery"
    if not claim:
        return "no matching session lock"
    if not _owns_session_claim(job, claim):
        return "session owner or token differs"
    if type(claim.get("cleanup_required", False)) is not bool:
        return "invalid session cleanup flag"
    if claim.get("work_dir") is not None and claim["work_dir"] != job.get("work_dir"):
        return "session and job workspace evidence differs"
    for field in ("ownership", "cancellation"):
        item = job.get(field)
        if item is not None and (not isinstance(item, dict) or
                                 type(item.get("cleanup_confirmed", False)) is not bool):
            return f"invalid {field} evidence"
    origin = job.get("attempt_origin")
    if (not isinstance(origin, dict) or origin != claim.get("attempt_origin")
            or not isinstance(origin.get("attempt"), str) or not re.fullmatch("[0-9a-f]{32}", origin["attempt"])
            or not _valid_boot(origin.get("boot"))):
        return "latest attempt lacks matching machine and boot evidence"
    if not _valid_boot(boot):
        return "current Windows machine or boot could not be verified"
    prior = origin["boot"]
    if prior["machine"] != boot["machine"]:
        return "recorded machine differs"
    if prior["boot_id"] == boot["boot_id"]:
        return "same Windows boot; descendants may still be running"
    if "reboot_cleanup" in job:
        audit = job["reboot_cleanup"]
        if (not isinstance(audit, dict) or audit.get("reason") != "verified_windows_reboot"
                or audit.get("attempt") != origin["attempt"] or job["status"] != "failed"
                or audit.get("prior_status") not in ("failed", "running")
                or not _valid_boot(audit.get("observed_boot"))
                or audit["observed_boot"]["machine"] != prior["machine"]
                or audit["observed_boot"]["boot_id"] == prior["boot_id"]
                or any(item and item.get("cleanup_confirmed") is not True
                       for item in (job.get("ownership"), job.get("cancellation")))):
            return "inconsistent previous recovery record"
    try:
        reserved = _timestamp_us(origin["at"])
        stamps = [_timestamp_us(job["created_at"]), reserved]
        for name in ("started_at", "recovery_started_at"):
            if job.get(name) is not None:
                stamps.append(_timestamp_us(job[name]))
        if prior["boot_us"] > reserved or max(stamps) >= boot["boot_us"]:
            return "restart does not verifiably follow the latest attempt"
    except (KeyError, ValueError, OverflowError):
        return "invalid attempt chronology"
    for owner in ("launcher", "worker", "reservation_owner"):
        if not _predates_boot(job, owner, boot["boot_us"]):
            return f"{owner} creation time conflicts with reboot evidence"
        if job.get(owner + "_pid") is not None and not manager._admission_process_gone(job, owner):
            return f"{owner} generation is live or unverifiable"
    if not _predates_boot(claim, "owner", boot["boot_us"]):
        return "session owner creation time conflicts with reboot evidence"
    targets = (job.get("cancellation") or {}).get("targets", [])
    if not isinstance(targets, list) or any(
        not isinstance(target, dict) or not _predates_boot(target, "worker", boot["boot_us"])
        or not manager._admission_process_gone(target, "worker")
        for target in targets
    ):
        return "recorded descendant generation is live or unverifiable"
    return None


def _repair(manager, job, claim):
    """Repeat the decision under capacity -> job -> session, then commit history first."""
    job_id = job["job_id"]
    path = manager._lock_path(job["session_id"])
    with manager.capacity_guard():
        manager.assert_update_admission()
        with manager._job_mutation(job_id, reclaim=False):
            with manager._exclusive_file_guard(path.with_suffix(".lock.guard")):
                current = manager._load(job_id)
                owner = manager._read_lock(path)
                if current != job or owner != claim:
                    return "skipped", "record or owner changed during inspection"
                boot = osutil.windows_boot_evidence()
                reason = _skip_reason(manager, current, owner, boot)
                if reason:
                    return "skipped", reason
                # A failed record may already be terminal. Keep admission closed even
                # if the command exits between the job write and the final unlink.
                if not owner.get("cleanup_required"):
                    atomic_write_json(path, {**owner, "cleanup_required": True})
                if "reboot_cleanup" not in current:
                    now = manager._now()
                    current["reboot_cleanup"] = {
                        "reason": "verified_windows_reboot", "at": now,
                        "attempt": current["attempt_origin"]["attempt"], "observed_boot": boot,
                        "prior_status": current["status"],
                    }
                    if current["status"] == "running":
                        current.update(status="failed", reaped=True)
                        if current.get("error") is None:
                            current["error"] = "worker abandoned before verified Windows reboot (reaped)"
                        if current.get("completed_at") is None:
                            current["completed_at"] = now
                        current.pop("recovery_in_progress", None)
                    for field in ("ownership", "cancellation"):
                        if isinstance(current.get(field), dict):
                            current[field]["cleanup_confirmed"] = True
                    manager._save(current, durable=True)
                # The same guard holds from the nonempty token comparison above.
                path.unlink()
                return "recovered", "verified Windows reboot; matching lock released"


def inspect_jobs(manager, *, apply=False):
    boot = None if apply else osutil.windows_boot_evidence()
    locks = {}
    findings = []
    seen = set()
    for path, job, error in manager.admission_records():
        if error:
            findings.append((path.name, "unknown", "skipped", error))
            continue
        if path.parent == manager.lock_dir:
            locks[path] = job
            continue
        job_id = path.stem
        status = job.get("status")
        action = "skipped"
        if (job.get("job_id") != job_id or manager._path(job_id) != path
                or not isinstance(job.get("session_id"), str) or not job["session_id"]):
            reason = "invalid job or session identity"
        else:
            lock_path = manager._lock_path(job["session_id"])
            claim = locks.get(lock_path)
            if claim and claim.get("job_id") == job_id:
                seen.add(lock_path)
            if apply and status in ("running", "failed") and _owns_session_claim(job, claim):
                try:
                    action, reason = _repair(manager, job, claim)
                except (ValueError, OSError) as exc:
                    reason = f"could not complete guarded recovery: {exc}"
                    action = "error"
            else:
                reason = _skip_reason(manager, job, claim, boot)
                if reason is None:
                    action, reason = "eligible", "verified Windows reboot after latest attempt"
        findings.append((job_id, status if isinstance(status, str) else "unknown", action, reason))
    for path, claim in locks.items():
        if path not in seen:
            findings.append((path.name, "lock", "skipped",
                             f"no matching job record for {claim.get('job_id') or 'unknown job'}"))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(prog="exoma jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    clean = sub.add_parser("clean-stale", help="explain stale-job evidence in the user store")
    clean.add_argument("--apply", action="store_true", help="recover jobs with verified reboot evidence")
    args = parser.parse_args(argv)
    if args.apply and os.getenv("WORKFLOW_AGENT_ROLE", "").strip().lower() == "second_agent":
        print("The read-only second agent cannot apply job cleanup.", file=sys.stderr)
        return 1
    manager = JobManager(initialize=False)
    findings = inspect_jobs(manager, apply=args.apply)
    for job_id, status, action, reason in findings:
        print(f"{job_id} [{status}] {action}: {reason}")
    if not findings:
        print("No job records or session claims found.")
    if not args.apply:
        return 0
    from exoma.core.runtime.upgrade import active_jobs_for_workspace
    from exoma.self_update import _workspace_root

    blockers = manager.update_job_blockers()
    try:
        manager.assert_update_admission()
    except (OSError, ValueError) as exc:
        blockers["update reservation"] = str(exc)
    workspace = _workspace_root(Path.cwd())
    if workspace:
        try:
            for job in active_jobs_for_workspace(workspace, manager=manager):
                blockers.setdefault(str(job.get("job_id")), "workspace job admission")
        except (ValueError, OSError) as exc:
            blockers["workspace"] = str(exc)
    for key, reason in blockers.items():
        print(f"Update blocker {key}: {reason}")
    if not blockers:
        print("No remaining job admission blockers for update.")
    return int(bool(blockers) or any(row[2] == "error" for row in findings))
