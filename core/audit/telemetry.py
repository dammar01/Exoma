"""Aggregation over the usage stream. Reads history, never writes it.

Every metric here is DERIVED at read time from `UsageRecord` rows rather than counted as
calls happen. That is the whole design: a counter incremented at runtime freezes one
definition of a metric forever, and the definitions in this file are exactly the ones
worth being able to change later — "accepted", "rework", and "first-pass correct" are
judgement calls, not facts. Re-deriving means a better definition re-reads history
instead of invalidating it.

Every metric also reports its own denominator. A rate computed over four calls and a rate
computed over four hundred are different claims, and a dashboard that shows only the
percentage lets the reader mistake the first for the second.
"""

import json
from pathlib import Path

from core.evidence.contracts import (
    QUALITY_STREAM_NAME,
    USAGE_STREAM_NAME,
    UsageRecord,
    billable_input,
    billable_output,
)
from core.workspace.workspace_paths import data_dir


def _stream_path(project_root, name: str) -> Path:
    return data_dir(Path(project_root)) / name


def load_usage(project_root) -> list[UsageRecord]:
    """Every usage row on disk, oldest first. Unreadable rows are skipped, not fatal.

    A stream is append-only and written fail-open, so a torn final line is a realistic
    state — a process killed mid-write. Dropping that one row is right; refusing to
    report anything because of it is not.
    """
    path = _stream_path(project_root, USAGE_STREAM_NAME)
    rows: list[UsageRecord] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            rows.append(UsageRecord.from_dict(payload))
    return rows


def load_quality(project_root) -> list[dict]:
    """Recorded check outcomes (tests, security, e2e, graph refresh), oldest first."""
    path = _stream_path(project_root, QUALITY_STREAM_NAME)
    rows: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 2)


def _by_correlation(rows: list[UsageRecord]) -> dict[str, list[UsageRecord]]:
    grouped: dict[str, list[UsageRecord]] = {}
    for row in rows:
        if row.correlation_id:
            grouped.setdefault(row.correlation_id, []).append(row)
    return grouped


def _cost(rows: list[UsageRecord]) -> dict:
    """Token spend, kept separate from any currency conversion.

    No price table here on purpose. Prices change per provider, per model, and per month;
    baking one in would make every historical figure silently wrong the day it moved. The
    denominator a cost-per-task question actually needs is tokens, and multiplying that by
    today's rate is a decision for whoever reads the report.
    """
    inputs = [billable_input(row) for row in rows]
    outputs = [billable_output(row) for row in rows]
    sources = {row.token_source for row in rows if row.token_source}
    # Reported beside the totals, never inside them. Reasoning is already counted in
    # output_tokens, so this is the answer to "how much of that was thinking", not an
    # amount to add to it. A history with no measured reasoning reports None rather than
    # zero: no provider having told us is not the same as a provider telling us none.
    reasoning = [
        row.actual_reasoning_tokens for row in rows if row.actual_reasoning_tokens is not None
    ]
    return {
        "input_tokens": sum(inputs),
        "output_tokens": sum(outputs),
        "total_tokens": sum(inputs) + sum(outputs),
        # A breakdown OF output_tokens above, not an addend to it.
        "reasoning_tokens_within_output": sum(reasoning) if reasoning else None,
        "measured_calls": sum(
            1
            for row in rows
            if row.actual_input_tokens is not None or row.actual_output_tokens is not None
        ),
        # `estimated` means chars//4, not a provider count. A cost figure that cannot
        # tell an estimate from a measurement is not a cost figure.
        "token_source": sorted(sources) or ["unknown"],
    }


def accepted_tasks(rows: list[UsageRecord]) -> dict:
    """Distinct pieces of work whose verification passed.

    Counted per correlation_id, not per call: a task verified twice is one accepted task,
    and counting calls would reward re-running verify until it went green.
    """
    judged: set[str] = set()
    accepted: set[str] = set()
    for correlation, group in _by_correlation(rows).items():
        verdicts = [row.accepted for row in group if row.accepted is not None]
        if not verdicts:
            continue
        judged.add(correlation)
        if any(verdicts):
            accepted.add(correlation)
    return {
        "accepted": len(accepted),
        "judged": len(judged),
        "accepted_ids": sorted(accepted),
    }


