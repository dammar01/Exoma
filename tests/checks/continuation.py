"""One bounded continuation when a reply stops short of its contract.

The failure this covers is not a dead call and not a refusal: the second agent reads
everything it was asked to read, runs out of room, and hands back a work-state summary
that ends in "continue if you have next steps". The work happened; the contract was
never emitted. Failing there discards a completed read and sends the user off to gather
evidence that already exists in a live session.
"""

from exoma.core.workspace.workspace_paths import data_dir
import os
import shutil
import tempfile
from pathlib import Path

from exoma.core.evidence.contract import _DIGEST_HEADER, validate_verification_contract
from exoma.core.provider.continuation import _VERIFY_SHAPE_KINDS
from exoma.core.provider.executor import Executor
from exoma.core.runtime.state import ensure_workflow_workspace

from tests.checks.support import assert_true

_STALLED = (
    "## Work State\n"
    "Read all 11 changed files. Evidence gathered for all 6 fixes.\n"
    "Continue if you have next steps, or stop and ask for clarification.\n"
)

_EVIDENCE = (
    "[EVIDENCE]\n"
    "confidence: high\n"
    "grounded:\n"
    "- entry point at app/main.py [app/main.py:1]\n"
    "assumptions: none\n"
    "dependencies: none\n"
    "dependents: none\n"
    "external: none\n"
    "scope_covered:\n"
    "- app/main.py\n"
    "scope_not_covered: none\n"
    "implications: none\n"
    "uncertainties: none\n"
    "[DIGEST]\n"
    "summary: resumed and finished.\n"
    "key_findings:\n"
    "- entry point located\n"
    "evidence_basis: grounded\n"
    "risk_level: low\n"
    "recommended_next_action: none\n"
    "confidence: high\n"
)

# The costlier stall: the read finished, the anchors are on the page, and the reply dies
# partway through its own digest. Observed in the field on three of four delegated calls —
# and the recovery discarded the whole body, keeping a digest that described evidence no
# longer present anywhere. The digest header below is deliberately cut mid-block.
_TRUNCATED_BODY = (
    "[EVIDENCE]\n"
    "confidence: high\n"
    "grounded:\n"
    "- token estimate is chars//4 [core/executor.py:808]\n"
    "- job timestamps persisted [core/job_manager.py:191]\n"
    "assumptions: none\n"
    "scope_covered:\n"
    "- core/executor.py\n"
    "uncertainties: none\n"
    "[DIGEST]\n"
    "summary: telemetry mapped, cost fie"
)

_DIGEST_ONLY = (
    "[DIGEST]\n"
    "summary: telemetry mapped, cost fields absent.\n"
    "key_findings:\n"
    "- token counts are estimates\n"
    "evidence_basis: grounded\n"
    "risk_level: low\n"
    "recommended_next_action: none\n"
    "confidence: high\n"
)

_VERIFICATION = (
    "[VERIFICATION]\n"
    "verdict: DONE\n"
    "blocking_findings: none\n"
    "escalations: none\n"
    "notes: none\n"
    "checks_run:\n"
    "- ran python tests/run.py -> success\n"
    "not_verified: none\n"
    "confidence: high\n"
    "[DIGEST]\n"
    "summary: every claim checked.\n"
)


class _ScriptedAdapter:
    """Returns queued replies in order, repeating the last one once exhausted."""

    def __init__(self, replies: list[str], provider_session_id="ses_scripted") -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.provider_session_id = provider_session_id

    def run(self, prompt, session, model=None, work_dir=None) -> dict:
        self.calls.append({"prompt": prompt, "session": dict(session)})
        content = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return {
            "ok": True,
            "content": content,
            "meta": {"provider_session_id": self.provider_session_id},
        }


def _session(provider_session_id="ses_scripted") -> dict:
    return {
        "session_id": "continuation-session",
        "provider_session_id": provider_session_id,
    }


