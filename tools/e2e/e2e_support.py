"""Shared scaffolding for the e2e check modules: report, CLI runner, paths.

Lives beside e2e.py rather than under it so REPO_ROOT keeps the same two-parent walk.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))
# Global state (job records, the main-session cache, the provider seed) goes to a
# throwaway EXOMA_HOME, never the user's own data directory. Set before anything imports
# exoma.config.settings, which resolves it once.
if not os.environ.get("EXOMA_HOME"):
    os.environ["EXOMA_HOME"] = tempfile.mkdtemp(prefix="exoma-test-home-")
# Child interpreters the checks spawn (`python -c ...` lock holders, probes) import exoma
# from this checkout's src/, as they used to import core/ from the repo root they ran in.
os.environ["PYTHONPATH"] = os.pathsep.join(
    part for part in (str(REPO_ROOT / "src"), os.environ.get("PYTHONPATH", "")) if part
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIPPED"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def record(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        print(f"  {status:8} {name}{'  — ' + detail if detail else ''}")

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        self.record(name, PASS if condition else FAIL, detail)
        return condition

    @property
    def failed(self) -> int:
        return sum(1 for _, status, _ in self.rows if status == FAIL)

    @property
    def skipped(self) -> int:
        return sum(1 for _, status, _ in self.rows if status == SKIP)


def _run_cli(*args: str, cwd: Path | None = None) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd or REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), "PYTHONUTF8": "1"},
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _json_from(output: str) -> dict | None:
    start = output.find("{")
    if start < 0:
        return None
    try:
        return json.loads(output[start:])
    except json.JSONDecodeError:
        return None
