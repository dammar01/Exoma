"""The hook failures reproduced on 2026-10-20, replayed through the real launchers (DEC-068).

Each block names the finding it pins (A-K in DEC-068) and fails on the hooks as they were
before that record: a payload past Windows' 32767-character environment limit crashed every
hook (A); a /branch session shared its parent's runtime state (B, C); the gate armed on
sentences, pasted text and unknown prompt sources (H) and refused skill reads, verify's diff
and multi-line runner tasks it was meant to pass (F, G); `git.exe commit` skipped approval
(J); and the launchers chose their own Python (K). The status-guard cases live with the
other hook contracts in hook_contract.py.

Called from hook_contract._test_hooks_run_through_their_launchers, so it runs wherever the
launcher contracts run. Needs bash (Git Bash on Windows), as those do.
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
from exoma.utils import osutil
from tests.checks.support import no_console

HOOKS = Path(__file__).resolve().parents[2] / "src" / "exoma" / "_bundle" / "config" / "claude" / "hooks"
BIG = 40_000  # past the 32767-character limit Windows puts on one environment variable


class _Repro:
    """A throwaway home and project; every hook run as Claude Code runs it."""

    def __init__(self, bash: str):
        self.bash = bash
        self.base = Path(tempfile.mkdtemp(prefix="hook-repro-"))
        self.home = self.base / "home"
        self.project = self.base / "project"
        self.outside = self.base / "outside"
        (self.home / ".claude" / "skills").mkdir(parents=True)
        (self.home / ".claude" / "skills" / "analyze.md").write_text("# Skill: analyze\n", encoding="utf-8")
        (self.project / ".workflow" / "data").mkdir(parents=True)
        (self.project / ".workflow" / "config.json").write_text("{}", encoding="utf-8")
        self.outside.mkdir()
        self.hooks = HOOKS

    def close(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    def env(self, **extra) -> dict:
        env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home), **extra}
        for key in ("WORKFLOW_LOCAL_MODE", "CLAUDE_HOOK_RAW", "CLAUDE_CONFIG_DIR"):
            env.pop(key, None)
        return env

    def run(self, hook: str, payload=None, raw: bytes | None = None, **env) -> tuple[int, str, str]:
        data = raw if raw is not None else json.dumps(payload).encode("utf-8")
        done = subprocess.run([self.bash, str(self.hooks / f"{hook}.sh")], input=data, capture_output=True,
                              env=self.env(**env), timeout=60, **no_console())
        return done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")

    def bind(self, sid: str, source: str = "startup") -> str:
        self.run("session-bind", {"session_id": sid, "cwd": str(self.project), "source": source,
                                  "hook_event_name": "SessionStart"})
        registry = json.loads((self.home / ".claude" / "session_registry.json").read_text(encoding="utf-8"))
        return registry[sid]["main_session_id"]

    def runtime(self, main_id: str) -> Path:
        return self.project / ".workflow" / "data" / "sessions" / main_id / "runtime"

    def prompt(self, sid: str, text: str, **extra) -> tuple[int, str, str]:
        return self.run("intent-gate-set", {"session_id": sid, "cwd": str(self.project),
                                            "hook_event_name": "UserPromptSubmit", "prompt": text, **extra})

    def tool(self, sid: str, hook: str, name: str, tool_input: dict) -> int:
        return self.run(hook, {"session_id": sid, "cwd": str(self.project), "hook_event_name": "PreToolUse",
                               "tool_name": name, "tool_input": tool_input})[0]

    def arm(self, main_id: str, command: str) -> None:
        self.runtime(main_id).mkdir(parents=True, exist_ok=True)
        (self.runtime(main_id) / "delegated.marker").write_text(
            json.dumps({"command": command, "set_at": "2099-01-01T00:00:00+00:00"}), encoding="utf-8")


def _payload_size(r: _Repro) -> None:
    """A: no payload size crashes a hook, and the gates still decide on big payloads."""
    m = r.bind("a1")
    code, _, err = r.run("task-events", {"session_id": "a1", "cwd": str(r.project), "hook_event_name": "PostToolUse",
                                         "tool_name": "Edit", "tool_input": {"file_path": str(r.project / "x.py"),
                                                                             "old_string": "x" * BIG, "new_string": "y"}})
    assert_true(code == 0 and "Traceback" not in err, f"A: a {BIG}-character Edit payload runs task-events: {code} {err[-200:]!r}")
    r.prompt("a1", "/.execute -y\n\n<pasted_content>" + "x" * BIG + "</pasted_content>")
    assert_true((r.runtime(m) / "execute.token").is_file(), "A: `/.execute -y` beside a big paste still writes execute.token")
    # Outside the root and outside temp (which the root check allows); nothing is written.
    elsewhere = Path(r.base.anchor) / "hook-repro-not-a-project" / "big.txt"
    code = r.tool("a1", "execution-gate", "Write", {"file_path": str(elsewhere), "content": "x" * BIG})
    assert_true(code == 2, f"A: a big Write outside the root is still refused, not failed open: {code}")
    (r.runtime(m) / "execute.token").unlink()
    r.prompt("a1", "/.execute -y Á")  # UTF-8 0xC3 0x81: 0x81 has no cp1252 character
    assert_true((r.runtime(m) / "execute.token").is_file(), "A: stdin is read as UTF-8 whatever the console code page is")


def _fork_isolation(r: _Repro) -> None:
    """B, C: a fork is its own session; one window's prompt cannot clear the other's approval."""
    parent = r.bind("b1")
    fork = r.bind("b2", source="fork")
    assert_true(fork != parent, "B: a fork gets its own MAIN_SESSION_ID")
    r.prompt("b2", "/.commit")
    r.prompt("b1", "hanya fitur besar, untuk perbaikan bisa di skip")
    r.prompt("b2", "yes")
    code = r.tool("b2", "execution-gate", "Bash", {"command": "git commit -q -F msg.txt"})
    assert_true(code == 0, f"C: the parent's prompt leaves the fork's commit approval alone: {code}")


def _commit_strict(r: _Repro) -> None:
    """D (kept strict by decision) and J."""
    r.bind("d1")
    r.prompt("d1", "/.commit")
    r.prompt("d1", "saes")
    r.prompt("d1", "yes")
    code = r.tool("d1", "execution-gate", "Bash", {"command": "git commit -m x"})
    assert_true(code == 2, f"D: any other prompt between /.commit and yes withdraws the approval: {code}")
    for command in ("git.exe commit -m x", '"C:/Program Files/Git/cmd/git.exe" commit -m x'):
        code = r.tool("d1", "execution-gate", "Bash", {"command": command})
        assert_true(code == 2, f"J: `{command}` needs approval like `git commit`: {code}")


def _skill_and_task_reads(r: _Repro) -> None:
    """F, G: what the gate exists to let through while armed."""
    m = r.bind("f1")
    r.arm(m, "analyze")
    skill = r.home / ".claude" / "skills" / "analyze.md"
    spellings = [str(skill), skill.as_posix(), "~/.claude/skills/analyze.md"]
    if osutil.IS_WINDOWS:
        posix = skill.as_posix()
        spellings.append(f"/{posix[0].lower()}{posix[2:]}")
    for path in spellings:
        assert_true(r.tool("f1", "intent-gate-check", "Read", {"file_path": path}) == 0, f"F: Read of the skill as {path!r} passes")
    for command in ("cat ~/.claude/skills/analyze.md", f"cat '{skill.as_posix()}'", f"Get-Content '{skill}'"):
        assert_true(r.tool("f1", "intent-gate-check", "Bash", {"command": command}) == 0, f"F: `{command}` passes")
    (r.project / "src.py").write_text("x = 1\n", encoding="utf-8")
    for command in (f"cat '{(r.project / 'src.py').as_posix()}'", "cat ~/.claude/skills/../settings.json",
                    "cat ~/.claude/skills/analyze.md | head"):
        assert_true(r.tool("f1", "intent-gate-check", "Bash", {"command": command}) == 2, f"F: `{command}` is still gated")

    tasks = Path(tempfile.mkdtemp(prefix="claude-hook-repro-")) / "s1" / "tasks"
    try:
        tasks.mkdir(parents=True)
        (tasks / "b1.output").write_text("{}", encoding="utf-8")
        (tasks.parent / "other.output").write_text("{}", encoding="utf-8")
        output = tasks / "b1.output"
        assert_true(r.tool("f1", "intent-gate-check", "Read", {"file_path": str(output)}) == 0,
                    "G: Read of a background task's output passes")
        assert_true(r.tool("f1", "intent-gate-check", "Bash", {"command": f"cat '{output.as_posix()}'"}) == 0,
                    "G: cat of a background task's output passes")
        assert_true(r.tool("f1", "intent-gate-check", "Read", {"file_path": str(tasks.parent / "other.output")}) == 2,
                    "G: an .output file outside a tasks/ folder is still gated")
    finally:
        shutil.rmtree(tasks.parent.parent, ignore_errors=True)


def _runner_and_verify_lanes(r: _Repro) -> None:
    """G: the runner with a multi-line task, and verify's file-name diff as models write it."""
    m = r.bind("g1")
    root = r.project.as_posix()
    r.arm(m, "plan")
    task = "Susun rencana.\n\nKEPUTUSAN:\nA. pakai $VAR; tanpa | pipe"
    code = r.tool("g1", "intent-gate-check", "Bash", {"command": f"bash '{root}/.workflow/run.sh' plan '{task}' '{m}'"})
    assert_true(code == 0, f"G: a runner call whose single-quoted task spans lines passes: {code}")
    code = r.tool("g1", "intent-gate-check", "Bash", {"command": f"bash '{root}/.workflow/run.sh' plan 'x'\ncat secrets '{m}'"})
    assert_true(code == 2, f"G: a newline outside quotes still ends the command and is refused: {code}")
    r.arm(m, "verify")
    outside = r.outside.as_posix()
    for command, want in ((f"cd '{root}' && git diff --name-only", 0), (f"git -C '{root}' diff --stat", 0),
                          (f"cd '{outside}' && git diff --name-only", 2), (f"git -C '{outside}' diff --stat", 2),
                          (f"cd '{root}' && git diff", 2), (f"cd '{root}' && cat src.py", 2)):
        code = r.tool("g1", "intent-gate-check", "Bash", {"command": command})
        assert_true(code == want, f"G: `{command}` under verify -> {want}, got {code}")