def first_pass_correctness(rows: list[UsageRecord]) -> dict:
    """Share of judged tasks that passed on their FIRST verification.

    The distinction from plain acceptance is the entire point: a task that failed twice
    and passed on the third attempt is accepted, and it is not first-pass correct. Only
    tasks that were judged at all appear in the denominator — never-verified work is
    unknown, not incorrect.
    """
    first_pass = 0
    judged = 0
    for group in _by_correlation(rows).values():
        verdicts = [row for row in group if row.accepted is not None]
        if not verdicts:
            continue
        judged += 1
        if verdicts[0].accepted:
            first_pass += 1
    return {
        "first_pass": first_pass,
        "judged": judged,
        "rate": round(first_pass / judged, 3) if judged else None,
    }


def rework(rows: list[UsageRecord]) -> dict:
    """Tasks whose work came back after a verify that did not pass (DEC-051).

    A group is reworked when a judged round that did not pass (`fail` or `incomplete`, both
    `accepted=False`) is followed by another judged round; the extra rounds are the ones
    after that first failure. Rounds that all pass are not rework: a plan built in phases
    verifies each phase under one correlation id, and counting those rounds read planned
    work as work that came back. A failure with nothing after it is not rework either: the
    work never came back, and first_pass_correctness already counts it.

    Read together with first_pass_correctness rather than instead of it: this counts how
    often work came back, that one counts how often it came back green the first time.
    """
    reworked = 0
    judged = 0
    extra_rounds = 0
    for group in _by_correlation(rows).values():
        verdicts = [row for row in group if row.accepted is not None]
        if not verdicts:
            continue
        judged += 1
        # Stable sort: rows without a timestamp keep their append order.
        verdicts.sort(key=lambda row: row.recorded_at or "")
        first_failure = next((i for i, row in enumerate(verdicts) if row.accepted is False), None)
        if first_failure is None:
            continue
        after = len(verdicts) - 1 - first_failure
        if after:
            reworked += 1
            extra_rounds += after
    return {
        "reworked_tasks": reworked,
        "judged": judged,
        "extra_verification_rounds": extra_rounds,
        "rate": round(reworked / judged, 3) if judged else None,
    }


def security_pass_rate(rows: list[UsageRecord]) -> dict:
    """Share of delegated calls whose output carried nothing credential-shaped.

    Measures the redaction boundary firing, which is a weaker claim than "no secret
    leaked" and is stated as such: a call with zero redactions is a call where the
    scanner found nothing, not a proof that nothing was there.
    """
    # Per command, not per row. A continuation writes two rows for one delegated call,
    # and counting them separately would move this rate whenever a retry happened —
    # a security figure drifting on an unrelated event. A command is clean only if
    # nothing was redacted from any of the replies that went into it.
    groups = _work_groups(rows)
    total = len(groups)
    clean = sum(1 for group in groups if not any(row.redactions for row in group))
    return {
        "clean_calls": clean,
        "total_calls": total,
        "rate": round(clean / total, 3) if total else None,
    }


def test_pass_rate(project_root) -> dict:
    """Recorded outcomes of the test suite, from the quality stream.

    Separate from the usage stream because it measures a different actor: usage rows are
    delegated calls the runtime made, quality rows are check runs someone (CI, a person)
    performed on the repo. Fusing them would let a green test run inflate the count of
    delegated work.
    """
    rows = [row for row in load_quality(project_root) if row.get("kind") == "tests"]
    total = len(rows)
    passed = sum(1 for row in rows if row.get("ok"))
    return {
        "passed_runs": passed,
        "total_runs": total,
        "rate": round(passed / total, 3) if total else None,
        # An empty stream is not a failing one. Saying so stops a fresh workspace from
        # reading as a repo whose tests never pass.
        "recorded": bool(total),
    }


