"""Update the installed Exoma package, global bundle, and current workspace."""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import sysconfig
import subprocess
import time
import uuid
from pathlib import Path

from exoma.installer import method as install_method
from exoma.utils.osutil import agent_home, run_bounded

STAGE_TIMEOUT_SECONDS = 900
HEARTBEAT_SECONDS = 15
IS_WINDOWS = os.name == "nt"


def _workspace_root(cwd: Path) -> Path | None:
    incomplete = None
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".workflow" / "config.json").exists():
            return candidate
        if incomplete is None and (candidate / ".workflow").is_dir():
            incomplete = candidate
    return incomplete


_uv_tool = install_method.uv_tool

def _externally_managed() -> bool:
    """Whether pip would refuse this environment as distribution-managed (PEP 668).

    The marker is an `EXTERNALLY-MANAGED` file in the stdlib directory, and it applies only
    outside a virtual environment. Read from the file rather than from pip's stderr, so pip
    keeps printing to the terminal as it runs.
    """
    if sys.prefix != sys.base_prefix:
        return False
    try:
        return (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").is_file()
    except (OSError, KeyError, TypeError):
        return False


def _package_command(manager: str) -> tuple[str, list[str]]:
    uv = _uv_tool() if manager in ("auto", "uv") else None
    if manager == "uv" and not uv:
        raise ValueError("this Exoma interpreter is not in the uv tool directory; choose its actual package manager")
    if uv:
        return "uv", [uv, "tool", "upgrade", "exoma"]
    pipx_env = install_method.pipx_venv() if manager in ("auto", "pipx") else False
    if manager == "pipx" and not pipx_env:
        raise ValueError("this Exoma interpreter is not in a pipx environment; choose its actual package manager")
    if pipx_env:
        pipx = shutil.which("pipx")
        if not pipx:
            raise ValueError("this Exoma was installed with pipx, but pipx is not on PATH; "
                             "run `pipx upgrade exoma` where pipx is available")
        return "pipx", [pipx, "upgrade", "exoma"]
    return "pip", [sys.executable, "-m", "pip", "install", "--upgrade", "exoma"]


def _run_step(label: str, argv: list[str], cwd: Path, *, pip: bool = False,
              timeout: float = STAGE_TIMEOUT_SECONDS) -> bool:
    print(f"[UPDATE] {label}: {' '.join(argv)}", flush=True)
    started = time.monotonic()

    def waiting(elapsed: float) -> None:
        print(f"[UPDATE] {label}: still waiting; elapsed {elapsed:.1f}s "
              f"(limit {timeout:g}s)", file=sys.stderr, flush=True)

    try:
        code, _out, _err = run_bounded(
            argv, timeout, cwd=str(cwd), inherit_output=True,
            on_tick=waiting, tick_seconds=HEARTBEAT_SECONDS,
        )
    except KeyboardInterrupt:
        print(f"[UPDATE] {label} interrupted after {time.monotonic() - started:.1f}s; "
              "later steps skipped", file=sys.stderr, flush=True)
        raise
    except OSError as exc:
        print(f"[UPDATE] {label} failed: {exc}", file=sys.stderr, flush=True)
        return False
    if code is None:
        print(f"[UPDATE] {label} timed out after {timeout:g}s; process-tree cleanup attempted; "
              "later steps skipped. Cleanup can take additional time.", file=sys.stderr, flush=True)
        return False
    if code:
        print(f"[UPDATE] {label} failed (exit {code}); later steps skipped", file=sys.stderr, flush=True)
        if pip and _externally_managed():
            print("[UPDATE] this Python is managed by the OS (PEP 668), so pip may not change it. "
                  "Install Exoma with pipx (`pipx install exoma`) or uv (`uv tool install exoma`) "
                  "instead; `exoma update` then upgrades through that manager.", file=sys.stderr)
        return False
    print(f"[UPDATE] {label} completed in {time.monotonic() - started:.1f}s", flush=True)
    return True


def _receipt_names() -> set[str]:
    """Install receipts that existed before this update started."""
    root = agent_home() / ".claude" / "backups"
    try:
        return {path.parent.name for path in root.glob("install_*/install_receipt.json")}
    except OSError:
        return set()


def _report_failed_refresh(stage: str, workspace: Path | None, before: set[str]) -> None:
    print(f"[UPDATE] package step succeeded, but {stage} failed; the update is incomplete", file=sys.stderr)
    for name in sorted(_receipt_names() - before):
        print(f"[UPDATE] global install receipt: {agent_home() / '.claude' / 'backups' / name / 'install_receipt.json'}",
              file=sys.stderr)
        if re.fullmatch(r"install_[A-Za-z0-9_-]+", name):
            print(f"[UPDATE] to undo its recorded global writes: exoma install --rollback {name} --apply",
                  file=sys.stderr)
    print("[UPDATE] repair the reported failure, then run exoma install and exoma install --check",
          file=sys.stderr)
    if workspace:
        print(f"[UPDATE] workspace recovery, if needed: exoma upgrade --work-dir {workspace}",
              file=sys.stderr)


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive finite number of seconds") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number of seconds")
    return seconds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exoma update", description="Update Exoma and refresh its installed config and current workspace")
    parser.add_argument("--manager", choices=("auto", "uv", "pipx", "pip"), default="auto",
                        help="package manager for this Exoma installation "
                             "(default: detect uv tool, then pipx, otherwise pip)")
    parser.add_argument("--timeout", type=_positive_seconds, default=STAGE_TIMEOUT_SECONDS,
                        metavar="SECONDS", help="deadline for each package, refresh and check step "
                        f"(default: {STAGE_TIMEOUT_SECONDS}s; cleanup can take additional time)")
    queries = parser.add_mutually_exclusive_group()
    queries.add_argument("--check", action="store_true", help="query installed/latest public version; advisory, no upgrade")
    queries.add_argument("--status", action="store_true", help="read the last update admitted by preflight on this installation")
    args = parser.parse_args(argv)
    try:
        if args.check:
            from exoma.update_metadata import check_release
            try:
                print(json.dumps(check_release(), indent=2))
                return 0
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(f"[UPDATE] release query failed: {exc}", file=sys.stderr)
                return 1
        if args.status:
            return _status()
        return _update(args)
    except KeyboardInterrupt:
        print("[UPDATE] interrupted; inspect the last reported stage and installed version before retrying",
              file=sys.stderr, flush=True)
        return 130


