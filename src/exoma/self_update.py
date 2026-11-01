"""Update the installed Exoma package, global bundle, and current workspace."""

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

from exoma.utils.osutil import agent_home, hidden_run_kwargs


def _workspace_root(cwd: Path) -> Path | None:
    incomplete = None
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".workflow" / "config.json").exists():
            return candidate
        if incomplete is None and (candidate / ".workflow").is_dir():
            incomplete = candidate
    return incomplete


def _uv_tool() -> str | None:
    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        result = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True,
                                **hidden_run_kwargs())
    except OSError:
        return None
    if result.returncode or not result.stdout.strip():
        return None
    tools_dir = Path(result.stdout.strip()).resolve()
    return uv if Path(sys.prefix).resolve() == (tools_dir / "exoma").resolve() else None


def _package_command(manager: str) -> tuple[str, list[str]]:
    uv = _uv_tool() if manager in ("auto", "uv") else None
    if manager == "uv" and not uv:
        raise ValueError("this Exoma interpreter is not in the uv tool directory; choose its actual package manager")
    if uv:
        return "uv", [uv, "tool", "upgrade", "exoma"]
    return "pip", [sys.executable, "-m", "pip", "install", "--upgrade", "exoma"]


def _run_step(label: str, argv: list[str], cwd: Path) -> bool:
    print(f"[UPDATE] {label}: {' '.join(argv)}", flush=True)
    try:
        result = subprocess.run(argv, cwd=cwd, **hidden_run_kwargs())
    except OSError as exc:
        print(f"[UPDATE] {label} failed: {exc}", file=sys.stderr)
        return False
    if result.returncode:
        print(f"[UPDATE] {label} failed (exit {result.returncode}); later steps skipped", file=sys.stderr)
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
    parser.add_argument("--manager", choices=("auto", "uv", "pip"), default="auto",
                        help="package manager for this Exoma installation (default: detect uv tool, otherwise pip)")
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
    if not _run_step("package", package, cwd):
        return 1

    fresh = [sys.executable, "-m", "exoma"]
    if not _run_step("global config and current workspace", [*fresh, "install"], cwd):
        _report_failed_refresh("install", workspace, receipts_before)
        return 1
    if not _run_step("check", [*fresh, "install", "--check"], cwd):
        _report_failed_refresh("check", workspace, receipts_before)
        return 1
    print("[UPDATE] complete", flush=True)
    return 0
