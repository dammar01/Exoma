"""Choosing which promoted knowledge a delegated call should see, and whether it applies.

Two questions the pack keeps apart, and so does this module:

    validity      is the claim still true in production?          -> verify.py
    applicability is it safe to describe the branch in hand?      -> here

They come apart on any feature branch. Knowledge verified on main stays true of main
while the branch under the developer's hands deliberately changes it, and a retrieval
layer that only asked the first question would hand the agent a confident, correct,
irrelevant answer.

Divergence is resolved per document, never globally. A branch that rewrites
authentication says nothing about payment, and blanking every document because one of
them is under construction throws away the knowledge the session most likely needs.
"""

import re
from pathlib import Path

from exoma.core.evidence.fact_store import MIN_RELEVANCE_SCORE, relevance
from exoma.core.knowledge import schema, store, verify
from exoma.utils import git

APPLICABLE = "applicable"
EXCLUDED = "excluded"
NEEDS_BRANCH_VERIFICATION = "needs_branch_verification"

# Same shape as the fact shortlist: a handful of leads, not a library. The sidecar is a
# starting point for an agent that can still read the code, and a long list buys
# breadth at the cost of the agent treating it as the answer.
DEFAULT_LIMIT = 3


def _score(doc: dict, task: str) -> tuple[float, int]:
    """(weighted score, identifier hits) of one document for one task.

    The fact store's `relevance`, applied to the document's own vocabulary with its
    anchored paths as the path text — matched rather than improved past it: two
    selectors with different ideas of relevance would make the facts and the knowledge
    in one prompt disagree about what the task is.
    """
    text = " ".join(
        [
            str(doc.get("id", "")).replace("-", " "),
            str(doc.get("title", "")),
            str(doc.get("summary", "")),
            " ".join(
                str(c.get("statement", "")) for c in (doc.get("claims") or []) if isinstance(c, dict)
            ),
        ]
    )
    paths = " ".join(str(p) for p in (doc.get("anchors", {}) or {}).get("paths", []) or [])
    return relevance(task, text, paths)


