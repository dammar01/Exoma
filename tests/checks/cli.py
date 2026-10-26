"""`python main.py` as a script, not as an imported module.

Written after `_browser_exit_commands` landed below the `if __name__ == "__main__":` block.
Every other check imports `main`, which skips that block entirely, so the whole suite
stayed green while every real CLI invocation printed its JSON and then died on a
`NameError` at the exit-code step. Only a subprocess runs the file the way users do.
"""

import json
import subprocess
import sys
from pathlib import Path

from exoma import cli as main
from tests.checks.support import assert_true
from tests.checks.support import no_console

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        **no_console(),
    )


def _test_cli_script_reaches_its_exit_code() -> None:
    # `status` for a job that does not exist: no provider, no workspace write, and it still
    # travels the full print-then-exit path at the bottom of the `__main__` block.
    proc = _run_cli("--command", "status", "--job-id", "job_cli_probe_does_not_exist")
    assert_true(
        "Traceback" not in proc.stderr,
        f"the CLI must exit through its exit-code mapping, not an exception:\n{proc.stderr[-800:]}",
    )
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise AssertionError(f"stdout must end in one JSON result ({exc}): {proc.stdout[-400:]!r}")
    assert_true(
        payload.get("ok") is False and payload.get("status") == "not_found",
        f"an unknown job reports not_found: {payload}",
    )
    assert_true(
        proc.returncode == 1,
        f"a failed non-verify command exits 1, got {proc.returncode}",
    )

    # The mapping the script applies before exiting. Four shapes reach it: a foreground
    # draft, a foreground completed run, `await` (flat output) and `result` (nested output).
    ran = {"ok": True, "meta": {"command": "verify", "verdict": "pass"}}
    draft = {"ok": True, "meta": {"command": "verify-browser", "phase": "draft"}}
    cases = [
        (("verify-browser", draft, None), ("verify-browser", None), "a foreground draft keeps its own command"),
        (("verify-browser", ran, None), ("verify", None), "a foreground completed run exits like verify"),
        (("await", ran, "verify-browser"), ("await", "verify"), "await reads the flat output"),
        (("await", draft, "verify-browser"), ("await", "verify-browser"), "an awaited draft stays verify-browser"),
        (("result", {"ok": True, "status": "completed", "output": ran}, "verify-browser"), ("result", "verify"), "result reads the nested output"),
        (("explore", {"ok": True, "meta": {"command": "verify"}}, None), ("explore", None), "other commands pass through untouched"),
        (("await", ran, "explore"), ("await", "explore"), "a non-browser job passes through untouched"),
    ]
    for arguments, expected, label in cases:
        got = main._browser_exit_commands(*arguments)
        assert_true(got == expected, f"{label}: expected {expected}, got {got}")
