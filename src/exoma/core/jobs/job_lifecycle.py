"""Job admission, stall detection, waiting, and worker spawn.

Split out of main.py in v3.4.3. main.py stays the process entry and keeps owning
the singletons (JOB_MANAGER, EXECUTOR, SESSION_MANAGER) — this module reaches them
through the module object rather than importing them by value, so the harnesses'
monkeypatching of `main.*` keeps working. Importing them by value would bind the
pre-patch objects and make tests assert against something nothing else uses.
"""

import os
import sys
import time

from exoma.config.settings import (
    DEFAULT_IDLE_STALL_SECONDS,
    DEFAULT_JOB_POLL_INTERVAL_SECONDS,
    DEFAULT_JOB_POLL_TIMEOUT_SECONDS,
    DEFAULT_MAX_PROBES,
    DEFAULT_PROBE_RECHECK_SECONDS,
    DEFAULT_STALL_THRESHOLD_SECONDS,
    PACKAGE_PARENT,
    load_provider_config_for,
)
from exoma.core.evidence import verify_tests
from exoma.core.evidence.contract import make_error
from exoma.core.jobs.job_manager import DEAD
from exoma.core.evidence.result_shaping import _finalize_verify_result
from exoma.core.workspace.workspace_paths import detect_project_root
from exoma.utils import osutil


def _main():
    """The CLI module that owns the singletons (JOB_MANAGER, EXECUTOR, ...).

    Always `exoma.cli`: every entry point (`exoma`, `python -m exoma`, the checkout's
    `main.py` shim) imports it instead of running it as __main__, so there is exactly
    one set of singletons, the one the test harnesses patch. The worker is spawned as
    `python -m exoma --command worker` and lands in the same module.
    """
    from exoma import cli

    return cli


