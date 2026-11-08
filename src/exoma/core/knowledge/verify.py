"""The freshness ladder: is a promoted claim still supported by the code it cites?

Four rungs, cheapest first, and the order is the whole point:

    clean source + verified_commit == HEAD -> no committed change; no file is read
    clean source + blob_oid unchanged      -> no committed change in this file
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

import os
from pathlib import Path, PureWindowsPath

from exoma.core.evidence.fact_store import _anchor_hash, current_anchor_line
from exoma.core.policy.secret_patterns import is_secret_path
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
    worktree: dict | None = None,
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

    # Retrieval now invokes this automatically on repository-authored documents. A
    # manually edited source must not turn verification into an arbitrary file read.
    relative = Path(path.replace("\\", "/"))
    root = Path(project_root).resolve()
    if relative.is_absolute() or PureWindowsPath(path).drive or ".." in relative.parts:
        return {"status": NEEDS_VERIFICATION, "reason": "source must stay inside the project"}
    try:
        resolved = (root / relative).resolve()
    except (OSError, RuntimeError, ValueError):
        return {"status": NEEDS_VERIFICATION, "reason": "cannot resolve source safely"}
    if not resolved.is_relative_to(root):
        return {"status": NEEDS_VERIFICATION, "reason": "source leaves the project"}
    path = relative.as_posix()
    target = resolved.relative_to(root).as_posix()
    if is_secret_path(path) or is_secret_path(target):
        return {"status": NEEDS_VERIFICATION, "reason": "secret or ignored source is not read"}

    if worktree is None:
        worktree = {"paths": git.changed_paths(project_root)}
    ignored = worktree.setdefault("ignored", {})
    # A symlink (or a symlinked directory on the way) is read through to its target, so
    # an ignored target must be refused like an ignored path: checking the alias alone
    # let a tracked link expose a file the ignore rules keep out of automatic reads.
    for candidate in dict.fromkeys((path, target)):
        if candidate not in ignored:
            ignored[candidate] = git.is_ignored(root, candidate)
        if ignored[candidate]:
            return {"status": NEEDS_VERIFICATION, "reason": "secret or ignored source is not read"}

    if not (Path(project_root) / path).exists():
        return {"status": SOURCE_MISSING, "reason": f"{path} no longer exists"}

    changed = worktree.get("paths")
    # Without Git, committed shortcuts are unavailable, but a working-file anchor can
    # still be checked. Applicability separately reports an unresolved branch comparison.
    # An alias's own entry says nothing about its target: Git stores a symlink as a blob
    # holding the link text, so an edited target leaves the alias clean and its blob
    # unchanged. Both shortcuts are skipped and the anchor is read through the link.
    # A spelling that differs only in case (Windows: resolve() returns the on-disk case)
    # is the same file, not an alias. Neither spelling is reliably Git's: with
    # core.ignoreCase, Git keeps the index spelling after an on-disk case-only rename, so
    # it can report `Case.py` dirty while resolve() says `case.py`. Membership is checked
    # under the same normalization for both spellings; on POSIX normcase is the identity
    # and this is the exact comparison it always was.
    same_file = os.path.normcase(path) == os.path.normcase(target)
    folded = None
    if changed is not None:
        folded = worktree.get("folded")
        if folded is None:
            folded = worktree["folded"] = {os.path.normcase(p) for p in changed}
    clean = (
        folded is not None
        and same_file
        and os.path.normcase(path) not in folded
        and os.path.normcase(target) not in folded
    )
    if clean and head and verified_commit and head == verified_commit:
        return {"status": FRESH_COMMIT, "reason": "HEAD is the verified commit and source is clean"}

    recorded_blob = source.get("blob_oid")
    if clean and recorded_blob:
        current_blob = git.blob_oid(project_root, target)
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
    worktree: dict | None = None,
) -> dict:
    if worktree is None:
        worktree = {"paths": git.changed_paths(project_root)}
    sources = claim.get("sources") or []
    results = [
        verify_source(
            project_root, source, head=head, verified_commit=verified_commit, cache=cache,
            worktree=worktree,
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
    worktree = {"paths": git.changed_paths(project_root)}

    claims = [
        verify_claim(
            project_root, claim, head=head, verified_commit=verified_commit, cache=cache,
            worktree=worktree,
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
