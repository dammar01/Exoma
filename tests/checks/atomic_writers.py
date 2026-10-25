"""Every temp-and-replace writer outlasts a briefly held file, and keeps its error policy.

Windows refuses `os.replace` onto a file another process holds open, and a `check.py` or
hook merely reading the target is enough. Only the shared helper used to retry; a dozen
writers staged and replaced on their own and failed the first time a reader was there.
They now go through `atomic_write_text` / `atomic_write_bytes` (tagging borrows only
`replace_with_retry`, because its identity check must sit right before the move).

Centralising the replace must not change what each writer does when the replace keeps
failing: the stores and the installer raise, the caches and the `current/` mirror swallow.
Both halves are checked here, writer by writer.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from tests.checks.support import assert_true


def _held(times: int):
    """An `os.replace` that refuses `times` times, then behaves."""
    original = os.replace
    left = {"n": times}

    def replace(source, destination):
        if left["n"] > 0:
            left["n"] -= 1
            raise PermissionError("[WinError 5] Access is denied")
        return original(source, destination)

    return replace, left


def _always_held(source, destination):
    raise PermissionError("[WinError 5] Access is denied")


def _no_temps(directory: Path) -> list[str]:
    return [p.name for p in directory.rglob("*") if p.name.endswith((".tmp", ".rollback"))]


class _Patched:
    """Swap `os.replace` (and the retry delays) for the duration of a block."""

    def __init__(self, replacement) -> None:
        self.replacement = replacement

    def __enter__(self):
        from exoma.core.workspace import workspace_paths

        self._wp = workspace_paths
        self._replace = os.replace
        self._delays = workspace_paths.REPLACE_RETRY_DELAYS
        workspace_paths.REPLACE_RETRY_DELAYS = (0, 0, 0, 0, 0)
        os.replace = self.replacement
        return self

    def __exit__(self, *exc) -> None:
        os.replace = self._replace
        self._wp.REPLACE_RETRY_DELAYS = self._delays


def _test_atomic_helpers_keep_their_contract() -> None:
    from exoma.core.workspace.workspace_paths import atomic_write_bytes, atomic_write_text

    root = Path(tempfile.mkdtemp(prefix="atomic-helpers-"))
    try:
        text_path = root / "nested" / "a.txt"
        atomic_write_text(text_path, "one\ntwo\n")
        assert_true(
            text_path.read_text(encoding="utf-8").splitlines() == ["one", "two"],
            "text helper creates parents and writes the content",
        )
        atomic_write_text(text_path, "x\ny\n", newline="")
        assert_true(
            text_path.read_bytes() == b"x\ny\n",
            "newline='' writes LF untranslated on every platform",
        )
        atomic_write_text(text_path, "durable", fsync=True)
        assert_true(text_path.read_text(encoding="utf-8") == "durable", "fsync=True still writes")

        bin_path = root / "b.bin"
        atomic_write_bytes(bin_path, b"\x00\x01\x02", fsync=True)
        assert_true(bin_path.read_bytes() == b"\x00\x01\x02", "bytes helper writes raw bytes")
        if os.name != "nt":
            atomic_write_bytes(bin_path, b"m", mode=0o640)
            assert_true(
                stat.S_IMODE(bin_path.stat().st_mode) == 0o640,
                "mode is applied to the staged file before the move",
            )
        try:
            atomic_write_text(text_path, 123)  # type: ignore[arg-type]
            raised = False
        except TypeError:
            raised = True
        assert_true(raised, "a write that fails while staging raises")
        assert_true(
            text_path.read_text(encoding="utf-8") == "durable",
            "a failed staging write leaves the previous content",
        )
        assert_true(not _no_temps(root), f"no temp is left behind: {_no_temps(root)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _writers(root: Path) -> list[tuple[str, callable, callable, bool]]:
    """(name, write, landed, raises_when_held) for every migrated writer."""
    from exoma.config import settings
    from exoma.core.evidence import evidence_store, fact_store
    from exoma.core.evidence.e2e import knowledge, tagging
    from exoma.core.graph import graph_index
    from exoma.core.provider import versions
    from exoma.core.runtime import migrations
    from exoma.core.workspace import current
    from exoma.core.workspace.workspace_paths import workflow_paths
    from exoma.installer import base, stale

    paths = workflow_paths(root, "s1")

    def facts_write():
        fact_store._save_facts(root, [])

    def facts_landed():
        return fact_store._facts_path(root).is_file()

    claim_cache = root / "claims.json"

    def claims_write():
        fact_store._save_claim_cache(claim_cache, {"s1": 1, "gone": 2}, {"s1"})

    def claims_landed():
        return claim_cache.is_file() and json.loads(claim_cache.read_text(encoding="utf-8")) == {"s1": 1}

    def evidence_write():
        evidence_store._save(root, [{"id": "e1"}])

    def evidence_landed():
        return '"e1"' in evidence_store._path(root).read_text(encoding="utf-8")

    def knowledge_write():
        knowledge._save(root, [{"id": "k1", "first_seen": "1"}])

    def knowledge_landed():
        return [e["id"] for e in knowledge.load(root)] == ["k1"]

    def sidecar_write():
        knowledge.write_sidecar(root, "s1", "http://app.test")

    def sidecar_landed():
        return (paths["runtime_dir"] / knowledge.SIDECAR_NAME).is_file()

    def stale_write():
        graph_index._write_stale_cache(root, 1, "fp", True)

    def stale_landed():
        return graph_index._stale_cache_path(root).is_file()

    versions_cache = root / "versions.json"

    def versions_write():
        original = versions._cache_path
        versions._cache_path = lambda: versions_cache
        try:
            versions._disk_store("exe|1|2", "1.0")
        finally:
            versions._cache_path = original

    def versions_landed():
        return versions_cache.is_file()

    index = root / "evidence-index.jsonl"
    old_root = root / "old"

    def migrate_write():
        index.write_text(
            json.dumps({"artifact_path": str(old_root / "sessions" / "x")}) + "\n",
            encoding="utf-8",
        )
        migrations._rewrite_evidence_paths(index, old_root, root / "new")

    def migrate_landed():
        row = json.loads(index.read_text(encoding="utf-8").splitlines()[0])
        return row["artifact_path"].startswith(str(root / "new"))

    session_cache = root / "main-session.json"

    def settings_write():
        original = settings.CACHE_FILE
        settings.CACHE_FILE = str(session_cache)
        try:
            settings.set_cached_main_session_id("main_x")
        finally:
            settings.CACHE_FILE = original

    def settings_landed():
        return json.loads(session_cache.read_text(encoding="utf-8"))["main_session_id"] == "main_x"

    receipt = root / "receipt.json"

    def receipt_write():
        base._reset_receipt()
        base._RECEIPT_TARGET["path"] = receipt
        base._RECEIPT.append({"dest": "x"})
        try:
            base._flush_receipt()
        finally:
            base._reset_receipt()

    def receipt_landed():
        return json.loads(receipt.read_text(encoding="utf-8"))["entries"] == [{"dest": "x"}]

    ledger = root / "ledger.json"

    def ledger_write():
        original = stale.LEDGER
        stale.LEDGER = ledger
        try:
            stale.write_ledger([], apply=True)
        finally:
            stale.LEDGER = original

    def ledger_landed():
        return json.loads(ledger.read_text(encoding="utf-8"))["files"] == {}

    shots = root / "final"
    e2e_dir = root / "e2e-out"
    shots.mkdir()
    e2e_dir.mkdir()
    (shots / "step01.png").write_bytes(b"\x89PNG-1")
    # Set up outside the patched replace: this write is the mirror's own, not the one tested.
    current._write_session(root, {"session_id": "s1"})

    def screenshot_write():
        current.e2e_finish(root, "s1", e2e_dir, shots)

    def screenshot_landed():
        target = paths["current_dir"] / "e2e" / "last.png"
        return target.is_file() and target.read_bytes() == b"\x89PNG-1"

    tagged = root / "page.html"

    def tagging_write():
        tagged.write_bytes(b"<a>old</a>")
        tagging._replace_contents(
            tagged, b"<a data-e2e='x'>old</a>", stat.S_IMODE(tagged.stat().st_mode), tagging._identity(tagged)
        )

    def tagging_landed():
        return b"data-e2e" in tagged.read_bytes()

    return [
        ("fact store", facts_write, facts_landed, True),
        ("recurrence cache", claims_write, claims_landed, False),
        ("evidence store", evidence_write, evidence_landed, True),
        ("e2e knowledge store", knowledge_write, knowledge_landed, True),
        ("e2e knowledge sidecar", sidecar_write, sidecar_landed, True),
        ("graph stale cache", stale_write, stale_landed, False),
        ("provider version cache", versions_write, versions_landed, False),
        ("migration index rewrite", migrate_write, migrate_landed, True),
        ("main-session cache", settings_write, settings_landed, True),
        ("installer receipt", receipt_write, receipt_landed, True),
        ("installer ledger", ledger_write, ledger_landed, True),
        ("current screenshot", screenshot_write, screenshot_landed, False),
        ("e2e tagging", tagging_write, tagging_landed, True),
    ]


def _test_every_writer_outlasts_a_held_file() -> None:
    root = Path(tempfile.mkdtemp(prefix="atomic-writers-"))
    try:
        for name, write, landed, raises in _writers(root):
            replacement, left = _held(2)
            with _Patched(replacement):
                write()
            assert_true(landed(), f"{name}: a reader holding the file briefly must not lose the write")
            assert_true(left["n"] == 0, f"{name}: the write went through the shared retry")

            with _Patched(_always_held):
                try:
                    write()
                    raised = False
                except PermissionError:
                    raised = True
            expected = "raises" if raises else "is swallowed"
            assert_true(
                raised == raises,
                f"{name}: a replace that never succeeds {expected}, as before the migration",
            )
        assert_true(not _no_temps(root), f"no temp file is left behind: {_no_temps(root)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
