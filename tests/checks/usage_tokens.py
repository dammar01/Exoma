"""Provider-reported token counts, asserted where getting it wrong stays invisible.

Every check here is aimed at one mistake: treating a breakdown as an addend. Reasoning is
reported INSIDE the output count and cached input INSIDE the input count, so any layer
that helpfully sums the fields it finds produces a bill that grows the harder a model
thinks about the same answer. That failure is silent — the number still looks like a
number — which is why it is asserted at every layer that could introduce it rather than
once at the top.

The second theme is absence. A provider that reports nothing must leave a row estimated
rather than zeroed, because a zero reads as a measurement and averages into the report as
one.
"""

from exoma.core.workspace.workspace_paths import data_dir
from pathlib import Path

from exoma.adapters.providers.codex_adapter import CodexAdapter
from exoma.adapters.providers.opencode_adapter import OpenCodeAdapter
from exoma.adapters.shared.usage import merge_usage, normalize_usage, token_source_for
from exoma.core.audit import telemetry
from exoma.core.evidence.contracts import UsageRecord, billable_input, billable_output
from exoma.core.policy.governance import budget_state
from tests.checks.support import assert_true

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "opencode"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _normalisation_reads_the_shapes_providers_send() -> None:
    nested = normalize_usage(
        {
            "input_tokens": 1000,
            "output_tokens": 800,
            "output_tokens_details": {"reasoning_tokens": 600},
            "input_tokens_details": {"cached_tokens": 900},
        }
    )
    assert_true(
        nested
        == {
            "input_tokens": 1000,
            "output_tokens": 800,
            "reasoning_tokens": 600,
            "cached_input_tokens": 900,
        },
        "a breakdown nested in a *_details container must be found; providers report it "
        "there as often as at the top level",
    )
    assert_true(
        nested["reasoning_tokens"] < nested["output_tokens"]
        and nested["cached_input_tokens"] < nested["input_tokens"],
        "the breakdowns must stay smaller than the totals they are part of — a reasoning "
        "count above the output count is the signature of the two being added",
    )

    alias = normalize_usage({"prompt_tokens": 10, "completion_tokens": 20})
    assert_true(
        alias["input_tokens"] == 10 and alias["output_tokens"] == 20,
        "the other common spelling must map to the same two fields, or one provider's "
        "rows silently read as unmeasured",
    )
    assert_true(
        normalize_usage({"unrelated": 5}) is None and normalize_usage(None) is None,
        "an object with nothing recognisable is no measurement at all; returning zeros "
        "would put a fabricated count on the row",
    )
    assert_true(
        normalize_usage({"input_tokens": True}) is None,
        "a bool is not a token count even though Python will happily add it to one",
    )


def _merging_adds_turns_but_never_fields() -> None:
    merged = merge_usage(
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "reasoning_tokens": 15,
            "cached_input_tokens": None,
        },
        {
            "input_tokens": 5,
            "output_tokens": 7,
            "reasoning_tokens": None,
            "cached_input_tokens": 3,
        },
    )
    assert_true(
        merged["input_tokens"] == 15 and merged["output_tokens"] == 27,
        "two turns of one run spent both turns' tokens; keeping the last would report a "
        "fraction of the bill as the whole of it",
    )
    assert_true(
        merged["reasoning_tokens"] == 15 and merged["cached_input_tokens"] == 3,
        "a count present on one side only must carry through rather than be zeroed into "
        "a sum — absent means unreported, not none spent",
    )
    assert_true(
        merge_usage(None, {"output_tokens": 4})["output_tokens"] == 4
        and merge_usage({"output_tokens": 4}, None)["output_tokens"] == 4,
        "merging with nothing must return the something",
    )


def _token_source_admits_a_half_measurement() -> None:
    assert_true(
        token_source_for({"input_tokens": 1, "output_tokens": 2}) == "provider",
        "both directions measured is the only state that may claim to be provider-counted",
    )
    assert_true(
        token_source_for({"output_tokens": 2, "input_tokens": None}) == "mixed",
        "a provider reporting only its output leaves the input estimated — calling that "
        "row either provider or estimated misdescribes half of it",
    )
    assert_true(
        token_source_for(None) == "estimated" and token_source_for({}) == "estimated",
        "no measurement means the row still carries its chars//4 estimate, and says so",
    )


