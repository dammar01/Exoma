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
    ("uv tool install exoma==1.0.0b1", ["1.0.0b1"]),
    ("1.0.0rc2 before 1.0.0, then 2.0.0a10", ["1.0.0rc2", "1.0.0", "2.0.0a10"]),
    ("[![version](https://img.shields.io/badge/version-1.0.0b1-informational)]", ["1.0.0b1"]),
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

    # A bump rewrites with `_SEMVER.sub(TOOL_VERSION, line)`: re-stamping a pre-release must
    # replace the whole version, not append a second suffix (`1.0.0b1b1`).
    for line, version, expected in (
        ("# Exoma v1.0.0b1", "1.0.0b1", "# Exoma v1.0.0b1"),
        ("# Exoma v1.0.0", "1.0.0b1", "# Exoma v1.0.0b1"),
        ("# Exoma v1.0.0b1", "1.0.0", "# Exoma v1.0.0"),
    ):
        stamped = stamp_version._SEMVER.sub(version, line)
        assert_true(stamped == expected, f"stamping {version} onto {line!r} gave {stamped!r}")

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