E2E_RUN_KINDS = ("project", "smoke", "fake")


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def _e2e_summary(runs: list[dict], usage_by_prompt: dict[str, list[UsageRecord]]) -> dict:
    """Plan §24 over one run kind. Every rate carries the count it was taken over."""
    total = len(runs)
    verdicts = {v: sum(1 for r in runs if r.get("verdict") == v) for v in ("pass", "fail", "incomplete")}
    reasons: dict[str, int] = {}
    for run in runs:
        if run.get("verdict") == "incomplete":
            key = run.get("reason") or "unspecified"
            reasons[key] = reasons.get(key, 0) + 1
    claims = sum((run.get("claims") or {}).get("total") or 0 for run in runs)
    browser_runs = sum(run.get("browser_runs") or 0 for run in runs)
    screenshots = sum((run.get("artifacts") or {}).get("screenshots") or 0 for run in runs)
    sent = sum(run.get("screenshots_sent_to_model") or 0 for run in runs)

    tokens: list[float] = []
    unmeasured = 0
    for run in runs:
        joined = [row for pid in run.get("provider_prompt_ids") or [] for row in usage_by_prompt.get(pid, [])]
        if joined:
            tokens.append(float(sum(billable_input(row) + billable_output(row) for row in joined)))
        elif run.get("provider_calls"):
            unmeasured += 1  # it called a provider, but the usage rows are gone or unjoined
        else:
            tokens.append(0.0)  # spec_path or replay, no reviewer: genuinely free
    durations = [float(run["duration_seconds"]) for run in runs if isinstance(run.get("duration_seconds"), (int, float))]

    # Reproducibility: the same scenario run more than once should reach the same browser
    # verdict. Only scenarios that were actually repeated are a denominator.
    by_scenario: dict[str, list] = {}
    for run in runs:
        if run.get("scenario_hash"):
            by_scenario.setdefault(run["scenario_hash"], []).append(run.get("browser_verdict"))
    repeated = [set(v) for v in by_scenario.values() if len(v) > 1]
    stable = sum(1 for verdict_set in repeated if len(verdict_set) == 1)

    harness_runs = sum(1 for run in runs if (run.get("failure_origins") or {}).get("harness"))
    unknown_runs = sum(1 for run in runs if (run.get("failure_origins") or {}).get("unknown"))
    return {
        "runs": total,
        "verdicts": verdicts,
        "incomplete_rate": _rate(verdicts["incomplete"], total),
        "incomplete_by_reason": dict(sorted(reasons.items())),
        # False failures need a human judgement per failed run; what the stream can say is
        # how often the harness, or nobody, was to blame. Those are the candidates.
        "harness_failure_rate": _rate(harness_runs, total),
        "unknown_origin_rate": _rate(unknown_runs, total),
        "claims": claims,
        "browser_runs_per_claim": _rate(browser_runs, claims),
        "probes_per_run": _mean([float(run.get("probes") or 0) for run in runs]),
        "screenshots_kept": screenshots,
        "screenshot_analysis_rate": _rate(sent, screenshots),
        "tokens_per_run": {
            "mean": _mean(tokens),
            "median": _median(tokens),
            "measured_runs": len(tokens),
            "unmeasured_runs": unmeasured,
        },
        "duration_seconds": {"mean": _mean(durations), "median": _median(durations)},
        "reproducibility": {"repeated_scenarios": len(repeated), "stable": stable, "rate": _rate(stable, len(repeated))},
        "replayed_runs": sum(1 for run in runs if run.get("replay_used")),
        "existing_test_runs": sum(1 for run in runs if run.get("existing_tests")),
    }


