"""Retrieval checks the working sources before exposing reviewed knowledge."""

import copy
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from exoma.core.knowledge import retrieve, store, verify
from exoma.core.provider.result_prep import knowledge_fingerprint
from exoma.utils import git, osutil
from tests.checks.support import assert_true


def _git(root, *args):
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        **osutil.hidden_run_kwargs(),
    )
    assert_true(result.returncode == 0, f"git fixture {args}: {result.stderr}")
    return result.stdout.strip()


def _test_knowledge_retrieval() -> None:
    with tempfile.TemporaryDirectory(prefix="knowledge-retrieve-") as directory:
        root = Path(directory)
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.email", "test@example.invalid")
        _git(root, "config", "user.name", "Exoma test")
        _git(root, "config", "core.autocrlf", "false")
        original = "def authenticate():\n    return 'a'\n"
        (root / "auth.py").write_text(original, encoding="utf-8")
        (root / "payments.py").write_text("def pay():\n    return 1\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "fixture")
        baseline = git.head_commit(root)
        doc = {
            "schema_version": 1, "id": "auth", "title": "Authentication payments",
            "summary": "obsolete summary must not return through filtered retrieval",
            "production": {"ref": "main", "verified_commit": baseline},
            "anchors": {"paths": ["auth.py", "payments.py"]},
            "claims": [], "applicability": {"excluded_branches": []},
        }
        for claim_id, path, statement in (
            ("c1", "auth.py", "Authentication returns method a"),
            ("c2", "payments.py", "Authentication payments return one"),
        ):
            doc["claims"].append({
                "id": claim_id, "type": "behavior", "statement": statement,
                "sources": [{"type": "code", "path": path, "lines": {"start": 2, "end": 2},
                             "anchor_hash": verify.anchor_for(root, path, 2),
                             "blob_oid": git.blob_oid(root, path)}],
            })
        assert_true(store.write(root, doc)["ok"], "fixture knowledge writes")

        def load():
            return retrieve.load_relevant(root, "authentication payments", production_ref="main")

        first = load()
        assert_true(len(first) == 1 and first[0]["applicability"] == retrieve.APPLICABLE,
                    f"clean production knowledge applies: {first}")
        assert_true(all(c["freshness"] == verify.FRESH_COMMIT for c in first[0]["claims"]),
                    "clean sources use the commit shortcut")
        first_identity = knowledge_fingerprint(first)

        (root / "auth.py").write_text("def authenticate():\n    return 'b'\n", encoding="utf-8")
        dirty = load()
        assert_true(git.head_commit(root) == baseline, "HEAD did not change")
        assert_true([c["id"] for c in dirty[0]["claims"]] == ["c2"],
                    f"a dirty stale claim is removed without removing the fresh one: {dirty}")
        assert_true(dirty[0]["applicability"] == retrieve.NEEDS_BRANCH_VERIFICATION,
                    "dirty production sources still need branch verification")
        assert_true("method a" not in str(dirty) and "obsolete summary" not in str(dirty),
                    "stale and original-summary text cannot leak back into the sidecar")
        assert_true(knowledge_fingerprint(dirty) != first_identity, "source changes invalidate reuse identity")
        assert_true(verify.verify_doc(root, doc)["claims"][0]["status"] == verify.NEEDS_VERIFICATION,
                    "verify and retrieval agree despite an unchanged HEAD/blob")

        (root / "auth.py").unlink()
        assert_true(verify.verify_doc(root, doc)["claims"][0]["status"] == verify.SOURCE_MISSING,
                    "source existence is checked before matching HEAD")
        (root / "auth.py").write_text("# moved\n" + original, encoding="utf-8")
        moved = load()[0]["claims"][0]
        assert_true(moved["freshness"] == verify.FRESH_ANCHOR and moved["sources"] == ["auth.py:3"],
                    f"relocated source uses the current anchor line: {moved}")

        _git(root, "add", "auth.py")
        (root / "auth.py").write_text(original, encoding="utf-8")
        assert_true("auth.py" in git.changed_paths(root), "an unstaged revert cannot hide a staged edit")
        assert_true(load()[0]["applicability"] == retrieve.NEEDS_BRANCH_VERIFICATION,
                    "staged changes are also considered")
        _git(root, "reset", "--hard", "HEAD")

        weird = root / " leading space file .py"
        weird.write_text("untracked", encoding="utf-8")
        assert_true(weird.name in git.changed_paths(root), "NUL path parsing preserves leading spaces")
        weird.unlink()

        _git(root, "checkout", "--detach", baseline)
        assert_true(load()[0]["applicability"] == retrieve.APPLICABLE,
                    "detached at the production commit with a clean working tree applies")
        (root / "auth.py").write_text("def authenticate():\n    return 'b'\n", encoding="utf-8")
        _git(root, "add", "auth.py")
        _git(root, "commit", "-m", "detached change")
        assert_true(load()[0]["applicability"] == retrieve.NEEDS_BRANCH_VERIFICATION,
                    "detached at a divergent commit is not assumed to be production")

        excluded = copy.deepcopy(doc)
        excluded["applicability"]["excluded_branches"] = [{"pattern": "feature/auth", "reason": "rewrite"}]
        assert_true(retrieve.applicability(root, excluded, branch="feature/auth", production_ref="main")["status"]
                    == retrieve.EXCLUDED, "explicit exclusions retain precedence")
        with patch.object(git, "_git", return_value=None):
            assert_true(retrieve.applicability(root, doc, branch=None, production_ref="main")["status"]
                        == retrieve.NEEDS_BRANCH_VERIFICATION, "unknown Git state is never applicable")

        # Automatic source verification must not widen the provider's read footprint.
        source = copy.deepcopy(doc["claims"][0]["sources"][0])
        with patch.object(Path, "open", side_effect=AssertionError("unexpected source read")):
            for unsafe in ("../outside.py", "C:\\outside.py", ".env", ".env.local"):
                result = verify.verify_source(root, {**source, "path": unsafe}, head=baseline,
                                              verified_commit=baseline)
                assert_true(result["status"] == verify.NEEDS_VERIFICATION,
                            f"unsafe source is rejected without reading it: {unsafe}")

        # Every source can go stale, including the last surviving claim.
        (root / "payments.py").unlink()
        assert_true(load() == [], "documents without any fresh claims are omitted")

        nested = root / "nested-project"
        nested.mkdir()
        body = "def f():\n    return 1\n"
        (root / "a.py").write_text(body, encoding="utf-8")
        (nested / "a.py").write_text(body, encoding="utf-8")
        _git(root, "add", "a.py", "nested-project/a.py")
        _git(root, "commit", "-m", "same-named source fixture")
        nested_doc = copy.deepcopy(doc)
        nested_doc["production"]["verified_commit"] = git.head_commit(nested)
        nested_doc["claims"] = [{
            "id": "c1", "type": "behavior", "statement": "f returns one",
            "sources": [{"type": "code", "path": "a.py", "lines": {"start": 2, "end": 2},
                         "anchor_hash": verify.anchor_for(nested, "a.py", 2),
                         "blob_oid": git.blob_oid(nested, "a.py")}],
        }]
        (nested / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        _git(root, "add", "nested-project/a.py")
        _git(root, "commit", "-m", "only nested source changes")
        assert_true(git.blob_oid(nested, "a.py") != git.blob_oid(root, "a.py"),
                    "blob lookups are relative to the project, not the repository root")
        assert_true(verify.verify_doc(nested, nested_doc)["claims"][0]["status"] == verify.NEEDS_VERIFICATION,
                    "an unchanged same-named root file cannot hide a committed subproject change")

    _check_git_output_decoding()
    _check_non_ascii_source()
    _check_index_flags()
    _check_symlink_sources()
    _check_case_only_spelling()


def _check_case_only_spelling() -> None:
    """A citation whose case differs from the file on disk is the same file, not an alias.

    Reached only on a case-insensitive file system, where resolve() returns the on-disk
    case. The shortcut stays available, and Git state is read by the on-disk spelling so
    an edit cannot hide behind the cited one.
    """
    directory, root = _repo("knowledge-case-")
    with directory:
        (root / "Case.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        if not (root / "case.py").exists() or (root / "case.py").resolve().name != "Case.py":
            return  # case-sensitive: `case.py` is a different, missing file
        _git(root, "add", ".")
        _git(root, "commit", "-m", "case fixture")
        head = git.head_commit(root)
        source = {**_source(root, "Case.py"), "path": "case.py"}
        clean = verify.verify_source(root, source, head=head, verified_commit=head)
        assert_true(clean["status"] == verify.FRESH_COMMIT,
                    f"a case-only spelling keeps the commit shortcut: {clean}")
        blob = verify.verify_source(root, source, head=head, verified_commit="0" * 40)
        assert_true(blob["status"] == verify.FRESH_BLOB,
                    f"the blob is looked up by the on-disk spelling: {blob}")
        (root / "Case.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        assert_true("Case.py" in (git.changed_paths(root) or set()), "Git reports the on-disk spelling")
        dirty = verify.verify_source(root, source, head=head, verified_commit=head)
        assert_true(dirty["status"] == verify.NEEDS_VERIFICATION,
                    f"an edit cannot hide behind the cited spelling: {dirty}")
        _git(root, "checkout", "--", "Case.py")

        # The index keeps `Case.py` after an on-disk rename to `case.py` (core.ignoreCase):
        # Git and resolve() now spell the same file differently, and neither may win.
        (root / "Case.py").rename(root / "tmp-case.py")
        (root / "tmp-case.py").rename(root / "case.py")
        assert_true((root / "Case.py").resolve().name == "case.py", "fixture: disk spelling changed")
        indexed = _source(root, "case.py")
        cited = {**indexed, "path": "Case.py"}
        renamed = verify.verify_source(root, cited, head=head, verified_commit=head)
        assert_true(renamed["status"] in (verify.FRESH_COMMIT, verify.FRESH_ANCHOR),
                    f"a rename that changed nothing keeps its claim: {renamed}")
        (root / "case.py").write_text("def f():\n    return 3\n", encoding="utf-8")
        reported = git.changed_paths(root) or set()
        assert_true(any(p.lower() == "case.py" for p in reported), f"fixture: Git reports the edit: {reported}")
        for spelling in ("Case.py", "case.py"):
            result = verify.verify_source(root, {**cited, "path": spelling}, head=head, verified_commit=head)
            assert_true(result["status"] == verify.NEEDS_VERIFICATION,
                        f"index spelling {sorted(reported)} vs disk spelling cannot hide an edit "
                        f"(cited {spelling}): {result}")


def _repo(prefix: str):
    directory = tempfile.TemporaryDirectory(prefix=prefix)
    root = Path(directory.name)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Exoma test")
    _git(root, "config", "core.autocrlf", "false")
    return directory, root


def _source(root: Path, path: str, line: int = 2) -> dict:
    return {"type": "code", "path": path, "lines": {"start": line, "end": line},
            "anchor_hash": verify.anchor_for(root, path, line), "blob_oid": git.blob_oid(root, path)}


def _check_git_output_decoding() -> None:
    """Names come back as the file system spells them, whatever the process locale."""
    name = "café ü.py"
    flagged = "flagged é.py"

    def fake_run(args, **kwargs):
        assert_true("text" not in kwargs and "encoding" not in kwargs and "universal_newlines" not in kwargs,
                    f"git output is captured as bytes, not decoded by the locale: {kwargs}")
        if args[1] == "rev-parse":
            out = b"0" * 40 + b"\n"
        elif args[1:3] == ["ls-files", "-v"]:
            out = b"h " + os.fsencode(flagged) + b"\0H normal.py\0"
        elif args[1] == "ls-files":
            out = os.fsencode(" lead\rcr.py") + b"\0"
        else:
            out = os.fsencode(name) + b"\0"
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr=b"")

    with patch.object(git.subprocess, "run", side_effect=fake_run):
        changed = git.changed_paths(Path("."))
        assert_true(changed == {name, " lead\rcr.py", flagged},
                    f"NUL records decode per name with carriage returns intact: {changed}")
        assert_true(git.current_branch(Path(".")) == "0" * 40, "non-path output keeps its str contract")
    if os.name == "nt":
        # Windows decodes names as strict UTF-8: an undecodable name is a failure, not a raise.
        bad = subprocess.CompletedProcess([], 0, stdout=b"\xff\xfe.py\0", stderr=b"")
        with patch.object(git.subprocess, "run", return_value=bad):
            assert_true(git.changed_paths(Path(".")) is None, "undecodable output is None, never a traceback")


def _check_non_ascii_source() -> None:
    directory, root = _repo("knowledge-ünïcode-")
    with directory:
        name = "café.py"
        try:
            (root / name).write_text("def f():\n    return 1\n", encoding="utf-8")
        except (OSError, UnicodeError):
            return  # a file system that cannot hold the name has nothing to misreport
        _git(root, "add", ".")
        _git(root, "commit", "-m", "non-ascii fixture")
        head = git.head_commit(root)
        source = _source(root, name)
        clean = verify.verify_source(root, source, head=head, verified_commit=head)
        assert_true(clean["status"] == verify.FRESH_COMMIT, f"clean non-ASCII source keeps the shortcut: {clean}")
        (root / name).write_text("def f():\n    return 2\n", encoding="utf-8")
        assert_true(name in (git.changed_paths(root) or set()), "a dirty non-ASCII name is reported by its real name")
        dirty = verify.verify_source(root, source, head=head, verified_commit=head)
        assert_true(dirty["status"] == verify.NEEDS_VERIFICATION,
                    f"a dirty non-ASCII source cannot pass through the commit shortcut: {dirty}")
        _git(root, "add", name)
        (root / "naïve new.py").write_text("x = 1\n", encoding="utf-8")
        changed = git.changed_paths(root) or set()
        assert_true({name, "naïve new.py"} <= changed, f"staged and untracked non-ASCII names: {changed}")


def _check_index_flags() -> None:
    directory, root = _repo("knowledge-flags-")
    with directory:
        for name in ("assumed.py", "skipped.py", "quiet.py"):
            (root / name).write_text("def f():\n    return 1\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "flag fixture")
        head = git.head_commit(root)
        sources = {name: _source(root, name) for name in ("assumed.py", "skipped.py", "quiet.py")}
        _git(root, "update-index", "--assume-unchanged", "assumed.py", "quiet.py")
        _git(root, "update-index", "--skip-worktree", "skipped.py")
        for name in ("assumed.py", "skipped.py"):
            (root / name).write_text("def f():\n    return 2\n", encoding="utf-8")
        index = root / ".git" / "index"
        before = index.read_bytes()
        changed = git.changed_paths(root)
        assert_true(changed is not None and {"assumed.py", "skipped.py", "quiet.py"} <= changed,
                    f"flagged entries count as changed: {changed}")
        for name in ("assumed.py", "skipped.py"):
            result = verify.verify_source(root, sources[name], head=head, verified_commit=head)
            assert_true(result["status"] == verify.NEEDS_VERIFICATION,
                        f"an edited flagged source loses its shortcut: {name} {result}")
        quiet = verify.verify_source(root, sources["quiet.py"], head=head, verified_commit=head)
        assert_true(quiet["status"] == verify.FRESH_ANCHOR,
                    f"an unchanged flagged source keeps its claim through the anchor: {quiet}")
        assert_true(index.read_bytes() == before, "the real index is never rewritten")
        flags = _git(root, "ls-files", "-v")
        assert_true("h assumed.py" in flags and "S skipped.py" in flags, f"flags stay set: {flags}")
        with patch.object(git, "_flagged_paths", return_value=None):
            assert_true(git.changed_paths(root) is None, "unlisted flags are a failed comparison, not a clean tree")


def _check_symlink_sources() -> None:
    """A link is read through to its target, so the target decides freshness and ignore."""
    directory, root = _repo("knowledge-links-")
    with directory:
        _git(root, "config", "core.symlinks", "true")
        (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        (root / "ignored").mkdir()
        (root / "ignored" / "conf.py").write_text("def g():\n    return 1\n", encoding="utf-8")
        (root / "target.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (root / "real").mkdir()
        (root / "real" / "m.py").write_text("def h():\n    return 1\n", encoding="utf-8")
        try:
            os.symlink("target.py", root / "alias.py")
            os.symlink(os.path.join("ignored", "conf.py"), root / "link.py")
            os.symlink("real", root / "linkdir", target_is_directory=True)
            linked = True
        except (OSError, NotImplementedError):
            linked = False
        if linked:
            _check_real_links(root)
        _check_mocked_links(root)


def _check_real_links(root: Path) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-m", "link fixture")
    head = git.head_commit(root)
    alias = _source(root, "alias.py")
    nested = _source(root, "linkdir/m.py")
    for source in (alias, nested):
        clean = verify.verify_source(root, source, head=head, verified_commit=head)
        assert_true(clean["status"] == verify.FRESH_ANCHOR,
                    f"a clean link target keeps its claim, without the alias shortcuts: {clean}")
    (root / "target.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    changed = git.changed_paths(root) or set()
    assert_true("target.py" in changed and "alias.py" not in changed, f"Git reports the target, not the link: {changed}")
    stale = verify.verify_source(root, alias, head=head, verified_commit=head)
    assert_true(stale["status"] == verify.NEEDS_VERIFICATION,
                f"an edited target retires the claim despite an unchanged alias/HEAD/blob: {stale}")
    doc = {"anchors": {"paths": ["alias.py"]}, "applicability": {"excluded_branches": []}}
    applies = retrieve.applicability(root, doc, branch="main", production_ref="main",
                                     comparison={"paths": changed})
    assert_true(applies["status"] == retrieve.NEEDS_BRANCH_VERIFICATION,
                f"a changed target makes the alias's document need verification: {applies}")
    (root / "target.py").write_text("# moved\ndef f():\n    return 1\n", encoding="utf-8")
    moved = verify.verify_source(root, alias, head=head, verified_commit=head)
    assert_true(moved["status"] == verify.FRESH_ANCHOR and moved.get("line") == 3,
                f"a relocated target anchor reports its current line: {moved}")
    _git(root, "checkout", "--", "target.py")
    hidden = {**alias, "path": "link.py"}
    with patch.object(Path, "open", side_effect=AssertionError("ignored target was read")):
        result = verify.verify_source(root, hidden, head=head, verified_commit=head)
    assert_true(result["status"] == verify.NEEDS_VERIFICATION,
                f"a tracked link to an ignored file is refused without reading it: {result}")


def _check_mocked_links(root: Path) -> None:
    """The same decisions where the platform cannot create links: resolution is faked."""
    for name in ("m_alias.py", "m_link.py"):
        (root / name).write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(root, "add", "m_alias.py", "m_link.py", "target.py", ".gitignore")
    _git(root, "commit", "-m", "mocked link fixture")
    head = git.head_commit(root)
    alias = _source(root, "m_alias.py")
    hidden = _source(root, "m_link.py")
    original = Path.resolve

    def fake_resolve(self, strict=False):
        resolved = original(self, strict)
        if resolved.name == "m_alias.py":
            return resolved.with_name("target.py")
        if resolved.name == "m_link.py":
            return resolved.parent / "ignored" / "conf.py"
        return resolved

    with patch.object(Path, "resolve", fake_resolve):
        result = verify.verify_source(root, alias, head=head, verified_commit=head)
        assert_true(result["status"] not in (verify.FRESH_COMMIT, verify.FRESH_BLOB),
                    f"an alias never takes the commit or blob shortcut: {result}")
        doc = {"anchors": {"paths": ["m_alias.py"]}, "applicability": {"excluded_branches": []}}
        applies = retrieve.applicability(root, doc, branch="main", production_ref="main",
                                         comparison={"paths": {"target.py"}})
        assert_true(applies["status"] == retrieve.NEEDS_BRANCH_VERIFICATION,
                    f"applicability compares the alias's target: {applies}")
        with patch.object(Path, "open", side_effect=AssertionError("ignored target was read")):
            refused = verify.verify_source(root, hidden, head=head, verified_commit=head)
        assert_true(refused["status"] == verify.NEEDS_VERIFICATION,
                    f"an ignored resolved target is refused before any read: {refused}")
