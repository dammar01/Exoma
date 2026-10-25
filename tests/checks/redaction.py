"""Secret redaction at the adapter boundary."""

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

def _test_key_pattern_needs_a_left_boundary() -> None:
    """Kebab-case words that contain "sk-" are not keys; keys in any usual context still are.

    `task-events-at-turn-boundaries` matched the openai-style pattern, so a file name in
    second-agent evidence came back as `ta[REDACTED:...]` and its anchor was lost.
    """
    from exoma.utils.redact import redact_value

    kept = (
        "docs/notes/task-events-at-turn-boundaries.md:17",
        "task-notifications-do-not-arm-the-gate",
        "risk-assessment-for-the-release-gate",
        "a disk-usage-report-for-the-week",
    )
    for text in kept:
        clean, hits = redact_value(text)
        assert_true(clean == text and not hits, f"not a key, but redacted: {text!r} -> {clean!r}")

    key = "sk-ant-api03-" + "A" * 24
    for text in (key, f"KEY={key}", f'"{key}"', f"Bearer-less {key}", f"API_KEY_{key}", f"/{key}"):
        clean, hits = redact_value(text)
        assert_true(key not in clean and hits, f"key survived redaction: {text!r} -> {clean!r}")


def _test_redaction_boundary() -> None:
    """Secrets and raw prompt argv must not survive any adapter result path."""
    secret = "sk-" + "A" * 36
    adapter = OpenCodeAdapter(command="opencode", timeout_seconds=5)

    def outcome(*, returncode: int, timed_out: bool) -> dict:
        return {
            "stdout": "safe response" if not timed_out else "",
            "stderr": f"provider diagnostic {secret}",
            "returncode": returncode,
            "timed_out": timed_out,
            "duration_seconds": 0.1,
            "idle_seconds": 0.0,
            "kill": None,
        }

    for returncode, timed_out in ((0, False), (1, False), (1, True)):
        adapter._popen_capture = (  # type: ignore[method-assign]
            lambda *args, rc=returncode, to=timed_out, **kwargs: outcome(
                returncode=rc, timed_out=to
            )
        )
        result = adapter._run_args(["opencode", "run", f"prompt {secret}"])
        serialized = json.dumps(result)
        assert_true(secret not in serialized, f"secret leaked from adapter result: {result}")
        assert_true(
            "\"args\"" not in serialized and "prompt " not in serialized,
            f"raw prompt argv leaked from adapter metadata: {result}",
        )
        assert_true(
            (result.get("meta") or {}).get("redaction_count", 0) > 0,
            f"redaction must remain auditable on every result path: {result}",
        )

    existing = RecordingOpenCodeAdapter()
    callback_calls: list[str] = []
    existing.on_session_created = callback_calls.append
    existing_result = existing.run(
        "prompt",
        {"session_id": "workflow", "provider_session_id": "ses_existing"},
        "provider/model",
    )
    assert_true(
        existing_result["ok"] and not callback_calls,
        "an existing OpenCode session must not invoke the new-session persistence callback",
    )

    bootstrap = OpenCodeAdapter(command="opencode", timeout_seconds=5)
    bootstrap._popen_capture = lambda *args, **kwargs: outcome(  # type: ignore[method-assign]
        returncode=1, timed_out=False
    )
    _session_id, bootstrap_meta = bootstrap.init_session()
    assert_true(
        secret not in json.dumps(bootstrap_meta)
        and bool(bootstrap_meta.get("stderr_tail")),
        f"bootstrap failures must retain sanitized diagnostics: {bootstrap_meta}",
    )
