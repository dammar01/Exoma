"""Same product as host and second agent: the child is marked, isolated, and cannot recurse.

A host and a provider may be the same CLI (Claude on Claude, Codex on Codex, ...). Three
things keep the delegated child from acting as the user's host:

- every adapter marks its child `WORKFLOW_AGENT_ROLE=second_agent`;
- the host hooks skip a marked process (covered per host by the `codex-host`,
  `opencode-host` and `agy-host` suites; the OpenCode plugin source is re-checked here);
- the runtime refuses a marked process that asks to delegate, queue or run a job, or
  change execution state (`recursion_refused`).

All offline: no provider CLI is started and no quota is spent.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.checks.support import assert_true, no_console

REPO = Path(__file__).resolve().parents[2]
ROLE = "WORKFLOW_AGENT_ROLE"


class _Spawned(Exception):
    """Raised by the fake Popen once it has seen the child's environment."""


def _child_env_of(adapter) -> dict:
    seen: dict = {}

    def fake_popen(*_args, **kwargs):
        seen.update(kwargs.get("env") or {})
        raise _Spawned()

    # agy takes its prompt on argv, so its capture has no prompt parameter.
    call = dict(args=["provider-cli"], cwd=None, timeout=5, phase="test", on_session=None)
    if getattr(adapter, "adapter", "") != "agy":
        call["prompt"] = "prompt"
    with patch.object(subprocess, "Popen", fake_popen):
        try:
            adapter._popen_capture(**call)
        except _Spawned:
            pass
    return seen


