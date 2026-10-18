"""Every shipped hook, run through its bash launcher as Claude Code runs it, on every OS.

A hook is `<stem>.sh` (resolves an interpreter, execs workflow_hooks.py) and `<stem>.py`
(the logic). The other hook checks call the `.py` directly; this one runs the launcher
itself — `python` resolution, stdin, exit code — on Windows through Git Bash too. Each case
asks one hook what its contract says (exit code, stdout/stderr, the file it writes) and
times the call.

The gate's K5 fixtures (tests/fixtures/hooks/gate_k5.json) assert what the gate decides now
and list where the target gate must decide differently; the list is printed, not failed.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tests.checks.support import assert_true
from utils import osutil

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / "dist" / "config" / "claude" / "hooks"
K5 = REPO / "tests" / "fixtures" / "hooks" / "gate_k5.json"
# settings.template.json: graph-refresh 60s, task-events 10s, gate set/check 15s; session-bind
# and the statusline carry none, so the shortest one stands in for them.
_TIMEOUT_S = {
    "graph-refresh": 60, "task-events": 10, "intent-gate-set": 15, "intent-gate-check": 15,
    "session-bind": 10, "workflow-statusline": 10,
}


def _bash() -> str | None:
    """The bash Claude Code's hook command would reach (Git Bash on Windows)."""
    return osutil.find_bash()


def _flavours(hook: str) -> list[tuple[str, list[str]]]:
    """The one way a hook runs on every OS: its bash launcher."""
    bash = _bash()
    return [("sh", [bash, str(HOOKS / f"{hook}.sh")])] if bash else []


class _Workspace:
    """A throwaway home with a session registry and one project bound to MAIN_SESSION_ID m1."""

    def __init__(self, label: str) -> None:
        self.base = Path(tempfile.mkdtemp(prefix=f"hook-contract-{label}-"))
        self.home, self.project = self.base / "home", self.base / "project"
        (self.home / ".claude").mkdir(parents=True)
        self.runtime = self.project / ".workflow" / "data" / "sessions" / "m1" / "runtime"
        self.runtime.mkdir(parents=True)
        self.registry = self.home / ".claude" / "session_registry.json"
        self.registry.write_bytes(
            json.dumps({"c1": {"main_session_id": "m1", "cwd": str(self.project)}}).encode("utf-8")
        )
        self.marker = self.runtime / "delegated.marker"
        self.env = {
            **os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
            # keeps task-events' git from finding the repository the temp dir sits under
            "GIT_CEILING_DIRECTORIES": str(self.base),
        }
        for key in ("WORKFLOW_LOCAL_MODE", "CLAUDE_HOOK_RAW"):
            self.env.pop(key, None)

    def run(self, command: list[str], payload: dict) -> tuple[int, str, float]:
        started = time.perf_counter()
        done = subprocess.run(
            command, input=json.dumps(payload), text=True, encoding="utf-8", errors="replace",
            env=self.env, capture_output=True, timeout=90, cwd=str(self.project),
        )
        self.last_stderr = done.stderr
        return done.returncode, done.stdout, time.perf_counter() - started

    def close(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


def _case_session_bind(ws: _Workspace, run) -> None:
    code, out, _ = run({"source": "startup", "session_id": "c2", "cwd": str(ws.project), "hook_event_name": "SessionStart"})
    assert_true(code == 0, f"session-bind exits 0, got {code}")
    context = json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "") if out.strip() else ""
    assert_true("MAIN_SESSION_ID=" in context, f"session-bind injects MAIN_SESSION_ID: {out[:200]!r}")
    registry = json.loads(ws.registry.read_text(encoding="utf-8-sig"))
    assert_true("c2" in registry and registry["c2"].get("main_session_id"), f"session-bind records the session: {registry}")


def _silent(ws: _Workspace, out: str, hook: str) -> None:
    """Claude Code reads a hook's stdout (UserPromptSubmit injects it as context), so a hook
    whose contract prints nothing must print nothing, on stderr either."""
    assert_true(not out.strip(), f"{hook} prints nothing on stdout: {out[:200]!r}")
    assert_true(not ws.last_stderr.strip(), f"{hook} prints nothing on stderr: {ws.last_stderr[:200]!r}")