def submit(
    command: str,
    task: str,
    session_id: str,
    work_dir: str | None = None,
    model: str | None = None,
    allow_reuse: bool = True,
) -> dict:
    normalized_command = command.strip().lower()
    if normalized_command not in _main().BACKGROUND_COMMANDS:
        return make_error(
            "job_submit_error",
            f"unsupported submit target: {command}",
            next_action="Use a delegated command (explore/plan/analyze/verify).",
            meta={"command": normalized_command},
        )

    expected_hash = _main().JOB_MANAGER.request_hash(
        normalized_command, task, session_id, work_dir, allow_reuse, model
    )
    recovering = False

    def capacity_error() -> dict:
        return make_error(
            "worker_capacity",
            f"global worker limit reached ({_main().JOB_MANAGER.max_global_workers} active)",
            next_action="Wait for an in-flight delegated job to finish, then retry.",
            meta={
                "command": normalized_command,
                "max_global_workers": _main().JOB_MANAGER.max_global_workers,
            },
        )

    try:
        with _main().JOB_MANAGER.capacity_guard():
            active = _main().JOB_MANAGER.active_job_for_session(session_id)
            if (
                active
                and active.get("request_hash") != expected_hash
                and _main().JOB_MANAGER.release_dead_session_lock(
                    session_id, active["job_id"]
                )
            ):
                # A different request found the session held by a worker that is gone.
                # Refusing it left the session blocked until someone ran `clean`; the old
                # job is failed (reaped) instead, and this request proceeds as new.
                active = None
            if active:
                if active.get("request_hash") != expected_hash:
                    active_id = active["job_id"]
                    return make_error(
                        "job_already_running",
                        f"session {session_id} already has active job {active_id}",
                        next_action=(
                            f"Wait for {active_id} or run: "
                            f"bash .workflow/check.sh {active_id} --wait"
                        ),
                        meta={"command": normalized_command},
                        active_job_id=active_id,
                    )

                if active.get("worker_pid") and _main().JOB_MANAGER.liveness(active) != DEAD:
                    return {
                        "ok": True,
                        "job_id": active["job_id"],
                        "status": active["status"],
                        "submitted_at": active["created_at"],
                        "meta": {
                            "pid": active.get("worker_pid"),
                            "reused": True,
                            "recovery": False,
                        },
                    }

                pending_reservation = (
                    active.get("status") == "pending"
                    and active.get("worker_pid") is None
                )
                if (
                    not pending_reservation
                    and _main().JOB_MANAGER.active_worker_count()
                    >= _main().JOB_MANAGER.max_global_workers
                ):
                    return capacity_error()

                recovery = _main().JOB_MANAGER.claim_recovery(active["job_id"])
                action = recovery.get("action")
                job = recovery.get("job") or active
                if action in {"wait", "attach"}:
                    waiting_for_recovery = bool(job.get("recovery_in_progress"))
                    return {
                        "ok": True,
                        "job_id": job["job_id"],
                        "status": job.get("status", "pending"),
                        "submitted_at": job["created_at"],
                        "meta": {
                            "pid": job.get("worker_pid"),
                            "reused": True,
                            "recovery": waiting_for_recovery,
                            "recovery_pending": waiting_for_recovery,
                        },
                    }
                if action == "exhausted":
                    return make_error(
                        "worker_died",
                        f"job {job['job_id']} exhausted its single recovery attempt",
                        next_action=(
                            "The session lock was released. Report the interruption or "
                            "invoke the original task again as a clean run; do not send "
                            "continue automatically."
                        ),
                        meta={
                            "reason": "recovery_exhausted",
                            "job_id": job["job_id"],
                            "recovery_attempt": job.get("recovery_attempt", 1),
                        },
                    )
                if action != "recover":
                    return make_error(
                        "job_submit_error",
                        f"cannot recover active job {active['job_id']}",
                        next_action="Inspect the job record, then retry the original task.",
                        meta={"reason": action or "recovery_unknown"},
                    )
                recovering = True
            else:
                if _main().JOB_MANAGER.active_worker_count() >= _main().JOB_MANAGER.max_global_workers:
                    return capacity_error()
                try:
                    job = _main().JOB_MANAGER.create_job(
                        normalized_command,
                        task,
                        session_id,
                        work_dir,
                        model,
                        allow_reuse,
                    )
                    if normalized_command == "verify":
                        # A new job, not a recovery or an attach: the test request is this
                        # job's from here on, and an earlier job's leftover is dropped.
                        verify_tests.claim(detect_project_root(work_dir), session_id)
                except ValueError as exc:
                    active = _main().JOB_MANAGER.active_job_for_session(session_id)
                    active_id = active["job_id"] if active else None
                    next_action = (
                        f"Wait for {active_id} or run: "
                        f"bash .workflow/check.sh {active_id} --wait"
                        if active_id
                        else "Wait for the active job to finish, then retry."
                    )
                    return make_error(
                        "job_already_running",
                        str(exc),
                        next_action=next_action,
                        meta={"command": normalized_command},
                        active_job_id=active_id,
                    )
    except OSError as exc:
        return make_error(
            "job_submit_error",
            str(exc),
            next_action="Check that the job store is writable, then retry.",
            meta={"command": normalized_command, "error": type(exc).__name__},
        )

    try:
        worker = _main()._spawn_worker(job["job_id"], work_dir)
        if not worker["ok"]:
            _main().JOB_MANAGER.fail_job(job["job_id"], worker["content"])
            return worker
        _main().JOB_MANAGER.set_worker_pid(job["job_id"], worker["meta"]["pid"])
        return {
            "ok": True,
            "job_id": job["job_id"],
            "status": job["status"],
            "submitted_at": job["created_at"],
            "meta": {
                "pid": worker["meta"]["pid"],
                "reused": recovering,
                "recovery": recovering,
                "recovery_attempt": job.get("recovery_attempt", 0),
            },
        }
    finally:
        if recovering:
            _main().JOB_MANAGER.release_recovery_claim(job["job_id"])


