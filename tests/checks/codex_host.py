"""Codex host lifecycle, installer ownership, and same-CLI role isolation."""

import json
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.checks.support import assert_true, no_console

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "src" / "exoma" / "_bundle" / "config" / "hosts" / "codex" / "codex-host.py"


def _hook(home, event, payload, role=None, intent_map=True):
    installed = home / ".codex" / "exoma" / "hooks"
    installed.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SCRIPT, installed / "codex-host.py")
    claude = REPO / "src" / "exoma" / "_bundle" / "config" / "hosts" / "claude" / "hooks"
    shutil.copyfile(claude / "workflow_policy.py", installed / "workflow_policy.py")
    if intent_map:
        shutil.copyfile(claude / "intent-map.json", installed / "intent-map.json")
    else:
        (installed / "intent-map.json").unlink(missing_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["AGENT_HOME"] = str(home)
    env.pop("WORKFLOW_AGENT_ROLE", None)
    if role:
        env["WORKFLOW_AGENT_ROLE"] = role
    proc = subprocess.run([sys.executable, str(installed / "codex-host.py"), event], input=json.dumps(payload), text=True,
                          capture_output=True, env=env, timeout=15, **no_console())
    assert_true(proc.returncode == 0, f"{event} hook failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _test_codex_host_contract():
    _commit_gate_contract()
    with tempfile.TemporaryDirectory(prefix="exoma-codex-host-") as tmp:
        home = Path(tmp)
        root = home / "project"
        (root / ".workflow").mkdir(parents=True)
        sid = "codex-thread-1"
        start = _hook(home, "SessionStart", {"session_id": sid, "cwd": str(root), "source": "startup"})
        binding = start["hookSpecificOutput"]["additionalContext"]
        main = binding.split("MAIN_SESSION_ID=")[1].splitlines()[0]
        assert_true("MAIN_SESSION_ID=" in binding, "Codex SessionStart injects the main session binding")
        assert_true("$exoma-plan" in binding and "/.plan" not in binding, "startup binding uses native commands")
        resumed = _hook(home, "SessionStart", {"session_id": sid, "cwd": str(root), "source": "resume"})
        assert_true(resumed["hookSpecificOutput"]["additionalContext"].split("MAIN_SESSION_ID=")[1].splitlines()[0]
                    == binding.split("MAIN_SESSION_ID=")[1].splitlines()[0], "resume reuses the main session")
        compacted = _hook(home, "SessionStart", {"session_id": sid, "cwd": str(root), "source": "compact"})
        assert_true(compacted["hookSpecificOutput"]["additionalContext"].split("MAIN_SESSION_ID=")[1].splitlines()[0]
                    == binding.split("MAIN_SESSION_ID=")[1].splitlines()[0], "compaction keeps the main session")
        prompt = {"session_id": sid, "cwd": str(root), "turn_id": "turn-1", "prompt": "$exoma-plan add a feature"}
        _hook(home, "UserPromptSubmit", prompt)
        from exoma.installer.base import runner_prefix
        runner = f"{runner_prefix()} '{(root / '.workflow' / 'run.sh').as_posix()}' plan --session '{main}' --task 'task'"
        accepted = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root),
                                                 "tool_name": "exec_command", "tool_input": {"cmd": runner}})
        assert_true(accepted == {}, f"the Codex runner call passes the armed gate: {accepted}")
        prepare = runner.split(" plan ")[0] + f" verify-prepare --session '{main}'"
        for tool in ("Bash", "exec_command", "functions.exec_command"):
            accepted_prepare = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root),
                                    "tool_name": tool, "tool_input": {"cmd": prepare}})
            assert_true(accepted_prepare == {}, f"{tool}: local prepare passes the armed gate")
        wrong_prepare = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root),
                              "tool_name": "exec_command", "tool_input": {"cmd": prepare.replace(main, "other")}})
        assert_true(wrong_prepare.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    "prepare remains bound to the authoritative Codex session")
        for suffix in ("--session 'wrong' --task 'task'", "--session 'default'", "--task 'task'",
                       f"--session '{main}' --session '{main}'", f"--session '{main}' --task '--session'",
                       f"--session '{main}' --task 'x' extra",
                       f"--session '{main}' --task 'x'; Write-Output extra"):
            candidate = runner.split(" plan ")[0] + " plan " + suffix
            refused = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root),
                           "tool_name": "exec_command", "tool_input": {"cmd": candidate}})
            assert_true(refused.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                        f"invalid or mismatched session is refused: {suffix}")
        for suffix in (f"--main-host codex --task 'text --session wrong' --session '{main}'",
                       f"--task-file 'request with spaces.txt' --session '{main}'", f"'task' '{main}'"):
            candidate = runner.split(" plan ")[0] + " plan " + suffix
            passed = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root),
                          "tool_name": "exec_command", "tool_input": {"cmd": candidate}})
            assert_true(passed == {}, f"matching session with opaque task data passes: {suffix}")
        denied_command = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root),
                                                       "tool_name": "exec_command", "tool_input": {"cmd": "rg TODO ."}})
        assert_true(denied_command.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    "Codex exec_command cannot gather before delegation")
        read = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root), "tool_name": "Bash",
                                             "tool_input": {"command": "rg TODO ."}})
        assert_true(read.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    "Codex preflight denies local codebase gathering before delegation")
        small = root / "small.py"
        small.write_text("x = 1\n", encoding="utf-8")
        large = root / "large.py"
        large.write_text("x = 1\n" * 300, encoding="utf-8")
        # `"²".isdigit()` is true and int("²") raises: the hook used to crash and fail open.
        odd_count = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root), "tool_name": "exec_command",
                                                  "tool_input": {"cmd": f"Get-Content -TotalCount ² '{large.as_posix()}'"}})
        assert_true(odd_count.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    f"a non-ASCII line count is not a precision read, and the gate holds: {odd_count}")
        for number in (1, 2):
            read_command = (f"Get-Content '{small.as_posix()}'" if number == 1
                            else f"sed -n '150,170p' '{large.as_posix()}'")
            precision = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root),
                                                       "tool_name": "exec_command",
                                                       "tool_input": {"cmd": read_command}})
            assert_true(precision == {}, f"precision shell read {number} passes: {precision}")
        quota = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root),
                                              "tool_name": "exec_command",
                                              "tool_input": {"cmd": f"Get-Content '{small.as_posix()}'"}})
        assert_true(quota.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    "a third precision shell read is denied")
        delegated = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root), "tool_name": "Bash",
                                                  "tool_input": {"command": "rg TODO ."}}, role="second_agent")
        assert_true(delegated == {}, "the Codex second agent bypasses host hooks")
        _hook(home, "UserPromptSubmit", {**prompt, "turn_id": "turn-2", "prompt": "normal question"})
        allowed = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root), "tool_name": "Bash",
                                                "tool_input": {"command": "rg TODO ."}})
        assert_true(allowed == {}, "a later ordinary prompt clears the delegation marker")
        no_marker = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root), "tool_name": "Bash",
                         "tool_input": {"command": runner.replace(main, "wrong")}})
        assert_true(no_marker.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    "binding validation applies without a delegated marker")
        compound = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root), "tool_name": "Bash",
                         "tool_input": {"command": runner + "; Write-Output extra"}})
        assert_true(compound.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    "malformed recognized runner calls do not evade binding validation without a marker")
        _hook(home, "UserPromptSubmit", {**prompt, "prompt": "$exoma-plan add a feature"})
        upgrade_command = "exoma upgrade --work-dir '%s'" % root.as_posix()
        upgrade_payload = {"session_id": sid, "cwd": str(root), "tool_name": "Bash",
                           "tool_input": {"command": upgrade_command}}
        refused_upgrade = _hook(home, "PreToolUse", upgrade_payload)
        assert_true(refused_upgrade.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    "old-runner recovery does not bypass an armed delegation gate")
        _hook(home, "UserPromptSubmit", {**prompt, "prompt": "$exoma-upgrade"})
        assert_true(_hook(home, "PreToolUse", upgrade_payload) == {},
                    "an explicit user upgrade command releases the old gate for bootstrap recovery")
        _hook(home, "UserPromptSubmit", {**prompt, "turn_id": "turn-2a"})
        _hook(home, "UserPromptSubmit", {**prompt, "turn_id": "turn-2b", "prompt": "normal question"},
              intent_map=False)
        allowed_without_map = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root),
                                                          "tool_name": "Bash", "tool_input": {"command": "rg TODO ."}})
        assert_true(allowed_without_map == {}, "a missing intent map clears the previous turn's delegation marker")
        profile_path = home / ".codex/exoma/hooks/host-profile.json"
        profile_path.write_text(json.dumps({"auto_intent": False}), encoding="utf-8")
        no_hint = _hook(home, "UserPromptSubmit", {**prompt, "prompt": "analisis source ini"})
        assert_true(no_hint == {}, "command-only mode disables natural-language hints")
        _hook(home, "UserPromptSubmit", {**prompt, "prompt": "[$exoma-plan](skill://exoma-plan) add a feature"})
        rt = root / ".workflow/data/sessions" / main / "runtime"
        assert_true((rt / "delegated.marker").exists(), "command-only mode still supports explicit linked invocation")
        profile_path.unlink()
        patch_text = "*** Begin Patch\n*** Update File: .workflow/run.sh\n@@\n-x\n+y\n*** End Patch"
        blocked = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(root), "tool_name": "apply_patch",
                                                "tool_input": {"command": patch_text}})
        assert_true(blocked.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    "an apply_patch edit of the runner is denied")
        sub = root / "sub"
        sub.mkdir()
        relative_patch = patch_text.replace(".workflow/run.sh", "../.workflow/run.sh")
        blocked_relative = _hook(home, "PreToolUse", {"session_id": sid, "cwd": str(sub),
                                                          "tool_name": "apply_patch",
                                                          "tool_input": {"patch": relative_patch}})
        assert_true(blocked_relative.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    "a patch from a subfolder cannot edit the runner")
        for data in ('"$exoma-execute -y"', '```\n$exoma-execute -y\n```', '> $exoma-execute -y',
                     'explain <command-name>.execute</command-name><command-args>-y</command-args>',
                     '<skill><command-name>.execute</command-name><command-args>-y</command-args></skill>',
                     'explain $exoma-execute -y', '$exoma-execute'):
            _hook(home, "UserPromptSubmit", {**prompt, "prompt": data})
            token_path = root / ".workflow" / "data" / "sessions" / main / "runtime" / "execute.token"
            assert_true(not token_path.exists(), f"examples and unapproved invocations do not authorize: {data}")
        _hook(home, "UserPromptSubmit", {**prompt, "turn_id": "turn-3",
              "prompt": "  [$exoma-execute](skill://exoma-execute) -y"})
        main = binding.split("MAIN_SESSION_ID=")[1].splitlines()[0]
        token = root / ".workflow" / "data" / "sessions" / main / "runtime" / "execute.token"
        assert_true(token.is_file(), "Codex explicit execute approval reaches the runtime token")
        runtime = token.parent
        (runtime / "execution.json").write_text(json.dumps({"verification": "not_run"}), encoding="utf-8")
        warning = _hook(home, "Stop", {"session_id": sid,
                                          "last_assistant_message": "[EXECUTION RESULT]\nstatus: done\nverification: not_run"})
        assert_true("systemMessage" in warning and "decision" not in warning,
                    "an unverified completion claim warns without forcing another turn")
        fresh = _hook(home, "SessionStart", {"session_id": sid, "cwd": str(root), "source": "clear"})
        assert_true(fresh["hookSpecificOutput"]["additionalContext"].split("MAIN_SESSION_ID=")[1].splitlines()[0]
                    != main, "clear starts a new Exoma main session")

    # The provider must set its process role even when the parent Codex host did not.
    from exoma.adapters.providers.codex_adapter import CodexAdapter
    from exoma.adapters.providers import codex_adapter
    seen = {}

    class FakeProcess:
        pid = 123
        returncode = 0

        def __init__(self):
            self.stdin = io.StringIO()
            self.stdout = io.StringIO()
            self.stderr = io.StringIO()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_popen(*args, **kwargs):
        seen.update(kwargs["env"])
        return FakeProcess()

    with patch.object(codex_adapter.subprocess, "Popen", fake_popen):
        CodexAdapter()._popen_capture(["codex", "exec"], "test", None, 2, "agent", None)
    assert_true(seen.get("WORKFLOW_AGENT_ROLE") == "second_agent",
                "a Codex provider child carries the role that bypasses Codex host hooks")


