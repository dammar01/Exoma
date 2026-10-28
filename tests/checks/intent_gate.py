"""The intent gate's SET side, run as shipped.

intent-gate-set once read the prompt from `user_prompt`, a field Claude Code never sends (it
sends `prompt`), so the runtime gate never armed. It must also leave alone every turn the user
did not write (DEC-045, DEC-059). Pinned on the real scripts: each hook's .py directly, and its
bash launcher as Claude Code runs it (Git Bash on Windows).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.checks.support import assert_true
from exoma.utils.osutil import find_bash
from tests.checks.support import no_console

HOOKS = Path(__file__).resolve().parents[2] / "src" / "exoma" / "_bundle" / "config" / "hosts" / "claude" / "hooks"


def _hook_runners(stem: str) -> list[tuple[str, list[str]]]:
    runners = [("py", [sys.executable, str(HOOKS / f"{stem}.py")])]
    bash = find_bash()
    if bash:
        runners.append(("sh", [bash, str(HOOKS / f"{stem}.sh")]))
    return runners


def _runners() -> list[tuple[str, list[str]]]:
    return _hook_runners("intent-gate-set")


def _test_intent_gate_reads_the_prompt_field() -> None:
    _check_prompt_field()
    _check_subagent_stays_gated()
    _check_verify_lane()
    _check_skill_read_lane()
    _check_precision_read_lane()


def _check_prompt_field() -> None:
    for label, command in _runners():
        base = Path(tempfile.mkdtemp(prefix=f"gate-set-{label.replace(':', '-')}-"))
        try:
            home, project = base / "home", base / "project"
            (home / ".claude").mkdir(parents=True)
            (project / ".workflow" / "data").mkdir(parents=True)
            registry = {"c1": {"main_session_id": "m1", "cwd": str(project)}}
            (home / ".claude" / "session_registry.json").write_text(json.dumps(registry), encoding="utf-8")
            payload = json.dumps({
                "session_id": "c1", "cwd": str(project),
                "hook_event_name": "UserPromptSubmit", "prompt": "/.plan add a feature",
            })
            env = {
                **os.environ, "HOME": str(home), "USERPROFILE": str(home),
                # What the bash wrapper hands its embedded python.
                "CLAUDE_HOOK_RAW": payload, "HOOK_DIR": str(HOOKS),
            }
            subprocess.run(command, input=payload, text=True, env=env, capture_output=True, timeout=60, **no_console())
            marker = project / ".workflow" / "data" / "sessions" / "m1" / "runtime" / "delegated.marker"
            assert_true(marker.is_file(), f"[{label}] a delegated prompt in Claude Code's `prompt` field arms the gate")

            # DEC-045: a background task's notification is not the user's prompt. One whose
            # text matches the NL map neither arms the gate nor clears the user's marker.
            def notify(text: str) -> None:
                note = json.dumps({"session_id": "c1", "cwd": str(project), "hook_event_name": "UserPromptSubmit", "prompt": text})
                subprocess.run(command, input=note, text=True, env={**env, "CLAUDE_HOOK_RAW": note}, capture_output=True, timeout=60, **no_console())

            notification = (
                "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n"
                "<summary>Background command \"Delegate analyze audit to second agent\" completed</summary>\n"
                "</task-notification>"
            )
            armed_by_user = marker.read_text(encoding="utf-8")
            notify(notification)
            assert_true(
                marker.is_file() and marker.read_text(encoding="utf-8") == armed_by_user,
                f"[{label}] a task notification leaves the user's marker as it was",
            )
            marker.unlink()
            notify(notification)
            assert_true(not marker.exists(), f"[{label}] a task notification matching the NL map does not arm the gate")
            notify("  " + notification)
            assert_true(not marker.exists(), f"[{label}] leading whitespace does not hide a task notification")
            notify("/.analyze kenapa <task-notification> muncul di log")
            assert_true(marker.is_file(), f"[{label}] a user prompt that merely mentions the tag still arms the gate")

            # DEC-068: a sentence that only resembles a delegated command arms nothing and
            # clears the marker the previous command left; it gets a hint instead.
            sentence = json.dumps({"session_id": "c1", "cwd": str(project), "hook_event_name": "UserPromptSubmit",
                                   "prompt": "saat saya analisa, ada titik dimana terlogout"})
            done = subprocess.run(command, input=sentence, text=True, env={**env, "CLAUDE_HOOK_RAW": sentence},
                                  capture_output=True, timeout=60, **no_console())
            assert_true(not marker.exists(), f"[{label}] an NL sentence does not arm the gate and clears the last marker")
            assert_true("[INTENT HINT]" in done.stdout and "/.analyze" in done.stdout,
                        f"[{label}] an NL sentence gets an intent hint: {done.stdout[:200]!r}")
            notify("/.analyze kenapa <task-notification> muncul di log")

            # DEC-059: the other turns the host writes, a subagent's hand-back among them,
            # armed the gate in the middle of an /.execute. Each fixture matches the NL map,
            # so passing here means the provenance check, not a missed pattern, held it back.
            def send(case: dict) -> None:
                body = {"session_id": "c1", "cwd": str(project), "hook_event_name": "UserPromptSubmit", "prompt": case["prompt"]}
                body.update(case.get("payload") or {})
                raw = json.dumps(body)
                subprocess.run(command, input=raw, text=True, env={**env, "CLAUDE_HOOK_RAW": raw}, capture_output=True, timeout=60, **no_console())

            for case in _HOST_TURNS["host"]:
                assert_true(_matches_map(case["prompt"]), f"fixture `{case['name']}` must match the NL map to test anything")
                armed_by_user = marker.read_text(encoding="utf-8")
                send(case)
                assert_true(
                    marker.is_file() and marker.read_text(encoding="utf-8") == armed_by_user,
                    f"[{label}] `{case['name']}` leaves the user's marker as it was",
                )
                marker.unlink()
                send(case)
                assert_true(not marker.exists(), f"[{label}] `{case['name']}` does not arm the gate")
                notify("/.analyze kenapa gate ini memblokir")
            for case in _HOST_TURNS["user"]:
                marker.unlink()
                send(case)
                assert_true(marker.is_file(), f"[{label}] `{case['name']}` is the user's own prompt and arms the gate")
            recorded = json.loads(marker.read_text(encoding="utf-8"))
            assert_true(
                {"source", "has_agent_id", "prompt_id"} <= recorded.keys(),
                f"[{label}] the marker records the prompt's provenance: {recorded}",
            )
        finally:
            shutil.rmtree(base, ignore_errors=True)


_HOST_TURNS = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "hooks" / "host_turns.json").read_text(encoding="utf-8")
)


def _matches_map(prompt: str) -> bool:
    import re

    mp = json.loads((HOOKS / "intent-map.json").read_text(encoding="utf-8"))
    if mp.get("prefix_regex") and re.search(mp["prefix_regex"], prompt):
        return True
    return any(
        re.search(pat, prompt, re.IGNORECASE)
        for cmd in mp.get("delegated", [])
        for pat in mp.get("patterns", {}).get(cmd, [])
    )


def _check_subagent_stays_gated() -> None:
    """DEC-059 keeps a real marker binding on subagents: a native subagent is not the
    default evidence path, so a gather call that carries `agent_id` is refused like the
    main agent's. Only false arming was fixed, not the gate's reach."""
    for label, command in _check_runners():
        base = Path(tempfile.mkdtemp(prefix=f"gate-sub-{label.replace(':', '-')}-"))
        try:
            home, project = base / "home", base / "project"
            (home / ".claude").mkdir(parents=True)
            runtime = project / ".workflow" / "data" / "sessions" / "m1" / "runtime"
            runtime.mkdir(parents=True)
            registry = {"c1": {"main_session_id": "m1", "cwd": str(project)}}
            (home / ".claude" / "session_registry.json").write_text(json.dumps(registry), encoding="utf-8")
            (runtime / "delegated.marker").write_text(json.dumps({"command": "analyze"}), encoding="utf-8")
            env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
            env.pop("WORKFLOW_LOCAL_MODE", None)
            payload = json.dumps({
                "session_id": "c1", "cwd": str(project), "hook_event_name": "PreToolUse",
                "agent_id": "a0000000000000003", "agent_type": "general-purpose",
                "tool_name": "Bash", "tool_input": {"command": "grep -rn gate ."},
            })
            done = subprocess.run(
                command, input=payload, text=True, env={**env, "CLAUDE_HOOK_RAW": payload},
                capture_output=True, timeout=60,
                **no_console(),
            )
            assert_true(done.returncode == 2, f"[{label}] a subagent's gather call stays gated by a real marker: {done.returncode}")
        finally:
            shutil.rmtree(base, ignore_errors=True)


def _check_runners() -> list[tuple[str, list[str]]]:
    return _hook_runners("intent-gate-check")


def _check_verify_lane() -> None:
    """A pending verify opens exactly what skills/verify.md needs before the run.

    The skill has main_agent pick tests from the diff and read the test allowlist before
    `.workflow/run verify`, while the gate blocks Read and Bash: without this lane the step
    the skill demands was the one the gate refused. The lane is the diff's file names, the
    config, and this session's tests.json; a pipe, a patch, or a source file stays blocked,
    and no other delegated command gets the lane.
    """
    for label, command in _check_runners():
        base = Path(tempfile.mkdtemp(prefix=f"gate-check-{label.replace(':', '-')}-"))
        try:
            home, project = base / "home", base / "project"
            (home / ".claude").mkdir(parents=True)
            session = project / ".workflow" / "data" / "sessions" / "m1"
            (session / "runtime").mkdir(parents=True)
            registry = {"c1": {"main_session_id": "m1", "cwd": str(project)}}
            (home / ".claude" / "session_registry.json").write_text(json.dumps(registry), encoding="utf-8")
            env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
            env.pop("WORKFLOW_LOCAL_MODE", None)
            tests_json = session / "verify" / "tests.json"
            config = project / ".workflow" / "config.json"
            other_tests = project / ".workflow" / "data" / "sessions" / "m2" / "verify" / "tests.json"

            def exit_code(tool: str, tool_input: dict) -> int:
                payload = json.dumps({
                    "session_id": "c1", "cwd": str(project), "hook_event_name": "PreToolUse",
                    "tool_name": tool, "tool_input": tool_input,
                })
                done = subprocess.run(
                    command, input=payload, text=True, env={**env, "CLAUDE_HOOK_RAW": payload},
                    capture_output=True, timeout=60,
                    **no_console(),
                )
                return done.returncode

            def arm(intent: str) -> None:
                (session / "runtime" / "delegated.marker").write_text(json.dumps({"command": intent}), encoding="utf-8")

            arm("verify")
            allowed = [
                ("Bash", {"command": "git diff --name-only"}),
                ("Bash", {"command": "git diff --name-only HEAD"}),
                ("Bash", {"command": "git diff --stat HEAD~1 -- src"}),
                ("Bash", {"command": "git diff --cached --name-status"}),
                ("Bash", {"command": "bash .workflow/run.sh verify 't' 'm1'"}),
                ("Read", {"file_path": str(config)}),
                ("Read", {"file_path": ".workflow/config.json"}),
                ("Read", {"file_path": str(tests_json)}),
                ("Write", {"file_path": str(tests_json), "content": "{}"}),
                ("PowerShell", {"command": "git diff --name-only HEAD"}),
            ]
            for tool, tool_input in allowed:
                code = exit_code(tool, tool_input)
                assert_true(code == 0, f"[{label}] verify lane must allow {tool} {tool_input}; exit {code}")
            blocked = [
                ("Bash", {"command": "git diff --name-only | cat"}),
                ("Bash", {"command": "git diff --name-only && cat src/app.py"}),
                ("Bash", {"command": "git diff --stat > out.txt"}),
                ("Bash", {"command": "git diff --stat -p"}),
                ("Bash", {"command": "git diff --name-only --output=out.txt"}),
                ("Bash", {"command": "git diff HEAD"}),
                ("Bash", {"command": "git show HEAD"}),
                ("Bash", {"command": "cat .workflow/config.json"}),
                ("Read", {"file_path": str(project / "src" / "app.py")}),
                ("Read", {"file_path": str(other_tests)}),
                ("Grep", {"pattern": "x"}),
                ("PowerShell", {"command": "git diff --name-only | Out-String"}),
                ("PowerShell", {"command": "git diff HEAD"}),
            ]
            for tool, tool_input in blocked:
                code = exit_code(tool, tool_input)
                assert_true(code == 2, f"[{label}] verify lane must still block {tool} {tool_input}; exit {code}")

            # Another delegated intent gets no lane.
            arm("plan")
            for tool, tool_input in [("Bash", {"command": "git diff --name-only"}), ("Read", {"file_path": str(config)})]:
                code = exit_code(tool, tool_input)
                assert_true(code == 2, f"[{label}] a pending plan must block {tool} {tool_input}; exit {code}")

            # The runner passes only as the command itself, never as a word inside another one.
            runner_calls = [
                "bash .workflow/run.sh plan 't' 'm1'",
                "./.workflow/run.sh plan 't' 'm1'",
                f"bash {project.as_posix()}/.workflow/run.sh plan 't' 'm1'",
                "bash .workflow/inspect.sh",
            ]
            for runner_call in runner_calls:
                code = exit_code("Bash", {"command": runner_call})
                assert_true(code == 0, f"[{label}] a runner call must pass the gate: {runner_call}; exit {code}")
            smuggled = [
                "python -c \"open('secret').read()\" /tmp/.workflow/run.sh",
                "cat src/app.py .workflow/run.sh",
                "rg secret .workflow/check.sh",
                "powershell -Command Get-Content secret .workflow/run.sh",
                ".workflow/run.sh.bak plan",
                # retired: the .ps1 runners pass no more, however they are called
                ".workflow/run.ps1 plan 't' 'm1'",
                "powershell -NoProfile -ExecutionPolicy Bypass -File .workflow/run.ps1 plan 't' 'm1'",
            ]
            for command_line in smuggled:
                code = exit_code("Bash", {"command": command_line})
                assert_true(code == 2, f"[{label}] a runner path inside another command must block: {command_line}; exit {code}")
        finally:
            shutil.rmtree(base, ignore_errors=True)


def _check_skill_read_lane() -> None:
    """Under any pending marker a Read of a skill definition passes, and nothing near it.

    The Skill tool tells the agent to read ~/.claude/skills/<name>.md, while the prompt
    that invoked it already armed the gate: the instruction saying how to dispatch was
    blocked until after the dispatch. The lane is an existing .md whose real path lies in
    ~/.claude/skills; `..`, a relative path, another extension, a missing file, and a link
    that leaves the folder stay blocked.
    """
    for label, command in _check_runners():
        gate = _ArmedGate(label, command)
        try:
            skills = gate.home / ".claude" / "skills"
            (skills / "pkg").mkdir(parents=True)
            (skills / "analyze.md").write_text("# Skill: analyze\n", encoding="utf-8")
            (skills / "pkg" / "SKILL.md").write_text("# nested\n", encoding="utf-8")
            (skills / "notes.txt").write_text("x\n", encoding="utf-8")
            (gate.home / ".claude" / "settings.md").write_text("x\n", encoding="utf-8")
            (gate.project / "secret.md").write_text("x\n", encoding="utf-8")
            for intent in ("plan", "analyze", "verify"):
                gate.arm(intent)
                gate.expect("Read", {"file_path": str(skills / "analyze.md")}, 0, f"a pending {intent} lets a skill be read")
                gate.expect("Read", {"file_path": str(skills / "pkg" / "SKILL.md")}, 0, "a skill in a subfolder")
            gate.arm("plan")
            blocked = [
                (str(skills / "notes.txt"), "a file that is not .md"),
                (str(skills / "missing.md"), "a skill that does not exist"),
                (str(gate.home / ".claude" / "settings.md"), "a .md outside the skills folder"),
                (str(skills / ".." / "settings.md"), "a path stepping out with .."),
                (os.path.join(".claude", "skills", "analyze.md"), "a relative path"),
                (str(gate.project / "secret.md"), "a project file"),
            ]
            for path, why in blocked:
                gate.expect("Read", {"file_path": path}, 2, f"{why} stays blocked")
            gate.expect("Grep", {"pattern": "x", "path": str(skills)}, 2, "a Grep over the skills folder stays blocked")
            try:
                os.symlink(gate.project / "secret.md", skills / "out.md")
                os.symlink(skills / "analyze.md", skills / "in.md")
            except (OSError, NotImplementedError):
                print(f"  note: [{label}] no symlink privilege; the link cases of the skill-read lane are not exercised")
            else:
                gate.expect("Read", {"file_path": str(skills / "out.md")}, 2, "a link leaving the skills folder stays blocked")
                gate.expect("Read", {"file_path": str(skills / "in.md")}, 0, "a link inside the skills folder passes")
            gate.disarm()
            gate.expect("Read", {"file_path": str(gate.project / "secret.md")}, 0, "with no marker every Read passes")
        finally:
            gate.close()


def _check_precision_read_lane() -> None:
    """DEC-043: under any marker the main agent may read a small slice itself, as CLAUDE.md
    allows for file:line attribution: a project file outside .workflow, at most 200 lines
    (`limit`, or a file that short), two per marker. A third, a longer span, a whole large
    file, a file outside the project, and Grep stay blocked; a new marker starts a new count.
    """
    for label, command in _check_runners():
        gate = _ArmedGate(label, command)
        try:
            src = gate.project / "src"
            src.mkdir()
            big = src / "big.tsx"
            big.write_text("\n".join(f"line {n}" for n in range(500)) + "\n", encoding="utf-8")
            small = src / "small.py"
            small.write_text("\n".join(f"x{n} = {n}" for n in range(200)) + "\n", encoding="utf-8")
            (gate.project / ".workflow" / "config.json").write_text("{}", encoding="utf-8")
            outside = gate.base / "outside.py"
            outside.write_text("x = 1\n", encoding="utf-8")

            gate.arm("plan", set_at="2026-10-04T00:00:00+00:00")
            for tool_input, why in (
                ({"file_path": str(big), "offset": 1, "limit": 201}, "a span over 200 lines"),
                ({"file_path": str(big)}, "a whole file over 200 lines"),
                ({"file_path": str(outside), "limit": 10}, "a file outside the project"),
                ({"file_path": str(gate.project / ".workflow" / "config.json"), "limit": 5}, "a .workflow file"),
                ({"file_path": str(src / "missing.py"), "limit": 5}, "a file that does not exist"),
                ({"file_path": str(big), "limit": "50"}, "a limit that is not a number"),
            ):
                gate.expect("Read", tool_input, 2, f"{why} is not a precision read")
            gate.expect("Read", {"file_path": str(big), "offset": 100, "limit": 200}, 0, "a 200-line slice of a large file passes")
            gate.expect("Read", {"file_path": str(small)}, 0, "a 200-line file read whole passes")
            gate.expect("Read", {"file_path": str(big), "offset": 1, "limit": 10}, 2, "a third precision read is over the quota")
            gate.expect("Grep", {"pattern": "line", "path": str(src)}, 2, "Grep stays blocked")

            gate.arm("plan", set_at="2026-10-04T00:05:00+00:00")
            gate.expect("Read", {"file_path": os.path.join("src", "small.py"), "limit": 20}, 0, "a new marker starts a new count; a relative path resolves to the project")
            gate.arm("plan")
            gate.expect("Read", {"file_path": str(small), "limit": 20}, 2, "a marker without set_at keys no count, so no precision read")
        finally:
            gate.close()


class _ArmedGate:
    """A throwaway home + project with the gate armed, and one way to run the hook."""

    def __init__(self, label: str, command: list[str], root_name: str = "project") -> None:
        self.label, self.command = label, command
        self.base = Path(tempfile.mkdtemp(prefix=f"gate-arm-{label.replace(':', '-')}-"))
        self.home, self.project = self.base / "home", self.base / root_name
        self.foreign = self.base / "other"
        (self.home / ".claude").mkdir(parents=True)
        self.runtime = self.project / ".workflow" / "data" / "sessions" / "m1" / "runtime"
        self.runtime.mkdir(parents=True)
        (self.foreign / ".workflow").mkdir(parents=True)
        registry = {"c1": {"main_session_id": "m1", "cwd": str(self.project)}}
        # session-bind writes UTF-8 without a BOM; so does this.
        (self.home / ".claude" / "session_registry.json").write_bytes(json.dumps(registry, ensure_ascii=False).encode("utf-8"))
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)}
        self.env.pop("WORKFLOW_LOCAL_MODE", None)

    def arm(self, intent: str = "plan", set_at: str | None = None) -> None:
        """Without `set_at` the marker keys no precision-read count, so that lane stays shut
        and the other lanes are tested alone; intent-gate-set always writes one."""
        marker = {"command": intent}
        if set_at is not None:
            marker["set_at"] = set_at
        (self.runtime / "delegated.marker").write_text(json.dumps(marker), encoding="utf-8")

    def disarm(self) -> None:
        (self.runtime / "delegated.marker").unlink(missing_ok=True)

    def exit_code(self, tool: str, tool_input: dict) -> int:
        payload = json.dumps({
            "session_id": "c1", "cwd": str(self.project), "hook_event_name": "PreToolUse",
            "tool_name": tool, "tool_input": tool_input,
        })
        done = subprocess.run(
            self.command, input=payload, text=True, encoding="utf-8", errors="replace",
            env={**self.env, "CLAUDE_HOOK_RAW": payload}, capture_output=True, timeout=60,
            **no_console(),
        )
        return done.returncode

    def expect(self, tool: str, tool_input: dict, code: int, why: str) -> None:
        got = self.exit_code(tool, tool_input)
        assert_true(got == code, f"[{self.label}] {why}: {tool} {tool_input}; exit {got}, expected {code}")

    def close(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


def _flavours() -> list[tuple[str, list[str]]]:
    runners = _check_runners()
    if "sh" not in {label for label, _ in runners}:
        print("  note: no bash found; intent-gate-check runs only through its .py, not its launcher")
    return runners


def _test_intent_gate_runner_parsed() -> None:
    """The runner allowlist is a parse, not a pattern, and is anchored to the project root.

    The regex let any directory sit in front of `.workflow/run.sh` and let an interpreter
    flag such as `-c`/`-Command` hand the rest of the line to PowerShell, and the PowerShell
    tool was never gated at all. One tokeniser now reads both tools (K5): words bare or in
    single quotes, which both shells take literally, so every case below is asked of the Bash
    and the PowerShell tool alike, through the .py and its launcher, with one verdict.
    """
    for label, command in _flavours():
        gate = _ArmedGate(label, command)
        try:
            gate.arm("plan")
            posix_root = gate.project.as_posix()
            foreign = gate.foreign.as_posix()
            allowed = [
                f"bash {posix_root}/.workflow/run.sh analyze 'task (x) kenapa' 'm1'",
                "bash .workflow/run.sh plan 't' 'm1'",
                "./.workflow/run.sh plan 't (x)' 'm1'",
                "bash .workflow/run.sh plan 'rancang X; lalu cek $(y) | z > w' 'm1'",
                "bash .workflow/check.sh job_1 --wait",
                "bash .workflow/inspect.sh",
                ".workflow/sub/../run.sh plan",
                "bash .workflow/run.sh plan HEAD~1",
            ]
            refused = [
                # a gather, plain
                "cat README.md",
                "Get-Content README.md",
                "Get-ChildItem -Recurse",
                # anything outside single quotes that either shell would act on
                "bash .workflow/run.sh plan \"x $(cat README.md)\" 'm1'",
                "bash .workflow/run.sh plan \"x $HOME\" 'm1'",
                "bash .workflow/run.sh plan \"$env:USERPROFILE\"",
                "bash .workflow/run.sh plan $x",
                "bash .workflow/run.sh plan 't'; cat README.md",
                "bash .workflow/run.sh plan | cat",
                "bash .workflow/run.sh plan > out.txt",
                "bash .workflow/run.sh plan & cat secret",
                "bash .workflow/run.sh plan `id`",
                "bash .workflow/run.sh (Get-Content README.md -Raw)",
                "bash .workflow/run.sh @{x=(gc y)}",
                "bash .workflow/run.sh -Task:x",
                "bash .workflow/run.sh plan\ncat README.md",
                "bash .workflow/run.sh 'plan",
                "bash .workflow/run.sh pl'a'n",
                "bash .workflow/run.sh ~/x",
                "bash .workflow/run.sh “plan”",
                # the runner must be the command itself, anchored to this project
                f"bash {foreign}/.workflow/run.sh plan",
                ".workflow/runx.sh plan",
                "../.workflow/run.sh plan",
                ".workflow/../../other/.workflow/run.sh plan",
                "bash -c .workflow/run.sh",
                "bash -x .workflow/run.sh plan",
                "FOO=1 .workflow/run.sh plan",
                "& .workflow/run.sh plan",
                ". .workflow/run.sh plan",
                "python x .workflow/run.sh",
                # the retired .ps1 runners, however they are called
                ".workflow/run.ps1 plan 't' 'm1'",
                "& .workflow/run.ps1 plan",
                f"{posix_root}/.workflow/run.ps1 plan",
                "powershell -NoProfile -ExecutionPolicy Bypass -File .workflow/run.ps1 plan",
                "pwsh -File .workflow/check.ps1",
            ]
            for tool in ("Bash", "PowerShell"):
                for line in allowed:
                    gate.expect(tool, {"command": line}, 0, "a plain runner call must pass")
                for line in refused:
                    gate.expect(tool, {"command": line}, 2, "a line that is not one plain anchored runner call must block")
        finally:
            gate.close()


def _test_intent_gate_runner_tamper() -> None:
    """While the gate is armed, the runner scripts cannot be rewritten; other writes pass.

    The runner is the one command the gate lets through, so a Write of run.sh followed by
    running it would have been an arbitrary command. Writes elsewhere are not gather and are
    not the gate's business (verify writes tests.json, verify-browser its request draft).
    """
    for label, command in _flavours():
        gate = _ArmedGate(label, command)
        try:
            wf = gate.project / ".workflow"
            gate.arm("plan")
            refused = [
                ("Write", {"file_path": str(wf / "run.sh"), "content": "cat secret"}),
                ("Write", {"file_path": str(wf / "sub" / ".." / "run.ps1"), "content": "x"}),
                ("Edit", {"file_path": ".workflow/run.ps1", "old_string": "a", "new_string": "b"}),
                ("MultiEdit", {"file_path": str(wf / "check.ps1"), "edits": []}),
                ("NotebookEdit", {"notebook_path": str(wf / "inspect.sh"), "new_source": "x"}),
                ("Write", {"file_path": str(wf / "RUN.PS1"), "content": "x"}),
            ]
            for tool, tool_input in refused:
                gate.expect(tool, tool_input, 2, "a write of a runner script must block while armed")
            allowed = [
                ("Write", {"file_path": str(gate.project / "src" / "app.py"), "content": "x"}),
                ("Write", {"file_path": str(wf / "config.json"), "content": "{}"}),
                ("Edit", {"file_path": str(wf / "runner-notes.md"), "old_string": "a", "new_string": "b"}),
                ("Write", {"file_path": str(gate.foreign / ".workflow" / "run.sh"), "content": "x"}),
            ]
            for tool, tool_input in allowed:
                gate.expect(tool, tool_input, 0, "a write that is not a runner script must pass")
            gate.disarm()
            # The runner is never written by hand: a runner rewritten between delegations
            # would pass the next armed gate as an arbitrary command.
            gate.expect("Write", {"file_path": str(wf / "run.sh"), "content": "x"}, 2, "with no marker the runner is still refused")
            gate.expect("Write", {"file_path": str(gate.project / "src" / "app.py"), "content": "x"}, 0,
                        "with no marker other writes pass")
        finally:
            gate.close()


def _test_intent_gate_non_ascii_root() -> None:
    """A project root with non-ASCII characters still resolves through the registry.

    session-bind writes the registry as UTF-8 without a BOM; Windows PowerShell 5.1 read it
    as ANSI (`proyék` -> `proyÃ©k`), resolved a root that does not exist, found no marker and
    let everything through. Both hooks, run both ways.
    """
    for label, command in _flavours():
        gate = _ArmedGate(label, command, root_name="proyék")
        try:
            gate.arm("plan")
            gate.expect("Grep", {"pattern": "x"}, 2, "a non-ASCII root must still arm the check")
            gate.expect("Bash", {"command": "bash .workflow/run.sh plan 't' 'm1'"}, 0, "the runner under a non-ASCII root must pass")
            gate.expect("Bash", {"command": f"bash {gate.project.as_posix()}/.workflow/run.sh plan 't' 'm1'"}, 0, "an absolute runner path through a non-ASCII root must pass")
        finally:
            gate.close()
    for label, command in _runners():
        base = Path(tempfile.mkdtemp(prefix=f"gate-set-{label.replace(':', '-')}-"))
        try:
            home, project = base / "home", base / "proyék"
            (home / ".claude").mkdir(parents=True)
            (project / ".workflow" / "data").mkdir(parents=True)
            registry = {"c1": {"main_session_id": "m1", "cwd": str(project)}}
            (home / ".claude" / "session_registry.json").write_bytes(json.dumps(registry, ensure_ascii=False).encode("utf-8"))
            payload = json.dumps({
                "session_id": "c1", "cwd": str(project),
                "hook_event_name": "UserPromptSubmit", "prompt": "/.plan add a feature",
            })
            env = {
                **os.environ, "HOME": str(home), "USERPROFILE": str(home),
                "CLAUDE_HOOK_RAW": payload, "HOOK_DIR": str(HOOKS),
            }
            subprocess.run(command, input=payload, text=True, encoding="utf-8", errors="replace", env=env, capture_output=True, timeout=60, **no_console())
            marker = project / ".workflow" / "data" / "sessions" / "m1" / "runtime" / "delegated.marker"
            assert_true(marker.is_file(), f"[{label}] intent-gate-set must arm the gate under a non-ASCII root")
        finally:
            shutil.rmtree(base, ignore_errors=True)