def get_status(job_id: str) -> dict:
    job = _main().JOB_MANAGER.get_job(job_id)
    if not job:
        return {"ok": False, "job_id": job_id, "status": "not_found", "meta": {}}
    return {"ok": True, **job}


def get_result(job_id: str) -> dict:
    return _main().JOB_MANAGER.get_result(job_id)


def should_run_in_background(command: str) -> bool:
    return command.strip().lower() in _main().BACKGROUND_COMMANDS


def _job_config(work_dir: str | None) -> dict:
    return load_provider_config_for(detect_project_root(work_dir))


def _apply_job_thresholds(work_dir: str | None) -> None:
    """Push the project's stall/idle thresholds onto the shared JobManager.

    These live in .workflow/second_agent.json next to the other adapter timings, but the
    JobManager is constructed at import time with the tool defaults — so without this
    a project that tuned them got the defaults anyway.
    """
    config = _job_config(work_dir)
    try:
        _main().JOB_MANAGER.stall_threshold_seconds = int(
            config.get("stall_threshold_seconds", DEFAULT_STALL_THRESHOLD_SECONDS)
        )
        _main().JOB_MANAGER.idle_stall_seconds = int(
            config.get("idle_stall_seconds", DEFAULT_IDLE_STALL_SECONDS)
        )
    except (TypeError, ValueError):
        pass


def _probe_recheck_seconds(work_dir: str | None) -> float:
    try:
        return max(
            10.0,
            float(
                _job_config(work_dir).get(
                    "probe_recheck_seconds", DEFAULT_PROBE_RECHECK_SECONDS
                )
            ),
        )
    except (TypeError, ValueError):
        return float(DEFAULT_PROBE_RECHECK_SECONDS)


def _max_probes(work_dir: str | None) -> int:
    try:
        return max(
            1,
            int(_job_config(work_dir).get("max_probes", DEFAULT_MAX_PROBES)),
        )
    except (TypeError, ValueError):
        return DEFAULT_MAX_PROBES



def check_stalled_job(
    job_id: str, work_dir: str | None = None, model: str | None = None
) -> dict | None:
    """Two independent checks on a stalled job; either failure reaps it.

    1. Is the worker PID still a live process?
    2. Does the second agent answer a trivial prompt in a SEPARATE session?

    Check 2 is what check 1 cannot do. A rate-limited opencode holds the PID and keeps
    the poll loop turning, so PID liveness reports health for a job that will never
    return. Asking a fresh session is the only way to distinguish "quota exhausted" from
    "genuinely working" — and it must be a fresh session, because the stuck one is the
    thing under suspicion.

    Returns the terminal result when the job was reaped, or None to keep waiting.
    """
    job = _main().JOB_MANAGER.get_job(job_id) or {}
    pid = job.get("worker_pid")

    if pid is None or not osutil.process_alive(pid):
        return _main().JOB_MANAGER.reap_stalled(
            job_id,
            "worker_died",
            f"worker process {pid} is gone while job {job_id} was still running (reaped)",
            next_action=(
                "Check .workflow job logs for the worker traceback, then resubmit the command."
            ),
        )

    probe = _main().probe_second_agent(work_dir, model)
    try:
        _main().JOB_MANAGER.record_probe(job_id, probe)
    except (OSError, KeyError, ValueError):
        pass

    if probe.get("alive"):
        return None

    if probe.get("rate_limited") or probe.get("reason") == "probe_rate_limited":
        return _main().JOB_MANAGER.reap_stalled(
            job_id,
            "rate_limited",
            (
                f"job {job_id} stalled and the second agent is refusing on quota "
                f"({probe.get('reason')}) — reaped instead of waiting out the limit"
            ),
            next_action=(
                "Second agent is out of quota. Wait for the limit to reset, switch model in "
                ".workflow/second_agent.json, or check the provider account, then rerun the command."
            ),
            probe=probe,
        )

    if probe.get("stream_failed") or probe.get("reason") == "probe_stream_failed":
        # A partial provider stream is transient; reap the stalled job for resubmission.
        return _main().JOB_MANAGER.reap_stalled(
            job_id,
            "streaming_failed",
            (
                f"job {job_id} stalled and the probe's provider stream dropped "
                f"({probe.get('reason')}) — reaped as transient, not as an outage"
            ),
            next_action=(
                "The provider stream is dropping, not refusing. Resubmit the command; if it "
                "dies again, split the task into two narrower calls. Do NOT wait for a quota reset."
            ),
            probe=probe,
        )

    return _main().JOB_MANAGER.reap_stalled(
        job_id,
        "second_agent_unavailable",
        (
            f"job {job_id} stalled and a fresh-session probe also failed "
            f"({probe.get('reason')}) — reaped"
        ),
        next_action=(
            "Second agent is not answering at all. Check the provider CLI named by "
            "provider_command runs manually and is logged in, then rerun the command."
        ),
        probe=probe,
    )


