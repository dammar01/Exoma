"""Deciding whether a reply stopped short, and asking for the rest of it."""

from exoma.core.evidence.contract import (
    STRUCTURAL_KINDS,
    contract_warnings,
    digest_split,
    digest_trim_point,
    validate_verification_contract,
)


# Markers that say the reply is evidence rather than conversation. Kept as one list so the
# gap detector and the failure path below cannot drift apart.
_EVIDENCE_MARKERS = (
    "[evidence]",
    "[digest]",
    "[exploration result]",
    "entry_points",
    "grounded:",
    "assumptions:",
    "scope_covered",
)

# Verify warnings that describe the SHAPE of the reply, not its findings. A run that
# emitted a complete [VERIFICATION] block and honestly declared INCOMPLETE is finished
# work and must never be re-prompted; one missing the fields never got there.
_VERIFY_SHAPE_KINDS = {
    "missing_fields",
    "empty_section",
    "checks_missing",
    "invalid_confidence",
    "invalid_finding_tags",
    "verdict_mismatch",
}

# `finding_misrouted` is deliberately NOT here. The router already moves a blocking-class
# finding into the verdict on its own, so nothing is lost by leaving it where the agent
# put it — and a re-prompt would only offer it the chance to retract a true finding.
# Both name the sentinel. Without it, an entry that only said nothing was found came back
# as a finding the agent was told neither to remove nor to leave untagged — so it got tags,
# and the verdict was moved to match it: a clean verify turned into a fail (CASE-009).
_VERIFY_WANT_BY_KIND = {
    "invalid_finding_tags": (
        "the same findings again, each opening line carrying severity, origin and "
        "scope_relation — no new findings, no removals; an entry that only says nothing "
        "was found is not a finding: write it as exactly `- none`, without tags"
    ),
    "verdict_mismatch": (
        "the verdict line alone, made consistent with the blocking_findings you already "
        "reported — change the verdict, not the findings; but if an entry under "
        "blocking_findings only says nothing was found, write it as exactly `- none` and "
        "keep the verdict"
    ),
}


def _contract_gap(command: str, role: str, result) -> dict | None:
    """Did the reply stop before the contract, or is it simply not evidence?

    Distinct from a failed call, and distinct from a refusal. The observed case: the
    second agent reads every file, then hits its own context ceiling and hands back a
    work-state summary ending in "continue if you have next steps" — real work done,
    contract never emitted. Treating that as failure throws away a completed read and
    sends the user to /.local for evidence that already exists.

    Returns None when there is nothing to continue: a failed call, a shape that is
    already correct, or a verdict the agent reached deliberately.
    """
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    content = result.get("content") or ""
    meta = result.get("meta") or {}

    if command == "verify":
        if meta.get("mode") == "quick" or "quick_verify" in meta:
            return None
        assessment = validate_verification_contract(content)
        shape = [
            warning
            for warning in assessment.get("warnings") or []
            if warning.get("kind") in _VERIFY_SHAPE_KINDS
        ]
        if not shape:
            return None
        kinds = {warning.get("kind") for warning in shape}
        # A block that arrived whole but tagged wrong needs a correction, not a rewrite:
        # asking for the whole thing again invites the agent to redo the reading too.
        narrow = [_VERIFY_WANT_BY_KIND[kind] for kind in sorted(kinds) if kind in _VERIFY_WANT_BY_KIND]
        if narrow and len(narrow) == len(kinds):
            wants = " and ".join(narrow)
            reason = "verification contract emitted but malformed"
        else:
            wants = (
                "the full [VERIFICATION] block — verdict, blocking_findings, "
                "escalations, notes, checks_run, not_verified, confidence — "
                "followed by [DIGEST]"
            )
            reason = "verification contract fields absent"
        return {
            "reason": reason,
            "missing": [warning.get("detail") for warning in shape],
            "wants": wants,
        }

    if role in ("exploration", "reasoning"):
        body = content.lower()
        if not any(marker in body for marker in _EVIDENCE_MARKERS):
            return {
                "reason": "no evidence contract in the reply",
                "missing": ["none of the evidence section markers are present"],
                "wants": "the normal [EVIDENCE] block followed by [DIGEST]",
            }
        damaged = [
            issue["kind"]
            for issue in contract_warnings(command, content)
            if issue["kind"] in STRUCTURAL_KINDS
        ]
        if damaged:
            return {
                "reason": "reply ended before its digest",
                "missing": damaged,
                "wants": "the missing [DIGEST] block",
            }
    return None

# How much of the first reply the digest trim may discard before the cut is read as a
# mis-hit rather than a trim. A digest that belongs to this reply sits at its END, so
# trimming it costs a tail; a cut that takes half the body found something else.
_MIN_BODY_RETENTION = 0.5

def _merge_continuation(first: str | None, retry: str | None) -> str:
    """Keep the work from the first reply and the contract block from the second.

    The continuation prompt asks for the missing block ONLY, so the follow-up comes back
    carrying a digest and little else. Swapping it in wholesale threw away the evidence the
    run had already paid for: artifacts landed holding a digest and zero anchors while the
    body that earned them was discarded, and the reader was left summarising a summary.
    """
    first = (first or "").strip()
    retry = (retry or "").strip()
    if not first:
        return retry
    if not retry:
        return first
    # An agent that ignored "do NOT redo it" and re-sent the whole answer supersedes the
    # first reply outright; concatenating there would duplicate every section.
    head = "\n".join(first.splitlines()[:3]).strip()
    if head and head in retry:
        return retry
    # Everything past the digest header is digest anyway, so the body is what precedes it
    # and the truncated header the first reply stopped inside has to go: left in place it
    # sits in front of the complete block behind it, and the contract reads the shadow.
    #
    # Two things narrow that cut. `digest_split` anchors to a standalone HEADER and takes
    # the LAST one, so a marker named mid-sentence no longer counts as the start of a
    # section. The retention floor catches what that still cannot: a quotation that happens
    # to sit alone on its line. A task ABOUT this contract names "[DIGEST]" in its findings
    # as a matter of course, and cutting there discarded every section that followed the
    # mention — an artifact then indexed, and its anchors counted, from what survived.
    if digest_split(retry) is not None:
        cut = digest_trim_point(first)
        if cut is not None:
            trimmed = first[:cut].rstrip()
            if len(trimmed) >= len(first) * _MIN_BODY_RETENTION:
                first = trimmed
    if not first:
        return retry
    return f"{first}\n\n{retry}"

# Enough to name every missing field several times over; far short of the argv budget
# the recovery call has to fit.
_MISSING_CHARS = 800


def _continuation_prompt(command: str, gap: dict) -> str:
    """Ask for the missing part only — the work itself already happened."""
    missing = "; ".join(str(item) for item in gap.get("missing") or []) or "unknown"
    # Bounded because this text is quoted FROM the reply that failed, so its size is set by
    # the provider rather than by us — a malformed tag can carry a paragraph. This prompt is
    # argv on opencode like any other, and an oversized one costs the recovery itself: the
    # adapter refuses the call that existed to rescue the run.
    if len(missing) > _MISSING_CHARS:
        missing = missing[:_MISSING_CHARS] + f" …(+{len(missing) - _MISSING_CHARS} chars)"
    return (
        f"Your previous reply for this {command} call stopped before the required "
        f"output: {gap['reason']} ({missing}).\n"
        "The work you already did in this session still counts. Do NOT redo it and do "
        "NOT start over.\n"
        f"Reply with {gap['wants']}, built from what you already found. If some part is "
        "genuinely unverified, say so in the field it belongs to rather than omitting "
        "the field."
    )
