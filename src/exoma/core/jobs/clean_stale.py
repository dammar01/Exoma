"""Inspect recorded process generations and retire abandoned jobs."""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
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


def _distinct_cold_starts(prior_us, reserved_us, boot_us):
    """Match two different System log records bracketing the job's reservation."""
    window_us = 60_000_000
    if prior_us + window_us >= boot_us:
        return False
    query = "*[System[Provider[@Name='Microsoft-Windows-Kernel-Boot'] and EventID=27]]"
    try:
        exe = Path(os.environ["SystemRoot"]) / "System32" / "wevtutil.exe"
        code, xml, _ = osutil.run_bounded([str(exe), "qe", "System", "/q:" + query,
                                           "/rd:true", "/c:128", "/f:xml", "/e:Events"], timeout=5)
        if code != 0:
            return False
        events = ET.fromstring(xml).findall("{*}Event")
        if not events:
            return False
        latest_id = None
        for index, event in enumerate(events):
            cold = event.findtext("{*}EventData/{*}Data[@Name='BootType']") == "0"
            stamp = event.find("{*}System/{*}TimeCreated")
            if stamp is None:
                return False
            at = _timestamp_us(stamp.get("SystemTime"))
            record_id = int(event.findtext("{*}System/{*}EventRecordID", default=""))
            if index == 0:
                if not cold or record_id <= 0 or not boot_us <= at <= boot_us + window_us:
                    return False
                latest_id = record_id
            elif cold and 0 < record_id < latest_id and prior_us <= at <= min(reserved_us, prior_us + window_us):
                return True
        return False
    except (KeyError, OSError, ValueError, ET.ParseError):
        return False


