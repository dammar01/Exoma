"""Did the second_agent write to the workspace? Asked before and after every agy call.

Every other provider answers this by construction. OpenCode is held to a `permission`
block that `core/policy/opencode_policy.py` overwrites rather than merges, and codex runs under
`--sandbox read-only`. agy has neither, and not for want of looking: `--sandbox` and
`--mode plan` were both probed against the shipped binary and changed nothing — same 56
tools, same `permission_mode: always-proceed`, `write_to_file` and `run_command` among
them. Dropping `--dangerously-skip-permissions` does move the mode to `request-review`,
but that mode refuses EVERY tool, reads included, which leaves a provider that cannot
gather evidence. There is no third setting and no config file to write one into: agy keeps
nothing under the user's home but `bin/`.

So the boundary cannot be enforced, and this module does not pretend otherwise. It
DETECTS. The write has already happened by the time the second snapshot is taken; what
this buys is that the change is named in the result instead of discovered days later in a
diff nobody attributed to a delegated call.

Three things it does not see, stated here rather than in a footnote, because a guard
trusted past its range is worse than no guard:

  * anything matched by .gitignore — build output, caches, .env
  * any project that is not a git repository, where it reports itself unavailable
  * a file written and restored within the same call

`git status --porcelain` is the whole mechanism. It is fast enough to run twice per
delegated call on a large repo, it already understands the ignore rules, and it needs no
state of our own to keep in sync.
"""

import subprocess
from pathlib import Path

from utils import osutil

# A snapshot must never be the reason a call fails. Ten seconds is far past what
# `git status` needs on a large repo, and reaching it means git is wedged, not slow.
_SNAPSHOT_TIMEOUT_SECONDS = 10

_STATUS_ARGS = (
    "status",
    "--porcelain=v1",
    "-z",
    # Untracked files count. A second_agent that drops a new file in the tree has written
    # to the workspace just as surely as one that edits a tracked file, and the default
    # `normal` mode collapses a new directory to one entry, hiding what is inside it.
    "--untracked-files=all",
)


def snapshot(project_root) -> dict:
    """The working tree as git sees it: `{available, entries, reason}`.

    `available: False` is a real answer, not an error — a project outside git is simply
    beyond this guard's reach, and saying so is what stops a later "no changes detected"
    from being read as proof.
    """
    root = Path(project_root) if project_root else None
    if root is None or not root.exists():
        return {"available": False, "entries": frozenset(), "reason": "no project root"}

    try:
        proc = subprocess.run(
            ["git", *_STATUS_ARGS],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SNAPSHOT_TIMEOUT_SECONDS,
            **osutil.hidden_run_kwargs(),
        )
    except FileNotFoundError:
        return {"available": False, "entries": frozenset(), "reason": "git not installed"}
    except subprocess.TimeoutExpired:
        return {"available": False, "entries": frozenset(), "reason": "git status timed out"}
    except OSError as exc:
        return {"available": False, "entries": frozenset(), "reason": f"git failed: {exc}"}

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-200:]
        return {
            "available": False,
            "entries": frozenset(),
            "reason": f"not a git repository ({tail})" if tail else "not a git repository",
        }

    # -z separates records with NUL and never quotes a path, so a filename with a space,
    # a quote or a newline in it survives the round trip. Renames emit two records; both
    # are kept, which is what we want — a rename is a write on both sides.
    entries = frozenset(part for part in proc.stdout.split("\0") if part.strip())
    return {"available": True, "entries": entries, "reason": None}


def diff(before: dict, after: dict) -> dict | None:
    """What changed between two snapshots, or None when nothing did.

    Returns None when the guard could not run at all, which the caller must report
    separately — see `verdict`. Reporting "nothing changed" from a guard that never
    looked is the one failure this module must not produce.
    """
    if not (before or {}).get("available") or not (after or {}).get("available"):
        return None

    added = (after["entries"] or frozenset()) - (before["entries"] or frozenset())
    removed = (before["entries"] or frozenset()) - (after["entries"] or frozenset())
    if not added and not removed:
        return None
    return {
        "appeared": sorted(added),
        # A status line that DISAPPEARED means a file the tree had reported as modified
        # now matches HEAD again: something reverted it. That is still a write.
        "cleared": sorted(removed),
    }


def verdict(before: dict, after: dict) -> dict:
    """Guard result in the shape the adapter puts on `meta`.

    Always includes `checked`. A caller reading only `mutated` would treat an unavailable
    guard and a clean tree identically, and those are opposite facts.
    """
    available = bool((before or {}).get("available") and (after or {}).get("available"))
    if not available:
        reason = (after or {}).get("reason") or (before or {}).get("reason") or "unknown"
        return {"checked": False, "mutated": False, "reason": reason}

    changed = diff(before, after)
    if not changed:
        return {"checked": True, "mutated": False, "reason": None}
    return {
        "checked": True,
        "mutated": True,
        "reason": "the second_agent changed the working tree",
        **changed,
    }