def _explicit_arming(r: _Repro) -> None:
    """H: only a command the user named arms the gate; the rest gets a hint."""
    m = r.bind("h1")
    marker = r.runtime(m) / "delegated.marker"
    for text in ("terkait playwright ini, sebelumnya saya menggunakan celery, apakah tetap menggunakan celery ini?",
                 "saat saya analisa, ada titik dimana terlogout tetapi arahnya membaik",
                 '<pasted_content id="1">\n/.analyze ini dulu, lalu commit\n</pasted_content>'):
        _, out, _ = r.prompt("h1", text)
        assert_true(not marker.exists(), f"H: {text[:40]!r} arms nothing")
        assert_true(not (r.runtime(m) / "commit.marker").exists(), f"H: {text[:40]!r} approves no commit")
    _, out, _ = r.prompt("h1", "saat saya analisa, ada titik dimana terlogout")
    assert_true("[INTENT HINT]" in out, f"H: a sentence that resembles a command gets a hint: {out[:160]!r}")
    r.prompt("h1", "<command-message>.analyze</command-message>\n<command-name>/.analyze</command-name>\n<command-args>x</command-args>")
    assert_true(marker.is_file() and json.loads(marker.read_text(encoding="utf-8"))["command"] == "analyze",
                "H: the slash-command payload arms the gate")

    r.prompt("h1", "analisa ulang dan commit", source="future_host_source")
    assert_true(not (r.runtime(m) / "commit.marker").exists(), "H: an unknown source approves a commit only from the prompt's start")
    r.prompt("h1", "commit perubahan ini", source="future_host_source")
    assert_true((r.runtime(m) / "commit.marker").is_file(), "H: an unknown source still counts as the user's")
    seen = json.loads((r.home / ".claude" / "prompt-sources.json").read_text(encoding="utf-8"))
    assert_true(seen.get("future_host_source", {}).get("count") == 2, f"H: unknown sources are counted: {seen}")

    (r.project / ".workflow" / "config.json").write_text(
        json.dumps({"policies": {"prompt_sources": {"host": ["future_host_source"]}}}), encoding="utf-8")
    r.prompt("h1", "/.execute -y", source="future_host_source")
    assert_true(not (r.runtime(m) / "execute.token").exists(), "H: a source the project names as host approves nothing")
    (r.project / ".workflow" / "config.json").write_text("{}", encoding="utf-8")


