"""`tools/maintain/stamp_version.py` must read versions, not addresses.

Its semver pattern once read `127.0.0.1` in a shipped skill as version "127.0.0" and
failed `--check` in CI. The pattern decides both what `--check` reports and what a bump
rewrites, so a miss in either direction is a release defect: an IP flagged as an
unreviewed version turns CI red, and a version the pattern stops seeing is left stale.

The repo-wide `--check` runs through the tool's real entry point, the same way
`bundle_sync` runs its tools: a check that reimplements the scan proves the copy.
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

from tests.checks.support import assert_true

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "src"))

# (text, the versions the pattern must find in it)
_CASES = (
    ("[![version](https://img.shields.io/badge/version-3.5.3-blue)]", ["3.5.3"]),
    ("# agent-workflow v3.6.0", ["3.6.0"]),
    ("Catatan rilis: (v3.6.0)", ["3.6.0"]),
    ("dirilis sebagai 3.6.0.", ["3.6.0"]),
    ("| 3.5.3 | notes |", ["3.5.3"]),
    ("pin 1.2.3-rc1", ["1.2.3"]),
    ("base_url: `http://localhost:8000` | `http://127.0.0.1:8000`", []),
    ("loopback 10.10.10.10", []),
    ("http://192.168.0.1:8080/login", []),
    ("::ffff:127.0.0.1", []),
)


def _test_stamp_version_reads_versions_not_addresses() -> None:
    from tools.maintain import stamp_version

    for text, expected in _CASES:
        found = stamp_version._SEMVER.findall(text)
        assert_true(found == expected, f"_SEMVER on {text!r}: expected {expected}, got {found}")

    buffer = io.StringIO()
    original_argv = sys.argv
    sys.argv = ["stamp_version.py", "--check"]
    try:
        with contextlib.redirect_stdout(buffer):
            code = stamp_version.main()
    except SystemExit as exc:
        code, buffer = 1, io.StringIO(f"{buffer.getvalue()}{exc}")
    finally:
        sys.argv = original_argv
    assert_true(
        code == 0,
        f"stamp_version --check fails on this tree:\n{buffer.getvalue().strip()}\n"
        "Run: python tools/maintain/stamp_version.py --check",
    )