def await_job(
    command: str,
    task: str,
    session_id: str,
    work_dir: str | None = None,
    model: str | None = None,
    poll_interval: float = DEFAULT_JOB_POLL_INTERVAL_SECONDS,
    poll_timeout: int = DEFAULT_JOB_POLL_TIMEOUT_SECONDS,
    allow_reuse: bool = True,
) -> dict:
    if command.strip().lower() == "sweep":
        return _main().run("sweep", task, session_id, work_dir, model)

    _apply_job_thresholds(work_dir)
    _main()._warn_if_workspace_stale(work_dir)
    submitted = submit(command, task, session_id, work_dir, model, allow_reuse)
    if not submitted.get("ok"):
        return submitted

    job_id = submitted["job_id"]
    started_at = time.monotonic()
    interval = poll_interval if poll_interval > 0 else DEFAULT_JOB_POLL_INTERVAL_SECONDS
    # poll_timeout=0 is the default and means "wait as long as this job may legitimately
    # run" — it was never meant to mean "wait forever". Every terminal status here comes
    # from the job record, so a record that stops advancing (deleted mid-flight, a status
    # that never reaches terminal, a clock the reaper reads differently) left this loop
    # with no exit at all, holding the session lock while it spun. The job's own hard
    # ceiling is the honest backstop: past it the reaper would fail the job anyway.
    # Both set to 0 stays unbounded — that is an explicit opt-out, not an oversight.
    deadline = poll_timeout if poll_timeout > 0 else _await_backstop_seconds()
    recheck_base = _probe_recheck_seconds(work_dir)
    max_probes = _max_probes(work_dir)
    last_probe_at: float | None = None
    probe_count = 0
    recheck = recheck_base

    while True:
        result = get_result(job_id)
        if (result.get("meta") or {}).get("error_type") == "worker_died" and (
            result.get("meta") or {}
        ).get("recoverable"):
            return {
                "ok": False,
                "content": result.get("content"),
                "meta": {
                    **dict(result.get("meta") or {}),
                    "job_id": job_id,
                    "submitted_at": submitted.get("submitted_at"),
                },
            }
        if (result.get("meta") or {}).get("error_type") == "worker_stalled":
            now = time.monotonic()
            if probe_count < max_probes and (
                last_probe_at is None or (now - last_probe_at) >= recheck
            ):
                last_probe_at = now
                probe_count += 1
                reaped = check_stalled_job(job_id, work_dir, model)
                if reaped is not None:
                    if reaped.get("status") == "completed":
                        continue
                    meta = dict(reaped.get("meta") or {})
                    meta.setdefault("job_id", job_id)
                    meta.setdefault("submitted_at", submitted.get("submitted_at"))
                    meta.setdefault("probe_count", probe_count)
                    return {
                        "ok": False,
                        "content": reaped.get("content") or f"job {job_id} reaped",
                        "meta": meta,
                    }
                # Probe came back alive: back off before spending the next one.
                recheck = min(recheck * 2, recheck_base * 8)
        if result.get("status") == "completed":
            output = result.get("output") or {}
            if isinstance(output, dict):
                output = _finalize_verify_result(command, output)
                meta = output.setdefault("meta", {})
                meta.setdefault("job_id", job_id)
                meta.setdefault("submitted_at", submitted.get("submitted_at"))
                meta.setdefault("worker_pid", submitted.get("meta", {}).get("pid"))
                return output
            return {
                "ok": False,
                "content": f"job {job_id} returned invalid output payload",
                "meta": {
                    "job_id": job_id,
                    "submitted_at": submitted.get("submitted_at"),
                },
            }

        if result.get("status") == "failed":
            meta = dict(result.get("meta") or {})
            meta.setdefault("job_id", job_id)
            meta.setdefault("submitted_at", submitted.get("submitted_at"))
            return {
                "ok": False,
                "content": result.get("content") or f"job {job_id} failed",
                "meta": meta,
            }

        if result.get("status") == "pending":
            queued = _main().JOB_MANAGER.get_job(job_id) or {}
            if queued.get("worker_pid") is None:
                resumed = submit(
                    command, task, session_id, work_dir, model, allow_reuse
                )
                if not resumed.get("ok"):
                    return resumed
                submitted = resumed

        if deadline > 0 and (time.monotonic() - started_at) >= deadline:
            return {
                "ok": False,
                "content": (
                    f"await timeout after {deadline}s"
                    if poll_timeout > 0
                    else (
                        f"await backstop after {deadline}s — no caller deadline was set "
                        "and the job never reached a terminal state"
                    )
                ),
                "meta": {
                    "job_id": job_id,
                    "status": result.get("status"),
                    "submitted_at": submitted.get("submitted_at"),
                    "worker_pid": submitted.get("meta", {}).get("pid"),
                    "deadline_source": (
                        "poll_timeout" if poll_timeout > 0 else "job_max_runtime"
                    ),
                },
            }

        time.sleep(interval)



