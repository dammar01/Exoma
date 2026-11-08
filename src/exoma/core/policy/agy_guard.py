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

`git status --porcelain` identifies changed paths. Non-secret dirty and untracked files
are fingerprinted too, so another edit with the same status entry is visible. Hashing
is bounded by the snapshot budget; incomplete snapshots never claim a clean tree.
Tracked gitlinks use parent-index metadata rather than recursive content hashing.

Those same ignore rules hide the files that matter most: a `.gitignore`d `.env` never
appears in `git status`. So the snapshot also records the size and mtime of every file the
canonical secret patterns match (`secret_patterns.find_secret_files`), ignored or not, and a
change there is reported as `secrets_changed`. Contents are never read or hashed.

What it still does not see, stated here rather than in a footnote, because a guard trusted
past its range is worse than no guard:

  * an ignored file that matches no secret pattern — build output, caches
  * a secret whose name matches no pattern
  * any project that is not a git repository, where it reports itself unavailable
  * a file written and restored within the same call, mtime included
  * further changes inside an already-dirty submodule with unchanged parent status/index
"""

import hashlib
import os
import stat
import subprocess
import time
from pathlib import Path

from exoma.core.policy import secret_patterns
from exoma.utils import git, osutil

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
    deadline = time.monotonic() + _SNAPSHOT_TIMEOUT_SECONDS
    root = Path(project_root).resolve() if project_root else None
    if root is None or not root.exists():
        return {"available": False, "entries": frozenset(), "reason": "no project root"}

    repo_root = git.toplevel(root)
    if repo_root is None:
        return {"available": False, "entries": frozenset(), "reason": "cannot resolve git root"}
    try:
        proc = subprocess.run(
            ["git", *_STATUS_ARGS, "--", "."],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SNAPSHOT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
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
    errors: list[str] = []
    found = secret_patterns.find_secret_files(root)
    secrets = _secret_stats(root, found["files"], errors)
    hashes = {}
    if found["truncated"]:
        errors.append("secret metadata scan incomplete")
    try:
        for relative in _status_paths(proc.stdout):
            path = repo_root / relative
            local = path.relative_to(root).as_posix()
            if secret_patterns.is_secret_path(local):
                continue
            if not path.parent.resolve().is_relative_to(root):
                raise OSError("dirty path leaves project root")
            hashes[local] = _content_hash(path, deadline, repo_root)
    except (OSError, ValueError):
        # Do not include exception text: an OS error can contain sensitive paths.
        errors.append("content snapshot incomplete")
    return {
        "available": not errors, "entries": entries, "secrets": secrets, "hashes": hashes,
        "reason": "; ".join(errors) if errors else None,
    }


def _status_paths(output: str):
    """Porcelain v1 -z puts the destination first and rename source in a second record."""
    records = iter(output.split("\0"))
    for record in records:
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise ValueError("invalid git status record")
        yield record[3:]
        if "R" in record[:2] or "C" in record[:2]:
            source = next(records, None)
            if not source:
                raise ValueError("missing rename source")
            yield source


def _gitlink_hash(path: Path, repo_root: Path, deadline: float) -> str:
    """Recognize only an exact, merged gitlink in the parent index; never open its tree."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OSError("snapshot budget exhausted")
    relative = path.relative_to(repo_root).as_posix()
    try:
        result = subprocess.run(
            ["git", "--literal-pathspecs", "ls-files", "--stage", "-z", "--", relative],
            cwd=str(repo_root), capture_output=True, timeout=remaining,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, **osutil.hidden_run_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("gitlink metadata unavailable") from exc
    if result.returncode != 0:
        raise OSError("gitlink metadata unavailable")
    records = result.stdout.split(b"\0")
    if len(records) != 2 or records[-1] != b"":
        raise OSError("unproven dirty directory")
    metadata, separator, indexed_path = records[0].partition(b"\t")
    fields = metadata.split()
    if (not separator or indexed_path != os.fsencode(relative) or len(fields) != 3
            or fields[0] != b"160000" or fields[2] != b"0"):
        raise OSError("unproven dirty directory")
    object_id = fields[1]
    if len(object_id) not in (40, 64) or any(byte not in b"0123456789abcdef" for byte in object_id):
        raise OSError("invalid gitlink metadata")
    if time.monotonic() >= deadline:
        raise OSError("snapshot budget exhausted")
    return "gitlink:" + object_id.decode("ascii")


def _content_hash(path: Path, deadline: float, repo_root: Path | None = None) -> str:
    if time.monotonic() >= deadline:
        raise OSError("snapshot budget exhausted")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(info.st_mode):
        # A symlink's target is metadata; never follow it into a secret or outside root.
        return "link:" + hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
    if stat.S_ISDIR(info.st_mode) and repo_root is not None:
        return _gitlink_hash(path, repo_root, deadline)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("unsupported dirty file type")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OSError("file replaced during snapshot")
        while True:
            if time.monotonic() >= deadline:
                raise OSError("snapshot budget exhausted")
            chunk = stream.read(128 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _secret_stats(root: Path, files=None, errors=None) -> dict:
    """`{relative_path: (size, mtime_ns)}` for every secret-pattern file under `root`."""
    stats: dict = {}
    for relative in files if files is not None else secret_patterns.find_secret_files(root)["files"]:
        try:
            st = (root / relative).stat()
        except OSError:
            if errors is not None:
                errors.append("secret metadata snapshot incomplete")
            continue
        stats[relative] = (st.st_size, st.st_mtime_ns)
    return stats


def _secrets_changed(before: dict, after: dict) -> list[str]:
    """Secret-pattern paths that appeared, disappeared, or changed size or mtime."""
    old = (before or {}).get("secrets") or {}
    new = (after or {}).get("secrets") or {}
    return sorted(path for path in set(old) | set(new) if old.get(path) != new.get(path))


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
    secrets = _secrets_changed(before, after)
    old_hashes, new_hashes = before.get("hashes", {}), after.get("hashes", {})
    content_changed = sorted(
        path for path in old_hashes.keys() | new_hashes.keys()
        if old_hashes.get(path) != new_hashes.get(path)
    )
    if not added and not removed and not secrets and not content_changed:
        return None
    changed = {
        "appeared": sorted(added),
        # A status line that DISAPPEARED means a file the tree had reported as modified
        # now matches HEAD again: something reverted it. That is still a write.
        "cleared": sorted(removed),
    }
    if secrets:
        changed["secrets_changed"] = secrets
    if content_changed:
        changed["content_changed"] = content_changed
    return changed


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
        "reason": "the working tree changed during the delegated call",
        **changed,
    }