def _reboot_skip_reason(manager, job, claim, boot):
    origin = job.get("attempt_origin")
    if (not isinstance(origin, dict) or (claim is not None and origin != claim.get("attempt_origin"))
            or not isinstance(origin.get("attempt"), str) or not re.fullmatch("[0-9a-f]{32}", origin["attempt"])
            or not _valid_boot(origin.get("boot"))):
        return "latest attempt lacks matching machine and boot evidence"
    if not _valid_boot(boot):
        return "current Windows machine or boot could not be verified"
    prior = origin["boot"]
    if prior["machine"] != boot["machine"]:
        return "recorded machine differs"
    if "reboot_cleanup" in job:
        audit = job["reboot_cleanup"]
        if (not isinstance(audit, dict) or audit.get("reason") != "verified_windows_reboot"
                or audit.get("attempt") != origin["attempt"] or job["status"] != "failed"
                or audit.get("prior_status") not in ("failed", "running")
                or not _valid_boot(audit.get("observed_boot"))
                or audit["observed_boot"]["machine"] != prior["machine"]
                or audit["observed_boot"]["boot_us"] <= prior["boot_us"]
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
    if prior["boot_id"] == boot["boot_id"] and not _distinct_cold_starts(prior["boot_us"], reserved, boot["boot_us"]):
        return "no independent evidence of two Windows boots"
    for owner in ("launcher", "worker", "reservation_owner"):
        if not _predates_boot(job, owner, boot["boot_us"]):
            return f"{owner} creation time conflicts with reboot evidence"
        if job.get(owner + "_pid") is not None and not manager._admission_process_gone(job, owner):
            return f"{owner} generation is live or unverifiable"
    if claim is not None and not _predates_boot(claim, "owner", boot["boot_us"]):
        return "session owner creation time conflicts with reboot evidence"
    targets = (job.get("cancellation") or {}).get("targets", [])
    if not isinstance(targets, list) or any(
        not isinstance(target, dict) or not _predates_boot(target, "worker", boot["boot_us"])
        or not manager._admission_process_gone(target, "worker")
        for target in targets
    ):
        return "recorded descendant generation is live or unverifiable"
    return None


def _decision(manager, job, claim, boot):
    status = job["status"]
    if status in ("pending", "running", "recovering") and "verify_checkpoint" in job:
        return "active", "verification awaiting publication; collect the original job result"
    if status in ("pending", "recovering"):
        owners = ("worker", "launcher", "reservation_owner")
        if any(manager.admission_process_state(job, prefix) == "alive" for prefix in owners):
            return "active", "startup or recovery is in progress"
        return "skipped", "startup or recovery reservation"
    if status not in ("running", "failed"):
        return "skipped", "outside recovery"
    if claim is not None:
        if not _owns_session_claim(job, claim):
            return "skipped", "session owner or token differs"
        if (type(claim.get("cleanup_required", False)) is not bool
                or claim.get("work_dir", job.get("work_dir")) != job.get("work_dir")
                or claim.get("attempt_origin") != job.get("attempt_origin")):
            return "skipped", "session and job evidence differs"
    for field in ("ownership", "cancellation"):
        item = job.get(field)
        if item is not None and (not isinstance(item, dict) or
                                 type(item.get("cleanup_confirmed", False)) is not bool):
            return "skipped", "invalid cleanup evidence"
    if status == "failed" and claim is None and all(
        job.get(field) is None or job[field].get("cleanup_confirmed") is True for field in ("ownership", "cancellation")
    ):
        return "skipped", "outside recovery"
    worker_state = manager.admission_process_state(job, "worker")
    if claim is None and worker_state != "gone":
        if worker_state == "alive":
            return "active", "worker is running"
        return "skipped", "missing worker evidence"
    if "pid_cleanup" in job:
        audit = job["pid_cleanup"]
        if (not isinstance(audit, dict) or status != "failed" or audit.get("reason") != "worker_process_gone"
                or audit.get("prior_status") != "running" or audit.get("worker_pid") != job.get("worker_pid")):
            return "skipped", "invalid PID cleanup history"
    if not _reboot_skip_reason(manager, job, claim, boot):
        return "eligible", "verified_windows_reboot"
    if "reboot_cleanup" in job or (status == "failed" and "pid_cleanup" not in job):
        return "skipped", "terminal job"
    origin = job.get("attempt_origin")
    if origin is not None and (not isinstance(origin, dict) or not _valid_boot(origin.get("boot"))
                               or not _valid_boot(boot) or origin["boot"]["machine"] != boot["machine"]):
        return "skipped", "machine identity unavailable or different"
    if worker_state == "alive":
        return "active", "worker is running"
    if worker_state != "gone" or (job.get("worker_registered") is not True and
            (job.get("worker_registered") is False or job.get("worker_pid") == job.get("launcher_pid"))):
        return "skipped", "worker generation unverified"
    for prefix in ("launcher", "reservation_owner"):
        if job.get(prefix + "_pid") is not None and not manager._admission_process_gone(job, prefix):
            return "skipped", "another owner is live or unverified"
    targets = (job.get("cancellation") or {}).get("targets", [])
    if not isinstance(targets, list) or any(not isinstance(t, dict) or
            not manager._admission_process_gone(t, "worker") for t in targets):
        return "skipped", "descendant generation unverified"
    if any(job.get(field) is not None and job[field].get("cleanup_confirmed") is not True
           for field in ("ownership", "cancellation")):
        return "skipped", "descendant cleanup unconfirmed"
    if (claim and claim.get("cleanup_required") and "pid_cleanup" not in job
            and claim.get("pid_cleanup_intent") != job.get("lock_token")):
        return "skipped", "session retains descendant ownership"
    return "eligible", "worker_process_gone"


def _repair(manager, job, claim):
    """Repeat the decision under capacity -> job -> session, then commit history first."""
    job_id = job["job_id"]
    path = manager._lock_path(job["session_id"])
    with manager.capacity_guard():
        manager.assert_update_admission()
        with manager._job_mutation(job_id, reclaim=False):
            with manager._exclusive_file_guard(path.with_suffix(".lock.guard")):
                current = manager._load(job_id)
                try:
                    with osutil.open_snapshot(path) as stream:
                        owner = json.load(stream)
                    if not isinstance(owner, dict):
                        raise ValueError("invalid session lock")
                except FileNotFoundError:
                    owner = None
                if current != job or owner != claim:
                    return "skipped", "record or owner changed during inspection"
                boot = osutil.windows_boot_evidence()
                action, reason = _decision(manager, current, owner, boot)
                if action != "eligible":
                    return action, reason
                # A failed record may already be terminal. Keep admission closed even
                # if the command exits between the job write and the final unlink.
                reboot = reason == "verified_windows_reboot"
                audit_key = "reboot_cleanup" if reboot else "pid_cleanup"
                if owner is not None and not owner.get("cleanup_required"):
                    retained = {**owner, "cleanup_required": True}
                    if not reboot:
                        retained["pid_cleanup_intent"] = current["lock_token"]
                    atomic_write_json(path, retained)
                if audit_key not in current:
                    now = manager._now()
                    audit = {"reason": reason, "at": now, "prior_status": current["status"],
                             "worker_pid": current.get("worker_pid")}
                    if reboot:
                        audit.update(attempt=current["attempt_origin"]["attempt"], observed_boot=boot)
                    current[audit_key] = audit
                    if current["status"] == "running":
                        current.update(status="failed", reaped=True)
                        if current.get("error") is None:
                            current["error"] = f"worker abandoned ({reason}; reaped)"
                        if current.get("completed_at") is None:
                            current["completed_at"] = now
                        current.pop("recovery_in_progress", None)
                    for field in ("ownership", "cancellation"):
                        if isinstance(current.get(field), dict):
                            current[field]["cleanup_confirmed"] = True
                    manager._save(current, durable=True)
                # The same guard holds from the nonempty token comparison above.
                if owner is not None:
                    path.unlink()
                return "recovered", reason


def inspect_jobs(manager, *, apply=False):
    boot = osutil.windows_boot_evidence()
    locks = {}
    unreadable = set()
    findings = []
    seen = set()
    for path, job, error in manager.admission_records():
        if error:
            unreadable.add(path)
            findings.append((path.name, "unknown", "error", error))
            continue
        if path.parent == manager.lock_dir:
            locks[path] = job
            continue
        job_id = path.stem
        status = job.get("status")
        action = "skipped"
        if (not isinstance(status, str) or job.get("job_id") != job_id or manager._path(job_id) != path
                or not isinstance(job.get("session_id"), str) or not job["session_id"]):
            reason = "invalid job or session identity"
        else:
            lock_path = manager._lock_path(job["session_id"])
            claim = locks.get(lock_path)
            if claim and claim.get("job_id") == job_id:
                seen.add(lock_path)
            if lock_path in unreadable or manager.lock_dir in unreadable:
                reason = "unreadable session lock"
            else:
                action, reason = _decision(manager, job, claim, boot)
            if apply and action == "eligible":
                try:
                    action, reason = _repair(manager, job, claim)
                except (ValueError, OSError) as exc:
                    reason = f"could not complete guarded recovery: {exc}"
                    action = "error"
        findings.append((job_id, status if isinstance(status, str) else "unknown", action, reason))
    for path, claim in locks.items():
        if path not in seen:
            findings.append((path.name, "lock", "skipped",
                             f"no matching job record for {claim.get('job_id') or 'unknown job'}"))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(prog="exoma jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    clean = sub.add_parser("clean-stale", help="clean stale jobs and print totals",
                           description="Retire confirmed stale jobs across your workspaces and print totals.")
    mode = clean.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview cleanup totals without changing jobs")
    mode.add_argument("--apply", action="store_true", help="apply cleanup (the default)")
    args = parser.parse_args(argv)
    if not args.dry_run and os.getenv("WORKFLOW_AGENT_ROLE", "").strip().lower() == "second_agent":
        print("The read-only second agent cannot apply job cleanup.", file=sys.stderr)
        return 1
    manager = JobManager(initialize=False)
    findings = inspect_jobs(manager, apply=not args.dry_run)
    # Global admission scans every workspace's jobs and retained claims.
    # Re-scanning cwd can count a single corrupt record a second time.
    blockers = manager.update_job_blockers()
    try:
        manager.assert_update_admission()
    except (OSError, ValueError) as exc:
        blockers["update reservation"] = str(exc)
    counts = Counter()
    for _, status, action, reason in findings:
        if action == "skipped":
            action = "terminal" if status in ("completed", "failed") and reason == "outside recovery" else "unverified"
        counts[action] += 1
    cleaned = "would_clean" if args.dry_run else "cleaned"
    print(f"examined={len(findings)} active={counts['active']} "
          f"{cleaned}={counts['eligible'] + counts['recovered']} terminal={counts['terminal']} "
          f"unverified={counts['unverified']} errors={counts['error']} update_blockers={len(blockers)}")
    return int(bool(counts["error"]) or (not args.dry_run and bool(blockers)))
