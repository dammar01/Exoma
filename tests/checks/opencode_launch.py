"""opencode's prompt never reaches cmd.exe.

On Windows `opencode` resolves to the npm shim `opencode.cmd`, so its command line is parsed
by cmd.exe, which does not understand the `\\"` escaping `subprocess` applies. With the prompt
on argv, an odd run of quotes flipped cmd's quote state and the rest became shell syntax:
every verify-browser draft failed with "The system cannot find the file specified" (its
scaffolding carries `"<value unique to this run>"`, read as an input redirect), and
`x" & echo INJECTED & "` in a task ran the echo. Both were reproduced through a dummy shim
before this was written.

The prompt now travels as a file attached with `-f`, and anything cmd.exe would still
interpret in the remaining arguments is refused before spawning.
"""

from __future__ import annotations
from exoma.core.workspace.workspace_paths import data_dir

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

from exoma.adapters.providers import opencode_adapter
from exoma.adapters.providers.opencode_adapter import OpenCodeAdapter
from tests.checks.support import assert_true

# Every hostile shape the old argv transport broke on, in one prompt.
_HOSTILE_PROMPT = (
    'line one\n'
    'test_data: {"marker": "<value unique to this run>"}\n'
    'x" & echo INJECTED_MARKER & "\n'
    "pipes | carets ^ percent %PATH% > redirect\n"
)


@contextlib.contextmanager
def _cmd_parsing(applies: bool):
    original = opencode_adapter._cmd_parsing_applies
    opencode_adapter._cmd_parsing_applies = lambda: applies
    try:
        yield
    finally:
        opencode_adapter._cmd_parsing_applies = original


class _Captured(OpenCodeAdapter):
    """Real argv building and real guards; only the spawn is replaced."""

    def __init__(self, command_path: str) -> None:
        super().__init__(command="opencode", timeout_seconds=5)
        self.command_path = command_path
        self.spawned: list[list[str]] = []
        self.bootstraps = 0

    def _resolve_command(self) -> str:
        return self.command_path

    def init_session(self, model=None, work_dir=None, workflow_session_id=None):
        self.bootstraps += 1
        return "ses_launch", {"provider_session_id": "ses_launch", "returncode": 0}

    def _popen_capture(self, args, env, cwd, timeout, phase):
        self.spawned.append(list(args))
        return {
            "returncode": 0,
            "stdout": '{"type":"text","part":{"text":"OK"}}',
            "stderr": "",
            "timed_out": False,
            "duration_seconds": 0.01,
            "idle_seconds": 0.0,
            "pid": 1,
            "kill": None,
        }


def _check_hazard_detection() -> None:
    hostile = ["C:/tools/opencode.cmd", "run", 'x" & echo INJECTED & "']
    with _cmd_parsing(True):
        hazards = opencode_adapter._cmd_shell_hazards(hostile)
        assert_true(
            hazards and hazards[0]["argv_index"] == 2,
            f"a quote-and-ampersand argument to a .cmd shim must be flagged: {hazards}",
        )
        assert_true(
            opencode_adapter._cmd_shell_hazards(["C:/a&b/opencode.cmd", "run"]),
            "the executable path is parsed by cmd.exe too; an `&` in it splits the command",
        )
        assert_true(
            not opencode_adapter._cmd_shell_hazards(["C:/tools/opencode.exe", "run", 'x" & y']),
            "a native .exe reaches CreateProcess without cmd.exe and has nothing to refuse",
        )
    with _cmd_parsing(False):
        assert_true(
            not opencode_adapter._cmd_shell_hazards(hostile),
            "off Windows there is no cmd.exe in the path, so nothing may be refused",
        )


