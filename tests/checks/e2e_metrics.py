"""Plan §24 metrics over recorded e2e runs: split by run kind, joined to token spend, and
compared with delegated verification — computed at read time, every rate with its count."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from exoma.core.audit.telemetry import e2e_metrics, report, test_pass_rate
from exoma.core.evidence.contracts import UsageRecord
from exoma.core.evidence.runtime_io import write_quality_record, write_usage_record
from exoma.core.runtime.state import ensure_workflow_workspace
from tests.checks.support import assert_true


def _run(run_kind: str, verdict: str, **fields) -> dict:
    row = {
        "kind": "e2e_run",
        "run_kind": run_kind,
        "verdict": verdict,
        "browser_verdict": fields.pop("browser_verdict", verdict),
        "reason": None,
        "claims": {"total": 1, "proven": 0, "failed": 0, "unproven": 1},
        "failure_origins": {"app": 0, "harness": 0, "unknown": 0},
        "browser_runs": 1,
        "probes": 0,
        "artifacts": {"kept": 0, "screenshots": 0, "traces": 0, "skipped": 0, "pruned": 0},
        "screenshots_sent_to_model": 0,
        "provider_prompt_ids": [],
        "provider_calls": 0,
        "scenario_hash": None,
        "duration_seconds": 10.0,
    }
    row.update(fields)
    return row


def _usage(prompt_id: str, command: str, tokens_in: int, tokens_out: int, duration: float | None = None) -> dict:
    return UsageRecord(
        prompt_id=prompt_id,
        command=command,
        ok=True,
        actual_input_tokens=tokens_in,
        actual_output_tokens=tokens_out,
        duration_seconds=duration,
    ).to_dict()


def _test_e2e_metrics() -> None:
    root = Path(tempfile.mkdtemp(prefix="e2e-metrics-"))
    try:
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
        for row in (
            _usage("p1", "e2e_spec", 100, 50),
            _usage("p2", "verify", 200, 100),  # stage 3 of an e2e run: not baseline
            _usage("p3", "e2e_spec", 80, 20),
            _usage("p9", "verify", 300, 100, 10.0),
            _usage("p10", "verify", 500, 100, 20.0),
        ):
            write_usage_record(root, row)
        for row in (
            _run("project", "pass", claims={"total": 2, "proven": 2, "failed": 0, "unproven": 0}, provider_prompt_ids=["p1", "p2"], provider_calls=2, scenario_hash="h1", label="change-1"),
            _run("project", "incomplete", reason="unknown_origin", failure_origins={"app": 0, "harness": 0, "unknown": 1},
                 claims={"total": 2, "proven": 1, "failed": 1, "unproven": 0}, provider_prompt_ids=["p3"], provider_calls=1, scenario_hash="h1", probes=2, label="change-1"),
            _run("project", "fail", artifacts={"kept": 2, "screenshots": 1, "traces": 1, "skipped": 0, "pruned": 0}, scenario_hash="h2", label="change-2"),
            _run("fake", "pass"),
            _run("smoke", "incomplete", reason="harness_error", failure_origins={"app": 0, "harness": 1, "unknown": 0}),
        ):
            write_quality_record(root, row)
        write_quality_record(root, {"kind": "tests", "ok": True, "suites": ["scenario"]})
        for draft in (
            {"kind": "e2e_draft", "run_kind": "project", "status": "ready", "first_error": "json_invalid", "categories": {}, "repair_attempted": True, "repair_recovered": True},
            {"kind": "e2e_draft", "run_kind": "project", "status": "invalid", "first_error": "source_refs", "categories": {"source_refs": 2, "selector_shape": 1}, "repair_attempted": True, "repair_recovered": False},
            {"kind": "e2e_draft", "run_kind": "project", "status": "invalid", "first_error": "source_refs", "categories": {"source_refs": 1}, "repair_attempted": False, "secret_literals": ["E2E_USER"]},
            {"kind": "e2e_draft", "run_kind": "fake", "status": "ready", "first_error": None, "categories": {}},
        ):
            write_quality_record(root, draft)

        metrics = e2e_metrics(root)
        project = metrics["by_run_kind"]["project"]
        assert_true(metrics["runs"] == 5 and project["runs"] == 3, f"runs are split by kind before anything is aggregated: {metrics['runs']} {project['runs']}")
        assert_true(project["verdicts"] == {"pass": 1, "fail": 1, "incomplete": 1}, f"verdicts: {project['verdicts']}")
        assert_true(project["incomplete_by_reason"] == {"unknown_origin": 1} and project["incomplete_rate"] == 0.333, "incomplete rate with its reason")
        assert_true(project["unknown_origin_rate"] == 0.333 and project["harness_failure_rate"] == 0.0, "origin rates over project runs only")
        assert_true(project["claims"] == 5 and project["browser_runs_per_claim"] == 0.6, f"browser runs per claim: {project['browser_runs_per_claim']}")
        assert_true(
            project["tokens_per_run"] == {"mean": 183.33, "median": 100.0, "measured_runs": 3, "unmeasured_runs": 0},
            f"tokens join through the stage prompt ids; a run with no provider call costs 0: {project['tokens_per_run']}",
        )
        assert_true(project["reproducibility"] == {"repeated_scenarios": 1, "stable": 0, "rate": 0.0}, f"a repeated scenario that changed verdict is not reproducible: {project['reproducibility']}")
        assert_true(project["screenshots_kept"] == 1 and project["screenshot_analysis_rate"] == 0.0, "screenshots kept vs. read by a model")
        assert_true(project["probes_per_run"] == 0.67, f"probes per run: {project['probes_per_run']}")
        assert_true(metrics["by_run_kind"]["fake"]["runs"] == 1, "fake runs are kept out of the project numbers")
        assert_true(metrics["by_run_kind"]["smoke"]["harness_failure_rate"] == 1.0, "smoke runs report their own harness rate")

        baseline = metrics["delegated_verify_baseline"]
        assert_true(
            baseline["verifications"] == 2 and baseline["tokens_per_verification"]["mean"] == 500.0 and baseline["duration_seconds"]["mean"] == 15.0,
            f"the baseline is delegated verify rows that belong to no e2e run: {baseline}",
        )
        assert_true(metrics["project_vs_baseline_token_ratio"] == 0.37, f"project e2e tokens against the baseline: {metrics['project_vs_baseline_token_ratio']}")
        assert_true(metrics["visual_browser_loop_baseline"] is None, "a baseline nobody measured is reported as absent, never estimated")
        assert_true(metrics["labels"] == ["change-1", "change-2"], "dataset labels are listed")
        drafts = metrics["drafts"]["project"]
        assert_true(
            drafts["drafts"] == 3 and drafts["ready"] == 1 and drafts["invalid"] == 2 and drafts["ready_rate"] == 0.333
            and drafts["repair_attempted"] == 2 and drafts["repair_recovered"] == 1 and drafts["secret_literal_drafts"] == 1,
            f"drafts are counted apart from runs: {drafts}",
        )
        assert_true(
            drafts["first_error"] == {"source_refs": 2, "json_invalid": 1} and drafts["categories"] == {"source_refs": 3, "selector_shape": 1},
            f"first errors (what CASE-005 tallied by hand) and what the repair left standing: {drafts}",
        )
        assert_true(metrics["runs"] == 5 and metrics["drafts"]["fake"]["drafts"] == 1, "a draft is never a run, and fake drafts stay apart")

        assert_true(test_pass_rate(root)["total_runs"] == 1, "e2e rows never count as test-suite runs")
        assert_true(report(root)["e2e"]["runs"] == 5, "the report carries the e2e section")

        empty = Path(tempfile.mkdtemp(prefix="e2e-metrics-empty-"))
        try:
            ensure_workflow_workspace(empty, os.getenv("AGENT_PATH"))
            nothing = e2e_metrics(empty)
            assert_true(
                nothing["runs"] == 0 and nothing["by_run_kind"]["project"]["incomplete_rate"] is None and nothing["project_vs_baseline_token_ratio"] is None,
                "no runs: no rates, not zeros",
            )
        finally:
            shutil.rmtree(empty, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)