def _case_gate_set(ws: _Workspace, run) -> None:
    ws.marker.unlink(missing_ok=True)
    code, out, _ = run({"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "UserPromptSubmit", "prompt": "/.plan add a feature"})
    assert_true(code == 0 and ws.marker.is_file(), f"intent-gate-set exits 0 and arms the marker (exit {code})")
    _silent(ws, out, "intent-gate-set")
    code, out, _ = run({"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "UserPromptSubmit", "prompt": "terima kasih"})
    assert_true(code == 0, f"intent-gate-set never blocks a prompt, got {code}")
    _silent(ws, out, "intent-gate-set")


def _case_gate_check(ws: _Workspace, run) -> None:
    read = {"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_input": {"pattern": "x"}}
    ws.marker.write_text(json.dumps({"command": "plan"}), encoding="utf-8")
    code, out, _ = run(read)
    assert_true(code == 2, f"intent-gate-check blocks a gather while armed with exit 2, got {code}")
    assert_true(not out.strip(), f"a block carries nothing on stdout: {out[:200]!r}")
    # exit 2 hands stderr to the model as the reason; an empty one leaves it guessing
    assert_true(".workflow/run" in ws.last_stderr, f"a block names the runner on stderr: {ws.last_stderr[:300]!r}")
    ws.marker.unlink()
    code, out, _ = run(read)
    assert_true(code == 0, f"intent-gate-check passes once the marker is gone, got {code}")
    _silent(ws, out, "intent-gate-check")


def _case_task_events(ws: _Workspace, run) -> None:
    stream = ws.project / ".workflow" / "data" / "tasks.jsonl"
    stream.unlink(missing_ok=True)
    code, out, _ = run({"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "UserPromptSubmit", "prompt": "/.execute -y"})
    rows = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()] if stream.exists() else []
    assert_true(code == 0, f"task-events exits 0, got {code}")
    assert_true([(r.get("kind"), r.get("skill")) for r in rows] == [("skill", "execute")], f"task-events records the skill event: {rows}")
    _silent(ws, out, "task-events")


def _case_statusline(ws: _Workspace, run) -> None:
    code, out, _ = run({"session_id": "c1", "workspace": {"project_dir": str(ws.project)}})
    lines = [line for line in out.splitlines() if line.strip()]
    assert_true(code == 0 and len(lines) == 1, f"the statusline prints one line and exits 0: exit {code}, {out!r}")


def _case_graph_refresh(ws: _Workspace, run) -> None:
    code, out, _ = run({"last_assistant_message": "Halo, ini jawaban biasa.", "cwd": str(ws.project), "hook_event_name": "Stop"})
    assert_true(code == 0, f"graph-refresh exits 0, got {code}")
    _silent(ws, out, "graph-refresh")
    assert_true(not (ws.project / "graphify-out" / ".refresh.lock").exists(), "graph-refresh takes no lock for a turn that changed no code")


_CASES = {
    "session-bind": _case_session_bind,
    "intent-gate-set": _case_gate_set,
    "intent-gate-check": _case_gate_check,
    "task-events": _case_task_events,
    "workflow-statusline": _case_statusline,
    "graph-refresh": _case_graph_refresh,
}


def _test_hooks_run_through_their_launchers() -> None:
    # Fails closed: the bash launchers are what every OS will run, so a machine that cannot
    # run them has not tested the contract, and a green result there would be false.
    assert_true(
        _bash() is not None,
        "no bash to run the hook launchers (Windows: install Git for Windows; its bash is found next to git)",
    )
    timings: dict[tuple[str, str], list[float]] = {}
    for hook, case in _CASES.items():
        for label, command in _flavours(hook):
            ws = _Workspace(f"{hook}-{label}")
            try:
                def run(payload: dict, _command=command, _key=(hook, label)) -> tuple[int, str, float]:
                    code, out, seconds = ws.run(_command, payload)
                    timings.setdefault(_key, []).append(seconds)
                    return code, out, seconds

                try:
                    case(ws, run)
                except AssertionError as exc:
                    raise AssertionError(f"[{hook}.{label}] {exc}") from None
            finally:
                ws.close()
    missing = [hook for hook in _CASES if (hook, "sh") not in timings]
    assert_true(not missing, f"every hook ran through its bash launcher; never ran: {missing}")
    for (hook, label), seconds in sorted(timings.items()):
        worst = max(seconds)
        assert_true(
            worst < _TIMEOUT_S[hook] / 2,
            f"[{hook}.{label}] a call took {worst:.2f}s, over half the {_TIMEOUT_S[hook]}s hook timeout",
        )
    if os.environ.get("HOOK_CONTRACT_TIMINGS"):
        print("  hook latency (s, per call): " + json.dumps(
            {f"{h}.{l}": [round(s, 3) for s in v] for (h, l), v in sorted(timings.items())}
        ))


def _shim(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _test_hook_launchers_resolve_python() -> None:
    """The launcher picks an interpreter by running it (field note: `python` called where
    only `python3` existed, and the other way round). PATH holds only bash's own tools plus
    shims, so nothing on the real PATH answers in their place."""
    bash = _bash()
    assert_true(bash is not None, "no bash to run the hook launchers")
    real = Path(sys.executable).as_posix()
    hook = [bash, str(HOOKS / "session-bind.sh")]
    payload = lambda ws: {"source": "startup", "session_id": "c9", "cwd": str(ws.project), "hook_event_name": "SessionStart"}  # noqa: E731
    cases = [
        ("only `python`", {"python": f'exec "{real}" "$@"'}, True),
        ("only `python3`", {"python3": f'exec "{real}" "$@"'}, True),
        ("a python3 that does not run (Store alias), then python", {"python3": "exit 9009", "python": f'exec "{real}" "$@"'}, True),
        ("an old python3 and nothing else", {"python3": 'case "$*" in *version_info*) exit 1;; esac; exec "' + real + '" "$@"'}, False),
        ("no python at all", {}, False),
    ]
    for label, shims, runs in cases:
        ws = _Workspace("resolve")
        try:
            shim_dir = ws.base / "bin"
            shim_dir.mkdir()
            for name, body in shims.items():
                _shim(shim_dir, name, body)
            # The launcher needs `cat` and nothing else from PATH (the shims start with an
            # absolute #!/bin/sh). POSIX: one symlink in a private dir, because cat's own
            # directory (/usr/bin) also holds the system python3. Git Bash: its bin dirs,
            # which carry msys-2.0.dll beside cat and ship no python.
            if osutil.IS_WINDOWS:
                git_root = Path(bash).parents[1]
                tools = [git_root / "usr" / "bin", git_root / "bin", git_root / "mingw64" / "bin"]
            else:
                tool_dir = ws.base / "tools"
                tool_dir.mkdir()
                (tool_dir / "cat").symlink_to(shutil.which("cat"))
                tools = [tool_dir]
            leaks = [str(d / n) for d in tools for n in ("python3", "python", "py", "python3.exe", "python.exe", "py.exe") if (d / n).exists()]
            assert_true(not leaks, f"[{label}] the isolated PATH must hold no interpreter of its own: {leaks}")
            ws.env["PATH"] = os.pathsep.join([str(shim_dir), *map(str, tools)])
            code, out, _ = ws.run(hook, payload(ws))
            assert_true(code == 0, f"[{label}] session-bind exits 0, got {code}: {ws.last_stderr[-300:]!r}")
            if runs:
                assert_true("MAIN_SESSION_ID=" in out, f"[{label}] the launcher found an interpreter and bound the session: {out[:200]!r}")
            else:
                assert_true(not out.strip(), f"[{label}] with no usable interpreter the hook fails open, silently: {out[:200]!r}")
        finally:
            ws.close()


def _test_gate_k5_fixtures() -> None:
    bash = _bash()
    assert_true(bash is not None, "no bash to run the K5 gate fixtures through intent-gate-check.sh")
    cases = json.loads(K5.read_text(encoding="utf-8"))["cases"]
    ws = _Workspace("k5")
    pending, drifted = [], []
    try:
        for name in ("run", "check", "inspect"):
            (ws.project / ".workflow" / f"{name}.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            (ws.project / ".workflow" / f"{name}.ps1").write_text("\n", encoding="utf-8")
        root = ws.project.as_posix()
        for case in cases:
            command = case["command"].replace("{root}", root)
            ws.marker.write_text(json.dumps({"command": "plan"}), encoding="utf-8")
            payload = {
                "session_id": "c1", "cwd": str(ws.project), "hook_event_name": "PreToolUse",
                "tool_name": case["tool"], "tool_input": {"command": command},
            }
            code, _, _ = ws.run([bash, str(HOOKS / "intent-gate-check.sh")], payload)
            if code != case["now"]:
                drifted.append(f"{case['tool']} {case['command']!r}: exit {code}, frozen {case['now']} ({case['why']})")
            if case["target"] != case["now"]:
                pending.append(f"{case['tool']}: {case['why']}")
    finally:
        ws.close()
    assert_true(not drifted, "[K5 now] the gate no longer decides as frozen:\n    " + "\n    ".join(drifted))
    print(f"  K5: {len(cases) - len(pending)}/{len(cases)} cases already decide as the target gate; pending: {len(pending)}")
    for line in pending:
        print(f"    - {line}")
