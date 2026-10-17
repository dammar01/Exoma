"""Every shipped hook, run through its bash launcher as Claude Code would run it.

Stage 0 of the OS-agnostic migration (bash launchers on every OS, one Python dispatcher):
the contract is frozen on the real `.sh` files before any of them changes. The other hook
checks run the `.sh` flavour's embedded python, and the bash wrapper on POSIX only, so the
launcher itself (`cat`, `python3`, exit code) was never exercised on Windows — and Windows is
where it will now run. Each case asks one hook what the contract says it must do (exit code,
stdout, the file it writes) and times the call; where PowerShell exists the `.ps1` flavour
answers the same case, so the exit codes are pinned for both.

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

from tests.checks.support import assert_true, powershell_for_hooks

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
    """The bash Claude Code's hook command would reach. On Windows that is Git Bash: PATH
    there can also hold WSL's System32\\bash.exe and a WindowsApps alias, neither of which
    runs these scripts against Windows paths."""
    if sys.platform != "win32":
        return shutil.which("bash")
    git = shutil.which("git")
    if not git:
        return None
    # git.exe sits in <Git>\cmd, or <Git>\mingw64\bin when PATH comes from Git Bash itself.
    for root in Path(git).parents:
        for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


def _flavours(hook: str) -> list[tuple[str, list[str]]]:
    runners = []
    bash = _bash()
    if bash:
        runners.append(("sh", [bash, str(HOOKS / f"{hook}.sh")]))
    shell = powershell_for_hooks()
    if shell:
        runners.append(("ps1", [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HOOKS / f"{hook}.ps1")]))
    return runners


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