def _draft_summary(drafts: list[dict]) -> dict:
    """Stage-1 outcomes: how many drafts came back ready, and what the rest got wrong.

    `first_error` counts the category of the first reply's first error — what CASE-005
    tabulated by hand — and `categories` every error still standing after the repair.
    Raw counts plus one rate, so a later definition re-reads history.
    """
    first: dict[str, int] = {}
    remaining: dict[str, int] = {}
    for draft in drafts:
        if draft.get("first_error"):
            first[draft["first_error"]] = first.get(draft["first_error"], 0) + 1
        for name, count in (draft.get("categories") or {}).items():
            remaining[name] = remaining.get(name, 0) + int(count or 0)
    statuses = [d.get("status") for d in drafts]
    repaired = [d for d in drafts if d.get("repair_attempted")]
    return {
        "drafts": len(drafts),
        "ready": statuses.count("ready"),
        "invalid": statuses.count("invalid"),
        "blocked": statuses.count("blocked"),
        "ready_rate": _rate(statuses.count("ready"), len(drafts)),
        "repair_attempted": len(repaired),
        "repair_recovered": sum(1 for d in repaired if d.get("repair_recovered")),
        "first_error": dict(sorted(first.items(), key=lambda item: (-item[1], item[0]))),
        "categories": dict(sorted(remaining.items(), key=lambda item: (-item[1], item[0]))),
        "secret_literal_drafts": sum(1 for d in drafts if d.get("secret_literals")),
    }


def e2e_metrics(project_root, rows: list[UsageRecord] | None = None) -> dict:
    """/.verify-browser runs from the quality stream, split by run kind before anything is
    aggregated.

    A fake-player run proves the pipeline and a smoke run proves the browser; only a
    project run says anything about value. Pooling them would let the test suite's own
    runs pass for evidence that e2e verification works on real changes.

    The baseline is delegated verification in the same workspace: `verify` usage rows that
    belong to no e2e run. There is no visual-browser-loop baseline to compare against
    here, and the report says so instead of inventing one (plan acceptance #13).
    """
    rows = load_usage(project_root) if rows is None else rows
    runs = [row for row in load_quality(project_root) if row.get("kind") == "e2e_run"]
    usage_by_prompt: dict[str, list[UsageRecord]] = {}
    for row in rows:
        if row.prompt_id:
            usage_by_prompt.setdefault(row.prompt_id, []).append(row)
    e2e_prompts = {pid for run in runs for pid in run.get("provider_prompt_ids") or []}
    summary = {kind: _e2e_summary([r for r in runs if r.get("run_kind") == kind], usage_by_prompt) for kind in E2E_RUN_KINDS}

    baseline = _work_groups([
        row for row in rows
        if row.command == "verify"
        and row.e2e_stage is None
        and row.prompt_id not in e2e_prompts
        and not provider_less_browser_run(row)
    ])
    baseline_tokens = [float(sum(billable_input(r) + billable_output(r) for r in group)) for group in baseline]
    baseline_durations = [
        float(sum(r.duration_seconds for r in group if r.duration_seconds is not None))
        for group in baseline
        if any(r.duration_seconds is not None for r in group)
    ]
    project_tokens = summary["project"]["tokens_per_run"]["mean"]
    baseline_mean = _mean(baseline_tokens)
    drafts = [row for row in load_quality(project_root) if row.get("kind") == "e2e_draft"]
    return {
        "runs": len(runs),
        "by_run_kind": summary,
        "drafts": {kind: _draft_summary([d for d in drafts if d.get("run_kind") == kind]) for kind in E2E_RUN_KINDS},
        "delegated_verify_baseline": {
            "verifications": len(baseline),
            "tokens_per_verification": {"mean": baseline_mean, "median": _median(baseline_tokens)},
            "duration_seconds": {"mean": _mean(baseline_durations), "median": _median(baseline_durations)},
        },
        "project_vs_baseline_token_ratio": (
            round(project_tokens / baseline_mean, 2) if project_tokens is not None and baseline_mean else None
        ),
        "visual_browser_loop_baseline": None,
        "labels": sorted({run["label"] for run in runs if run.get("label")}),
    }


def _work_groups(rows) -> list[list]:
    """Rows gathered into the pieces of work they belong to.

    A continuation writes one row per provider invocation, all sharing the command's
    `prompt_id`. A reuse hit has none — it never built a prompt — so each is its own unit
    rather than collapsing with every other id-less row into one.

    One grouping, used by every metric that counts work rather than invocations, so those
    metrics cannot drift apart from each other the way they drifted from `calls`.
    """
    groups: dict[object, list] = {}
    for index, row in enumerate(rows):
        groups.setdefault(row.prompt_id or ("row", index), []).append(row)
    return list(groups.values())


