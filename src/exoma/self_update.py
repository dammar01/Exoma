"""Update the installed Exoma package, global bundle, and current workspace."""

import argparse
import re
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

from exoma.installer import method as install_method
from exoma.utils.osutil import agent_home, hidden_run_kwargs


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


def _run_step(label: str, argv: list[str], cwd: Path, *, pip: bool = False) -> bool:
    print(f"[UPDATE] {label}: {' '.join(argv)}", flush=True)
    try:
        result = subprocess.run(argv, cwd=cwd, **hidden_run_kwargs())
    except OSError as exc:
        print(f"[UPDATE] {label} failed: {exc}", file=sys.stderr)
        return False
    if result.returncode:
        print(f"[UPDATE] {label} failed (exit {result.returncode}); later steps skipped", file=sys.stderr)
        if pip and _externally_managed():
            print("[UPDATE] this Python is managed by the OS (PEP 668), so pip may not change it. "
                  "Install Exoma with pipx (`pipx install exoma`) or uv (`uv tool install exoma`) "
                  "instead; `exoma update` then upgrades through that manager.", file=sys.stderr)
        return False
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exoma update", description="Update Exoma and refresh its installed config and current workspace")
    parser.add_argument("--manager", choices=("auto", "uv", "pipx", "pip"), default="auto",
                        help="package manager for this Exoma installation "
                             "(default: detect uv tool, then pipx, otherwise pip)")
    args = parser.parse_args(argv)
    cwd = Path.cwd().resolve()
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
    try:
        manager, package = _package_command(args.manager)
    except (OSError, ValueError) as exc:
        print(f"[UPDATE] cannot choose package manager: {exc}", file=sys.stderr)
        return 2

    print(f"[UPDATE] manager: {manager}; workspace: {workspace or 'none'}", flush=True)
    receipts_before = _receipt_names()
    if not _run_step("package", package, cwd, pip=manager == "pip"):
        return 1

    fresh = [sys.executable, "-m", "exoma"]
    # --no-input: an install from a terminal asks every choice, and an update is not the
    # place to re-choose them; the stored choices stand.
    if not _run_step("global config and current workspace", [*fresh, "install", "--no-input"], cwd):
        _report_failed_refresh("install", workspace, receipts_before)
        return 1
    if not _run_step("check", [*fresh, "install", "--check"], cwd):
        _report_failed_refresh("check", workspace, receipts_before)
        return 1
    print("[UPDATE] complete", flush=True)
    return 0