def _adapters_read_usage_out_of_their_own_streams() -> None:
    stream = "\n".join(
        [
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":300,'
            '"output_tokens_details":{"reasoning_tokens":250}}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"answer"}}',
            '{"type":"turn.completed","usage":{"input_tokens":50,"output_tokens":20}}',
        ]
    )
    usage = CodexAdapter.extract_usage(stream)
    assert_true(
        usage["input_tokens"] == 150 and usage["output_tokens"] == 320,
        "every turn.completed in a run must be summed; a multi-turn run that reports one "
        "turn's tokens understates the call it measures",
    )
    assert_true(
        CodexAdapter.clean_output(stream) == "answer",
        "reading usage must not disturb what the adapter returns as the answer",
    )
    assert_true(
        CodexAdapter.extract_usage("plain text, no events") is None,
        "a build that emits no usage must leave the row estimated, not zeroed",
    )
    assert_true(
        OpenCodeAdapter.extract_usage("some log line\nthe answer") is None,
        "OpenCode's formatted output carries no counts — bootstrap and every error tail "
        "still arrive this way, and inventing a number for them would be worse than "
        "leaving the row estimated",
    )
    assert_true(
        OpenCodeAdapter.extract_usage('{"usage":{"input_tokens":5,"output_tokens":7}}')
        == {
            "input_tokens": 5,
            "output_tokens": 7,
            "reasoning_tokens": None,
            "cached_input_tokens": None,
        },
        "the older top-level shape stays supported: it costs one branch, and it is what "
        "another build on PATH would most plausibly emit",
    )


def _opencode_maps_its_own_arithmetic_onto_the_shared_shape() -> None:
    """Fixtures captured from a live `opencode run --format json`, unedited except for
    trimming the file bodies a tool call embedded and adding three lines of noise.

    Worth asserting against real captures rather than a hand-written stream because the
    whole mapping rests on two facts about this provider that no other provider shares,
    and both are only observable in a real run.
    """
    stream = _fixture("format-json-step-finish.jsonl")
    usage = OpenCodeAdapter.extract_usage(stream)
    assert_true(
        usage
        == {
            "input_tokens": 45976,
            "output_tokens": 85,
            "reasoning_tokens": 0,
            "cached_input_tokens": 17536,
        },
        "two steps, summed, with each step's cached read folded into its input. The "
        f"capture's own totals add to 46061, and so must input+output here: {usage}",
    )
    assert_true(
        (usage["input_tokens"] + usage["output_tokens"]) == 15863 + 30198,
        "the mapping must reproduce the provider's own `total` exactly — losing "
        "cache.read here would undercount this run by 17536 tokens with nothing to show "
        "that anything was dropped",
    )
    assert_true(
        OpenCodeAdapter.clean_output(stream) == "DONE",
        "the answer is the text events and nothing else; returning the raw event objects "
        "would hand the main agent a wall of JSON where the evidence belongs",
    )

    reasoning = OpenCodeAdapter.extract_usage(_fixture("format-json-reasoning.jsonl"))
    assert_true(
        reasoning
        == {
            "input_tokens": 16353,
            "output_tokens": 65,
            "reasoning_tokens": 38,
            "cached_input_tokens": 0,
        },
        "OpenCode reports reasoning as a SIBLING of output, not inside it: that capture's "
        "total is 16418 and input+output alone is 16380. The 38 it thought with have to "
        "be folded into output here, because every layer above reads reasoning as a "
        f"breakdown and will not add it again: {reasoning}",
    )
    assert_true(
        token_source_for(reasoning) == "provider",
        "both directions measured, so the row may say who counted it",
    )