def _cli(args: list[str], role: str | None, cwd: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    env.pop(ROLE, None)
    if role:
        env[ROLE] = role
    proc = subprocess.run([sys.executable, "-m", "exoma", *args], cwd=cwd, env=env, text=True,
                          capture_output=True, timeout=60, **no_console())
    return proc.returncode, proc.stdout


def _test_recursion_guard() -> None:
    from exoma import cli
    from exoma.core.evidence.contract import ERROR_TYPES

    assert_true("recursion_refused" in ERROR_TYPES, "recursion_refused is a registered error_type")

    refused = [
        (["explore", "--prompt", "x", "--session", "s"], 1),
        (["--command", "plan", "--prompt", "x", "--session", "s"], 1),
        (["--command", "verify", "--prompt", "x", "--session", "s"], 2),
        (["--command", "await", "--job-command", "analyze", "--prompt", "x", "--session", "s"], 1),
        (["--command", "submit", "--job-command", "explore", "--prompt", "x", "--session", "s"], 1),
        (["--command", "worker", "--job-id", "job_x"], 1),
        (["--command", "plan-record", "--session", "s"], 1),
        (["--command", "execute-begin", "--session", "s"], 1),
    ]
    with tempfile.TemporaryDirectory(prefix="exoma-recursion-") as tmp:
        for args, code in refused:
            rc, out = _cli(args, "second_agent", tmp)
            result = json.loads(out)
            assert_true(result.get("ok") is False and result["meta"].get("error_type") == "recursion_refused",
                        f"a marked process is refused for {args[:2]}: {out[:300]}")
            assert_true(result["meta"].get("next_action"), "the refusal names a next action")
            assert_true(rc == code, f"{args[:2]} exits {code} on refusal, got {rc}")
        assert_true(not (Path(tmp) / ".workflow").exists(), "a refusal writes nothing")

        # Read-only commands stay open to a marked process.
        rc, out = _cli(["--command", "status", "--job-id", "job_missing"], "second_agent", tmp)
        assert_true("recursion_refused" not in out, f"status stays open to the child: {out[:300]}")

        # Control: the same refused call without the marker is not refused by the guard.
        rc, out = _cli(["--command", "worker", "--job-id", "job_missing"], None, tmp)
        assert_true("recursion_refused" not in out, f"an unmarked process is never refused: {out[:300]}")

    # `exoma check --wait` reaches submit() without cli.main's parser: a marked caller
    # reclaiming a pending reservation must not get a worker spawned.
    from exoma import check as check_module

    spawned: list = []
    with tempfile.TemporaryDirectory(prefix="exoma-recursion-check-") as tmp, \
            patch.dict(os.environ, {ROLE: "second_agent"}), \
            patch.object(cli, "_spawn_worker", side_effect=lambda *a, **k: spawned.append(a) or {"ok": True}), \
            patch.object(check_module.JOB_MANAGER, "get_job",
                         return_value={"job_id": "job_p", "command": "explore", "task": "x",
                                       "session_id": "s", "work_dir": tmp, "worker_pid": None}), \
            patch.object(check_module, "_status_payload", return_value={"status": "pending"}):
        submitted = cli.submit("explore", "x", "s", tmp)
        assert_true(submitted["meta"].get("error_type") == "recursion_refused"
                    and submitted["meta"].get("job_command") == "explore",
                    f"submit() refuses a marked caller: {submitted}")
        check_module._resume_pending_reservation("job_p", {"status": "pending"})
        assert_true(not spawned, "check --wait on a pending job spawns no worker for a marked caller")

    # `check --wait` on a stalled job probes the provider: a marked caller keeps waiting
    # instead, with no provider call and no reap.
    from exoma.utils import osutil

    probed: list = []
    reaped: list = []
    with patch.dict(os.environ, {ROLE: "second_agent"}), \
            patch.object(cli, "probe_second_agent", side_effect=lambda *a, **k: probed.append(a) or {"alive": False}), \
            patch.object(cli.JOB_MANAGER, "get_job", return_value={"job_id": "job_s", "worker_pid": 4242}), \
            patch.object(cli.JOB_MANAGER, "reap_stalled", side_effect=lambda *a, **k: reaped.append(a) or {}), \
            patch.object(osutil, "process_alive", return_value=True):
        outcome = cli.check_stalled_job("job_s", None, None)
        assert_true(outcome is None and not probed and not reaped,
                    "a marked caller never probes the provider or reaps a stalled job")
    with patch.object(cli, "probe_second_agent", side_effect=lambda *a, **k: probed.append(a) or {"alive": True}), \
            patch.object(cli.JOB_MANAGER, "get_job", return_value={"job_id": "job_s", "worker_pid": 4242}), \
            patch.object(cli.JOB_MANAGER, "record_probe", return_value=None), \
            patch.object(osutil, "process_alive", return_value=True):
        saved = os.environ.pop(ROLE, None)
        try:
            cli.check_stalled_job("job_s", None, None)
        finally:
            if saved is not None:
                os.environ[ROLE] = saved
        assert_true(len(probed) == 1, "an unmarked caller still probes a stalled job")

    with patch.dict(os.environ, {ROLE: "second_agent"}):
        direct = cli.run("analyze", "x", "s", work_dir=None)
        assert_true(direct["meta"].get("error_type") == "recursion_refused",
                    "run() refuses a marked caller that skipped the CLI")
        for command in ("inspect", "status", "result", "report", "audit", "doctor"):
            assert_true(cli._recursion_refusal(command) is None, f"{command} stays open to the child")
    with patch.dict(os.environ, {ROLE: "main_agent"}):
        assert_true(cli._recursion_refusal("explore") is None, "only the second_agent role is refused")
    saved = os.environ.pop(ROLE, None)
    try:
        assert_true(cli._recursion_refusal("explore") is None, "no marker, no refusal")
    finally:
        if saved is not None:
            os.environ[ROLE] = saved


def _test_same_provider_child_isolation() -> None:
    from exoma.adapters.contract.registry import resolve_adapter
    from exoma.adapters.providers.opencode_adapter import OpenCodeAdapter

    saved = os.environ.pop(ROLE, None)
    try:
        for name in ("codex", "claude", "agy"):
            env = _child_env_of(resolve_adapter(name))
            assert_true(env.get(ROLE) == "second_agent", f"the {name} child is marked second_agent")
        with tempfile.TemporaryDirectory(prefix="exoma-oc-role-") as tmp:
            (Path(tmp) / ".workflow").mkdir()
            env = OpenCodeAdapter()._child_env(tmp)
        assert_true(env.get(ROLE) == "second_agent", "the opencode child is marked second_agent")
        assert_true(ROLE not in os.environ, "marking a child never marks the runtime itself")
    finally:
        if saved is not None:
            os.environ[ROLE] = saved

    claude = resolve_adapter("claude")
    args = claude._build_args(resume_id=None, model=None, cwd=None, last_message=None)
    assert_true(args[args.index("--tools") + 1] == "Read,Grep,Glob", "Claude-on-Claude: read tools only")
    assert_true(args[args.index("--setting-sources") + 1] == "" and "--strict-mcp-config" in args,
                "Claude-on-Claude: no host settings, hooks or MCP servers reach the child")

    plugin = (REPO / "src" / "exoma" / "_bundle" / "config" / "hosts" / "opencode" / "exoma.js").read_text(
        encoding="utf-8")
    assert_true(ROLE in plugin and "second_agent" in plugin,
                "OpenCode-on-OpenCode: the host plugin checks the role marker")


def _test_same_provider_export_picks_the_host() -> None:
    from exoma.core.audit.session_export import find_host_transcript

    sid = "main_project_20261031_000000000_abcd"
    with tempfile.TemporaryDirectory(prefix="exoma-export-same-") as tmp:
        home = Path(tmp)
        # Claude host + Claude child: the child's `claude -p` transcript sits beside the
        # host's, but only the host's is in the registry under this main session.
        projects = home / ".claude" / "projects" / "p"
        projects.mkdir(parents=True)
        host_t = projects / "host-uuid.jsonl"
        child_t = projects / "child-uuid.jsonl"
        host_t.write_text("{}\n", encoding="utf-8")
        child_t.write_text("{}\n", encoding="utf-8")
        (home / ".claude" / "session_registry.json").write_text(
            json.dumps({"host-uuid": {"main_session_id": sid, "transcript_path": str(host_t)}}),
            encoding="utf-8")
        path, host, _ = find_host_transcript(sid, home)
        assert_true(path == host_t and host == "claude", f"Claude: the host transcript is exported, got {path}")

    with tempfile.TemporaryDirectory(prefix="exoma-export-same-") as tmp:
        home = Path(tmp)
        bindings = home / ".codex" / "exoma" / "sessions"
        bindings.mkdir(parents=True)
        (bindings / "hostthread.json").write_text(json.dumps({"main_session_id": sid}), encoding="utf-8")
        rollouts = home / ".codex" / "sessions" / "2026" / "10" / "31"
        rollouts.mkdir(parents=True)
        host_t = rollouts / "rollout-hostthread.jsonl"
        host_t.write_text("{}\n", encoding="utf-8")
        (rollouts / "rollout-childthread.jsonl").write_text("{}\n", encoding="utf-8")
        path, host, _ = find_host_transcript(sid, home)
        assert_true(path == host_t and host == "codex", f"Codex: the host transcript is exported, got {path}")
