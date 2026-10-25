"""A provider thread is resumed only by the provider that issued it.

One session record used to hold one thread id whoever issued it. After `/.provider`
switched codex to opencode mid-session, the next call handed opencode codex's id to
resume and failed in two seconds with "Session not found" — reproduced in a real
`usage.jsonl` before this check existed. The executor now binds the record to the
selected provider before every call, and keeps each provider's thread for when it is
selected again.
"""

from __future__ import annotations
from exoma.core.workspace.workspace_paths import data_dir

import json
import os
import shutil
import tempfile
from pathlib import Path

from exoma.core.prompt.router import Router
from exoma.core.provider.executor import Executor
from exoma.core.runtime.state import ensure_workflow_workspace
from exoma.core.workspace.session_manager import SessionManager, bind_provider
from tests.checks.continuation import _EVIDENCE
from tests.checks.support import assert_true


class _ThreadAdapter:
    """Resumes what it is handed; issues its own id when handed none."""

    def __init__(self, name: str, issued: str) -> None:
        self.adapter = name
        self.issued = issued
        self.resumed: list[str | None] = []
        self.on_session_created = None

    def run(self, prompt, session, model=None, work_dir=None) -> dict:
        resume = session.get("provider_session_id")
        self.resumed.append(resume)
        thread = resume or self.issued
        if not resume and self.on_session_created:
            self.on_session_created(thread)
        return {"ok": True, "content": _EVIDENCE, "meta": {"provider_session_id": thread}}


def _router(provider: str) -> Router:
    return Router({"provider": provider, "provider_command": provider, "default_model": None, "routes": {}})


def _test_provider_threads_are_kept_per_provider() -> None:
    # Legacy adoption: a bare id is kept only when its shape fits the selected provider.
    legacy_codex = {"session_id": "s", "provider_session_id": "01a0a4e5-f884-71e3-968e-d68ac3a944ea"}
    assert_true(bind_provider(legacy_codex, "opencode"), "binding a legacy record must report a change")
    assert_true(
        legacy_codex["provider_session_id"] is None and legacy_codex["provider_sessions"] == {},
        f"a codex-shaped legacy id must never be offered to opencode: {legacy_codex}",
    )
    legacy_opencode = {"session_id": "s", "provider_session_id": "ses_legacy"}
    bind_provider(legacy_opencode, "opencode")
    assert_true(
        legacy_opencode["provider_sessions"] == {"opencode": "ses_legacy"}
        and legacy_opencode["provider_session_id"] == "ses_legacy",
        f"an opencode-shaped legacy id is adopted by opencode: {legacy_opencode}",
    )
    legacy_for_codex = {"session_id": "s", "provider_session_id": "ses_legacy"}
    bind_provider(legacy_for_codex, "codex")
    assert_true(legacy_for_codex["provider_session_id"] is None, f"and never by codex: {legacy_for_codex}")
    unbound = {"session_id": "s", "provider_session_id": "x"}
    assert_true(not bind_provider(unbound, None) and unbound == {"session_id": "s", "provider_session_id": "x"}, "no provider, no rewrite")

    # An id written onto a NEW record before its first call (recovery does exactly this) is
    # adopted too: the empty map on a fresh record must not read as "already bound".
    early_root = Path(tempfile.mkdtemp(prefix="provider-threads-early-"))
    try:
        early_store = SessionManager(early_root)
        early = early_store.load_or_create("recover")
        early_store.update_provider_session_id(early, "ses_recover")
        early_store.bind_provider(early, "opencode")
        assert_true(early["provider_session_id"] == "ses_recover" and early["provider_sessions"] == {"opencode": "ses_recover"}, f"a pre-binding id survives the first bind: {early}")
        early_store.bind_provider(early, "codex")
        assert_true(early["provider_session_id"] is None, f"and a bound record never hands it to another provider: {early}")
        early_store.bind_provider(early, "opencode")
        assert_true(early["provider_session_id"] == "ses_recover", f"switching back finds it again: {early}")
    finally:
        shutil.rmtree(early_root, ignore_errors=True)

    root = Path(tempfile.mkdtemp(prefix="provider-threads-"))
    try:
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
        store = SessionManager(data_dir(root) / "provider-sessions")
        session = store.load_or_create("switch-session")

        def call(provider: str, adapter: _ThreadAdapter, task: str) -> dict:
            return Executor(router=_router(provider), adapter=adapter, session_manager=store).execute(
                "analyze", task, session, str(root), allow_reuse=False, session_manager=store
            )

        codex = _ThreadAdapter("codex", "codex-thread-1")
        first = call("codex", codex, "map the thing on codex")
        assert_true(first.get("ok") and codex.resumed == [None], f"a new session bootstraps on codex: {codex.resumed} {first.get('meta')}")

        opencode = _ThreadAdapter("opencode", "ses_opencode_1")
        second = call("opencode", opencode, "map the thing on opencode")
        assert_true(
            second.get("ok") and opencode.resumed == [None],
            f"after a switch opencode must not be handed codex's thread: {opencode.resumed} {second.get('meta')}",
        )

        back = _ThreadAdapter("codex", "codex-thread-2")
        third = call("codex", back, "map the thing on codex again")
        assert_true(
            third.get("ok") and back.resumed == ["codex-thread-1"],
            f"switching back resumes codex's own thread, not a new one: {back.resumed}",
        )
        again = _ThreadAdapter("codex", "codex-thread-3")
        call("codex", again, "and once more on codex")
        assert_true(again.resumed == ["codex-thread-1"], f"same provider keeps resuming: {again.resumed}")

        stored = json.loads((data_dir(root) / "provider-sessions" / "switch-session.json").read_text(encoding="utf-8"))
        assert_true(
            stored.get("provider_sessions") == {"codex": "codex-thread-1", "opencode": "ses_opencode_1"}
            and stored.get("provider") == "codex"
            and stored.get("provider_session_id") == "codex-thread-1",
            f"both threads are persisted, the flat key follows the active provider: {stored}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