def _opencode_survives_a_stream_that_is_not_all_events() -> None:
    assert_true(
        OpenCodeAdapter.extract_usage(
            '{"type":"step_finish","part":{"tokens":{"total":1,\n'
            "not json at all\n"
            '{"type":"step_finish","part":{"tokens":{"input":10,"output":2,'
            '"reasoning":0,"cache":{"write":0,"read":5}}}}'
        )
        == {
            "input_tokens": 15,
            "output_tokens": 2,
            "reasoning_tokens": 0,
            "cached_input_tokens": 5,
        },
        "a truncated line and a banner must cost nothing: the stream is shared with logs "
        "and with this adapter's own truncation marker, and one bad line must not take "
        "the run's measurement with it",
    )
    assert_true(
        OpenCodeAdapter.extract_usage(
            '{"type":"step_finish","part":{"reason":"stop"}}'
        )
        is None,
        "a step that reported no tokens is an absent measurement, not a zero one",
    )
    assert_true(
        OpenCodeAdapter.clean_output(
            '{"type":"step_start","part":{}}\n'
            '{"type":"step_finish","part":{"tokens":{"input":1,"output":1}}}'
        )
        == "",
        "a JSON run that emitted no text has no answer to give, and must not fall back "
        "to handing the caller its own event objects",
    )
    assert_true(
        OpenCodeAdapter.clean_output(
            "INFO  2026-01-01T00:00:00Z service=session id=ses_x\n> build\nthe answer"
        )
        == "the answer",
        "bootstrap and error tails still arrive as formatted text, and still need their "
        "logs stripped",
    )
    quoted = (
        "[EVIDENCE]\ngrounded:\n- the endpoint returns\n"
        '{"type":"request","id":1}\n'
        "which the caller ignores"
    )
    assert_true(
        OpenCodeAdapter.clean_output(quoted) == quoted,
        "an evidence body that quotes a JSON object is prose, not a machine stream. "
        "Telling the two apart by `type` alone would silently delete the quoted line from "
        "the answer — evidence lost with the run still reported as a success, which is "
        "why the event types are named rather than inferred",
    )


def _billing_prefers_measurement_and_refuses_to_double_count() -> None:
    measured = UsageRecord(
        estimated_input_tokens=10,
        estimated_output_tokens=10,
        actual_input_tokens=1000,
        actual_output_tokens=800,
        actual_reasoning_tokens=600,
        actual_cached_input_tokens=900,
    )
    assert_true(
        billable_input(measured) == 1000 and billable_output(measured) == 800,
        "a measured count must win over the estimate beside it",
    )
    assert_true(
        billable_input(measured) + billable_output(measured) == 1800,
        "the breakdowns must not enter the total — adding reasoning and cached input here "
        "would bill 3300 for a call that spent 1800",
    )
    estimated = UsageRecord(estimated_input_tokens=10, estimated_output_tokens=10)
    assert_true(
        billable_input(estimated) == 10 and billable_output(estimated) == 10,
        "an unmeasured row falls back to its estimate rather than to zero",
    )


def _report_and_budget_read_the_measured_rows() -> None:
    rows = [
        UsageRecord(
            session_id="s",
            command="explore",
            prompt_id="p1",
            estimated_input_tokens=10,
            estimated_output_tokens=10,
            actual_input_tokens=1000,
            actual_output_tokens=800,
            actual_reasoning_tokens=600,
            token_source="provider",
            provider_call_index=0,
        ),
        UsageRecord(
            session_id="s",
            command="explore",
            prompt_id="p1",
            estimated_input_tokens=5,
            estimated_output_tokens=5,
            token_source="estimated",
            provider_call_index=1,
        ),
    ]
    cost = telemetry._cost(rows)
    assert_true(
        cost["total_tokens"] == 1000 + 800 + 5 + 5,
        "a history mixing measured and estimated rows must charge each row by what it "
        "actually knows, not fall back to one source for all of them",
    )
    assert_true(
        cost["reasoning_tokens_within_output"] == 600
        and cost["reasoning_tokens_within_output"] < cost["output_tokens"],
        "reasoning is reported beside the total as a share of the output, never added to it",
    )
    assert_true(
        sorted(cost["token_source"]) == ["estimated", "provider"],
        "a mixed history must show both sources; collapsing them hides that half the "
        "figure is an estimate",
    )
    assert_true(
        cost["measured_calls"] == 1,
        "the denominator for the measured half of a report has to be readable too",
    )
    assert_true(
        telemetry._cost([])["reasoning_tokens_within_output"] is None,
        "no provider having reported reasoning is not the same as none being spent",
    )

    state = budget_state(rows, "s", 5000)
    assert_true(
        state["spent"] == 1810,
        "the ceiling must be measured against provider counts where they exist — a budget "
        "read off chars//4 stops watching the tokens a model spends thinking",
    )


