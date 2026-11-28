"""Job admission, stall detection, waiting, and worker spawn.

Split out of main.py in v3.4.3. main.py stays the process entry and keeps owning
the singletons (JOB_MANAGER, EXECUTOR, SESSION_MANAGER) — this module reaches them
through the module object rather than importing them by value, so the harnesses'
monkeypatching of `main.*` keeps working. Importing them by value would bind the
pre-patch objects and make tests assert against something nothing else uses.
"""

import errno
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

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
from exoma.core.evidence import verify_tests, verify_followup
from exoma.core.evidence.contract import make_error
from exoma.core.jobs.job_manager import DEAD, JobCancellation, WorkerOwnership
from exoma.core.evidence.result_shaping import _finalize_verify_result
from exoma.core.workspace.workspace_paths import REPLACE_RETRY_DELAYS, atomic_write_json, detect_project_root, now_iso
from exoma._bundle.config.hosts.claude.hooks import workflow_policy as gate
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


# The errnos a sandbox or a read-only mount refuses a write with. A Windows access error
# arrives as PermissionError (winerror 5 maps to EACCES), checked by type as well.
_REFUSAL_ERRNOS = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})


def _within(path, root) -> bool:
    """Whether `path` is `root` or lies under it, compared as the OS resolves both."""
    from pathlib import Path

    try:
        full = Path(os.path.normcase(os.path.abspath(path)))
        base = Path(os.path.normcase(os.path.abspath(root)))
        return full == base or base in full.parents
    except (TypeError, ValueError):
        return False


def job_store_fix(job_dir) -> dict:
    """How to make the job store writable for a sandboxed host (CASE-034).

    Every delegated submit writes the global job store, which lives under the per-user data
    directory, outside any project. A host sandbox that only lets the workspace be written
    (Codex `workspace-write`) refuses it. An install whose hosts include codex writes this grant
    as a managed block in ~/.codex/config.toml (installer/base.py `_install_codex_grant`) unless
    the user already keeps that table; this names the root and the exact snippet for that
    case and for any other host. The root is the storage
    directory when the store sits under it: it also holds the main-session cache. The
    provider-version and model-catalog caches live in the per-user cache directory, not
    under the data directory, and fail silently, so they need no grant; the data
    directory's `.env` (a secret source) stays out of the sandbox's reach.
    """
    from pathlib import Path

    from exoma.config.settings import STORAGE_DIR

    job_dir = Path(job_dir)
    root = STORAGE_DIR if job_dir == STORAGE_DIR or STORAGE_DIR in job_dir.parents else job_dir
    text = str(root)
    quoted = f"'{text}'" if "'" not in text else '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    snippet = f"[sandbox_workspace_write]\nwritable_roots = [{quoted}]"
    next_action = (
        f"The job store {job_dir} is not writable from this process. Grant the host's sandbox "
        f"write access to {text} (Codex: run `exoma install` again with codex among the chosen hosts, "
        f"which writes the grant, or add {quoted} to `writable_roots` under `[sandbox_workspace_write]` in "
        "~/.codex/config.toml yourself), or start the host with EXOMA_HOME set to a directory it "
        "can write. Then retry."
    )
    return {"writable_root": text, "snippet": snippet, "next_action": next_action}