def _await_backstop_seconds() -> int:
    """The job's own hard ceiling, plus one grace interval for the reaper to act.

    Read off the live JOB_MANAGER rather than the setting, so a harness that lowered the
    ceiling gets a backstop that matches the job it actually submitted.
    """
    try:
        ceiling = int(getattr(_main().JOB_MANAGER, "max_runtime_seconds", 0) or 0)
    except Exception:
        ceiling = 0
    if ceiling <= 0:
        return 0
    return ceiling + int(DEFAULT_JOB_POLL_INTERVAL_SECONDS * 5)


def run_worker(job_id: str) -> dict:
    job = _main().JOB_MANAGER.get_job(job_id)
    if not job:
        return {"ok": False, "job_id": job_id, "status": "not_found", "meta": {}}

    # The worker is authoritative for its own PID. Parent-side registration is only a
    # fast-path; this closes the small spawn-to-persist race after caller interruption.
    _main().JOB_MANAGER.set_worker_pid(job_id, os.getpid())
    running = _main().JOB_MANAGER.mark_running(job_id)
    if running.get("status") != "running":
        return make_error(
            "worker_died",
            f"job {job_id} became terminal before its worker started",
            next_action="Inspect the terminal job result; do not restart this worker.",
            meta={"job_id": job_id, "status": running.get("status")},
        )
    _main().JOB_MANAGER.touch_heartbeat(job_id, {"phase": "starting", "elapsed_seconds": 0})

    def _beat(progress: dict) -> None:
        _main().JOB_MANAGER.touch_heartbeat(job_id, progress)

    try:
        effective_task = job["task"]
        require_provider_session = int(job.get("recovery_attempt") or 0) > 0
        if require_provider_session and job.get("command") == "verify-browser":
            # A browser run has no "rest of the task" to continue: recovering it replays the
            # whole scenario, including every write the user allowed, on top of whatever the
            # dead attempt already sent. Refused, and the lock released with the job.
            refused = make_error(
                "worker_died",
                f"job {job_id} was a browser run; it is not resumed after its worker died",
                next_action=(
                    "Check the application's state first — steps may already have written. "
                    "Then start /.verify-browser again as a clean run."
                ),
                meta={"job_id": job_id, "reason": "not_recoverable", "command": "verify-browser"},
            )
            _main().JOB_MANAGER.fail_job(job_id, refused["content"], output=refused)
            return refused
        if require_provider_session:
            effective_task = (
                f"Continue the interrupted task for job {job_id}.\n"
                f"Original task: {job['task']}\n"
                "Inspect the existing session state. Resume unfinished work only; "
                "do not repeat completed findings. Return the normal evidence contract."
            )

        with verify_tests.job(job_id):
            output = _main().run(
                job["command"],
                effective_task,
                job["session_id"],
                job.get("work_dir"),
                job.get("model"),
                on_progress=_beat,
                allow_reuse=bool(job.get("allow_reuse", True)),
                require_provider_session=require_provider_session,
            )
        output.setdefault("meta", {}).setdefault("job_id", job_id)
        if output.get("ok"):
            _main().JOB_MANAGER.complete_job(job_id, output)
        else:
            _main().JOB_MANAGER.fail_job(
                job_id, output.get("content") or "worker failed", output=output
            )
        return output
    except Exception as exc:
        # Guarded: this is the worker's last line. If recording the failure raises too (the
        # same disk that failed the run), letting it escape would lose the crash report the
        # caller is about to get. fail_job releases the session lock itself, even when its
        # own save fails, so a raise here no longer strands the lock.
        record_error = None
        try:
            _main().JOB_MANAGER.fail_job(job_id, str(exc))
        except Exception as fail_exc:  # noqa: BLE001 — reported in the failure meta
            record_error = f"{type(fail_exc).__name__}: {fail_exc}"
        # Through make_error like every other failure: a plain dict here carried no
        # error_type and no next_action, so a caller branching on error_type saw None and
        # treated a crashed worker as an unknown shape rather than a known failure.
        failure = make_error(
            "worker_crashed",
            str(exc),
            next_action="inspect the worker log for this job_id, then resubmit",
        )
        failure["job_id"] = job_id
        failure["status"] = "failed"
        failure.setdefault("meta", {})["job_id"] = job_id
        if record_error:
            failure["meta"]["job_record_error"] = record_error
        return failure