def _work_units(rows) -> int:
    """How many pieces of work these rows represent, not how many rows there are."""
    return len(_work_groups(rows))


def graph_refresh(project_root) -> dict:
    """What the graph-refresh Stop hook cost, from its rows in the quality stream (DEC-012).

    `hook_ms` is what the turn paid; `graphify_ms` ran detached after it. A refresh counts
    as done when graph.json was rewritten, whatever graphify's exit code. The hook writes
    no row for a turn that implemented nothing, so these are implementing turns only.
    """
    rows = [row for row in load_quality(project_root) if row.get("kind") == "graph_refresh"]
    outcomes: dict[str, int] = {}
    for row in rows:
        outcome = str(row.get("outcome") or "unknown")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    def _values(key, subset):
        return [row[key] for row in subset if isinstance(row.get(key), (int, float))]

    refreshes = [row for row in rows if "graphify_ms" in row]
    return {
        "runs": len(rows),
        "by_outcome": dict(sorted(outcomes.items())),
        "hook_ms_median": _median(_values("hook_ms", rows)),
        "scan_ms_median": _median(_values("scan_ms", rows)),
        "graphify_ms_median": _median(_values("graphify_ms", refreshes)),
        "graphify_ms_max": max(_values("graphify_ms", refreshes), default=None),
    }


_REUSE_ROLES = ("exploration", "reasoning")


def evidence_reuse(rows) -> dict:
    """How often a reuse-eligible command was served from a stored artifact.

    Counted in work units (a continuation is one command), over the roles the executor
    offers reuse to. A row with no recorded role cannot be placed and is named, not
    guessed from its command. Provider cached input is a different thing and is not
    counted here: a reuse hit never reaches a provider.
    """
    # A verify-browser draft takes the exploration route but is never offered reuse.
    eligible = [
        row for row in rows if row.role in _REUSE_ROLES and row.command != "verify-browser"
    ]
    groups = _work_groups(eligible)
    reused = sum(1 for group in groups if any(row.reused_evidence for row in group))
    # One outcome per command. Rows written before contract v3 carry none and are counted
    # as `unrecorded`, so the breakdown never claims more history than it has.
    outcomes: dict[str, int] = {}
    for group in groups:
        recorded = next((row.reuse_outcome for row in group if row.reuse_outcome), None)
        key = recorded or "unrecorded"
        outcomes[key] = outcomes.get(key, 0) + 1
    return {
        "reused": reused,
        "eligible_commands": len(groups),
        "rate": round(reused / len(groups), 3) if groups else None,
        "by_outcome": dict(sorted(outcomes.items())),
        "role_unrecorded_rows": sum(1 for row in rows if row.role is None),
    }


def provider_cache(rows) -> dict:
    """Share of measured input the provider served from its own prompt cache (H-008).

    Per provider invocation, not per command: caching happens per request. Only rows where
    the provider reported both counts enter the share. Two splits: a continuation resends
    the first call's prefix, and a resumed thread resends the whole thread so far. A row
    whose adapter did not say whether it resumed is in neither thread bucket.
    """

    def _share(subset):
        measured = [
            row for row in subset
            if row.actual_input_tokens and row.actual_cached_input_tokens is not None
        ]
        total = sum(row.actual_input_tokens for row in measured)
        cached = sum(row.actual_cached_input_tokens for row in measured)
        return {
            "measured_calls": len(measured),
            "input_tokens": total,
            "cached_input_tokens": cached,
            "share": round(cached / total, 3) if total else None,
        }

    def _is_continuation(row):
        return isinstance(row.provider_call_index, int) and row.provider_call_index > 0

    continuation = [row for row in rows if _is_continuation(row)]
    first = [row for row in rows if not _is_continuation(row)]
    return {
        "all": _share(rows),
        "first_call": _share(first),
        "continuation": _share(continuation),
        "fresh_thread": _share([row for row in rows if row.provider_resumed is False]),
        "resumed_thread": _share([row for row in rows if row.provider_resumed is True]),
    }