def submit(
    command: str,
    task: str,
    session_id: str,
    work_dir: str | None = None,
    model: str | None = None,
    allow_reuse: bool = True,
    *,
    on_admission=None,
    expected_job=None,
) -> dict:
    normalized_command = command.strip().lower()
    # Every job, and so every worker spawn (_spawn_worker is reached only from here), goes
    # through submit: the CLI's submit/await, `exoma check --wait` reclaiming a pending
    # reservation, recovery. One refusal here covers callers that skip cli.main's parser.
    refusal = _main()._recursion_refusal("submit", normalized_command)
    if refusal is not None:
        return refusal
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
            try:
                _main().JOB_MANAGER.assert_update_admission()
            except ValueError as exc:
                return make_error("runtime_lock", str(exc),
                                  next_action="Use the update operation observer; do not start another provider.")
            active = _main().JOB_MANAGER.active_job_for_session(session_id)
            if expected_job and (not active or active["job_id"] != expected_job):
                return _gate_error("The original job is no longer active. Collect its existing result.")
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

                if on_admission:
                    try:
                        on_admission(active)
                    except _SelectionChanged as exc:
                        return _gate_error(str(exc))
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
                    if on_admission:
                        try:
                            on_admission(job)
                        except _SelectionChanged as exc:
                            _main().JOB_MANAGER.fail_job(job["job_id"], str(exc))
                            return _gate_error(str(exc))
                        except OSError as exc:
                            # Nobody can reclaim this reservation before the capacity guard
                            # is released. Leave the pending test request for the next job.
                            _main().JOB_MANAGER.fail_job(job["job_id"], f"cannot bind delegation: {exc}")
                            raise
                    if normalized_command == "verify":
                        # A new job, not a recovery or an attach: the test request is this
                        # job's from here on, and an earlier job's leftover is dropped.
                        try:
                            verify_tests.claim(detect_project_root(work_dir), session_id)
                        except OSError as exc:
                            _main().JOB_MANAGER.fail_job(job["job_id"], f"cannot claim test request: {exc}")
                            raise
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
        # A write refused inside the job store (or the storage root it sits in), whatever the
        # error's type: a host sandbox raises PermissionError on one platform and a plain
        # OSError (EACCES, EPERM, EROFS, a Windows access error) on another, and only the first
        # used to get the fix. An error that names a path outside the store (the project's
        # verify request, say) is not the store's: it is reported as that path's.
        # Only a permission errno is a refusal a sandbox grant fixes. `write`, `fsync` and
        # `fdopen` raise without a filename, so a full disk or an I/O error in the store
        # (ENOSPC, EIO) used to arrive here with grant advice that could not help.
        job_dir = _main().JOB_MANAGER.job_dir
        fix = job_store_fix(job_dir)
        path = getattr(exc, "filename", None)
        meta = {"command": normalized_command, "error": type(exc).__name__, "path": path}
        in_store = path is None or _within(path, fix["writable_root"]) or _within(path, job_dir)
        refused = isinstance(exc, PermissionError) or exc.errno in _REFUSAL_ERRNOS
        if in_store and refused:
            meta.update(
                reason="job_store_not_writable",
                job_store=str(job_dir),
                writable_root=fix["writable_root"],
                fix_snippet=fix["snippet"],
            )
            next_action = fix["next_action"]
        elif in_store:
            meta.update(reason="job_store_write_failed", job_store=str(job_dir), errno=exc.errno)
            next_action = (
                f"Exoma could not write the job store {job_dir} ({exc.strerror or exc}). "
                "This is not a permission refusal: check free disk space and the device, then retry."
            )
        else:
            meta["reason"] = "write_refused"
            next_action = f"Exoma could not write {path} ({exc.strerror or exc}). Make it writable, then retry."
        return make_error(
            "job_submit_error",
            str(exc),
            next_action=next_action,
            meta=meta,
        )

    try:
        worker = _main()._spawn_worker(job["job_id"], work_dir)
        if not worker["ok"]:
            _main().JOB_MANAGER.fail_job(job["job_id"], worker["content"])
            return worker
        _main().JOB_MANAGER.set_launcher_pid(job["job_id"], worker["meta"]["pid"])
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
            observed=job,
        )

    # The probe is a provider call. A second_agent process (`exoma check --wait` run by a
    # marked child) must not start one: keep waiting and leave reaping to an unmarked
    # caller. `_recursion_refusal` is the shared marker test; "await" is a refused command.
    if _main()._recursion_refusal("await") is not None:
        return None

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
            observed=job,
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
            observed=job,
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
        observed=job,
    )


class _SelectionChanged(ValueError):
    """The host advanced its selected request before admission."""


def _gate_error(message):
    return make_error("session_state_error", message, next_action="Collect or report the current request in its original session.")


