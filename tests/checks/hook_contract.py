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

from tests.checks.hook_repro import check_repro_matrix
from tests.checks.support import assert_true
from exoma.utils import osutil
from tests.checks.support import no_console

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / "src" / "exoma" / "_bundle" / "config" / "hosts" / "claude" / "hooks"
K5 = REPO / "tests" / "fixtures" / "hooks" / "gate_k5.json"
# settings.template.json: graph-refresh 60s, task-events 10s, gate set/check 15s; session-bind
# and the statusline carry none, so the shortest one stands in for them.
_TIMEOUT_S = {
    "graph-refresh": 60, "task-events": 10, "intent-gate-set": 15, "intent-gate-check": 15,
    "session-bind": 10, "workflow-statusline": 10, "execution-gate": 10, "status-guard": 10,
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

    def __init__(self, label: str, main_id: str = "m1") -> None:
        self.base = Path(tempfile.mkdtemp(prefix=f"hook-contract-{label}-"))
        self.home, self.project = self.base / "home", self.base / "project"
        (self.home / ".claude").mkdir(parents=True)
        from exoma.core.workspace.workspace_paths import workflow_paths
        self.main_id = main_id
        self.runtime = workflow_paths(self.project, main_id)["runtime_dir"]
        self.runtime.mkdir(parents=True)
        self.registry = self.home / ".claude" / "session_registry.json"
        self.registry.write_bytes(
            json.dumps({"c1": {"main_session_id": main_id, "cwd": str(self.project)}}).encode("utf-8")
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
            **no_console(),
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
    # DEC-068: `/branch` and `/fork` start a SessionStart with source=fork under a new session
    # id. The fork is its own session: sharing the parent's let a prompt in one window clear
    # the commit approval the other was about to use.
    run({"source": "fork", "session_id": "c3", "cwd": str(ws.project), "hook_event_name": "SessionStart"})
    registry = json.loads(ws.registry.read_text(encoding="utf-8-sig"))
    forked = (registry.get("c3") or {}).get("main_session_id")
    assert_true(forked and forked != registry["c2"]["main_session_id"],
                f"a fork gets its own MAIN_SESSION_ID: {registry.get('c3')}")


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

    token, commit = ws.runtime / "execute.token", ws.runtime / "commit.marker"
    from exoma.core.runtime import execution as execution_runtime
    execution_runtime.record_plan(ws.project, ws.main_id, {
        "task": "hook approval binding", "decision": "proceed",
        "confidence": {key: "high" for key in execution_runtime.CONFIDENCE_KEYS},
        "open_questions": [], "files": ["core/a.py"],
        "options": [{"name": "scoped", "minus": "requires approval", "recommended": True}],
    })
    for prompt, wants_token in (
        ("kerjakan", False),
        ("/.execute", False),
        ("/.execute -y", True),
        ("<command-message>.execute</command-message>\n<command-name>/.execute</command-name>\n<command-args>-y</command-args>", True),
    ):
        token.unlink(missing_ok=True)
        run({"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "UserPromptSubmit", "prompt": prompt})
        assert_true(token.is_file() == wants_token, f"only the user's `/.execute -y` writes execute.token: {prompt!r}")
        if wants_token:
            saved_token = json.loads(token.read_text(encoding="utf-8"))
            assert_true(saved_token.get("plan_binding") == execution_runtime.approval_binding(ws.project, ws.main_id),
                        "standalone hook and runtime bind the same plan revision and scope")
    run({"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "UserPromptSubmit", "prompt": "tolong commit perubahan ini"})
    assert_true(commit.is_file(), "a prompt asking to commit writes commit.marker")
    run({"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "UserPromptSubmit", "prompt": "yes"})
    assert_true(commit.is_file(), "the yes to the proposed message keeps the approval")
    run({"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "UserPromptSubmit", "prompt": "jangan commit dulu, cek lagi"})
    assert_true(not commit.is_file(), "a turn that does not ask to commit (or says not to) clears it")
    token.unlink(missing_ok=True)
    for prompt in ("gas commit", "tolong commit perubahan ini", "please commit these changes"):
        run({"source": "future_host_source", "session_id": "c1", "cwd": str(ws.project),
             "hook_event_name": "UserPromptSubmit", "prompt": prompt})
        assert_true(commit.is_file() and not token.exists(),
                    "a direct commit request under an unknown source never authorizes execute: " + prompt)
    for prompt in ("/.execute", "gas /.execute -y", "<skill>commit changes</skill>",
                   "```text\ncommit changes\n```", '"commit changes"', "jangan commit"):
        run({"source": "future_host_source", "session_id": "c1", "cwd": str(ws.project),
             "hook_event_name": "UserPromptSubmit", "prompt": prompt})
        assert_true(not commit.exists() and not token.exists(),
                    "examples cannot approve commit, and execute still requires its anchored command: " + prompt)
    token.unlink(missing_ok=True)
    run({"source": "system", "session_id": "c1", "cwd": str(ws.project), "hook_event_name": "UserPromptSubmit", "prompt": "/.execute -y"})
    assert_true(not token.is_file(), "a turn the user did not write approves nothing")


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

    # Anchor reads: a synthesis written from a plan/analyze digest without opening any cited
    # anchor is named once per run; a Read in the transcript, after the runner call, that
    # overlaps a cited range clears it.
    (ws.project / "src").mkdir(exist_ok=True)
    app = ws.project / "src" / "app.py"
    app.write_text("".join(f"line {n}\n" for n in range(1, 31)), encoding="utf-8")
    logs = ws.runtime.parent / "logs"
    first = logs / "20261025_000001_000000_analyze_aa11"
    first.mkdir(parents=True)
    (first / "output.raw.md").write_text("- claim [src/app.py:10-12] and [missing.py:3]\n", encoding="utf-8")
    transcript = ws.base / "transcript.jsonl"

    def tool(name: str, role: str = "assistant", **tool_input) -> str:
        block = {"type": "tool_use", "id": "t", "name": name, "input": tool_input}
        return json.dumps({"type": role, "message": {"role": role, "content": [block]}})

    def runner(command: str) -> str:
        return tool("Bash", command=f"bash '{ws.project.as_posix()}/.workflow/run.sh' {command} 'task' 'sid'")

    def calls(*lines: str) -> None:
        transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    calls(runner("analyze"))
    stop = {"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "Stop", "transcript_path": str(transcript)}
    code, out, _ = run({**stop, "transcript_path": str(ws.base / "absent.jsonl"), "last_assistant_message": "**[ANALYSIS RESULT]**\nx"})
    assert_true(code == 0 and not out.strip(), f"an unreadable transcript warns nothing: {out[:200]!r}")
    code, out, _ = run({**stop, "last_assistant_message": "Ringkasan biasa tanpa blok."})
    assert_true(code == 0 and not out.strip(), f"a reply without its own [PLAN]/[ANALYSIS RESULT] is not judged: {out[:200]!r}")
    code, out, _ = run({**stop, "last_assistant_message": "```\n[ANALYSIS RESULT]\n```"})
    assert_true(code == 0 and not out.strip(), f"a fenced block is not this reply's synthesis: {out[:200]!r}")
    code, out, _ = run({**stop, "last_assistant_message": "**[ANALYSIS RESULT]**\nroot cause ..."})
    assert_true(code == 0 and "ANCHOR CHECK" in out, f"a synthesis with no anchor opened is named: {out[:200]!r}")
    code, out, _ = run({**stop, "last_assistant_message": "**[ANALYSIS RESULT]**\nagain"})
    assert_true(code == 0 and not out.strip(), f"the warning fires once per run: {out[:200]!r}")

    second = logs / "20261025_000002_000000_plan_bb22"
    second.mkdir(parents=True)
    (second / "output.raw.md").write_text("[src/app.py:10-12]\n", encoding="utf-8")
    plan = {**stop, "last_assistant_message": "[PLAN]\ntask: ..."}
    overlapping = tool("Read", file_path=str(app), offset=5, limit=10)
    calls(runner("analyze"), overlapping, runner("plan"), tool("Bash", command="bash '.workflow/run.sh' plan-record 'p' 'sid'"),
          tool("Read", file_path=str(app), offset=20, limit=5))
    code, out, _ = run(plan)
    assert_true(code == 0 and "ANCHOR CHECK" in out,
                f"a Read before the newest runner call, or outside the cited range, opens nothing: {out[:200]!r}")
    (ws.runtime / "anchor-reads.json").unlink()
    calls(runner("plan"), overlapping)
    code, out, _ = run(plan)
    assert_true(code == 0 and not out.strip(), f"a synthesis after opening an anchor is not named: {out[:200]!r}")
    calls(tool("Read", file_path=str(app)))
    code, out, _ = run(plan)
    assert_true(code == 0 and not out.strip(), f"a transcript without the runner call warns nothing: {out[:200]!r}")

    # Read backward across several chunks, CRLF endings, a Windows runner path, a malformed
    # Read beside a valid one, and a tool_use outside an assistant message.
    result = json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "x" * 200_000}]}})
    windows_runner = tool("Bash", command=f"bash '{ws.project}\\.workflow\\run.sh' plan 'task' 'sid'")
    malformed = tool("Read", file_path=str(app), offset="ten")
    transcript.write_text("\r\n".join([runner("analyze"), overlapping, windows_runner, result, malformed,
                                       result, overlapping, result]) + "\r\n", encoding="utf-8")
    code, out, _ = run(plan)
    assert_true(code == 0 and not out.strip(),
                f"an overlapping Read after a Windows runner call, past large results, counts: {out[:200]!r}")
    (ws.runtime / "anchor-reads.json").unlink(missing_ok=True)
    calls(windows_runner, result, malformed, tool("Read", role="user", file_path=str(app), offset=5, limit=10), result)
    code, out, _ = run(plan)
    assert_true(code == 0 and "ANCHOR CHECK" in out,
                f"a malformed Read or a non-assistant tool_use opens nothing: {out[:200]!r}")


def _case_statusline(ws: _Workspace, run) -> None:
    code, out, _ = run({"session_id": "c1", "workspace": {"project_dir": str(ws.project)}})
    lines = [line for line in out.splitlines() if line.strip()]
    assert_true(code == 0 and len(lines) == 1, f"the statusline prints one line and exits 0: exit {code}, {out!r}")


def _case_graph_refresh(ws: _Workspace, run) -> None:
    code, out, _ = run({"last_assistant_message": "Halo, ini jawaban biasa.", "cwd": str(ws.project), "hook_event_name": "Stop"})
    assert_true(code == 0, f"graph-refresh exits 0, got {code}")
    _silent(ws, out, "graph-refresh")
    assert_true(not (ws.project / "graphify-out" / ".refresh.lock").exists(), "graph-refresh takes no lock for a turn that changed no code")


def _case_execution_gate(ws: _Workspace, run) -> None:
    def write(path: str) -> int:
        code, _, _ = run({"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "PreToolUse",
                          "tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}})
        return code

    # The workspace itself sits in the system temp dir, which the gate allows; point the
    # hook's temp dir elsewhere so a sibling directory reads as another project.
    scratch = ws.base / "tmp"
    scratch.mkdir()
    ws.env.update(TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
    inside, other = str(ws.project / "core" / "a.py"), str(ws.base / "sibling" / "b.py")
    assert_true(write(inside) == 0, "with no active execution a write inside the root passes")
    assert_true(write(other) == 2 and "outside the session's project root" in ws.last_stderr,
                f"a write into another project is blocked: {ws.last_stderr[:200]!r}")
    assert_true(write(str(ws.home / ".claude" / "memory" / "m.md")) == 0, "~/.claude stays writable")
    (ws.base / "sibling").mkdir()
    link = ws.project / "linked"
    try:
        os.symlink(ws.base / "sibling", link, target_is_directory=True)
    except (OSError, NotImplementedError):
        # No symlink privilege (Windows without developer mode): a junction needs none.
        made = os.name == "nt" and subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(ws.base / "sibling")],
            capture_output=True, **osutil.hidden_run_kwargs(),
        ).returncode == 0
        link = link if made else None
    if link is not None:
        assert_true(write(str(link / "b.py")) == 2 and "outside the session's project root" in ws.last_stderr,
                    f"a link inside the root does not carry a write out of it: {ws.last_stderr[:200]!r}")
        os.rmdir(link) if os.name == "nt" else link.unlink()

    execution = ws.runtime / "execution.json"
    execution.write_text(json.dumps({"status": "active", "plan_id": "p1", "scope": ["core/a.py", "tests/"]}), encoding="utf-8")
    assert_true(write(inside) == 0 and write(str(ws.project / "tests" / "t.py")) == 0,
                "an active execution allows its scope: exact files and directory prefixes")
    assert_true(write(str(ws.project / "docs" / "x.md")) == 2 and "outside the active execution's scope" in ws.last_stderr,
                f"and blocks everything else in the root: {ws.last_stderr[:200]!r}")
    assert_true(write(str(ws.project / ".workflow" / "data" / "x.json")) == 0, ".workflow/ is always writable")
    alias = ws.project / "alias"
    (ws.project / "tests").mkdir(exist_ok=True)
    try:
        os.symlink(ws.project / "tests", alias, target_is_directory=True)
        aliased = True
    except (OSError, NotImplementedError):
        aliased = False
    if aliased:
        execution.write_text(json.dumps({"status": "active", "plan_id": "p1", "scope": ["alias/"]}), encoding="utf-8")
        assert_true(write(str(alias / "t.py")) == 0,
                    "a scope named through an in-root link matches the path the plan wrote")
        alias.unlink()
        execution.write_text(json.dumps({"status": "active", "plan_id": "p1", "scope": ["core/a.py", "tests/"]}), encoding="utf-8")
    execution.write_text(json.dumps({"status": "implemented", "plan_id": "p1", "scope": ["core/a.py"]}), encoding="utf-8")
    assert_true(write(str(ws.project / "docs" / "x.md")) == 0, "a closed execution no longer narrows writes")

    commit = {"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "PreToolUse",
              "tool_name": "Bash", "tool_input": {"command": "git -C . commit -m 'x'"}}
    code, _, _ = run(commit)
    assert_true(code == 2 and "git commit" in ws.last_stderr, f"an unasked commit is blocked, got {code}")
    for shaped in ('git -C "repo with spaces" commit -m x', "git --git-dir='a b/.git' commit", "git -c user.name=x commit"):
        code, _, _ = run({**commit, "tool_input": {"command": shaped}})
        assert_true(code == 2, f"a quoted or optioned commit is still a commit: {shaped!r} -> {code}")
    import datetime as _dt
    stale = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)).isoformat()
    (ws.runtime / "commit.marker").write_text(json.dumps({"set_at": stale}), encoding="utf-8")
    code, _, _ = run(commit)
    assert_true(code == 2, f"a commit marker from hours ago approves nothing, got {code}")
    fresh = _dt.datetime.now(_dt.timezone.utc).isoformat()
    (ws.runtime / "commit.marker").write_text(json.dumps({"set_at": fresh}), encoding="utf-8")
    code, out, _ = run(commit)
    assert_true(code == 0, f"a commit the user asked for passes, got {code}")
    _silent(ws, out, "execution-gate")
    code, _, _ = run({**commit, "tool_input": {"command": "git status"}})
    assert_true(code == 0, "other git commands are not the gate's business")

    config = ws.project / ".workflow" / "config.json"
    config.write_text(json.dumps({"policies": {"enforcement": "warn"}}), encoding="utf-8")
    code, out, _ = run({"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "PreToolUse",
                        "tool_name": "Write", "tool_input": {"file_path": other}})
    assert_true(code == 0 and "EXECUTION GATE: warn" in out, f"warn mode reports and never blocks: {code} {out[:120]!r}")

    # Every refusal is logged for EXP-014: rule, tool, mode, exit and plan, never the target.
    log = ws.runtime / "gate-blocks.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    rules = [r["rule"] for r in rows]
    assert_true(
        rules.count("commit_unapproved") == 5 and rules.count("outside_scope") == 1
        and rules.count("outside_root") >= 2,
        f"each refusal is one row, under the rule that refused: {rules}",
    )
    scope_row = next(r for r in rows if r["rule"] == "outside_scope")
    assert_true(
        scope_row["tool"] == "Write" and scope_row["exit"] == 2 and scope_row["plan_id"] == "p1"
        and scope_row["mode"] == "hybrid",
        f"a block names its tool, exit and the plan it enforced: {scope_row}",
    )
    assert_true(
        rows[-1]["mode"] == "warn" and rows[-1]["exit"] == 0,
        f"a warning is logged as one, with the exit it really had: {rows[-1]}",
    )
    text = log.read_text(encoding="utf-8")
    assert_true(
        "sibling" not in text and "docs/x.md" not in text and "commit -m" not in text,
        "the log holds no path and no command",
    )

    config.write_text(json.dumps({"policies": {"enforcement": "off"}}), encoding="utf-8")
    code, out, _ = run({"session_id": "c1", "cwd": str(ws.project), "tool_name": "Write",
                        "tool_input": {"file_path": other}})
    assert_true(code == 0 and not out.strip(), "intentional off remains silent and allows")
    skips = ws.runtime / "gate-skips.jsonl"
    off_rows = [json.loads(line) for line in skips.read_text(encoding="utf-8").splitlines()]
    assert_true(off_rows[-1]["reason"] == "enforcement_off", "intentional off has an explicit observation")

    payload = {"session_id": "unbound", "cwd": str(ws.project), "tool_name": "Write",
               "tool_input": {"file_path": other, "content": "not-for-the-log"}}
    code, out, _ = run(payload)
    assert_true(code == 0 and "EXECUTION GATE: unavailable" in out,
                "missing binding allows with a visible protection warning")
    run(payload)
    code, out, _ = run({k: v for k, v in payload.items() if k != "session_id"})
    assert_true(code == 0 and "session_id_unavailable" in out, "missing session id is reported")
    shared = ws.home / ".claude" / "gate-skips.jsonl"
    shared_rows = [json.loads(line) for line in shared.read_text(encoding="utf-8").splitlines()]
    assert_true(sum(r["reason"] == "session_or_workspace_unavailable" for r in shared_rows) == 1,
                "repeated missing-binding observations are deduplicated")
    assert_true(all(r.get("root_digest") and not r.get("root") for r in shared_rows),
                "shared fallback rows use a digest instead of a raw project path")
    assert_true("not-for-the-log" not in shared.read_text(encoding="utf-8")
                and other not in shared.read_text(encoding="utf-8"), "fallback logs exclude tool inputs")
    plain = ws.base / "plain-project"
    (plain / ".git").mkdir(parents=True)
    before_plain = shared.read_text(encoding="utf-8")
    code, out, _ = run({**payload, "cwd": str(plain)})
    assert_true(code == 0 and not out.strip() and shared.read_text(encoding="utf-8") == before_plain,
                "a global hook stays silent and records no failure outside Exoma workspaces")
    subdirectory = ws.project / "subdirectory"
    subdirectory.mkdir()
    code, out, _ = run({**payload, "session_id": "unbound-subdir", "cwd": str(subdirectory)})
    latest = json.loads(shared.read_text(encoding="utf-8").splitlines()[-1])
    assert_true(code == 0 and "unavailable" in out and latest["root_digest"] == shared_rows[0]["root_digest"],
                "subdirectory failures are attributed to the owning workflow root")
    registry = json.loads(ws.registry.read_text(encoding="utf-8"))
    registry["bound-subdir"] = {"main_session_id": "subdir-session", "cwd": str(subdirectory)}
    ws.registry.write_text(json.dumps(registry), encoding="utf-8")
    code, out, _ = run({**payload, "session_id": "bound-subdir", "cwd": str(subdirectory)})
    assert_true(code == 0 and "unavailable" in out,
                "a subfolder binding is reported unavailable, not silently rebound to another runtime")
    assert_true(not (subdirectory / ".workflow").exists(), "reporting never creates another workflow")
    code, out, _ = run(["invalid-payload"])
    assert_true(code == 0 and "hook_exception" in out, "unexpected payloads fail open visibly")

    from unittest.mock import patch
    from exoma.core.audit import diagnostics
    from exoma.core.workspace.workspace_paths import workflow_paths

    with patch.dict(os.environ, {"HOME": str(ws.home), "USERPROFILE": str(ws.home)}):
        report = diagnostics.run_doctor(ws.project, "exoma-fixture-no-provider", "m1")
        report_path = workflow_paths(ws.project, "m1")["doctor_report"]
        checks = json.loads(report_path.read_text(encoding="utf-8"))["checks"]
    gate_status = checks["execution_gate"]
    assert_true(gate_status["configuration_status"] == "disabled"
                and "session_or_workspace_unavailable" in gate_status["recent_fail_open"],
                f"doctor distinguishes off configuration and recorded failures: {gate_status}")


def _case_status_guard(ws: _Workspace, run) -> None:
    stop = {"session_id": "c1", "cwd": str(ws.project), "hook_event_name": "Stop"}
    code, out, _ = run({**stop, "last_assistant_message": "Semua selesai."})
    assert_true(code == 0, f"status-guard never blocks, got {code}")
    _silent(ws, out, "status-guard")  # no execution recorded: nothing to guard
    (ws.runtime / "execution.json").write_text(
        json.dumps({"status": "implemented", "plan_id": "p1", "verification": "not_run"}), encoding="utf-8"
    )
    # DEC-068: the guard reads the [EXECUTION RESULT] fields skills/execute.md defines, never
    # prose. The prose below fired the word list on every turn of a real session.
    for prose in ("Begitu ketiganya selesai, saya akan menilai biaya.", "Hasil menyusul begitu selesai.",
                  "> job selesai, 12 test lolos", "```\n[exited] done\n```", "Blok 3 selesai, semua berjalan."):
        code, out, _ = run({**stop, "last_assistant_message": prose})
        assert_true(code == 0 and not out.strip(), f"prose is not judged: {prose!r} -> {out[:200]!r}")
    result = "[EXECUTION RESULT]\n- files_changed: 3\n- verification: %s\n- **status:** %s\n\n[NEXT] /.verify"
    code, out, _ = run({**stop, "last_assistant_message": result % ("not_run", "implemented")})
    assert_true(code == 0 and not out.strip(), f"the honest status fields are not a claim: {out[:200]!r}")
    code, out, _ = run({**stop, "last_assistant_message": result % ("not_run", "done")})
    assert_true(code == 0 and "STATUS GUARD" in out and "status: done" in out,
                f"status: done before verify is reported: {out[:200]!r}")
    code, out, _ = run({**stop, "last_assistant_message": result % ("pass", "implemented")})
    assert_true(code == 0 and "verification: pass" in out, f"a verification the runtime did not record is reported: {out[:200]!r}")
    # Someone else's result relayed in the reply — quoted, fenced, or named mid-sentence — is
    # not this reply's claim.
    relayed = (
        "> [EXECUTION RESULT]\n> - verification: pass\n> - status: done",
        "```\n[EXECUTION RESULT]\nstatus: done\n```",
        "The subagent printed [EXECUTION RESULT] with status: done earlier.",
        "Its output:\n```\n[EXECUTION RESULT]\nstatus: done\n",  # a fence never closed
    )
    for text in relayed:
        code, out, _ = run({**stop, "last_assistant_message": text})
        assert_true(code == 0 and not out.strip(), f"a relayed result is not judged: {text!r} -> {out[:200]!r}")
    code, out, _ = run({**stop, "last_assistant_message": "- [EXECUTION RESULT]\n  - status: done"})
    assert_true(code == 0 and "status: done" in out, f"the reply's own block opened as a list item is read: {out[:200]!r}")
    (ws.runtime / "execution.json").write_text(
        json.dumps({"status": "implemented", "plan_id": "p1", "verification": "pass"}), encoding="utf-8"
    )
    code, out, _ = run({**stop, "last_assistant_message": "Done."})
    assert_true(code == 0 and not out.strip(), "after a passing verify, done is allowed")


_CASES = {
    "execution-gate": _case_execution_gate,
    "status-guard": _case_status_guard,
    "session-bind": _case_session_bind,
    "intent-gate-set": _case_gate_set,
    "intent-gate-check": _case_gate_check,
    "task-events": _case_task_events,
    "workflow-statusline": _case_statusline,
    "graph-refresh": _case_graph_refresh,
}


def _check_hashed_session_approval() -> None:
    """The real hook launchers must approve and gate the runtime's hashed session."""
    from exoma.core.runtime import execution as execution_runtime

    for main_id in ("main_Mixed Project_03ef", "main_\u65e5\u672c\u8a9e_03ef--6cf57e7e3cc7"):
        ws = _Workspace("hashed-session", main_id)
        try:
            commands = {hook: _flavours(hook)[0][1] for hook in ("intent-gate-set", "intent-gate-check")}
            _case_gate_set(ws, lambda payload: ws.run(commands["intent-gate-set"], payload))
            _case_gate_check(ws, lambda payload: ws.run(commands["intent-gate-check"], payload))
            code, _, _ = ws.run(commands["intent-gate-set"], {
                "session_id": "c1", "cwd": str(ws.project), "hook_event_name": "UserPromptSubmit",
                "prompt": "/.execute -y",
            })
            assert_true(code == 0 and execution_runtime.begin_execution(ws.project, main_id)["ok"],
                        "a fresh hook approval opens the plan in the runtime's canonical session")
            assert_true(not (ws.runtime / "execute.token").exists()
                        and execution_runtime.begin_execution(ws.project, main_id).get("reason") == "not_approved",
                        "hook approval is consumed once in a hashed session")
            raw = ws.project / ".workflow/data/sessions" / main_id
            assert_true(not raw.exists(), "hooks must not create a second raw-ID session directory")
        finally:
            ws.close()


def _check_fork_execution_approval() -> None:
    """A forked session needs its own plan and its own `/.execute -y`, through the real launchers.

    DEC-068 gave a fork its own MAIN_SESSION_ID; this follows it through to execution. The
    parent's approval opens nothing in the fork, not even when copied into the fork's
    runtime, and the fork's own approval leaves the parent's untouched.
    """
    from exoma.core.runtime import execution as execution_runtime
    from exoma.core.workspace.workspace_paths import workflow_paths

    ws = _Workspace("fork-approval")
    try:
        bind, gate_set = _flavours("session-bind")[0][1], _flavours("intent-gate-set")[0][1]

        def start(source: str, host_id: str) -> str:
            code, _, _ = ws.run(bind, {"source": source, "session_id": host_id, "cwd": str(ws.project),
                                       "hook_event_name": "SessionStart"})
            assert_true(code == 0, f"session-bind ({source}) exits 0, got {code}")
            registry = json.loads(ws.registry.read_text(encoding="utf-8-sig"))
            bound = (registry.get(host_id) or {}).get("main_session_id")
            assert_true(bound, f"session-bind ({source}) records {host_id}: {registry}")
            return bound

        def approve(host_id: str) -> None:
            code, _, _ = ws.run(gate_set, {"session_id": host_id, "cwd": str(ws.project),
                                           "hook_event_name": "UserPromptSubmit", "prompt": "/.execute -y"})
            assert_true(code == 0, f"intent-gate-set exits 0 for {host_id}, got {code}")

        def plan() -> dict:
            return {
                "task": "fork approval", "decision": "proceed",
                "confidence": {key: "high" for key in execution_runtime.CONFIDENCE_KEYS},
                "open_questions": [], "files": ["core/a.py"],
                "options": [{"name": "scoped", "minus": "requires approval", "recommended": True}],
            }

        def begin(session: str) -> dict:
            return execution_runtime.begin_execution(ws.project, session)

        parent, child = start("startup", "p1"), start("fork", "f1")
        assert_true(parent != child, f"the fork is bound to its own MAIN_SESSION_ID: {parent} / {child}")
        parent_rt, child_rt = (workflow_paths(ws.project, s)["runtime_dir"] for s in (parent, child))

        execution_runtime.record_plan(ws.project, parent, plan())
        approve("p1")
        assert_true((parent_rt / "execute.token").is_file(), "the parent's `/.execute -y` writes the parent's token")

        assert_true(begin(child).get("reason") == "no_plan", "the fork does not inherit the parent's plan")
        execution_runtime.record_plan(ws.project, child, plan())
        assert_true(begin(child).get("reason") == "not_approved",
                    "an identical plan in the fork is not approved by the parent's `-y`")
        child_rt.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(parent_rt / "execute.token", child_rt / "execute.token")
        assert_true(begin(child).get("reason") == "not_approved",
                    "the parent's token copied into the fork's runtime is bound to the parent and refused")
        # Copy the parent's plan as well: every binding field now matches except the runtime
        # location, so this refusal is the runtime field's alone.
        shutil.copyfile(parent_rt / execution_runtime.PLAN_FILE, child_rt / execution_runtime.PLAN_FILE)
        parent_binding = execution_runtime.approval_binding(ws.project, parent)
        child_binding = execution_runtime.approval_binding(ws.project, child)
        without_runtime = lambda binding: {k: v for k, v in binding.items() if k != "runtime"}  # noqa: E731
        assert_true(without_runtime(parent_binding) == without_runtime(child_binding)
                    and parent_binding["runtime"] != child_binding["runtime"],
                    f"a copied plan binds alike except for its runtime: {parent_binding} / {child_binding}")
        assert_true(begin(child).get("reason") == "not_approved",
                    "a copied plan and token are refused on the runtime location alone")

        approve("f1")
        assert_true(begin(child)["ok"], "the fork's own `/.execute -y` opens the fork's plan")
        assert_true((parent_rt / "execute.token").is_file() and begin(parent)["ok"],
                    "the fork's approval leaves the parent's approval intact and usable")
    finally:
        ws.close()


def _check_alias_execution_approval() -> None:
    """Unchanged host-issued approval is consumed through a physical runtime alias."""
    from unittest.mock import patch
    from exoma.core.runtime import execution as execution_runtime

    ws = _Workspace("alias-approval")
    try:
        execution_runtime.record_plan(ws.project, ws.main_id, {
            "task": "alias approval", "decision": "proceed",
            "confidence": {key: "high" for key in execution_runtime.CONFIDENCE_KEYS},
            "open_questions": [], "files": ["core/a.py"],
            "options": [{"name": "scoped", "minus": "requires identity proof", "recommended": True}],
        })
        gate_set = _flavours("intent-gate-set")[0][1]
        code, _, _ = ws.run(gate_set, {"session_id": "c1", "cwd": str(ws.project),
                                      "hook_event_name": "UserPromptSubmit", "prompt": "/.execute -y"})
        token = ws.runtime / "execute.token"
        assert_true(code == 0 and token.is_file(), "unchanged standalone launcher publishes fixture approval")
        issued = json.loads(token.read_text())["plan_binding"]
        plan = ws.runtime / execution_runtime.PLAN_FILE
        before = plan.read_bytes()
        alias = str(ws.runtime / ".." / ws.runtime.name)
        assert_true(alias != issued["runtime"] and Path(alias).samefile(issued["runtime"]),
                    "host integration uses genuinely equal directories with different text")
        original = execution_runtime.approval_binding
        def consumer_spelling(*args, **kwargs):
            binding = original(*args, **kwargs)
            return {**binding, "runtime": alias} if binding is not None else None
        # Simulate the consumer's alternate path spelling without changing the
        # host producer, plan or issued token. All identity checks remain real.
        with patch.object(execution_runtime, "approval_binding", side_effect=consumer_spelling):
            begun = execution_runtime.begin_execution(ws.project, ws.main_id)
        assert_true(begun["ok"] and not token.exists() and plan.read_bytes() == before,
                    "real host approval opens one execution across a proven alias without rewriting the plan")
        assert_true(execution_runtime.begin_execution(ws.project, ws.main_id).get("reason") == "not_approved",
                    "host approval remains single-use after alias admission")
    finally:
        ws.close()


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
    # The failures reproduced from real sessions (DEC-068), through the same launchers.
    check_repro_matrix()
    _check_hashed_session_approval()
    _check_fork_execution_approval()
    _check_alias_execution_approval()


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