def provider_threads(rows) -> dict:
    """Whether provider sessions stay stateful, per provider, per invocation.

    `thread_changed` is the alarm: a resumed call answered on a different thread, so the
    session silently lost what it held. `unknown` is an adapter that does not report it
    (opencode), never counted as fresh. Rows before contract v3 are left out, as in
    `by_effort`: none of them could report it, and counting them would bury the adapters
    that do not under history.
    """
    out: dict[str, dict] = {}
    for row in rows:
        if (row.contract_version or 0) < 3:
            continue
        entry = out.setdefault(
            row.provider or "unknown",
            {"resumed": 0, "fresh": 0, "unknown": 0, "thread_changed": 0},
        )
        if row.provider_resumed is True:
            entry["resumed"] += 1
        elif row.provider_resumed is False:
            entry["fresh"] += 1
        else:
            entry["unknown"] += 1
        if row.provider_thread_changed:
            entry["thread_changed"] += 1
    return dict(sorted(out.items()))


def by_effort(rows) -> dict:
    """Time and tokens per command, split by the effort the adapter passed (H-010).

    Per command (a continuation is one), keyed `<command>/<effort>`; `default` is a call
    that sent no effort flag. `commands` counts every command in the bucket; each median
    only those where the value was measured, input and output tokens apart, so an adapter
    that reports one and not the other never reads as zero. Rows before contract v3 are
    left out.
    """
    buckets: dict[str, dict] = {}
    for group in _work_groups(rows):
        head = group[0]
        # Before contract v3 no effort was recorded; those rows would all read `default`.
        if (head.contract_version or 0) < 3:
            continue
        key = f"{head.command}/{head.effort or 'default'}"
        bucket = buckets.setdefault(key, {"commands": 0, "durations": [], "input": [], "output": []})
        bucket["commands"] += 1
        for field, name in (("duration_seconds", "durations"), ("actual_input_tokens", "input"), ("actual_output_tokens", "output")):
            measured = [getattr(row, field) for row in group if getattr(row, field) is not None]
            if measured:
                bucket[name].append(sum(measured))
    return {
        key: {
            "commands": bucket["commands"],
            "duration_seconds_median": _median(bucket["durations"]),
            "input_tokens_median": _median(bucket["input"]),
            "output_tokens_median": _median(bucket["output"]),
        }
        for key, bucket in sorted(buckets.items())
    }


def graph_leads(rows) -> dict:
    """How delegated calls found the graph, and what the lookup cost (RQ-13, DEC-012).

    Counted per command. `refreshing` is a call that ran while the refresh worker held the
    lock and so went without leads; for a verify that is the case the latency question is
    about.
    """
    groups = _work_groups(rows)
    statuses: dict[str, int] = {}
    refreshing_by_command: dict[str, int] = {}
    lookup_ms = []
    for group in groups:
        head = next((row for row in group if row.graph_status), None)
        status = head.graph_status if head else "unrecorded"
        statuses[status] = statuses.get(status, 0) + 1
        if head is None:
            continue
        if status == "refreshing":
            refreshing_by_command[head.command] = refreshing_by_command.get(head.command, 0) + 1
        if isinstance(head.graph_ms, (int, float)):
            lookup_ms.append(head.graph_ms)
    return {
        "by_status": dict(sorted(statuses.items())),
        "refreshing_by_command": dict(sorted(refreshing_by_command.items())),
        "lookup_ms_median": _median(lookup_ms),
        "lookup_ms_max": max(lookup_ms, default=None),
    }


def task_report(project_root) -> dict:
    """DEC-015 tasks: by state, their skill sequences, and commits no task claimed."""
    from core.audit import task_telemetry

    return task_telemetry.report(project_root)


def provider_less_browser_run(row: UsageRecord) -> bool:
    """A row written before contract v4 for a browser run that reached no provider.

    A run that ended before its review (incomplete, preflight, refused spec) still wrote a
    `verify` row with no prompt id, no provider and no tokens. It was not a call: it read
    as an extra second-agent call, a judged verify, and a zero-token baseline verification.
    Every ordinary verify has a prompt id, so the shape is unambiguous. v4 writes none.
    """
    return (
        row.command == "verify"
        and row.prompt_id is None
        and row.provider is None
        and not row.reused_evidence
        and row.actual_input_tokens is None
        and row.estimated_input_tokens is None
    )


