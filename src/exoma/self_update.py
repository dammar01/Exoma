"""Update the installed Exoma package, global bundle, and current workspace."""

import argparse
import math
import re
import shutil
import sys
import sysconfig
import time
from pathlib import Path

from exoma.installer import method as install_method
from exoma.utils.osutil import agent_home, run_bounded

STAGE_TIMEOUT_SECONDS = 900
HEARTBEAT_SECONDS = 15


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
    args = parser.parse_args(argv)
    try:
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
    receipts_before = _receipt_names()
    package_ok = False
    stage = "install"
    try:
        package_ok = _run_step("package", package, cwd, pip=manager == "pip", timeout=args.timeout)
        if not package_ok:
            _report_package_uncertain(manager)
            return 1

        fresh = [sys.executable, "-m", "exoma"]
        # Stored choices stand: the new installer must not prompt during an update.
        for stage, label, command in (
            ("install", "global config and current workspace", [*fresh, "install", "--no-input"]),
            ("check", "check", [*fresh, "install", "--check"]),
        ):
            if not _run_step(label, command, cwd, timeout=args.timeout):
                _report_failed_refresh(stage, workspace, receipts_before)
                return 1
        print("[UPDATE] complete", flush=True)
        return 0
    except KeyboardInterrupt:
        if package_ok:
            _report_failed_refresh(stage, workspace, receipts_before)
        else:
            _report_package_uncertain(manager)
        return 130


def _report_package_uncertain(manager: str) -> None:
    print(f"[UPDATE] {manager} did not complete successfully; package state is uncertain. "
          "Check its output and installed version before retrying with that manager. "
          "Global configuration and workspace refresh were not started.", file=sys.stderr, flush=True)
