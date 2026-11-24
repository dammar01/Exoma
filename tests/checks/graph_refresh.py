"""The graph-refresh Stop hook returns before graphify finishes, and a reader waits it out.

DEC-012 after CASE-008: on a large graph `graphify update` outlived the hook's 45 s wait,
was killed before writing, and the turn paid 47 s for a graph that stayed stale. The hook
now prunes skip directories before walking, starts a detached worker under
`graphify-out/.refresh.lock`, and returns. These checks hold it to that with a fake
graphify that sleeps, rewrites graph.json, and exits 1 as the real one does on a large
graph.

Runners follow tests/checks/statusline.py: graph-refresh.py directly, and the bash launcher
(Git Bash on Windows) that Claude Code runs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from exoma.core.graph import graph_index
from exoma.utils.osutil import find_bash, hidden_run_kwargs
from tests.checks.support import assert_true

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "src" / "exoma" / "_bundle" / "config" / "hosts" / "claude" / "hooks"
_FAKE_SLEEP_S = 3

# FAKE_GRAPHIFY_MODE: "ok" sleeps then writes a graph; "corrupt" sleeps then writes half a
# graph.
_FAKE_GRAPHIFY = """
import os, sys, time
mode = os.environ.get("FAKE_GRAPHIFY_MODE", "ok")
pause = float(os.environ.get("FAKE_GRAPHIFY_SLEEP", "0"))
time.sleep(pause)
body = '{"nodes": [' if mode == "corrupt" else '{"nodes": [], "links": [], "refreshed": true}'
with open(os.path.join("graphify-out", "graph.json"), "w", encoding="utf-8") as fh:
    fh.write(body)
