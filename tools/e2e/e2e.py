"""Exoma end-to-end checks.

    python tools/e2e/e2e.py           # local + integrity + installer
    python tools/e2e/e2e.py --full    # also the delegated calls (spends quota)

Check bodies live in tools/e2e/e2e_*.py; this module owns the CLI and the order they
run in.
"""

import argparse
import os
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

from tools.e2e.e2e_delegated import delegated_checks  # noqa: E402
from tools.e2e.e2e_installer import installer_checks  # noqa: E402
from tools.e2e.e2e_integrity import integrity_checks  # noqa: E402
from tools.e2e.e2e_local import local_checks  # noqa: E402
from tools.e2e.e2e_promote import promote_checks  # noqa: E402
from tools.e2e.e2e_support import Report, SKIP, _json_from, _run_cli  # noqa: E402,F401


def main() -> int:
    parser = argparse.ArgumentParser(description="Exoma end-to-end checks")
    parser.add_argument(
        "--full",
        action="store_true",
        help="also run delegated commands (minutes + real quota)",
    )
    parser.add_argument(
        "--session", default="e2e-session", help="session id for delegated runs"
    )
    args = parser.parse_args()

    report = Report()
    local_checks(report)
    integrity_checks(report)
    promote_checks(report)
    installer_checks(report)
    if args.full:
        delegated_checks(report, args.session)
    else:
        report.record("delegated commands", SKIP, "opt-in via --full")

    total = len(report.rows)
    passed = total - report.failed - report.skipped
    print("\n[E2E REPORT]")
    print(
        f"  {passed} passed | {report.failed} failed | {report.skipped} skipped "
        f"({total} checks)"
    )
    if report.skipped:
        print("  skipped checks are NOT passes — see the reasons above")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
