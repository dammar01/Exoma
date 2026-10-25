"""The freshness ladder: is a promoted claim still supported by the code it cites?

Four rungs, cheapest first, and the order is the whole point:

    verified_commit == HEAD   -> nothing in the repository moved; no file is read
    blob_oid unchanged        -> nothing in THIS file moved; the file is not read
    anchor still locatable    -> the cited line survives, possibly at a new number
    otherwise                 -> needs verification by someone who can read

Going straight to the anchor check would be correct and slow: it reads and hashes every
cited file on every retrieval. Stopping at the blob check would be fast and wrong: a
touched comment changes the blob while the cited line is untouched, and calling that
"stale" retires knowledge that is still true.

The anchor rung is borrowed rather than rebuilt. core/evidence/fact_store.py already
solved "where does this line live now", including the relocation case, and a second
implementation would be a second set of bugs.
"""

from pathlib import Path

from exoma.core.evidence.fact_store import _anchor_hash, current_anchor_line
from exoma.utils import git

FRESH_COMMIT = "fresh_commit"
FRESH_BLOB = "fresh_blob"
FRESH_ANCHOR = "fresh_anchor"
NEEDS_VERIFICATION = "needs_verification"
SOURCE_MISSING = "source_missing"

# Worst-first. A claim is only as fresh as its weakest source.
_SEVERITY = {
    SOURCE_MISSING: 0,
    NEEDS_VERIFICATION: 1,
    FRESH_ANCHOR: 2,
    FRESH_BLOB: 3,
    FRESH_COMMIT: 4,
}

_STALE = (SOURCE_MISSING, NEEDS_VERIFICATION)


def verify_source(
    project_root: Path,
    source: dict,
    *,
    head: str | None,
    verified_commit: str | None,
    cache: dict | None = None,
) -> dict:
    """One source's rung on the ladder, plus where its anchor sits now.

    A `user` source has no code to check against. It is reported fresh: user
    clarification records intent that the code was never able to prove, so letting the
    code decide whether it survives would discard exactly the knowledge it exists to
    hold.
    """
    if source.get("type") != "code":
        return {"status": FRESH_COMMIT, "reason": "user source, not code-backed"}

    path = source.get("path")
    if not isinstance(path, str) or not path:
        return {"status": SOURCE_MISSING, "reason": "source has no path"}

    if head and verified_commit and head == verified_commit:
        return {"status": FRESH_COMMIT, "reason": "HEAD is the verified commit"}

    if not (Path(project_root) / path).exists():
        return {"status": SOURCE_MISSING, "reason": f"{path} no longer exists"}

    recorded_blob = source.get("blob_oid")
    if recorded_blob:
        current_blob = git.blob_oid(project_root, path)
        if current_blob and current_blob == recorded_blob:
            return {"status": FRESH_BLOB, "reason": f"{path} unchanged since promotion"}

    lines = source.get("lines") or {}
    start = lines.get("start") if isinstance(lines, dict) else None
    anchor = source.get("anchor_hash")
    located = current_anchor_line(project_root, path, start, anchor, cache)
    if located is None:
        return {
            "status": NEEDS_VERIFICATION,
            "reason": f"anchor for {path}:{start} no longer locatable",
        }
    if located != start:
        return {
            "status": FRESH_ANCHOR,
            "reason": f"anchor moved {start} -> {located}",
            "line": located,
        }
    return {"status": FRESH_ANCHOR, "reason": "anchor intact", "line": located}


def verify_claim(
    project_root: Path,
    claim: dict,
    *,
    head: str | None,
    verified_commit: str | None,
    cache: dict | None = None,
) -> dict:
    sources = claim.get("sources") or []
    results = [
        verify_source(
            project_root, source, head=head, verified_commit=verified_commit, cache=cache
        )
        for source in sources
        if isinstance(source, dict)
    ]
    if not results:
        return {"id": claim.get("id"), "status": SOURCE_MISSING, "sources": []}
    worst = min(results, key=lambda r: _SEVERITY[r["status"]])
    return {"id": claim.get("id"), "status": worst["status"], "sources": results}


def verify_doc(project_root: Path, doc: dict) -> dict:
    """Every claim's freshness, plus the counts a promote plan reports.

    `head` is resolved once for the whole document. Resolving it per claim would let a
    commit landing mid-verification split one report across two repository states.
    """
    head = git.head_commit(project_root)
    production = doc.get("production") or {}
    verified_commit = production.get("verified_commit")
    cache: dict = {}

    claims = [
        verify_claim(
            project_root, claim, head=head, verified_commit=verified_commit, cache=cache
        )
        for claim in doc.get("claims") or []
        if isinstance(claim, dict)
    ]
    stale = [c for c in claims if c["status"] in _STALE]
    return {
        "ok": True,
        "id": doc.get("id"),
        "head": head,
        "verified_commit": verified_commit,
        "claims": claims,
        "fresh_count": len(claims) - len(stale),
        "stale_count": len(stale),
        "stale_ids": [c["id"] for c in stale],
    }


def anchor_for(project_root: Path, path: str, line: int) -> str | None:
    """The anchor hash a new code source should record. None when the line cannot be read.

    Exposed so the promote flow records anchors with the same function that will later
    check them; two implementations of "hash this line" drift apart on the first
    whitespace decision.
    """
    return _anchor_hash(Path(project_root), path, line)