def _git_bash_env() -> None:
    """I: Windows installs name Git Bash to Claude Code; the user's own value wins."""
    from exoma.installer import settings as installer_settings
    from exoma.installer.base import Plan

    env = installer_settings._git_bash_env()
    if osutil.IS_WINDOWS and osutil.find_bash():
        value = env.get(installer_settings.GIT_BASH_ENV)
        assert_true(value and os.path.isfile(value), f"I: Windows settings carry {installer_settings.GIT_BASH_ENV}: {env}")
    else:
        assert_true(env == {}, f"I: no Git Bash variable off Windows: {env}")
    template = {"env": {installer_settings.GIT_BASH_ENV: "C:/Git/bin/bash.exe"}}
    merged, added = installer_settings._merge_env({"env": {"MINE": "1"}}, template, Plan())
    assert_true(added == [installer_settings.GIT_BASH_ENV] and merged["MINE"] == "1", f"I: a missing key is added beside the user's: {merged}")
    merged, added = installer_settings._merge_env({"env": {installer_settings.GIT_BASH_ENV: "D:/mine/bash.exe"}}, template, Plan())
    assert_true(merged is None and added == [], "I: the user's own Git Bash path is kept")


def _pin_beside_settings(r: _Repro) -> None:
    """K: the pin lands beside the settings.json being written, never in the real home."""
    from exoma.installer import settings as installer_settings
    from exoma.installer.base import Plan

    real_pin = osutil.pinned_python_path()
    existed = real_pin.exists()
    dest = r.base / "claude-dir" / "settings.json"
    installer_settings._pin_python(dest, Plan(), True)
    pin = dest.parent / "hooks" / osutil.PINNED_PYTHON_FILE
    assert_true(pin.read_text(encoding="utf-8").strip() == Path(sys.executable).as_posix(),
                f"K: the pin is written beside the target settings.json: {pin}")
    assert_true(real_pin.exists() == existed, f"K: installing elsewhere leaves {real_pin} as it was")