def _with_generation(result, generation):
    """Expose the recovery handle independently of any terminal receipt."""
    return {**result, "meta": {**(result.get("meta") or {}), "generation": generation}}


def _clear_armed_marker(rt, generation):
    """An overlapping Windows reader may briefly deny delete sharing."""
    marker = rt / "delegated.marker"
    try:
        for delay in REPLACE_RETRY_DELAYS:
            try:
                marker.unlink(missing_ok=True)
                return
            except PermissionError:
                time.sleep(delay)
        marker.unlink(missing_ok=True)
    except OSError as exc:
        return _with_generation(_gate_error(f"Could not clear the armed marker: {exc}. Retry in this session."), generation)


def _owned_job(state, session, root):
    job_id = state.get("job_id")
    job = _main().JOB_MANAGER.get_job(job_id) if job_id else None
    if (job and job.get("session_id") == session and job.get("command") == state["command"]
            and job.get("request_hash") == state.get("request_hash") and job.get("work_dir")
            and Path(job["work_dir"]).resolve() == Path(root)):
        return job
    return None


def _result_identity(result):
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def delegation_control(action, argument, session, work_dir):
    """Host-facing controls; the state path and job ownership come from the bound session."""
    refusal = _main()._recursion_refusal(action)
    if refusal:
        return refusal
    root = str(Path(work_dir).resolve())
    rt = Path(gate.runtime_dir(root, session))
    path = rt / "delegation.json"
    if action == "delegate-cancel":
        with gate.execution_transaction(rt):
            current = gate.collection_state(rt)
            if (current.get("phase") != "running" or current.get("generation") != argument
                    or not _owned_job(current, session, root)):
                return _gate_error("Cancellation requires the matching admitted request.")
            job_id = current["job_id"]
        # Cancellation can wait for worker cleanup. Never hold the host state lock while waiting.
        return _main().JOB_MANAGER.cancel(job_id, session, root)
    with gate.execution_transaction(rt):
        state = gate.collection_state(rt)
        phase = state.get("phase")
        active = phase not in {None, "collected", "aborted"}
        if action == "delegate-select":
            if argument not in _main().BACKGROUND_COMMANDS:
                return _gate_error("Select an evidence command.")
            marker = gate.delegation_marker(rt)
            if marker and marker.get("command") != argument:
                return _gate_error("A different delegation is pending.")
            if not active:
                state = {"command": argument, "generation": secrets.token_hex(16), "phase": "selected",
                         "set_at": now_iso(), "session": session, "root": root}
                atomic_write_json(path, state)
            cleanup_error = _clear_armed_marker(rt, state["generation"])
            if cleanup_error:
                return cleanup_error
            return {"ok": True, "meta": {"protocol": 1, **{key: state[key] for key in ("generation", "command", "phase")}}}
        if state.get("session") != session or state.get("root") != root:
            return _gate_error("No matching delegation for this project and session.")
        if action == "delegate-ack":
            if not argument or phase not in {"returned", "collected"} or argument != state.get("receipt"):
                return _gate_error("Receipt does not match the current terminal result.")
            if phase == "returned":
                job = _owned_job(state, session, root)
                if (not job or job["status"] not in {"completed", "failed"}
                        or _result_identity(_main().JOB_MANAGER.result_snapshot(job)) != state["result_digest"]):
                    return _gate_error("The terminal result changed. Collect it again before acknowledgement.")
                cleanup_error = _clear_armed_marker(rt, state["generation"])
                if cleanup_error:
                    return cleanup_error
                atomic_write_json(path, {**state, "phase": "collected"})
            return {"ok": True, "meta": {"phase": "collected", "job_id": state["job_id"]}}
        if not active or argument != state.get("generation"):
            return _gate_error("Request generation does not match the active selection.")
        if action == "delegate-abort":
            if phase != "selected" or state.get("job_id"):
                return _gate_error("Abort is only available before job admission.")
            cleanup_error = _clear_armed_marker(rt, argument)
            if cleanup_error:
                return cleanup_error
            atomic_write_json(path, {**state, "phase": "aborted"})
            return {"ok": True, "meta": {"phase": "aborted"}}
        job_id = state.get("job_id")
        job = _owned_job(state, session, root)
        if not job:
            return _gate_error("No owned job result is available for this request.")
    # Publication takes the job guard and then the execution guard. Collect without
    # the host guard, then fence the result against the current selection.
    result = get_result(job_id)
    with gate.execution_transaction(rt):
        current = gate.collection_state(rt)
        job = _owned_job(current, session, root) if current.get("job_id") == job_id else None
        if (not job or current.get("root") != root or current.get("session") != session
                or current.get("generation") != argument or current.get("phase") not in {"running", "returned"}
                or current.get("request_hash") != state.get("request_hash") or current.get("command") != state["command"]):
            return _gate_error("Selection changed during result collection.")
        state = current
        status = result.get("status")
        if status not in {"completed", "failed"} or (result.get("meta") or {}).get("error_type") in {
                "cleanup_pending", "cancellation_pending"}:
            return {**result, "ok": False, "meta": {**(result.get("meta") or {}), "job_id": job_id, "phase": "running"}}
        identity = _result_identity(result)
        if job["status"] != status or _result_identity(_main().JOB_MANAGER.result_snapshot(job)) != identity:
            return _gate_error("Terminal result changed during collection.")
        output = result.get("output") if status == "completed" else result
        if not isinstance(output, dict) or not isinstance(output.get("ok"), bool):
            output = _gate_error("Job returned an invalid terminal payload.")
        output = _finalize_verify_result(state["command"], dict(output))
        receipt = state.get("receipt")
        if not receipt or state.get("result_digest") != identity:
            receipt = secrets.token_hex(24)
            atomic_write_json(path, {**state, "phase": "returned", "receipt": receipt, "result_digest": identity})
        return {**output, "meta": {**(output.get("meta") or {}), "job_id": job_id,
                "submitted_at": job["created_at"], "worker_pid": job.get("worker_pid"),
                "delegation": {"generation": argument, "receipt": receipt, "command": state["command"]}}}


