"""Fact store concurrency and evidence artifact reuse."""

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from exoma import check
from exoma import cli as main
from exoma.adapters.providers.opencode_adapter import OpenCodeAdapter
from exoma.core.provider.executor import Executor
from exoma.core.jobs.job_manager import JobManager
from exoma.core.prompt.prompt_builder import build_prompt
from exoma.core.runtime.state import ensure_workflow_workspace

from tests.checks.support import (
    FakeJobProcess,
    FakeOpenCodeAdapter,
    RecordingOpenCodeAdapter,
    assert_true,
    clean_output,
    extract_session_id,
)

def _test_facts_concurrency() -> None:
    """Concurrent ingest must not lose updates and must leave valid JSONL.

    N threads each ingest one distinct, file:line-anchored fact into the same store. The
    _FactLock read-modify-write serialises them, so all N must survive (an unlocked store
    would let the last writer clobber earlier additions), and every line must still parse.
    """
    import json as _json
    import shutil as _shutil

    from exoma.core.evidence import fact_store
    from exoma.core.workspace.workspace_paths import workflow_paths

    root = Path(tempfile.mkdtemp(prefix="facts-conc-"))
    try:
        wf = workflow_paths(root)["workflow_dir"]
        wf.mkdir(parents=True, exist_ok=True)
        src = root / "src.py"
        src.write_text(
            "\n".join(f"line_{i} = {i}" for i in range(20)) + "\n", encoding="utf-8"
        )

        n = 8
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                fact_store.ingest(
                    root,
                    f"durable_facts:\n- [config] fact number {i} distinct value [src.py:{i + 1}]\n",
                    f"sess-{i}",
                )
            except Exception as exc:  # noqa: BLE001 - surfaced via errors list
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert_true(not errors, f"concurrent ingest raised: {errors}")
        facts = fact_store._load_facts(root)
        assert_true(
            len(facts) == n,
            f"concurrent ingest lost updates: expected {n} facts, got {len(facts)}",
        )
        for line in fact_store._facts_path(root).read_text(encoding="utf-8").splitlines():
            if line.strip():
                _json.loads(line)  # raises if the atomic rewrite ever left a torn line
    finally:
        _shutil.rmtree(root, ignore_errors=True)


