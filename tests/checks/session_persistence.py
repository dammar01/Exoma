"""A provider thread id, once handed over, is kept — or its loss is named.

codex and agy report their thread id mid-stream, after the work has started, through a
callback whose failure the adapters swallow so the run survives. The write behind that
callback could fail on Windows whenever any reader held the session file open, and the
executor then believed the id was saved: `update_provider_session_id` sets it on the dict
before writing, so the post-run save skipped the one id that needed it. The thread was
lost without a word, and the next call or recovery started from nothing.

Now: a held file is outlasted by a bounded retry, storage that cannot be written fails
the call before the provider runs, a write that still fails mid-run is retried after it,
and a loss that survives both is named on the result. Every delegated result also says
what its provider was kept from (`provider_boundary`), and an agy result warns it can write.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from exoma.core.prompt.router import Router
from exoma.core.provider.executor import Executor
from exoma.core.runtime.state import ensure_workflow_workspace
from exoma.core.workspace import workspace_paths
from exoma.core.workspace.session_manager import SessionManager
from exoma.core.workspace.workspace_paths import data_dir, read_json_file
from tests.checks.continuation import _EVIDENCE
from tests.checks.support import assert_true


class _MidStreamAdapter:
    """Reports its thread id through the callback while the run is under way, like codex."""

    def __init__(self, name: str, issued: str) -> None:
        self.adapter = name
        self.issued = issued
        self.ran = 0
        self.on_session_created = None

    def run(self, prompt, session, model=None, work_dir=None) -> dict:
        self.ran += 1
        if self.on_session_created:
            try:
                self.on_session_created(self.issued)
            except Exception:
                pass  # what the real adapters do: the run must survive its callback
        return {"ok": True, "content": _EVIDENCE, "meta": {"provider_session_id": self.issued}}


class _FlakyStore(SessionManager):
    """A session store whose thread-id writes fail the first `failures` times."""

    def __init__(self, session_dir: Path, failures: int, writable: bool = True) -> None:
        super().__init__(session_dir)
        self.failures = failures
        self.writable = writable

    def ensure_writable(self, session: dict) -> None:
        if not self.writable:
            raise PermissionError("session directory is read-only")
        super().ensure_writable(session)

    def update_provider_session_id(self, session: dict, provider_session_id: str) -> None:
        if self.failures > 0:
            self.failures -= 1
            # The real failure mode: the id lands on the dict, then the write raises.
            session["provider_session_id"] = provider_session_id
            raise PermissionError("[WinError 5] Access is denied")
        super().update_provider_session_id(session, provider_session_id)


def _router(provider: str) -> Router:
    return Router({"provider": provider, "provider_command": provider, "default_model": None, "routes": {}})


def _call(root: Path, store: SessionManager, provider: str, adapter, session_name: str) -> dict:
    session = store.load_or_create(session_name)
    return Executor(router=_router(provider), adapter=adapter, session_manager=store).execute(
        "analyze", f"map the thing ({session_name})", session, str(root), allow_reuse=False, session_manager=store
    )


def _test_atomic_write_outlasts_a_held_file() -> None:
    original_replace = os.replace
    original_delays = workspace_paths.REPLACE_RETRY_DELAYS
    root = Path(tempfile.mkdtemp(prefix="atomic-held-"))
    target = root / "record.json"
    refusals = {"left": 2}

    def held_replace(source, destination):
        if refusals["left"] > 0:
            refusals["left"] -= 1
            raise PermissionError("[WinError 5] Access is denied")
        return original_replace(source, destination)

    try:
        workspace_paths.REPLACE_RETRY_DELAYS = (0, 0, 0, 0, 0)
        os.replace = held_replace
        workspace_paths.atomic_write_json(target, {"kept": True})
        assert_true(read_json_file(target) == {"kept": True}, "a reader holding the file briefly must not lose the write")
        assert_true(refusals["left"] == 0, "the write was refused twice before it landed")

        def always_held(source, destination):
            raise PermissionError("[WinError 5] Access is denied")

        os.replace = always_held
        try:
            workspace_paths.atomic_write_json(target, {"kept": False})
            raised = False
        except PermissionError:
            raised = True
        assert_true(raised, "a real permission problem still raises after the last attempt")
        os.replace = original_replace
        assert_true(read_json_file(target) == {"kept": True}, "a failed write leaves the previous record intact")
        assert_true(
            [p.name for p in root.iterdir()] == ["record.json"],
            f"no temp file is left behind: {[p.name for p in root.iterdir()]}",
        )
    finally:
        os.replace = original_replace
        workspace_paths.REPLACE_RETRY_DELAYS = original_delays
        shutil.rmtree(root, ignore_errors=True)


def _test_provider_thread_id_is_never_lost_silently() -> None:
    root = Path(tempfile.mkdtemp(prefix="session-persistence-"))
    try:
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
        sessions = data_dir(root) / "persist-sessions"

        # Mid-run write fails once: retried after the run, and the id is on disk.
        once = _FlakyStore(sessions, failures=1)
        adapter = _MidStreamAdapter("codex", "codex-thread-once")
        result = _call(root, once, "codex", adapter, "fails-once")
        stored = read_json_file(sessions / "fails-once.json")
        assert_true(result.get("ok"), f"a failed mid-run write must not fail the call: {result.get('meta')}")
        assert_true(
            stored.get("provider_session_id") == "codex-thread-once",
            f"the id the mid-run write lost is saved after the run: {stored}",
        )
        assert_true(
            "session_persisted" not in (result.get("meta") or {}),
            f"a recovered write is not reported as a loss: {result.get('meta')}",
        )

        # Both writes fail: the answer stands, the loss is named.
        never = _FlakyStore(sessions, failures=2)
        lost = _call(root, never, "codex", _MidStreamAdapter("codex", "codex-thread-lost"), "fails-twice")
        meta = lost.get("meta") or {}
        assert_true(lost.get("ok"), f"paid evidence is kept even when the id is not: {meta}")
        assert_true(
            meta.get("session_persisted") is False and "PermissionError" in str(meta.get("session_persist_error")),
            f"a lost thread id is named on the result: {meta}",
        )

        # Storage that cannot be written fails before the provider runs.
        blocked_adapter = _MidStreamAdapter("codex", "codex-thread-never")
        blocked = _call(root, _FlakyStore(sessions, failures=0, writable=False), "codex", blocked_adapter, "read-only")
        assert_true(
            not blocked.get("ok") and (blocked.get("meta") or {}).get("error_type") == "session_capture_failed",
            f"unwritable session storage is a named error: {blocked}",
        )
        assert_true(blocked_adapter.ran == 0, "and the provider is never started, so nothing is paid for")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_result_names_the_provider_boundary() -> None:
    root = Path(tempfile.mkdtemp(prefix="provider-boundary-meta-"))
    try:
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
        store = SessionManager(data_dir(root) / "boundary-sessions")
        expected = {
            "opencode": {"reads": "secrets_denied", "writes": "denied"},
            "codex": {"reads": "unbounded", "writes": "sandboxed"},
            "agy": {"reads": "unbounded", "writes": "unbounded"},
        }
        for provider, boundary in expected.items():
            result = _call(root, store, provider, _MidStreamAdapter(provider, f"{provider}-thread"), f"boundary-{provider}")
            meta = result.get("meta") or {}
            assert_true(meta.get("provider_boundary") == boundary, f"{provider}: result carries its boundary: {meta.get('provider_boundary')}")
            warning = meta.get("provider_write_warning")
            if boundary["writes"] == "unbounded":
                assert_true(bool(warning) and provider in warning, f"{provider}: a writer is warned about on the result: {meta}")
            else:
                assert_true(warning is None, f"{provider}: no write warning where writes are stopped: {warning}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