def _update(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    print("[UPDATE] preflight: checking workspace and active jobs", flush=True)
    workspace = _workspace_root(cwd)
    if workspace:
        config = workspace / ".workflow" / "config.json"
        if not config.is_file():
            print(f"[UPDATE] {workspace} has .workflow/ but no config.json file; "
                  "restore the workspace config before updating", file=sys.stderr)
            return 1
        from exoma.core.workspace.workspace_paths import read_json_file

        try:
            workspace_config = read_json_file(config)
        except (OSError, ValueError) as exc:
            print(f"[UPDATE] invalid workspace config {config}: {exc}; package unchanged", file=sys.stderr)
            return 1
        if "runtime" in workspace_config and not isinstance(workspace_config["runtime"], dict):
            print(f"[UPDATE] workspace config runtime must be an object: {config}; package unchanged",
                  file=sys.stderr)
            return 1
        from exoma.core.runtime.upgrade import active_jobs_for_workspace

        active = active_jobs_for_workspace(workspace)
        if active:
            print(f"[UPDATE] {len(active)} active job(s) in {workspace}; finish them before updating",
                  file=sys.stderr)
            return 1
    print(f"[UPDATE] detecting package manager ({args.manager}; "
          f"uv detection limit {install_method.DETECTION_TIMEOUT_SECONDS}s)", flush=True)
    try:
        manager, package = _package_command(args.manager)
    except (OSError, ValueError) as exc:
        print(f"[UPDATE] cannot choose package manager: {exc}", file=sys.stderr)
        return 2

    print(f"[UPDATE] manager: {manager}; workspace: {workspace or 'none'}", flush=True)
    return _handoff(manager, package, cwd, args.timeout)


def _report_package_uncertain(manager: str) -> None:
    print(f"[UPDATE] {manager} did not complete successfully; package state is uncertain. "
          "Check its output and installed version before retrying with that manager. "
          "Global configuration and workspace refresh were not started.", file=sys.stderr, flush=True)


def _update_root() -> Path:
    from exoma.config.settings import DATA_DIR

    return DATA_DIR / "updates"


def _status() -> int:
    """Read only: no directory creation, lock acquisition, manager spawn or repair."""
    from exoma import _update_worker as worker
    try:
        api = worker.platform_api()
        key = worker.installation_key(sys.prefix, api.user())
        root = _update_root() / key
        latest = worker.read_json(root / "latest.json")["operation_id"]
        if not isinstance(latest, str) or not re.fullmatch(r"[a-f0-9]{32}", latest):
            raise ValueError("invalid operation ID")
        state, code = worker.outcome(root / latest, key)
        state.update(status_scope="last_published_operation", status_notice=(
            "This is the last update admitted by preflight, not necessarily the last attempted command. "
            "Preflight refusals do not replace an active update. Inspect the refusing command's error and any printed log directory."))
        print(json.dumps(state, indent=2))
        return code
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"[UPDATE] status unavailable/unknown: {exc}", file=sys.stderr)
        return 4




