import hashlib
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from config.settings import (
    DEFAULT_IDLE_STALL_SECONDS,
    DEFAULT_JOB_MAX_RUNTIME_SECONDS,
    DEFAULT_MAX_GLOBAL_WORKERS,
    DEFAULT_STALL_THRESHOLD_SECONDS,
    JOB_DIR,
)
from core.workspace.workspace_paths import atomic_write_json
from utils import osutil
from utils.path_guard import safe_path_component

ALIVE_PROGRESSING = "alive-progressing"  # PID up, heartbeat fresh, stream producing
ALIVE_STALLED = "alive-stalled"  # PID up but silent -> probe before judging
DEAD = "dead"  # PID gone
_CLAIM_STALE_SECONDS = 30.0
_MUTATION_WAIT_SECONDS = 5.0
# The recorded process is no longer the one the PID names: it exited, or the PID was reused.
_GONE = frozenset({osutil.PROCESS_DEAD, osutil.PROCESS_REUSED})

# --- What in this file is load-bearing, and for whom -------------------------------------
#
# This is a job scheduler for one person on one machine, and the size is fair to question.
# The answer is not uniform, so it is written down rather than re-derived each time someone
# reads the line count and reaches for a rewrite.
#
# Earns its place even single-user:
#   _acquire_session_lock  - two Claude sessions on ONE project root is the case that forced
#                            per-session state to exist; without it their jobs overwrite each
#                            other. This is the concurrency that actually happens.
#   capacity_guard /
#   active_worker_count    - a burst of parallel main-agents would otherwise spawn unbounded
#                            opencode processes. The ceiling has to be enforced across
#                            processes to be a ceiling at all.
#   claim_recovery         - bounded restart after a dead worker. One attempt, then it stops.
#   liveness               - alive-but-silent and dead need opposite responses; collapsing
#                            them either kills working jobs or waits forever on dead ones.
#
# Costs more than it returns HERE, kept because it is cheap and correct where it does apply:
#   osutil.process_generation
#                          - the `unverifiable` outcome exists for platforms that expose no
#                            process generation. Callers must still handle it; a kill
#                            treats it as a different process (_kill_worker).
#   _exclusive_file_guard  - cross-process advisory locking on mutation files. Single-process
#                            runs never contend for these.
#   the retry loop in _acquire_session_lock
#                          - contention it retries against needs a second live main-agent.
#
# Nothing above is dead code, and none of it should be deleted on those grounds alone. If
# single-user ever becomes a first-class configuration, the second group is what a flag
# would gate — not a rewrite.
# -----------------------------------------------------------------------------------------


