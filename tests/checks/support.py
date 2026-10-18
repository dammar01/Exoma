"""Fakes and the assertion helper shared by every checks/ module."""

import os
import json

from adapters.providers.opencode_adapter import OpenCodeAdapter
from core.jobs.job_manager import JobManager
from core.provider.executor import Executor
from core.prompt.prompt_builder import build_prompt
from core.runtime.state import ensure_workflow_workspace
from adapters.providers.opencode_adapter import OpenCodeAdapter as _OpenCodeAdapterForParsing

clean_output = _OpenCodeAdapterForParsing.clean_output
extract_session_id = _OpenCodeAdapterForParsing.extract_session_id
import check
import main
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# `install.py --apply` writes `config/second_agent.seed.json` from the maintainer's provider
# pick (git-ignored), and every workspace a test initialises copies it. A machine that picked
# codex ran the suite as codex: an opencode-shaped thread id (`ses_...`) was dropped by
# `bind_provider`, and the continuation and recovery checks failed on that machine only.
# Every checks/ module imports this one, so the patch lands before any workspace is built.
EXAMPLE_SEED = "second_agent.example.json"


def isolate_provider_seed() -> None:
    from adapters.install import opencode_install

    opencode_install.PROVIDER_SEED_NAME = EXAMPLE_SEED


isolate_provider_seed()


class FakeOpenCodeAdapter:
    def __init__(self) -> None:
        self.calls = []
        self.fail_next = False

    def run(self, prompt: str, session: dict, model: str | None = None, work_dir: str | None = None) -> dict:
        self.calls.append({"prompt": prompt, "session": dict(session), "model": model, "work_dir": work_dir})
        if self.fail_next:
            self.fail_next = False
            return {"ok": False, "content": "simulated opencode failure", "meta": {"simulated": True}}
        content = """
INFO  2026-05-09T12:10:24 +1ms service=session.prompt session.id=ses_test123 step=0 loop
> build · gpt-5.3-codex
[EVIDENCE]
findings:
- entry point at app/main.py
[DIGEST]
summary: OpenCode response.
confidence: high
INFO  2026-05-09T12:10:28 +0ms service=session.idle publishing
""".strip()
        return {
            "ok": True,
            "content": clean_output(content),
            "meta": {
                "simulated": True,
                "provider_session_id": extract_session_id(content),
                "args": ["opencode", "run", prompt],
            },
        }


class RecordingOpenCodeAdapter(OpenCodeAdapter):
    def __init__(self) -> None:
        super().__init__(command="opencode")
        self.init_calls = []
        self.run_calls = []

    def init_session(
        self,
        model: str | None = None,
        work_dir: str | None = None,
        workflow_session_id: str | None = None,
    ) -> tuple[str | None, dict]:
        args = ["opencode", "run", "Initialize session. Reply READY."]
        if model:
            args.extend(["-m", model])
        args.extend(["--print-logs", "--log-level", "INFO"])
        self.init_calls.append(
            {"args": args, "model": model, "work_dir": work_dir, "workflow_session_id": workflow_session_id}
        )
        return "ses_boot123", {
            "args": args,
            "provider_session_id": "ses_boot123",
            "returncode": 0,
        }

    def _run_args(self, args: list[str], work_dir: str | None = None) -> dict:
        self.run_calls.append({"args": args, "work_dir": work_dir})
        return {
            "ok": True,
            "content": "OpenCode response",
            "meta": {"args": args, "provider_session_id": None, "cwd": work_dir},
        }


class FakeJobProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
