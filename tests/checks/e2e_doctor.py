"""doctor reports /.verify-browser readiness from package metadata — and probes nothing.

The browser and the app URL belong to each run's preflight, against the URL that run was
given. The probes are patched to record calls, not to decide results: whether this
machine happens to have Playwright must not decide what the check proves.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from exoma.core.audit.diagnostics import run_doctor
from exoma.core.evidence.e2e import preflight as e2e_preflight
from exoma.core.runtime.state import ensure_workflow_workspace
from exoma.core.workspace.workspace_paths import read_json_file
from tests.checks.support import assert_true


def _workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="e2e-doctor-"))
    ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
    return root


def _doctor(root: Path) -> tuple[dict, dict]:
    meta = run_doctor(root, "does-not-exist", "e2e-doctor")["meta"]
    report = read_json_file(Path(meta["doctor_report"]))
    return meta, report["checks"]["e2e_readiness"]


def _mentions_playwright(items: list[str]) -> list[str]:
    return [item for item in items if "playwright" in item.lower()]


def _test_e2e_doctor_readiness() -> None:
    saved = (
        e2e_preflight.installed_playwright,
        e2e_preflight.playwright_available,
        e2e_preflight.browser_available,
        e2e_preflight.base_url_reachable,
    )
    probes: list[str] = []

    def record(name: str, answer):
        def probe(*args, **kwargs):
            probes.append(name)
            return answer
        return probe

    roots: list[Path] = []
    try:
        e2e_preflight.playwright_available = record("package", (True, "ok"))
        e2e_preflight.browser_available = record("browser", (True, "ok"))
        e2e_preflight.base_url_reachable = record("url", (False, "ConnectionRefusedError"))
        pinned = e2e_preflight.pinned_playwright()

        # installed at the pin: reported, nothing to fix, nothing probed
        root = _workspace()
        roots.append(root)
        e2e_preflight.installed_playwright = lambda: pinned
        meta, e2e = _doctor(root)
        assert_true(e2e["playwright_pinned"] == pinned == "1.60.0" and e2e["python"], f"the interpreter and the shipped pin are reported: {e2e}")
        assert_true(e2e["fix"] is None and e2e["blocking"] is False, f"a matching install needs nothing: {e2e}")
        assert_true(probes == [], f"doctor starts no driver and touches no URL: {probes}")
        assert_true(not _mentions_playwright(meta["issues"]), f"and raises no issue: {meta['issues']}")

        # not installed: stated, never an issue and never a fix — most projects never open a browser
        e2e_preflight.installed_playwright = lambda: None
        meta, e2e = _doctor(root)
        assert_true("playwright_missing" in e2e["detail"] and "--with-e2e" in e2e["detail"], f"the detail names the run-time reason and the installer flag: {e2e['detail']}")
        assert_true(e2e["fix"] is None and not _mentions_playwright(meta["issues"] + meta["recommended_fixes"]), f"a missing optional extra is not a problem: {meta}")

        # drifted from the pin: a recommended fix, still not an issue
        e2e_preflight.installed_playwright = lambda: "1.0.0"
        meta, e2e = _doctor(root)
        assert_true(e2e["fix"] and pinned in e2e["fix"] and e2e["fix"] in meta["recommended_fixes"], f"a drifted install is named: {e2e}")
        assert_true(not _mentions_playwright(meta["issues"]), "and stays advice")
    finally:
        (
            e2e_preflight.installed_playwright,
            e2e_preflight.playwright_available,
            e2e_preflight.browser_available,
            e2e_preflight.base_url_reachable,
        ) = saved
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)
