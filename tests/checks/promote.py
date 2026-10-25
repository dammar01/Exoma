"""Promoted knowledge stays inside the project, and changing it changes evidence identity.

The behaviour checks for promote (determinism, the freshness ladder, reconciliation)
live in tools/e2e/e2e_promote.py. These two are boundaries the e2e harness never
exercised: where the store is allowed to write, and whether knowledge it serves is part
of what makes two delegated calls "the same".
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from tests.checks.support import assert_true


def _configure(root: Path, knowledge_dir: str) -> None:
    from exoma.core.workspace.workspace_paths import workflow_paths

    config = workflow_paths(root)["config"]
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps({"policies": {"knowledge_dir": knowledge_dir}}), encoding="utf-8"
    )


def _doc(doc_id: str = "auth") -> dict:
    return {
        "schema_version": 1,
        "id": doc_id,
        "title": "Authentication",
        "summary": "how requests authenticate",
        "production": {"ref": "main", "verified_commit": "abcdef1234567"},
        "anchors": {"paths": ["a.py"]},
        "claims": [
            {
                "id": "c1",
                "type": "behavior",
                "statement": "f returns 1",
                "sources": [
                    {
                        "type": "code",
                        "path": "a.py",
                        "lines": {"start": 2, "end": 2},
                        "anchor_hash": "0" * 16,
                    }
                ],
            }
        ],
        "applicability": {"excluded_branches": []},
    }


def _knowledge_dir_is_contained() -> None:
    from exoma.core.knowledge import store

    root = Path(tempfile.mkdtemp(prefix="knowledge-contain-"))
    outside = Path(tempfile.mkdtemp(prefix="knowledge-outside-"))
    try:
        (root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

        _configure(root, "docs/kb")
        assert_true(
            store.knowledge_dir(root) == (root / "docs" / "kb").resolve(),
            f"a relative directory resolves inside the root: {store.knowledge_dir(root)}",
        )

        for escape in ("../escaped", str(outside), "docs/../../escaped"):
            _configure(root, escape)
            refused = False
            try:
                store.knowledge_dir(root)
            except store.KnowledgePathError:
                refused = True
            assert_true(refused, f"knowledge_dir {escape!r} is refused, not followed")
            written = store.write(root, _doc())
            assert_true(
                not written["ok"] and written["error_type"] == "knowledge_path_escape",
                f"write refuses before touching disk for {escape!r}: {written}",
            )
            assert_true(
                store.list_docs(root) == [] and store.load(root, "auth")["ok"] is False,
                f"readers see no knowledge rather than another directory's: {escape!r}",
            )
        assert_true(
            not any(outside.iterdir()) and not (root.parent / "escaped").exists(),
            "and nothing was written outside the root",
        )

        _configure(root, "docs/kb")
        traversal = store.load(root, "../../etc/passwd")
        assert_true(
            traversal["ok"] is False and "slug" in traversal["error"],
            f"an id that is not a slug never reaches the filesystem: {traversal}",
        )

        link = root / "linked"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            link = None  # no symlink privilege on this machine; the path checks still ran
        if link is not None:
            _configure(root, "linked")
            refused = False
            try:
                store.knowledge_dir(root)
            except store.KnowledgePathError:
                refused = True
            assert_true(refused, "a symlink out of the repository is refused like `..`")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)


def _knowledge_is_part_of_evidence_identity() -> None:
    from exoma.core.provider.result_prep import _evidence_context, knowledge_fingerprint

    route = {"role": "reasoning", "model": "m"}
    entry = {"id": "auth", "claims": [{"id": "c1", "statement": "f returns 1"}]}
    changed = {"id": "auth", "claims": [{"id": "c1", "statement": "f returns 2"}]}

    without = _evidence_context(route, False, None, ["fact"])
    with_one = _evidence_context(route, False, None, ["fact"], [entry])
    with_changed = _evidence_context(route, False, None, ["fact"], [changed])
    assert_true(
        without != with_one and with_one != with_changed,
        "promoting or correcting knowledge changes the reuse identity of the same task",
    )
    assert_true(
        knowledge_fingerprint(None) is None and knowledge_fingerprint([]) is None,
        "no knowledge has no fingerprint",
    )
    assert_true(
        knowledge_fingerprint([{"b": 1, "a": 2}]) == knowledge_fingerprint([{"a": 2, "b": 1}]),
        "the fingerprint does not depend on key order",
    )
    assert_true(
        "f returns" not in json.dumps(with_one),
        "the context carries a hash, never the knowledge text",
    )


def _test_promote_boundaries() -> None:
    _knowledge_dir_is_contained()
    _knowledge_is_part_of_evidence_identity()
