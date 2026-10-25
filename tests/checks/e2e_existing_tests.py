"""Existing project tests: selected only through the user's command and allowlist, run
without a shell, bounded, scrubbed — and folded into claims without being trusted blindly."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from exoma.core.evidence.e2e.classify import build_report
from exoma.core.evidence.e2e.existing_tests import build_argv, run, select
from tests.checks.support import assert_true

_PASS = "import os, sys; print('files', sys.argv[1:]); print('user', os.environ.get('E2E_UNIT_USER')); sys.exit(0)"
_FAIL = "import sys; print('1 failed'); sys.exit(1)"
_SLEEP = "import time; time.sleep(30)"
_USER = "unit.user@internal.example"


def _test_e2e_existing_tests() -> None:
    root = Path(tempfile.mkdtemp(prefix="e2e-existing-"))
    saved_user = os.environ.get("E2E_UNIT_USER")
    try:
        (root / "e2e").mkdir()
        (root / "e2e" / "login.spec.ts").write_text("// login spec\n", encoding="utf-8")
        (root / "scripts").mkdir()
        (root / "scripts" / "seed.sh").write_text("echo seed\n", encoding="utf-8")
        listed = [
            {"path": "e2e/login.spec.ts", "covers": ["login"]},
            {"path": "e2e/gone.spec.ts", "covers": ["logout"]},
            {"path": "../outside.spec.ts", "covers": ["x"]},
            {"path": "scripts/seed.sh", "covers": ["y"]},
            {"path": "e2e/login.spec.ts; rm -rf /", "covers": ["z"]},
        ]

        # --- selection -----------------------------------------------------------------------------
        runnable, skipped = select(listed, {}, root)
        assert_true(runnable == [] and all("existing_test_command is not set" in s["result"] for s in skipped), "no command configured: nothing runs")
        config = {
            "existing_test_command": [sys.executable, "-c", _PASS, "{files}", "--base={base_url}"],
            "existing_test_allowlist": ["e2e/*.spec.ts"],
            "base_url": "http://localhost:8000",
            "existing_test_timeout_s": 20,
        }
        runnable, skipped = select(listed, config, root)
        reasons = {s["path"]: s["result"] for s in skipped}
        assert_true([t["path"] for t in runnable] == ["e2e/login.spec.ts"], f"only the allow-listed file that exists runs: {runnable}")
        assert_true("does not exist" in reasons["e2e/gone.spec.ts"], "a listed file that is not there is skipped")
        assert_true("plain" in reasons["../outside.spec.ts"] and "plain" in reasons["e2e/login.spec.ts; rm -rf /"], "a path that escapes or smuggles argv is refused")
        assert_true("allowlist" in reasons["scripts/seed.sh"], "a real file outside the allowlist is skipped")
        assert_true(
            build_argv(["npx", "playwright", "test", "{files}", "--base-url={base_url}"], ["a.spec.ts", "b.spec.ts"], "http://x")
            == ["npx", "playwright", "test", "a.spec.ts", "b.spec.ts", "--base-url=http://x"],
            "{files} expands in place, {base_url} is substituted",
        )
        assert_true(build_argv(["pytest", "-q"], ["t.py"], "") == ["pytest", "-q", "t.py"], "without {files} the files are appended")

        # --- execution -----------------------------------------------------------------------------
        os.environ["E2E_UNIT_USER"] = _USER
        result = run(runnable, config, root, {"E2E_UNIT_USER": _USER})
        output = result["output_tail"]
        assert_true(result["status"] == "passed" and result["covers"] == ["login"], f"exit 0 passes: {result['status']} {output}")
        assert_true("e2e/login.spec.ts" in output and "--base=http://localhost:8000" in output, f"the command got the files and the base URL: {output}")
        assert_true(_USER not in output and "${E2E_UNIT_USER}" in output, f"a resolved value echoed by the test is scrubbed: {output}")

        result = run(runnable, dict(config, existing_test_command=[sys.executable, "-c", _FAIL]), root, {})
        assert_true(result["status"] == "failed" and result["returncode"] == 1 and "1 failed" in result["output_tail"], f"non-zero exit fails: {result}")

        started = __import__("time").monotonic()
        result = run(runnable, dict(config, existing_test_command=[sys.executable, "-c", _SLEEP], existing_test_timeout_s=1), root, {})
        assert_true(result["status"] == "timeout" and __import__("time").monotonic() - started < 20, f"the timeout ends the run: {result['status']}")

        result = run(runnable, dict(config, existing_test_command=["definitely-not-a-test-runner-xyz", "{files}"]), root, {})
        assert_true(result["status"] == "launch_failed" and "not found" in result["output_tail"], f"a missing runner is launch_failed: {result}")

        # --- folding into claims -------------------------------------------------------------------
        scenario = {"claims": [{"id": "login", "severity": "blocking"}]}
        finished = [{"type": "result", "status": "finished"}]

        def report(status: str, events: list[dict] = finished) -> dict:
            return build_report(events, scenario, run_meta={"existing_tests": {"status": status, "covers": ["login"], "files": ["e2e/login.spec.ts"], "returncode": 0}})

        passed = report("passed")
        assert_true(passed["browser_verdict"] == "pass" and passed["claims"]["login"]["status"] == "proven", f"a passing test proves its claim: {passed['claims']}")
        failed = report("failed")
        assert_true(
            failed["browser_verdict"] == "incomplete" and failed["reason"] == "unknown_origin" and failed["claims"]["login"]["origin"] == "unknown",
            f"a failing project test is unknown, not app: {failed['reason']} {failed['claims']}",
        )
        assert_true(report("launch_failed")["failures"][0]["origin"] == "harness", "a runner that never started is the harness's")
        browser_fail = [
            {"type": "progress", "step": 1, "action": "expect_url", "status": "failed", "claim_id": "login", "error": {"kind": "assertion"}},
            *finished,
        ]
        mixed = report("passed", browser_fail)
        assert_true(mixed["browser_verdict"] == "fail" and mixed["claims"]["login"]["status"] == "failed", "a passing project test does not excuse a browser assertion that failed")
    finally:
        if saved_user is None:
            os.environ.pop("E2E_UNIT_USER", None)
        else:
            os.environ["E2E_UNIT_USER"] = saved_user
        shutil.rmtree(root, ignore_errors=True)