def _with_link_targets(project_root: Path, paths: list) -> set[str]:
    """Anchored paths plus the in-project target of each one reached through a symlink.

    An edit lands on the target, never on the link: Git reports the target as changed
    and the alias as clean, so comparing aliases alone called a document applicable while
    its cited file had moved under it. Paths are only resolved here, never read; a path
    that leaves the project or cannot be resolved contributes itself alone.
    """
    anchored = {p for p in paths if isinstance(p, str) and p}
    try:
        root = Path(project_root).resolve()
    except (OSError, RuntimeError, ValueError):
        return anchored
    targets: set[str] = set()
    for path in anchored:
        relative = Path(path.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        try:
            resolved = (root / relative).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.is_relative_to(root):
            target = resolved.relative_to(root).as_posix()
            if target != relative.as_posix():
                targets.add(target)
    return anchored | targets


def applicability(
    project_root: Path,
    doc: dict,
    *,
    branch: str | None,
    production_ref: str,
    comparison: dict | None = None,
) -> dict:
    """Whether this document may describe the current branch, and which claims survive.

    Order follows the pack's pseudo-policy: an explicit exclusion wins over a diff. A
    developer who wrote down "this branch reworks auth" knows something the diff cannot
    show — that the change is intended and incomplete — and that statement should not be
    overridden by a file that happens not to be touched yet.
    """
    exclusions = (doc.get("applicability") or {}).get("excluded_branches") or []
    excluded_claims: list[str] = []
    for exclusion in exclusions:
        if not isinstance(exclusion, dict):
            continue
        pattern = exclusion.get("pattern")
        if branch is None or not isinstance(pattern, str) or not store.matches_branch(pattern, branch):
            continue
        affected = exclusion.get("affected_claims")
        if not affected:
            return {
                "status": EXCLUDED,
                "reason": f"branch excluded: {exclusion.get('reason')}",
                "excluded_claims": [],
            }
        excluded_claims.extend(str(c) for c in affected)

    changed = comparison.get("paths") if comparison is not None else git.changed_paths(project_root, production_ref)
    if changed is None:
        # Could not compare. Reporting "applicable" here would assert something the
        # repository never confirmed, on the branch where being wrong matters most.
        return {
            "status": NEEDS_BRANCH_VERIFICATION,
            "reason": f"cannot compare {production_ref} with the index and working tree",
            "excluded_claims": sorted(set(excluded_claims)),
        }

    anchored = _with_link_targets(project_root, (doc.get("anchors") or {}).get("paths") or [])
    overlap = sorted(anchored & changed)
    if not overlap:
        return {
            "status": APPLICABLE,
            "reason": "commit, index and working tree leave this document's anchored paths unchanged",
            "excluded_claims": sorted(set(excluded_claims)),
        }
    return {
        "status": NEEDS_BRANCH_VERIFICATION,
        "reason": f"commit, index or working tree changes anchored path(s): {', '.join(overlap)}",
        "excluded_claims": sorted(set(excluded_claims)),
    }


def load_relevant(
    project_root: Path,
    task: str,
    *,
    production_ref: str,
    limit: int = DEFAULT_LIMIT,
) -> list[dict]:
    """The knowledge entries worth putting in front of a delegated call.

    Documents that are excluded outright are dropped rather than shipped with a warning:
    a sidecar entry the agent is told to ignore still spends context and still invites
    being used.
    """
    branch = git.current_branch(project_root)
    head = git.head_commit(project_root)
    worktree = {"paths": git.changed_paths(project_root)}
    comparison = {"paths": git.changed_paths(project_root, production_ref)}
    cache: dict = {}
    scored: list[tuple[tuple, dict]] = []
    for doc_id in store.list_docs(project_root):
        loaded = store.load(project_root, doc_id)
        doc = loaded.get("doc")
        if not loaded.get("ok") or not isinstance(doc, dict):
            continue
        if schema.validate(doc):
            continue
        verdict = applicability(
            project_root, doc, branch=branch, production_ref=production_ref, comparison=comparison
        )
        if verdict["status"] == EXCLUDED:
            continue
        excluded = set(verdict["excluded_claims"])
        claims = []
        retained = []
        verified_commit = (doc.get("production") or {}).get("verified_commit")
        for claim in doc.get("claims") or []:
            if not isinstance(claim, dict) or claim.get("id") in excluded:
                continue
            freshness = verify.verify_claim(
                project_root, claim, head=head, verified_commit=verified_commit,
                cache=cache, worktree=worktree,
            )
            if freshness["status"] in (verify.SOURCE_MISSING, verify.NEEDS_VERIFICATION):
                continue
            retained.append(claim)
            sources = [s for s in claim.get("sources") or [] if isinstance(s, dict)]
            claims.append({
                "id": claim.get("id"),
                "type": claim.get("type"),
                "statement": claim.get("statement"),
                "freshness": freshness["status"],
                "sources": [
                    f"{s.get('path')}:{checked.get('line', (s.get('lines') or {}).get('start'))}"
                    for s, checked in zip(sources, freshness["sources"])
                    if s.get("type") == "code"
                ],
            })
        if not claims:
            continue
        # The original summary may describe excluded/stale claims. Only surviving
        # statements may influence ranking or ride the knowledge sidecar.
        summary = " ".join(str(c.get("statement", "")) for c in retained)
        score, id_hits = _score({**doc, "summary": summary, "claims": retained}, task)
        if score < MIN_RELEVANCE_SCORE:
            continue
        scored.append(
            (
                (-score, -id_hits, str(doc.get("id"))),
                {
                    "id": doc.get("id"),
                    "title": doc.get("title"),
                    "summary": summary,
                    "applicability": verdict["status"],
                    "applicability_reason": verdict["reason"],
                    "verified_commit": (doc.get("production") or {}).get("verified_commit"),
                    "claims": claims,
                },
            )
        )

    scored.sort(key=lambda pair: pair[0])
    return [entry for _score_value, entry in scored[:limit]]


def prune_dead_exclusions(project_root: Path, doc: dict) -> dict:
    """Drop branch exclusions whose branch no longer exists.

    The cost of keeping exclusions inside the Git-tracked document: branch names are
    short-lived and the file is not, so without this the list grows one dead entry per
    merged feature branch until nobody trusts any of it. Bounded here rather than argued
    about later.
    """
    applicability_block = doc.get("applicability")
    if not isinstance(applicability_block, dict):
        return {"doc": doc, "dropped": []}
    exclusions = applicability_block.get("excluded_branches")
    if not isinstance(exclusions, list) or not exclusions:
        return {"doc": doc, "dropped": []}

    living = git.branches(project_root)
    if living is None:
        # Cannot enumerate branches; keeping every exclusion is the conservative side.
        return {"doc": doc, "dropped": []}

    kept, dropped = [], []
    for exclusion in exclusions:
        pattern = exclusion.get("pattern") if isinstance(exclusion, dict) else None
        if isinstance(pattern, str) and not any(
            store.matches_branch(pattern, branch) for branch in living
        ):
            dropped.append(pattern)
            continue
        kept.append(exclusion)

    if not dropped:
        return {"doc": doc, "dropped": []}
    updated = dict(doc)
    updated["applicability"] = {**applicability_block, "excluded_branches": kept}
    return {"doc": updated, "dropped": dropped}
