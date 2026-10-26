"""The statusline's `~` means an estimate, not "a call failed somewhere".

Every row that was not provider-counted used to mark the whole session `~`, and a call
that failed before any provider counted it (a rate limit, a refused resume) writes exactly
such a row. One failed call turned a fully measured session into an "estimate" — read, in
practice, as "this provider cannot be measured". Failed calls are now counted beside the
calls and kept out of the totals.

workflow-statusline.py runs directly through this interpreter (the logic under test lives
there), and its bash launcher runs as Claude Code runs it — through Git Bash on Windows,
found next to git, never WSL's bash, which cannot read a Windows path.
"""

from __future__ import annotations
from exoma.core.workspace.workspace_paths import data_dir

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.checks.support import assert_true
from exoma.utils.osutil import find_bash
from tests.checks.support import no_console

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "src" / "exoma" / "_bundle" / "config" / "claude" / "hooks"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_MEASURED = {"session_id": "main-1", "ok": True, "prompt_id": "p1", "token_source": "provider",
             "actual_input_tokens": 1000, "actual_output_tokens": 200, "actual_cached_input_tokens": 800,
             "estimated_input_tokens": 40, "estimated_output_tokens": 10}
_FAILED = {"session_id": "main-1", "ok": False, "prompt_id": "p2", "token_source": "estimated",
           "estimated_input_tokens": 50000, "estimated_output_tokens": 0, "premium_context_avoided_tokens": 999}
_ESTIMATED = {"session_id": "main-1", "ok": True, "prompt_id": "p3", "token_source": "estimated",
              "estimated_input_tokens": 500, "estimated_output_tokens": 100, "premium_context_avoided_tokens": 300}
_OTHER_SESSION = {**_MEASURED, "session_id": "main-2", "prompt_id": "p9"}


def _runners() -> list[tuple[str, list[str]]]:
    runners: list[tuple[str, list[str]]] = [("py", [sys.executable, str(HOOKS / "workflow-statusline.py")])]
    bash = find_bash()
    if bash:
        runners.append(("sh", [bash, str(HOOKS / "workflow-statusline.sh")]))
    return runners


def _render(command: list[str], rows: list[dict]) -> str:
    root = Path(tempfile.mkdtemp(prefix="statusline-"))
    try:
        claude_dir = root / "claude"
        claude_dir.mkdir()
        (claude_dir / "session_registry.json").write_text(json.dumps({"claude-sid": {"main_session_id": "main-1"}}), encoding="utf-8")
        project = root / "project"
        (project / ".workflow").mkdir(parents=True)
        data_dir(project).mkdir(parents=True, exist_ok=True)
        (data_dir(project) / "usage.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        context = json.dumps({"session_id": "claude-sid", "workspace": {"project_dir": str(project)}})
        # The bash wrapper hands stdin to python through this variable; the direct python
        # runner reads it from the same place.
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(claude_dir), "CLAUDE_STATUSLINE_RAW": context}
        done = subprocess.run(command, input=context, capture_output=True, text=True, encoding="utf-8", env=env, timeout=60, **no_console())
        return _ANSI.sub("", done.stdout)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_statusline_failed_calls_are_not_estimates() -> None:
    for flavour, command in _runners():
        measured = _render(command, [_MEASURED, _FAILED, _OTHER_SESSION])
        assert_true(
            "Second Agent 1.2k tok (800 cached) / 1 calls, 1 failed" in measured,
            f"[{flavour}] a failed call is counted beside the calls, not in the tokens and not as `~`: {measured!r}",
        )
        assert_true("Saved 200 tok" in measured and "~" not in measured, f"[{flavour}] nothing in a measured session is marked estimated: {measured!r}")

        mixed = _render(command, [_MEASURED, _ESTIMATED])
        assert_true(
            "Second Agent ~1.8k tok (800 cached) / 2 calls" in mixed and "failed" not in mixed and "Saved ~500 tok" in mixed,
            f"[{flavour}] a completed call that only has an estimate still marks `~`: {mixed!r}",
        )