def _pinned_python(r: _Repro) -> None:
    """K: the launcher runs the interpreter install.py pinned, PATH or no PATH."""
    hooks = r.base / "hooks"
    shutil.copytree(HOOKS, hooks, ignore=shutil.ignore_patterns("__pycache__"))
    r.hooks = hooks
    try:
        bare_path = str(Path(r.bash).parent)  # bash itself, no python beside it
        start = {"session_id": "k1", "cwd": str(r.project), "source": "startup", "hook_event_name": "SessionStart"}
        _, out, _ = r.run("session-bind", start, PATH=bare_path)
        if "MAIN_SESSION_ID=" not in out:  # the bash folder has no python: the case this pins
            (hooks / osutil.PINNED_PYTHON_FILE).write_text(Path(sys.executable).as_posix() + "\r\n", encoding="utf-8")
            _, out, _ = r.run("session-bind", start, PATH=bare_path)
            assert_true("MAIN_SESSION_ID=" in out, f"K: the pinned interpreter runs with no python on PATH: {out[:160]!r}")
    finally:
        r.hooks = HOOKS
    pin = osutil.pinned_python_path(r.home)
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(sys.executable + "\n", encoding="utf-8")
    assert_true(osutil.pinned_python(r.home) == sys.executable, "K: the generated scripts read the same pin")
    pin.write_text(str(r.base / "gone" / "python.exe"), encoding="utf-8")
    assert_true(osutil.pinned_python(r.home) is None, "K: a pin naming a missing interpreter is ignored")