class JobManager:
    def __init__(
        self,
        job_dir: Path = JOB_DIR,
        stall_threshold_seconds: int = DEFAULT_STALL_THRESHOLD_SECONDS,
        max_runtime_seconds: int = DEFAULT_JOB_MAX_RUNTIME_SECONDS,
        idle_stall_seconds: int = DEFAULT_IDLE_STALL_SECONDS,
        max_global_workers: int = DEFAULT_MAX_GLOBAL_WORKERS,
    ) -> None:
        self.job_dir = Path(job_dir)
        self.lock_dir = self.job_dir / "locks"
        self.beat_dir = self.job_dir / "beats"
        self.recovery_dir = self.job_dir / "recovery"
        self.mutation_dir = self.job_dir / "mutations"
        self.log_dir = self.job_dir / "logs"
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.beat_dir.mkdir(parents=True, exist_ok=True)
        self.recovery_dir.mkdir(parents=True, exist_ok=True)
        self.mutation_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.stall_threshold_seconds = stall_threshold_seconds
        self.max_runtime_seconds = max_runtime_seconds
        self.idle_stall_seconds = idle_stall_seconds
        self.max_global_workers = max_global_workers
        self._recovery_claim_tokens: dict[str, str] = {}

    def create_job(
        self,
        workflow_command: str,
        task: str | None,
        session_id: str,
        work_dir: str | None,
        model: str | None,
        allow_reuse: bool = True,
    ) -> dict:
        task = task or ""
        request_hash = self._request_hash(
            workflow_command, task, session_id, work_dir, allow_reuse, model
        )

        # Idempotency: identical request already active on this session → reuse it.
        existing = self.active_job_for_session(session_id)
        if existing and existing.get("request_hash") == request_hash:
            return existing

        job_id = self._new_job_id()
        lock_token = self._acquire_session_lock(session_id, job_id)
        job = {
            "job_id": job_id,
            "request_hash": request_hash,
            "command": workflow_command,
            "task": task,
            "session_id": session_id,
            "lock_token": lock_token,
            "work_dir": work_dir,
            "model": model,
            "allow_reuse": bool(allow_reuse),
            "status": "pending",
            "worker_pid": None,
            "worker_identity": None,
            "reservation_owner_pid": os.getpid(),
            "reservation_owner_create_time": osutil.pid_create_time(os.getpid()),
            "reservation_owner_identity": osutil.process_identity(os.getpid()),
            "recovery_attempt": 0,
            "created_at": self._now(),
            "started_at": None,
            "completed_at": None,
            "output": None,
            "error": None,
        }
        try:
            self._save(job)
        except Exception:
            self._release_session_lock(job)
            raise
        return job

    def claim_recovery(
        self, job_id: str, max_attempts: int = 1, stale_after_seconds: float = 30.0
    ) -> dict:
        """Claim one bounded restart of a dead worker without releasing its session lock."""
        claim_path = self.recovery_dir / f"{self._safe(job_id)}.claim"
        token = secrets.token_hex(16)
        try:
            fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = self._read_side(claim_path)
            if not self._claim_is_stale(claim_path, stale_after_seconds, existing):
                return {"action": "wait", "job": self.get_job(job_id)}
            try:
                self._release_claim_path(
                    claim_path,
                    expected_token=(existing or {}).get("token"),
                    force=existing is None,
                )
                fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except (FileNotFoundError, FileExistsError, OSError):
                return {"action": "wait", "job": self.get_job(job_id)}

        payload = {
            "job_id": job_id,
            "claimed_at": self._now(),
            "owner_pid": os.getpid(),
            "owner_create_time": osutil.pid_create_time(os.getpid()),
            "owner_identity": osutil.process_identity(os.getpid()),
            "token": token,
        }
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file)
            file.flush()
            os.fsync(file.fileno())
        self._recovery_claim_tokens[job_id] = token

        with self._job_mutation(job_id):
            job = self.get_job(job_id)
            if not job:
                self.release_recovery_claim(job_id)
                return {"action": "missing", "job": None}
            if job.get("status") not in {"pending", "running", "recovering"}:
                self.release_recovery_claim(job_id)
                return {"action": "attach", "job": job}
            worker_pid = job.get("worker_pid")
            worker_generation = osutil.process_generation(
                worker_pid,
                job.get("worker_create_time"),
                job.get("worker_identity"),
            )
            if worker_pid and worker_generation not in _GONE:
                self.release_recovery_claim(job_id)
                return {"action": "attach", "job": job}
            initial_orphan = (
                job.get("status") == "pending"
                and job.get("worker_pid") is None
                and not job.get("recovery_in_progress")
            )
            if initial_orphan:
                owner_pid = job.get("reservation_owner_pid")
                owner_generation = osutil.process_generation(
                    owner_pid,
                    job.get("reservation_owner_create_time"),
                    job.get("reservation_owner_identity"),
                )
                if owner_generation == osutil.PROCESS_MATCHED:
                    self.release_recovery_claim(job_id)
                    return {"action": "wait", "job": job}
                age = self._age_seconds(job.get("created_at"))
                if age is None or age < stale_after_seconds:
                    self.release_recovery_claim(job_id)
                    return {"action": "wait", "job": job}

            attempt = int(job.get("recovery_attempt") or 0)
            stale_before_spawn = (
                job.get("recovery_in_progress") is True
                and job.get("worker_pid") is None
            )
            counts_as_restart = not stale_before_spawn and not initial_orphan
            if counts_as_restart and attempt >= max_attempts:
                job["status"] = "failed"
                job["completed_at"] = self._now()
                job["error"] = f"worker died again after {attempt} recovery attempt(s)"
                job["reaped"] = True
                job.pop("recovery_in_progress", None)
                self._save(job)
                exhausted = True
            else:
                if counts_as_restart:
                    job["recovery_attempt"] = attempt + 1
                job["status"] = "pending"
                job["recovery_in_progress"] = True
                job["recovery_started_at"] = self._now()
                if initial_orphan or (
                    stale_before_spawn
                    and job.get("recovery_reason") == "pre_spawn_orphan"
                ):
                    job["recovery_reason"] = "pre_spawn_orphan"
                else:
                    job["recovery_reason"] = "worker_died"
                job["previous_worker_pid"] = job.get("worker_pid")
                job["worker_pid"] = None
                job["worker_create_time"] = None
                job["worker_identity"] = None
                job["reservation_owner_pid"] = os.getpid()
                job["reservation_owner_create_time"] = osutil.pid_create_time(
                    os.getpid()
                )
                job["reservation_owner_identity"] = osutil.process_identity(os.getpid())
                job["error"] = None
                job["output"] = None
                job.pop("reaped", None)
                self._save(job)
                exhausted = False

        if exhausted:
            self._release_session_lock(job)
            self.release_recovery_claim(job_id)
            return {"action": "exhausted", "job": job}

        for side in (self._beat_path(job_id), self._probe_path(job_id)):
            try:
                side.unlink()
            except FileNotFoundError:
                pass
        return {"action": "recover", "job": job}

    def release_recovery_claim(self, job_id: str) -> None:
        token = self._recovery_claim_tokens.pop(job_id, None)
        if token:
            self._release_claim_path(
                self.recovery_dir / f"{self._safe(job_id)}.claim",
                expected_token=token,
            )

    def set_worker_pid(self, job_id: str, pid: int | None) -> None:
        with self._job_mutation(job_id):
            job = self._load(job_id)
            if job.get("status") not in {"pending", "running", "recovering"}:
                return
            job["worker_pid"] = pid
            job["worker_create_time"] = osutil.pid_create_time(pid)
            job["worker_identity"] = osutil.process_identity(pid)
            job.pop("reservation_owner_pid", None)
            job.pop("reservation_owner_create_time", None)
            job.pop("reservation_owner_identity", None)
            self._save(job)

    def _kill_worker(self, job: dict) -> dict:
        """Terminate a worker by PID — only while the PID provably still hosts it.

        Centralises the reuse guard so every by-PID kill path is protected. Fail-closed:
        a PID whose process cannot be compared with the recorded one (`unverifiable`) is
        treated like a reused one and left alone. A missed kill leaves a stray worker that
        heartbeat and max-runtime reaping already fail the job for; a wrongful kill takes
        out someone else's process. A dead PID has nothing left to kill.
        """
        pid = job.get("worker_pid")
        if not pid:
            return osutil.terminate_tree(None, pid=pid)
        if pid == os.getpid():
            # Never terminate the current process. A worker_pid set to (or recycled into)
            # this runtime's own PID must not let a reap take the runtime down — and on
            # Windows terminate_tree runs `taskkill /T`, which would also kill our children.
            return {"method": "skipped", "ok": False, "reason": "self_pid", "pid": pid}
        generation = osutil.process_generation(
            pid, job.get("worker_create_time"), job.get("worker_identity")
        )
        if generation != osutil.PROCESS_MATCHED:
            reason = {
                osutil.PROCESS_REUSED: "pid_reuse_detected",
                osutil.PROCESS_DEAD: "worker_dead",
            }.get(generation, "identity_unverifiable")
            return {"method": "skipped", "ok": False, "reason": reason, "pid": pid}
        return osutil.terminate_tree(None, pid=pid)

    def touch_heartbeat(self, job_id: str, progress: dict | None = None) -> None:
        """Record one beat from the worker's poll loop.

        Written to a SIDE FILE, never into the job record. The job JSON is
        read-modify-written by whoever calls get_result (a different process), and a
        beat lands every couple of seconds: folding the beat into the job record let a
        late write resurrect a job that had just been reaped, wiping its terminal state.
        One writer per file removes the race instead of narrowing it.

        Best-effort: a failed heartbeat write must never abort the call it is watching.
        """
        payload = {"at": self._now()}
        if progress is not None:
            payload["progress"] = progress
        try:
            self._write_side(self._beat_path(job_id), payload)
        except OSError:
            pass

    def read_heartbeat(self, job_id: str) -> dict | None:
        return self._read_side(self._beat_path(job_id))

    def mark_running(self, job_id: str) -> dict:
        with self._job_mutation(job_id):
            job = self._load(job_id)
            if job.get("status") not in {"pending", "recovering"}:
                return job
            job["status"] = "running"
            job["started_at"] = job["started_at"] or self._now()
            self._save(job)
            return job

    def complete_job(self, job_id: str, output: dict) -> dict:
        # Release in `finally`, outside the mutation: a failed save used to raise past the
        # release, leaving the job "running" AND its session locked until a reaper ran. The
        # release is token-checked, so freeing it after a failed save cannot free a lock a
        # newer job holds.
        job = None
        try:
            with self._job_mutation(job_id):
                job = self._load(job_id)
                if job.get("status") == "completed":
                    pass
                elif job.get("reaped") or job.get("status") == "failed":
                    job["late_output"] = output
                    self._save(job)
                else:
                    job["status"] = "completed"
                    job.pop("recovery_in_progress", None)
                    job["completed_at"] = self._now()
                    job["output"] = output
                    job["error"] = None
                    self._save(job)
        finally:
            self._release_after_finish(job_id, job)
        return job

    def _release_after_finish(self, job_id: str, job: dict | None) -> None:
        """Release a finished job's session lock, even when its record could not be read.

        A missing or corrupt job file used to leave `job` unset, so the release was
        skipped and the session stayed locked. The lock file itself names its job, so the
        lock is found by that and released with the token it carries — still never a lock
        some other job holds.
        """
        if job is not None:
            self._release_session_lock(job)
            return
        for path in sorted(self.lock_dir.glob("*.lock")):
            current = self._read_lock(path)
            if current and current.get("job_id") == job_id:
                self._release_lock_path(
                    path, expected_job_id=job_id, expected_token=current.get("token")
                )

    def fail_job(
        self,
        job_id: str,
        error: str,
        output: dict | None = None,
        reaped: bool = False,
    ) -> dict:
        job = None
        try:
            with self._job_mutation(job_id):
                job = self._load(job_id)
                self._apply_failure(job, error, output, reaped)
        finally:
            # Same reasoning as complete_job: the lock is released even when the save fails.
            self._release_after_finish(job_id, job)
        return job

    def _apply_failure(
        self, job: dict, error: str, output: dict | None, reaped: bool
    ) -> None:
        if job.get("status") == "completed":
            pass
        elif job.get("status") == "failed":
            changed = False
            if output is not None and job.get("output") is None:
                job["output"] = output
                changed = True
            if reaped and not job.get("reaped"):
                job["reaped"] = True
                changed = True
            if changed:
                self._save(job)
        else:
            job["status"] = "failed"
            job.pop("recovery_in_progress", None)
            job["completed_at"] = self._now()
            job["error"] = error
            if output is not None:
                job["output"] = output
            if reaped:
                job["reaped"] = True
            self._save(job)

    def get_job(self, job_id: str) -> dict | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        return self._load(job_id)

    def get_result(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            return {"ok": False, "job_id": job_id, "status": "not_found", "meta": {}}

        if job["status"] in {"pending", "running"}:
            state = self.liveness(job)
            if state == DEAD:
                attempt = int(job.get("recovery_attempt") or 0)
                if attempt >= 1:
                    from core.evidence.contract import make_error

                    message = f"worker died again after {attempt} recovery attempt(s)"
                    output = make_error(
                        "worker_died",
                        message,
                        next_action=(
                            "The session lock was released. Report the interruption or "
                            "start a clean run; do not continue automatically."
                        ),
                        meta={
                            "reason": "recovery_exhausted",
                            "recovery_attempt": attempt,
                            "worker_pid": job.get("worker_pid"),
                        },
                    )
                    job = self.fail_job(
                        job_id,
                        message,
                        output=output,
                        reaped=True,
                    )
                else:
                    return {
                        "ok": False,
                        "job_id": job_id,
                        "status": job["status"],
                        "content": "worker process died; identical request can recover it once",
                        "meta": {
                            "error_type": "worker_died",
                            "recoverable": True,
                            "recovery_attempt": attempt,
                            "worker_pid": job.get("worker_pid"),
                            "next_action": (
                                "Invoke the same runner command with the same session and task "
                                "to resume through the saved provider session."
                            ),
                        },
                    }
            elif self._exceeded_max_runtime(job):
                self._kill_worker(job)
                job = self.fail_job(
                    job_id,
                    f"job exceeded max runtime {self.max_runtime_seconds}s (reaped)",
                    reaped=True,
                )
            elif state == ALIVE_STALLED:
                # Alive but silent. Report it — never reap on suspicion alone.
                beat = self.read_heartbeat(job_id) or {}
                probe = self.read_probe(job_id) or {}
                return {
                    "ok": False,
                    "job_id": job_id,
                    "status": job["status"],
                    "content": (
                        f"job {job_id} is running but has produced no progress for "
                        f">{self.stall_threshold_seconds}s"
                    ),
                    "meta": {
                        "error_type": "worker_stalled",
                        "liveness": probe.get("liveness") or ALIVE_STALLED,
                        "next_action": (
                            "Probe the second agent to tell a rate limit from a hang; "
                            "do not resubmit blindly."
                        ),
                        "worker_pid": job.get("worker_pid"),
                        "last_heartbeat": beat.get("at"),
                        "idle_seconds": self._idle_seconds(job),
                        "progress": beat.get("progress"),
                        "probe": probe.get("probe"),
                    },
                }

        if job["status"] == "completed":
            return {
                "ok": True,
                "job_id": job_id,
                "status": "completed",
                "output": job["output"],
            }
        if job["status"] == "failed":
            stored = job.get("output")
            if isinstance(stored, dict) and stored.get("meta"):
                return {
                    "ok": False,
                    "job_id": job_id,
                    "status": "failed",
                    "content": stored.get("content")
                    or job.get("error")
                    or "job failed",
                    "meta": dict(stored.get("meta") or {}),
                }
            return {
                "ok": False,
                "job_id": job_id,
                "status": "failed",
                "content": job.get("error") or "job failed",
                "meta": {"error_type": self._failure_type(job.get("error"))},
            }
        return {
            "ok": False,
            "job_id": job_id,
            "status": job["status"],
            "content": f"job {job_id} not completed yet",
            "meta": {},
        }

    def release_stale_session_locks(self) -> dict:
        """Release session locks whose owning job can never finish.

        The lock is deliberately released by job state, not by a timer — a long delegated
        call must not have its lock stolen while it works. The gap that leaves: a worker
        that DIES keeps the lock, because get_result reports `worker_died` as recoverable
        and never fails the record. The identical request recovers it, and since v3.8.2 a
        DIFFERENT request on the same session releases it too (release_dead_session_lock),
        but a caller who simply stops leaves it held until this runs.

        Only provably-finished owners are cleared: a missing record, a terminal status, or
        a worker PID that is gone. A live or merely silent worker is left alone — that is
        the case the lock exists for.
        """
        released: list[dict] = []
        kept = 0
        for path in sorted(self.lock_dir.glob("*.lock")):
            outcome = self._release_if_stale(path, "clean")
            if outcome is None:
                kept += 1
            else:
                released.append(outcome)
        return {"released": released, "kept": kept}

    def release_dead_session_lock(self, session_id: str, job_id: str) -> bool:
        """Free one session for a new request when its job's worker is provably gone.

        Narrower than `release_stale_session_locks` on purpose: only this session, only
        the job the caller saw, and only a DEAD worker. A record mid-recovery has no
        worker PID, so liveness cannot call it dead and it is never taken. Callers hold
        `capacity_guard`, the same guard `claim_recovery` runs under, so an identical
        request cannot be claiming the job while it is released. The old job is failed
        (reaped), not recovered: the session moved on to a different request.
        """
        outcome = self._release_if_stale(
            self._lock_path(session_id),
            "a different request on the session",
            dead_worker_only=True,
            expected_job_id=job_id,
        )
        return outcome is not None

    def _release_if_stale(
        self,
        path: Path,
        released_by: str,
        dead_worker_only: bool = False,
        expected_job_id: str | None = None,
    ) -> dict | None:
        """Release the lock at `path` if its owner can never finish. None when kept.

        `expected_job_id` pins the decision to the job the caller saw: the lock is read
        once here and everything after — the liveness verdict, `fail_job`, the token-checked
        release — follows that read, so a lock that changed hands is left alone instead of
        failing a job the caller never looked at.
        """
        data = self._read_lock(path) or {}
        job_id = data.get("job_id") or ""
        if expected_job_id is not None and job_id != expected_job_id:
            return None
        job = self.get_job(job_id) if job_id else None
        if job is None:
            reason = "no_job_record"
        elif job.get("status") not in {"pending", "running", "recovering"}:
            reason = f"job_{job.get('status')}"
        elif self.liveness(job) == DEAD:
            reason = "worker_died"
        else:
            return None
        if dead_worker_only and reason != "worker_died":
            return None
        if reason == "worker_died":
            try:
                self.fail_job(
                    job_id,
                    f"worker died and the session lock was released by {released_by}",
                    reaped=True,
                )
            except OSError:
                # A job file that cannot be written stays as it is; the lock is still
                # released, which is what the caller asked for. Narrowed from a bare
                # Exception: a non-I/O failure here means fail_job itself is broken, and
                # silently continuing hid that.
                pass
        outcome = {"session_id": path.stem, "job_id": job_id, "reason": reason}
        if not path.exists():
            return outcome
        if self._release_lock_path(
            path,
            expected_job_id=data.get("job_id"),
            expected_token=data.get("token"),
            force=job is None,
        ):
            return outcome
        return None

    def prune_jobs(self, ttl_days: int = 7, keep_last: int = 50) -> dict:
        """Delete terminal jobs older than ttl_days, always keeping the newest keep_last."""
        files = sorted(
            (p for p in self.job_dir.glob("job_*.json")),
            key=lambda p: p.name,
            reverse=True,
        )
        cutoff = time.time() - ttl_days * 86400
        removed = 0
        logs_removed = 0
        for index, path in enumerate(files):
            if index < keep_last:
                continue
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") not in {"completed", "failed"}:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    # Side files outlive their job otherwise: they are never rewritten
                    # after it ends, so nothing else would ever clean them up.
                    for side in (
                        self._beat_path(job.get("job_id", "")),
                        self._probe_path(job.get("job_id", "")),
                        self.log_path(job.get("job_id", "")),
                    ):
                        try:
                            side.unlink()
                            if side.parent == self.log_dir:
                                logs_removed += 1
                        except OSError:
                            pass
                    removed += 1
            except OSError:
                continue

        for log_path in self.log_dir.glob("*.log"):
            if self._path(log_path.stem).exists():
                continue
            try:
                if log_path.stat().st_mtime < cutoff:
                    log_path.unlink()
                    logs_removed += 1
            except OSError:
                continue
        return {
            "removed": removed,
            "kept": len(files) - removed,
            "logs_removed": logs_removed,
        }

    def _worker_dead(self, job: dict) -> bool:
        return self.liveness(job) == DEAD

    def liveness(self, job: dict) -> str | None:
        """Tri-state worker health, or None when there is nothing to judge yet.

        Three signals, in order of how much they can be trusted:

        1. PID gone            -> DEAD. Unambiguous.
        2. Stream idle         -> ALIVE_STALLED. The adapter reports how long it has
           been since opencode emitted a byte. This is the only one that catches a
           rate-limited agent: its PID is up and its heartbeat is fresh, because the
           poll loop that emits the beat keeps turning while nothing comes back.
        3. Heartbeat stale     -> ALIVE_STALLED. Catches a worker that stopped beating
           entirely (crashed loop, blocked thread) rather than one that beats emptily.

        Stream idleness is checked before heartbeat age because a polling loop can
        keep emitting fresh heartbeats while provider output is stalled.
        """
        pid = job.get("worker_pid")
        if pid is None:
            return None  # not yet spawned; nothing to reap
        generation = osutil.process_generation(
            pid, job.get("worker_create_time"), job.get("worker_identity")
        )
        if generation in _GONE:
            return DEAD

        idle = self._idle_seconds(job)
        if (
            self.idle_stall_seconds
            and idle is not None
            and idle > self.idle_stall_seconds
        ):
            return ALIVE_STALLED

        age = self._heartbeat_age_seconds(job)
        if age is None:
            # No beat yet: fall back to how long the job has been running, so a worker
            # that died before its first tick still gets classified instead of hanging.
            age = self._age_seconds(job.get("started_at") or job.get("created_at"))
        if age is not None and age > self.stall_threshold_seconds:
            return ALIVE_STALLED
        return ALIVE_PROGRESSING

    def _idle_seconds(self, job: dict) -> float | None:
        """Seconds since opencode last produced output, per the latest beat.

        None when the beat predates this field (a worker still running from an older
        build) — absence must not be read as zero, or an old worker would look busy.
        """
        beat = self.read_heartbeat(job.get("job_id") or "")
        progress = (beat or {}).get("progress")
        if not isinstance(progress, dict):
            return None
        idle = progress.get("idle_seconds")
        if idle is None:
            return None
        try:
            reported = float(idle)
        except (TypeError, ValueError):
            return None
        # The beat itself may be stale; the idle window has kept growing since it was
        # written. Add that gap, otherwise a worker that stopped beating mid-wait looks
        # like it is only as idle as it was at its last beat.
        drift = self._age_seconds((beat or {}).get("at")) or 0.0
        return reported + max(0.0, drift)

    def _heartbeat_age_seconds(self, job: dict) -> float | None:
        beat = self.read_heartbeat(job.get("job_id") or "")
        stamp = beat.get("at") if beat else job.get("last_heartbeat")
        return self._age_seconds(stamp)

    @staticmethod
    def _age_seconds(stamp: str | None) -> float | None:
        if not stamp:
            return None
        try:
            when = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds()

    def _exceeded_max_runtime(self, job: dict) -> bool:
        """Backstop for the OOM case: the box ran out of RAM, the worker went down in a
        way the PID check can miss (or the PID got reused), and the job would otherwise
        sit in `running` forever."""
        if not self.max_runtime_seconds or self.max_runtime_seconds <= 0:
            return False
        age = self._age_seconds(job.get("started_at") or job.get("created_at"))
        return age is not None and age > self.max_runtime_seconds

    def record_probe(self, job_id: str, probe: dict) -> dict:
        """Attach a liveness-probe verdict without mutating the job record.

        Side file for the same reason as the heartbeat: the worker may complete the job
        at any moment, and a probe written into the job record could overwrite that.
        """
        payload = {
            "probe": probe,
            "liveness": (
                "stalled_no_progress" if probe.get("alive") else "stalled_on_limit"
            ),
            "at": self._now(),
        }
        try:
            self._write_side(self._probe_path(job_id), payload)
        except OSError:
            pass
        return payload

    def read_probe(self, job_id: str) -> dict | None:
        return self._read_side(self._probe_path(job_id))

    def reap_stalled(
        self,
        job_id: str,
        error_type: str,
        message: str,
        next_action: str,
        probe: dict | None = None,
    ) -> dict:
        """Terminate a stalled worker and fail its job with a typed reason.

        The kill is not optional. `fail_job` releases the session lock, so without it
        the detached worker keeps burning quota against a job nobody is waiting for,
        and a later `complete_job` would resurrect a record the caller already treated
        as terminal. Best-effort: an unkillable worker still gets its job failed —
        reporting a stall the caller can act on beats blocking on a process kill.
        """
        from core.evidence.contract import make_error

        with self._job_mutation(job_id):
            job = self._load(job_id)
            if job.get("status") not in {"pending", "running", "recovering"}:
                terminal = True
            else:
                job["status"] = "failed"
                job["reaped"] = True
                job["completed_at"] = self._now()
                job["error"] = message
                job.pop("recovery_in_progress", None)
                self._save(job)
                terminal = False

        if terminal:
            result = self.get_result(job_id)
            if result.get("status") == "completed":
                return {
                    "ok": True,
                    "job_id": job_id,
                    "status": "completed",
                    "output": result.get("output"),
                }
            return result

        kill = self._kill_worker(job)
        meta = {
            "worker_pid": job.get("worker_pid"),
            "kill": kill,
            "idle_seconds": self._idle_seconds(job),
            "stall_threshold_seconds": self.stall_threshold_seconds,
            "idle_stall_seconds": self.idle_stall_seconds,
        }
        if probe is not None:
            meta["probe"] = probe
        output = make_error(error_type, message, next_action=next_action, meta=meta)

        failed = self.fail_job(job_id, message, output=output, reaped=True)
        return {**output, "job_id": job_id, "status": failed.get("status", "failed")}

    def active_worker_count(self) -> int:
        """Workers in flight across ALL sessions (global concurrency).

        Counts live startup reservations and live workers. A pending/running record whose
        recorded process generation is gone does not consume capacity, so it can recover
        without deadlocking on its own stale slot.
        """
        n = 0
        for path in self.job_dir.glob("*.json"):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or rec.get("reaped"):
                continue
            status = rec.get("status")
            if status in {"pending", "recovering"}:
                worker_pid = rec.get("worker_pid")
                if worker_pid:
                    generation = osutil.process_generation(
                        worker_pid,
                        rec.get("worker_create_time"),
                        rec.get("worker_identity"),
                    )
                    if generation == osutil.PROCESS_MATCHED:
                        n += 1
                    elif generation == osutil.PROCESS_UNVERIFIABLE:
                        age = self._age_seconds(
                            rec.get("recovery_started_at") or rec.get("created_at")
                        )
                        if age is None or age <= _CLAIM_STALE_SECONDS:
                            n += 1
                    continue

                owner_pid = rec.get("reservation_owner_pid")
                owner_generation = osutil.process_generation(
                    owner_pid,
                    rec.get("reservation_owner_create_time"),
                    rec.get("reservation_owner_identity"),
                )
                if owner_generation == osutil.PROCESS_MATCHED:
                    n += 1
                elif owner_generation == osutil.PROCESS_UNVERIFIABLE and owner_pid:
                    age = self._age_seconds(
                        rec.get("recovery_started_at") or rec.get("created_at")
                    )
                    if age is None or age <= _CLAIM_STALE_SECONDS:
                        n += 1
            elif status == "running":
                generation = osutil.process_generation(
                    rec.get("worker_pid"),
                    rec.get("worker_create_time"),
                    rec.get("worker_identity"),
                )
                if generation not in _GONE:
                    n += 1
        return n

    def active_job_for_session(self, session_id: str) -> dict | None:
        lock_path = self._lock_path(session_id)
        if not lock_path.exists():
            return None
        data = self._read_lock(lock_path)
        if not data:
            return None
        job = self.get_job(data.get("job_id", ""))
        if job and job.get("status") in {"pending", "running", "recovering"}:
            return job
        return None

    def _path(self, job_id: str) -> Path:
        return self.job_dir / f"{self._safe(job_id)}.json"

    def _beat_path(self, job_id: str) -> Path:
        return self.beat_dir / f"{self._safe(job_id)}.beat.json"

    def _probe_path(self, job_id: str) -> Path:
        return self.beat_dir / f"{self._safe(job_id)}.probe.json"

    def _mutation_path(self, job_id: str) -> Path:
        return self.mutation_dir / f"{self._safe(job_id)}.claim"

    def log_path(self, job_id: str) -> Path:
        return self.log_dir / f"{self._safe(job_id)}.log"

    @contextmanager
    def _exclusive_file_guard(self, path: Path, timeout: float = 30.0):
        """Cross-process advisory lock backed by a persistent one-byte file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = osutil.open_lock_file(path)
        deadline = time.monotonic() + timeout
        locked = False
        try:
            while not locked:
                try:
                    osutil.lock_file(handle)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for guard {path}")
                    time.sleep(0.01)
            yield
        finally:
            if locked:
                osutil.unlock_file(handle)
            handle.close()

    @contextmanager
    def capacity_guard(self):
        """Serialize worker admission and workspace upgrades across processes."""
        with self._exclusive_file_guard(self.job_dir / ".capacity.guard"):
            yield

    @contextmanager
    def _job_mutation(self, job_id: str):
        path = self._mutation_path(job_id)
        token = secrets.token_hex(16)
        deadline = time.monotonic() + _MUTATION_WAIT_SECONDS
        payload = {
            "job_id": job_id,
            "owner_pid": os.getpid(),
            "owner_create_time": osutil.pid_create_time(os.getpid()),
            "owner_identity": osutil.process_identity(os.getpid()),
            "created_at": self._now(),
            "token": token,
        }

        while True:
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                existing = self._read_side(path)
                if self._claim_is_stale(path, _CLAIM_STALE_SECONDS, existing):
                    self._release_claim_path(
                        path,
                        expected_token=(existing or {}).get("token"),
                        force=existing is None,
                    )
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting to update job {job_id}")
                time.sleep(0.01)

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file)
                file.flush()
                os.fsync(file.fileno())
            yield
        finally:
            self._release_claim_path(path, expected_token=token)

    @staticmethod
    def _claim_is_stale(
        path: Path, stale_after_seconds: float, payload: dict | None
    ) -> bool:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        if age <= stale_after_seconds:
            return False
        if payload:
            pid = payload.get("owner_pid")
            generation = osutil.process_generation(
                pid,
                payload.get("owner_create_time"),
                payload.get("owner_identity"),
            )
            if generation == osutil.PROCESS_MATCHED:
                return False
        return True

    def _release_claim_path(
        self,
        path: Path,
        expected_token: str | None = None,
        force: bool = False,
    ) -> bool:
        guard_path = path.with_name(f"{path.name}.guard")
        with self._exclusive_file_guard(guard_path):
            if not force:
                current = self._read_side(path)
                if not current or current.get("token") != expected_token:
                    return False
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False

    def _write_side(self, path: Path, payload: dict) -> None:
        # The shared primitive (unique temp name, `os.replace` retried while a reader holds
        # the target on Windows). A fixed `<id>.tmp` plus a bare replace failed a real run
        # with WinError 5 whenever `check.py` or a waiting runner was reading the record.
        atomic_write_json(path, payload)

    @staticmethod
    def _read_side(path: Path) -> dict | None:
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _lock_path(self, session_id: str) -> Path:
        return self.lock_dir / f"{self._safe(session_id)}.lock"

    def _load(self, job_id: str) -> dict:
        with self._path(job_id).open("r", encoding="utf-8") as file:
            return json.load(file)

    def _save(self, job: dict) -> None:
        atomic_write_json(self._path(job["job_id"]), job)

    def _acquire_session_lock(self, session_id: str, job_id: str) -> str:
        lock_path = self._lock_path(session_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(16)
        payload = {
            "session_id": session_id,
            "job_id": job_id,
            "created_at": self._now(),
            "owner_pid": os.getpid(),
            "owner_create_time": osutil.pid_create_time(os.getpid()),
            "owner_identity": osutil.process_identity(os.getpid()),
            "token": token,
        }

        for _ in range(3):
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                existing = self._read_lock(lock_path)
                if not existing:
                    if not self._claim_is_stale(lock_path, _CLAIM_STALE_SECONDS, None):
                        raise ValueError(
                            f"session {session_id} lock is being initialized"
                        )
                    self._release_lock_path(lock_path, force=True)
                    continue

                existing_job = self.get_job(existing.get("job_id", ""))
                if existing_job and existing_job.get("status") in {
                    "pending",
                    "running",
                    "recovering",
                }:
                    if self._worker_dead(existing_job):
                        self.fail_job(
                            existing_job["job_id"],
                            "worker process died before completing (reaped)",
                            reaped=True,
                        )
                    elif self._exceeded_max_runtime(existing_job):
                        self._kill_worker(existing_job)
                        self.fail_job(
                            existing_job["job_id"],
                            f"job exceeded max runtime {self.max_runtime_seconds}s (reaped)",
                            reaped=True,
                        )
                    else:
                        raise ValueError(
                            f"session {session_id} already has active job {existing_job['job_id']}"
                        )
                else:
                    if existing_job or self._claim_is_stale(
                        lock_path, _CLAIM_STALE_SECONDS, existing
                    ):
                        self._release_lock_path(
                            lock_path,
                            expected_job_id=existing.get("job_id"),
                            expected_token=existing.get("token"),
                        )
                    else:
                        raise ValueError(
                            f"session {session_id} lock has no job record yet"
                        )
        else:
            raise ValueError(f"session {session_id} lock changed during acquisition")

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
        except Exception:
            self._release_lock_path(
                lock_path, expected_job_id=job_id, expected_token=token, force=True
            )
            raise
        return token

    def _release_session_lock(self, job: dict) -> None:
        self._release_lock_path(
            self._lock_path(job["session_id"]),
            expected_job_id=job.get("job_id"),
            expected_token=job.get("lock_token"),
        )

    def _release_lock_path(
        self,
        path: Path,
        expected_job_id: str | None = None,
        expected_token: str | None = None,
        force: bool = False,
    ) -> bool:
        guard_path = path.with_name(f"{path.name}.guard")
        with self._exclusive_file_guard(guard_path):
            if not force:
                current = self._read_lock(path)
                if not current or current.get("job_id") != expected_job_id:
                    return False
                if expected_token and current.get("token") != expected_token:
                    return False
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False

    @staticmethod
    def _read_lock(path: Path) -> dict | None:
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _failure_type(error: str | None) -> str:
        text = error or ""
        if "max runtime" in text:
            return "job_expired"
        if "reaped" in text:
            return "worker_died"
        return "unknown"

    @staticmethod
    def request_hash(
        command: str,
        task: str | None,
        session_id: str,
        work_dir: str | None,
        allow_reuse: bool = True,
        model: str | None = None,
    ) -> str:
        raw = "|".join(
            [
                (command or "").strip().lower(),
                (task or "").strip(),
                session_id,
                work_dir or "",
                "reuse" if allow_reuse else "fresh",
                model or "",
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    _request_hash = request_hash

    @staticmethod
    def _safe(value: str) -> str:
        return safe_path_component(value)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _new_job_id(self) -> str:
        return f"job_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