def _test_contract_continuation() -> None:
    root = Path(tempfile.mkdtemp(prefix="continuation-"))
    try:
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))

        # A reply that reached the contract on its own must cost exactly one call. The
        # whole mechanism is worthless if it also taxes the runs that were already fine.
        clean = _ScriptedAdapter([_EVIDENCE])
        first_try = Executor(adapter=clean).execute(
            "analyze", "map the thing", _session(), str(root)
        )
        assert_true(
            first_try.get("ok") and len(clean.calls) == 1,
            f"a complete reply must not be re-prompted: calls={len(clean.calls)} {first_try.get('meta')}",
        )
        assert_true(
            "continuation_attempts" not in (first_try.get("meta") or {}),
            "a run that needed no continuation must not carry continuation meta",
        )

        # The real case: stalled first, contract second.
        stalled = _ScriptedAdapter([_STALLED, _EVIDENCE])
        resumed = Executor(adapter=stalled).execute(
            "analyze", "map the thing", _session(), str(root)
        )
        meta = resumed.get("meta") or {}
        assert_true(
            resumed.get("ok"),
            f"a reply recovered by continuation must succeed, not fail: {meta}",
        )
        assert_true(
            meta.get("continuation_recovered") is True
            and meta.get("continuation_attempts") == 1,
            f"the recovery must be recorded, not silent: {meta}",
        )
        assert_true(
            "grounded:" in (resumed.get("content") or ""),
            "the continuation's evidence must replace the stalled summary",
        )
        assert_true(
            len(stalled.calls) == 2
            and "NOT redo it" in stalled.calls[1]["prompt"]
            and stalled.calls[1]["session"].get("provider_session_id")
            == "ses_scripted",
            f"the follow-up must resume the SAME session and forbid a restart: {stalled.calls[-1]['prompt'][:120]!r}",
        )

        # A first reply that already carries the evidence must KEEP it. The follow-up only
        # ever supplies the missing block, so replacing the reply with it destroys the read
        # the run paid for and leaves an artifact with a digest and no anchors behind it.
        truncated = _ScriptedAdapter([_TRUNCATED_BODY, _DIGEST_ONLY])
        salvaged = Executor(adapter=truncated).execute(
            "explore", "map the telemetry", _session(), str(root)
        )
        salvaged_content = salvaged.get("content") or ""
        salvaged_meta = salvaged.get("meta") or {}
        assert_true(
            salvaged.get("ok") and salvaged_meta.get("continuation_recovered") is True,
            f"a truncated-digest reply must recover, not fail: {salvaged_meta}",
        )
        assert_true(
            "core/executor.py:808" in salvaged_content
            and "core/job_manager.py:191" in salvaged_content,
            "the continuation must not discard anchors the first reply already delivered",
        )
        assert_true(
            "cost fields absent" in salvaged_content
            and salvaged_meta.get("continuation_merged") is True,
            f"the recovered digest must be joined onto the body, not replace it: {salvaged_meta}",
        )
        assert_true(
            salvaged_content.count("[DIGEST]") == 1,
            "the truncated digest header must be dropped so the complete block is the one read",
        )

        # A reply that quoted the output template before stalling carries TWO [DIGEST]
        # markers. Cutting at the FIRST match took the quotation for the start of the
        # block and discarded everything behind it — sections, anchors, the whole body the
        # run had already paid for. Observed live on tasks about this contract, which name
        # the marker as a matter of course.
        #
        # The late anchor is the point of the fixture: it sits AFTER the quotation, so a
        # cut at the first marker loses it while a cut at the standalone header keeps it.
        # Without it the case passes under either behaviour and proves nothing.
        quoted = _TRUNCATED_BODY.replace(
            "assumptions: none",
            "assumptions: none\n"
            "format note: the [DIGEST] block wants summary/key_findings\n"
            "- late claim, after the quotation [core/telemetry.py:44]",
        )
        twice = _ScriptedAdapter([quoted, _DIGEST_ONLY])
        rescued = Executor(adapter=twice).execute(
            "explore", "map the telemetry", _session(), str(root)
        )
        rescued_content = rescued.get("content") or ""
        assert_true(
            (rescued.get("meta") or {}).get("continuation_merged") is True
            and "core/executor.py:808" in rescued_content,
            f"a quoted [DIGEST] in the stalled reply must not block the merge: {rescued.get('meta')}",
        )
        assert_true(
            "core/telemetry.py:44" in rescued_content,
            "a [DIGEST] named inside the body must not truncate the sections behind it",
        )
        assert_true(
            len(_DIGEST_HEADER.findall(rescued_content)) == 1,
            "the stalled reply's own digest header must be cut, leaving one real block "
            "— the quotation in the body is prose and does not count",
        )


        # The quotation can also sit ALONE on its line, where no amount of header-shape
        # matching separates it from the real thing. What does: a digest is the last thing
        # a reply writes, so sections FOLLOWING the marker mean the reply kept going and
        # the marker was quoted. Cutting there cost every section behind it.
        standalone_quote = (
            "[EVIDENCE]\n"
            "confidence: high\n"
            "grounded:\n"
            "- token estimate is chars//4 [core/executor.py:808]\n"
            "the template reads:\n"
            "\n"
            "[DIGEST]\n"
            "summary: <what main_agent needs>\n"
            "\n"
            "scope_covered:\n"
            "- core/executor.py\n"
            "uncertainties:\n"
            "- cost fields unread [core/job_manager.py:191]\n"
        )
        kept = Executor(
            adapter=_ScriptedAdapter([standalone_quote, _DIGEST_ONLY])
        ).execute("explore", "map the telemetry", _session(), str(root))
        kept_content = kept.get("content") or ""
        assert_true(
            "core/job_manager.py:191" in kept_content,
            "sections written AFTER a quoted [DIGEST] prove it was not the block, and "
            "must survive the merge",
        )

        # The tail-shape check passes whenever nothing after the marker HEADS a section,
        # so a quotation followed by plain prose still slips through it. The retention
        # floor is what stops that cut: a trim that discards most of the reply did not
        # find the block this reply ends with, whatever it found.
        prose_tail = (
            "[EVIDENCE]\n"
            "confidence: high\n"
            "grounded:\n"
            "- token estimate is chars//4 [core/executor.py:808]\n"
            "quoting the template:\n"
            "\n"
            "[DIGEST]\n"
            "summary: <what main_agent needs>\n"
            "\n"
            "- residual note 1 on telemetry [core/job_manager.py:191]\n"
            "- residual note 2 on telemetry [core/job_manager.py:192]\n"
            "- residual note 3 on telemetry [core/job_manager.py:193]\n"
            "- residual note 4 on telemetry [core/job_manager.py:194]\n"
            "- residual note 5 on telemetry [core/job_manager.py:195]\n"
            "- residual note 6 on telemetry [core/job_manager.py:196]\n"
            "- residual note 7 on telemetry [core/job_manager.py:197]\n"
            "- residual note 8 on telemetry [core/job_manager.py:198]\n"
            "- residual note 9 on telemetry [core/job_manager.py:199]\n"
            "- residual note 10 on telemetry [core/job_manager.py:200]\n"
            "- residual note 11 on telemetry [core/job_manager.py:201]\n"
            "uncertainties: none\n"
        )
        floored = Executor(
            adapter=_ScriptedAdapter([prose_tail, _DIGEST_ONLY])
        ).execute("explore", "map the telemetry", _session(), str(root))
        floored_content = floored.get("content") or ""
        assert_true(
            "core/job_manager.py:200" in floored_content,
            "a trim that would discard most of the body must be refused, quoted marker "
            "or not",
        )

        # Header matching is stricter than the substring search it replaced, and strictness
        # has its own failure mode: a class of `[ \t]*` before `$` excludes `\r`, so every
        # reply from a provider that emits CRLF reads as a MISSING digest. That is the
        # false truncation this helper exists to prevent, arriving from the other side.
        crlf_adapter = _ScriptedAdapter([_EVIDENCE.replace("\n", "\r\n"), _DIGEST_ONLY])
        crlf = Executor(adapter=crlf_adapter).execute(
            "explore", "map the telemetry", _session(), str(root)
        )
        assert_true(
            len(crlf_adapter.calls) == 1,
            "a complete CRLF reply carries its digest and must not trigger a continuation",
        )
        assert_true(
            "resumed and finished" in (crlf.get("content") or ""),
            "the CRLF reply's own digest must survive, not be replaced by a retry's",
        )

        # The reply that failed the contract is archived beside the one that replaced it.
        # Without it the only trace of a failed contract is its character count, and asking
        # later WHY it failed means guessing at text the recovery threw away.
        first_files = sorted(
            (data_dir(root) / "sessions").glob("*/logs/*/output.first.md")
        )
        assert_true(
            bool(first_files),
            "a continuation must archive the reply it was asked to complete",
        )
        assert_true(
            any("core/executor.py:808" in f.read_text(encoding="utf-8") for f in first_files),
            "the archived first reply must be the failing text itself, not a summary",
        )
        # Archival is diagnostics for a call that already succeeded. A disk that cannot
        # take the file must not turn a recovered run into a failed one.
        import exoma.core.evidence.runtime_io as _io

        def _refuse(*_a, **_k):
            raise OSError("no space left on device")

        _real_write = _io.atomic_write_text
        _io.atomic_write_text = _refuse
        try:
            _io.write_first_reply(
                root,
                "some first reply",
                prompt_id="20260101_000000_explore_deadbeef",
                session_id=_session()["session_id"],
            )
        except Exception as exc:
            raise AssertionError(f"archival I/O must not escape: {exc!r}") from exc
        finally:
            _io.atomic_write_text = _real_write
        # Bounded: an agent that cannot produce the contract twice is a real failure, and
        # must surface as one rather than being retried until the quota is gone.
        hopeless = _ScriptedAdapter([_STALLED, _STALLED])
        failed = Executor(adapter=hopeless).execute(
            "analyze", "map the thing", _session(), str(root)
        )
        assert_true(
            not failed.get("ok")
            and (failed.get("meta") or {}).get("error_type") == "invalid_evidence",
            f"two contract-less replies must still fail: {failed.get('meta')}",
        )
        assert_true(
            len(hopeless.calls) == 2,
            f"the continuation must be attempted once, never looped: calls={len(hopeless.calls)}",
        )
        assert_true(
            "continuation" in (failed.get("content") or "")
            or "continuation" in str(failed.get("meta")),
            f"the failure must say a continuation was already tried: {failed.get('meta')}",
        )

        # Without a captured provider session there is no thread to continue; asking again
        # would open an empty one. Fail, but say which of the two happened.
        orphan = _ScriptedAdapter([_STALLED], provider_session_id=None)
        no_session = Executor(adapter=orphan).execute(
            "analyze", "map the thing", _session(None), str(root)
        )
        assert_true(
            not no_session.get("ok") and len(orphan.calls) == 1,
            f"a sessionless stall must not be re-prompted into a fresh thread: calls={len(orphan.calls)}",
        )

        # verify has its own contract, and the same stall. A block that is merely absent
        # must be asked for; a verdict the agent reached deliberately must not be.
        verify_stalled = _ScriptedAdapter([_STALLED, _VERIFICATION])
        verified = Executor(adapter=verify_stalled).execute(
            "verify", "check the six fixes", _session(), str(root)
        )
        verify_meta = verified.get("meta") or {}
        assert_true(
            verified.get("ok") and verify_meta.get("continuation_recovered") is True,
            f"a verify reply missing its contract must be continued too: {verify_meta}",
        )
        # Imported rather than restated: two copies of this set drifted apart once already,
        # and the copy here is the one that decides whether the assertion means anything.
        shape_kinds = _VERIFY_SHAPE_KINDS
        assessment = validate_verification_contract(verified.get("content") or "")
        assert_true(
            not [
                warning
                for warning in assessment.get("warnings") or []
                if warning.get("kind") in shape_kinds
            ],
            f"the continued verify output must satisfy the contract: {assessment}",
        )

        # An agent that DID emit the block and honestly declared the work incomplete has
        # finished. Re-prompting it would be asking it to talk itself out of a true answer.
        honest = _VERIFICATION.replace("verdict: DONE", "verdict: INCOMPLETE").replace(
            "not_verified: none", "not_verified:\n- hooks/*.sh never executed"
        )
        settled = _ScriptedAdapter([honest])
        settled_result = Executor(adapter=settled).execute(
            "verify", "check the six fixes", _session(), str(root)
        )
        assert_true(
            settled_result.get("ok") and len(settled.calls) == 1,
            f"an honest incomplete verdict must be left alone: calls={len(settled.calls)}",
        )

        # A "nothing found" spelled in a way the parser does not take must be steered back
        # to `- none`. Asking only for tags is how the entry got tagged as a finding and a
        # clean verify became a fail (CASE-009).
        from exoma.core.provider.continuation import _contract_gap

        odd_none = _VERIFICATION.replace(
            "blocking_findings: none", "blocking_findings:\n- semua jalur bersih"
        )
        gap = _contract_gap("verify", "verification", {"ok": True, "content": odd_none, "meta": {}})
        assert_true(
            gap is not None and "`- none`" in gap.get("wants", ""),
            f"the re-prompt names `- none` for an entry that only says nothing was found: {gap}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_continuation_prompt_is_bounded() -> None:
    """The recovery prompt must fit the same command line every other prompt does.

    It quotes the reply that failed, so its size is chosen by the provider rather than by
    this runtime: one malformed tag can carry a paragraph, and four of them a page. The
    prompt travels through argv on opencode like any other, and an oversized one costs the
    recovery itself — the adapter refuses the very call that existed to rescue the run,
    turning a recoverable stall into a lost one.
    """
    from exoma.core.provider.continuation import _MISSING_CHARS, _continuation_prompt

    gap = {
        "reason": "verification contract emitted but malformed",
        "missing": ["x" * 5000] * 4,
        "wants": "the full [VERIFICATION] block",
    }
    prompt = _continuation_prompt("verify", gap)
    # opencode's own accounting: each newline becomes ` \\n ` in argv.
    cost = len(prompt) + 3 * prompt.count(chr(10))
    assert_true(
        cost < 8191 - 400,
        f"a continuation quoting 20000 chars of malformed detail built a {cost}-char "
        "command line, which the adapter refuses — the recovery call cannot be the one "
        "prompt nobody sized",
    )
    assert_true(
        str(len("x" * 5000 * 4 + "; " * 3) - _MISSING_CHARS) in prompt,
        "the truncation is silent: a reader cannot tell a short list of missing fields "
        "from a long one that was cut",
    )
