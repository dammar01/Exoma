"""What the project looked like when a browser run failed — so the repeat brake can tell
"the same run again" from "a run after someone changed something".

The brake counts runs that end the same way against one origin and refuses the fourth.
Its purpose is to stop a loop of scenario edits against an environment problem. Before
this, the only ways out were a passing run, a per-request `ignore_repeat_brake`, or a new
session — so a person who had FIXED the application still hit the brake on the next run
and cleared their session to get past it, losing the rest of the session's state.

The fingerprint is the project's source as Git sees it: HEAD, the tracked diff against it,
and the untracked files that are not ignored — all outside `.workflow/`. The scenario,
the request, and the runtime's own state live under `.workflow/`, so editing the scenario
does not change the fingerprint: that is still the loop the brake exists for. A change to
the application's code, config, or fixtures does.

`None` when there is nothing to compare: not a Git work tree, Git missing, or Git failing.
The brake then behaves as it did before this module existed.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from exoma.utils.osutil import hidden_run_kwargs

GIT_TIMEOUT_S = 10
_EXCLUDE = ":(exclude).workflow"


def _git(project_root: Path, *args: str) -> bytes | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(project_root), *args],
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            **hidden_run_kwargs(),  # Windows: the worker has no console, so git would flash one
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def project_fingerprint(project_root: Path) -> str | None:
    """A short hash of the project's working state outside `.workflow/`, or None."""
    root = Path(project_root)
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != b"true":
        return None
    diff = _git(root, "diff", "HEAD" if head else "--cached", "--binary", "--", ".", _EXCLUDE)
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", _EXCLUDE)
    if diff is None or untracked is None:
        return None
    digest = hashlib.sha256()
    digest.update(head or b"no-head")
    digest.update(b"\0diff\0")
    digest.update(diff)
    digest.update(b"\0untracked\0")
    for name in sorted(n for n in untracked.split(b"\0") if n):
        digest.update(name)
        try:
            # Content, not mtime: touching a file is not a change, and a rebuilt file
            # with the same bytes is not one either.
            digest.update(hashlib.sha256((root / name.decode("utf-8", "surrogateescape")).read_bytes()).digest())
        except OSError:
            digest.update(b"\0unreadable\0")
    return digest.hexdigest()[:16]
