"""The P1 metrics, asserted on the distinctions that would be invisible if broken.

A wrong aggregate reads exactly like a right one, so these checks are aimed at the places
where the definition is a judgement call rather than arithmetic: acceptance counted per
task instead of per call, first-pass correctness kept apart from eventual acceptance, and
every rate carrying the denominator that makes it readable.
"""

from exoma.core.workspace.workspace_paths import data_dir
import shutil
import tempfile
from pathlib import Path

from exoma.core.audit import telemetry
from exoma.core.evidence.contracts import UsageRecord
from exoma.core.evidence.runtime_io import write_quality_record, write_usage_record
from tests.checks.support import assert_true


def _usage(**kwargs) -> UsageRecord:
    return UsageRecord(**kwargs)


def _acceptance_is_per_task() -> None:
    rows = [
        _usage(correlation_id="t1", command="verify", accepted=False),
        _usage(correlation_id="t1", command="verify", accepted=True),
        _usage(correlation_id="t2", command="verify", accepted=True),
        _usage(correlation_id="t3", command="explore"),
    ]
    acceptance = telemetry.accepted_tasks(rows)
    assert_true(
        acceptance["accepted"] == 2 and acceptance["judged"] == 2,
        "acceptance counts distinct tasks, not calls — counting calls rewards re-running "
        "verify until it goes green",
    )

    first = telemetry.first_pass_correctness(rows)
    assert_true(
        first["first_pass"] == 1 and first["judged"] == 2,
        "t1 passed eventually and not first time; collapsing that into acceptance hides "
        "every retry the workflow needed",
    )

    again = telemetry.rework(rows)
    assert_true(
        again["reworked_tasks"] == 1 and again["extra_verification_rounds"] == 1,
        "rework must count the tasks that came back, and how many extra rounds they cost",
    )


def _rework_counts_work_that_came_back_after_a_failure() -> None:
    def rounds(*verdicts):
        return [
            _usage(correlation_id="t", command="verify", accepted=v, recorded_at=f"2026-10-09T00:00:0{i}+00:00")
            for i, v in enumerate(verdicts)
        ]

    cases = {
        "pass, pass": (rounds(True, True), 0, 0),
        "four phases, all pass": (rounds(True, True, True, True), 0, 0),
        "fail, pass": (rounds(False, True), 1, 1),
        "fail, fail, pass": (rounds(False, False, True), 1, 2),
        "pass, fail": (rounds(True, False), 0, 0),
        "pass, fail, pass": (rounds(True, False, True), 1, 1),
    }
    for name, (rows, reworked, extra) in cases.items():
        got = telemetry.rework(rows)
        assert_true(
            got["reworked_tasks"] == reworked and got["extra_verification_rounds"] == extra
            and got["judged"] == 1,
            f"{name}: rework counts rounds after the first failure, not every round; got {got}",
        )

    # incomplete is accepted=False, the same as fail.
    incomplete = [
        _usage(correlation_id="t", command="verify", verdict="incomplete", accepted=False,
               recorded_at="2026-10-09T00:00:00+00:00"),
        _usage(correlation_id="t", command="verify", verdict="pass", accepted=True,
               recorded_at="2026-10-09T00:00:01+00:00"),
    ]
    assert_true(
        telemetry.rework(incomplete)["reworked_tasks"] == 1,
        "an incomplete verify followed by another round is rework, like a failed one",
    )

    # Order is the rows' timestamps, not the order they were appended in.
    late_first = list(reversed(rounds(False, True)))
    assert_true(
        telemetry.rework(late_first)["reworked_tasks"] == 1,
        "a failure recorded earlier counts as earlier, whatever the append order",
    )


def _unjudged_work_is_not_incorrect() -> None:
    rows = [_usage(correlation_id="t9", command="explore")]
    first = telemetry.first_pass_correctness(rows)
    assert_true(
        first["judged"] == 0 and first["rate"] is None,
        "work that was never verified is unknown, not wrong — scoring it as a failure "
        "would punish every command that does not end in a verify",
    )