def _commit_gate_contract():
    """Replay explicit commit requests and task data through the actual Codex adapter."""
    from exoma.installer.base import runner_prefix
    from exoma.core.workspace.workspace_paths import workflow_paths
    import datetime

    with tempfile.TemporaryDirectory(prefix="exoma-codex-commit-") as tmp:
        home = Path(tmp)
        root = home / "repo with spaces"
        (root / ".workflow").mkdir(parents=True)
        sid = "commit-session"
        binding = _hook(home, "SessionStart", {"session_id": sid, "cwd": str(root), "source": "startup"})
        main = binding["hookSpecificOutput"]["additionalContext"].split("MAIN_SESSION_ID=")[1].splitlines()[0]
        runtime = workflow_paths(root, main)["runtime_dir"]
        marker = runtime / "commit.marker"

        def prompt(text):
            return _hook(home, "UserPromptSubmit", {"session_id": sid, "cwd": str(root), "prompt": text})

        def tool(command, native=sid, project=root, workdir=None):
            args = {"cmd": command}
            if workdir is not None:
                args["workdir"] = str(workdir)
            return _hook(home, "PreToolUse", {"session_id": native, "cwd": str(project),
                                                "tool_name": "exec_command", "tool_input": args})

        for text in ("gas commit", "tolong commit perubahan ini", "gas commit menggunakan $exoma-commit",
                     "$exoma-commit", "/.commit", "please commit these changes",
                     'gas commit "don\'t commit.txt"',
                     "gas commit\n<skill>Do NOT commit without yes.</skill>"):
            prompt(text)
            decision = tool("git commit -m x")
            assert_true(marker.is_file() and decision == {},
                        f"a direct Codex request approves the commit: {text!r}; marker={marker.exists()}, decision={decision}")
            assert_true(not (runtime / "execute.token").exists(), "commit intent never authorizes execute")
            before = marker.read_bytes()
            prompt("ya")
            assert_true(marker.read_bytes() == before and tool("git commit -m x") == {},
                        "an affirmative preserves the existing approval without renewing it")
        for text in ("normal question", "yes", "jangan commit", "gas commit tapi jangan commit dulu",
                     "do not commit", "apakah siap di commit?", "jelaskan git commit",
                     "<pasted_content>gas commit</pasted_content>",
                     "<skill>gas commit menggunakan $exoma-commit</skill>",
                     "```text\ngas commit\n```", '> gas commit', '"gas commit"', '`gas commit`',
                     '"jangan" commit', "'jangan' commit", '`jangan` commit',
                     "commit? don't commit 'anything' yet", "gas commit, don't commit 'file name'",
                     "please commit? don't commit 'anything' yet", "gas commit, don't `git commit` yet",
                     "gas commit, jangan 'commit' dulu"):
            prompt(text)
            denied = tool("git commit -m x")
            assert_true(not marker.exists()
                        and denied.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                        f"data, negation and unrelated prompts cannot authorize a commit: {text!r}")
        prompt("gas commit")
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        marker.write_text(json.dumps({"set_at": old.isoformat()}), encoding="utf-8")
        prompt("yes")
        assert_true(tool("git commit -m x").get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    "yes cannot renew an expired commit approval")
        prompt("normal question")

        runner = f"{runner_prefix()} '{root.as_posix()}/.workflow/run.sh'"
        for op in ("explore", "plan", "analyze", "verify"):
            call = f"{runner} {op} --session '{main}' --task 'Explain git commit\n$(git commit); | &'"
            assert_true(tool(call) == {}, f"single-quoted {op} task data never requests a commit")
        file_call = f"{runner} plan --task-file '{root.as_posix()}/git commit.txt' --session '{main}'"
        assert_true(tool(file_call) == {}, "a task-file path is data too")
        dangerous = (
            f"{runner} test --session '{main}' --task 'git commit -m x'",
            f"{runner} plan --session '{main}' --task 'git commit' ; git commit -m x",
            f"{runner} plan --session '{main}' --task 'git commit'\ngit commit -m x",
            f"{runner} plan --session '{main}' --task 'git commit' | git commit -m x",
            f'{runner} plan --session \'{main}\' --task "$(git commit -m x)"',
            f"{runner} plan --session '{main}' --task 'git commit' --unexpected yes",
            f"{runner} plan --task 'git commit'",
            f"{runner_prefix()} '~/repo with spaces/.workflow/run.sh' plan --session '{main}' --task 'git commit'",
            f"'{root.as_posix()}/other/.workflow/run.sh' plan --session '{main}' --task 'git commit'",
            'bash -c "git commit -m x"', 'pwsh -Command "git commit -m x"',
            '"C:/Program Files/Git/cmd/git.exe" -C "repo with spaces" commit -m x',
            'git.exe -c user.name=x commit -m x',
        )
        for call in dangerous:
            denied = tool(call)
            assert_true(denied.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                        f"actual or ambiguous commit execution still requires approval: {call!r}")

        foreign_cwd = home / "foreign cwd"
        (foreign_cwd / ".workflow").mkdir(parents=True)
        relative = f"{runner_prefix()} '.workflow/run.sh' plan --session '{main}' --task 'git commit -m x'"
        denied = tool(relative, workdir=foreign_cwd)
        assert_true(denied.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                    "a relative runner cannot gain an exception using the bound root instead of its actual workdir")
        absolute = f"{runner} plan --session '{main}' --task 'Explain git commit'"
        assert_true(tool(absolute, workdir=foreign_cwd) == {},
                    "an absolute runner remains bound to the correct project from another workdir")

        prompt("gas commit")
        other_root = home / "another project"
        (other_root / ".workflow").mkdir(parents=True)
        for native, project in (("other-session", root), ("other-project-session", other_root)):
            _hook(home, "SessionStart", {"session_id": native, "cwd": str(project), "source": "startup"})
            _hook(home, "UserPromptSubmit", {"session_id": native, "cwd": str(project), "prompt": "yes"})
            denied = tool("git commit -m x", native, project)
            assert_true(denied.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
                        and tool("git commit -m x") == {},
                        "commit intent neither leaks to nor is revoked by another session or project")


