"""Claude transcript parsing, asserted on the distinctions that would be invisible if broken.

The schema belongs to a vendor and is not documented here, so the fixture is the contract:
every record shape in `tests/fixtures/claude/session-basic.jsonl` was copied from a real
transcript, including the torn final line a live writer can leave behind.

The checks are aimed at the judgement calls rather than the arithmetic. A parser that
counts tool results as user turns still returns a number, and the number still looks like
a measurement — it just answers "how many tools did the model call" while claiming to
answer "how many times did a person have to ask".
"""

from pathlib import Path

from exoma.core.audit import transcript
from tests.checks.support import assert_true

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "claude" / "session-basic.jsonl"


def _a_torn_line_does_not_lose_the_file() -> None:
    records = transcript.load_transcript(FIXTURE)
    assert_true(
        len(records) == 12,
        "a transcript is append-only and written by a live process, so a torn final line "
        f"is a normal state: it must cost one record, not the file (got {len(records)})",
    )
    assert_true(
        transcript.load_transcript(FIXTURE.parent / "does-not-exist.jsonl") == [],
        "a missing transcript reads as no records, not as a crash in the harvester",
    )


def _only_a_person_counts_as_a_turn() -> None:
    records = transcript.load_transcript(FIXTURE)
    turns = transcript.count_turns(records)
    assert_true(
        turns["message_turns"] == 3,
        "tool results, task notifications and isMeta injections all arrive in the user "
        "role; counting them turns 'how many times did a person ask' into 'how many tools "
        f"did the model call' (got {turns['message_turns']})",
    )
    assert_true(
        turns["assistant_turns"] == 3 and turns["sidechain_assistant_turns"] == 1,
        "the native-subagent transcript is interleaved into the parent session. Arm B is "
        "the arm that produces it, so folding it into the main count hides arm B's cost "
        f"and dropping it understates arm B (got {turns})",
    )


def _turns_to_first_edit_stops_at_the_first_write() -> None:
    records = transcript.load_transcript(FIXTURE)
    assert_true(
        transcript.turns_to_first_edit(records) == 2,
        "the second ask is the one that produced an Edit; a Grep before it is the model "
        "reading, not executing",
    )
    no_edits = [r for r in records if "Edit" not in str(r)]
    assert_true(
        transcript.turns_to_first_edit(no_edits) is None,
        "a session that never touched a file has no turns-to-first-edit. None, not zero: "
        "zero would read as 'it edited immediately'",
    )


def _context_counts_the_cache_and_sums_across_turns() -> None:
    records = transcript.load_transcript(FIXTURE)
    context = transcript.context_tokens(records)
    # (2+1000+500) + (4+3000+250) + (6+9000+100), sidechain excluded.
    assert_true(
        context["context_input_tokens"] == 13862,
        "a turn that read 200k cached tokens still carried 200k tokens of context; cache "
        f"reads are part of the window, not a discount on it (got {context['context_input_tokens']})",
    )
    assert_true(
        context["context_peak_tokens"] == 9106,
        "the peak is one turn's context, not the running total — it is the number that "
        f"says how close the session came to filling the window (got {context['context_peak_tokens']})",
    )
    assert_true(
        context["context_sidechain_tokens"] == 251,
        "sidechain context is carried separately so arm B's subagent load stays visible "
        f"instead of merging into the main figure (got {context['context_sidechain_tokens']})",
    )
    assert_true(
        context["context_measured_messages"] == 3,
        "totals over three measured messages and over three hundred are different claims; "
        "the denominator travels with the number",
    )
    assert_true(
        context["transcript_reasoning_tokens"] == 110,
        "reasoning is a breakdown of output, reported beside it rather than added to it",
    )


def _an_empty_transcript_reports_nulls_not_zeroes() -> None:
    summary = transcript.summarize(FIXTURE.parent / "nothing-here.jsonl")
    assert_true(
        summary["message_turns"] is None and summary["context_input_tokens"] is None,
        "an unread transcript is unknown, not empty. A zero here would make an arm whose "
        "transcript never arrived look like the arm that used no context at all",
    )
    assert_true(
        summary["transcript_source"] == "unreadable_or_empty",
        "the gap is named in the row rather than left to be inferred from nulls",
    )


def _a_transcript_is_found_by_session_id_not_by_mtime() -> None:
    slug = transcript.project_slug("E:\\Work\\project\\agent-workflow")
    assert_true(
        slug == "E--Work-project-agent-workflow",
        f"the project slug is how Claude Code names its transcript directory (got {slug})",
    )
    assert_true(
        transcript.find_transcript("", "E:\\Work\\project\\agent-workflow") is None,
        "no session id, no transcript. Falling back to the newest file would attach one "
        "unit's transcript to another whenever two units run at once",
    )


def _test_transcript_parsing() -> None:
    _a_torn_line_does_not_lose_the_file()
    _only_a_person_counts_as_a_turn()
    _turns_to_first_edit_stops_at_the_first_write()
    _context_counts_the_cache_and_sums_across_turns()
    _an_empty_transcript_reports_nulls_not_zeroes()
    _a_transcript_is_found_by_session_id_not_by_mtime()