def _report_reports_its_denominators() -> None:
    root = Path(tempfile.mkdtemp(prefix="aw-telemetry-"))
    try:
        write_usage_record(
            root,
            _usage(
                correlation_id="t1",
                command="explore",
                estimated_input_tokens=400,
                estimated_output_tokens=600,
                premium_context_avoided_tokens=225,
                duration_seconds=10.0,
                token_source="estimated",
            ).to_dict(),
        )
        write_usage_record(
            root,
            _usage(
                correlation_id="t1",
                command="verify",
                accepted=True,
                estimated_input_tokens=100,
                estimated_output_tokens=100,
                token_source="estimated",
            ).to_dict(),
        )
        summary = telemetry.report(root)

        assert_true(
            summary["cost"]["total_tokens"] == 1200
            and summary["accepted_tasks"]["tokens_per_accepted_task"] == 1200,
            "the headline number is total tokens over accepted tasks; got "
            f"{summary['accepted_tasks']}",
        )
        assert_true(
            summary["cost"]["token_source"] == ["estimated"],
            "a cost figure that cannot tell an estimate from a measurement is not a cost figure",
        )
        assert_true(
            summary["time_to_completion_seconds"]["unmeasured_calls"] == 1,
            "calls with no recorded duration must be named, not averaged away — a mean over "
            "half the calls is a different claim from a mean over all of them",
        )
        assert_true(
            summary["premium_context_avoided"]["total_tokens"] == 225,
            "premium context avoided is the digest-first contract's justification as a number",
        )
        assert_true(
            summary["tests"]["recorded"] is False and summary["tests"]["rate"] is None,
            "an empty quality stream is not a failing one; a fresh workspace must not read "
            "as a repo whose tests never pass",
        )

        write_quality_record(root, {"kind": "tests", "ok": True, "suites": ["scenario"]})
        write_quality_record(root, {"kind": "tests", "ok": False, "suites": ["scenario"]})
        rate = telemetry.test_pass_rate(root)
        assert_true(
            rate["passed_runs"] == 1 and rate["total_runs"] == 2 and rate["rate"] == 0.5,
            "test pass rate reads the quality stream, which records check runs rather than "
            "delegated calls",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _evidence_reuse_counts_commands_it_could_serve() -> None:
    rows = [
        # One explore that ran a continuation: two rows, one command.
        _usage(command="explore", role="exploration", prompt_id="p1"),
        _usage(command="explore", role="exploration", prompt_id="p1"),
        # A reuse hit has no prompt id and is its own command.
        _usage(command="analyze", role="reasoning", reused_evidence=True, provider_call_avoided=True),
        # Verify is never offered reuse, so it is not in the denominator.
        _usage(command="verify", role="verification", prompt_id="p2"),
        _usage(command="explore", prompt_id="p3"),
        # A verify-browser draft takes the exploration route but is never offered reuse.
        _usage(command="verify-browser", role="exploration", prompt_id="p4", e2e_stage="draft"),
    ]
    reuse = telemetry.evidence_reuse(rows)
    assert_true(
        reuse["reused"] == 1 and reuse["eligible_commands"] == 2 and reuse["rate"] == 0.5,
        "reuse rate is reused commands over reuse-eligible commands, a continuation counted "
        f"once; got {reuse}",
    )
    assert_true(
        reuse["role_unrecorded_rows"] == 1,
        "a row without a role is named, not guessed into the denominator",
    )
    assert_true(
        telemetry.evidence_reuse([])["rate"] is None,
        "no eligible command is no answer, not a rate of zero",
    )


def _reuse_misses_are_broken_down_by_reason() -> None:
    rows = [
        _usage(command="explore", role="exploration", prompt_id="p1", reuse_outcome="no_prior"),
        _usage(command="explore", role="exploration", prompt_id="p1", reuse_outcome="no_prior"),
        _usage(command="analyze", role="reasoning", prompt_id="p2", reuse_outcome="stale"),
        _usage(command="analyze", role="reasoning", reused_evidence=True, reuse_outcome="hit"),
        # Written before contract v3: named, not guessed.
        _usage(command="explore", role="exploration", prompt_id="p3"),
    ]
    reuse = telemetry.evidence_reuse(rows)
    assert_true(
        reuse["by_outcome"] == {"hit": 1, "no_prior": 1, "stale": 1, "unrecorded": 1},
        "each reuse-eligible command carries one outcome, a continuation counted once and a "
        f"pre-v3 row as unrecorded; got {reuse['by_outcome']}",
    )


def _provider_cache_share_splits_continuations() -> None:
    rows = [
        _usage(prompt_id="p1", provider_call_index=0, provider_resumed=False,
               actual_input_tokens=1000, actual_cached_input_tokens=200),
        _usage(prompt_id="p1", provider_call_index=1, provider_resumed=True,
               actual_input_tokens=1000, actual_cached_input_tokens=900),
        # The provider did not report a cached count: outside the share, not a zero in it.
        _usage(prompt_id="p2", actual_input_tokens=500),
    ]
    cache = telemetry.provider_cache(rows)
    assert_true(
        cache["all"]["share"] == 0.55 and cache["all"]["measured_calls"] == 2,
        f"share is cached over measured input, unreported rows left out; got {cache['all']}",
    )
    assert_true(
        cache["first_call"]["share"] == 0.2 and cache["continuation"]["share"] == 0.9,
        f"a continuation's cache share is reported apart from first calls; got {cache}",
    )
    assert_true(
        cache["fresh_thread"]["share"] == 0.2 and cache["resumed_thread"]["share"] == 0.9
        and cache["fresh_thread"]["measured_calls"] + cache["resumed_thread"]["measured_calls"] == 2,
        f"fresh and resumed threads are split, a row that did not say is in neither; got {cache}",
    )
    assert_true(
        telemetry.provider_cache([])["all"]["share"] is None,
        "no measured input is no answer, not a share of zero",
    )


def _provider_threads_flag_lost_sessions() -> None:
    rows = [
        _usage(provider="codex", provider_resumed=False),
        _usage(provider="codex", provider_resumed=True),
        _usage(provider="codex", provider_resumed=True, provider_thread_changed=True),
        _usage(provider="opencode"),
        # Contract v2: could not report resume state, so it is not counted at all.
        _usage(provider="codex", contract_version=2),
        _usage(provider="legacy-only", contract_version=2),
    ]
    threads = telemetry.provider_threads(rows)
    assert_true(
        threads["codex"] == {"resumed": 2, "fresh": 1, "unknown": 0, "thread_changed": 1}
        and threads["opencode"] == {"resumed": 0, "fresh": 0, "unknown": 1, "thread_changed": 0}
        and "legacy-only" not in threads,
        "resume health is counted per provider, a lost thread named, an unreported row "
        f"unknown rather than fresh, a pre-v3 row left out; got {threads}",
    )


def _by_effort_splits_commands_and_skips_old_rows() -> None:
    rows = [
        _usage(command="explore", prompt_id="p1", effort="low", duration_seconds=100,
               actual_input_tokens=1000, actual_output_tokens=10),
        _usage(command="explore", prompt_id="p1", effort="low", duration_seconds=20,
               actual_input_tokens=500, actual_output_tokens=5),
        _usage(command="explore", prompt_id="p2", effort="high", duration_seconds=300,
               actual_input_tokens=4000, actual_output_tokens=40),
        _usage(command="explore", prompt_id="p3", duration_seconds=50),
        # Contract v2: no effort was recorded, so it cannot be put in any bucket.
        _usage(command="explore", prompt_id="p4", contract_version=2, duration_seconds=999),
    ]
    split = telemetry.by_effort(rows)
    assert_true(
        set(split) == {"explore/low", "explore/high", "explore/default"},
        f"one bucket per command and effort, old rows left out; got {sorted(split)}",
    )
    assert_true(
        split["explore/low"]["duration_seconds_median"] == 120
        and split["explore/low"]["input_tokens_median"] == 1500
        and split["explore/high"]["input_tokens_median"] == 4000
        and split["explore/default"]["input_tokens_median"] is None,
        f"a continuation adds up within its command, unmeasured tokens stay None; got {split}",
    )

    # A command with no measured duration still counts; input measured without output is
    # not an output of zero.
    partial = telemetry.by_effort([
        _usage(command="analyze", prompt_id="q1", effort="low", duration_seconds=10,
               actual_input_tokens=100, actual_output_tokens=50),
        _usage(command="analyze", prompt_id="q2", effort="low", duration_seconds=None,
               actual_input_tokens=300),
    ])["analyze/low"]
    assert_true(
        partial["commands"] == 2
        and partial["duration_seconds_median"] == 10
        and partial["input_tokens_median"] == 200
        and partial["output_tokens_median"] == 50,
        f"commands counts every command; an unmeasured output stays out of its median; got {partial}",
    )


def _graph_leads_reports_calls_that_ran_during_a_refresh() -> None:
    rows = [
        _usage(command="verify", prompt_id="p1", graph_status="refreshing", graph_ms=3),
        _usage(command="verify", prompt_id="p1", graph_status="refreshing", graph_ms=3),
        _usage(command="explore", prompt_id="p2", graph_status="used", graph_ms=40),
        _usage(command="analyze", prompt_id="p3", graph_status="absent", graph_ms=1),
        _usage(command="explore", prompt_id="p4"),
    ]
    leads = telemetry.graph_leads(rows)
    assert_true(
        leads["by_status"] == {"absent": 1, "refreshing": 1, "unrecorded": 1, "used": 1},
        f"graph status is counted per command; got {leads['by_status']}",
    )
    assert_true(
        leads["refreshing_by_command"] == {"verify": 1},
        f"calls that went without leads because of a refresh are named by command; got {leads}",
    )
    assert_true(
        leads["lookup_ms_median"] == 3 and leads["lookup_ms_max"] == 40,
        f"lookup cost is reported per command; got {leads}",
    )


def _graph_refresh_reports_what_the_turn_paid() -> None:
    root = Path(tempfile.mkdtemp(prefix="aw-graph-refresh-"))
    try:
        for row in (
            {"kind": "graph_refresh", "outcome": "skipped_fresh", "hook_ms": 200, "scan_ms": 180},
            {"kind": "graph_refresh", "outcome": "refreshed", "hook_ms": 220, "scan_ms": 190,
             "graphify_ms": 90000, "graphify_exit": 1, "graph_rewritten": True},
            {"kind": "graph_refresh", "outcome": "skipped_running", "hook_ms": 10},
            {"kind": "tests", "ok": True},
        ):
            write_quality_record(root, row)
        summary = telemetry.graph_refresh(root)
        assert_true(
            summary["runs"] == 3
            and summary["by_outcome"] == {"refreshed": 1, "skipped_fresh": 1, "skipped_running": 1},
            f"graph refresh counts its own rows by outcome, not test rows; got {summary}",
        )
        assert_true(
            summary["hook_ms_median"] == 200 and summary["graphify_ms_median"] == 90000,
            "the turn's cost (hook_ms) and the detached graphify run are reported apart; "
            f"got {summary}",
        )
        assert_true(
            telemetry.test_pass_rate(root)["total_runs"] == 1,
            "graph refresh rows in the quality stream must not count as test runs",
        )
        assert_true(
            telemetry.report(root)["graph_refresh"]["runs"] == 3,
            "the report carries the graph_refresh section",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _torn_row_is_skipped_not_fatal() -> None:
    root = Path(tempfile.mkdtemp(prefix="aw-torn-"))
    try:
        write_usage_record(root, _usage(command="explore").to_dict())
        path = data_dir(root) / "usage.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"command": "verify", "ok"\n')  # killed mid-write
        rows = telemetry.load_usage(root)
        assert_true(
            len(rows) == 1,
            "a torn final line is a realistic state for an append-only stream; dropping "
            "that row is right, refusing to report anything is not",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _torn_multibyte_row_is_skipped_not_fatal() -> None:
    """A write killed mid UTF-8 character leaves bytes no decoder accepts. Strict decoding
    raised on the whole file and took `report` down with it; the torn row is skipped."""
    from exoma.core.audit import task_telemetry
    from exoma.core.evidence.contracts import QUALITY_STREAM_NAME, TASK_STREAM_NAME, USAGE_STREAM_NAME

    root = Path(tempfile.mkdtemp(prefix="aw-torn-utf8-"))
    try:
        write_usage_record(root, _usage(command="explore", prompt_id="p1", provider="opencode").to_dict())
        write_quality_record(root, {"kind": "tests", "passed": True})
        task_telemetry.record_skill(root, "s", "explore")
        cut = '{"command": "verify", "note": "café'.encode("utf-8")[:-1]  # half of é
        for name in (USAGE_STREAM_NAME, QUALITY_STREAM_NAME, TASK_STREAM_NAME):
            path = data_dir(root) / name
            assert_true(path.exists(), f"{name} was written before the torn row")
            with path.open("ab") as handle:
                handle.write(cut)
        assert_true(len(telemetry.load_usage(root)) == 1, "the usage row before the torn one is kept")
        assert_true(len(telemetry.load_quality(root)) == 1, "the quality row before the torn one is kept")
        assert_true(len(task_telemetry.load_events(root)) == 1, "the task event before the torn one is kept")
        assert_true(telemetry.report(root)["calls"] == 1, "report runs over a torn multi-byte row")
        task_telemetry.report(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _provider_less_browser_runs_are_not_calls() -> None:
    """Rows written before contract v4 for a browser run that reached no provider."""
    root = Path(tempfile.mkdtemp(prefix="telemetry-ghost-"))
    try:
        (root / ".workflow" / "data").mkdir(parents=True)
        ghost = _usage(command="verify", session_id="s", ok=True, verdict="incomplete", accepted=False)
        real = _usage(
            command="verify", session_id="s", ok=True, prompt_id="p1", provider="opencode",
            verdict="pass", accepted=True, correlation_id="t1", estimated_input_tokens=100,
        )
        assert_true(
            telemetry.provider_less_browser_run(ghost) and not telemetry.provider_less_browser_run(real),
            "the legacy ghost shape is recognised, an ordinary verify is not",
        )
        for row in (ghost, real):
            write_usage_record(root, row.to_dict())
        report = telemetry.report(root)
        assert_true(
            report["calls"] == 1,
            f"a browser run with no provider call is not a call: {report['calls']}",
        )
        baseline = report["e2e"]["delegated_verify_baseline"]
        assert_true(
            baseline["verifications"] == 1,
            f"nor a zero-token verification in the baseline: {baseline}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _fact_lifecycle_counts_commands_not_invocations() -> None:
    """Fact counts ride the command's final row; old rows are unrecorded, not zero."""
    from exoma.core.evidence.contracts import TaskSpec, usage_from_result

    spec = TaskSpec(command="analyze", task="t", session_id="s", correlation_id="c")
    built = usage_from_result(
        {"ok": True, "meta": {"facts": {
            "candidates": 12, "injected": 3, "rejected_stale": 2, "rejected_irrelevant": 7,
            "ingested": 1, "ingest_rejected_anchor": 2, "ingest_rejected_length": 1,
        }}},
        spec=spec, call_meta={"role": "reasoning", "prompt_id": "p1"}, recorded_at="now",
    )
    assert_true(
        (built.facts_candidates, built.facts_injected, built.facts_ingest_rejected) == (12, 3, 3),
        f"meta.facts reaches the usage row: {built}",
    )
    bare = usage_from_result({"ok": True, "meta": {}}, spec=spec, call_meta={}, recorded_at="now")
    assert_true(
        bare.facts_candidates is None and bare.facts_ingest_rejected is None,
        "a command that asked for no facts records none, not zeros",
    )

    rows = [
        # one continuation: two invocations, counts only on the final row
        _usage(prompt_id="a", command="analyze", role="reasoning"),
        _usage(prompt_id="a", command="analyze", role="reasoning", facts_candidates=10,
               facts_injected=3, facts_rejected_stale=1, facts_rejected_irrelevant=6,
               facts_ingested=2, facts_ingest_rejected=0),
        _usage(prompt_id="b", command="explore", role="exploration", facts_candidates=10,
               facts_injected=0, facts_rejected_stale=0, facts_rejected_irrelevant=10,
               facts_ingested=0, facts_ingest_rejected=1),
        _usage(prompt_id="c", command="plan", role="reasoning"),  # written before the fields
        _usage(prompt_id="d", command="verify", role="verification"),
    ]
    facts = telemetry.fact_lifecycle(rows)
    assert_true(
        facts["candidates"] == 20 and facts["injected"] == 3
        and facts["recorded_commands"] == 2 and facts["unrecorded_commands"] == 1
        and facts["commands_with_facts"] == 1 and facts["injection_rate"] == 0.15,
        f"counted per command, verify excluded, history named: {facts}",
    )


def _call_meta_names_the_facts_it_served() -> None:
    """call.meta.json lists the facts a call was served, so a replay need not guess (EXP-013)."""
    import os

    import exoma.core.provider.executor as executor_module
    from exoma.core.evidence import fact_store
    from exoma.core.provider.executor import Executor
    from exoma.core.runtime.state import ensure_workflow_workspace
    from tests.checks.continuation import _EVIDENCE, _ScriptedAdapter, _session

    root = Path(tempfile.mkdtemp(prefix="aw-served-facts-"))
    saved = executor_module.write_call_meta
    written: list[dict] = []
    executor_module.write_call_meta = lambda _root, _pid, _sid, meta: written.append(meta)
    try:
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
        (root / "store.py").write_text("RETRY_LIMIT = 3\n", encoding="utf-8")
        fact_store.ingest(root, "durable_facts:\n- [config] `RETRY_LIMIT` is three [store.py:1]\n", "s0")
        Executor(adapter=_ScriptedAdapter([_EVIDENCE])).execute(
            "analyze", "why is `RETRY_LIMIT` three", _session(), str(root)
        )
        served = (written[-1] if written else {}).get("served_facts")
        assert_true(
            isinstance(served, list) and len(served) == 1
            and served[0]["file"] == "store.py" and served[0]["line"] == 1
            and len(served[0]["key"]) == 16,
            f"the analyze call archives the fact it was served: {served}",
        )
        written.clear()
        Executor(adapter=_ScriptedAdapter([_EVIDENCE])).execute(
            "analyze", "unrelated wording only", _session(), str(root)
        )
        assert_true(
            (written[-1] if written else {}).get("served_facts") == [],
            f"a call served nothing records an empty list, not a missing field: {written}",
        )
    finally:
        executor_module.write_call_meta = saved
        shutil.rmtree(root, ignore_errors=True)


def _test_telemetry_metrics() -> None:
    _call_meta_names_the_facts_it_served()
    _fact_lifecycle_counts_commands_not_invocations()
    _acceptance_is_per_task()
    _rework_counts_work_that_came_back_after_a_failure()
    _provider_less_browser_runs_are_not_calls()
    _unjudged_work_is_not_incorrect()
    _report_reports_its_denominators()
    _evidence_reuse_counts_commands_it_could_serve()
    _reuse_misses_are_broken_down_by_reason()
    _provider_cache_share_splits_continuations()
    _graph_leads_reports_calls_that_ran_during_a_refresh()
    _provider_threads_flag_lost_sessions()
    _by_effort_splits_commands_and_skips_old_rows()
    _graph_refresh_reports_what_the_turn_paid()
    _torn_row_is_skipped_not_fatal()
    _torn_multibyte_row_is_skipped_not_fatal()