# Codex's Windows hook wrapper (codex-rs core/src/hook_environment_executor.rs): the command
# runs at the top level of `powershell -NoProfile -Command`, fed one stdin line.
_CODEX_POWERSHELL_WRAPPER = (
    "$global:LASTEXITCODE = 0\n[Console]::In.ReadLine() | {command}\n"
    "if ($?) {{ exit 0 }}\nif ($LASTEXITCODE) {{ exit $LASTEXITCODE }}\nexit 1"
)


def _codex_windows_hooks_launch():
    """Every rendered `commandWindows` launches under Codex's PowerShell wrapper.

    1.0.0 wrote it as `"python" "codex-host.py" Event`: PowerShell reads the leading quoted
    path as a string expression and fails to parse the rest, so every hook exited 1 while
    `--check` reported READY. The direct-script checks above could not see it."""
    from exoma.installer import base

    # `$` and a backtick in the home path: PowerShell expands both inside double quotes.
    with tempfile.TemporaryDirectory(prefix="exoma-codex-win-$x`y-") as tmp:
        home = Path(tmp)
        root = home / "project"
        (root / ".workflow").mkdir(parents=True)
        _hook(home, "SessionStart", {"session_id": "warm", "cwd": str(root), "source": "startup"})
        entries = base.codex_hook_entries(home)
        old_form = json.loads(json.dumps(entries))
        python, script = Path(sys.executable).as_posix(), base.codex_host_hook_path(home).as_posix()
        for event, groups in old_form.items():
            for group in groups:
                for handler in group["hooks"]:
                    # Pre-release 1.0.0: one double-quoted form for both keys.
                    handler["command"] = handler["commandWindows"] = f'"{python}" "{script}" {event}'
        if os.name == "nt":
            assert_true(base.codex_hook_events_present(entries, home)
                        and not base.codex_hook_events_present(old_form, home),
                        "--check accepts the call-operator form and flags the 1.0.0 form")
        assert_true(base.codex_hook_events_present(old_form, home, launchable=False),
                    "host recovery still finds the 1.0.0 form registered, so it is repaired, not withdrawn")
        assert_true(all(base._codex_hook_owned(group["hooks"][0], home, event)
                        for event, groups in old_form.items() for group in groups),
                    "the 1.0.0 form stays Exoma's, so a reinstall replaces it")
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if os.name != "nt" or not powershell:
            return
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), AGENT_HOME=str(home))
        env.pop("WORKFLOW_AGENT_ROLE", None)
        payload = json.dumps({"session_id": "codex-win", "cwd": str(root), "source": "startup",
                              "prompt": "hi", "tool_name": "Bash", "tool_input": {"command": "ls"}}) + "\n"
        for event, groups in entries.items():
            command = groups[0]["hooks"][0]["commandWindows"]
            proc = subprocess.run([powershell, "-NoProfile", "-Command",
                                   _CODEX_POWERSHELL_WRAPPER.format(command=command)],
                                  input=payload, text=True, capture_output=True, env=env,
                                  timeout=60, **no_console())
            assert_true(proc.returncode == 0 and isinstance(json.loads(proc.stdout or "null"), dict),
                        f"{event} launches under Codex's PowerShell wrapper: rc={proc.returncode} "
                        f"{proc.stderr[-400:]}")
        legacy = old_form["Stop"][0]["hooks"][0]["commandWindows"]
        proc = subprocess.run([powershell, "-NoProfile", "-Command",
                               _CODEX_POWERSHELL_WRAPPER.format(command=legacy)],
                              input=payload, text=True, capture_output=True, env=env,
                              timeout=60, **no_console())
        assert_true(proc.returncode != 0, "the 1.0.0 form fails under the wrapper (the bug this guards)")