def _a_continuation_records_each_call_and_counts_the_saving_once() -> None:
    """The end-to-end shape: two provider calls, two rows, one saving.

    Written against the writer rather than the helper because the failure it guards is a
    seam failure. Every part can be individually right while the rows still double-count
    the digest or credit one call's tokens to both.
    """
    import json
    import shutil
    import tempfile
    from pathlib import Path

    from exoma.core.provider.executor import Executor
    from exoma.core.runtime.state import ensure_workflow_workspace

    root = Path(tempfile.mkdtemp(prefix="aw-usage-rows-"))
    try:
        ensure_workflow_workspace(root, str(Path("main.py").resolve()))
        executor = Executor.__new__(Executor)
        executor._call_metas = [
            {
                "adapter_meta": {
                    "duration_seconds": 1.0,
                    "resumed": False,
                    "provider_usage": {
                        "input_tokens": 100,
                        "output_tokens": 300,
                        "reasoning_tokens": 250,
                        "cached_input_tokens": None,
                    },
                },
                "prompt_chars": 400,
                "response_chars": 800,
            },
            {
                "adapter_meta": {
                    "duration_seconds": 2.0,
                    "resumed": True,
                    "thread_changed": True,
                    "provider_usage": {
                        "input_tokens": 50,
                        "output_tokens": 20,
                        "reasoning_tokens": None,
                        "cached_input_tokens": None,
                    },
                },
                "prompt_chars": 200,
                "response_chars": 100,
            },
        ]
        executor._last_call_meta = {
            "command": "explore",
            "role": "exploration",
            "prompt_id": "pid-1",
            "response_chars": 900,
            "token_source": "provider",
            # The aggregate carries the LAST invocation's flag; the first row must not inherit it.
            "resumed": True,
        }
        executor._record_usage(
            {
                "ok": True,
                "content": "x" * 900,
                "digest": {"summary": "s" * 100, "key_findings": ["f" * 20]},
                "meta": {},
            },
            root,
            "explore",
            "a task",
            "sid-1",
        )
        rows = [
            json.loads(line)
            for line in (data_dir(root) / "usage.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        assert_true(
            [row["provider_call_index"] for row in rows] == [0, 1],
            "a continuation ran the adapter twice and must leave two rows, indexed in the "
            "order the calls happened",
        )
        assert_true(
            rows[0]["actual_output_tokens"] == 300 and rows[1]["actual_output_tokens"] == 20,
            "each row carries the tokens ITS call spent; repeating the aggregate on both "
            "would bill the retry for the first attempt as well",
        )
        assert_true(
            [row["provider_resumed"] for row in rows] == [False, True],
            "each row carries ITS call's thread state: a fresh first call followed by a "
            f"resumed retry must not both read as resumed; got {[row['provider_resumed'] for row in rows]}",
        )
        assert_true(
            [row["provider_thread_changed"] for row in rows] == [None, True],
            "a lost thread is flagged on the invocation it happened in, and an adapter that "
            f"did not say stays None; got {[row['provider_thread_changed'] for row in rows]}",
        )
        assert_true(
            rows[0]["digest_chars"] is None and rows[1]["digest_chars"] == 120,
            "the digest belongs to the answer the command returned, so it lands on the "
            "final row alone — on both, the saving is counted twice",
        )
        assert_true(
            rows[1]["premium_context_avoided_tokens"] == (900 - 120) // 4,
            "the saving is measured against the MERGED answer main_agent did not read, "
            "not against the retry's fragment — that comparison reports near zero and "
            "quietly erases the digest contract's whole justification",
        )
        report = telemetry.report(root)
        assert_true(
            report["time_to_completion_seconds"]["measured_calls"] == 1
            and report["security"]["total_calls"] == 1,
            "every metric that counts WORK must agree that this was one command. A "
            "retry is not a second delegated call to the user who waited for one answer, "
            f"and a security rate that moves when one happens is measuring noise: {report}",
        )
        assert_true(
            report["provider_calls"] == 2 and report["calls"] == 1,
            "the report must be able to say two provider calls served one command; a "
            "single figure for both makes cost per task move when a retry happens. "
            "`calls` keeps its published meaning — a number read outside this repository "
            f"must not start counting something else under the same name: {report}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _rows(root) -> list[dict]:
    import json

    path = data_dir(root) / "usage.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _the_common_single_call_path_records_who_reported_it() -> None:
    """One adapter call, one row, and the row names its provider.

    The end-to-end test that existed covered the continuation — two adapter calls, the
    RARE shape. The single-call path underneath it is the one nearly every command takes
    and it had no end-to-end assertion at all, so `_per_invocation_metas` returning `[]`
    and the fallback that follows were only ever exercised by accident.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from exoma.core.provider.executor import Executor
    from exoma.core.runtime.state import ensure_workflow_workspace
    from tests.checks.support import FakeOpenCodeAdapter

    root = Path(tempfile.mkdtemp(prefix="aw-usage-single-"))
    try:
        ensure_workflow_workspace(root, str(Path("main.py").resolve()))
        result = Executor(adapter=FakeOpenCodeAdapter()).execute(
            "analyze",
            "why is this slow",
            {"session_id": "sid-single", "provider_session_id": None},
            str(root),
        )
        assert_true(result["ok"], f"the fake adapter returns evidence: {result}")
        rows = _rows(root)
        assert_true(
            len(rows) == 1,
            f"one adapter call must leave exactly one row, not zero and not the "
            f"continuation's two; got {len(rows)}",
        )
        assert_true(
            isinstance(rows[0]["provider"], str) and rows[0]["provider"],
            "a row must name the provider that reported it. While one provider is "
            "measured and another is not, a report showing both `provider` and "
            "`estimated` in token_source cannot say which half is which if this is null",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _a_failed_call_records_what_it_burned() -> None:
    """Failure after the provider ran must still cost something in the stream.

    Four shapes, and the differences between them are the whole point: a provider that
    ran and errored, a provider that ran and answered with a menu (so the guard, not the
    provider, rejected it), an adapter that refused BEFORE spawning anything, and a
    refusal that happened before dispatch at all. The first two burned tokens and must
    appear. The last two burned none and must not — a row there would invent spend,
    which is this fix pointed backwards.

    `returncode` is what separates them, so every fake here sets `last_call_meta` exactly
    as its real counterpart would: the two that "ran" carry one, the preflight refusal
    does not.
    """
    import json
    import shutil
    import tempfile
    from pathlib import Path

    from exoma.core.evidence.contract import make_error
    from exoma.core.evidence.contracts import UsageRecord
    from exoma.core.evidence.runtime_io import write_usage_record
    from exoma.core.provider.executor import Executor
    from exoma.core.runtime.state import ensure_workflow_workspace
    from tests.checks.support import FakeOpenCodeAdapter

    class _Spawning:
        """Base for fakes that stand in for an adapter which did reach a subprocess."""

        command = "opencode"
        timeout_seconds = 0
        no_timeout = True
        last_call_meta = None

        def _spawned(self) -> None:
            self.last_call_meta = {"returncode": 0, "duration_seconds": 1.0}

    class ErroringAdapter(_Spawning):
        def run(self, prompt, session, model=None, work_dir=None):
            self._spawned()
            return {"ok": False, "content": "provider failed", "meta": {"returncode": 1}}

    class MenuAdapter(_Spawning):
        def run(self, prompt, session, model=None, work_dir=None):
            self._spawned()
            return {
                "ok": True,
                "content": "Specify command: explore, plan, analyze.",
                "meta": {"provider_session_id": "ses_menu"},
            }

    class PreflightRefusingAdapter(_Spawning):
        """Refuses before Popen, exactly as the oversized-command-line guard does."""

        def run(self, prompt, session, model=None, work_dir=None):
            return make_error(
                "prompt_too_long",
                "command line is 9999 chars",
                next_action="Shorten the task text.",
                meta={"command_line_chars": 9999},
            )

    root = Path(tempfile.mkdtemp(prefix="aw-usage-failed-"))
    try:
        ensure_workflow_workspace(root, str(Path("main.py").resolve()))

        errored = Executor(adapter=ErroringAdapter()).execute(
            "analyze",
            "task one",
            {"session_id": "sid-fail", "provider_session_id": None},
            str(root),
        )
        assert_true(not errored["ok"], f"the adapter was told to fail: {errored}")
        rows = _rows(root)
        assert_true(
            len(rows) == 1 and rows[0]["ok"] is False,
            "a provider call that failed spent its tokens before failing; recording "
            f"nothing leaves the most expensive shape of call invisible, got {rows}",
        )

        rejected = Executor(adapter=MenuAdapter()).execute(
            "analyze",
            "task two",
            {"session_id": "sid-fail", "provider_session_id": None},
            str(root),
        )
        assert_true(
            rejected["meta"]["error_type"] == "invalid_evidence",
            f"a menu is not evidence: {rejected}",
        )
        rows = _rows(root)
        assert_true(
            len(rows) == 2 and rows[1]["error_type"] == "invalid_evidence",
            "here the ADAPTER succeeded and the guard threw the answer away — the most "
            f"wasteful outcome there is, and it must still be billed; got {rows}",
        )
        assert_true(
            "policy" not in (rejected.get("meta") or {}),
            "recording a failed call must not drag the whole finalizer along with it: "
            "that also writes the command cache, and a menu stored as last_analyze_result "
            "would be read as evidence by the next command",
        )

        before = len(_rows(root))
        refused_early = Executor(adapter=PreflightRefusingAdapter()).execute(
            "analyze",
            "task three",
            {"session_id": "sid-fail", "provider_session_id": None},
            str(root),
        )
        assert_true(
            refused_early["meta"]["error_type"] == "prompt_too_long",
            f"the adapter refuses before spawning: {refused_early}",
        )
        assert_true(
            len(_rows(root)) == before,
            "an adapter that refused before Popen spent nothing. `ok:false` alone is not "
            "evidence that a provider ran, and billing every failure indiscriminately "
            "invents spend — the same error as the missing row, pointing backwards",
        )

        before = len(_rows(root))
        config_path = root / ".workflow" / "second_agent.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["session_token_budget"] = 1
        config_path.write_text(json.dumps(config), encoding="utf-8")
        write_usage_record(
            root,
            UsageRecord(
                session_id="sid-broke",
                estimated_input_tokens=99,
                estimated_output_tokens=99,
            ).to_dict(),
        )
        refused = Executor(adapter=FakeOpenCodeAdapter()).execute(
            "analyze",
            "task four",
            {"session_id": "sid-broke", "provider_session_id": None},
            str(root),
            workflow_session_id="sid-broke",
        )
        assert_true(
            refused["meta"]["error_type"] == "budget_exceeded",
            f"the ceiling must refuse before dispatch: {refused}",
        )
        assert_true(
            len(_rows(root)) == before + 1,
            "only the ceiling's own seeded row may have been added. A refusal that never "
            "reached a provider spent nothing, and a row for it would report spend that "
            "did not happen — the mirror image of the bug this fixes",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _a_continuation_that_never_spawned_does_not_double_the_first_call() -> None:
    """The stale-metadata trap, asserted at the seam where it would be invisible.

    A first call spawns and is measured. The continuation is refused before Popen — an
    oversized command line is the real trigger. `last_call_meta` is one attribute the
    adapter never resets, so without clearing it per invocation the second snapshot
    copies the FIRST call's numbers, `_per_invocation_metas` splits them into two rows,
    and the run bills twice for one subprocess. Nothing errors; the total simply doubles.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from exoma.core.evidence.contract import make_error
    from exoma.core.provider.executor import Executor
    from exoma.core.runtime.state import ensure_workflow_workspace

    class TruncatedThenRefusing:
        """Answers short enough to trigger a continuation, then refuses to make one."""

        command = "opencode"
        timeout_seconds = 0
        no_timeout = True
        last_call_meta = None

        def __init__(self) -> None:
            self.calls = 0

        def run(self, prompt, session, model=None, work_dir=None):
            self.calls += 1
            if self.calls == 1:
                self.last_call_meta = {
                    "returncode": 0,
                    "duration_seconds": 1.0,
                    "provider_usage": {
                        "input_tokens": 4321,
                        "output_tokens": 765,
                        "reasoning_tokens": 700,
                        "cached_input_tokens": None,
                    },
                }
                return {
                    "ok": True,
                    "content": "[EVIDENCE]\nfindings:\n- something",
                    "meta": {"provider_session_id": "ses_cont"},
                }
            # Refused before Popen: last_call_meta is left exactly as the adapter left it.
            return make_error(
                "prompt_too_long",
                "command line is 9999 chars",
                next_action="Shorten the task text.",
                meta={"command_line_chars": 9999},
            )

    root = Path(tempfile.mkdtemp(prefix="aw-usage-stale-"))
    try:
        ensure_workflow_workspace(root, str(Path("main.py").resolve()))
        adapter = TruncatedThenRefusing()
        Executor(adapter=adapter).execute(
            "analyze",
            "a task that gets a short answer",
            {"session_id": "sid-stale", "provider_session_id": "ses_cont"},
            str(root),
        )
        rows = _rows(root)
        assert_true(
            len(rows) == 1,
            "one subprocess ran, so one row. A second row here is the first call's "
            f"tokens counted twice — same numbers, no error, double the bill; got {rows}",
        )
        assert_true(
            rows[0]["actual_input_tokens"] == 4321
            and rows[0]["actual_output_tokens"] == 765
            and rows[0]["token_source"] == "provider",
            "and the measurement must survive. Clearing the adapter's metadata per "
            "invocation is what stops the double count, but the attempt that cleared it "
            "reached no provider — reading it here would write `estimated` over a call "
            f"that was actually counted, a downgrade with no error to show for it: {rows[0]}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _every_row_that_reached_a_provider_names_its_release() -> None:
    """The release a call ran on, and whether it was the tested one, on single AND
    continuation rows (DEC-028). The stamp used to land only on the per-invocation snapshot:
    a single call's row spread the adapter's unstamped attribute, and a continuation's rows
    copied only the thread flags out of the snapshot — so opencode rows carried no version
    and codex rows no status."""
    from exoma.core.provider import versions
    from exoma.utils import osutil

    class Versioned:
        adapter = "opencode"
        command = "opencode"  # the route rebinds it to the provider's command anyway
        timeout_seconds = 0
        no_timeout = True
        last_call_meta = None

        def __init__(self, answers: int) -> None:
            self.answers = answers
            self.calls = 0

        def run(self, prompt, session, model=None, work_dir=None):
            self.calls += 1
            self.last_call_meta = {"returncode": 0, "duration_seconds": 1.0}
            complete = self.calls >= self.answers
            content = (
                "[EVIDENCE]\nfindings:\n- entry point at app/main.py\n[DIGEST]\n"
                "summary: done.\nconfidence: high"
                if complete
                else "[EVIDENCE]\nfindings:\n- something"
            )
            return {"ok": True, "content": content, "meta": {"provider_session_id": "ses_rel"}}

    # Seeded in the process cache, so no real CLI is asked; restored after.
    exe = osutil.resolve_exe("opencode")
    saved = versions._CACHE.get(exe, KeyError)
    versions._CACHE[exe] = "opencode 0.0.1"
    try:
        _rows_name_their_release(Versioned)
    finally:
        if saved is KeyError:
            versions._CACHE.pop(exe, None)
        else:
            versions._CACHE[exe] = saved


def _rows_name_their_release(Versioned) -> None:
    import shutil
    import tempfile
    from pathlib import Path

    from exoma.core.provider.executor import Executor
    from exoma.core.runtime.state import ensure_workflow_workspace

    for answers, label in ((1, "single"), (2, "continuation")):
        root = Path(tempfile.mkdtemp(prefix="aw-usage-release-"))
        try:
            ensure_workflow_workspace(root, str(Path("main.py").resolve()))
            adapter = Versioned(answers)
            Executor(adapter=adapter).execute(
                "analyze",
                "where is the entry point",
                {"session_id": f"sid-{label}", "provider_session_id": "ses_rel"},
                str(root),
            )
            rows = _rows(root)
            assert_true(
                adapter.calls == answers and len(rows) == answers,
                f"{label}: {answers} provider call(s), one row each; got {adapter.calls} "
                f"call(s) and {len(rows)} row(s)",
            )
            assert_true(
                all(
                    row.get("provider_version") == "opencode 0.0.1"
                    and row.get("provider_version_status") == "untested"
                    for row in rows
                ),
                f"{label}: every row names the release and its status: "
                f"{[(r.get('provider_version'), r.get('provider_version_status')) for r in rows]}",
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)


def _test_usage_token_accounting() -> None:
    _normalisation_reads_the_shapes_providers_send()
    _merging_adds_turns_but_never_fields()
    _token_source_admits_a_half_measurement()
    _adapters_read_usage_out_of_their_own_streams()
    _opencode_maps_its_own_arithmetic_onto_the_shared_shape()
    _opencode_survives_a_stream_that_is_not_all_events()
    _billing_prefers_measurement_and_refuses_to_double_count()
    _report_and_budget_read_the_measured_rows()
    _a_continuation_records_each_call_and_counts_the_saving_once()
    _the_common_single_call_path_records_who_reported_it()
    _a_failed_call_records_what_it_burned()
    _a_continuation_that_never_spawned_does_not_double_the_first_call()
    _every_row_that_reached_a_provider_names_its_release()