def _check_prompt_travels_as_file(root: Path) -> None:
    adapter = _Captured("C:/tools/opencode.cmd")
    with _cmd_parsing(True):
        result = adapter.run(
            _HOSTILE_PROMPT,
            {"session_id": "sid-launch", "provider_session_id": None},
            "openrouter/some/model",
            str(root),
        )
    assert_true(result["ok"], f"a hostile PROMPT is content, not a hazard, once it is a file: {result}")
    assert_true(len(adapter.spawned) == 1, f"exactly one agent spawn expected: {adapter.spawned}")
    args = adapter.spawned[0]
    assert_true(
        args[1:5] == ["run", opencode_adapter._ATTACHED_PROMPT_INSTRUCTION, "-f", args[4]],
        f"argv must carry the static instruction and the attached file, got {args}",
    )
    assert_true(
        not any("INJECTED_MARKER" in str(arg) or "<value" in str(arg) for arg in args),
        f"no part of the prompt may remain on the command line: {args}",
    )
    prompt_path = Path(args[4])
    expected_dir = data_dir(root.resolve()) / "sessions" / "sid-launch" / "runtime"
    assert_true(
        prompt_path.parent == expected_dir,
        f"the prompt file belongs in the session runtime dir ({expected_dir}), got {prompt_path}",
    )
    assert_true(
        prompt_path.read_text(encoding="utf-8") == _HOSTILE_PROMPT,
        "the file must hold the prompt byte for byte, newlines included — the old ` \\n ` "
        "rewrite existed only to survive argv",
    )
    assert_true(args[-2:] == ["-s", "ses_launch"], f"the captured session must be resumed: {args}")


def _check_unsafe_config_is_refused_before_bootstrap(root: Path) -> None:
    adapter = _Captured("C:/tools/opencode.cmd")
    with _cmd_parsing(True):
        result = adapter.run(
            "plain prompt",
            {"session_id": "sid-launch", "provider_session_id": None},
            'model" & calc & "',
            str(root),
        )
    assert_true(
        not result["ok"] and result["meta"].get("error_type") == "unsafe_command_line",
        f"a model id cmd.exe would execute must be refused: {result}",
    )
    assert_true(
        adapter.bootstraps == 0 and not adapter.spawned,
        "the refusal is knowable before bootstrap, so no session may be opened for it",
    )


def _check_missing_model_is_refused(root: Path) -> None:
    """Without `-m`, opencode answers with the model it used LAST on this machine."""
    adapter = _Captured("C:/tools/opencode.exe")
    result = adapter.run(
        "plain prompt", {"session_id": "sid-launch", "provider_session_id": None}, None, str(root)
    )
    assert_true(
        not result["ok"] and result["meta"].get("error_type") == "model_unset",
        f"an opencode call with no model must be refused: {result}",
    )
    assert_true(
        adapter.bootstraps == 0 and not adapter.spawned,
        "and refused before a session is opened on whatever model opencode would pick",
    )


def _check_doctor_names_free_tier_models() -> None:
    from exoma.core.audit.diagnostics import _free_tier_models

    found = _free_tier_models(
        {
            "default_model": "opencode/mimo-v2.5-free",
            "routes": {
                "explore": {"model": "opencode/mimo-v2.5-free"},
                "e2e_spec": {"model": "opencode/nemotron-3-ultra-free"},
                "plan": {"model": "openrouter/deepseek/deepseek-v4-flash-0731"},
            },
        }
    )
    assert_true(
        found == [("default_model", "opencode/mimo-v2.5-free"), ("routes.e2e_spec", "opencode/nemotron-3-ultra-free")],
        f"doctor names each free-tier pick once, routes that repeat the default excluded: {found}",
    )


def _check_through_a_real_shim(root: Path) -> None:
    """Windows only: the whole argv, through an actual .cmd, runs nothing it was not meant to."""
    if os.name != "nt":
        return
    shim = root / "fake-opencode.cmd"
    shim.write_text("@echo off\r\necho ARGS: %*\r\n", encoding="utf-8")

    class _RealSpawn(OpenCodeAdapter):
        def _resolve_command(self) -> str:
            return str(shim)

    adapter = _RealSpawn(command="opencode", timeout_seconds=30)
    result = adapter.run_agent(_HOSTILE_PROMPT, "ses_shim", None, str(root), workflow_session_id="sid-shim")
    content = result.get("content") or ""
    assert_true(result["ok"], f"the shim must run cleanly with a hostile prompt attached: {result}")
    assert_true(
        "ARGS:" in content and "INJECTED_MARKER" not in content,
        f"cmd.exe must see only the instruction and the file path: {content!r}",
    )


def _test_opencode_prompt_never_reaches_cmd() -> None:
    _check_hazard_detection()
    root = Path(tempfile.mkdtemp(prefix="aw-opencode-launch-"))
    try:
        _check_prompt_travels_as_file(root)
        _check_unsafe_config_is_refused_before_bootstrap(root)
        _check_missing_model_is_refused(root)
        _check_doctor_names_free_tier_models()
        _check_through_a_real_shim(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
