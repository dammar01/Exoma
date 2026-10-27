"""The e2e harness names its provider and model before it spends quota.

`tools/e2e/e2e.py --full` used to run against the seed Exoma ships, whose model is unset,
and the first delegated call came back `model_unset`. These checks pin the fail-fast on a
missing model and the provider selection that replaces it, without a real provider call.
"""

import json
import subprocess
import sys
from pathlib import Path

from tests.checks.support import assert_true, no_console

REPO = Path(__file__).resolve().parents[2]


def _e2e(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "tools" / "e2e" / "e2e.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO),
        timeout=60,
        **no_console(),
    )


def _test_e2e_harness_names_its_model() -> None:
    # --full with no model, or a blank one, stops at argument parsing: no check runs.
    for extra in ((), ("--model", "  ")):
        done = _e2e("--full", *extra)
        assert_true(
            done.returncode == 2 and "--model" in done.stderr and "[E2E REPORT]" not in done.stdout,
            f"--full without a model refuses before any check: rc={done.returncode} "
            f"{done.stderr[-200:]!r} {done.stdout[-200:]!r}",
        )

    from tools.e2e import e2e_delegated
    from tools.e2e.e2e_support import FAIL, PASS, Report

    # A direct caller without a model is refused the same way, before a project exists.
    # A whitespace-only one counts as none: the provider command would read it as unset and
    # fall back to the provider default.
    for missing in (None, "", "   "):
        report = Report()
        e2e_delegated.delegated_checks(report, "e2e-harness", "opencode", missing)
        assert_true(
            len(report.rows) == 1 and report.rows[0][1] == FAIL and "--model" in report.rows[0][2],
            f"delegated_checks with model {missing!r} records only a failure: {report.rows}",
        )

    # With a model, the provider is selected through the provider command before the runner
    # starts. No bash stops the run right after the selection, so no provider is called.
    selected: dict = {}
    real_find_bash, real_run_cli = e2e_delegated.find_bash, e2e_delegated._run_cli

    def spy_run_cli(*args, **kwargs):
        code, output = real_run_cli(*args, **kwargs)
        if "provider" in args:
            config = Path(args[args.index("--work-dir") + 1]) / ".workflow" / "second_agent.json"
            if config.is_file():
                selected.update(json.loads(config.read_text(encoding="utf-8")))
        return code, output

    e2e_delegated.find_bash = lambda: None
    e2e_delegated._run_cli = spy_run_cli
    try:
        report = Report()
        e2e_delegated.delegated_checks(report, "e2e-harness", "opencode", "e2e-test-model")
    finally:
        e2e_delegated.find_bash, e2e_delegated._run_cli = real_find_bash, real_run_cli
    names = {name: status for name, status, _detail in report.rows}
    assert_true(
        names.get("provider opencode with e2e-test-model selected") == PASS,
        f"the provider command applies the named model: {report.rows}",
    )
    assert_true(
        selected.get("provider") == "opencode" and selected.get("default_model") == "e2e-test-model",
        f"the temp project's second_agent.json names that provider and model: {selected}",
    )
