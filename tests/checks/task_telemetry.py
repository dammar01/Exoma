"""Tasks are sequences of skill calls, closed by a verify the runtime derives as pass (DEC-015,
DEC-020); a commit joins the task it belongs to and closes nothing."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from exoma.core.audit import task_telemetry
from exoma.core.audit.task_telemetry import derive_tasks

from tests.checks.support import assert_true
from exoma.utils.osutil import find_bash
from tests.checks.support import no_console

HOOKS = Path(__file__).resolve().parents[2] / "src" / "exoma" / "_bundle" / "config" / "hosts" / "claude" / "hooks"


def _skill(sid, skill, verdict=None, derived=None):
    return {"kind": "skill", "session_id": sid, "skill": skill, "verdict": verdict, "derived": derived, "at": "t"}


def _edit(sid, path):
    return {"kind": "edit", "session_id": sid, "path": path, "at": "t"}


def _commit(sid, commit, *paths):
    return {"kind": "commit", "session_id": sid, "commit": commit, "paths": list(paths), "at": "t"}


def _check_states() -> None:
    passed = _skill("s", "verify", "DONE", "pass")
    cases = {
        "completed": [_skill("s", "plan"), _edit("s", "a.py"), passed],
        "completed without a commit": [_edit("s", "a.py"), passed],
        "DONE derived incomplete": [_edit("s", "a.py"), _skill("s", "verify", "DONE", "incomplete")],
        "NEEDS FIX": [_edit("s", "a.py"), _skill("s", "verify", "NEEDS FIX", "fail")],
        "committed, never verified": [_edit("s", "a.py"), _commit("s", "c1", "a.py")],
        "DONE before the derived verdict": [_edit("s", "a.py"), _skill("s", "verify", "DONE")],
        "read_only": [_skill("s", "analyze")],
        "read-only pass": [_skill("s", "verify", "DONE", "pass")],
        "unknown": [_edit(None, "a.py")],
    }
    expected = {
        "completed without a commit": "completed",
        "DONE derived incomplete": "open",
        "NEEDS FIX": "open",
        "committed, never verified": "open",
        "DONE before the derived verdict": "open",
        "read-only pass": "read_only",
    }
    for name, events in cases.items():
        tasks, _ = derive_tasks(events)
        want = expected.get(name, name)
        assert_true(len(tasks) == 1 and tasks[0]["state"] == want, f"[{name}] expected {want}: {tasks}")

    # The commit recommended after a pass joins the task it closed; it closes nothing itself.
    tasks, unclaimed = derive_tasks([_skill("s", "plan"), _edit("s", "a.py"), passed, _commit("s", "c1", "a.py")])
    assert_true(
        len(tasks) == 1 and tasks[0]["state"] == "completed" and unclaimed == 0
        and tasks[0]["sequence"] == ["plan", "edit", "verify", "commit"] and tasks[0]["commit"] == "c1",
        f"a task keeps its ordered sequence, edits collapsed, the commit after its pass included: {tasks}",
    )

    # An edit after a pass is the next task; an unrelated commit is unclaimed; the same
    # commit seen twice counts once; sessions never share a task.
    tasks, unclaimed = derive_tasks([
        _edit("s", "a.py"), _commit("s", "c0", "other.py"), passed,
        _commit("s", "c1", "a.py"), _commit("s", "c1", "a.py"),
        _edit("s", "b.py"), _edit("t", "b.py"),
    ])
    assert_true(
        [t["state"] for t in tasks] == ["completed", "open", "open"] and unclaimed == 1,
        f"tasks split at a pass, commits attributed by edited path, per session, once per hash: {[t['state'] for t in tasks]} unclaimed={unclaimed}",
    )

    # A commit matching both the open task and the one just completed joins the open one;
    # a commit without a session is never claimed.
    tasks, unclaimed = derive_tasks([
        _edit("s", "a.py"), passed, _edit("s", "a.py"), _commit("s", "c1", "a.py"), _commit(None, "c2", "a.py"),
    ])
    assert_true(
        [t["state"] for t in tasks] == ["completed", "open"] and tasks[0]["commit"] is None
        and tasks[1]["commit"] == "c1" and unclaimed == 1,
        f"the open task claims a shared commit first; a sessionless commit is unclaimed: {tasks} unclaimed={unclaimed}",
    )

    # Two sessions in one project both record the same commit (each turn saw HEAD move);
    # the copy from the session that did not make it arrives first. It must not hide the
    # commit from the session that did, and a claimed commit is not counted unclaimed.
    tasks, unclaimed = derive_tasks([
        _edit("a", "a.py"), _skill("b", "analyze"),
        _commit("b", "c1", "a.py"), _commit("a", "c1", "a.py"),
        _commit("b", "c2", "z.py"), _commit("a", "c2", "z.py"),
    ])
    owner = next(t for t in tasks if t["session_id"] == "a")
    assert_true(owner["commit"] == "c1" and owner["sequence"][-1] == "commit" and unclaimed == 1,
                f"session A keeps its commit when B's copy comes first; a commit no copy claimed counts once: {tasks} unclaimed={unclaimed}")


def _check_runtime_event() -> None:
    root = Path(tempfile.mkdtemp(prefix="task-events-"))
    try:
        (root / ".workflow" / "data").mkdir(parents=True)
        task_telemetry.record_skill(root, "s1", "verify", verdict="DONE", derived="incomplete", next_action="/.commit", prompt_id="p1")
        (root / ".workflow" / "data" / "tasks.jsonl").open("a", encoding="utf-8").write("{torn\n")
        events = task_telemetry.load_events(root)
        assert_true(
            len(events) == 1 and events[0]["source"] == "runtime" and events[0]["verdict"] == "DONE"
            and events[0]["derived"] == "incomplete",
            f"the runtime writes one event per command with both verdicts; a torn line is skipped: {events}",
        )
        summary = task_telemetry.report(root)
        assert_true(summary["by_state"] == {"read_only": 1}, f"report reads the stream: {summary}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _runners() -> list[tuple[str, list[str]]]:
    """task-events.py directly, and the bash launcher Claude Code runs (Git Bash on Windows)."""
    runners = [("py", [sys.executable, str(HOOKS / "task-events.py")])]
    bash = find_bash()
    if bash:
        runners.append(("sh", [bash, str(HOOKS / "task-events.sh")]))
    return runners


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, **no_console())


def _check_hook_flavours() -> None:
    """The hook and its launcher turn the same hook events into the same task events (event v2).

    A skill comes from the prompt or the Skill tool, never from a Read; a commit is a HEAD
    that moved between two turn boundaries, recorded once, whatever Bash calls ran.
    """
    base = Path(tempfile.mkdtemp(prefix="task-hook-"))
    try:
        home = base / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "session_registry.json").write_text(
            json.dumps({"claude-1": {"main_session_id": "main_x"}}), encoding="utf-8"
        )
        skills = home / ".claude" / "skills"
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
        results = {}
        for name, argv in _runners():
            project = base / name.replace(":", "-")
            (project / ".workflow" / "data").mkdir(parents=True)
            (project / "src").mkdir()
            _git(project, "init", "-q")
            _git(project, "config", "user.email", "t@example.test")
            _git(project, "config", "user.name", "t")
            (project / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
            _git(project, "add", ".")
            _git(project, "commit", "-q", "-m", "a")
            def commit_b():
                (project / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
                _git(project, "commit", "-q", "-am", "b")

            def commit_c():
                (project / "src" / "c.py").write_text("y = 1\n", encoding="utf-8")
                _git(project, "add", "src")
                _git(project, "commit", "-q", "-m", "c")

            # (event, tool, input, prompt, what happens after the hook ran)
            calls = [
                # The session's first event snapshots HEAD; /.execute is a local skill.
                ("UserPromptSubmit", None, None, "/.execute -y", None),
                # Delegated commands are the runtime's; prose is no command.
                ("UserPromptSubmit", None, None, "/.plan something", None),
                ("UserPromptSubmit", None, None, "tolong /.commit", None),
                # A Read is not a skill load any more, a skill file included.
                ("PostToolUse", "Read", {"file_path": str(skills / "verify.md")}, None, None),
                ("PostToolUse", "Skill", {"skill": ".commit"}, None, None),
                ("PostToolUse", "Skill", {"skill": "caveman:caveman"}, None, None),
                ("PostToolUse", "Edit", {"file_path": str(project / "src" / "a.py")}, None, None),
                ("PostToolUse", "Write", {"file_path": str(project / ".workflow" / "config.json")}, None, None),
                ("PostToolUse", "Write", {"file_path": str(base / "outside.py")}, None, None),
                ("PostToolUse", "Bash", {"command": "git commit -m b"}, None, commit_b),
                # The turn ends: the commit made during it is recorded, once.
                ("Stop", None, None, None, None),
                ("Stop", None, None, None, commit_c),
                # A commit made between turns is recorded when the next prompt arrives.
                ("UserPromptSubmit", None, None, "lanjut", None),
                ("Stop", None, None, None, None),
            ]
            for event, tool, tool_input, prompt, after in calls:
                payload = {"session_id": "claude-1", "cwd": str(project), "hook_event_name": event}
                if tool:
                    payload.update({"tool_name": tool, "tool_input": tool_input})
                if prompt is not None:
                    payload["prompt"] = prompt
                raw = json.dumps(payload)
                # The .sh wrapper hands its stdin to the embedded python as CLAUDE_HOOK_RAW.
                subprocess.run(argv, input=raw, text=True, env={**env, "CLAUDE_HOOK_RAW": raw}, capture_output=True, timeout=60, **no_console())
                if after:
                    after()
            stream = project / ".workflow" / "data" / "tasks.jsonl"
            rows = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()] if stream.exists() else []
            for row in rows:
                row.pop("at", None)
                if row.get("kind") == "commit":
                    row["commit"] = len(row["commit"])
            results[name] = rows
        expected = [
            {"v": 2, "source": "hook", "session_id": "main_x", "kind": "skill", "skill": "execute"},
            {"v": 2, "source": "hook", "session_id": "main_x", "kind": "skill", "skill": "commit"},
            {"v": 2, "source": "hook", "session_id": "main_x", "kind": "edit", "path": "src/a.py"},
            {"v": 2, "source": "hook", "session_id": "main_x", "kind": "commit", "commit": 40, "paths": ["src/a.py"]},
            {"v": 2, "source": "hook", "session_id": "main_x", "kind": "commit", "commit": 40, "paths": ["src/c.py"]},
        ]
        for name, rows in results.items():
            assert_true(rows == expected, f"[{name}] hook events:\n{rows}\nexpected:\n{expected}")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _check_without_git() -> None:
    """A project that is no git repository still gets its skill events.

    Under Windows PowerShell 5.1 a native command's stderr is an error record, and with
    $ErrorActionPreference='Stop' git's "fatal: not a git repository" ended the hook before
    it wrote anything: every event of such a project was lost.
    """
    base = Path(tempfile.mkdtemp(prefix="task-hook-nogit-"))
    try:
        home = base / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "session_registry.json").write_text(
            json.dumps({"claude-1": {"main_session_id": "main_x"}}), encoding="utf-8"
        )
        # GIT_CEILING_DIRECTORIES keeps git from finding a repository above the temp dir.
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "GIT_CEILING_DIRECTORIES": str(base)}
        for name, argv in _runners():
            project = base / name.replace(":", "-")
            (project / ".workflow" / "data").mkdir(parents=True)
            raw = json.dumps({
                "session_id": "claude-1", "cwd": str(project),
                "hook_event_name": "UserPromptSubmit", "prompt": "/.execute -y",
            })
            subprocess.run(argv, input=raw, text=True, env={**env, "CLAUDE_HOOK_RAW": raw}, capture_output=True, timeout=60, **no_console())
            stream = project / ".workflow" / "data" / "tasks.jsonl"
            rows = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()] if stream.exists() else []
            assert_true(
                [(row.get("kind"), row.get("skill")) for row in rows] == [("skill", "execute")],
                f"[{name}] a project without git still records its skill event: {rows}",
            )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _test_task_telemetry() -> None:
    _check_states()
    _check_runtime_event()
    _check_hook_flavours()
    _check_without_git()
