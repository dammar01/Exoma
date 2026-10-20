"""The five workflow contracts, and the usage stream they feed.

Two things are worth proving here and they are not the same thing. The first is that the
dataclasses round-trip — cheap, and the reason `from_dict` can be trusted at a version
boundary. The second is that `usage_from_result` DERIVES the right things, which is where
a telemetry bug would actually live: a metric that is quietly wrong reads exactly like a
metric that is right, and nothing downstream can tell the difference.

So the derivation assertions are deliberately about the distinctions that would be
invisible if broken: `accepted=None` versus `accepted=False`, an absent duration versus a
zero one, and a reuse hit being recorded at all.
"""

from core.workspace.workspace_paths import data_dir
import json
import shutil
import tempfile
from pathlib import Path

from core.evidence.contracts import (
    CONTRACT_VERSION,
    EvidenceBundle,
    RouteDecision,
    TaskSpec,
    UsageRecord,
    VerificationReport,
    correlation_id_for,
    usage_from_result,
)
from core.provider.executor import Executor
from core.prompt.router import Router
from core.evidence.runtime_io import write_usage_record
from core.runtime.state import ensure_workflow_workspace
from tests.checks.support import assert_true


def _round_trips() -> None:
    spec = TaskSpec.build("EXPLORE", "  find the router  ", "sid-1", "/repo", model="m")
    assert_true(
        spec.command == "explore",
        "TaskSpec.build must normalise the command; routing and the usage stream key off it",
    )
    assert_true(
        TaskSpec.from_dict(spec.to_dict()) == spec,
        "TaskSpec must survive a dict round trip",
    )

    route = RouteDecision.from_dict(Router().route("explore"))
    assert_true(
        route.role == "exploration" and route.command == "explore",
        "RouteDecision must absorb a real Router.route() payload, not a hand-made one",
    )
    assert_true(
        RouteDecision.from_dict(route.to_dict()) == route,
        "RouteDecision must survive a dict round trip",
    )

    bundle = EvidenceBundle(artifact_path="a.md", anchors=3, reused=True)
    assert_true(
        EvidenceBundle.from_dict(bundle.to_dict()) == bundle,
        "EvidenceBundle must survive a dict round trip",
    )

    report = VerificationReport.from_dict(
        {"verdict": "pass", "declared_verdict": "DONE", "checks_run": 4}
    )
    assert_true(
        report.passed and report.checks_run == 4,
        "VerificationReport.passed must read the DERIVED verdict, not the declared one",
    )
    assert_true(
        not VerificationReport.from_dict({"verdict": "incomplete"}).passed,
        "an incomplete verification is not a pass — accepted-task accounting depends on it",
    )

    # Unknown keys are dropped, not raised on. An archived record written by a later
    # version must stay readable by the aggregator, or history breaks on every release.
    grown = UsageRecord().to_dict()
    grown["a_field_from_the_future"] = 1
    assert_true(
        UsageRecord.from_dict(grown).contract_version == CONTRACT_VERSION,
        "from_dict must ignore unknown keys instead of failing on a newer record",
    )


def _correlation_scoping() -> None:
    same = correlation_id_for("/repo", "sid-1", "Add the thing")
    assert_true(
        same == correlation_id_for("/repo", "sid-1", "  add   the thing  "),
        "correlation must survive whitespace and case, or rework counts as new work",
    )
    assert_true(
        same != correlation_id_for("/repo", "sid-2", "Add the thing"),
        "the same task in another session is a fresh attempt, not rework of the old one",
    )
    assert_true(
        same != correlation_id_for("/other", "sid-1", "Add the thing"),
        "correlation must not cross project roots",
    )


