"""The content of the project's working tree, as one Git tree id.

One part of the identity /.verify reuses a test run under (verify_tests.reuse_identity):
the files Git would see outside `.workflow/`, tracked or untracked-but-not-ignored, with
their content as Git stores it and their modes. That content is taken after the
repository's own conversions (`text`/`eol` attributes, `core.autocrlf`, clean filters), so
an edit those conversions undo, such as LF to CRLF in a file Git normalizes, leaves the id
unchanged: a documented limit. Unlike the e2e fingerprint (core/evidence/e2e/fingerprint.py) HEAD
is not part of it, so committing the tested change without touching a file keeps the
reuse, while any edit, new file or deletion of such a file ends it. Ignored files, the
environment and `.workflow/config.json` are not seen here; reuse_identity adds them.

Computed with a throwaway index: a copy of `.git/index` with its mtime (so Git's stat
cache spares re-hashing files that did not change, and its racy-entry check still
holds), `git add -A` of everything outside `.workflow/` into it, then `git write-tree`. The repository's own index, HEAD and objects the user sees
are untouched; `write-tree` may add loose tree/blob objects, which `git gc` prunes.

`None` when there is nothing to compare: not a Git work tree, Git missing, or Git failing.
A caller then treats the tree as unknown and reuses nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from exoma.utils.osutil import hidden_run_kwargs

GIT_TIMEOUT_S = 60


def _git(root: Path, args: list[str], env: dict | None = None) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            env=env,
            **hidden_run_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def tree_fingerprint(project_root) -> str | None:
    """The Git tree id of the working tree's content outside `.workflow/`, or None."""
    root = Path(project_root)
    if _git(root, ["rev-parse", "--is-inside-work-tree"]) != "true":
        return None
    index = _git(root, ["rev-parse", "--git-path", "index"])
    if not index:
        return None
    index_path = Path(index) if Path(index).is_absolute() else root / index
    with tempfile.TemporaryDirectory(prefix="exoma-tree-") as tmp:
        scratch = Path(tmp) / "index"
        try:
            if index_path.is_file():
                # copy2, not copyfile: the copy must keep the index's mtime. Git trusts a
                # cached stat only for entries older than the index file ("racy git"); a
                # copy stamped "now" made a same-size edit written just after the index
                # look clean, and verify reused a pass for code that had changed.
                shutil.copy2(index_path, scratch)
        except OSError:
            pass  # an empty index still gives the right answer, only slower
        env = dict(os.environ, GIT_INDEX_FILE=str(scratch))
        # No `:(exclude).workflow` pathspec here: when `.workflow/` is ignored (the usual
        # case) naming it makes `git add` exit 1 ("paths are ignored"). The rm below takes
        # it out whether it was ignored, untracked or tracked.
        if _git(root, ["add", "-A", "--", "."], env) is None:
            return None
        # `.workflow/` is the runtime's own state, never part of what was tested.
        _git(root, ["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", ".workflow"], env)
        return _git(root, ["write-tree"], env) or None


def changed_paths(project_root, old: str | None, new: str | None) -> list[dict] | None:
    """What changed between two ids `tree_fingerprint` returned: one `{"status", "path"}`
    per file (status A, M, D or T, the path relative to the project root, forward slashes).

    Comparing two snapshots, not the working tree with HEAD, isolates the edits made
    between them from changes the tree already carried. None when either id is missing or
    Git cannot compare them (no Git, or `git gc` pruned an unreferenced tree): unknown,
    never "nothing changed"."""
    if not old or not new:
        return None
    if old == new:
        return []
    # -z: names unquoted, NUL-separated (`<status>\0<path>\0` per file).
    out = _git(Path(project_root), ["diff-tree", "-r", "-z", "--no-renames", "--relative",
                                    "--name-status", old, new])
    if out is None:
        return None
    fields = out.split("\0")
    changed = []
    for status, path in zip(fields[0::2], fields[1::2]):
        if status.strip() and path:
            changed.append({"status": status.strip()[:1], "path": path})
    return changed