def _submit_selected(command, task, session, work_dir, model, allow_reuse, generation):
    rt = Path(gate.runtime_dir(work_dir, session))
    path = rt / "delegation.json"
    with gate.execution_transaction(rt):
        if not generation:
            marker = gate.delegation_marker(rt)
            if marker and marker.get("phase") not in {None, "armed"}:
                error = _gate_error("Recover the pending generation with delegate-select for the same session and command.")
                if marker.get("session") == session and marker.get("command") == command and marker.get("root") == str(Path(work_dir).resolve()):
                    return _with_generation(error, marker["generation"])
                return error
            selected = delegation_control("delegate-select", command, session, work_dir)
            if not selected["ok"]:
                return selected
            generation = selected["meta"]["generation"]
        state = gate.collection_state(rt)
        if (state.get("session") != session or state.get("root") != str(Path(work_dir).resolve())
                or state.get("generation") != generation or state.get("command") != command
                or state.get("phase") not in {"selected", "running", "returned"}):
            return _gate_error("Dispatch requires the current selected request.")
        manager = _main().JOB_MANAGER
        request_hash = manager.request_hash(command, task, session, work_dir, allow_reuse, model)
        if state.get("request_hash", request_hash) != request_hash:
            return _with_generation(_gate_error("An existing selection is already bound to another task."), generation)
        original = state.get("job_id")
        root = str(Path(work_dir).resolve())
        if original and not _owned_job(state, session, root):
            return _with_generation(_gate_error("The admitted job is unavailable."), generation)

    def matches(current):
        return (current.get("generation") == generation and current.get("command") == command
                and current.get("session") == session and current.get("root") == root
                and current.get("request_hash", request_hash) == request_hash
                and current.get("phase") in {"selected", "running", "returned"})

    if original:
        result = get_result(original)
        with gate.execution_transaction(rt):
            current = gate.collection_state(rt)
            record = _owned_job(current, session, root) if current.get("job_id") == original else None
            if not matches(current) or not record:
                return _with_generation(_gate_error("The admitted selection changed during redispatch."), generation)
            meta = result.get("meta") or {}
            if meta.get("error_type") != "worker_died" or not meta.get("recoverable"):
                return {"ok": True, "job_id": original, "submitted_at": record["created_at"], "generation": generation}

    def bind(job):
        with gate.execution_transaction(rt):
            current = gate.collection_state(rt)
            if not matches(current) or current.get("job_id") not in (None, job["job_id"]):
                raise _SelectionChanged("Selection changed before job admission.")
            if current.get("job_id") is None:
                atomic_write_json(path, {**current, "phase": "running", "job_id": job["job_id"], "request_hash": request_hash})

    submitted = submit(command, task, session, work_dir, model, allow_reuse,
                       on_admission=bind, expected_job=original)
    with gate.execution_transaction(rt):
        current = gate.collection_state(rt)
        if not matches(current):
            return _with_generation(_gate_error("Selected request changed during admission."), generation)
        if not submitted["ok"]:
            # Only this validated dispatch can recover an admitted job that reached a
            # terminal state during submit (for example a failed worker launch).
            job = _owned_job(current, session, root)
            if not job or job.get("request_hash") != request_hash or job["status"] not in {"completed", "failed"}:
                return _with_generation(submitted, generation)
            submitted = {"ok": True, "job_id": job["job_id"], "submitted_at": job["created_at"]}
        elif current.get("job_id") != submitted.get("job_id"):
            return _with_generation(_gate_error("The admitted job changed."), generation)
        return {**submitted, "generation": generation}


