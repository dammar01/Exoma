"""The git queries the promote flow needs, in one place.

Three call sites already shell out to git independently — core/graph/graph_meta.py
(`rev-parse HEAD`), core/evidence/quick_verify.py (`_git_lines`), and
core/audit/diagnostics.py (`git_run`). This module deliberately does NOT absorb them
yet: rewriting three verified paths to introduce a helper buys no user-visible
behaviour and hands back a real regression surface. It owns the queries promote
needs, and is where those three belong when one of them next needs editing anyway.

Every subprocess here carries `osutil.hidden_run_kwargs()`. On Windows a console
program inherits its parent's console, and allocates a NEW one when the parent has none
— which is exactly the delegated worker's situation, spawned DETACHED_PROCESS with no
console of its own. Without the flags, `core/knowledge/retrieve.py` asking for the
current branch flashes a console window on the user's screen once per delegated call.

Every function answers with None on failure rather than raising. A promote precondition
that cannot resolve the repository must produce a readable block reason, not a traceback
from inside a subprocess call four frames down.
"""

import subprocess
from pathlib import Path

from exoma.utils import osutil

GIT_TIMEOUT_SECONDS = 10


def _git(args: list[str], cwd: Path) -> str | None:
    """stdout of `git <args>`, stripped. None when git fails, is missing, or hangs."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            **osutil.hidden_run_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def toplevel(project_root: Path) -> Path | None:
    """The git repository root, which is NOT always the project root.

    A project inside a monorepo, a submodule, or a worktree has a project root below
    the repository root, and every path recorded in promoted knowledge is resolved
    against one of the two. Recording them against the wrong one produces anchors that
    point at nothing on another developer's checkout.
    """
    out = _git(["rev-parse", "--show-toplevel"], project_root)
    return Path(out) if out else None


def current_branch(project_root: Path) -> str | None:
    """The checked-out branch name. None when detached, mid-rebase, or on an empty repo.

    `rev-parse --abbrev-ref HEAD` answers the literal string "HEAD" on a detached head,
    which is not a branch and must never be compared against the configured production
    branch — a repo detached exactly at main would otherwise read as "not on main" or,
    worse, as some branch literally named HEAD. Collapsed to None so callers have one
    "no branch here" case instead of two.
    """
    out = _git(["rev-parse", "--abbrev-ref", "HEAD"], project_root)
    if not out or out == "HEAD":
        return None
    return out


def head_commit(project_root: Path) -> str | None:
    """Full SHA of HEAD. None on a repository with no commits yet."""
    return _git(["rev-parse", "HEAD"], project_root)


def branches(project_root: Path) -> list[str] | None:
    """Every local branch name. None when the repository cannot be read.

    None is not an empty list here either: "this repo has no branches" and "I could not
    ask" lead to opposite decisions when deciding whether a branch exclusion still
    protects anything.
    """
    out = _git(["for-each-ref", "--format=%(refname:short)", "refs/heads/"], project_root)
    if out is None:
        return None
    return [line for line in out.splitlines() if line]


def blob_oid(project_root: Path, path: str, ref: str = "HEAD") -> str | None:
    """Git's content id for one path at one ref. None when the path is not tracked there.

    Cheap file-level staleness: an unchanged blob means nothing in the file moved, so
    every claim anchored in it can be passed without reading the file. A CHANGED blob
    proves nothing on its own — a touched comment changes it — which is why this sits
    above the anchor check in the ladder rather than replacing it.
    """
    out = _git(["rev-parse", f"{ref}:{path}"], project_root)
    return out or None


def diff_names(project_root: Path, ref_a: str, ref_b: str = "HEAD") -> list[str] | None:
    """Paths that differ between two refs. None when either ref cannot be resolved.

    None and [] mean different things and callers must keep them apart: [] is "compared,
    nothing differs", None is "could not compare". Treating the second as the first would
    quietly report every promoted claim as applicable on a branch nobody could diff.
    """
    out = _git(["diff", "--name-only", f"{ref_a}...{ref_b}"], project_root)
    if out is None:
        return None
    return [line for line in out.splitlines() if line]


def is_ignored(project_root: Path, path: str) -> bool:
    """Whether git ignores this path.

    The gate that keeps `.env` out of Git-tracked knowledge. Pattern-matching the
    content cannot do this job: `DB_PASSWORD=hunter2` matches none of the shapes in
    utils/redact.py and never will, because it has no shape — it is an ordinary word.
    What marks it as secret is not how it looks but where it lives, and that is exactly
    the question `check-ignore` answers.

    Unresolvable git (no repo, git missing) reads as "not ignored" so that a project
    outside version control still works; the branch precondition has already refused to
    promote in that case, so nothing reaches this on that path.
    """
    try:
        completed = subprocess.run(
            ["git", "check-ignore", "-q", "--", path],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            **osutil.hidden_run_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    # 0 = ignored, 1 = not ignored, 128 = not a repo / bad usage.
    return completed.returncode == 0
