"""Old promoted claims versus freshly verified candidates: what changed, and how badly.

Five outcomes, decided by rules applied in order, stopping at the first match:

    1. no existing claim under this id                       -> NEW
    2. the candidate failed fresh verification                -> UNVERIFIED
    3. statements match after normalisation                   -> UNCHANGED
    4. same type, statement differs, every source verified    -> UPDATE
    5. anything else                                          -> CONFLICT

Rule 2 sits above the comparison rules on purpose. A candidate whose evidence did not
hold up is not a smaller version of an update; it is an absence of knowledge, and
letting it reach rule 4 would let unverified text overwrite verified text.

Rule 5 is where the authority collision lands: an existing claim sourced from a user
clarification, contradicted by fresh code. Neither side is obviously right — the code
may have drifted from the intent, or the intent may have been revised and never written
down — so it goes to the user rather than to a heuristic.
"""

from exoma.core.knowledge.verify import NEEDS_VERIFICATION, SOURCE_MISSING

NEW = "NEW"
UNCHANGED = "UNCHANGED"
UPDATE = "UPDATE"
CONFLICT = "CONFLICT"
UNVERIFIED = "UNVERIFIED"

_UNVERIFIED_STATUSES = (NEEDS_VERIFICATION, SOURCE_MISSING)


def normalize_statement(text) -> str:
    """Whitespace-insensitive comparison form.

    Re-wrapping a sentence is not a change in what the project does, and a reconciler
    that reported it as UPDATE would send the reviewer a conflict prompt for a line
    break.
    """
    if not isinstance(text, str):
        return ""
    return " ".join(text.split()).casefold()


def _has_user_source(claim: dict) -> bool:
    return any(
        isinstance(s, dict) and s.get("type") == "user"
        for s in claim.get("sources") or []
    )


def reconcile_claim(existing: dict | None, candidate: dict, verdict: dict | None) -> dict:
    """One claim's status, with the reason stated so the promote plan can print it."""
    status_of_verdict = (verdict or {}).get("status")

    if existing is None:
        if status_of_verdict in _UNVERIFIED_STATUSES:
            return {
                "id": candidate.get("id"),
                "status": UNVERIFIED,
                "reason": "new claim, but its sources did not verify",
            }
        return {"id": candidate.get("id"), "status": NEW, "reason": "no existing claim under this id"}

    if status_of_verdict in _UNVERIFIED_STATUSES:
        return {
            "id": candidate.get("id"),
            "status": UNVERIFIED,
            "reason": "candidate sources did not verify; existing claim left untouched",
        }

    same_statement = normalize_statement(existing.get("statement")) == normalize_statement(
        candidate.get("statement")
    )
    same_type = existing.get("type") == candidate.get("type")

    if same_statement and same_type:
        return {"id": candidate.get("id"), "status": UNCHANGED, "reason": "materially equivalent"}

    if not same_type:
        return {
            "id": candidate.get("id"),
            "status": CONFLICT,
            "reason": (
                f"claim type changed {existing.get('type')!r} -> {candidate.get('type')!r}; "
                "a claim that changed kind is not a revision of the same claim"
            ),
        }

    if _has_user_source(existing) and not _has_user_source(candidate):
        return {
            "id": candidate.get("id"),
            "status": CONFLICT,
            "reason": (
                "existing claim carries user-stated intent that the fresh code-only "
                "candidate contradicts"
            ),
        }

    return {
        "id": candidate.get("id"),
        "status": UPDATE,
        "reason": "same concept, fresh production-backed statement",
    }


def reconcile(
    existing_claims: list[dict] | None,
    candidate_claims: list[dict] | None,
    verdicts: list[dict] | None = None,
) -> dict:
    """Statuses for every candidate, plus the existing claims nothing spoke to.

    Existing claims with no candidate are reported separately rather than deleted.
    Silence in one promotion run means the subject was not re-derived this time, which
    is not evidence that the claim stopped being true — and a run that quietly drops
    claims makes every promotion a potential loss of knowledge.
    """
    existing_by_id = {
        c.get("id"): c for c in (existing_claims or []) if isinstance(c, dict) and c.get("id")
    }
    verdict_by_id = {
        v.get("id"): v for v in (verdicts or []) if isinstance(v, dict) and v.get("id")
    }

    results = []
    for candidate in candidate_claims or []:
        if not isinstance(candidate, dict):
            continue
        claim_id = candidate.get("id")
        results.append(
            reconcile_claim(
                existing_by_id.get(claim_id), candidate, verdict_by_id.get(claim_id)
            )
        )

    touched = {r["id"] for r in results}
    untouched = sorted(cid for cid in existing_by_id if cid not in touched)

    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1

    return {
        "results": results,
        "counts": counts,
        "untouched_existing": untouched,
        "requires_user": [r for r in results if r["status"] == CONFLICT],
        "writable": [r for r in results if r["status"] in (NEW, UPDATE, UNCHANGED)],
    }


def apply_reconciliation(
    existing_claims: list[dict] | None,
    candidate_claims: list[dict] | None,
    decisions: dict,
) -> list[dict]:
    """The claim list to write, given a status per claim id.

    `decisions` maps claim id -> status, and is the USER's answer after review, not the
    reconciler's proposal. The two are kept apart so that approving a plan is a distinct
    act from computing one: nothing here can write a status the user never saw.

    UNVERIFIED and CONFLICT keep the existing claim. Refusing to write is the safe
    direction — a claim that stays one revision behind is recoverable, one overwritten
    by unverified text is not.
    """
    existing_by_id = {
        c.get("id"): c for c in (existing_claims or []) if isinstance(c, dict) and c.get("id")
    }
    candidate_by_id = {
        c.get("id"): c for c in (candidate_claims or []) if isinstance(c, dict) and c.get("id")
    }

    out: dict[str, dict] = dict(existing_by_id)
    for claim_id, status in (decisions or {}).items():
        if status in (NEW, UPDATE) and claim_id in candidate_by_id:
            out[claim_id] = candidate_by_id[claim_id]
        # UNCHANGED keeps what is already there; UNVERIFIED and CONFLICT do too.
    return [out[cid] for cid in sorted(out)]
