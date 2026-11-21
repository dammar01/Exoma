"""Tasks are sequences of skill calls, closed by a verify the runtime derives as pass (DEC-015,
DEC-020); a commit joins the task it belongs to and closes nothing."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from exoma.core.audit import diagnostics as audit_diagnostics
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
        stream = root / ".workflow/data/tasks.jsonl"
        with stream.open("a", encoding="utf-8") as handle:
            for bad in ({"kind": "skill", "session_id": ["s"]},
                        {"kind": "commit", "session_id": "s", "paths": 7},
                        {"kind": [], "session_id": "s"}):
                handle.write(json.dumps(bad) + "\n")
        events = task_telemetry.load_events(root)
        assert_true(
            len(events) == 1 and events[0]["source"] == "runtime" and events[0]["verdict"] == "DONE"
            and events[0]["derived"] == "incomplete",
            f"the runtime writes one event per command with both verdicts; a torn line is skipped: {events}",
        )
        summary = task_telemetry.report(root)
        assert_true(summary["by_state"] == {"read_only": 1}, f"report reads the stream: {summary}")
        assert_true(summary["event_stream"] == {"status": "partial", "invalid_rows": 4},
                    "malformed events must be counted without erasing valid events")
        tasks, _ = derive_tasks([{"kind": "skill", "session_id": []}, _edit("valid", "ok.py")])
        assert_true(len(tasks) == 1 and tasks[0]["session_id"] == "valid",
                    "direct consumers also isolate wrong-type events")
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
            diag_report = task_telemetry.report(project)["hook_diagnostics"]
            assert_true(
                diag_report["status"] == "none" and diag_report["total"] == 0,
                f"[{name}] non-repo must not emit false hook diagnostics: {diag_report}",
            )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _make_git_shim(shim_dir: Path, script_body: str) -> Path:
    """Create a cross-platform `git` shim in shim_dir backed by a Python script."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    py_script = shim_dir / "fake_git.py"
    py_script.write_text(script_body, encoding="utf-8")
    if os.name == "nt":
        wrapper = shim_dir / "git.cmd"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{py_script}" %*\r\n', encoding="utf-8")
        return wrapper
    wrapper = shim_dir / "git"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py_script}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def _run_py_hook(project: Path, home: Path, payload: dict, *, extra_env: dict | None = None) -> tuple[subprocess.CompletedProcess, float]:
    raw = json.dumps(payload)
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "CLAUDE_HOOK_RAW": raw}
    if extra_env:
        env.update(extra_env)
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(HOOKS / "task-events.py")],
        input=raw, text=True, env=env, capture_output=True, timeout=30, **no_console(),
    )
    return proc, time.monotonic() - start