def _spawn_worker(job_id: str, work_dir: str | None = None) -> dict:
    args = [
        sys.executable,
        "-m",
        "exoma",
        "--command",
        "worker",
        "--job-id",
        job_id,
    ]
    # `-m exoma` must resolve to this very package, installed or a source checkout's
    # src/, whatever the child's cwd (the project) happens to contain.
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(PACKAGE_PARENT), env.get("PYTHONPATH", "")) if part
    )
    log_path = _main().JOB_MANAGER.log_path(job_id)
    log_handle = _main().subprocess.DEVNULL
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
    except OSError:
        log_handle = _main().subprocess.DEVNULL  # never block spawning on logging
    try:
        proc = _main().subprocess.Popen(
            args,
            cwd=work_dir,
            env=env,
            stdin=_main().subprocess.DEVNULL,
            stdout=log_handle,
            stderr=(
                _main().subprocess.STDOUT
                if log_handle is not _main().subprocess.DEVNULL
                else _main().subprocess.DEVNULL
            ),
            **osutil.detached_popen_kwargs(),
        )
    except OSError as exc:
        return make_error(
            "unknown",
            str(exc),
            next_action="Check python is on PATH and the job dir is writable, then retry.",
            meta={"error": type(exc).__name__},
        )
    finally:
        if log_handle is not _main().subprocess.DEVNULL:
            log_handle.close()  # child inherited its own fd; parent copy not needed
    meta = {"pid": proc.pid}
    if log_handle is not _main().subprocess.DEVNULL:
        meta["log"] = str(log_path)
    return {"ok": True, "content": "worker started", "meta": meta}