def _pin_follows_agent_home(r: _Repro) -> None:
    """K: the pin is read from AGENT_HOME, where an install into AGENT_HOME wrote it (CASE-029)."""
    saved = os.environ.get("AGENT_HOME")
    os.environ["AGENT_HOME"] = str(r.home)
    try:
        assert_true(osutil.pinned_python_path() == r.home / ".claude" / "hooks" / osutil.PINNED_PYTHON_FILE,
                    f"K: the pin lookup follows AGENT_HOME: {osutil.pinned_python_path()}")
    finally:
        if saved is None:
            os.environ.pop("AGENT_HOME", None)
        else:
            os.environ["AGENT_HOME"] = saved
    assert_true(osutil.agent_home() == (Path(saved).expanduser() if saved else Path.home()),
                "K: without the override the lookup is the user's home again")


def _policy_helper_missing(r: _Repro) -> None:
    """A hooks directory without workflow_policy.py (a partial install) fails open, silently."""
    m = r.bind("p1")
    hooks = r.base / "hooks-no-policy"
    shutil.copytree(HOOKS, hooks, ignore=shutil.ignore_patterns("__pycache__", "workflow_policy.py"))
    r.hooks = hooks
    log_path = r.home / ".claude" / "hook-errors.log"
    before = log_path.read_text(encoding="utf-8").count("--- ") if log_path.exists() else 0
    try:
        code, _, _ = r.prompt("p1", "/.analyze why")
        assert_true(code == 0 and not (r.runtime(m) / "delegated.marker").exists(),
                    f"partial install: intent-gate-set arms nothing and exits 0, got {code}")
        code = r.tool("p1", "execution-gate", "Write", {"file_path": str(r.outside / "x.txt"), "content": "x"})
        assert_true(code == 0, f"partial install: execution-gate fails open, got {code}")
        code, _, _ = r.run("status-guard", {"session_id": "p1", "cwd": str(r.project), "hook_event_name": "Stop"})
        assert_true(code == 0, f"partial install: status-guard fails open, got {code}")
        after = log_path.read_text(encoding="utf-8").count("--- ") if log_path.exists() else 0
        assert_true(after == before, "partial install: a missing helper is not logged as a hook crash")
    finally:
        r.hooks = HOOKS


def _exit_codes(r: _Repro) -> None:
    """A hook exiting anything but 0 or 2 is failed open and logged, not shown as an error."""
    hooks = r.base / "hooks-exit"
    shutil.copytree(HOOKS, hooks, ignore=shutil.ignore_patterns("__pycache__"))
    r.hooks = hooks
    try:
        stop = {"session_id": "x", "cwd": str(r.project), "hook_event_name": "Stop"}
        log_path = r.home / ".claude" / "hook-errors.log"
        before = log_path.read_text(encoding="utf-8").count("--- ") if log_path.exists() else 0
        for body, label in (("import sys\nsys.exit(3)\n", "exit 3"), ("import sys\nsys.exit('broken')\n", "a message exit"),
                            ("raise RuntimeError('boom')\n", "an exception")):
            (hooks / "status-guard.py").write_text(body, encoding="utf-8")
            code, _, _ = r.run("status-guard", stop)
            assert_true(code == 0, f"escalation: {label} fails open with 0, got {code}")
        log = log_path.read_text(encoding="utf-8")
        assert_true(log.count("--- ") - before == 3 and "boom" in log, f"escalation: each failure is logged in ~/.claude: {log[-300:]!r}")
        assert_true(not (hooks / "hook-errors.log").exists(), "escalation: nothing is logged beside the hook scripts")
        (hooks / "status-guard.py").write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
        assert_true(r.run("status-guard", stop)[0] == 2, "escalation: exit 2 still blocks")
    finally:
        r.hooks = HOOKS