def report(project_root) -> dict:
    """Every P1 metric, over the whole recorded history of this project."""
    rows = [row for row in load_usage(project_root) if not provider_less_browser_run(row)]
    # Time to complete a COMMAND, so a continuation's two invocations add up rather than
    # being averaged as two independent calls. The user waited for their sum; a mean over
    # the parts reports a wait nobody had, and reports it as shorter.
    durations = []
    unmeasured_durations = 0
    for group in _work_groups(rows):
        measured = [row.duration_seconds for row in group if row.duration_seconds is not None]
        if measured:
            durations.append(sum(measured))
        else:
            unmeasured_durations += 1
    avoided = [
        row.premium_context_avoided_tokens
        for row in rows
        if row.premium_context_avoided_tokens is not None
    ]
    acceptance = accepted_tasks(rows)
    cost = _cost(rows)

    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.command, []).append(row)

    accepted_count = acceptance["accepted"]
    return {
        # Commands, as it has always meant. One command is one call here even when a
        # continuation ran the adapter twice for it.
        #
        # `usage.jsonl` used to hold one row per command, so `len(rows)` and this were the
        # same number and the distinction never had to be made. Now that a continuation
        # writes a row per invocation they diverge, and this key is read outside the
        # repository — `main.py --command report` prints it. A published number whose
        # definition moves underneath its name is worse than a new name: nothing errors,
        # the figure simply starts meaning something else.
        "calls": _work_units(rows),
        # The new quantity, under a name that says what it counts.
        "provider_calls": len(rows),
        # Kept as an alias of `calls` for readers written against the interim shape.
        "commands": _work_units(rows),
        "calls_by_command": {
            command: _work_units(group) for command, group in sorted(grouped.items())
        },
        "cost": cost,
        "accepted_tasks": {
            "accepted": accepted_count,
            "judged": acceptance["judged"],
            # The headline P1 number. `None` rather than a large-looking figure when
            # nothing has been accepted yet: dividing by zero accepted tasks does not
            # produce an expensive project, it produces no answer.
            "tokens_per_accepted_task": (
                round(cost["total_tokens"] / accepted_count) if accepted_count else None
            ),
        },
        "premium_context_avoided": {
            "total_tokens": sum(avoided),
            "measured_calls": len(avoided),
            "provider_calls_avoided": sum(1 for row in rows if row.provider_call_avoided),
        },
        "evidence_reuse": evidence_reuse(rows),
        "provider_cache": provider_cache(rows),
        "provider_threads": provider_threads(rows),
        "by_effort": by_effort(rows),
        "graph_leads": graph_leads(rows),
        "time_to_completion_seconds": {
            "mean": _mean(durations),
            "median": _median(durations),
            "measured_calls": len(durations),
            # Unmeasured calls are named, not averaged away. A mean over a third of the
            # calls is a different claim from a mean over all of them.
            "unmeasured_calls": unmeasured_durations,
        },
        "first_pass_correctness": first_pass_correctness(rows),
        "rework": rework(rows),
        "security": security_pass_rate(rows),
        "tests": test_pass_rate(project_root),
        "e2e": e2e_metrics(project_root, rows),
        "graph_refresh": graph_refresh(project_root),
        # Its own stream: tasks span local skills and commits, which are not usage rows.
        "tasks": task_report(project_root),
        # Which metric definitions, tool commit and inputs produced every figure above
        # (core/audit/metrics.py): a figure is cited as <metric id>@<version>.
        "provenance": _report_provenance(project_root),
    }


def _report_provenance(project_root) -> dict:
    from core.audit import metrics
    from core.evidence.contracts import TASK_STREAM_NAME

    streams = [USAGE_STREAM_NAME, QUALITY_STREAM_NAME, TASK_STREAM_NAME]
    return metrics.provenance(
        "core/audit/telemetry.py report()",
        {},
        [_stream_path(project_root, name) for name in streams],
        metric_ids=[mid for mid in metrics.REGISTRY if mid.startswith("runtime.")],
    )