def _codex_hook_quoting():
    """Both shells get single-quoted paths: `$`/backticks stay literal under a POSIX shell,
    and a home holding `'` stays Exoma's (owned, present) and keeps a custom handler's
    reference to the script visible to uninstall, under either escaping."""
    from exoma.installer import base

    # Two apostrophes in a row: decoding POSIX then PowerShell in a chain changed the path.
    with tempfile.TemporaryDirectory(prefix="exoma-codex-o''q-") as tmp:
        home = Path(tmp)
        script = base.codex_host_hook_path(home).as_posix()
        for custom in ("'py' " + base._sh_quote(script) + " custom",
                       "& 'py' " + base._ps_quote(script) + " Custom"):
            current = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": custom}]}]}}
            assert_true(base.codex_hooks_reference_script(current, home),
                        f"a home holding '' keeps a custom handler's script: {custom}")

    with tempfile.TemporaryDirectory(prefix="exoma-codex-o'q-$x`y-") as tmp:
        home = Path(tmp)
        entries = base.codex_hook_entries(home)
        for event, groups in entries.items():
            handler = groups[0]["hooks"][0]
            assert_true(handler["command"].count("'") >= 4 and '"' not in handler["command"],
                        f"POSIX command is single-quoted: {handler['command']}")
            assert_true(base._codex_hook_owned(handler, home, event),
                        f"{event}: a quote-bearing home stays Exoma's under both escapings")
        assert_true(base.codex_hook_events_present(entries, home), "and every event is present")
        script = base.codex_host_hook_path(home).as_posix()
        for custom in ("& 'py' " + base._ps_quote(script) + " Custom",
                       "'py' " + base._sh_quote(script) + " custom"):
            current = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": custom}]}]}}
            assert_true(base.codex_hooks_reference_script(current, home),
                        f"a custom handler naming the script through its escaping keeps it: {custom}")

        # The launch uses `entries`, rendered for this home: it stays inside this context.
        if os.name == "nt" or not shutil.which("sh"):
            return
        root = home / "project"
        (root / ".workflow").mkdir(parents=True)
        env = dict(os.environ, HOME=str(home), AGENT_HOME=str(home))
        env.pop("WORKFLOW_AGENT_ROLE", None)
        _hook(home, "SessionStart", {"session_id": "warm", "cwd": str(root), "source": "startup"})
        command = entries["SessionStart"][0]["hooks"][0]["command"]
        payload = json.dumps({"session_id": "codex-sh", "cwd": str(root), "source": "startup"}) + "\n"
        proc = subprocess.run(["sh", "-c", command], input=payload, text=True, capture_output=True,
                              env=env, timeout=60, **no_console())
        assert_true(proc.returncode == 0 and isinstance(json.loads(proc.stdout or "null"), dict),
                    f"the POSIX command launches with `$`, a backtick and `'` in the home: "
                    f"rc={proc.returncode} {proc.stderr[-400:]}")