def _registry_concurrency(r: _Repro) -> None:
    """Sessions starting together all keep their binding, round after round (a lock that gave
    up on Windows' busy-delete PermissionError lost bindings only some of the time)."""
    lock = r.home / ".claude" / "session_registry.json.lock"
    for rnd in range(3):
        sids = [f"r{rnd}c{i}" for i in range(8)]
        procs = []
        for sid in sids:
            payload = json.dumps({"session_id": sid, "cwd": str(r.project), "source": "startup",
                                  "hook_event_name": "SessionStart"}).encode("utf-8")
            p = subprocess.Popen([r.bash, str(HOOKS / "session-bind.sh")], stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=r.env(), **no_console())
            procs.append((p, payload))
        for p, payload in procs:
            p.communicate(payload, timeout=60)
        registry = json.loads((r.home / ".claude" / "session_registry.json").read_text(encoding="utf-8"))
        missing = [sid for sid in sids if sid not in registry]
        assert_true(not missing, f"escalation: concurrent session starts lose no binding (round {rnd}); missing {missing}")
        assert_true(not lock.exists(), "escalation: the registry lock is released")

    # A lock someone else holds now is left alone; one left by a dead holder is broken.
    lock.write_text("foreign-live", encoding="ascii")
    r.bind("held")
    assert_true(lock.read_text(encoding="ascii") == "foreign-live", "escalation: a bind never removes a live lock it does not own")
    registry = json.loads((r.home / ".claude" / "session_registry.json").read_text(encoding="utf-8"))
    assert_true("held" in registry, "escalation: past the wait a bind goes ahead unlocked")
    old = time.time() - 60
    os.utime(lock, (old, old))
    r.bind("stale")
    assert_true(not lock.exists(), "escalation: a lock left by a dead holder is broken and the new one released")


def _runner_symlink(r: _Repro) -> None:
    """A run.sh that links outside the project is not the runner."""
    m = r.bind("s1")
    r.arm(m, "plan")
    elsewhere = r.outside / "run.sh"
    elsewhere.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    link = r.project / ".workflow" / "run.sh"
    try:
        os.symlink(elsewhere, link)
    except (OSError, NotImplementedError):
        print("  hook-repro: symlinks not permitted here; runner-symlink case skipped")
        return
    try:
        code = r.tool("s1", "intent-gate-check", "Bash", {"command": f"bash '{link.as_posix()}' plan 'x' '{m}'"})
        assert_true(code == 2, f"escalation: a runner symlinked outside the project is refused: {code}")
    finally:
        link.unlink()
    link.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    code = r.tool("s1", "intent-gate-check", "Bash", {"command": f"bash '{link.as_posix()}' plan 'x' '{m}'"})
    assert_true(code == 0, f"escalation: the project's own runner still passes: {code}")


def check_repro_matrix() -> None:
    bash = osutil.find_bash()
    if not bash:
        print("  hook-repro: no bash on this machine, skipped (the launchers need it)")
        return
    r = _Repro(bash)
    # Running the shipped hooks must leave nothing behind in dist/ (a log written beside the
    # scripts once landed in the bundle and its manifest).
    shipped = {p.name for p in HOOKS.iterdir() if p.name != "__pycache__"}
    try:
        _payload_size(r)
        _fork_isolation(r)
        _commit_strict(r)
        _skill_and_task_reads(r)
        _runner_and_verify_lanes(r)
        _explicit_arming(r)
        _git_bash_env()
        _pinned_python(r)
        _pin_follows_agent_home(r)
        _policy_helper_missing(r)
        _exit_codes(r)
        _registry_concurrency(r)
        _runner_symlink(r)
        _pin_beside_settings(r)
        left = {p.name for p in HOOKS.iterdir() if p.name != "__pycache__"} - shipped
        assert_true(not left, f"running the hooks wrote into src/exoma/_bundle/config/claude/hooks: {sorted(left)}")
    finally:
        r.close()