def _wait_for_handshake(operation, name, token, proc, timeout):
    from exoma import _update_worker as worker

    def check_worker():
        if proc.poll() is not None:
            try:
                reason = worker.read_json(operation / "state.json").get("error", "worker exited")
            except (OSError, ValueError):
                reason = "worker exited; inspect worker.log"
            raise OSError(reason)

    worker.wait_for_token(operation / name, token, timeout, check_worker)


def _handoff(manager: str, package: list[str], cwd: Path, timeout: float) -> int:
    from exoma import _update_worker as worker
    from exoma.update_metadata import distribution_info

    committed = False
    operation = None
    proc = None
    reservation = None
    cwd = cwd.resolve()
    try:
        from exoma.core.jobs.job_manager import DEAD, JobManager

        jobs = JobManager()
        for path in jobs.job_dir.glob("job_*.json"):
            job = worker.read_json(path)
            if job.get("status") in ("pending", "running", "recovering") and jobs.liveness(job) != DEAD:
                raise ValueError("active Exoma jobs exist in this user store; finish them before updating")
        descriptor = install_method.update_descriptor()
        api = worker.platform_api()
        key = worker.installation_key(descriptor["prefix"], api.user())
        root = _update_root().resolve()
        if worker.inside(root, descriptor["prefix"]):
            raise ValueError("update state must be outside the target environment")
        targets = worker.callers(api, sys.executable, set(descriptor["entrypoints"]))
        manager_path = shutil.which(package[0]) or package[0]
        # In a POSIX venv python is commonly a symlink. Resolving it would make
        # `-m pip` mutate the base interpreter instead of the target environment.
        package = [os.path.abspath(manager_path), *package[1:]]
        if not Path(package[0]).is_file():
            raise ValueError("package manager executable is unavailable")
        if manager != "pip" and worker.inside(package[0], descriptor["prefix"]):
            raise ValueError("package manager must run outside the target environment")
        operation_id, token = uuid.uuid4().hex, uuid.uuid4().hex
        operation = root / key / operation_id
        operation.mkdir(parents=True)
        snapshot = Path(worker.__file__).read_bytes()
        script = operation / "worker.py"
        script.write_bytes(snapshot)
        reservation = jobs.reserve_update(operation, token)
        request = {"schema_version": worker.SCHEMA, "operation_id": operation_id,
                   "installation_key": key, "token": token, "prefix": descriptor["prefix"],
                   "python_relative": descriptor["python_relative"], "package": package,
                   "cwd": str(cwd), "timeout": timeout, "callers": targets,
                   "worker_sha256": hashlib.sha256(snapshot).hexdigest(),
                   "version_before": distribution_info()["installed"],
                   "receipt_root": str(agent_home() / ".claude" / "backups"),
                   "receipts_before": sorted(_receipt_names()),
                   "workspace": str(_workspace_root(cwd)) if _workspace_root(cwd) else None,
                   "reservation": str(reservation), "runtime": descriptor["runtime"],
                   "caller_job": api.job_snapshot() if os.name == "nt" else {"session": os.getsid(0)}}
        worker.atomic_json(operation / "request.json", request)
        # This worker must survive the caller. Shared bounded-spawn cleanup is unsuitable.
        launch = {"start_new_session": True}
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            if api.in_job():
                flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
            launch = {"creationflags": flags}
        worker.atomic_json(operation / "launch.json", {"options": launch, "runtime": descriptor["runtime"], "caller_job": request["caller_job"]})
        with (operation / "worker.log").open("ab") as log:
            launch_env = os.environ.copy()
            if launch_env.get("EXOMA_HOME"):
                launch_env["EXOMA_HOME"] = str(Path(launch_env["EXOMA_HOME"]).resolve())
            proc = subprocess.Popen([descriptor["runtime"], "-I", "-S", "-B", str(script), str(operation)],
                                    cwd=operation, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    env=launch_env, close_fds=True, **launch)

        _wait_for_handshake(operation, "ready.json", token, proc, worker.HANDSHAKE_SECONDS)
        # Mark ambiguous BEFORE writing COMMIT: replacement may succeed before fsync fails.
        committed = True
        worker.atomic_json(operation / "commit.json", {"token": token})
        _wait_for_handshake(operation, "accepted.json", token, proc, worker.HANDSHAKE_SECONDS)
        print(f"[UPDATE] accepted; not complete. Operation: {operation_id}", flush=True)
        print(f"[UPDATE] state: {operation / 'state.json'}", flush=True)
        print(f"[UPDATE] logs: {operation}", flush=True)
        observer = [descriptor["runtime"], "-I", "-S", "-B", str(script), "--observe", str(operation), "--wait"]
        print(json.dumps({"operation_id": operation_id, "accepted": True, "completed": False,
                          "observer_argv": observer, "cancel_argv": [*observer, "--cancel"]}), flush=True)
        print("[UPDATE] let this launcher exit. Watch state/log files directly during upgrade; "
              "afterward use exoma update --status (0 succeeded, 1 failed, 3 running, 4 unknown).", flush=True)
        return 3
    except (OSError, ValueError, KeyError, TypeError, KeyboardInterrupt) as exc:
        if reservation and not committed and (proc is None or proc.poll() is not None):
            try:
                if worker.read_json(reservation).get("token") == token:
                    reservation.unlink()
            except FileNotFoundError:
                pass
        outcome = "handoff outcome unknown; inspect state before retrying" if committed else "handoff refused before package mutation"
        print(f"[UPDATE] {outcome}: {exc}", file=sys.stderr, flush=True)
        if operation:
            print(f"[UPDATE] logs/state: {operation}", file=sys.stderr)
            if not committed:
                print("[UPDATE] --status reports the last update admitted by preflight; "
                      "if this attempt was rejected before admission it may show an earlier result. "
                      "Inspect this attempt's log directory above.", file=sys.stderr)
        print("[UPDATE] after updater and manager children exit, run the installation manager directly, "
              "then exoma install --no-input and exoma install --check", file=sys.stderr)
        return (4 if committed else 130) if isinstance(exc, KeyboardInterrupt) else (4 if committed else 1)