sys.exit(1)
"""


def _hook_source() -> str:
    return (HOOKS / "graph-refresh.py").read_text(encoding="utf-8").replace("\r\n", "\n")


def _runners() -> list[tuple[str, list[str]]]:
    runners: list[tuple[str, list[str]]] = [("py", [sys.executable, str(HOOKS / "graph-refresh.py")])]
    bash = find_bash()
    if bash:
        runners.append(("sh", [bash, str(HOOKS / "graph-refresh.sh")]))
    return runners


def _fake_bin(root: Path) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    (bin_dir / "fake_graphify.py").write_text(_FAKE_GRAPHIFY, encoding="utf-8")
    if os.name == "nt":
        (bin_dir / "graphify.cmd").write_text(
            f'@"{sys.executable}" "%~dp0fake_graphify.py" %*\r\n', encoding="utf-8"
        )
    else:
        script = bin_dir / "graphify"
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/fake_graphify.py" "$@"\n', encoding="utf-8")
        script.chmod(0o755)
    return bin_dir


def _project(root: Path, *, stale: bool) -> Path:
    project = root / "project"
    (project / "src").mkdir(parents=True)
    (project / "graphify-out").mkdir()
    (project / ".workflow" / "data").mkdir(parents=True)
    (project / "node_modules" / "dep").mkdir(parents=True)
    (project / "Target" / "debug").mkdir(parents=True)
    graph = project / "graphify-out" / "graph.json"
    graph.write_text('{"nodes": [], "links": []}', encoding="utf-8")
    source = project / "src" / "app.py"
    source.write_text("x = 1\n", encoding="utf-8")
    dependency = project / "node_modules" / "dep" / "index.js"
    dependency.write_text("module.exports = 1\n", encoding="utf-8")
    now = time.time()
    os.utime(graph, (now - 100, now - 100))
    os.utime(source, (now - (50 if stale else 200),) * 2)
    # Newer than the graph on purpose: a walk that entered node_modules would call the
    # graph stale here. Only pruning before descent keeps this project fresh.
    os.utime(dependency, (now,) * 2)
    # A build directory in another case, with an extension in another case: skipped in
    # both flavours, as Windows and macOS treat the names as one.
    build_output = project / "Target" / "debug" / "Build.RS"
    build_output.write_text("fn main() {}", encoding="utf-8")
    os.utime(build_output, (now,) * 2)
    return project


def _run_hook(
    command: list[str], project: Path, bin_dir: Path, message: str = "[EXECUTION RESULT] done",
    extra_env: dict | None = None,
) -> float:
    payload = json.dumps({"last_assistant_message": message, "cwd": str(project)})
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
           "CLAUDE_HOOK_RAW": payload, "FAKE_GRAPHIFY_SLEEP": str(_FAKE_SLEEP_S)}
    env.pop("GRAPH_REFRESH_LIMIT_S", None)
    env.pop("FAKE_GRAPHIFY_MODE", None)
    env.update(extra_env or {})
    started = time.monotonic()
    # Hidden: a PowerShell started from a console-less test runner opens a window of its own.
    subprocess.run(command, input=payload, capture_output=True, text=True, env=env, timeout=60, **hidden_run_kwargs())
    return time.monotonic() - started


def _rows(project: Path) -> list[dict]:
    path = project / ".workflow" / "data" / "quality.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _wait_for_release(project: Path, limit: float = 60.0) -> bool:
    lock = project / "graphify-out" / ".refresh.lock"
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not lock.exists():
            return True
        time.sleep(0.2)
    return False


def _write_lock(project: Path, pid: int, started: float, beat: float | None = None) -> None:
    """Without `beat` the lock is one written before DEC-042, read by the legacy age rule."""
    lock = {"pid": pid, "started": int(started)}
    if beat is not None:
        lock["beat"] = int(beat)
    (project / "graphify-out" / ".refresh.lock").write_text(json.dumps(lock), encoding="utf-8")


def _check_runner(flavour: str, command: list[str]) -> None:
    root = Path(tempfile.mkdtemp(prefix=f"graph-refresh-{flavour.replace(':', '-')}-"))
    try:
        bin_dir = _fake_bin(root)

        # Gate 1: a turn that implemented nothing touches nothing.
        project = _project(root / "gate", stale=True)
        _run_hook(command, project, bin_dir, message="just an answer")
        assert_true(
            not _rows(project) and not (project / "graphify-out" / ".refresh.lock").exists(),
            f"[{flavour}] a turn without [EXECUTION RESULT] must not scan, lock, or record",
        )
        # The marker of the removed refactor skill no longer counts as an implementation.
        project = _project(root / "legacy", stale=True)
        _run_hook(command, project, bin_dir, message="[REFACTOR RESULT] done")
        assert_true(
            not _rows(project) and not (project / "graphify-out" / ".refresh.lock").exists(),
            f"[{flavour}] [REFACTOR RESULT] alone must not trigger a refresh",
        )

        # Fresh: a newer file under node_modules/ does not make the graph stale.
        project = _project(root / "fresh", stale=False)
        _run_hook(command, project, bin_dir)
        rows = _rows(project)
        assert_true(
            [row.get("outcome") for row in rows] == ["skipped_fresh"] and rows[0].get("files_visited") == 1,
            f"[{flavour}] skip directories are pruned before the walk, so only src/app.py is "
            f"visited and the graph is fresh; got {rows}",
        )

        # Stale: the hook returns before graphify finishes; the worker refreshes and records.
        project = _project(root / "stale", stale=True)
        elapsed = _run_hook(command, project, bin_dir)
        locked = (project / "graphify-out" / ".refresh.lock").exists()
        assert_true(
            elapsed < _FAKE_SLEEP_S and locked,
            f"[{flavour}] the hook must return while graphify still runs ({elapsed:.1f}s against "
            f"a {_FAKE_SLEEP_S}s graphify) and leave the refresh lock held; locked={locked}",
        )
        assert_true(
            graph_index.load_graph(project) is None,
            f"[{flavour}] a reader must not parse graph.json while the refresh lock is held: "
            "graphify rewrites it in place",
        )
        assert_true(_wait_for_release(project), f"[{flavour}] the worker must remove its lock when done")
        rows = _rows(project)
        assert_true(
            len(rows) == 1 and rows[0].get("outcome") == "refreshed"
            and rows[0].get("graphify_exit") == 1 and rows[0].get("graph_rewritten") is True,
            f"[{flavour}] a rewritten graph is a refresh even when graphify exits 1 (CASE-008); got {rows}",
        )
        assert_true(
            (graph_index.load_graph(project) or {}).get("refreshed") is True,
            f"[{flavour}] once the lock is gone the refreshed graph is readable",
        )

        # A live lock from an earlier turn: no second graphify.
        project = _project(root / "running", stale=True)
        _write_lock(project, os.getpid(), time.time())
        _run_hook(command, project, bin_dir)
        rows = _rows(project)
        assert_true(
            [row.get("outcome") for row in rows] == ["skipped_running"],
            f"[{flavour}] a refresh already running is not started twice; got {rows}",
        )

        # A lock caught half written is held while young: failing open would read the graph
        # exactly while its owner is replacing the lock.
        project = _project(root / "torn", stale=True)
        (project / "graphify-out" / ".refresh.lock").write_text('{"pid": ', encoding="utf-8")
        assert_true(
            graph_index.load_graph(project) is None,
            f"[{flavour}] an unreadable young lock keeps the graph unavailable",
        )
        _run_hook(command, project, bin_dir)
        rows = _rows(project)
        assert_true(
            [row.get("outcome") for row in rows] == ["skipped_running"],
            f"[{flavour}] an unreadable young lock is not refreshed over; got {rows}",
        )

        # No .workflow/data: the refresh still runs, it just records nothing.
        project = _project(root / "nodata", stale=True)
        shutil.rmtree(project / ".workflow")
        _run_hook(command, project, bin_dir)
        assert_true(_wait_for_release(project), f"[{flavour}] the worker finishes without a data dir")
        assert_true(
            (graph_index.load_graph(project) or {}).get("refreshed") is True
            and not (project / ".workflow").exists(),
            f"[{flavour}] a project without .workflow/data is refreshed and gets no stream",
        )

        # A lock whose holder is long gone is reclaimed.
        project = _project(root / "dead", stale=True)
        _write_lock(project, os.getpid(), time.time() - 3600)
        _run_hook(command, project, bin_dir)
        assert_true(_wait_for_release(project), f"[{flavour}] the reclaiming worker removes its lock")
        rows = _rows(project)
        assert_true(
            [row.get("outcome") for row in rows] == ["refreshed"],
            f"[{flavour}] a lock older than the limit holds nothing; got {rows}",
        )

        # No time limit (DEC-042): the old GRAPH_REFRESH_LIMIT_S no longer kills graphify, and
        # the worker beats the lock while graphify runs, naming graphify's pid.
        project = _project(root / "nolimit", stale=True)
        _run_hook(command, project, bin_dir, extra_env={"GRAPH_REFRESH_LIMIT_S": "1", "GRAPH_REFRESH_BEAT_S": "0.5"})
        # Polled, not read once: on Windows a read that lands while the worker swaps the lock
        # in (File.Replace) fails with a sharing violation, which the real reader treats as a
        # held lock; one unlucky read here failed the check about 1 run in 4.
        beating: dict = {}
        deadline = time.monotonic() + _FAKE_SLEEP_S + 1
        while time.monotonic() < deadline:
            try:
                beating = json.loads((project / "graphify-out" / ".refresh.lock").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                beating = {}
            if isinstance(beating.get("beat"), int) and beating["beat"] > beating.get("started", 0) and isinstance(beating.get("graphify_pid"), int):
                break
            time.sleep(0.1)
        assert_true(
            isinstance(beating.get("beat"), int) and beating["beat"] > beating.get("started", 0)
            and isinstance(beating.get("graphify_pid"), int),
            f"[{flavour}] the worker rewrites `beat` and names graphify while it runs; lock={beating}",
        )
        assert_true(_wait_for_release(project), f"[{flavour}] the worker removes its lock when graphify ends")
        rows = _rows(project)
        assert_true(
            [row.get("outcome") for row in rows] == ["refreshed"] and rows[0].get("graphify_ms", 0) >= _FAKE_SLEEP_S * 1000,
            f"[{flavour}] graphify runs to its end past the old limit; got {rows}",
        )

        # A lock of any age is held while its worker beats; a beat gone quiet holds nothing.
        project = _project(root / "longrun", stale=True)
        _write_lock(project, os.getpid(), time.time() - 3600, beat=time.time())
        _run_hook(command, project, bin_dir)
        assert_true(
            [row.get("outcome") for row in _rows(project)] == ["skipped_running"],
            f"[{flavour}] an hour-old lock with a fresh beat is a refresh still running; got {_rows(project)}",
        )
        project = _project(root / "quiet", stale=True)
        _write_lock(project, os.getpid(), time.time() - 3600, beat=time.time() - 300)
        _run_hook(command, project, bin_dir)
        assert_true(_wait_for_release(project), f"[{flavour}] the reclaiming worker removes its lock")
        assert_true(
            [row.get("outcome") for row in _rows(project)] == ["refreshed"],
            f"[{flavour}] a lock whose beat stopped is reclaimed; got {_rows(project)}",
        )

        # Finished, rewrote graph.json, and left it unparseable: corrupt, not refreshed.
        project = _project(root / "corrupt", stale=True)
        _run_hook(command, project, bin_dir, extra_env={"FAKE_GRAPHIFY_MODE": "corrupt", "FAKE_GRAPHIFY_SLEEP": "0"})
        assert_true(_wait_for_release(project), f"[{flavour}] the worker removes its lock after a corrupt write")
        rows = _rows(project)
        assert_true(
            [row.get("outcome") for row in rows] == ["corrupt"] and rows[0].get("graph_rewritten") is True,
            f"[{flavour}] a rewritten graph.json that does not parse is recorded as corrupt; got {rows}",
        )

        # A bad GRAPH_REFRESH_BEAT_S keeps the default interval instead of stopping the hook.
        project = _project(root / "badbeat", stale=True)
        _run_hook(command, project, bin_dir, extra_env={"GRAPH_REFRESH_BEAT_S": "soon", "FAKE_GRAPHIFY_SLEEP": "0"})
        assert_true(_wait_for_release(project), f"[{flavour}] the worker runs under the default beat")
        rows = _rows(project)
        assert_true(
            [row.get("outcome") for row in rows] == ["refreshed"],
            f"[{flavour}] an unparseable GRAPH_REFRESH_BEAT_S falls back to the default; got {rows}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_call_during_refresh() -> None:
    """A delegated call during a refresh goes without leads, and says so (RQ-13).

    Never waits: a verify that ran while the worker held the lock must record
    `refreshing`, not block on it and not pass as a project with no graph.
    """
    from exoma.core.provider.executor import graph_leads_for_call, graph_state_for_call

    root = Path(tempfile.mkdtemp(prefix="graph-leads-call-"))
    try:
        cases = [
            ("live", lambda p: _write_lock(p, os.getpid(), time.time()), "refreshing"),
            ("torn", lambda p: (p / "graphify-out" / ".refresh.lock").write_text('{"pid": ', encoding="utf-8"), "refreshing"),
            ("expired", lambda p: _write_lock(p, os.getpid(), time.time() - 3600), "empty"),
            ("beating", lambda p: _write_lock(p, 999999, time.time() - 3600, beat=time.time()), "refreshing"),
            ("quiet", lambda p: _write_lock(p, os.getpid(), time.time() - 3600, beat=time.time() - 300), "empty"),
            ("released", lambda p: None, "empty"),
            ("absent", lambda p: shutil.rmtree(p / "graphify-out"), "absent"),
        ]
        for name, arrange, expected in cases:
            project = _project(root / name, stale=False)
            arrange(project)
            started = time.monotonic()
            leads, status, ms = graph_leads_for_call(project, "app")
            waited = time.monotonic() - started
            assert_true(
                status == expected and leads is None,
                f"[{name}] graph status for a delegated call: expected {expected}, got {status}",
            )
            assert_true(
                waited < 1 and isinstance(ms, int) and ms >= 0,
                f"[{name}] the lookup must not wait on the refresh lock; took {waited:.2f}s",
            )
            # verify-browser takes no leads: same refresh judgement, no lookup.
            state, state_ms = graph_state_for_call(project)
            assert_true(
                state == {"empty": "available"}.get(expected, expected) and isinstance(state_ms, int),
                f"[{name}] graph state for a browser call: expected {expected}, got {state}",
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _sh_namespace() -> dict:
    """graph-refresh.py, loaded without running a hook (no payload)."""
    namespace: dict = {"__name__": "graph_refresh_hook"}
    saved = os.environ.pop("CLAUDE_HOOK_RAW", None)
    try:
        exec(_hook_source(), namespace)
    except SystemExit:
        pass
    finally:
        if saved is not None:
            os.environ["CLAUDE_HOOK_RAW"] = saved
    return namespace


def _check_lock_rule_agrees() -> None:
    """The hook and the reader judge a lock by the same numbers (DEC-042): held while its
    beat is younger than the stale window, a legacy lock by its age, and the interval a
    worker beats at is well inside the window. GRAPH_REFRESH_BEAT_S only shortens it."""
    import re

    sh = _hook_source()

    def number(text: str, pattern: str) -> int:
        return int(re.search(pattern, text, re.M).group(1))

    sh_beat, sh_stale, sh_legacy = (number(sh, rf"^{name} = (\d+)$") for name in ("BEAT_INTERVAL_S", "BEAT_STALE_S", "LEGACY_MAX_AGE_S"))
    reader = (graph_index._REFRESH_BEAT_STALE_SECONDS, graph_index._REFRESH_LEGACY_MAX_AGE_SECONDS)
    assert_true(
        (sh_stale, sh_legacy) == reader and 0 < sh_beat * 4 <= sh_stale,
        f"lock rule agrees across graph-refresh.py ({sh_beat}/{sh_stale}/{sh_legacy}) and core.graph.graph_index {reader}",
    )
    assert_true(
        "GRAPH_REFRESH_LIMIT_S" not in sh,
        "the hook no longer reads GRAPH_REFRESH_LIMIT_S: graphify has no time limit",
    )

    namespace = _sh_namespace()
    saved = os.environ.get("GRAPH_REFRESH_BEAT_S")
    try:
        for given, expected in (("0.5", 0.5), ("0", sh_beat), (str(sh_stale), sh_beat), ("soon", sh_beat)):
            os.environ["GRAPH_REFRESH_BEAT_S"] = given
            got = namespace["beat_interval"]()
            assert_true(got == expected, f"[py] GRAPH_REFRESH_BEAT_S={given} beats every {expected}s; got {got}")
    finally:
        if saved is None:
            os.environ.pop("GRAPH_REFRESH_BEAT_S", None)
        else:
            os.environ["GRAPH_REFRESH_BEAT_S"] = saved


class _Worker:
    def __init__(self, pid: int, exit_code):
        self.pid, self._exit_code = pid, exit_code

    def poll(self):
        return self._exit_code


def _check_hand_off_only_while_the_lock_is_ours() -> None:
    """The hook hands its lock to the worker only while it still holds it. A worker that
    already finished has removed the lock; writing it back would leave an orphan naming a
    dead pid. Missing stays missing, another hook's lock is not clobbered."""
    hand_off = _sh_namespace()["hand_off"]
    root = Path(tempfile.mkdtemp(prefix="graph-refresh-handoff-"))
    try:
        lock = root / ".refresh.lock"

        def leftovers() -> list[str]:
            return sorted(p.name for p in root.iterdir() if p.name.startswith(".refresh.lock."))

        hand_off(str(lock), "tok", _Worker(4242, None))
        assert_true(not lock.exists() and not leftovers(), "[py] a lock the worker already removed is not recreated")

        foreign = json.dumps({"pid": 1, "token": "other", "started": 1})
        lock.write_text(foreign, encoding="utf-8")
        hand_off(str(lock), "tok", _Worker(4242, None))
        assert_true(lock.read_text(encoding="utf-8") == foreign and not leftovers(), "[py] another hook's lock is left alone")

        lock.write_text(json.dumps({"pid": 7, "token": "tok", "started": 1}), encoding="utf-8")
        hand_off(str(lock), "tok", _Worker(4242, None))
        handed = json.loads(lock.read_text(encoding="utf-8"))
        assert_true(
            handed.get("pid") == 4242 and handed.get("token") == "tok" and isinstance(handed.get("beat"), int)
            and not leftovers(),
            f"[py] a lock still ours is handed to the running worker: {handed}",
        )

        lock.write_text(json.dumps({"pid": 7, "token": "tok", "started": 1}), encoding="utf-8")
        hand_off(str(lock), "tok", _Worker(4242, 0))
        assert_true(not lock.exists(), "[py] a worker that finished during the hand-off leaves no lock behind")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_doctor_and_clean_read_the_lock() -> None:
    """With no time limit a hung graphify holds the lock for as long as its worker lives, so
    doctor names the lock and clean removes it only once nothing beats it (DEC-042). clean
    never kills a process and never removes a lock a worker is beating."""
    from exoma.core.audit.diagnostics import run_doctor
    from exoma.core.runtime.state import ensure_workflow_workspace
    from exoma.core.workspace.workspace_paths import read_json_file

    root = Path(tempfile.mkdtemp(prefix="graph-refresh-clean-"))
    try:
        now = time.time()
        cases = [
            ("absent", None, "absent", False),
            ("beating", lambda p: _write_lock(p, 999999, now - 7200, beat=now), "active", False),
            ("quiet", lambda p: _write_lock(p, os.getpid(), now - 7200, beat=now - 300), "stale", True),
            ("legacy-live", lambda p: _write_lock(p, os.getpid(), now), "active", False),
            ("legacy-old", lambda p: _write_lock(p, os.getpid(), now - 3600), "stale", True),
            ("torn", lambda p: (p / "graphify-out" / ".refresh.lock").write_text('{"pid": ', encoding="utf-8"), "unreadable", False),
        ]
        for name, arrange, state, removed in cases:
            project = _project(root / name, stale=False)
            if arrange:
                arrange(project)
            got = graph_index.refresh_lock_state(project)
            assert_true(got["state"] == state, f"[{name}] lock state is {state}: {got}")
            released = graph_index.release_stale_refresh_lock(project)
            lock_left = (project / "graphify-out" / ".refresh.lock").exists()
            assert_true(
                released["removed"] is removed and lock_left is (not removed and state != "absent"),
                f"[{name}] clean removes only a stale lock: {released}, lock left={lock_left}",
            )

        workspace = root / "doctor"
        workspace.mkdir()
        ensure_workflow_workspace(workspace, os.getenv("AGENT_PATH"))
        (workspace / "graphify-out").mkdir()
        (workspace / "graphify-out" / "graph.json").write_text("{}", encoding="utf-8")
        _write_lock(workspace, 999999, now - 7200, beat=now)
        meta = run_doctor(workspace, "does-not-exist", "graph-lock")["meta"]
        checks = read_json_file(Path(meta["doctor_report"]))["checks"]
        assert_true(
            checks.get("graph_refresh_lock", {}).get("state") == "active"
            and any("graph refresh has held" in fix for fix in meta["recommended_fixes"])
            and not any(".refresh.lock" in issue for issue in meta["issues"]),
            f"doctor names a refresh running for hours as advice, not an issue: {checks.get('graph_refresh_lock')}, {meta}",
        )
        _write_lock(workspace, os.getpid(), now - 7200, beat=now - 300)
        meta = run_doctor(workspace, "does-not-exist", "graph-lock")["meta"]
        assert_true(
            any("--command clean" in fix and "no live worker" in fix for fix in meta["recommended_fixes"]),
            f"doctor points a stale refresh lock at clean: {meta['recommended_fixes']}",
        )

        # W4: Graphify instruction conflict detection (read-only, active Stop hook only, no false positives).
        fake_home = root / "agent-home"
        (fake_home / ".claude").mkdir(parents=True)
        saved_agent_home = os.environ.get("AGENT_HOME")
        saved_path = os.environ["PATH"]
        os.environ["AGENT_HOME"] = str(fake_home)
        try:
            proj_claude = workspace / "CLAUDE.md"
            proj_claude.write_text(
                "# Project rules\n"
                "- Never run graphify update manually (belongs to Stop hook).\n"
                "- If graphify-out/ is absent, offer `.graphifyignore` + `graphify update`.\n",
                encoding="utf-8",
            )
            # Without graph-refresh Stop hook registered -> inactive_hook.
            meta_inactive = run_doctor(workspace, "does-not-exist", "graph-conflict")["meta"]
            c_inactive = read_json_file(Path(meta_inactive["doctor_report"]))["checks"]["graphify_instruction_conflict"]
            assert_true(c_inactive["status"] == "inactive_hook", f"inactive hook reported cleanly: {c_inactive}")

            from exoma.core.audit.diagnostics import _is_installed_graph_refresh_command
            hooks_dir = fake_home / ".claude/hooks"
            assert _is_installed_graph_refresh_command('bash ~/".claude/hooks/graph-refresh.sh"', hooks_dir)
            assert not _is_installed_graph_refresh_command("bash '$HOME/.claude/hooks/graph-refresh.sh'", hooks_dir)

            # Non-Exoma commands (and quoted literal tilde) must NOT count as active Exoma hook.
            for fake_cmd in (
                "echo graph-refresh",
                "echo ~/.claude/hooks/graph-refresh.sh",
                'bash "~/.claude/hooks/graph-refresh.sh"',
            ):
                (fake_home / ".claude" / "settings.json").write_text(
                    json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": fake_cmd}]}]}}),
                    encoding="utf-8",
                )
                meta_foreign = run_doctor(workspace, "does-not-exist", "graph-conflict")["meta"]
                c_foreign = read_json_file(Path(meta_foreign["doctor_report"]))["checks"]["graphify_instruction_conflict"]
                assert_true(c_foreign["status"] == "inactive_hook",
                            f"foreign command {fake_cmd!r} must not count as Exoma hook: {c_foreign}")

            # Quoted absolute path with spaces in home directory IS recognized as active Exoma hook.
            spaced_home = root / "home with spaces"
            (spaced_home / ".claude").mkdir(parents=True)
            spaced_cmd = f'"E:/Program Files/Git/bin/bash.exe" "{(spaced_home / ".claude" / "hooks" / "graph-refresh.sh").as_posix()}"'
            (spaced_home / ".claude" / "settings.json").write_text(
                json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": spaced_cmd}]}]}}),
                encoding="utf-8",
            )
            os.environ["AGENT_HOME"] = str(spaced_home)
            from exoma.core.audit.diagnostics import _claude_graph_refresh_hook_state
            assert_true(_claude_graph_refresh_hook_state() == "inactive", "missing launcher is inactive")
            (spaced_home / ".claude/hooks").mkdir()
            (spaced_home / ".claude/hooks/graph-refresh.sh").touch()
            assert_true(_claude_graph_refresh_hook_state() == "active",
                        f"quoted hook path with spaces recognized as active: {spaced_cmd}")
            os.environ["AGENT_HOME"] = str(fake_home)

            # Register Exoma Stop graph-refresh hook -> safe lines produce zero conflicts.
            (fake_home / ".claude" / "settings.json").write_text(
                json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "bash ~/.claude/hooks/graph-refresh.sh"}]}]}}),
                encoding="utf-8",
            )
            (fake_home / ".claude/hooks").mkdir()
            (fake_home / ".claude/hooks/graph-refresh.sh").touch()
            from exoma.core.audit.diagnostics import _graphify_instruction_conflict
            from unittest.mock import patch
            with patch.dict(os.environ, {"PATH": str(fake_home)}):
                unavailable = _graphify_instruction_conflict(workspace)
                assert_true(unavailable["stop_hook"] == "no_graphify" and not unavailable["conflicts"],
                            "a registered hook needs the graphify executable")
            os.environ["PATH"] = str(_fake_bin(fake_home)) + os.pathsep + saved_path
            meta_clean = run_doctor(workspace, "does-not-exist", "graph-conflict")["meta"]
            c_clean = read_json_file(Path(meta_clean["doctor_report"]))["checks"]["graphify_instruction_conflict"]
            assert_true(c_clean["status"] == "none" and not c_clean["conflicts"],
                        f"negated/bootstrap lines do not false-positive: {c_clean}")

            # Add affirmative post-edit directive (including one mentioning Stop hook) -> reported read-only.
            conflict_text = (
                proj_claude.read_text(encoding="utf-8")
                + "- Auto-run `graphify update` after every code change.\n"
                + "- Always run `graphify update` after edits even if a Stop hook exists.\n"
            )
            proj_claude.write_text(conflict_text, encoding="utf-8")
            meta_hit = run_doctor(workspace, "does-not-exist", "graph-conflict")["meta"]
            c_hit = read_json_file(Path(meta_hit["doctor_report"]))["checks"]["graphify_instruction_conflict"]
            assert_true(
                c_hit["status"] == "conflict"
                and len(c_hit["conflicts"]) == 2
                and c_hit["conflicts"][0]["file"] == "CLAUDE.md"
                and c_hit["conflicts"][0]["line"] == 4
                and c_hit["conflicts"][1]["line"] == 5
                and any("CLAUDE.md:4" in fix for fix in meta_hit["recommended_fixes"])
                and not any("graph-refresh" in issue for issue in meta_hit["issues"])
                and proj_claude.read_text(encoding="utf-8") == conflict_text,
                f"affirmative post-edit conflict reported read-only: {c_hit} fixes={meta_hit['recommended_fixes']}",
            )

            examples = workspace / "CLAUDE.local.md"
            examples.write_text("```sh\nRun graphify update after edits\n```\n"
                                "Example: Run graphify update after edits.\n"
                                "<!-- Run graphify update after edits -->\n", encoding="utf-8")
            assert_true(len(_graphify_instruction_conflict(workspace)["conflicts"]) == 2, "ignore examples and comments")
            with examples.open("a", encoding="utf-8") as stream:
                stream.write(" " * (512 * 1024))
            mixed = _graphify_instruction_conflict(workspace)
            assert_true(mixed["status"] == "partial" and len(mixed["conflicts"]) == 2,
                        "keep confirmed conflicts alongside truncated observations")
            examples.unlink()

            # Unreadable project instruction file reports unavailable without appearing in checked_files.
            orig_open = Path.open
            def _deny_claude(self, *a, **kw):
                if self == proj_claude:
                    raise OSError("unreadable")
                return orig_open(self, *a, **kw)
            with patch.object(Path, "open", _deny_claude):
                c_unreadable = _graphify_instruction_conflict(workspace)
            assert_true(
                c_unreadable["status"] == "unavailable"
                and "CLAUDE.md" not in c_unreadable["checked_files"]
                and c_unreadable.get("unreadable_files") == ["CLAUDE.md"],
                f"all-unreadable instruction files report unavailable: {c_unreadable}",
            )
        finally:
            os.environ["PATH"] = saved_path
            if saved_agent_home is None:
                os.environ.pop("AGENT_HOME", None)
            else:
                os.environ["AGENT_HOME"] = saved_agent_home
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_source_extensions_agree() -> None:
    """The reader calls a graph stale over the same files the hook would refresh it for.

    The reader fingerprinted `*.py` only while the hook scanned eight languages, so in a
    PHP project one said "current" about a graph the other was about to rebuild.
    """
    import ast
    import re

    block = re.search(r"^SOURCE_EXT = (\([^)]*\))", _hook_source(), re.M | re.S)
    hook_ext = ast.literal_eval(block.group(1)) if block else None
    assert_true(
        hook_ext == graph_index.SOURCE_EXTENSIONS,
        f"graph-refresh.py SOURCE_EXT {hook_ext} == graph_index.SOURCE_EXTENSIONS",
    )
    # The skip sets diverged once (vendor, storage): each side then judged a different
    # file set, so the reader warned "stale" about a graph the hook called current.
    block = re.search(r"^SKIP_DIRS = (\{[^}]*\})", _hook_source(), re.M | re.S)
    hook_skip = ast.literal_eval(block.group(1)) if block else None
    assert_true(
        hook_skip == graph_index._SKIP_DIRS,
        f"graph-refresh.py SKIP_DIRS {hook_skip} == graph_index._SKIP_DIRS",
    )
    root = Path(tempfile.mkdtemp(prefix="graph-ext-"))
    try:
        (root / "app.php").write_text("<?php\n", encoding="utf-8")
        for skipped in ("node_modules", "vendor", "storage"):
            (root / skipped).mkdir()
            (root / skipped / "dep.php").write_text("<?php\n", encoding="utf-8")
        state = graph_index._source_state(root)
        assert_true(
            state is not None,
            "a project with no Python still has a source fingerprint",
        )
        for skipped in ("node_modules", "vendor", "storage"):
            os.utime(root / skipped / "dep.php", None)
            assert_true(
                graph_index._source_state(root)[0] == state[0],
                f"{skipped}/ is pruned, so a file under it does not move the fingerprint",
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_graph_refresh_hook() -> None:
    _check_source_extensions_agree()
    _check_lock_rule_agrees()
    _check_doctor_and_clean_read_the_lock()
    _check_hand_off_only_while_the_lock_is_ours()
    _check_call_during_refresh()
    for flavour, command in _runners():
        _check_runner(flavour, command)