def _test_anchor_relocation() -> None:
    """A fact whose anchor line MOVED survives; one whose content changed still does not.

    Inserting a block at the top of a file used to invalidate every fact anchored below it:
    lookup was by line NUMBER, so line 40 now held different text and hashed differently.
    Nothing about those claims had changed. Relocation closes that, and the assertions here
    pin the boundary — moved survives, edited drops, ambiguous drops.
    """
    import shutil as _shutil

    from exoma.core.evidence import fact_store
    from exoma.core.workspace.workspace_paths import workflow_paths

    root = Path(tempfile.mkdtemp(prefix="facts-reloc-"))
    try:
        workflow_paths(root)["workflow_dir"].mkdir(parents=True, exist_ok=True)
        src = root / "src.py"
        # Lines 2 and 3 are deliberately identical: their shared hash is what an ambiguous
        # anchor looks like, and no relocation may pick between them.
        src.write_text(
            "alpha_anchor = 1\ndup_line = 0\ndup_line = 0\nbeta_anchor = 2\n",
            encoding="utf-8",
        )
        fact_store.ingest(
            root,
            "durable_facts:\n"
            "- [config] alpha setting governs alpha behaviour [src.py:1]\n"
            "- [config] ambiguous setting sits on a repeated line [src.py:3]\n"
            "- [config] beta setting governs beta behaviour [src.py:4]\n",
            "sess-reloc",
        )
        assert_true(
            len(fact_store._load_facts(root)) == 3,
            f"fixture must start with 3 facts: {fact_store._load_facts(root)}",
        )

        # Shift everything down by four lines without touching any anchored text.
        src.write_text(
            "# header\n# header two\n# header three\n# header four\n"
            + src.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        task = "alpha ambiguous beta setting"
        placed = {f["claim"]: f["line"] for f in fact_store.load_relevant(root, task, limit=10)}
        alpha = next((c for c in placed if "alpha" in c), None)
        beta = next((c for c in placed if "beta" in c), None)
        ambiguous = next((c for c in placed if "ambiguous" in c), None)
        assert_true(
            alpha is not None and placed[alpha] == 5,
            f"a fact whose anchor only moved must be served at its new line: {placed}",
        )
        assert_true(
            beta is not None and placed[beta] == 8,
            f"relocation must track each anchor independently: {placed}",
        )
        assert_true(
            ambiguous is None,
            "an anchor matching more than one line identifies nothing — it must stay "
            f"dropped rather than be relocated to a guess: {placed}",
        )

        # prune persists the move: the lock is held and the file is rewritten anyway.
        report = fact_store.prune(root)
        assert_true(
            report["relocated"] == 2 and report["removed"] == 1,
            f"prune must relocate the two movable facts and drop the ambiguous one: {report}",
        )
        stored = {f["claim"]: f["line"] for f in fact_store._load_facts(root)}
        assert_true(
            sorted(stored.values()) == [5, 8],
            f"relocated line numbers must be written back to facts.jsonl: {stored}",
        )

        # Content change is a different event and must still invalidate.
        src.write_text(
            src.read_text(encoding="utf-8").replace("alpha_anchor = 1", "alpha_anchor = 99"),
            encoding="utf-8",
        )
        after = [f["claim"] for f in fact_store.load_relevant(root, task, limit=10)]
        assert_true(
            not any("alpha" in c for c in after),
            f"an anchor whose CONTENT changed must still be dropped: {after}",
        )
    finally:
        _shutil.rmtree(root, ignore_errors=True)


def _test_fact_selection() -> None:
    """Every cited anchor must hold, relevance ignores grammar, and eviction keeps value."""
    import json as _json
    import shutil as _shutil

    from exoma.core.evidence import fact_store
    from exoma.core.workspace.workspace_paths import workflow_paths

    root = Path(tempfile.mkdtemp(prefix="facts-select-"))
    try:
        paths = workflow_paths(root)
        paths["workflow_dir"].mkdir(parents=True, exist_ok=True)
        (root / "core").mkdir()
        (root / "core" / "store.py").write_text(
            "def fact_store_ingest():\n    pass\nLIMIT = 3\nTHRESHOLD = 5\n", encoding="utf-8"
        )

        stats: dict = {}
        added = fact_store.ingest(
            root,
            "durable_facts:\n"
            "- [invariant] `fact_store_ingest` validates anchors [core/store.py:1] [core/provider/registry.py:30]\n"
            "- [config] `LIMIT` and `THRESHOLD` bound retrieval [core/store.py:3] [core/store.py:4]\n"
            "- [config] " + "very long claim " * 40 + "[core/store.py:3]\n"
            "- [config] no anchor at all\n",
            "sess-select",
            stats=stats,
        )
        stored = fact_store._load_facts(root)
        assert_true(
            added == 1 and stats["rejected_anchor"] == 2 and stats["rejected_length"] == 1,
            f"one bad citation refuses the claim, as does a paragraph: {added} {stats}",
        )
        assert_true(
            stored[0]["line"] == 3
            and stored[0]["anchors"] == [
                {"file": "core/store.py", "line": 4,
                 "anchor_hash": fact_store._anchor_hash(root, "core/store.py", 4)}
            ],
            f"the primary anchor stays where old readers look, the rest ride beside it: {stored}",
        )

        read: dict = {}
        served = fact_store.load_relevant(root, "kenapa `LIMIT` dipakai", stats=read)
        assert_true(
            len(served) == 1 and read == {"candidates": 1, "rejected_stale": 0,
                                          "rejected_irrelevant": 0, "injected": 1},
            f"an Indonesian task finds the fact through the identifier it names: {served} {read}",
        )
        refs = fact_store.served_fact_refs(served)
        usage = _json.loads(
            paths["facts_store"].with_name(fact_store.USAGE_FILENAME).read_text(encoding="utf-8")
        )
        assert_true(
            refs == [{"key": fact_store._fact_key(served[0]), "file": "core/store.py", "line": 3}]
            and refs[0]["key"] in usage,
            f"a served fact is named by its usage key and the anchor it was served at: {refs}",
        )
        for noise in ("the and for with that", "yang dan untuk dari", "core tests py"):
            assert_true(
                fact_store.load_relevant(root, noise) == [],
                f"grammar and generic path segments match nothing: {noise!r}",
            )

        (root / "core" / "store.py").write_text(
            "def fact_store_ingest():\n    pass\nLIMIT = 3\nTHRESHOLD = 50\n", encoding="utf-8"
        )
        stale: dict = {}
        assert_true(
            fact_store.load_relevant(root, "`LIMIT` `THRESHOLD`", stats=stale) == []
            and stale["rejected_stale"] == 1,
            f"a fact is only as fresh as its weakest citation: {stale}",
        )
        assert_true(
            fact_store.prune(root)["removed"] == 1,
            "and prune drops it for the same reason",
        )

        score_id, ids = fact_store.relevance("why `fact_store_ingest` fails", "fact_store_ingest runs", "x.py")
        score_word, none = fact_store.relevance("why ingest fails", "ingest runs here", "x.py")
        assert_true(
            ids == 1 and score_id > score_word and none == 0,
            f"a shared identifier outweighs a shared word: {score_id} {score_word}",
        )

        # Per-role limits, and the scalar override still winning when set.
        assert_true(
            fact_store._policy_limit(root, "reasoning") == 4
            and fact_store._policy_limit(root, "exploration") == 3,
            "reasoning gets its own default",
        )
        paths["config"].write_text(
            _json.dumps({"policies": {"fact_relevant_limit": 2,
                                      "fact_relevant_limits": {"exploration": 6}}}),
            encoding="utf-8",
        )
        assert_true(
            fact_store._policy_limit(root, "exploration") == 6
            and fact_store._policy_limit(root, "reasoning") == 2,
            "a per-role value wins, else the user's scalar, else the default",
        )

        # Eviction by value: the used fact outlives two newer, never-served ones.
        (root / "a.py").write_text("one_value = 1\ntwo_value = 2\nthree_value = 3\n", encoding="utf-8")
        paths["config"].write_text(_json.dumps({"policies": {"fact_max_facts": 2}}), encoding="utf-8")
        paths["facts_store"].write_text("", encoding="utf-8")
        fact_store.ingest(root, "durable_facts:\n- [config] `one_value` is one [a.py:1]\n", "s1")
        fact_store.load_relevant(root, "`one_value`")
        fact_store.ingest(root, "durable_facts:\n- [config] `two_value` is two [a.py:2]\n", "s2")
        fact_store.ingest(root, "durable_facts:\n- [config] `three_value` is three [a.py:3]\n", "s3")
        kept = [f["claim"] for f in fact_store._load_facts(root)]
        assert_true(
            any("one_value" in c for c in kept) and any("three_value" in c for c in kept)
            and len(kept) == 2,
            f"a fact that has been served is not evicted for being old: {kept}",
        )
    finally:
        _shutil.rmtree(root, ignore_errors=True)


def _test_evidence_anchor_relocation() -> None:
    """Evidence survives a shifted citation. Freshness demands EVERY anchor hold, so one
    insertion above one cited line used to discard a whole artifact and force a re-run."""
    import shutil as _shutil

    from exoma.core.evidence import evidence_store
    from exoma.core.evidence.runtime_io import write_response_snapshot
    from exoma.core.workspace.workspace_paths import workflow_paths

    root = Path(tempfile.mkdtemp(prefix="evidence-reloc-"))
    try:
        src = root / "src.py"
        src.write_text(
            "\n".join(f"anchor_{i} = {i}" for i in range(1, 21)) + "\n", encoding="utf-8"
        )
        session_id = "evidence-reloc-session"
        content = "FINDING [src.py:3]"
        context = {"model": "provider/model-a", "agent": "plan"}
        write_response_snapshot(root, content, "run-a", session_id)
        evidence_store.record(
            root,
            "analyze",
            "shifted query",
            session_id,
            {"summary": "first"},
            workflow_paths(root, session_id)["logs_dir"] / "run-a" / "output.raw.md",
            content,
            context=context,
        )
        assert_true(
            evidence_store.find_fresh(root, "analyze", "shifted query", context=context)
            is not None,
            "fixture must be reusable before the file is touched",
        )

        src.write_text("# added\n# added two\n" + src.read_text(encoding="utf-8"), encoding="utf-8")
        assert_true(
            evidence_store.find_fresh(root, "analyze", "shifted query", context=context)
            is not None,
            "an artifact whose only change is a shifted anchor line must stay fresh",
        )

        src.write_text(
            src.read_text(encoding="utf-8").replace("anchor_3 = 3", "anchor_3 = 300"),
            encoding="utf-8",
        )
        assert_true(
            evidence_store.find_fresh(root, "analyze", "shifted query", context=context)
            is None,
            "an artifact citing a line whose CONTENT changed must not be reused",
        )
        assert_true(
            evidence_store.find_fresh_with_reason(
                root, "analyze", "shifted query", context=context
            )[1] == "stale"
            and evidence_store.find_fresh_with_reason(
                root, "analyze", "never asked", context=context
            ) == (None, "no_prior"),
            "a miss names its cause: a moved anchor is `stale`, an unseen query `no_prior`",
        )
    finally:
        _shutil.rmtree(root, ignore_errors=True)


_HOLD_EVIDENCE_LOCK = """
import sys, time
from pathlib import Path
from exoma.core.evidence.evidence_store import _EvidenceLock
with _EvidenceLock(Path(sys.argv[1])):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def _test_evidence_lock_file() -> None:
    """`evidence.jsonl.lock` is a persistent one-byte OS lock, not an owner record.

    It holds a single NUL byte (so it reads as empty), survives release, and excludes a
    second process while held. Deleting it under a holder is what the design rules out:
    Windows refuses the delete, and on POSIX a new file would be a second, unlocked lock.
    """
    import subprocess
    import sys

    from exoma.core.evidence.evidence_store import _EvidenceLock
    from exoma.core.workspace.workspace_paths import workflow_paths

    repo = Path(__file__).resolve().parents[2]
    root = Path(tempfile.mkdtemp(prefix="evidence-lock-"))
    try:
        with _EvidenceLock(root):
            pass
        lock = workflow_paths(root)["evidence_store"].with_name("evidence.jsonl.lock")
        assert_true(
            lock.read_bytes() == b"\0",
            f"the lock file is one NUL byte and stays after release: {lock.read_bytes()!r}",
        )

        hold_s = 1.5

        def _acquire_while_held(before_acquire) -> float:
            holder = subprocess.Popen(
                [sys.executable, "-c", _HOLD_EVIDENCE_LOCK, str(root), str(hold_s)],
                cwd=repo,
                env={**os.environ, "PYTHONPATH": str(repo / "src")},
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert_true(holder.stdout.readline().strip() == "held", "the holder process took the lock")
                before_acquire()
                started = time.monotonic()
                with _EvidenceLock(root):
                    return time.monotonic() - started
            finally:
                holder.wait(timeout=30)

        waited = _acquire_while_held(lambda: None)
        assert_true(
            waited >= hold_s * 0.5,
            f"a second process waits for the holder, it does not share the lock; waited {waited:.2f}s",
        )

        refused: list[bool] = []

        def _delete_lock() -> None:
            try:
                lock.unlink()
                refused.append(False)
            except OSError:
                refused.append(True)

        waited = _acquire_while_held(_delete_lock)
        if os.name == "nt":
            assert_true(
                refused == [True] and waited >= hold_s * 0.5,
                f"Windows refuses to delete a held lock file, so exclusion holds; refused={refused}",
            )
        else:
            assert_true(
                refused == [False] and waited < hold_s * 0.5,
                "on POSIX a deleted lock file is replaced by a new, unlocked one: exclusion is "
                f"lost, which is why the file is never cleaned up; waited {waited:.2f}s",
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_evidence_reuse() -> None:
    """Reuse must read immutable, hash-checked artifacts and preserve concurrent rows."""
    import shutil as _shutil

    from exoma.core.evidence import evidence_store
    from exoma.core.evidence.runtime_io import write_response_snapshot
    from exoma.core.workspace.workspace_paths import workflow_paths

    root = Path(tempfile.mkdtemp(prefix="evidence-reuse-"))
    try:
        (root / "src.py").write_text(
            "\n".join(f"anchor_{i} = {i}" for i in range(1, 51)) + "\n",
            encoding="utf-8",
        )
        session_id = "evidence-session"
        first = "FIRST [src.py:1]"
        evidence_context = {"model": "provider/model-a", "agent": "plan"}
        write_response_snapshot(root, first, "run-a", session_id)
        paths = workflow_paths(root, session_id)
        first_artifact = paths["logs_dir"] / "run-a" / "output.raw.md"
        evidence_store.record(
            root,
            "analyze",
            "same query",
            session_id,
            {"summary": "first"},
            first_artifact,
            first,
            context=evidence_context,
        )

        write_response_snapshot(root, "UNRELATED SECOND", "run-b", session_id)
        hit = evidence_store.find_fresh(
            root, "analyze", "same query", context=evidence_context
        )
        assert_true(hit is not None, "fresh immutable evidence must be reusable")
        assert_true(
            evidence_store.read_artifact(root, hit) == first,
            "reuse must return the indexed run, never mutable response.last.md",
        )
        assert_true(
            evidence_store.find_fresh(
                root,
                "analyze",
                "same query",
                context={"model": "provider/model-b", "agent": "plan"},
            )
            is None
            and evidence_store.find_fresh(
                root, "analyze", "Same query", context=evidence_context
            )
            is None,
            "evidence reuse must distinguish effective model context and task casing",
        )

        errors: list[Exception] = []

        def record_one(index: int) -> None:
            try:
                content = f"claim {index} [src.py:1]"
                prompt_id = f"run-{index + 10}"
                write_response_snapshot(root, content, prompt_id, f"session-{index}")
                artifact = (
                    workflow_paths(root, f"session-{index}")["logs_dir"]
                    / prompt_id
                    / "output.raw.md"
                )
                evidence_store.record(
                    root,
                    "explore",
                    f"query {index}",
                    f"session-{index}",
                    {"summary": str(index)},
                    artifact,
                    content,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=record_one, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert_true(not errors, f"concurrent evidence record failed: {errors}")
        index_path = workflow_paths(root)["data_dir"] / "evidence.jsonl"
        rows = [json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines()]
        assert_true(len(rows) == 7, f"concurrent evidence writes lost rows: {len(rows)}")

        outside = root.parent / f"{root.name}-outside.py"
        outside.write_text("outside_secret = 1\n", encoding="utf-8")
        try:
            traversal_content = f"claim [../{outside.name}:1]"
            write_response_snapshot(
                root, traversal_content, "run-traversal", session_id
            )
            traversal_entry = evidence_store.record(
                root,
                "analyze",
                "outside anchor",
                session_id,
                {"summary": "outside"},
                paths["logs_dir"] / "run-traversal" / "output.raw.md",
                traversal_content,
            )
            assert_true(
                traversal_entry.get("anchors_complete") is False
                and evidence_store.find_fresh(root, "analyze", "outside anchor") is None,
                "an anchor outside the project must never be certifiable or reusable",
            )
            traversal_anchors = traversal_entry.get("anchors") or []
            assert_true(
                len(traversal_anchors) == 1
                and traversal_anchors[0].get("certified") is False
                and traversal_anchors[0].get("hash") is None,
                "an uncertifiable anchor is recorded and marked, not dropped: "
                f"{traversal_anchors}",
            )

            drive_content = "claim [E:\\elsewhere\\router.py:16]"
            write_response_snapshot(root, drive_content, "run-drive", session_id)
            drive_entry = evidence_store.record(
                root,
                "analyze",
                "drive anchor",
                session_id,
                {"summary": "drive"},
                paths["logs_dir"] / "run-drive" / "output.raw.md",
                drive_content,
            )
            drive_anchors = drive_entry.get("anchors") or []
            assert_true(
                len(drive_anchors) == 1
                and drive_anchors[0].get("file") == "E:\\elsewhere\\router.py",
                "a Windows drive-qualified anchor keeps its drive letter: "
                f"{drive_anchors}",
            )
            # The widening admits a drive letter and nothing else. A prose colon reads like
            # `name:line` to a pattern that stops requiring an extension, and one anchor
            # invented out of a ratio is a claim the reader is told was verified.
            from exoma.core.evidence import contract as evidence_contract
            from exoma.core.evidence.fact_store import _FILELINE

            for prose in ("a ratio 3:14 of runs", "at 09:30 today", "chapter 2:4"):
                assert_true(
                    not _FILELINE.search(prose),
                    f"prose carrying a colon is not an anchor: {prose!r}",
                )
            # One pattern, two readers. They lived in separate files and drifted apart once
            # already, silently — the same text has to yield the same anchors in both.
            bare = "dispatch happens in E:\\elsewhere\\router.py:16 and core/runtime/state.py:301"
            assert_true(
                evidence_contract.split_claim(bare)["refs"]
                == [m.group(0) for m in _FILELINE.finditer(bare)]
                == ["E:\\elsewhere\\router.py:16", "core/runtime/state.py:301"],
                f"the contract parser and the fact store read one pattern the same way: "
                f"{evidence_contract.split_claim(bare)['refs']}",
            )
            assert_true(
                evidence_contract.split_claim("a ratio 3:14 of runs")["refs"] == [],
                "and neither of them turns prose into an anchor",
            )
        finally:
            outside.unlink(missing_ok=True)

        many = "\n".join(f"claim [src.py:{line}]" for line in range(1, 42))
        write_response_snapshot(root, many, "run-many", session_id)
        many_artifact = paths["logs_dir"] / "run-many" / "output.raw.md"
        many_entry = evidence_store.record(
            root,
            "analyze",
            "many anchors",
            session_id,
            {"summary": "many"},
            many_artifact,
            many,
        )
        assert_true(
            many_entry.get("anchors_complete") is False
            and evidence_store.find_fresh(root, "analyze", "many anchors") is None,
            "evidence with more anchors than the validation cap must never be reused",
        )

        first_artifact.write_text("tampered", encoding="utf-8")
        assert_true(
            evidence_store.find_fresh(
                root, "analyze", "same query", context=evidence_context
            )
            is None,
            "artifact hash mismatch must invalidate reuse",
        )
    finally:
        _shutil.rmtree(root, ignore_errors=True)
