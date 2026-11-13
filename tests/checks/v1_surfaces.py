"""1.0 surfaces: OpenCode host, Claude second agent, per-host seeds, session export, e2e opt-in."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from tests.checks.support import assert_true, no_console

REPO = Path(__file__).resolve().parents[2]
BUNDLE = REPO / "src" / "exoma" / "_bundle" / "config"


def _env(home: Path) -> dict:
    env = dict(os.environ)
    for key in ("HOME", "USERPROFILE", "AGENT_HOME"):
        env[key] = str(home)
    env["PYTHONPATH"] = str(REPO / "src")
    env.pop("WORKFLOW_AGENT_ROLE", None)
    return env


def _opencode_hook(home: Path, event: str, payload: dict, role: str | None = None) -> dict:
    hooks = home / ".config" / "opencode" / "exoma" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BUNDLE / "hosts" / "opencode" / "opencode-host.py", hooks / "opencode-host.py")
    for name in ("workflow_policy.py", "intent-map.json"):
        shutil.copyfile(BUNDLE / "hosts" / "claude" / "hooks" / name, hooks / name)
    env = _env(home)
    if role:
        env["WORKFLOW_AGENT_ROLE"] = role
    proc = subprocess.run([sys.executable, str(hooks / "opencode-host.py"), event], input=json.dumps(payload),
                          text=True, capture_output=True, env=env, timeout=15, **no_console())
    assert_true(proc.returncode == 0, f"{event} hook failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _test_opencode_host_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="exoma-opencode-host-") as tmp:
        home = Path(tmp) / "home"
        project = Path(tmp) / "project"
        (project / ".workflow").mkdir(parents=True)
        sid = "ses_test1"
        first = _opencode_hook(home, "UserPromptSubmit", {"session_id": sid, "cwd": str(project), "prompt": "hello"})
        assert_true("[SESSION BINDING" in first.get("context", ""), "the first prompt binds the session")
        main_id = first["context"].split("MAIN_SESSION_ID=")[1].split()[0]
        again = _opencode_hook(home, "System", {"session_id": sid, "cwd": str(project)})
        assert_true(main_id in again.get("context", ""), "every model call carries the same binding")
        assert_true("Read ~/.config/opencode/exoma/CONTRACT.md" in again["context"],
                    "without an installed contract the agent is told where to read it")
        # Installed, the contract rides in the system prompt: a `read` of a path outside the
        # project would ask for external_directory access (or be denied by opencode.json).
        contract = home / ".config" / "opencode" / "exoma" / "CONTRACT.md"
        contract.write_text("## CONTRACT-SENTINEL\nrole: orchestrator\n", encoding="utf-8")
        carried = _opencode_hook(home, "System", {"session_id": sid, "cwd": str(project)})
        assert_true("CONTRACT-SENTINEL" in carried.get("context", "")
                    and "do not read ~/.config/opencode/exoma/CONTRACT.md again" in carried["context"]
                    and main_id in carried["context"],
                    "the installed contract is carried in the system prompt, beside the binding")
        assert_true(_opencode_hook(home, "System", {"session_id": sid, "cwd": str(project)},
                                   role="second_agent") == {},
                    "a second-agent process never gets the main contract")
        # An empty or unreadable contract is no contract: the agent is told where to read it,
        # rather than handed an empty block it would take as the whole rule set.
        contract.write_text("  \n", encoding="utf-8")
        blank = _opencode_hook(home, "System", {"session_id": sid, "cwd": str(project)})
        assert_true("Read ~/.config/opencode/exoma/CONTRACT.md" in blank.get("context", "")
                    and "do not read" not in blank["context"],
                    "an empty contract falls back to the read instruction")
        contract.unlink()
        contract.mkdir()
        try:
            unreadable = _opencode_hook(home, "System", {"session_id": sid, "cwd": str(project)})
        finally:
            contract.rmdir()
        assert_true("Read ~/.config/opencode/exoma/CONTRACT.md" in unreadable.get("context", "")
                    and main_id in unreadable["context"],
                    "an unreadable contract falls back to the read instruction, the binding intact")

        # An OpenCode command expands to `$exoma-<name> <args>` + the skill instruction; that
        # prompt arms the gate as a typed command would.
        sid_cmd = "ses_command"
        bound_cmd = _opencode_hook(home, "UserPromptSubmit", {
            "session_id": sid_cmd, "cwd": str(project),
            "prompt": "$exoma-analyze why is X slow\n\nLoad the `exoma-analyze` "
                      "skill with the skill tool and follow it exactly."})
        armed = _opencode_hook(home, "PreToolUse", {"session_id": sid_cmd, "cwd": str(project), "tool_name": "grep",
                                                    "tool_input": {"pattern": "X"}})
        assert_true("deny" in armed, "an expanded /exoma-analyze command arms the gate: %s" % armed)
        # `/exoma-execute -y` expands the same way, and its `-y` is the user's approval.
        main_cmd = bound_cmd["context"].split("MAIN_SESSION_ID=")[1].split()[0]
        _opencode_hook(home, "UserPromptSubmit", {
            "session_id": sid_cmd, "cwd": str(project),
            "prompt": "$exoma-execute -y\n\nLoad the `exoma-execute` skill with the skill tool and "
                      "follow it exactly, with the arguments above."})
        token = project / ".workflow" / "data" / "sessions" / main_cmd / "runtime" / "execute.token"
        assert_true(token.is_file(), "an expanded /exoma-execute -y records the execute approval")
        commit_sid = "ses_commit"
        for text in ("gas commit", "gas commit menggunakan $exoma-commit", "tolong commit perubahan ini"):
            commit_bound = _opencode_hook(home, "UserPromptSubmit", {
                "session_id": commit_sid, "cwd": str(project), "prompt": text})
            approved = _opencode_hook(home, "PreToolUse", {"session_id": commit_sid, "cwd": str(project),
                                                           "tool_name": "bash", "tool_input": {"command": "git commit -m x"}})
            assert_true(approved == {}, "OpenCode recognizes a direct commit request: " + text)
        _opencode_hook(home, "UserPromptSubmit", {"session_id": commit_sid, "cwd": str(project), "prompt": "jangan commit"})
        denied_commit = _opencode_hook(home, "PreToolUse", {"session_id": commit_sid, "cwd": str(project),
                                                            "tool_name": "bash", "tool_input": {"command": "git commit -m x"}})
        assert_true("deny" in denied_commit, "OpenCode revokes commit intent on negation")
        from exoma.installer.base import runner_prefix
        commit_main = commit_bound["context"].split("MAIN_SESSION_ID=")[1].split()[0]
        evidence = (f"{runner_prefix()} '{project.as_posix()}/.workflow/run.sh' plan "
                    f"--session '{commit_main}' --task 'Explain git commit'")
        accepted = _opencode_hook(home, "PreToolUse", {"session_id": commit_sid, "cwd": str(project),
                                                      "tool_name": "bash", "tool_input": {"command": evidence}})
        assert_true(accepted == {}, "OpenCode forwards the runner context so a task mention is data")
        nested = _opencode_hook(home, "PreToolUse", {"session_id": commit_sid, "cwd": str(project),
                                                    "tool_name": "bash", "tool_input": {"command": evidence + "; git commit -m x"}})
        assert_true("deny" in nested, "OpenCode still requires approval for an appended commit")
        outside = _opencode_hook(home, "UserPromptSubmit", {"session_id": "ses_x", "cwd": str(Path(tmp)), "prompt": "hi"})
        assert_true(outside == {}, "a directory without .workflow/ is left alone")

        _opencode_hook(home, "UserPromptSubmit", {"session_id": sid, "cwd": str(project), "prompt": "/.explore where is X"})
        denied = _opencode_hook(home, "PreToolUse", {"session_id": sid, "cwd": str(project), "tool_name": "grep",
                                                     "tool_input": {"pattern": "X"}})
        assert_true("[EXECUTION GATE]" in denied.get("deny", ""), "an armed delegation refuses gathering: %s" % denied)
        runner = _opencode_hook(home, "PreToolUse", {"session_id": sid, "cwd": str(project), "tool_name": "write",
                                                     "tool_input": {"filePath": str(project / ".workflow" / "run.sh")}})
        assert_true("deny" in runner, "the runner scripts are never written by hand")
        second = _opencode_hook(home, "PreToolUse", {"session_id": sid, "cwd": str(project), "tool_name": "grep",
                                                     "tool_input": {"pattern": "X"}}, role="second_agent")
        assert_true(second == {}, "a second-agent process is never gated")

        marker = project / ".workflow" / "data" / "sessions" / main_id / "runtime" / "delegated.marker"

        def check_runner_sessions(armed):
            before = marker.read_bytes() if armed else None
            for command in ("verify-prepare", "verify"):
                runner = f"'{project.resolve().as_posix()}/.workflow/run.sh' {command}"
                for suffix, allowed in (
                    (f" --session '{main_id}'", True),
                    ("", False), (" --session 'wrong'", False), (" --session 'default'", False),
                    (f" --session '{main_id}' --session '{main_id}'", False),
                ):
                    decision = _opencode_hook(home, "PreToolUse", {
                        "session_id": sid, "cwd": str(project), "tool_name": "bash",
                        "tool_input": {"command": runner + suffix},
                    })
                    assert_true((decision == {}) if allowed else ("deny" in decision and main_id in decision["deny"]),
                                f"OpenCode validates session with marker={armed}, {command}{suffix}: {decision}")
                    after = marker.read_bytes() if marker.is_file() else None
                    assert_true(after == before, "hook decisions never consume or change the delegated marker")

        _opencode_hook(home, "UserPromptSubmit", {"session_id": sid, "cwd": str(project), "prompt": "hello"})
        assert_true(not marker.exists(), "normal prompts leave delegation unarmed")
        check_runner_sessions(False)
        ordinary = _opencode_hook(home, "PreToolUse", {
            "session_id": sid, "cwd": str(project), "tool_name": "bash", "tool_input": {"command": "python --version"},
        })
        assert_true(ordinary == {}, "session binding does not restrict ordinary shell commands")
        _opencode_hook(home, "UserPromptSubmit", {"session_id": sid, "cwd": str(project), "prompt": "/.verify"})
        assert_true(marker.is_file(), "verify arms the delegated marker")
        check_runner_sessions(True)
        local_flag = project / ".workflow" / "local_mode.flag"
        local_flag.touch()
        try:
            check_runner_sessions(True)
        finally:
            local_flag.unlink()
        still_armed = _opencode_hook(home, "PreToolUse", {
            "session_id": sid, "cwd": str(project), "tool_name": "grep", "tool_input": {"pattern": "X"},
        })
        assert_true("deny" in still_armed, "preparation leaves source gathering gated")

        plugin = (BUNDLE / "hosts" / "opencode" / "exoma.js").read_text(encoding="utf-8")
        assert_true('"tool.execute.before"' in plugin and "WORKFLOW_AGENT_ROLE" in plugin
                    and "{{PYTHON_JSON}}" in plugin, "the plugin shim forwards tool calls and skips second agents")
    _opencode_second_agent_is_marked()

    with tempfile.TemporaryDirectory(prefix="exoma-opencode-quoted-") as tmp:
        home = Path(tmp) / "home"
        project = Path(tmp) / "project"
        (project / ".workflow").mkdir(parents=True)
        sid = "ses_quoted"
        _opencode_hook(home, "UserPromptSubmit", {"session_id": sid, "cwd": str(project),
                                                  "prompt": '"$exoma-explore where is main"'})
        denied = _opencode_hook(home, "PreToolUse", {"session_id": sid, "cwd": str(project), "tool_name": "glob",
                                                     "tool_input": {"pattern": "*.py"}})
        assert_true("deny" in denied, "a prompt wrapped in quotes (opencode run on Windows) still arms the gate")


def _test_opencode_host_install() -> None:
    with tempfile.TemporaryDirectory(prefix="exoma-opencode-install-") as tmp:
        home = Path(tmp)
        env = _env(home)
        result = subprocess.run([sys.executable, "-m", "exoma.install", "opencode", "--provider", "codex"],
                                cwd=home, env=env, text=True, capture_output=True, timeout=120, **no_console())
        assert_true(result.returncode == 0, "opencode install succeeds: " + result.stdout[-800:] + result.stderr[-500:])
        oc = home / ".config" / "opencode"
        for rel in ("plugins/exoma.js", "exoma/CONTRACT.md", "exoma/hooks/opencode-host.py",
                    "exoma/hooks/workflow_policy.py", "skills/exoma-plan/SKILL.md", "agents/exm-slice.md",
                    "commands/exoma-plan.md", "commands/exoma-execute.md"):
            assert_true((oc / rel).is_file(), f"opencode install writes {rel}")
        command = (oc / "commands" / "exoma-plan.md").read_text(encoding="utf-8")
        assert_true(command.startswith("---\ndescription: ") and "\n$exoma-plan $ARGUMENTS\n" in command
                    and "`exoma-plan` skill with the skill tool" in command and "~/.claude" not in command
                    and "argument-hint" not in command,
                    "an OpenCode command runs its exoma- skill, never a Claude path: " + command)
        skill = (oc / "skills" / "exoma-plan" / "SKILL.md").read_text(encoding="utf-8")
        assert_true("read ~/.config/opencode/exoma/CONTRACT.md only when it is not there" in skill,
                    "the skill points at the contract in the system prompt first")
        assert_true(not list((oc / "agents").glob("wf-*.md")), "the retired wf- agents are not installed")
        agents = (oc / "AGENTS.md").read_text(encoding="utf-8")
        assert_true("EXOMA-OPENCODE-HOST" in agents and "WORKFLOW-SECOND-AGENT" in agents,
                    "OpenCode host and provider blocks share AGENTS.md")
        plugin = (oc / "plugins" / "exoma.js").read_text(encoding="utf-8")
        assert_true("{{" not in plugin and "opencode-host.py" in plugin, "the plugin is rendered with real paths")
        assert_true((home / ".claude" / "exoma" / "second-agent.md").is_file(),
                    "the claude second-agent contract is installed as its own file")
        claude_md = home / ".claude" / "CLAUDE.md"
        assert_true(not claude_md.exists() or "WORKFLOW-SECOND-AGENT" not in claude_md.read_text(encoding="utf-8"),
                    "the claude second-agent contract never lands in ~/.claude/CLAUDE.md")
        checked = subprocess.run([sys.executable, "-m", "exoma.install", "--check"], cwd=home, env=env,
                                 text=True, capture_output=True, timeout=60, **no_console())
        assert_true(checked.returncode == 0, "opencode install passes --check: " + checked.stdout[-800:])

        # doctor sees a complete OpenCode host, and names a missing command file.
        from exoma.core.audit.bundle_integrity import _host_status

        status = _host_status(home)
        assert_true("opencode" in status["chosen"] and status["hosts"]["opencode"]["missing"] == [],
                    f"doctor recognizes a complete OpenCode host: {status}")
        doctor_command = oc / "commands" / "exoma-doctor.md"
        saved_doctor = doctor_command.read_text(encoding="utf-8")
        doctor_command.unlink()
        missing = _host_status(home)["hosts"]["opencode"]["missing"]
        doctor_command.write_text(saved_doctor, encoding="utf-8")
        assert_true("~/.config/opencode/commands/exoma-doctor.md" in missing,
                    f"doctor names a missing OpenCode command file: {missing}")

        # --check reports an edited command file as drift, by its bundle key.
        plan_command = oc / "commands" / "exoma-plan.md"
        saved_plan = plan_command.read_text(encoding="utf-8")
        plan_command.write_text(saved_plan + "\nlocal edit\n", encoding="utf-8")
        drifted = subprocess.run([sys.executable, "-m", "exoma.install", "--check"], cwd=home, env=env,
                                 text=True, capture_output=True, timeout=60, **no_console())
        plan_command.write_text(saved_plan, encoding="utf-8")
        assert_true(drifted.returncode != 0 and "DRIFTED hosts/opencode/commands/exoma-plan.md" in drifted.stdout,
                    "--check reports an edited OpenCode command: " + drifted.stdout[-800:])

        # Dropping the host takes Exoma's OpenCode files back out, the provider block stays.
        subprocess.run([sys.executable, "-m", "exoma.install", "claude", "--provider", "codex"], cwd=home,
                       env=env, text=True, capture_output=True, timeout=120, **no_console())
        assert_true(not (oc / "plugins" / "exoma.js").exists(), "a host no longer chosen loses its plugin")
        assert_true(not list((oc / "commands").glob("exoma-*.md")) if (oc / "commands").is_dir() else True,
                    "a host no longer chosen loses its commands")
        agents = (oc / "AGENTS.md").read_text(encoding="utf-8")
        assert_true("EXOMA-OPENCODE-HOST" not in agents and "WORKFLOW-SECOND-AGENT" in agents,
                    "withdrawing the OpenCode host keeps the provider block")

        # --uninstall takes the OpenCode host out whole: commands, plugin and block.
        reinstalled = subprocess.run([sys.executable, "-m", "exoma.install", "opencode", "--provider", "codex"],
                                     cwd=home, env=env, text=True, capture_output=True, timeout=120, **no_console())
        assert_true(reinstalled.returncode == 0 and (oc / "commands" / "exoma-plan.md").is_file(),
                    "the OpenCode host installs again: " + reinstalled.stdout[-600:])
        removed = subprocess.run([sys.executable, "-m", "exoma.install", "--uninstall", "--apply"], cwd=home,
                                 env=env, text=True, capture_output=True, timeout=120, **no_console())
        left = sorted(p.name for p in (oc / "commands").glob("exoma-*.md")) if (oc / "commands").is_dir() else []
        assert_true(removed.returncode == 0 and not left,
                    f"--uninstall removes the OpenCode commands: left={left} " + removed.stdout[-600:])
        assert_true(not (oc / "plugins" / "exoma.js").exists(), "--uninstall removes the OpenCode plugin")
        agents_md = oc / "AGENTS.md"
        assert_true(not agents_md.exists() or "EXOMA-OPENCODE-HOST" not in agents_md.read_text(encoding="utf-8"),
                    "--uninstall cuts the OpenCode host block")

        # An edited plugin survives the prune, and then the hook it calls must survive too.
        subprocess.run([sys.executable, "-m", "exoma.install", "opencode", "--provider", "codex"], cwd=home,
                       env=env, text=True, capture_output=True, timeout=120, **no_console())
        plugin = oc / "plugins" / "exoma.js"
        plugin.write_text(plugin.read_text(encoding="utf-8") + "\n// local tweak\n", encoding="utf-8")
        dropped = subprocess.run([sys.executable, "-m", "exoma.install", "claude", "--provider", "codex"], cwd=home,
                                 env=env, text=True, capture_output=True, timeout=120, **no_console())
        assert_true(plugin.is_file() and (oc / "exoma" / "hooks" / "opencode-host.py").is_file(),
                    "an edited plugin keeps the hook it calls")
        assert_true("was edited since Exoma installed it" in dropped.stdout,
                    "the kept plugin is reported: " + dropped.stdout[-600:])


def _test_claude_second_agent() -> None:
    from exoma.adapters.contract.registry import resolve_adapter
    from exoma.config.providers import provider_boundary, transport_budget

    adapter = resolve_adapter("claude")
    args = adapter._build_args(resume_id="abc", model="sonnet", cwd=None, last_message=None)
    tools = args[args.index("--tools") + 1]
    assert_true(tools == "Read,Grep,Glob", "the delegated call has only read tools")
    assert_true(args[args.index("--setting-sources") + 1] == "" and "--strict-mcp-config" in args,
                "no settings file, hook or MCP server reaches the delegated call")
    assert_true(args[args.index("--resume") + 1] == "abc", "a session is continued with --resume")
    assert_true(transport_budget("claude")["kind"] == "stdin", "the prompt rides stdin")
    assert_true(provider_boundary("claude") == {"reads": "unbounded", "writes": "denied"},
                "the boundary states what the tool list enforces")
    stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "11111111-2222-3333-4444-555555555555"}),
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "draft"}]}}),
        json.dumps({"type": "result", "is_error": False, "result": "[EVIDENCE] final",
                    "usage": {"input_tokens": 3, "output_tokens": 2}}),
    ])
    assert_true(adapter.extract_session_id(stream) == "11111111-2222-3333-4444-555555555555", "session id from init")
    assert_true(adapter.clean_output(stream) == "[EVIDENCE] final", "the answer is the result event")
    assert_true(adapter.extract_usage(stream)["output_tokens"] == 2, "usage from the result event")

    import subprocess as _subprocess
    from unittest.mock import patch

    failed = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "11111111-2222-3333-4444-555555555555"}),
        json.dumps({"type": "result", "is_error": True, "result": "API Error: overloaded"}),
    ])

    class _Proc:
        def __init__(self, *args, **kwargs):
            import io
            self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(failed + "\n"), io.StringIO("")
            self.returncode, self.pid = 0, 1

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    with patch.object(_subprocess, "Popen", _Proc):
        result = adapter.run("task", {}, work_dir=None)
    assert_true(result.get("ok") is False, "an is_error result fails the call even at exit 0: %s" % result)


def _test_per_host_seed() -> None:
    from exoma import install as install_mod
    from exoma.adapters.install.opencode_install import _copy_provider_config
    from exoma.installer.base import Plan

    with tempfile.TemporaryDirectory(prefix="exoma-seed-") as tmp:
        seeds = Path(tmp) / "config"
        seeds.mkdir()
        saved = install_mod.SEED_DIR
        install_mod.SEED_DIR = seeds
        try:
            install_mod._seed_second_agent_config(Plan(), True, "codex", "gpt-5.6-sol", host="codex")
            install_mod._seed_second_agent_config(Plan(), True, "claude", "sonnet", host="claude")
        finally:
            install_mod.SEED_DIR = saved
        codex = json.loads((seeds / "second_agent.seed.codex.json").read_text(encoding="utf-8"))
        claude = json.loads((seeds / "second_agent.seed.claude.json").read_text(encoding="utf-8"))
        assert_true(codex["provider"] == "codex" and claude["provider"] == "claude",
                    "each host keeps its own second-agent choice")
        from exoma.adapters.install import opencode_install
        from exoma.core.workspace.workspace_paths import PROVIDER_SEED_NAME

        # support.py points the seed name at the example for every other check; this one
        # is about the real per-host names.
        examples = Path(tmp) / "example"
        examples.mkdir()
        (examples / "second_agent.example.json").write_text('{"provider": "opencode"}', encoding="utf-8")
        isolated, opencode_install.PROVIDER_SEED_NAME = opencode_install.PROVIDER_SEED_NAME, PROVIDER_SEED_NAME
        try:
            for host in ("claude", "codex", "agy"):
                project = Path(tmp) / host
                (project / ".workflow").mkdir(parents=True)
                _copy_provider_config(project, seed_dir=seeds, example_dir=examples, host=host)
        finally:
            opencode_install.PROVIDER_SEED_NAME = isolated
        # agy has no seed of its own and there is no shared one: it falls back to the example.
        for host, provider in (("claude", "claude"), ("codex", "codex"), ("agy", "opencode")):
            project = Path(tmp) / host
            copied = json.loads((project / ".workflow" / "second_agent.json").read_text(encoding="utf-8"))
            assert_true(copied["provider"] == provider, f"init from {host} copies its seed (or the example)")


def _test_session_export() -> None:
    from exoma.core.audit.session_export import export_session

    with tempfile.TemporaryDirectory(prefix="exoma-export-") as tmp:
        root = Path(tmp) / "project"
        sid = "main_project_20261031_000000000_abcd"
        session = root / ".workflow" / "data" / "sessions" / sid
        (session / "logs" / "call1").mkdir(parents=True)
        (session / "logs" / "call1" / "output.raw.md").write_text(
            "[EVIDENCE]\ntoken ghp_" + "a" * 36 + "\n", encoding="utf-8")
        (session / "runtime").mkdir()
        (session / "runtime" / "lock").write_text("x", encoding="utf-8")
        data = root / ".workflow" / "data"
        rows = [{"session_id": sid, "command": "explore"}, {"session_id": "other", "command": "plan"}]
        (data / "usage.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        task_rows = [{"session_id": sid, "event": kind, "prompt_id": "p1"}
                     for kind in ("edit", "verify", "commit")]
        task_rows[0]["detail"] = "credential ghp_" + "c" * 36
        task_rows.append({"session_id": "other", "event": "edit"})
        (data / "tasks.jsonl").write_text(
            "\n".join(json.dumps(row) for row in task_rows) + "\n{torn", encoding="utf-8")
        home = Path(tmp) / "home"
        home.mkdir()

        first = export_session(root, sid, home=home)
        assert_true(first["ok"], "export succeeds")
        manifest = first["manifest"]
        assert_true(manifest["review"]["status"] == "template", "an unfilled review is reported as such")
        assert_true(manifest["transcript"]["status"] == "unavailable", "no host transcript is said, not guessed")
        assert_true(manifest["streams"]["tasks.jsonl"]["malformed_rows"] == 1,
                    "export records malformed rows separately from exported bytes")
        with zipfile.ZipFile(first["path"]) as bundle:
            names = set(bundle.namelist())
            raw = bundle.read("session/logs/call1/output.raw.md").decode("utf-8")
            usage = bundle.read("streams/usage.jsonl").decode("utf-8")
            tasks = bundle.read("streams/tasks.jsonl").decode("utf-8")
        assert_true("session/runtime/lock" not in names and "review.md" in names and "manifest.json" in names,
                    "lock files stay out; review and manifest go in")
        assert_true("ghp_" not in raw and "[REDACTED" in raw and manifest["redactions"] >= 1,
                    "credential-shaped text is redacted")
        assert_true(sid in usage and "other" not in usage, "only this session's stream rows")
        exported_tasks = [json.loads(line) for line in tasks.splitlines()]
        assert_true([row["event"] for row in exported_tasks] == ["edit", "verify", "commit"]
                    and all(row["session_id"] == sid for row in exported_tasks),
                    "raw task lifecycle events are portable, session-filtered and torn rows skipped")
        assert_true("ghp_" not in tasks and "[REDACTED" in tasks,
                    "task event payloads use the same credential redaction as other streams")
        import hashlib
        task_entry = next(row for row in manifest["files"] if row["path"] == "streams/tasks.jsonl")
        assert_true(task_entry["sha256"] == hashlib.sha256(tasks.encode("utf-8")).hexdigest(),
                    "task stream manifest hash matches exported bytes")

        review = Path(manifest["review"]["source"])
        review.write_text(review.read_text(encoding="utf-8") + "\n- solved | partly solved | not solved: solved\n",
                          encoding="utf-8")
        second = export_session(root, sid, home=home)
        assert_true(second["manifest"]["review"]["status"] == "filled", "a filled review is picked up")
        (session / "logs" / "blob.bin").write_bytes(b"\xff\xfe secret-ish \x00\x81")
        outside = Path(tmp) / "outside.txt"
        outside.write_text("token ghp_" + "b" * 36, encoding="utf-8")
        linked = True
        try:
            os.symlink(outside, session / "logs" / "link.txt")
        except (OSError, NotImplementedError):
            linked = False  # Windows without the symlink privilege: the binary case still runs
        third = export_session(root, sid, home=home)
        skipped = {entry["path"]: entry["reason"] for entry in third["manifest"]["skipped"]}
        with zipfile.ZipFile(third["path"]) as bundle:
            names = set(bundle.namelist())
        assert_true("session/logs/blob.bin" in skipped and "session/logs/blob.bin" not in names,
                    "bytes that are not UTF-8 text are listed, never copied unredacted")
        if linked:
            assert_true("session/logs/link.txt" in skipped and "session/logs/link.txt" not in names,
                        "a symbolic link is listed, never followed")
        review.write_bytes(b"\xff\xfe not utf-8 \x81")
        unreadable = export_session(root, sid, home=home)
        assert_true(unreadable["ok"] and unreadable["manifest"]["review"]["status"] == "unreadable",
                    "an unreadable review is reported, the export still completes")
        from exoma.utils.path_guard import safe_path_component

        for hostile in ("../../evil", "a/b", ".."):
            (data / "sessions" / safe_path_component(hostile)).mkdir(parents=True, exist_ok=True)
            refused = export_session(root, hostile, home=home)
            assert_true(not refused["ok"], f"an id that is not a safe path component is refused: {hostile!r}")
        assert_true(not (data.parent.parent / "evil.review.md").exists() and not list(Path(tmp).glob("evil*")),
                    "nothing is written outside the exports directory")
        missing = export_session(root, "main_nope", home=home)
        assert_true(not missing["ok"], "an unknown session is refused")
        (data / "tasks.jsonl").unlink()
        without_tasks = export_session(root, sid, home=home)
        assert_true(without_tasks["manifest"]["streams"]["tasks.jsonl"]["status"] == "missing",
                    "missing instrumentation is explicit, not an apparently empty measurement")
        with zipfile.ZipFile(without_tasks["path"]) as bundle:
            assert_true("streams/tasks.jsonl" not in bundle.namelist(),
                        "a session without task instrumentation still exports")


def _test_e2e_stays_opt_in() -> None:
    from unittest.mock import patch

    from exoma.installer import settings as installer_settings
    from exoma.installer.base import Plan

    called = []
    plan = Plan()
    with patch.object(installer_settings, "_install_e2e_deps", side_effect=lambda *a: called.append(a)):
        installer_settings._install_deps(plan, True, False)
        assert_true(not called and not plan.actions, "an install without --with-e2e installs nothing")
        installer_settings._install_deps(plan, True, True)
        assert_true(len(called) == 1, "--with-e2e is the only way in")
    install = (REPO / "src" / "exoma" / "install.py").read_text(encoding="utf-8")
    assert_true('"--with-e2e",\n        action="store_true"' in install, "--with-e2e defaults off")
    update = (REPO / "src" / "exoma" / "self_update.py").read_text(encoding="utf-8")
    assert_true("with-e2e" not in update, "exoma update never adds the browser extra")


def _opencode_second_agent_is_marked() -> None:
    """The delegated OpenCode process carries the role, so the host plugin stays off."""
    from exoma.adapters.providers.opencode_adapter import OpenCodeAdapter

    with tempfile.TemporaryDirectory(prefix="exoma-oc-role-") as tmp:
        (Path(tmp) / ".workflow").mkdir()
        env = OpenCodeAdapter()._child_env(tmp)
    assert_true(env.get("WORKFLOW_AGENT_ROLE") == "second_agent",
                "an OpenCode second-agent call is marked as one")