def _init_repo_with_commit(project: Path) -> str:
    (project / ".workflow" / "data").mkdir(parents=True, exist_ok=True)
    (project / "src").mkdir(exist_ok=True)
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@example.test")
    _git(project, "config", "user.name", "t")
    (project / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(project, "add", ".")
    _git(project, "commit", "-q", "-m", "init")
    return subprocess.run(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, **no_console(),
    ).stdout.strip()


def _load_hook_module(name: str = "task_events_unit"):
    spec = importlib.util.spec_from_file_location(name, str(HOOKS / "task-events.py"))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    old_raw = os.environ.pop("CLAUDE_HOOK_RAW", None)
    try:
        spec.loader.exec_module(mod)
    finally:
        if old_raw is not None:
            os.environ["CLAUDE_HOOK_RAW"] = old_raw
    return mod


def _check_git_timeout_and_retry() -> None:
    """W2: rev-parse timeout preserves skill event; git log failure/timeout keeps snapshot for retry;
    shared deadline bounds total Git time to <=2s instead of 2x5s; diagnostics visible in report/doctor."""
    real_git = shutil.which("git")
    assert_true(bool(real_git), "real git required for task-events reliability tests")
    base = Path(tempfile.mkdtemp(prefix="task-hook-w2-"))
    try:
        home = base / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "session_registry.json").write_text(
            json.dumps({"claude-1": {"main_session_id": "main_w2"}}), encoding="utf-8"
        )
        project = base / "proj"
        head_a = _init_repo_with_commit(project)

        # 1) rev-parse timeout must NOT lose UserPromptSubmit skill event (6s child aborted by ~2s deadline).
        shim_rp_timeout = base / "shim_rp_timeout"
        _make_git_shim(
            shim_rp_timeout,
            "import sys, time\n"
            "if 'rev-parse' in sys.argv:\n"
            "    sys.stdout.close()\n"
            "    sys.stderr.close()\n"
            "    time.sleep(6.0)\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n",
        )
        proc, elapsed = _run_py_hook(
            project, home,
            {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "UserPromptSubmit", "prompt": "/.execute -y"},
            extra_env={"PATH": f"{shim_rp_timeout}{os.pathsep}{os.environ.get('PATH', '')}"},
        )
        assert_true(proc.returncode == 0, f"hook must stay fail-open on rev-parse timeout: rc={proc.returncode}")
        assert_true(elapsed < 3.5, f"rev-parse timeout must abort 6s child near 2s deadline, took {elapsed:.2f}s")
        events = task_telemetry.load_events(project)
        assert_true(
            [(e.get("kind"), e.get("skill")) for e in events] == [("skill", "execute")],
            f"rev-parse timeout must still record UserPromptSubmit skill event: {events}",
        )
        diag = task_telemetry.load_hook_diagnostics(project)
        assert_true(
            diag["status"] == "failures_recorded"
            and diag["by_stage"].get("git_rev_parse") == 1
            and diag["by_reason"].get("git_timeout") == 1,
            f"rev-parse timeout must be recorded in hook diagnostics: {diag}",
        )
        raw_diag = (project / ".workflow" / "data" / "task-hook" / "diagnostics.jsonl").read_text(encoding="utf-8")
        assert_true("/.execute" not in raw_diag, f"diagnostics must not leak prompt text: {raw_diag}")

        # 2) Initialize snapshot normally at head_a, then make commit B.
        _run_py_hook(project, home, {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "Stop"})
        snap_file = project / ".workflow" / "data" / "task-hook" / "claude-1.head"
        assert_true(snap_file.read_text(encoding="utf-8").strip() == head_a, "initial snapshot must record head_a")
        (project / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
        _git(project, "commit", "-q", "-am", "commit-b")
        head_b = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, **no_console(),
        ).stdout.strip()
        assert_true(head_b != head_a, "commit B must move HEAD")

        # 3) git log nonzero must NOT advance snapshot.
        shim_log_fail = base / "shim_log_fail"
        _make_git_shim(
            shim_log_fail,
            f"import subprocess, sys\n"
            f"if 'log' in sys.argv:\n"
            f"    sys.exit(128)\n"
            f"res = subprocess.run([{real_git!r}, *sys.argv[1:]])\n"
            f"sys.exit(res.returncode)\n",
        )
        proc, _ = _run_py_hook(
            project, home,
            {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "Stop"},
            extra_env={"PATH": f"{shim_log_fail}{os.pathsep}{os.environ.get('PATH', '')}"},
        )
        assert_true(proc.returncode == 0, "git log failure must exit 0")
        assert_true(snap_file.read_text(encoding="utf-8").strip() == head_a, "git log nonzero must NOT advance .head")
        assert_true(
            all(e.get("kind") != "commit" for e in task_telemetry.load_events(project)),
            "git log nonzero must emit no commit rows",
        )

        # 4) Shared deadline: 1.2s in rev-parse + 1.5s in log = 2.7s > 2.0s shared deadline
        #    (would have succeeded under old 2x5s independent timeouts).
        shim_shared = base / "shim_shared"
        _make_git_shim(
            shim_shared,
            f"import subprocess, sys, time\n"
            f"if 'rev-parse' in sys.argv:\n"
            f"    time.sleep(1.2)\n"
            f"    res = subprocess.run([{real_git!r}, *sys.argv[1:]])\n"
            f"    sys.exit(res.returncode)\n"
            f"if 'log' in sys.argv:\n"
            f"    sys.stdout.close()\n"
            f"    sys.stderr.close()\n"
            f"    time.sleep(1.5)\n"
            f"    sys.exit(0)\n"
            f"sys.exit(1)\n",
        )
        proc, elapsed = _run_py_hook(
            project, home,
            {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "Stop"},
            extra_env={"PATH": f"{shim_shared}{os.pathsep}{os.environ.get('PATH', '')}"},
        )
        assert_true(proc.returncode == 0, "shared deadline timeout must exit 0")
        assert_true(elapsed < 4.5, f"shared deadline must bound rev-parse+log near 2s, took {elapsed:.2f}s")
        assert_true(snap_file.read_text(encoding="utf-8").strip() == head_a, "git log timeout must NOT advance .head")

        # 5) Subsequent normal call captures previously missed commit B and advances .head.
        _run_py_hook(project, home, {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "Stop"})
        assert_true(snap_file.read_text(encoding="utf-8").strip() == head_b, "recovery call must advance .head to head_b")
        commits = [e for e in task_telemetry.load_events(project) if e.get("kind") == "commit"]
        assert_true(
            len(commits) == 1 and commits[0]["commit"] == head_b and commits[0]["paths"] == ["src/a.py"],
            f"recovery call must capture previously missed commit B: {commits}",
        )

        # 6) Report & doctor surface failure diagnostics cleanly.
        rep = task_telemetry.report(project)
        assert_true(
            rep["hook_diagnostics"]["total"] == 3
            and rep["hook_diagnostics"]["by_stage"] == {"git_log": 2, "git_rev_parse": 1},
            f"report must surface hook_diagnostics counts: {rep['hook_diagnostics']}",
        )
        doc = audit_diagnostics.run_doctor(project, "codex")
        report_payload = json.loads(Path(doc["meta"]["doctor_report"]).read_text(encoding="utf-8"))
        doc_health = report_payload["checks"].get("task_events_health")
        assert_true(
            isinstance(doc_health, dict) and doc_health.get("total") == 3,
            f"doctor must expose task_events_health: {doc_health}",
        )
        assert_true(
            any("task-events hook recorded 3 observation failure" in fix for fix in doc["meta"].get("recommended_fixes", [])),
            f"doctor recommended_fixes should note task-events failures: {doc['meta'].get('recommended_fixes')}",
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _check_durability_replay_torn_and_concurrency() -> None:
    """W2: append fails -> snapshot stays; snapshot fails after append -> replay deduplicates;
    torn prior line does not swallow next append; concurrent stale advance cannot regress .head."""
    base = Path(tempfile.mkdtemp(prefix="task-hook-durability-"))
    try:
        home = base / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "session_registry.json").write_text(
            json.dumps({"claude-1": {"main_session_id": "main_dur"}}), encoding="utf-8"
        )
        project = base / "proj"
        head_a = _init_repo_with_commit(project)
        _run_py_hook(project, home, {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "Stop"})
        snap_file = project / ".workflow" / "data" / "task-hook" / "claude-1.head"
        assert_true(snap_file.read_text(encoding="utf-8").strip() == head_a, "snapshot initialized at head_a")

        # Edit a.py, verify pass, commit B.
        _run_py_hook(
            project, home,
            {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "PostToolUse",
             "tool_name": "Edit", "tool_input": {"file_path": str(project / "src" / "a.py")}},
        )
        task_telemetry.record_skill(project, "main_dur", "verify", verdict="DONE", derived="pass")
        (project / "src" / "a.py").write_text("x = 9\n", encoding="utf-8")
        _git(project, "commit", "-q", "-am", "commit-b")
        head_b = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, **no_console(),
        ).stdout.strip()

        # (a) Append failure (tasks.jsonl blocked by directory) must NOT advance .head.
        stream = project / ".workflow" / "data" / "tasks.jsonl"
        saved_stream = stream.read_bytes()
        stream.unlink()
        stream.mkdir()
        _run_py_hook(project, home, {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "Stop"})
        assert_true(snap_file.read_text(encoding="utf-8").strip() == head_a, "append failure must NOT advance .head")
        stream.rmdir()
        # Write back saved stream PLUS an unterminated torn line (no trailing newline).
        stream.write_bytes(saved_stream + b'{"kind":"edit","broken')

        # (b) Simulate crash between successful append and snapshot advance:
        #     append runs normally (guarding the torn line!), then snapshot stays at head_a.
        mod = _load_hook_module()
        deadline = time.monotonic() + mod.GIT_DEADLINE_SECONDS
        commits, pending = mod.collect_commits(
            {"session_id": "claude-1", "hook_event_name": "Stop"}, str(project), deadline, "main_dur"
        )
        assert_true(len(commits) == 1 and pending == (str(snap_file), head_a, head_b),
                    f"collect_commits must return commit B + pending snapshot without writing: {commits} {pending}")
        assert_true(snap_file.read_text(encoding="utf-8").strip() == head_a,
                    "collect_commits must not mutate .head before append")
        mod.append_events(str(project), "main_dur", commits)
        # Crash happens here before commit_snapshot; next hook invocation replays commit B and advances .head.
        _run_py_hook(project, home, {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "Stop"})
        assert_true(snap_file.read_text(encoding="utf-8").strip() == head_b, "retry after crash must advance .head")

        # Also test runtime writer after an unterminated torn line.
        with stream.open("ab") as fh:
            fh.write(b'{"kind":"skill","torn2')
        task_telemetry.record_skill(project, "main_dur", "analyze")

        stream_diag: dict = {}
        loaded = task_telemetry.load_events(project, diagnostics=stream_diag)
        assert_true(
            stream_diag == {"status": "partial", "invalid_rows": 2},
            f"only the two torn lines should be invalid; subsequent appends must survive: {stream_diag}",
        )
        tasks, unclaimed = derive_tasks(loaded)
        assert_true(
            len(tasks) == 2
            and tasks[0]["state"] == "completed"
            and tasks[0]["sequence"] == ["edit", "verify", "commit"]
            and tasks[0]["commit"] == head_b
            and tasks[1]["state"] == "read_only"
            and tasks[1]["sequence"] == ["analyze"]
            and unclaimed == 0,
            f"replay dedupe, torn-line isolation, and event ordering must hold: {tasks} unclaimed={unclaimed}",
        )

        # (c) Concurrent stale observation must not move .head backward.
        mod.commit_snapshot((str(snap_file), head_a, "0" * 40))
        assert_true(
            snap_file.read_text(encoding="utf-8").strip() == head_b,
            "stale concurrent snapshot update must not overwrite newer .head",
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _check_initial_baseline_and_lock_contention() -> None:
    """Blocking #1 & #2 regressions:
    - Initial baseline seeds independently of a failed local skill append.
    - OS-backed lock contention preserves independent events, defers Git observation,
      and prevents overlapping hooks from regressing .head."""
    base = Path(tempfile.mkdtemp(prefix="task-hook-lock-"))
    try:
        home = base / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "session_registry.json").write_text(
            json.dumps({"claude-1": {"main_session_id": "main_lock"}}), encoding="utf-8"
        )
        project = base / "proj"
        head_a = _init_repo_with_commit(project)
        snap_file = project / ".workflow" / "data" / "task-hook" / "claude-1.head"
        stream = project / ".workflow" / "data" / "tasks.jsonl"

        # (1) Block tasks.jsonl on the very first prompt with /.execute -y:
        #     skill append fails, but initial baseline MUST still be seeded at head_a.
        stream.mkdir()
        proc, _ = _run_py_hook(
            project, home,
            {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "UserPromptSubmit", "prompt": "/.execute -y"},
        )
        assert_true(proc.returncode == 0, "failed initial skill append must still exit 0")
        assert_true(
            snap_file.is_file() and snap_file.read_text(encoding="utf-8").strip() == head_a,
            "initial HEAD baseline must be saved even when local skill append fails",
        )
        stream.rmdir()

        # Make commit B after initial baseline is seeded.
        (project / "src" / "a.py").write_text("x = 42\n", encoding="utf-8")
        _git(project, "commit", "-q", "-am", "commit-b")
        head_b = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, **no_console(),
        ).stdout.strip()

        # (2) Hold the real OS lock across a concurrent subprocess hook invocation:
        #     the contender must defer Git observation (leaving .head at head_a),
        #     still append its independent local skill event, and record lock_contention.
        mod = _load_hook_module("task_events_lock_unit")
        lock_fh = mod.acquire_lock(str(snap_file) + ".lock")
        assert_true(lock_fh is not None, "test must acquire OS lock on snapshot")
        try:
            proc, _ = _run_py_hook(
                project, home,
                {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "UserPromptSubmit", "prompt": "/.commit"},
            )
            assert_true(proc.returncode == 0, "lock contention must exit 0")
            assert_true(
                snap_file.read_text(encoding="utf-8").strip() == head_a,
                "contending hook without OS lock must NOT advance .head",
            )
            rows = task_telemetry.load_events(project)
            assert_true(
                [(r.get("kind"), r.get("skill")) for r in rows] == [("skill", "commit")],
                f"contending hook must preserve independent skill event: {rows}",
            )
            diag = task_telemetry.load_hook_diagnostics(project)
            assert_true(
                diag["by_reason"].get("lock_contention") == 1,
                f"contending hook must record lock_contention diagnostic: {diag}",
            )
        finally:
            mod.release_lock(lock_fh)

        # After lock release, the next Stop captures commit B cleanly.
        _run_py_hook(project, home, {"session_id": "claude-1", "cwd": str(project), "hook_event_name": "Stop"})
        assert_true(snap_file.read_text(encoding="utf-8").strip() == head_b, "post-lock Stop must advance .head to head_b")
        commits = [r for r in task_telemetry.load_events(project) if r.get("kind") == "commit"]
        assert_true(
            len(commits) == 1 and commits[0]["commit"] == head_b,
            f"commit B must be captured once lock clears: {commits}",
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _check_diagnostics_permissions_and_write_only_streams() -> None:
    """Blocking #3 & #4 regressions + diagnostic allowlist:
    - PermissionError reading diagnostics.jsonl never crashes report() or run_doctor().
    - Unknown stage/reason or missing timestamp rows are rejected as invalid_rows.
    - Non-task runtime_io streams remain pure write-only (no read probe); task streams
      still append when the read probe raises PermissionError."""
    from exoma.core.evidence import runtime_io

    base = Path(tempfile.mkdtemp(prefix="task-hook-perm-"))
    try:
        project = base / "proj"
        (project / ".workflow" / "data" / "task-hook").mkdir(parents=True)
        diag_path = project / ".workflow" / "data" / "task-hook" / "diagnostics.jsonl"
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        deep_nested = ("[" * 2000) + ("]" * 2000)
        diag_path.write_text(
            json.dumps({"v": 1, "at": now_iso, "stage": "/etc/passwd", "reason": "C:/secret"}) + "\n"
            + json.dumps({"v": 1, "stage": "git_log", "reason": "git_timeout"}) + "\n"
            + json.dumps({"v": 1, "at": now_iso, "stage": [], "reason": "git_timeout"}) + "\n"
            + json.dumps({"v": 1, "at": now_iso, "stage": "git_log", "reason": {}}) + "\n"
            + deep_nested + "\n"
            + json.dumps({"v": 1, "at": now_iso, "stage": "git_log", "reason": "git_timeout"}) + "\n",
            encoding="utf-8",
        )
        filtered = task_telemetry.load_hook_diagnostics(project)
        assert_true(
            filtered["total"] == 1 and filtered["invalid_rows"] == 5
            and filtered["by_stage"] == {"git_log": 1} and filtered["by_reason"] == {"git_timeout": 1},
            f"foreign, missing-ts, wrong-type, and deeply-nested rows must be skipped while valid rows survive: {filtered}",
        )
        rep_ok = task_telemetry.report(project)
        assert_true(
            rep_ok["hook_diagnostics"]["total"] == 1 and rep_ok["hook_diagnostics"]["invalid_rows"] == 5,
            f"report() must survive wrong-type and deeply-nested diagnostic fields: {rep_ok['hook_diagnostics']}",
        )
        doc_ok = audit_diagnostics.run_doctor(project, "codex")
        doc_ok_payload = json.loads(Path(doc_ok["meta"]["doctor_report"]).read_text(encoding="utf-8"))
        assert_true(
            doc_ok_payload["checks"]["task_events_health"]["total"] == 1,
            f"doctor must survive wrong-type diagnostic fields: {doc_ok_payload['checks']['task_events_health']}",
        )

        # Pre-baseline operational rev-parse failure (e.g. dubious ownership) records a diagnostic
        # without leaking stderr text, whereas an unborn repo stays quiet.
        home = base / "home_notes"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "session_registry.json").write_text(
            json.dumps({"claude-1": {"main_session_id": "main_notes"}}), encoding="utf-8"
        )
        unborn = base / "unborn"
        (unborn / ".workflow" / "data").mkdir(parents=True)
        _git(unborn, "init", "-q")
        _run_py_hook(unborn, home, {"session_id": "claude-1", "cwd": str(unborn), "hook_event_name": "Stop"})
        assert_true(
            task_telemetry.load_hook_diagnostics(unborn)["total"] == 0,
            "unborn repo before baseline must not emit false hook diagnostics",
        )
        dubious = base / "dubious"
        (dubious / ".workflow" / "data").mkdir(parents=True)
        shim_dubious = base / "shim_dubious"
        locale_seen = base / "locale_seen.json"
        _make_git_shim(
            shim_dubious,
            f"import json, os, sys\n"
            f"with open({str(locale_seen)!r}, 'w', encoding='utf-8') as fh:\n"
            f"    json.dump({{'LC_ALL': os.environ.get('LC_ALL'), 'LANG': os.environ.get('LANG')}}, fh)\n"
            f"sys.stderr.write('fatal: detected dubious ownership in repository at /secret/not a git repository/repo\\n')\n"
            f"sys.exit(128)\n",
        )
        _run_py_hook(
            dubious, home,
            {"session_id": "claude-1", "cwd": str(dubious), "hook_event_name": "UserPromptSubmit", "prompt": "/.execute -y"},
            extra_env={"PATH": f"{shim_dubious}{os.pathsep}{os.environ.get('PATH', '')}"},
        )
        assert_true(
            locale_seen.is_file() and json.loads(locale_seen.read_text(encoding="utf-8")) == {"LC_ALL": "C", "LANG": "C"},
            f"git subprocess must receive LC_ALL=C and LANG=C: {locale_seen.read_text(encoding='utf-8') if locale_seen.exists() else 'missing'}",
        )
        dubious_diag = task_telemetry.load_hook_diagnostics(dubious)
        assert_true(
            dubious_diag["by_stage"].get("git_rev_parse") == 1
            and dubious_diag["by_reason"].get("git_nonzero") == 1,
            f"pre-baseline dubious ownership must be recorded as git_rev_parse/git_nonzero: {dubious_diag}",
        )
        raw_dubious = (dubious / ".workflow" / "data" / "task-hook" / "diagnostics.jsonl").read_text(encoding="utf-8")
        assert_true("/secret/" not in raw_dubious, f"diagnostics must never leak stderr text: {raw_dubious}")

        # Simulate PermissionError on diagnostics.jsonl (no exists() crash).
        orig_read_text = Path.read_text
        def _deny_diag(self, *args, **kwargs):
            if self.name == "diagnostics.jsonl":
                raise PermissionError("denied")
            return orig_read_text(self, *args, **kwargs)
        Path.read_text = _deny_diag  # type: ignore[method-assign]
        try:
            rep = task_telemetry.report(project)
            assert_true(
                rep["hook_diagnostics"]["status"] == "unavailable",
                f"report() must degrade to unavailable on PermissionError: {rep['hook_diagnostics']}",
            )
            doc = audit_diagnostics.run_doctor(project, "codex")
            payload = json.loads(Path(doc["meta"]["doctor_report"]).read_text(encoding="utf-8"))
            assert_true(
                payload["checks"]["task_events_health"]["status"] == "unavailable",
                f"doctor must degrade to unavailable on PermissionError: {payload['checks']['task_events_health']}",
            )
        finally:
            Path.read_text = orig_read_text  # type: ignore[method-assign]

        # Non-task streams must NOT invoke the read tail probe.
        orig_probe = runtime_io._needs_leading_newline
        probe_calls: list[str] = []
        def _spy_probe(p: Path) -> bool:
            probe_calls.append(p.name)
            return False
        runtime_io._needs_leading_newline = _spy_probe
        try:
            runtime_io.write_audit_record(project, {"kind": "audit"})
            runtime_io.write_usage_record(project, {"kind": "usage"})
            runtime_io.write_quality_record(project, {"kind": "quality"})
            assert_true(probe_calls == [], f"non-task streams must stay pure write-only: {probe_calls}")
            runtime_io.write_task_event(project, {"v": 2, "kind": "skill", "skill": "plan", "session_id": "s1", "at": "t"})
            assert_true(probe_calls == ["tasks.jsonl"], f"only tasks.jsonl should probe tail: {probe_calls}")
        finally:
            runtime_io._needs_leading_newline = orig_probe

        # Write-only fallback: even if read probe fails with PermissionError, task append succeeds.
        mod = _load_hook_module("task_events_wo_unit")
        orig_hook_probe = mod.needs_leading_newline
        mod.needs_leading_newline = lambda _p: False
        try:
            mod.append_events(str(project), "s1", [{"kind": "edit", "path": "src/wo.py"}])
        finally:
            mod.needs_leading_newline = orig_hook_probe
        events = task_telemetry.load_events(project)
        assert_true(
            any(e.get("kind") == "edit" and e.get("path") == "src/wo.py" for e in events),
            f"write-only fallback must append hook events: {events}",
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _test_task_telemetry() -> None:
    _check_states()
    _check_runtime_event()
    _check_hook_flavours()
    _check_without_git()
    _check_git_timeout_and_retry()
    _check_durability_replay_torn_and_concurrency()
    _check_initial_baseline_and_lock_contention()
    _check_diagnostics_permissions_and_write_only_streams()