def _test_codex_host_install():
    from tests.checks.codex_auto_review import check_codex_auto_review
    check_codex_auto_review()
    from exoma.installer import base, codex_policy, uninstall
    from exoma.install import _resolve_hosts

    with tempfile.TemporaryDirectory(prefix="exoma-codex-install-") as tmp:
        home = Path(tmp)
        saved = base.HOME, uninstall.HOME
        base.HOME = uninstall.HOME = home
        try:
            assert_true(_resolve_hosts("all", False, base.Plan()) == base.HOSTS,
                        "all includes Claude, Agy, and Codex")
            codex = home / ".codex"
            codex.mkdir()
            (codex / "AGENTS.md").write_text("User instructions\n\n<!-- WORKFLOW-SECOND-AGENT:START -->\nsecond\n<!-- WORKFLOW-SECOND-AGENT:END -->\n", encoding="utf-8")
            script = base.codex_host_hook_path(home).as_posix()
            foreign_handlers = [{"type": "command", "command": "echo foreign"}]
            foreign = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": foreign_handlers}]}}
            owned = base.codex_hook_entries(home)["PreToolUse"][0]["hooks"][0]
            assert_true(base._codex_hook_owned(owned, home, "PreToolUse")
                        and not base._codex_hook_owned(owned, home, "Stop"),
                        "only the generated command in its registered event belongs to Exoma")
            (codex / "hooks.json").write_text(json.dumps(foreign), encoding="utf-8")
            for src, dest, key in base._targets(("codex",)):
                if key.startswith("hosts/codex/"):
                    base._install_text(src, dest, key, base.Plan(), True, home / "backup", None)
            agents = (codex / "AGENTS.md").read_text(encoding="utf-8")
            hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
            assert_true("User instructions" in agents and "WORKFLOW-SECOND-AGENT:START" in agents
                        and "EXOMA-CODEX-HOST:START" in agents, "host and provider blocks coexist with user text")
            assert_true(foreign["hooks"]["PreToolUse"][0] in hooks["hooks"]["PreToolUse"],
                        "installer preserves a foreign Codex hook")
            source = base.DIST_CONFIG / base.CODEX_HOST_HOOKS
            base._install_text(source, codex / "hooks.json", base.CODEX_HOST_HOOKS,
                               base.Plan(), True, home / "backup-again", None)
            assert_true(json.loads((codex / "hooks.json").read_text(encoding="utf-8")) == hooks,
                        "reinstalling Codex hooks is idempotent")
            assert_true((codex / "skills" / "exoma-plan" / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: exoma-plan"),
                        "Codex skill has native frontmatter")
            # Codex has no command files: its skills are the commands, and the host block says so.
            assert_true(not (codex / "prompts").exists() and not (codex / "commands").exists(),
                        "no Codex command directory is written")
            assert_true("`$exoma-<name>` skills" in agents and "/skills" in agents
                        and "require_escalated" in agents and "approval_policy=never" in agents
                        and "host denial" in agents,
                        "the Codex host block names skills and policy-aware runner permissions")
            contract = (codex / "exoma" / "CONTRACT.md").read_text(encoding="utf-8")
            assert_true("require_escalated" in contract and "approval_policy=never" in contract
                        and "BEFORE process creation" in contract and "existing user authorization" in contract,
                        "the rendered contract distinguishes conditional escalation and pre-execution denial")
            from exoma.core.audit.bundle_integrity import _host_status
            mode = home / ".claude" / ".workflow-install-mode.json"
            mode.parent.mkdir(parents=True)
            mode.write_text(json.dumps({"hosts": ["codex"]}), encoding="utf-8")
            assert_true(_host_status(home)["hosts"]["codex"]["missing"] == [],
                        "doctor recognizes a complete Codex host")
            changed_python = json.loads(json.dumps(hooks))
            for event, groups in changed_python["hooks"].items():
                for group in groups:
                    for handler in group["hooks"]:
                        if base._codex_hook_owned(handler, home):
                            for key in ("command", "commandWindows"):
                                call = "& " if handler[key].startswith("&") else ""
                                quote = handler[key].removeprefix(call)[0]
                                rest = handler[key].removeprefix(call).split(quote + " ", 1)[1]
                                handler[key] = f"{call}{quote}different-python{quote} {rest}"
            (codex / "hooks.json").write_text(json.dumps(changed_python), encoding="utf-8")
            assert_true(_host_status(home)["hosts"]["codex"]["missing"] == [],
                        "doctor accepts an Exoma hook installed by another Python")
            (codex / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
            decoy = {"hooks": {event: [{"hooks": [{"type": "command", "command": "echo codex-host.py"}]}]
                               for event in base.codex_hook_entries()}}
            (codex / "hooks.json").write_text(json.dumps(decoy), encoding="utf-8")
            assert_true("~/.codex/hooks.json (Exoma lifecycle hooks)" in
                        _host_status(home)["hosts"]["codex"]["missing"],
                        "doctor rejects a hook that only mentions the Exoma script")
            (codex / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
            mode.unlink()
            assert_true("codex" in _host_status(home)["chosen"],
                        "doctor infers Codex from its hooks when the mode file is lost")
            (codex / "hooks.json").write_text(json.dumps(decoy), encoding="utf-8")
            assert_true("codex" not in _host_status(home)["chosen"],
                        "a hook that only mentions the script does not infer Codex")
            (codex / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
            mode.write_text(json.dumps({"hosts": ["codex"]}), encoding="utf-8")
            uninstall._uninstall_text(codex / "AGENTS.md", base.CODEX_HOST_INSTRUCTIONS, base.Plan(), True, home / "backup2")
            assert_true(uninstall._uninstall_codex_hooks(base.Plan(), True, home / "backup2"), "Codex hooks can be removed")
            remaining = (codex / "AGENTS.md").read_text(encoding="utf-8")
            hooks = json.loads((codex / "hooks.json").read_text(encoding="utf-8"))
            assert_true("WORKFLOW-SECOND-AGENT:START" in remaining and "EXOMA-CODEX-HOST" not in remaining,
                        "removing the Codex host retains the Codex provider")
            assert_true(hooks == foreign, "removing the Codex host retains foreign hooks")
            user_references = [
                {"type": "command", "command": f'echo "{script}" PreToolUse'},
                {"type": "command", "command": f'"{sys.executable}" "{script}" PreToolUse --log'},
            ]
            assert_true(all(not base._codex_hook_owned(handler, home, "PreToolUse")
                            for handler in user_references),
                        "a path mention or extra arguments do not claim a user hook")
            hooks["hooks"]["PreToolUse"][0]["hooks"].extend(user_references)
            referenced = json.loads(json.dumps(hooks))
            (codex / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
            base._install_text(source, codex / "hooks.json", base.CODEX_HOST_HOOKS,
                               base.Plan(), True, home / "backup-referenced", None)
            referenced_plan = base.Plan()
            assert_true(not uninstall._uninstall_codex_hooks(referenced_plan, True, home / "backup-referenced-out")
                        and json.loads((codex / "hooks.json").read_text(encoding="utf-8")) == referenced
                        and any("script was kept" in warning for warning in referenced_plan.warnings),
                        "install and uninstall preserve user hooks mentioning the script")
            mixed = dict(owned)
            mixed["commandWindows"] = "echo foreign on Windows"
            referenced["hooks"]["PreToolUse"][0]["hooks"].append(mixed)
            (codex / "hooks.json").write_text(json.dumps(referenced), encoding="utf-8")
            mixed_plan = base.Plan()
            assert_true(not uninstall._uninstall_codex_hooks(mixed_plan, True, home / "backup-mixed")
                        and any("script was kept" in warning for warning in mixed_plan.warnings),
                        "a modified cross-platform hook keeps its script until the owner repairs it")
            # An unreadable registration must not leave an active host contract behind.
            (codex / "hooks.json").write_text("{bad", encoding="utf-8")
            with patch.object(uninstall, "previously_installed", return_value={}):
                assert_true(uninstall._uninstall_codex_hooks(base.Plan(), False, home / "backup-foreign"),
                            "a foreign malformed hooks.json cannot block a Claude-only uninstall")
            with patch.object(uninstall, "previously_installed", return_value={
                    str(codex / "hooks.json"): {"key": base.CODEX_HOST_HOOKS}}):
                assert_true(not uninstall._uninstall_codex_hooks(base.Plan(), False, home / "backup-owned"),
                            "a malformed owned Codex registration still protects its scripts")
            (codex / "AGENTS.md").write_text(agents, encoding="utf-8")
            plan = base.Plan()
            base._install_text(base.DIST_CONFIG / base.CODEX_HOST_INSTRUCTIONS, codex / "AGENTS.md",
                               base.CODEX_HOST_INSTRUCTIONS, plan, True, home / "backup-broken", None)
            assert_true("EXOMA-CODEX-HOST" not in (codex / "AGENTS.md").read_text(encoding="utf-8")
                        and any("hooks" in w.lower() for w in plan.warnings),
                        "invalid hooks.json withdraws an earlier Codex host block")
        finally:
            base.HOME, uninstall.HOME = saved

    _codex_windows_hooks_launch()
    _codex_hook_quoting()

    # Exercise the public installer entry point with an isolated agent home.
    with tempfile.TemporaryDirectory(prefix="exoma-codex-cli-") as tmp:
        home = Path(tmp)
        fakebin = home / "bin"
        fakebin.mkdir()
        command = fakebin / ("codex.cmd" if os.name == "nt" else "codex")
        command.write_text("@echo off\necho codex-cli 0.163.0\n" if os.name == "nt"
                           else '#!/bin/sh\nprintf "codex-cli 0.163.0\\n"\n', encoding="utf-8")
        if os.name != "nt":
            command.chmod(0o755)
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), AGENT_HOME=str(home),
                   CODEX_HOME=str(home / ".codex"), AGENT_ENV_FILE=str(home / "no-env"),
                   PATH=str(fakebin) + os.pathsep + os.environ.get("PATH", ""))
        env["PYTHONPATH"] = str(REPO / "src")
        result = subprocess.run([sys.executable, "-m", "exoma.install", "codex",
                                 "--provider", "codex"], cwd=home, env=env, text=True, capture_output=True,
                                timeout=60, **no_console())
        assert_true(result.returncode == 0, "public Codex install succeeds: " + result.stdout[-500:] + result.stderr[-500:])
        codex = home / ".codex"
        assert_true((codex / "hooks.json").is_file() and (codex / "AGENTS.md").is_file(),
                    "public install writes Codex host registration and instructions")
        assert_true(codex_policy.START in (codex / "config.toml").read_text(encoding="utf-8"),
                    "the public host install also writes reviewer context without an extra flag")
        assert_true("EXOMA-CODEX-HOST" in (codex / "AGENTS.md").read_text(encoding="utf-8")
                    and "WORKFLOW-SECOND-AGENT" in (codex / "AGENTS.md").read_text(encoding="utf-8"),
                    "public install supports Codex as host and second agent together")
        checked = subprocess.run([sys.executable, "-m", "exoma.install", "--check"], cwd=home, env=env,
                                 text=True, capture_output=True, timeout=30, **no_console())
        assert_true(checked.returncode == 0, "public install passes --check: " + checked.stdout[-500:])
        skill_path = codex / "skills/exoma-analyze/SKILL.md"
        installed_skill = skill_path.read_text(encoding="utf-8")
        assert_true("$exoma-analyze" in installed_skill and "/.analyze" not in installed_skill
                    and "Claude only" not in installed_skill, "public install renders the entire native skill")
        skill_path.write_text(installed_skill + "\n/.analyze stale payload\n", encoding="utf-8")
        agents = codex / "AGENTS.md"
        agents.write_text("User-owned instructions.\n" + agents.read_text(encoding="utf-8"), encoding="utf-8")
        for _ in range(2):
            refreshed = subprocess.run([sys.executable, "-m", "exoma.install", "--no-input"], cwd=home, env=env,
                                       text=True, capture_output=True, timeout=60, **no_console())
            assert_true(refreshed.returncode == 0, "flagless refresh preserves Codex selection: " + refreshed.stdout[-500:])
            assert_true(skill_path.read_text(encoding="utf-8") == installed_skill,
                        "refresh repairs a stale payload and is idempotent")
            assert_true(agents.read_text(encoding="utf-8").startswith("User-owned instructions."),
                        "refresh preserves text outside managed instruction blocks")
        retired = subprocess.run([sys.executable, "-m", "exoma.install", "both", "--dry-run"], cwd=home, env=env,
                                 text=True, capture_output=True, timeout=30, **no_console())
        assert_true(retired.returncode == 2, "the 0.x `both` host choice is gone in 1.0")
        legacy = subprocess.run([sys.executable, "-m", "exoma.install", "--dry-run", "--host", "codex"], cwd=home,
                                env=env, text=True, capture_output=True, timeout=30, **no_console())
        assert_true(legacy.returncode == 0 and "hosts:  codex" in legacy.stdout and "--host is deprecated" in legacy.stdout,
                    "--host still names a host, with a deprecation notice: " + legacy.stdout[-500:])
        # A lost mode file: a flagless install still finds Codex through its hooks.
        (home / ".claude" / ".workflow-install-mode.json").unlink()
        flagless = subprocess.run([sys.executable, "-m", "exoma.install", "--dry-run"], cwd=home, env=env,
                                  text=True, capture_output=True, timeout=30, **no_console())
        assert_true(flagless.returncode == 0 and "hosts:  claude, codex" in flagless.stdout,
                    "a flagless install infers Codex when the mode file is lost: " + flagless.stdout[-500:])
        config = codex / "config.toml"
        original_config = config.read_bytes()
        config.unlink()
        audited = subprocess.run([
            sys.executable, "-c",
            "import json; from exoma.installer.base import DIST_CONFIG; "
            "from exoma.core.audit.bundle_integrity import _bundle_integrity; "
            "print(json.dumps(_bundle_integrity(DIST_CONFIG, DIST_CONFIG.parent / 'manifest.json')))",
        ], cwd=home, env=env, text=True, capture_output=True, timeout=30, **no_console())
        assert_true(audited.returncode == 0, "lost-mode doctor runs: " + audited.stderr[-500:])
        audit = json.loads(audited.stdout)
        assert_true(codex_policy.TEMPLATE_KEY in audit["missing"],
                    "lost-mode doctor reports missing Codex context instead of skipping the host")
        assert_true(not config.exists() and not (home / ".claude/.workflow-install-mode.json").exists(),
                    "doctor infers a lost host choice without recreating any config")
        checked_missing = subprocess.run([sys.executable, "-m", "exoma.install", "--check"],
                                         cwd=home, env=env, text=True, capture_output=True,
                                         timeout=30, **no_console())
        assert_true(checked_missing.returncode == 1 and "Auto-review context: missing" in checked_missing.stdout,
                    "doctor and installer-check agree when Codex context and install-mode are missing")
        config.write_bytes(original_config)
        # A refused hooks.json withdraws the AGENTS.md block; --check reports the hooks only.
        (codex / "hooks.json").write_text("{bad", encoding="utf-8")
        subprocess.run([sys.executable, "-m", "exoma.install", "codex",
                        "--provider", "codex"], cwd=home, env=env, text=True, capture_output=True,
                       timeout=60, **no_console())
        assert_true("EXOMA-CODEX-HOST" not in (codex / "AGENTS.md").read_text(encoding="utf-8"),
                    "a refused hooks.json withdraws the Codex host block")
        refused = subprocess.run([sys.executable, "-m", "exoma.install", "--check"], cwd=home, env=env,
                                 text=True, capture_output=True, timeout=30, **no_console())
        assert_true(f"DRIFTED {base.CODEX_HOST_HOOKS}" in refused.stdout
                    and base.CODEX_HOST_INSTRUCTIONS not in refused.stdout,
                    "--check reports the refused hooks, not the withdrawn block: " + refused.stdout[-800:])