def await_job(
    command: str,
    task: str,
    session_id: str,
    work_dir: str | None = None,
    model: str | None = None,
    poll_interval: float = DEFAULT_JOB_POLL_INTERVAL_SECONDS,
    poll_timeout: int = DEFAULT_JOB_POLL_TIMEOUT_SECONDS,
    allow_reuse: bool = True,
    generation: str | None = None,
) -> dict:
    if command.strip().lower() == "sweep":
        return _main().run("sweep", task, session_id, work_dir, model)

    _apply_job_thresholds(work_dir)
    _main()._warn_if_workspace_stale(work_dir)
    rt = gate.runtime_dir(work_dir or os.getcwd(), session_id)
    if generation or gate.bound_delegation_host(rt) or gate.delegation_marker(rt):
        work_dir = work_dir or os.getcwd()
        submitted = _submit_selected(command, task, session_id, work_dir, model, allow_reuse, generation)
        generation = submitted.get("generation")
    else:
        submitted = submit(command, task, session_id, work_dir, model, allow_reuse)
    if not submitted.get("ok"):
        return submitted
    result = _await_submitted(submitted, command, task, session_id, work_dir, model, allow_reuse,
                             generation, poll_interval, poll_timeout)
    return _with_generation(result, generation) if generation else result


def _await_submitted(submitted, command, task, session_id, work_dir, model, allow_reuse,
                    generation, poll_interval, poll_timeout):
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
    last_notice_at, last_diagnostic = started_at, None

    while True:
        result = get_result(job_id)
        if generation and result.get("status") in {"completed", "failed"}:
            return delegation_control("delegate-result", generation, session_id, work_dir)
        if result.get("status") in {"pending", "running", "recovering"}:
            # Keep stdout a single final JSON result. Bounded stderr notices expose
            # observed diagnosis while awaiting the SAME worker; no extra probe here.
            try:
                import json
                from exoma.utils.redact import redact_value
                progress = (_main().JOB_MANAGER.read_heartbeat(job_id) or {}).get("progress") or {}
                notice_at = time.monotonic()
                diagnostic = progress.get("diagnostic")
                if (diagnostic and diagnostic != last_diagnostic) or notice_at - last_notice_at >= 30:
                    notice = redact_value({"job_id": job_id, "status": result["status"],
                        "elapsed_seconds": round(notice_at - started_at, 1),
                        **{key: progress.get(key) for key in ("phase", "idle_seconds", "work_idle_seconds",
                            "work_events", "diagnostic", "retry_count", "diagnostics")}})[0]
                    print("[JOB] " + json.dumps(notice), file=sys.stderr, flush=True)
                    last_notice_at, last_diagnostic = notice_at, diagnostic
            except (OSError, ValueError, TypeError, AttributeError):
                pass
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
        if ((result.get("meta") or {}).get("error_type") in {"cleanup_pending", "cancellation_pending"}
                or (result.get("meta") or {}).get("reason") == "verify_checkpoint_pending"):
            return {"ok": False, "content": result.get("content"),
                    "meta": {**(result.get("meta") or {}), "job_id": job_id,
                             "submitted_at": submitted.get("submitted_at")}}
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
                    if generation:
                        return delegation_control("delegate-result", generation, session_id, work_dir)
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
                    command, task, session_id, work_dir, model, allow_reuse, expected_job=job_id
                )
                if not resumed.get("ok"):
                    if generation:
                        # The worker can finish between the pending snapshot and the
                        # capacity guard. Only collect this selection's owned result.
                        terminal = delegation_control("delegate-result", generation, session_id, work_dir)
                        if (terminal.get("meta") or {}).get("delegation", {}).get("receipt"):
                            return terminal
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

    def _beat(progress: dict) -> None:
        if (_main().JOB_MANAGER.get_job(job_id) or {}).get("cancellation"):
            raise JobCancellation()
        _main().JOB_MANAGER.touch_heartbeat(job_id, progress)

    owner = None
    try:
        # Registration can fail on the same storage faults as the call itself.
        _main().JOB_MANAGER.set_worker_pid(job_id, os.getpid())
        running = _main().JOB_MANAGER.mark_running(job_id)
        if running.get("status") != "running":
            return make_error(
                "worker_died",
                f"job {job_id} became terminal before its worker started",
                next_action="Inspect the terminal job result; do not restart this worker.",
                meta={"job_id": job_id, "status": running.get("status")},
            )
        if os.environ.get("WORKFLOW_JOB_WORKER") == job_id:
            owner = WorkerOwnership()  # Before any provider/bootstrap child can start.
            _main().JOB_MANAGER.begin_worker_ownership(job_id)
        _beat({"phase": "starting", "elapsed_seconds": 0})
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
            if owner:
                owner.cleanup()
                _main().JOB_MANAGER.confirm_worker_cleanup(job_id)
            _main().JOB_MANAGER.fail_job(job_id, refused["content"], output=refused)
            return refused
        if require_provider_session:
            effective_task = (
                f"Continue the interrupted task for job {job_id}.\n"
                f"Original task: {job['task']}\n"
                "Inspect the existing session state. Resume unfinished work only; "
                "do not repeat completed findings. Return the normal evidence contract."
            )

        pin = job.get("verify_round")
        if pin and "verify_checkpoint" in job:
            output = job["verify_checkpoint"]["output"]
        else:
            admitted = pin
            if "verify_round" in job and pin is None:
                admitted = {"standalone": True}
            with verify_tests.job(job_id), verify_followup.using(admitted):
                output = _main().run(
                    job["command"], effective_task, job["session_id"], job.get("work_dir"),
                    job.get("model"), on_progress=_beat,
                    allow_reuse=bool(job.get("allow_reuse", True)),
                    require_provider_session=require_provider_session,
                )
        _beat({"phase": "finishing"})
        if owner:
            try:
                owner.cleanup()
            except Exception as cleanup_exc:
                return _main().JOB_MANAGER.retain_worker_cleanup(job_id, str(cleanup_exc))
            _main().JOB_MANAGER.confirm_worker_cleanup(job_id)
        output.setdefault("meta", {}).setdefault("job_id", job_id)
        if pin:
            finished = _main().JOB_MANAGER.complete_verify_round(job_id, job.get("recovery_attempt", 0), output)
            if finished.get("status") in ("completed", "failed") and finished.get("output"):
                return finished["output"]
            if not finished.get("cancellation"):
                return make_error("runtime_lock", "verification attempt lost publication ownership",
                                  next_action="Collect the result of the current attempt.")
        elif output.get("ok"):
            finished = _main().JOB_MANAGER.complete_job(job_id, output)
            if job.get("command") == "verify":
                try:
                    verify_tests.clear_job_checkpoint(detect_project_root(job.get("work_dir")), job["session_id"], job_id)
                except ValueError:
                    pass
        else:
            finished = _main().JOB_MANAGER.fail_job(
                job_id, output.get("content") or "worker failed", output=output
            )
            if job.get("command") == "verify":
                try:
                    verify_tests.clear_job_checkpoint(detect_project_root(job.get("work_dir")), job["session_id"], job_id)
                except ValueError:
                    pass
        if isinstance(finished, dict) and finished.get("cancellation"):
            raise JobCancellation()
        return output
    except JobCancellation:
        if job.get("command") == "verify":
            try:
                verify_tests.clear_job_checkpoint(detect_project_root(job.get("work_dir")), job["session_id"], job_id)
            except ValueError:
                pass
        cleanup = {"ok": False, "reason": "worker_ownership_unavailable"}
        if owner:
            try:
                owner.cleanup()
                _main().JOB_MANAGER.confirm_worker_cleanup(job_id)
                cleanup = {"ok": True, "reason": "worker_owned_descendants_stopped"}
            except Exception as exc:
                cleanup = {"ok": False, "reason": str(exc)}
                _main().JOB_MANAGER.retain_worker_cleanup(job_id, str(exc))
        return _main().JOB_MANAGER.acknowledge_cancellation(job_id, cleanup)
    except BaseException as exc:
        if owner:
            try:
                owner.cleanup()
                _main().JOB_MANAGER.confirm_worker_cleanup(job_id)
            except Exception as cleanup_exc:
                return _main().JOB_MANAGER.retain_worker_cleanup(job_id, str(cleanup_exc))
        # Guarded: this is the worker's last line. If recording the failure raises too (the
        # same disk that failed the run), letting it escape would lose the crash report the
        # caller is about to get. fail_job releases the session lock itself, even when its
        # own save fails, so a raise here no longer strands the lock.
        # Through make_error like every other failure: a plain dict here carried no
        # error_type and no next_action, so a caller branching on error_type saw None and
        # treated a crashed worker as an unknown shape rather than a known failure.
        failure = make_error(
            "worker_crashed",
            str(exc),
            next_action="inspect the worker log for this job_id, then resubmit",
        )
        if job.get("verify_round") and (_main().JOB_MANAGER.get_job(job_id) or {}).get("verify_checkpoint"):
            failure["meta"]["reason"] = "verify_checkpoint_pending"
            return failure
        failure["job_id"] = job_id
        failure["status"] = "failed"
        failure.setdefault("meta", {})["job_id"] = job_id
        # The same typed result is what the job record keeps: a reader attaching later
        # must see `worker_crashed`, not a bare message the direct caller never got.
        try:
            _main().JOB_MANAGER.fail_job(job_id, str(exc), output=dict(failure, meta=dict(failure["meta"])))
        except Exception as fail_exc:  # noqa: BLE001 — reported in the failure meta
            failure["meta"]["job_record_error"] = f"{type(fail_exc).__name__}: {fail_exc}"
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
    env["WORKFLOW_JOB_WORKER"] = job_id
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(PACKAGE_PARENT), env.get("PYTHONPATH", "")) if part
    )
    log_path = _main().JOB_MANAGER.log_path(job_id)
    log_handle = _main().subprocess.DEVNULL
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "a", encoding="utf-8", errors="replace")
        log_handle.write(f"\n--- worker spawn {now_iso()} ---\n")
        log_handle.flush()
    except OSError:
        if log_handle is not _main().subprocess.DEVNULL:
            try:
                log_handle.close()
            except OSError:
                pass
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