def _correlation_chain() -> None:
    """A plan and the verify that follows it must land in the usage stream as ONE task.

    This is the aggregation the P1 metrics group by: before the chain hop, plan and
    verify derived ids from their own task texts, so the same piece of work produced
    three subjects and "right on the first try" was true of none of them.
    """
    assert_true(
        TaskSpec.build("verify", "t", "sid", "/repo", correlation_id="chain-1").correlation_id
        == "chain-1",
        "an explicit correlation id must win over derivation — the chain depends on it",
    )
    assert_true(
        TaskSpec.build("verify", "t", "sid", "/repo", correlation_id=None).correlation_id
        == correlation_id_for("/repo", "sid", "t"),
        "no explicit id must fall back to deriving exactly as before",
    )

    root = Path(tempfile.mkdtemp(prefix="aw-chain-"))
    try:
        ensure_workflow_workspace(root, str(Path("main.py").resolve()))
        executor = Executor()
        executor._last_call_meta = None
        executor._finalize_runtime_result(
            {"ok": True, "content": "plan body", "meta": {}},
            root, "plan", "build feature X", "sid-1", {"session_reset": False}, False,
        )
        executor._finalize_runtime_result(
            {"ok": True, "content": _VERIFICATION, "meta": {}},
            root, "verify", "check the feature X work", "sid-1",
            {"session_reset": False}, False,
        )
        rows = [
            json.loads(line)
            for line in (data_dir(root) / "usage.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        plan_row = next(row for row in rows if row["command"] == "plan")
        verify_row = next(row for row in rows if row["command"] == "verify")
        assert_true(
            plan_row["correlation_id"] == verify_row["correlation_id"],
            "a verify following a plan must adopt the plan's correlation id — "
            "different task texts were exactly what broke the aggregation",
        )
        assert_true(
            plan_row["correlation_id"]
            == correlation_id_for(root, "sid-1", "build feature X"),
            "the chain's identity must be the PLAN's derived id, so history and "
            "derivation still agree on what the task is",
        )

        # Another session has no chain: verify there still derives its own id.
        executor._finalize_runtime_result(
            {"ok": True, "content": _VERIFICATION, "meta": {}},
            root, "verify", "unrelated check", "sid-2",
            {"session_reset": False}, False,
        )
        rows = [
            json.loads(line)
            for line in (data_dir(root) / "usage.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        lone = next(row for row in rows if row["session_id"] == "sid-2")
        assert_true(
            lone["correlation_id"] == correlation_id_for(root, "sid-2", "unrelated check"),
            "a chainless verify must fall back to derivation, never inherit another "
            "session's chain",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _usage_derivation() -> None:
    verify_pass = usage_from_result(
        {"ok": True, "content": "x" * 400, "meta": {"verdict": "pass"}},
        spec=TaskSpec.build("verify", "t", "sid", "/repo"),
        call_meta={"role": "verification", "model": "m", "response_chars": 400,
                   "duration_seconds": 12.5, "token_source": "estimated"},
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    assert_true(
        verify_pass.accepted is True,
        "a verify whose derived verdict is pass is the definition of an accepted task",
    )

    verify_fail = usage_from_result(
        {"ok": True, "content": "x", "meta": {"verdict": "fail"}},
        spec=TaskSpec.build("verify", "t", "sid", "/repo"),
        call_meta=None,
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    assert_true(
        verify_fail.accepted is False,
        "a judged-and-rejected task must be False, never None",
    )

    explore = usage_from_result(
        {"ok": True, "content": "x" * 1000, "digest": {"summary": "s" * 100}},
        spec=TaskSpec.build("explore", "t", "sid", "/repo"),
        call_meta={"response_chars": 1000},
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    assert_true(
        explore.accepted is None,
        "a command that never goes through verify is unjudged, not rejected — "
        "collapsing the two is what makes cost-per-accepted-task dishonest",
    )
    assert_true(
        explore.premium_context_avoided_tokens == (1000 - 100) // 4,
        "premium context avoided is the output the digest stood in for, in tokens",
    )
    assert_true(
        explore.duration_seconds is None,
        "an unrecorded duration must stay None; a zero would average into the report as fact",
    )

    reused = usage_from_result(
        {"ok": True, "content": "x" * 40, "meta": {"reused_evidence": True}},
        spec=TaskSpec.build("explore", "t", "sid", "/repo"),
        call_meta=None,
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    assert_true(
        reused.provider_call_avoided and reused.reused_evidence,
        "a reuse hit is a delegated call that cost nothing — omitting it overstates cost per task",
    )

    # Provider-reported counts travel through untouched. The assertion is deliberately on
    # the values being IDENTICAL to what was measured: any arithmetic applied on the way
    # here would be this layer inventing a number it is not entitled to invent.
    measured = usage_from_result(
        {"ok": True, "content": "x" * 40},
        spec=TaskSpec.build("explore", "t", "sid", "/repo"),
        call_meta={
            "token_source": "provider",
            "estimated_input_tokens": 10,
            "estimated_output_tokens": 10,
            "actual_input_tokens": 1200,
            "actual_output_tokens": 900,
            "actual_reasoning_tokens": 700,
            "actual_cached_input_tokens": 1000,
            "provider_call_index": 1,
        },
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    assert_true(
        measured.actual_input_tokens == 1200 and measured.actual_output_tokens == 900,
        "provider-reported token counts must reach the record unmodified",
    )
    assert_true(
        measured.actual_reasoning_tokens == 700
        and measured.actual_reasoning_tokens < measured.actual_output_tokens,
        "reasoning is a breakdown OF the output count, not a figure to add to it — a "
        "reasoning total exceeding the output it lives inside would mean the two were summed",
    )
    assert_true(
        measured.actual_cached_input_tokens == 1000
        and measured.actual_cached_input_tokens < measured.actual_input_tokens,
        "cached input is likewise a breakdown of the input count, never an addend",
    )
    assert_true(
        measured.estimated_output_tokens == 10,
        "the estimate stays on the row beside the measurement; dropping it would remove "
        "the only baseline that shows how far chars//4 was off",
    )
    assert_true(
        measured.provider_call_index == 1,
        "the invocation index distinguishes a continuation retry from the first attempt",
    )

    # The far more common row: a provider that reports nothing. Absent must stay absent
    # rather than becoming zero — an unmeasured count and a measured zero are different
    # facts, and only one of them belongs in an average.
    unmeasured = usage_from_result(
        {"ok": True, "content": "x" * 40},
        spec=TaskSpec.build("explore", "t", "sid", "/repo"),
        call_meta={"token_source": "estimated", "estimated_output_tokens": 10},
        recorded_at="2026-01-01T00:00:00+00:00",
    )
    assert_true(
        unmeasured.actual_output_tokens is None
        and unmeasured.actual_reasoning_tokens is None
        and unmeasured.provider_call_index is None,
        "a provider that reported no usage must leave the actual_* fields None, not zero",
    )


def _stream_append() -> None:
    root = Path(tempfile.mkdtemp(prefix="aw-contracts-"))
    try:
        write_usage_record(root, UsageRecord(command="explore").to_dict())
        write_usage_record(root, UsageRecord(command="verify").to_dict())
        path = data_dir(root) / "usage.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        assert_true(
            [row["command"] for row in rows] == ["explore", "verify"],
            "the usage stream is append-only; a rewritten history is not a measurement",
        )

        # Instrumentation must never fail the call it measures. A payload json cannot
        # serialise is the realistic version of that, so it is the one asserted.
        write_usage_record(root, {"command": "explore", "bad": object()})
        still = path.read_text(encoding="utf-8").splitlines()
        assert_true(
            len([line for line in still if line]) == 2,
            "an unserialisable record must be dropped silently, never partially written",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


_VERIFICATION = """[VERIFICATION]
verdict: DONE
blocking_findings: none
escalations: none
notes: none
checks_run:
- read core/router.py:48
not_verified: none
confidence: high
"""


def _verify_verdict_is_recorded() -> None:
    """The one ordering bug this wiring can have, asserted directly.

    `meta.verdict` is derived a layer above the executor, in job_lifecycle — so the usage
    record is assembled BEFORE it exists. Left unhandled, every verify lands with
    accepted=None and cost-per-accepted-task has no denominator at all: an empty metric
    that looks like a conservative one.

    Also asserts the result handed back is unchanged. `meta.verdict` belongs to
    job_lifecycle; measuring here must read the same validator without pre-empting the
    layer whose job it is to decide.
    """
    root = Path(tempfile.mkdtemp(prefix="aw-usage-"))
    try:
        ensure_workflow_workspace(root, str(Path("main.py").resolve()))
        executor = Executor()
        executor._last_call_meta = None
        result = {"ok": True, "content": _VERIFICATION, "meta": {}}
        returned = executor._finalize_runtime_result(
            result, root, "verify", "check it", "sid-1", {"session_reset": False}, False
        )
        row = json.loads(
            (data_dir(root) / "usage.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert_true(
            row["verdict"] == "pass" and row["accepted"] is True,
            "a clean verification must record accepted=True; the verdict is derived later, "
            "so reading meta alone would leave every verify unjudged",
        )
        assert_true(
            "verdict" not in (returned.get("meta") or {}),
            "measuring must not mutate the result — job_lifecycle owns the verdict field",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


_GAPPY_VERIFICATION = """[VERIFICATION]
verdict: DONE
blocking_findings: none
escalations: none
notes: none
checks_run:
- read core/router.py:48
not_verified:
- the delegated path, no provider credentials here
confidence: high
"""


def _finalize_is_idempotent() -> None:
    """Finalising the same verify payload twice must not double its warnings.

    It happens twice by design: the worker finalises what it produced, and `await`
    finalises the stored output again on the way back — it cannot assume the record it
    read was ever finalised. The old `extend` turned one real gap into two rows, which
    reads as two problems and makes a clean-ish verify look worse than it is.

    Warnings from other producers must survive the merge. `contract_warnings` also
    carries executor entries (evidence contract misses, task truncation), so a replace
    would fix the duplication by dropping them.
    """
    from core.evidence.result_shaping import _finalize_verify_result

    foreign = {"kind": "task_truncated", "detail": "120 chars cut from the instruction"}
    result = {
        "ok": True,
        "content": _GAPPY_VERIFICATION,
        "meta": {"contract_warnings": [dict(foreign)]},
    }
    once = _finalize_verify_result("verify", result)
    first = list(once["meta"]["contract_warnings"])
    assert_true(
        any(item["kind"] == "verification_gap" for item in first),
        "the declared gap must be reported at least once",
    )

    twice = _finalize_verify_result("verify", once)
    assert_true(
        twice["meta"]["contract_warnings"] == first,
        "a second finalisation must add nothing; got "
        f"{len(twice['meta']['contract_warnings'])} vs {len(first)}",
    )
    # Three passes, not two. Two is the count the bug produced, so a fix asserted only at
    # two passes proves the count changed rather than that it stopped growing — and the
    # `await`-of-a-`result`-of-a-retry path can finalise more than twice.
    thrice = _finalize_verify_result("verify", twice)
    assert_true(
        thrice["meta"]["contract_warnings"] == first,
        "warnings must stop growing, not merely grow more slowly",
    )

    # No pre-existing key at all: the first producer to touch a fresh payload.
    fresh = _finalize_verify_result(
        "verify", {"ok": True, "content": _GAPPY_VERIFICATION, "meta": {}}
    )
    fresh_warnings = list(fresh["meta"]["contract_warnings"])
    assert_true(
        fresh_warnings
        and _finalize_verify_result("verify", fresh)["meta"]["contract_warnings"]
        == fresh_warnings,
        "a payload with no contract_warnings key must gain them once and only once",
    )
    assert_true(
        foreign in twice["meta"]["contract_warnings"],
        "merging must keep warnings this function did not produce — a wholesale replace "
        "would silently drop the executor's own entries",
    )

    kinds = [item["kind"] for item in twice["meta"]["contract_warnings"]]
    assert_true(
        len(kinds) == len(set(kinds)) or kinds.count("verification_gap") == 1,
        f"no warning may appear twice after two passes; got {kinds}",
    )


def _confidence_caps_keep_their_reasons() -> None:
    """A cap explains itself even when it has no number left to change.

    `confidence_capped_by` is what main_agent reads to know what was WRONG with a run, not
    only to explain why a number moved. A run that graded itself `low` and then hit a stale
    dependency graph used to record neither: the value needed no change, so the reason was
    dropped along with it, and the one signal saying why the run was weak never arrived.
    """
    from core.evidence.contract import cap_confidence

    already_low = cap_confidence({"confidence": "low"}, [("medium", "dependency graph is stale")])
    assert_true(
        already_low["confidence"] == "low"
        and already_low["confidence_capped_by"] == ["dependency graph is stale"]
        and already_low["confidence_reported"] == "low",
        f"a digest already under the ceiling still records why it was capped: {already_low}",
    )
    # Two callers, one reason. The membership test was taken against a list snapshotted
    # before either was appended, so both cleared it and the record said it twice.
    doubled = cap_confidence(
        {"confidence": "high"},
        [("medium", "scope is wide"), ("low", "scope is wide")],
    )
    assert_true(
        doubled["confidence"] == "low" and doubled["confidence_capped_by"] == ["scope is wide"],
        f"one reason is recorded once, however many caps carry it: {doubled}",
    )
    # Across calls: some signals are only known after archival, and arrive on a second
    # pass. The first pass's number is what the second agent said; the second pass must not
    # report the first cap as that.
    twice = cap_confidence({"confidence": "high"}, [("medium", "grounded claims carry no file:line")])
    twice = cap_confidence(twice, [("low", "no cited anchor could be verified in this project")])
    assert_true(
        twice["confidence"] == "low"
        and twice["confidence_reported"] == "high"
        and len(twice["confidence_capped_by"]) == 2,
        f"a later cap appends its reason and leaves the original number alone: {twice}",
    )
    assert_true(
        cap_confidence({"confidence": "high"}, []) == {"confidence": "high"}
        and cap_confidence(None, [("low", "x")]) is None,
        "nothing to cap with, or nothing to cap, changes nothing",
    )


def _reused_evidence_reports_its_anchor_ratio() -> None:
    """A recalled digest says how much of it could be verified, in the block read first."""
    from core.evidence import evidence_store
    from core.evidence.runtime_io import write_response_snapshot
    from core.workspace.workspace_paths import workflow_paths

    root = Path(tempfile.mkdtemp(prefix="anchor-ratio-"))
    try:
        (root / "src.py").write_text("\n".join(f"line_{i} = {i}" for i in range(1, 30)) + "\n", encoding="utf-8")
        session_id = "ratio-session"
        content = "claim [src.py:3] and claim [src.py:9]"
        context = {"model": "provider/model-a", "agent": "analyze"}
        write_response_snapshot(root, content, "run-ratio", session_id)
        stored = {"summary": "recalled", "confidence": "high"}
        evidence_store.record(
            root, "analyze", "ratio query", session_id, stored,
            workflow_paths(root, session_id)["logs_dir"] / "run-ratio" / "output.raw.md",
            content, context=context,
        )
        executor = Executor()
        result = executor._maybe_reuse(root, "analyze", "ratio query", session_id, context)
        assert_true(
            result is not None
            and result["digest"]["anchors"] == {"certified": 2, "total": 2, "scanned_all": True},
            f"a reused digest carries the ratio its evidence_ref reports: {result and result.get('digest')}",
        )
        assert_true(
            executor._reuse_outcome == "hit",
            f"a served artifact records the outcome `hit`: {executor._reuse_outcome}",
        )
        executor._maybe_reuse(root, "analyze", "other query", session_id, context)
        assert_true(
            executor._reuse_outcome == "no_prior",
            f"a query never asked before records `no_prior`: {executor._reuse_outcome}",
        )
        assert_true(
            "anchors" not in stored,
            f"and the stored digest is not annotated with this run's numbers: {stored}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _partial_anchor_evidence_is_priced_in() -> None:
    """Half-unopenable evidence lowers the number, not only the footnote.

    The cap used to fire only when NOTHING could be certified. Between that and a clean
    run sat a digest reading `confidence: high` over two verifiable anchors out of forty,
    and the reader was told to read the digest FIRST and open the artifact only on a gap
    — so the one run whose gap most needed announcing was the one that announced nothing.
    """
    from core.evidence.contract import anchor_cap, cap_confidence

    assert_true(
        anchor_cap(4, 4) is None and anchor_cap(0, 0) is None,
        f"nothing to say about evidence that all checks out, or about none of it: "
        f"{anchor_cap(4, 4)} {anchor_cap(0, 0)}",
    )
    assert_true(
        anchor_cap(0, 40) == ("medium", "no cited anchor could be verified in this project"),
        f"nothing verified keeps the ceiling it always had: {anchor_cap(0, 40)}",
    )
    # The two rungs that did not exist. 1-of-40 and 39-of-40 used to be graded the same as
    # 40-of-40, which is the whole finding.
    mostly_unverified = anchor_cap(1, 40)
    mostly_verified = anchor_cap(39, 40)
    assert_true(
        mostly_unverified is not None and mostly_unverified[0] == "low" and "1 of 40" in mostly_unverified[1],
        f"mostly unverified is worth no more than low, and says the count: {mostly_unverified}",
    )
    assert_true(
        mostly_verified is not None and mostly_verified[0] == "medium" and "1 of 40" in mostly_verified[1],
        f"mostly verified is worth a medium, naming what was missed: {mostly_verified}",
    )
    # The boundary is a decision, so it is asserted rather than left to arithmetic.
    assert_true(
        anchor_cap(2, 4)[0] == "medium" and anchor_cap(1, 4)[0] == "low",
        f"half verified is the medium rung, below it is low: {anchor_cap(2, 4)} {anchor_cap(1, 4)}",
    )
    # And a cap never RAISES anything. A digest that graded itself low stays low, with the
    # ratio's reason appended to whatever was already recorded.
    lowered = cap_confidence({"confidence": "low"}, [anchor_cap(39, 40)])
    assert_true(
        lowered["confidence"] == "low" and lowered["confidence_capped_by"] == [mostly_verified[1]],
        f"a ceiling above the reported number moves nothing and still records why: {lowered}",
    )


def _anchor_scan_reads_past_the_stored_prefix() -> None:
    """A bad citation past the 40th must still count against the run.

    The scan used to stop at 40, so a session's explore citing three nonexistent files
    after forty good ones reported `40/40 certified` and kept an uncapped confidence.
    """
    from core.evidence import evidence_store
    from core.evidence.contract import anchor_cap
    from core.provider.executor import _anchor_counts

    root = Path(tempfile.mkdtemp(prefix="anchor-scan-"))
    try:
        (root / "src.py").write_text(
            "\n".join(f"line_{i} = {i}" for i in range(1, 260)) + "\n", encoding="utf-8"
        )
        forty_then_bad = "\n".join(f"claim [src.py:{n}]" for n in range(1, 41))
        forty_then_bad += "\nclaim [core/provider/registry.py:30]"
        scan = evidence_store.scan_anchors(root, forty_then_bad)
        assert_true(
            scan["cited"] == 41 and scan["certified"] == 40 and scan["scanned_all"]
            and len(scan["anchors"]) == 40 and scan["complete"] is False
            and scan["uncertified"] == ["core/provider/registry.py:30"],
            f"the 41st citation is certified, counted and named, not skipped: {scan}",
        )
        cap = anchor_cap(scan["certified"], scan["cited"], scan["scanned_all"])
        assert_true(
            cap is not None and cap[0] == "medium" and "1 of 41" in cap[1],
            f"and it lowers the number the reader sees first: {cap}",
        )

        past_limit = "\n".join(
            f"claim [src.py:{n}]" for n in range(1, evidence_store.MAX_ANCHORS_SCANNED + 2)
        )
        wide = evidence_store.scan_anchors(root, past_limit)
        assert_true(
            wide["scanned_all"] is False and wide["cited"] == evidence_store.MAX_ANCHORS_SCANNED,
            f"a scan that stops early says so: {wide['cited']} {wide['scanned_all']}",
        )
        clean_prefix = anchor_cap(wide["certified"], wide["cited"], wide["scanned_all"])
        assert_true(
            clean_prefix is not None and clean_prefix[0] == "medium"
            and "were not checked" in clean_prefix[1],
            f"a clean prefix is not a clean run: {clean_prefix}",
        )
        assert_true(
            anchor_cap(5, 5, True) is None and anchor_cap(5, 5) is None,
            "a fully scanned, fully certified run keeps its own number",
        )

        legacy_cut = {"anchors": [{"certified": True}] * 40, "anchors_complete": False}
        legacy_short = {"anchors": [{"certified": True}] * 3, "anchors_complete": True}
        assert_true(
            _anchor_counts(legacy_cut) == (40, 40, False)
            and _anchor_counts(legacy_short) == (3, 3, True),
            f"rows from before the scan record read as what they can prove: "
            f"{_anchor_counts(legacy_cut)} {_anchor_counts(legacy_short)}",
        )

        claims = evidence_store.certify_claim_refs(
            root,
            [
                {"text": "real", "refs": ["src.py:3"]},
                {"text": "moved", "refs": ["src.py:3", "core/provider/registry.py:30"]},
                {"text": "label", "refs": ["req:login"]},
            ],
        )
        assert_true(
            [c["ref_status"] for c in claims]
            == [["certified"], ["certified", "unverified"], ["not_a_file"]],
            f"each claim says which of its refs this project can open: {claims}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _scope_gaps_cap_only_what_the_answer_needed() -> None:
    """Live state and external systems are disclosed, not priced in."""
    from core.provider.result_prep import _scope_incomplete, scope_gaps, scope_warnings

    tagged = (
        "scope_not_covered:\n"
        "- [live_state] live provider sessions\n"
        "- [external] MCP database\n"
        "- [out_of_scope] unrelated tests\n"
    )
    assert_true(
        not _scope_incomplete(tagged) and len(scope_warnings(tagged)) == 3,
        f"non-capping gaps leave the number alone and stay visible: {scope_warnings(tagged)}",
    )
    reachable = tagged + "- [in_scope_unreachable] promote caller wiring\n"
    assert_true(_scope_incomplete(reachable), "a hole in the requested ground still caps")
    untagged = "scope_not_covered:\n- live browser behaviour\n"
    assert_true(
        _scope_incomplete(untagged) and scope_gaps(untagged) == [("unknown", "live browser behaviour")],
        "an untagged bullet keeps the cap it always had",
    )
    assert_true(
        not _scope_incomplete("scope_not_covered:\n- none\n"),
        "an empty section is still empty",
    )
    assert_true(
        scope_gaps("scope_not_covered:\n- [bogus] x\n") == [("unknown", "[bogus] x")],
        "an unrecognised tag is not trusted as a non-capping one",
    )


def _test_workflow_contracts() -> None:
    _anchor_scan_reads_past_the_stored_prefix()
    _scope_gaps_cap_only_what_the_answer_needed()
    _confidence_caps_keep_their_reasons()
    _reused_evidence_reports_its_anchor_ratio()
    _partial_anchor_evidence_is_priced_in()
    _round_trips()
    _correlation_scoping()
    _correlation_chain()
    _usage_derivation()
    _stream_append()
    _verify_verdict_is_recorded()
    _finalize_is_idempotent()
