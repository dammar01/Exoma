"""The public update command refreshes the package before using the fresh CLI."""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from exoma import self_update
from tests.checks.support import assert_true, no_console

REPO = Path(__file__).resolve().parents[2]


def _test_self_update() -> None:
    if os.environ.get("EXOMA_UPDATE_HANDOFF_SMOKE") == "1":
        _check_handoff_wheel_upgrade()
        return
    _check_release_queries()
    _check_artifact_reporting()
    _check_handshake_reads()
    _check_handoff_uncertainty()
    _check_handoff_polling()
    _check_cleanup_publication()
    _check_guardian_diagnostics()
    _check_guardian_cleanup_attempts()
    _check_ready_failure_context()
    _check_worker_protocol()
    _check_synchronous_update()
    _check_update_deadlines_and_interruption()


def _check_synchronous_update() -> None:
    """Frontend admission/manager tests; stages are tested through the shared worker."""
    with tempfile.TemporaryDirectory(prefix="exoma-update-") as tmp:
        root = Path(tmp).resolve()
        project = root / "project"
        project.mkdir()
        with patch.object(self_update.Path, "cwd", return_value=project), \
             patch.object(self_update, "_package_command", return_value=("pip", [sys.executable, "-m", "pip"])), \
             patch.object(self_update, "_handoff", return_value=3) as handoff:
            for windows in (False, True):
                with patch.object(self_update, "IS_WINDOWS", windows):
                    assert_true(self_update.main(["--manager", "pip"]) == 3,
                                "every platform delegates to the same accepted-not-complete handoff")
            assert_true(handoff.call_count == 2, "no synchronous production fork remains")
            (project / ".workflow").mkdir()
            for invalid in (None, "{broken", "[]", '{"runtime":[]}'):
                config = project / ".workflow/config.json"
                if invalid is not None:
                    config.write_text(invalid, encoding="utf-8")
                handoff.reset_mock()
                assert_true(self_update.main(["--manager", "pip"]) == 1,
                            "invalid workspace refuses before any handoff")
                handoff.assert_not_called()
        uv_dir = root / "uv-tools"
        with patch.object(self_update.shutil, "which", return_value="uv"), \
             patch.object(self_update.sys, "prefix", str(uv_dir / "exoma")), \
             patch.object(self_update.install_method, "run_bounded", return_value=(0, str(uv_dir), "")):
            assert_true(self_update._package_command("auto") == ("uv", ["uv", "tool", "upgrade", "exoma"]),
                        "uv tool installs update through uv")
        with patch.object(self_update.shutil, "which", return_value="uv"), \
             patch.object(self_update.sys, "prefix", str(root / "other-python")), \
             patch.object(self_update.install_method, "run_bounded", return_value=(0, str(uv_dir), "")):
            assert_true(self_update._package_command("auto")[0] == "pip",
                        "an unrelated uv installation does not change the current interpreter")
            try:
                self_update._package_command("uv")
            except ValueError:
                pass
            else:
                raise AssertionError("explicit uv cannot update a different Exoma installation")

        # pipx (the usual Linux/macOS route past PEP 668): recognised by its metadata file
        # in the environment, not by a pipx on PATH.
        pipx_env = root / "pipx" / "venvs" / "exoma"
        pipx_env.mkdir(parents=True)
        (pipx_env / "pipx_metadata.json").write_text("{}", encoding="utf-8")
        with patch.object(self_update, "_uv_tool", return_value=None), \
             patch.object(self_update.shutil, "which", return_value="/bin/pipx"), \
             patch.object(self_update.sys, "prefix", str(pipx_env)):
            assert_true(self_update._package_command("auto") == ("pipx", ["/bin/pipx", "upgrade", "exoma"]),
                        "a pipx install updates through pipx")
            assert_true(self_update._package_command("pipx")[0] == "pipx", "explicit pipx is accepted in a pipx env")
        with patch.object(self_update, "_uv_tool", return_value=None), \
             patch.object(self_update.shutil, "which", return_value=None), \
             patch.object(self_update.sys, "prefix", str(pipx_env)):
            try:
                self_update._package_command("auto")
            except ValueError as exc:
                assert_true("pipx is not on PATH" in str(exc), f"a pipx env without pipx says so: {exc}")
            else:
                raise AssertionError("a pipx env without pipx on PATH must not fall back to pip")
        with patch.object(self_update, "_uv_tool", return_value=None), \
             patch.object(self_update.shutil, "which", return_value="/bin/pipx"), \
             patch.object(self_update.sys, "prefix", str(root / "other-python")):
            assert_true(self_update._package_command("auto")[0] == "pip",
                        "pipx on PATH does not make a plain environment pipx")
            try:
                self_update._package_command("pipx")
            except ValueError:
                pass
            else:
                raise AssertionError("explicit pipx cannot update an Exoma outside pipx")

        # PEP 668: pip refuses a distribution-managed Python; the update names pipx and uv.
        # pip's output is not captured (it prints as it runs); the marker file decides.
        for managed, named in ((True, True), (False, False)):
            errors = io.StringIO()
            with patch.object(self_update, "_externally_managed", return_value=managed), \
                 patch.object(self_update, "run_bounded", return_value=(1, "", "")) as pip_run, \
                 redirect_stderr(errors):
                assert_true(not self_update._run_step("package", [sys.executable, "-m", "pip"], root, pip=True),
                            "a failed pip step fails")
            assert_true(pip_run.call_args.kwargs.get("inherit_output") is True,
                        "pip's output is left on the terminal, live")
            hinted = "pipx install exoma" in errors.getvalue() and "uv tool install exoma" in errors.getvalue()
            assert_true(hinted == named,
                        f"the pipx and uv routes are named exactly when the env is externally managed ({managed}): "
                        f"{errors.getvalue()}")

        # The marker: EXTERNALLY-MANAGED in the stdlib directory, outside a venv only.
        stdlib = root / "stdlib"
        stdlib.mkdir()
        (stdlib / "EXTERNALLY-MANAGED").write_text("[externally-managed]\n", encoding="utf-8")
        with patch.object(self_update.sysconfig, "get_path", return_value=str(stdlib)), \
             patch.object(self_update.sys, "prefix", "/usr"), \
             patch.object(self_update.sys, "base_prefix", "/usr"):
            assert_true(self_update._externally_managed(), "the marker outside a venv is externally managed")
        with patch.object(self_update.sysconfig, "get_path", return_value=str(stdlib)), \
             patch.object(self_update.sys, "prefix", "/home/u/venv"), \
             patch.object(self_update.sys, "base_prefix", "/usr"):
            assert_true(not self_update._externally_managed(), "a venv over that Python is not")

    for spelling in (("update", "--help"), ("--command", "update", "--help")):
        result = subprocess.run([sys.executable, str(REPO / "main.py"), *spelling],
                                capture_output=True, text=True, timeout=15, **no_console())
        assert_true(result.returncode == 0 and "--manager" in result.stdout and "pipx" in result.stdout and "--timeout" in result.stdout,
                    f"public CLI routes {spelling} to update help, pipx among the managers")




def _check_update_deadlines_and_interruption() -> None:
    with tempfile.TemporaryDirectory(prefix="exoma-update-deadline-") as tmp:
        root = Path(tmp).resolve()
        (root / ".workflow").mkdir()
        (root / ".workflow" / "config.json").write_text("{}", encoding="utf-8")
        for failure in (None, KeyboardInterrupt(), OSError("blocked")):
            def run(argv, timeout, **kwargs):
                kwargs["on_tick"](0.1)
                if isinstance(failure, BaseException):
                    raise failure
                return failure, "", ""
            with patch.object(self_update, "run_bounded", side_effect=run):
                try:
                    result = self_update._run_step("package", ["unused"], root, timeout=0.25)
                except KeyboardInterrupt:
                    assert_true(isinstance(failure, KeyboardInterrupt), "interrupt propagates after native cleanup")
                else:
                    assert_true(result is False, "deadline and spawn failures do not claim success")

        for invalid in ("0", "-1", "nan", "inf", "-inf", "text"):
            with patch.object(self_update, "run_bounded") as not_called, redirect_stderr(io.StringIO()):
                try:
                    self_update.main(["--timeout=" + invalid])
                except SystemExit as exc:
                    assert_true(exc.code == 2, "invalid deadlines produce an argument error")
                else:
                    raise AssertionError(f"invalid timeout accepted: {invalid}")
                not_called.assert_not_called()

        method = self_update.install_method
        with patch.object(method.shutil, "which", return_value="uv"), \
             patch.object(method, "run_bounded", return_value=(None, "", "")) as detect_run, \
             patch.object(method, "pipx_venv") as pipx_fallback, \
             patch.object(self_update, "run_bounded") as package_run, \
             patch.object(self_update.Path, "cwd", return_value=root), \
             redirect_stderr(io.StringIO()) as err:
            assert_true(self_update.main([]) == 2, "a detection timeout refuses update before package changes")
            assert_true("no fallback to pip" in err.getvalue(), "detection failure explains manager uncertainty")
            assert_true(method.detect() == ("unknown", None), "shared detection is unknown after timeout")
            assert_true(detect_run.call_args.args[1] == method.DETECTION_TIMEOUT_SECONDS,
                        "uv discovery has its own short deadline")
            package_run.assert_not_called()
            pipx_fallback.assert_not_called()

        from exoma.installer import settings as installer_settings
        from exoma.installer.base import Plan

        requirements = root / "requirements.txt"
        requirements.write_text("playwright==1.60.0\n", encoding="utf-8")
        for apply in (False, True):
            plan = Plan()
            with patch.object(method, "uv_tool", side_effect=method.DetectionTimeout("uv detection timed out")), \
                 patch.object(installer_settings.subprocess, "run") as no_install:
                outcome, failure = installer_settings._pip_install(plan, apply, requirements, "empty")
            assert_true(outcome == ("failed" if apply else "unavailable"),
                        "optional installation refuses an inconclusive manager in apply and dry-run modes")
            detail = " ".join(plan.warnings)
            assert_true("uv detection timed out" in detail and "ensurepip" not in detail
                        and "no pip, uv tool or pipx environment" not in detail,
                        "a detection timeout retains its cause without claiming the environment is absent")
            assert_true(bool(failure) == apply, "only an applied install returns the detection failure")
            no_install.assert_not_called()

        out, err = io.StringIO(), io.StringIO()
        with patch.object(self_update, "run_bounded", return_value=(0, "", "")), \
             redirect_stdout(out), redirect_stderr(err):
            assert_true(self_update._run_step("check", ["unused"], root), "successful step returns true")
        assert_true("completed in" in out.getvalue() and "still waiting" not in err.getvalue(),
                    "successful steps report elapsed completion without a stale heartbeat")


def _seed_migration_user_data(home: Path, project: Path) -> None:
    instructions = home / ".claude/CLAUDE.md"
    instructions.write_text(instructions.read_text(encoding="utf-8") + "\nUser prose survives migration.\n",
                            encoding="utf-8")
    settings_path = home / ".claude/settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["migration_user_setting"] = {"keep": True}
    settings.setdefault("hooks", {}).setdefault("Stop", []).append({
        "matcher": "UserOnly", "hooks": [{"type": "command", "command": "echo user-owned-hook"}],
    })
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    config_path = project / ".workflow/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("commands", {}).update(verify_mode="delegated",
                                             verify_test_commands=["python check.py"],
                                             verify_test_timeout_seconds=37)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    sentinel = project / ".workflow/data/sessions/migration-session/runtime/keep.txt"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("keep session data", encoding="utf-8")


def _assert_migrated_user_data(run, home: Path, project: Path) -> None:
    from exoma.utils.osutil import find_bash

    assert_true("User prose survives migration." in (home / ".claude/CLAUDE.md").read_text(encoding="utf-8"),
                "migration preserves prose outside managed instructions")
    settings = json.loads((home / ".claude/settings.json").read_text(encoding="utf-8"))
    assert_true(settings["migration_user_setting"] == {"keep": True}
                and any(entry.get("matcher") == "UserOnly"
                        and entry.get("hooks") == [{"type": "command", "command": "echo user-owned-hook"}]
                        for entry in settings["hooks"]["Stop"]),
                "migration preserves user settings and foreign hooks")
    runtime = project / ".workflow/data/sessions/migration-session/runtime"
    assert_true((runtime / "keep.txt").read_text(encoding="utf-8") == "keep session data",
                "migration preserves session files")
    marker = runtime / "delegated.marker"
    marker.write_text('{"command":"verify"}', encoding="utf-8")
    output = json.loads(run([find_bash() or "bash", project / ".workflow/run.sh",
                             "verify-prepare", "--session", "migration-session", "--main-host", "claude"]))
    meta = output["meta"]
    assert_true(output["ok"] and meta["command"] == "verify-prepare"
                and meta["session_id"] == "migration-session" and meta["request"]["status"] == "missing"
                and meta["next_step"]["action"] == "select_tests"
                and meta["policy"]["test_command_prefixes"] == [["python", "check.py"]]
                and meta["policy"]["test_timeout_seconds"] == 37 and marker.exists(),
                "the migrated runner enters the new preparation flow with preserved policy and marker")


def _check_update_migrates_workflow() -> None:
    """Simulate only package installation; run the actual fresh installer/check in a sandbox."""
    with tempfile.TemporaryDirectory(prefix="exoma-update-migration-") as tmp:
        root = Path(tmp).resolve()
        home, project = root / "home", root / "project"
        home.mkdir()
        project.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(("EXOMA_", "WORKFLOW_", "AGENT_", "CLAUDE_", "GEMINI_", "OPENCODE_"))
               and key.upper() not in ("PYTHONPATH", "PYTHONHOME", "CODEX_HOME", "MAIN_SESSION_ID")}
        env.update(HOME=str(home), USERPROFILE=str(home), AGENT_HOME=str(home),
                   CODEX_HOME=str(home / ".codex"), EXOMA_HOME=str(root / "data"),
                   PYTHONPATH=str(REPO / "src"))
        real_run = self_update.run_bounded

        def run(argv):
            code, out, err = real_run([str(item) for item in argv], 120, cwd=str(project))
            assert_true(code == 0, f"migration command exited zero: {argv}\n{out}\n{err}")
            return out

        calls = []

        def package_only_mock(argv, timeout, **kwargs):
            calls.append(argv)
            if argv[1:4] == ["-m", "pip", "install"]:
                return 0, "package installation simulated", ""
            return real_run(argv, timeout, **kwargs)

        with patch.dict(os.environ, env, clear=True):
            run([sys.executable, "-m", "exoma", "install", "claude", "--provider", "opencode", "--no-input"])
            run([sys.executable, "-m", "exoma", "init", "--work-dir", project, "--host", "claude"])
            _seed_migration_user_data(home, project)
            instructions = home / ".claude/CLAUDE.md"
            artifacts = [instructions, home / ".claude/skills/verify.md",
                         home / ".claude/hooks/session-bind.py", project / ".workflow/run.sh"]
            expected = {path: path.read_bytes() for path in artifacts}
            instructions.write_text("<!-- WORKFLOW-MAIN-AGENT:START -->\nstale owned instructions\n"
                                    "<!-- WORKFLOW-MAIN-AGENT:END -->\nUser prose survives migration.\n",
                                    encoding="utf-8")
            for path in artifacts[1:]:
                path.write_text("stale artifact\n", encoding="utf-8")
            with patch.object(self_update.Path, "cwd", return_value=project), \
                    patch.object(self_update, "run_bounded", side_effect=package_only_mock):
                assert_true(self_update.main(["--manager", "pip"]) == 0,
                            "update refreshes real installed artifacts after the simulated package stage")
            assert_true(len(calls) == 3 and "--no-input" in calls[1] and "--check" in calls[2],
                        "migration uses package, real install, and real check in order")
            # Marker splicing may normalize whitespace outside the block; check owned
            # instruction content through install --check and user prose separately.
            assert_true("stale owned instructions" not in instructions.read_text(encoding="utf-8")
                        and all(path.read_bytes() == expected[path] for path in artifacts[1:]),
                        "update replaces stale instructions, skills, hooks and runner even at the same version")
            _assert_migrated_user_data(run, home, project)
            snapshot = {path: path.read_bytes() for path in [*artifacts, home / ".claude/settings.json",
                                                            project / ".workflow/config.json"]}
            with patch.object(self_update.Path, "cwd", return_value=project), \
                    patch.object(self_update, "run_bounded", side_effect=package_only_mock):
                assert_true(self_update.main(["--manager", "pip"]) == 0, "repeat update completes refresh/check")
            assert_true(all(path.read_bytes() == content for path, content in snapshot.items()),
                        "repeated migration leaves current artifacts and user settings unchanged")
            _assert_migrated_user_data(run, home, project)


def _check_installed_upgrade() -> None:
    """Opt-in artifact test: published baseline -> local wheel, with every write isolated."""
    from exoma.utils import osutil
    from exoma.config.settings import TOOL_VERSION

    candidate = Path(os.environ["EXOMA_UPDATE_WHEEL"]).resolve()
    assert_true(candidate.is_file(), "the upgrade smoke test receives a built candidate wheel")
    uv = shutil.which("uv")
    assert_true(uv is not None, "uv is available for isolated tool installation")
    with urllib.request.urlopen("https://pypi.org/pypi/exoma/1.0.1/json", timeout=30) as response:
        metadata = json.load(response)
    baseline = next(item for item in metadata["urls"] if item["filename"] == "exoma-1.0.1-py3-none-any.whl")
    with urllib.request.urlopen(baseline["url"], timeout=30) as response:
        payload = response.read()
    baseline_hash = hashlib.sha256(payload).hexdigest()
    assert_true(baseline_hash == baseline["digests"]["sha256"], "published baseline wheel matches PyPI SHA-256")

    with tempfile.TemporaryDirectory(prefix="exoma-wheel-upgrade-") as tmp:
        root = Path(tmp).resolve()
        wheels, home, project = root / "wheels", root / "home", root / "project"
        for folder in (wheels, home, project):
            folder.mkdir()
        (wheels / baseline["filename"]).write_bytes(payload)
        env = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("EXOMA_", "WORKFLOW_", "AGENT_", "UV_", "PIP_", "XDG_",
                                           "CLAUDE_", "GEMINI_", "OPENCODE_"))
            and key.upper() not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "CODEX_HOME")
        }
        for key, folder in {
            "HOME": home, "USERPROFILE": home, "AGENT_HOME": home,
            "EXOMA_HOME": root / "exoma-state", "APPDATA": home / "AppData/Roaming",
            "LOCALAPPDATA": home / "AppData/Local", "UV_TOOL_DIR": root / "tools",
            "UV_TOOL_BIN_DIR": root / "bin", "UV_CACHE_DIR": root / "cache",
            "UV_CONFIG_DIR": root / "uv-config", "XDG_CONFIG_HOME": home / ".config",
            "XDG_DATA_HOME": home / ".local/share", "XDG_CACHE_HOME": home / ".cache",
        }.items():
            folder.mkdir(parents=True, exist_ok=True)
            env[key] = str(folder)
        env.update(UV_NO_CONFIG="1", UV_OFFLINE="1", UV_PYTHON_DOWNLOADS="never", UV_LINK_MODE="copy",
                   PATH=str(root / "bin") + os.pathsep + env.get("PATH", ""))
        exe = root / "bin" / ("exoma.exe" if os.name == "nt" else "exoma")

        def run(argv):
            code, out, err = osutil.run_bounded([str(item) for item in argv], 120, cwd=str(project))
            assert_true(code == 0, f"isolated artifact command exited zero: {argv}\n{out}\n{err}")
            return out

        with patch.dict(os.environ, env, clear=True):
            uv_version = run([uv, "--version"]).strip()
            # Unpinned: uv retains the original constraints at tool upgrade. Only the
            # baseline wheel exists at bootstrap; adding the candidate makes it selectable.
            run([uv, "tool", "install", "exoma", "--python", sys.executable,
                 "--no-index", "--find-links", wheels])
            assert_true("1.0.1" in run([exe, "--version"]), "temporary launcher starts the published baseline")
            run([exe, "install", "claude", "--provider", "opencode", "--no-input"])
            run([exe, "init", "--work-dir", project, "--host", "claude"])
            _seed_migration_user_data(home, project)
            shutil.copyfile(candidate, wheels / candidate.name)
            old_code, transition, old_error = osutil.run_bounded([str(exe), "update"], 120, cwd=str(project))
            assert_true(old_code is not None, "the published updater returned within the harness deadline")
            if old_code:
                # This is a recovery-path check, NOT a successful old-updater transition.
                # Keep its exit status in the result so release evidence cannot conflate them.
                assert_true("package failed" in transition + old_error,
                            "the old updater names the failed package stage")
                run([uv, "tool", "upgrade", "exoma", "--verbose"])
                run([exe, "install", "--no-input"])
                run([exe, "install", "--check"])
            else:
                assert_true("[UPDATE] complete" in transition, "successful old update completed refresh/check")
            assert_true(TOOL_VERSION in run([exe, "--version"]), "the candidate version is installed")
            config = json.loads((project / ".workflow/config.json").read_text(encoding="utf-8"))
            assert_true(config["runtime"]["tool_version"] == TOOL_VERSION, "workspace runtime stamp follows upgrade")
            receipts = list((home / ".claude/backups").glob("install_*/install_receipt.json"))
            assert_true(receipts and all(json.loads(path.read_text(encoding="utf-8"))["complete"] for path in receipts),
                        "isolated installs have complete receipts")
            _assert_migrated_user_data(run, home, project)
            second_result = subprocess.run([str(exe), "update", "--timeout", "60"], cwd=project,
                                           capture_output=True, text=True, timeout=90, **no_console())
            assert_true(second_result.returncode == 0, second_result.stdout + second_result.stderr)
            if os.name == "nt":
                _poll_handoff(root / "exoma-state", 180)
            else:
                assert_true("[UPDATE] complete" in second_result.stdout, "synchronous candidate completed")
            run([exe, "install", "--check"])
            _assert_migrated_user_data(run, home, project)
            installed_python = root / "tools/exoma" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            smoke = run([installed_python, "-c", "import exoma, sys; print(exoma.__file__); print(sys.executable)"])
            assert_true(str(root / "tools/exoma") in smoke, "imports and interpreter come from the isolated tool")
        report = {
            "baseline": "1.0.1", "candidate": TOOL_VERSION, "uv": uv_version,
            "python": sys.version.split()[0], "platform": sys.platform,
            "old_updater_exit": old_code, "manual_recovery_checked": bool(old_code),
            "new_updater_exit": 0, "install_check_exit": 0,
            "user_data_preserved": True, "post_upgrade_prepare_checked": True,
            "baseline_sha256": baseline_hash,
            "candidate_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        }
        report_path = os.environ.get("EXOMA_UPDATE_SMOKE_REPORT")
        if report_path:
            Path(report_path).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"UPGRADE SMOKE: 1.0.1 -> {TOOL_VERSION}; old updater exit={old_code}; "
              f"manual recovery={'required and checked' if old_code else 'not needed'}; "
              f"new updater/check=exit 0; {uv_version}; "
              f"baseline SHA256={baseline_hash}; candidate SHA256={hashlib.sha256(candidate.read_bytes()).hexdigest()}")


def _check_release_queries() -> None:
    from exoma import update_metadata as metadata, _update_worker as worker

    local = {"installed": "1.9.0", "running": "1.9.0", "shadowed": False}
    releases = json.dumps({"releases": {"1.9.0": [{}], "1.10.0": [{}],
                                      "99.0": [{"yanked": True}], "2.0rc1": [{}]}}).encode()
    with patch.object(metadata, "distribution_info", return_value=local), \
         patch.object(metadata.urllib.request, "urlopen", return_value=io.BytesIO(releases)), \
         patch.object(self_update, "_update") as no_update:
        out = io.StringIO()
        with redirect_stdout(out):
            assert_true(self_update.main(["--check"]) == 0, "release query completes")
        value = json.loads(out.getvalue())
        assert_true(value["latest_public"] == "1.10.0" and value["comparison"] == "newer_public_release",
                    "release comparison is numeric, excludes yanked and prerelease versions")
        no_update.assert_not_called()
    assert_true(metadata.stable_version("1.0.0") == metadata.stable_version("1")
                and metadata.stable_version("1!2.0") is None
                and metadata.stable_version("1.0.post1") is None, "unsupported versions stay unknown")
    for unsupported in ("1.0.0.post1", "1!2.0", "v2.0", "1.0+local"):
        public = json.dumps({"releases": {"1.0.0": [{}], unsupported: [{}], "2.0rc1": [{}]}}).encode()
        with patch.object(metadata, "distribution_info", return_value={**local, "installed": "1.0.0"}), \
             patch.object(metadata.urllib.request, "urlopen", return_value=io.BytesIO(public)):
            value = metadata.check_release()
        assert_true(value["comparison"] == "unknown" and value["latest_public"] is None
                    and value["latest_supported_stable"] == "1.0.0"
                    and value["unsupported_public_versions"] == [unsupported],
                    "unsupported public releases cannot produce a current/latest claim")
    for public, latest in (({"1.0.post1": [{}]}, None),
                           ({"1.0.0": [{}], "1.0.post1": [{"yanked": True}]}, "1.0.0")):
        with patch.object(metadata, "distribution_info", return_value={**local, "installed": "1.0.0"}), \
             patch.object(metadata.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"releases": public}).encode())):
            value = metadata.check_release()
        assert_true(value["latest_public"] == latest, "unsupported-only is unknown; yanked versions do not obscure stable releases")
    assert_true(all(metadata.recognized_prerelease(version) for version in
                    ("2.0a1", "2.0b2", "2.0rc3", "2.0.dev1"))
                and not metadata.recognized_prerelease("1.0.post1"), "post-releases are not prereleases")
    with patch.object(metadata, "distribution_info", return_value={**local, "shadowed": True}), \
         patch.object(metadata.urllib.request, "urlopen", return_value=io.BytesIO(releases)):
        assert_true(metadata.check_release()["comparison"] == "unknown", "checkout shadowing is explicit")
    with patch.object(metadata.urllib.request, "urlopen", side_effect=OSError("offline")), \
         redirect_stderr(io.StringIO()):
        assert_true(self_update.main(["--check"]) == 1, "offline is not reported as up to date")
    for invalid in (b"[]", b'{"releases":[]}', b"x" * (metadata.MAX_METADATA_BYTES + 1)):
        with patch.object(metadata.urllib.request, "urlopen", return_value=io.BytesIO(invalid)), \
             redirect_stderr(io.StringIO()):
            assert_true(self_update.main(["--check"]) == 1, "invalid/oversize metadata fails cleanly")
    with redirect_stderr(io.StringIO()):
        try:
            self_update.main(["--check", "--status"])
        except SystemExit as exc:
            assert_true(exc.code == 2, "query flags are mutually exclusive")
        else:
            raise AssertionError("conflicting queries accepted")
    with tempfile.TemporaryDirectory(prefix="exoma-status-") as tmp:
        root = Path(tmp)
        key = worker.installation_key(sys.prefix, "fixture-user")
        operation = root / key / ("a" * 32)
        operation.mkdir(parents=True)
        worker.atomic_json(operation.parent / "latest.json", {"operation_id": operation.name})
        state = {"schema_version": worker.SCHEMA, "installation_key": key, "operation_id": operation.name,
                 "phase": "succeeded", "stages": {stage: {"exit_code": 0} for stage in
                                                   ("package", "probe", "install", "check")}}
        class API:
            def user(self):
                return "fixture-user"
            def open_identity(self, *args):
                return 1
            def signaled(self, handle):
                return False
            def close(self, handle):
                pass
        with patch.object(self_update, "IS_WINDOWS", True), \
             patch.object(self_update, "_update_root", return_value=root), \
             patch.object(worker, "platform_api", return_value=API()), \
             patch.object(self_update, "_update") as no_update, redirect_stdout(io.StringIO()), \
             redirect_stderr(io.StringIO()):
            for phase, expected in (("succeeded", 0), ("failed", 1), ("accepted", 3)):
                state.update(phase=phase, helper={"pid": 123}, heartbeat=time.time())
                worker.atomic_json(operation / "state.json", state)
                before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
                assert_true(self_update.main(["--status"]) == expected, f"status exit contract: {phase}")
                after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
                assert_true(before == after, "status does not write state or repair journal")
            rejected = root / key / ("b" * 32)
            rejected.mkdir()
            worker.atomic_json(rejected / "state.json", {"phase": "failed", "error": "host job refused"})
            for phase, expected in (("succeeded", 0), ("accepted", 3)):
                state.update(phase=phase, heartbeat=time.time())
                worker.atomic_json(operation / "state.json", state)
                output = io.StringIO()
                with redirect_stdout(output):
                    assert_true(self_update.main(["--status"]) == expected, "refused attempt does not replace admitted outcome")
                observed = json.loads(output.getvalue())
                assert_true(observed["operation_id"] == operation.name
                            and observed["status_scope"] == "last_published_operation"
                            and "not necessarily the last attempted command" in observed["status_notice"]
                            and worker.read_json(operation.parent / "latest.json")["operation_id"] == operation.name,
                            "status explicitly distinguishes older success/active work from a newer rejected attempt")
            state["heartbeat"] = 0
            worker.atomic_json(operation / "state.json", state)
            before = (operation / "state.json").read_bytes()
            assert_true(self_update.main(["--status"]) == 4, "stale live helper yields unknown")
            assert_true((operation / "state.json").read_bytes() == before, "unknown is observational")
            (operation / "state.json").write_text("{broken", encoding="utf-8")
            assert_true(self_update.main(["--status"]) == 4, "torn/corrupt state is unknown")
            no_update.assert_not_called()


def _read_handoff_state(root):
    from exoma import _update_worker as worker

    states = list(Path(root).glob("updates/*/*/state.json"))
    if not states:
        return None, None
    state_path = max(states, key=lambda path: path.stat().st_mtime_ns)
    return worker.read_json(state_path), state_path


def _handoff_diagnostics(value, state_path):
    keys = ("phase", "heartbeat", "stages", "cleanup_error", "error", "helper",
            "completed", "cleanup_confirmed")
    summary = {key: value[key] for key in keys if key in value} if value is not None else None
    parts = [f"state path: {state_path or 'unavailable'}",
             "last state: " + json.dumps(summary, ensure_ascii=False, default=str)[:6000]]
    budget = 12000
    if state_path is not None:
        for path in sorted(state_path.parent.glob("*.log")):
            if budget <= 0:
                break
            try:
                size = min(6000, budget)
                with path.open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell() - size))
                    tail = stream.read(size).decode("utf-8", errors="replace")
            except OSError as exc:
                tail = f"log unavailable: {exc}"
            parts.append(path.name + ":\n" + tail)
            budget -= len(tail)
    return "\n".join(parts)


def _poll_handoff(root, timeout):
    deadline = time.monotonic() + timeout
    last_error = None
    last_value, last_path = None, None
    while time.monotonic() < deadline:
        try:
            value, state_path = _read_handoff_state(root)
        except (FileNotFoundError, PermissionError) as exc:
            last_error = exc
        else:
            last_error = None
        if last_error is None and value is not None:
            last_value, last_path = value, state_path
            if value["phase"] in ("succeeded", "failed", "interrupted", "cleanup_pending"):
                if value["phase"] != "succeeded":
                    raise AssertionError("handoff outcome:\n" + _handoff_diagnostics(value, state_path))
                return value, state_path
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))
    detail = f"; last read error: {last_error}" if last_error is not None else ""
    raise AssertionError(f"handoff did not finish within {timeout}s: {root}{detail}\n"
                         + _handoff_diagnostics(last_value, last_path)) from last_error


class _PollingClock:
    def __init__(self):
        self.now, self.sleeps = 0, []
    def monotonic(self):
        self.now += 0.01
        return self.now
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _check_handshake_reads():
    from exoma import _update_worker as worker

    for name in ("ready.json", "accepted.json", "commit.json"):
        clock, checks = _PollingClock(), []
        with patch.object(worker, "read_json", side_effect=[PermissionError(13, "busy", name),
                          FileNotFoundError(name), {"token": "wrong"}, {"token": "expected"}]) as reads, \
             patch.object(worker.time, "monotonic", side_effect=clock.monotonic), \
             patch.object(worker.time, "sleep", side_effect=clock.sleep):
            worker.wait_for_token(Path(name), "expected", 0.5, lambda: checks.append(True))
        assert_true(reads.call_count == 4 and len(checks) == 3 and all(delay <= 0.05 for delay in clock.sleeps),
                    f"{name}: contention, disappearance and wrong token are retried with peer checks")
        clock, attempts = _PollingClock(), []
        denied = PermissionError(13, "persistent denial", name)
        def deny(path):
            attempts.append(path)
            if len(attempts) > 10:
                raise AssertionError("handshake deadline was reset")
            raise denied
        with patch.object(worker, "read_json", side_effect=deny), \
             patch.object(worker.time, "monotonic", side_effect=clock.monotonic), \
             patch.object(worker.time, "sleep", side_effect=clock.sleep):
            try:
                worker.wait_for_token(Path(name), "expected", 0.12, lambda: None)
            except TimeoutError as exc:
                assert_true(exc.__cause__ is denied and "persistent denial" in str(exc) and name in str(exc),
                            "deadline failure retains the actual read denial")
            else:
                raise AssertionError("persistent denial accepted as a message")
        assert_true(clock.now < 0.2 and sum(clock.sleeps) <= 0.12, "retries spend only the original deadline")

    live, dead = MagicMock(), MagicMock()
    live.poll.return_value, dead.poll.return_value = None, 7
    with tempfile.TemporaryDirectory(prefix="exoma-handshake-") as tmp:
        root = Path(tmp)
        for payload in ("{broken", "[]"):
            (root / "ready.json").write_text(payload, encoding="utf-8")
            try:
                self_update._wait_for_handshake(root, "ready.json", "expected", live, 1)
            except ValueError:
                pass
            else:
                raise AssertionError("malformed handshake was retried or accepted")
        with patch.object(worker, "read_json", side_effect=[PermissionError(13, "busy"), {"error": "worker stopped"}]):
            try:
                self_update._wait_for_handshake(root, "ready.json", "expected", dead, 1)
            except OSError as exc:
                assert_true("worker stopped" in str(exc), "worker exit is still checked during read contention")
            else:
                raise AssertionError("dead worker kept polling")
        with patch.object(worker, "read_json", side_effect=KeyboardInterrupt()):
            try:
                worker.wait_for_token(root / "commit.json", "expected", 1, lambda: None)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("cancellation was swallowed")
        if os.name == "nt":
            import ctypes
            import threading
            api = worker.Win32()
            create = api.k.CreateFileW
            create.argtypes = [worker.w.LPCWSTR, worker.w.DWORD, worker.w.DWORD, ctypes.c_void_p,
                               worker.w.DWORD, worker.w.DWORD, worker.w.HANDLE]
            create.restype = worker.w.HANDLE
            for name in ("ready.json", "accepted.json", "commit.json"):
                path = root / name
                worker.atomic_json(path, {"token": "expected"})
                handle = create(str(path), 0x80000000, 0, None, 3, 0, None)
                assert_true(handle not in (None, ctypes.c_void_p(-1).value), "exclusive Windows read handle opens")
                release = threading.Timer(0.1, api.close, args=(handle,))
                try:
                    try:
                        worker.read_json(path)
                    except PermissionError:
                        pass
                    else:
                        raise AssertionError("exclusive Windows file did not reproduce the reported read denial")
                    release.start()
                    worker.wait_for_token(path, "expected", 2, lambda: None)
                finally:
                    if release.ident is None:
                        api.close(handle)
                    else:
                        release.join(timeout=3)


def _check_handoff_uncertainty():
    from exoma import _update_worker as worker, update_metadata
    from exoma.core.jobs import job_manager

    with tempfile.TemporaryDirectory(prefix="exoma-handoff-outcome-") as tmp:
        root = Path(tmp)
        jobs = MagicMock()
        jobs.job_dir = root / "jobs"
        jobs.job_dir.mkdir()
        jobs.reserve_update.side_effect = lambda operation, token: root / "reservation.json"
        api = MagicMock()
        api.user.return_value, api.in_job.return_value = "fixture-user", False
        api.job_snapshot.return_value = {"in_job": False}
        descriptor = {"prefix": str(root / "target"), "runtime": sys.executable,
                      "python_relative": "Scripts/python.exe", "entrypoints": []}
        for committed in (False, True):
            state_root = root / str(committed)
            effects = [None, TimeoutError("accepted.json access denied")] if committed else [TimeoutError("ready.json access denied")]
            errors = io.StringIO()
            with patch.object(job_manager, "JobManager", return_value=jobs), \
                 patch.object(self_update.install_method, "update_descriptor", return_value=descriptor), \
                 patch.object(worker, "platform_api", return_value=api), patch.object(worker, "callers", return_value=[]), \
                 patch.object(self_update, "_update_root", return_value=state_root), \
                 patch.object(update_metadata, "distribution_info", return_value={"installed": "fixture-A"}), \
                 patch.object(self_update, "_receipt_names", return_value=set()), \
                 patch.object(self_update, "_workspace_root", return_value=None), \
                 patch.object(self_update.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                 patch.object(self_update.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, create=True), \
                 patch.object(self_update.subprocess, "Popen") as spawn, \
                 patch.dict(os.environ, {"EXOMA_HOME": "relative-update-home"}), \
                 patch.object(self_update, "_wait_for_handshake", side_effect=effects), redirect_stderr(errors):
                code = self_update._handoff("pip", [sys.executable, "-m", "pip"], root, 1)
            assert_true(spawn.call_args.kwargs["env"]["EXOMA_HOME"] == str(Path("relative-update-home").resolve()),
                        "helper receives one absolute storage interpretation before its cwd changes")
            assert_true(code == (4 if committed else 1) and bool(list(state_root.rglob("commit.json"))) == committed,
                        "only an acknowledged READY opens COMMIT publication")
            expected = "handoff outcome unknown" if committed else "refused before package mutation"
            assert_true(expected in errors.getvalue(), "post-COMMIT denial never claims the package was unchanged")


def _check_handoff_polling():
    with tempfile.TemporaryDirectory(prefix="exoma-journal-poll-") as tmp:
        root = Path(tmp)
        state_path = root / "state.json"
        clock = _PollingClock()
        with patch(__name__ + "._read_handoff_state", side_effect=[PermissionError(13, "busy"),
                   FileNotFoundError("replaced"), ({"phase": "succeeded"}, state_path)]), \
             patch.object(time, "monotonic", side_effect=clock.monotonic), \
             patch.object(time, "sleep", side_effect=clock.sleep):
            result, path = _poll_handoff(root, 0.5)
        assert_true(result["phase"] == "succeeded" and path == state_path, "journal poll tolerates temporary read contention")
        clock = _PollingClock()
        with patch(__name__ + "._read_handoff_state", side_effect=PermissionError(13, "persistent journal denial")), \
             patch.object(time, "monotonic", side_effect=clock.monotonic), \
             patch.object(time, "sleep", side_effect=clock.sleep):
            try:
                _poll_handoff(root, 0.12)
            except AssertionError as exc:
                assert_true("persistent journal denial" in str(exc), "poll timeout retains read diagnostics")
            else:
                raise AssertionError("unreadable journal reported success")
        assert_true(clock.now < 0.2, "journal retries preserve the original deadline")
        (root / "worker.log").write_text("early-log-marker\n" + "x" * 7000 + "\nlatest-cleanup-diagnostic",
                                        encoding="utf-8")
        pending = {"phase": "cleanup_pending", "heartbeat": 12,
                   "cleanup_error": "guardian receipt unavailable", "completed": False,
                   "stages": {"package": {"exit_code": 0}}}
        clock = _PollingClock()
        with patch(__name__ + "._read_handoff_state", return_value=(pending, state_path)) as read, \
             patch.object(time, "monotonic", side_effect=clock.monotonic), \
             patch.object(time, "sleep", side_effect=clock.sleep):
            try:
                _poll_handoff(root, 480)
            except AssertionError as exc:
                detail = str(exc)
                assert_true(all(text in detail for text in ("cleanup_pending", str(state_path),
                            "guardian receipt unavailable", "latest-cleanup-diagnostic"))
                            and "early-log-marker" not in detail and len(detail) < 14000,
                            "cleanup-pending refusal retains state and bounded recent logs")
            else:
                raise AssertionError("cleanup-pending journal reported success")
        assert_true(read.call_count == 1 and not clock.sleeps,
                    "cleanup-pending fails acceptance immediately instead of waiting 480 seconds")
        clock = _PollingClock()
        running = {"phase": "package", "heartbeat": 21, "stages": {"package": {"exit_code": None}}}
        observations = [(running, state_path)]
        def denied_after_state(_root):
            if observations:
                return observations.pop()
            raise PermissionError(13, "journal became unreadable")
        with patch(__name__ + "._read_handoff_state", side_effect=denied_after_state), \
             patch.object(time, "monotonic", side_effect=clock.monotonic), \
             patch.object(time, "sleep", side_effect=clock.sleep):
            try:
                _poll_handoff(root, 0.25)
            except AssertionError as exc:
                detail = str(exc)
                assert_true(all(text in detail for text in (str(state_path), '"phase": "package"',
                            '"heartbeat": 21', '"exit_code": null', "journal became unreadable",
                            "latest-cleanup-diagnostic")),
                            "timeout retains the last observed state and logs after a later read denial")
            else:
                raise AssertionError("stalled journal reported success")
        assert_true(clock.now < 0.35, "state observations and read errors never extend the deadline")
        for value in (ValueError("corrupt journal"), ({"phase": "failed"}, state_path),
                      ({"phase": "interrupted"}, state_path)):
            with patch(__name__ + "._read_handoff_state", side_effect=value if isinstance(value, Exception) else None,
                       return_value=value if not isinstance(value, Exception) else None):
                try:
                    _poll_handoff(root, 1)
                except (ValueError, AssertionError):
                    pass
                else:
                    raise AssertionError("failed/corrupt journal accepted")


def _check_worker_protocol() -> None:
    if os.name != "nt":
        _check_posix_worker_protocol()
        return
    from exoma import _update_worker as worker

    api = worker.Win32()
    _check_restricted_worker_job()
    _check_windows_update_observer()
    own = api.process(os.getpid())
    handle = api.open_identity(os.getpid(), own)
    assert_true(not api.signaled(handle), "current generation can be waited without terminate rights")
    api.close(handle)
    assert_true(api.open_identity(os.getpid(), {**own, "created": own["created"] - 1}) is None,
                "reused generation is not waited or terminated")
    assert_true(worker.installation_key(sys.prefix, api.user()) == worker.installation_key(Path(sys.prefix), api.user()),
                "installation identity is independent of cwd/state overrides")
    class Ancestors:
        def parents(self):
            return {10: 20, 20: 30, 30: 40, 40: 0}
        def process(self, pid):
            return {"pid": pid, "created": 100 - pid,
                    "image": worker.canonical({10: "base/python.exe", 20: "tool/python.exe",
                                                30: "bin/exoma.exe", 40: "host/python.exe"}[pid])}
    with patch.object(worker.os, "getpid", return_value=10):
        chain = worker.callers(Ancestors(), "tool/python.exe", {worker.canonical("bin/exoma.exe")})
        assert_true([row["pid"] for row in chain] == [10, 20, 30], "launcher discovery stops before a Python host")
    with tempfile.TemporaryDirectory(prefix="exoma-worker-ü-") as tmp:
        root = Path(tmp).resolve()
        key = worker.installation_key(root / "target", api.user())
        job = worker.ChildJob(api, key)
        try:
            code = job.run([sys.executable, "-I", "-c", "raise SystemExit(7)"], root,
                           root / "exit.log", 10, lambda child: None)
            assert_true(code == 7 and job.active() == 0, "native child exit is preserved and descendants cleaned")
            try:
                job.run([sys.executable, "-I", "-c", "import time; time.sleep(30)"], root,
                        root / "timeout.log", 0.2, lambda child: None)
            except TimeoutError:
                assert_true(job.active() == 0, "timeout finishes job cleanup before returning")
            else:
                raise AssertionError("mutative child deadline ignored")
            blocked = root / "must-not-execute"
            with patch.object(api.k, "UpdateProcThreadAttribute", return_value=0):
                try:
                    job.run([sys.executable, "-I", "-c", "from pathlib import Path; Path(" + repr(str(blocked)) + ").touch()"],
                            root, root / "assignment.log", 10, lambda child: None)
                except OSError:
                    pass
                else:
                    raise AssertionError("uncontained suspended child was allowed to resume")
            assert_true(not blocked.exists(), "failed containment terminates the child before execution")
        finally:
            job.close()
        loader = ("import importlib.util; s=importlib.util.spec_from_file_location('worker', "
                  + repr(str(Path(worker.__file__).resolve())) + "); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); ")
        lock = worker.InstallationLock(api, key)
        try:
            code = loader + "\ntry:\n m.InstallationLock(m.Win32(), " + repr(key) + ")\nexcept OSError:\n print('busy')\nelse:\n raise SystemExit(1)\n"
            competing = subprocess.run([sys._base_executable, "-I", "-S", "-B", "-c", code],
                                       env={**os.environ, "EXOMA_HOME": str(root / "other-store")},
                                       cwd=root, capture_output=True, text=True, timeout=10, **no_console())
            assert_true(competing.returncode == 0 and "busy" in competing.stdout,
                        "another workspace/storage override cannot acquire the same installation lock")
        finally:
            lock.close()
        # The job handle is not inherited: killing its owner must kill a running child.
        crash_key = key + "Crash"
        marker, ready = root / "crash-child.json", root / "child-running"
        child_code = ("from pathlib import Path; import time,subprocess,sys; "
                      "p=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)'],creationflags=0x08000000); "
                      "r=Path(" + repr(str(ready)) + "); t=r.with_suffix('.tmp'); t.write_text(str(p.pid)); t.replace(r); time.sleep(30)")
        controller_code = (loader + "a=m.Win32(); j=m.ChildJob(a," + repr(crash_key) + "); "
                           "j.run(" + repr([sys._base_executable, "-I", "-c", child_code]) + ","
                           + repr(str(root)) + "," + repr(str(root / "crash.log")) + ",60,"
                           "lambda identity: m.atomic_json(" + repr(str(marker)) + ",identity) if identity else None)")
        with (root / "controller.log").open("wb") as log:
            controller = subprocess.Popen([sys._base_executable, "-I", "-S", "-B", "-c", controller_code],
                                          stdout=log, stderr=log, **no_console())
        child_handle = grandchild_handle = None
        try:
            until = time.monotonic() + 10
            while not ready.exists() and controller.poll() is None and time.monotonic() < until:
                time.sleep(0.02)
            assert_true(ready.exists(), "contained child began executing: " + (root / "controller.log").read_text())
            identity = worker.read_json(marker)
            child_handle = api.open_identity(identity["pid"], identity)
            grandchild = api.process(int(ready.read_text()))
            grandchild_handle = api.open_identity(grandchild["pid"], grandchild)
            controller.kill()
            controller.wait(timeout=10)
            until = time.monotonic() + 10
            while any(not api.signaled(h) for h in (child_handle, grandchild_handle) if h) and time.monotonic() < until:
                time.sleep(0.02)
            assert_true(child_handle and grandchild_handle and all(api.signaled(h) for h in (child_handle, grandchild_handle)),
                        "worker crash kills its contained child and grandchild")
        finally:
            api.close(child_handle)
            api.close(grandchild_handle)
            if controller.poll() is None:
                controller.kill()
                controller.wait(timeout=10)
        # Independent worker processes use the same native installation mutex even
        # when journals live under different storage overrides.
        with (root / "caller.log").open("wb") as log:
            caller = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"],
                                      stdout=log, stderr=log, **no_console())
        try:
            identity = api.process(caller.pid)
            identity["parent"] = api.parents()[caller.pid]
            for commit, exit_caller in ((False, False), (True, False), (True, True)):
                operation = root / "updates" / key / uuid_hex()
                operation.mkdir(parents=True)
                script = operation / "worker.py"
                snapshot = Path(worker.__file__).read_text(encoding="utf-8").replace(
                    "HANDSHAKE_SECONDS = 15", "HANDSHAKE_SECONDS = 0.5").replace(
                    "CALLER_SECONDS = 60", "CALLER_SECONDS = 0.5")
                script.write_text(snapshot, encoding="utf-8")
                token = uuid_hex()
                request = {"schema_version": worker.SCHEMA, "operation_id": operation.name,
                           "installation_key": key, "token": token, "prefix": str(root / "target"),
                           "python_relative": "Scripts/python.exe", "package": [sys.executable, "-c", "raise SystemExit(7)"],
                           "cwd": str(root), "timeout": 5, "callers": [identity],
                           "worker_sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
                worker.atomic_json(operation / "request.json", request)
                flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                if api.in_job():
                    flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB
                with (operation / "worker.log").open("wb") as log:
                    try:
                        process = subprocess.Popen([sys._base_executable, "-I", "-S", "-B", str(script), str(operation)],
                                                   stdout=log, stderr=log, creationflags=flags)
                    except PermissionError as exc:
                        # The runtime's test supervisor intentionally disallows breakaway.
                        # Assert refusal and then exercise handshake under containment;
                        # this is NOT the independent-worker artifact acceptance gate.
                        assert_true(exc.winerror == 5 and api.in_job(), "only restrictive-job refusal is expected")
                        assert_true(not (operation / "state.json").exists(), "refused spawn never starts package step")
                        snapshot = snapshot.replace("if api.in_job():", "if False:  # protocol-only test inside supervisor")
                        script.write_text(snapshot, encoding="utf-8")
                        request["worker_sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
                        worker.atomic_json(operation / "request.json", request)
                        process = subprocess.Popen([sys._base_executable, "-I", "-S", "-B", str(script), str(operation)],
                                                   stdout=log, stderr=log, **no_console())
                try:
                    until = time.monotonic() + 10
                    while not (operation / "ready.json").exists() and process.poll() is None and time.monotonic() < until:
                        time.sleep(0.01)
                    if not (operation / "ready.json").exists() and process.poll() is not None:
                        refused = worker.read_json(operation / "state.json")
                        if "worker still belongs to a host job" in refused.get("error", ""):
                            assert_true(not refused["stages"] and not (operation / "commit.json").exists(),
                                        "successful spawn with residual membership refuses before mutation")
                            snapshot = snapshot.replace("if api.in_job():", "if False:  # protocol-only contained fixture")
                            script.write_text(snapshot, encoding="utf-8")
                            request["worker_sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
                            worker.atomic_json(operation / "request.json", request)
                            with (operation / "worker.log").open("ab") as log:
                                process = subprocess.Popen([sys._base_executable, "-I", "-S", "-B", str(script), str(operation)],
                                                           stdout=log, stderr=log, **no_console())
                            until = time.monotonic() + 10
                            while not (operation / "ready.json").exists() and process.poll() is None and time.monotonic() < until:
                                time.sleep(0.01)
                    assert_true((operation / "ready.json").exists(), (operation / "worker.log").read_text())
                    if commit:
                        worker.atomic_json(operation / "commit.json", {"token": token})
                    if exit_caller:
                        caller.terminate()
                        caller.wait(timeout=10)
                    assert_true(process.wait(timeout=10) == 1, "uncommitted/still-running caller prevents mutation")
                    state = worker.read_json(operation / "state.json")
                    if exit_caller:
                        assert_true(state["phase"] == "failed" and state["stages"]["package"]["exit_code"] == 7
                                    and "install" not in state["stages"], "package failure stops before refresh")
                        assert_true(state["callers_exited_at"] <= state["stages"]["package"]["started_at"],
                                    "native process handles signaled before package started")
                    else:
                        assert_true(state["phase"] == "failed" and "package" not in state["stages"],
                                    "missing COMMIT or live caller never starts package manager")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
        finally:
            caller.terminate()
            caller.wait(timeout=10)


def _check_windows_update_observer():
    """Production observer + real OS Ctrl+C, with native contained worker cleanup."""
    if os.name != "nt":
        return
    import textwrap
    from exoma import _update_worker as worker
    from exoma.utils import osutil

    observer_entry = textwrap.dedent(r'''
        import ctypes, runpy, signal, sys
        from pathlib import Path
        source, operation = sys.argv[1:3]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_int]
        assert kernel.GetConsoleCP(), 'observer must share the private console'
        assert kernel.SetConsoleCtrlHandler(None, False), 'enable inherited Ctrl+C processing'
        signal.signal(signal.SIGINT, signal.default_int_handler)
        def ready(frame, event, arg):
            if (event == 'line' and frame.f_code.co_filename == source
                    and frame.f_code.co_name == 'observe' and frame.f_locals.get('code') == 3):
                Path(operation, 'observer-ready').touch()
                sys.settrace(None)
                return None
            return ready
        sys.argv = [source, '--observe', operation, '--wait']
        sys.settrace(ready)
        runpy.run_path(source, run_name='__main__')
    ''')
    controller = textwrap.dedent(r'''
        import ctypes, hashlib, json, os, subprocess, sys, time
        from ctypes import wintypes
        from pathlib import Path
        from exoma import _update_worker as worker
        from exoma.utils import osutil
        root = Path(sys.argv[1])
        api = worker.Win32()
        kernel = api.k
        kernel.GetConsoleCP.restype = wintypes.UINT
        kernel.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
        report = {'contained': True, 'independence_verified': False, 'production_observer': str(Path(worker.__file__).resolve())}
        caller = process = observer = None
        handles = []
        try:
            report['attached'] = bool(kernel.GetConsoleCP())
            assert report['attached'], 'controller must own a private console'
            assert kernel.SetConsoleCtrlHandler(None, True), 'controller ignores its own broadcast'
            operation = root / 'updates' / 'observer-operation'
            operation.mkdir(parents=True)
            source = Path(worker.__file__).read_text(encoding='utf-8')
            assert source.count('if api.in_job():') == 1
            # Only the worker admission guard is waived in this contained fixture.
            # The observer runs the original production file, never this copy.
            source = source.replace('if api.in_job():', 'if False:  # contained observer fixture; no independence proof')
            script = operation / 'worker.py'
            script.write_text(source, encoding='utf-8')
            python = sys._base_executable or sys.executable
            marker = operation / 'package-ready.json'
            stage = ('import json,os,pathlib,subprocess,sys,time\n'
                     'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
                     'pathlib.Path(' + repr(str(marker)) + ').write_text(json.dumps([os.getpid(),p.pid]))\n'
                     'time.sleep(60)\n')
            caller = subprocess.Popen([python, '-c', 'import time; time.sleep(60)'], **osutil.hidden_run_kwargs())
            identity = api.process(caller.pid)
            identity['parent'] = api.parents()[caller.pid]
            token = 'observer-fixture'
            worker.atomic_json(operation / 'request.json', {
                'schema_version': worker.SCHEMA, 'operation_id': operation.name,
                'installation_key': worker.installation_key(root / 'target', api.user()),
                'token': token, 'prefix': str(root / 'target'), 'python_relative': 'Scripts/python.exe',
                'package': [python, '-c', stage], 'cwd': str(root), 'timeout': 20,
                'callers': [identity], 'worker_sha256': hashlib.sha256(script.read_bytes()).hexdigest()})
            with (root / 'worker.log').open('wb') as log:
                process = subprocess.Popen([python, '-I', '-S', '-B', str(script), str(operation)],
                                           stdout=log, stderr=log, **osutil.hidden_run_kwargs())
            deadline = time.monotonic() + 8
            while not (operation / 'ready.json').exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert (operation / 'ready.json').exists(), (root / 'worker.log').read_text(errors='replace')
            worker.atomic_json(operation / 'commit.json', {'token': token})
            caller.terminate()
            caller.wait(timeout=5)
            deadline = time.monotonic() + 8
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert marker.exists(), worker.read_json(operation / 'state.json')
            identities = [api.process(pid) for pid in json.loads(marker.read_text())]
            handles = [api.open_identity(row['pid'], row) for row in identities]
            assert all(handles), 'pin each real stage/descendant generation'
            observer = subprocess.Popen([python, '-I', '-S', '-B', str(root / 'observer.py'),
                                         str(Path(worker.__file__).resolve()), str(operation)],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding='utf-8', creationflags=0)
            # creationflags=0 deliberately inherits this already-hidden console.
            deadline = time.monotonic() + 8
            while not (operation / 'observer-ready').exists() and observer.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert (operation / 'observer-ready').exists(), 'production observer did not enter wait'
            report['signal_sent'] = bool(kernel.GenerateConsoleCtrlEvent(0, 0))
            assert report['signal_sent'], ctypes.get_last_error()
            stdout, stderr = observer.communicate(timeout=8)
            detached = json.loads(stdout)
            report['detach_status'] = observer.returncode
            report['observer_detached'] = detached.get('observer_detached') is True
            report['update_survived_detach'] = (process.poll() is None and not (operation / 'cancel.json').exists()
                                                and all(not api.signaled(handle) for handle in handles))
            assert report['detach_status'] == 3 and report['observer_detached'] and report['update_survived_detach'], stderr
            cancelled = subprocess.run([python, '-I', '-S', '-B', str(Path(worker.__file__).resolve()),
                                        '--observe', str(operation), '--wait', '--cancel'],
                                       capture_output=True, text=True, encoding='utf-8', timeout=10,
                                       **osutil.hidden_run_kwargs())
            state = json.loads(cancelled.stdout)
            report['cancel_status'] = cancelled.returncode
            report['worker_status'] = process.wait(timeout=8)
            report['descendants_stopped'] = all(api.signaled(handle) for handle in handles)
            report['cleanup_confirmed'] = state.get('cleanup_confirmed') is True
            report['terminal_interrupted'] = state.get('phase') == 'interrupted' and state.get('completed') is True
            report['explicit_source'] = (state.get('cancellation') or {}).get('source') == 'explicit_observer'
            report['later_stages_skipped'] = 'install' not in state['stages'] and 'probe' not in state['stages']
            assert report['cancel_status'] == report['worker_status'] == 130, cancelled.stderr
            assert all(report[key] for key in ('descendants_stopped', 'cleanup_confirmed', 'terminal_interrupted',
                                               'explicit_source', 'later_stages_skipped')), state
        except BaseException as exc:
            report['error'] = repr(exc)
        finally:
            for child in (observer, process, caller):
                if child and child.poll() is None:
                    osutil.terminate_tree(child)
            for handle in handles:
                api.close(handle)
            (root / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        raise SystemExit(1 if 'error' in report else 0)
    ''')
    with tempfile.TemporaryDirectory(prefix="windows-update-observer-") as tmp:
        root = Path(tmp).resolve()
        (root / "observer.py").write_text(observer_entry, encoding="utf-8")
        (root / "controller.py").write_text(controller, encoding="utf-8")
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        env = {**os.environ, "PYTHONPATH": str(REPO / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}
        with (root / "controller.log").open("wb") as log:
            proc = subprocess.Popen([sys._base_executable or sys.executable, str(root / "controller.py"), str(root)],
                                    env=env, stdout=log, stderr=log,
                                    creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
        try:
            proc.wait(timeout=45)
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            evidence = os.environ.get("WORKFLOW_WINDOWS_OBSERVER_REPORT")
            if evidence:
                Path(evidence).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            assert_true(proc.returncode == 0 and not report.get("error"),
                        "production Windows observer Ctrl+C/detach/cancel: " + repr(report))
        finally:
            if proc.poll() is None:
                osutil.terminate_tree(proc)


def uuid_hex():
    import uuid
    return uuid.uuid4().hex


def _check_cleanup_publication():
    from exoma import _update_worker as worker
    with tempfile.TemporaryDirectory(prefix="update-cleanup-") as tmp:
        root = Path(tmp)
        operation = root / uuid_hex()
        operation.mkdir()
        reservation = root / "reservation.json"
        worker.atomic_json(reservation, {"token": "fixture"})
        worker.atomic_json(operation / "request.json", {"schema_version": worker.SCHEMA,
            "operation_id": operation.name, "installation_key": "fixture", "token": "fixture",
            "reservation": str(reservation)})
        fake = MagicMock()
        fake.process.return_value = {"pid": 123, "created": 1, "image": "fixture"}
        fake.in_job.return_value = True
        fake.job_snapshot.return_value = {"in_job": True}
        unlink = Path.unlink
        def denied(path, *args, **kwargs):
            if path == reservation:
                raise PermissionError("fixture reservation is not writable")
            return unlink(path, *args, **kwargs)
        with patch.object(worker, "platform_api", return_value=fake), patch.object(Path, "unlink", denied):
            assert_true(worker.run_request(operation) == 4, "admission release failure cannot publish terminal completion")
        state, code = worker.outcome(operation, "fixture")
        assert_true(code == 4 and state["phase"] == "cleanup_pending" and not state["completed"]
                    and reservation.exists() and not state["stages"], "unconfirmed cleanup remains unknown and reserved")
    # Exercise conservative ancestry classification without pretending to run a
    # Darwin kernel on this host. A new orphan is uncertain; a foreign live parent
    # or unchanged generation is evidence that a process is outside our stage.
    owner = worker.DescendantOwner.__new__(worker.DescendantOwner)
    owner.subreaper = False
    parent = os.getpid() + 1000000
    owner.baseline = {parent: {"created": 1, "parent": 1}, 1: {"created": 1, "parent": 0}}
    current = {**owner.baseline, parent + 1: {"created": 2, "parent": parent}}
    with patch.object(owner, "inventory", return_value=current):
        owner.cleanup()
    inventory = MagicMock()
    inventory.pid, inventory.returncode = 999999, 0
    inventory.communicate.return_value = ("123456 1 Fri Nov 13 00:00:00 2026\n999999 99 Fri Nov 13 00:00:01 2026\n", "")
    inventory.poll.return_value = 0
    with patch.object(worker.subprocess, "Popen", return_value=inventory), \
         patch.object(worker.Posix, "process", side_effect=ProcessLookupError):
        observed = owner.inventory()
    assert_true(123456 in observed and 999999 not in observed,
                "a disappearing new parent retains ancestry uncertainty; only the reaped inventory child is excluded")
    assert_true(observed.query_status[123456]["generation_source"] == "ps_fallback" and
                observed.query_status[123456]["query_error"]["type"] == "ProcessLookupError",
                "the same inventory observation retains why a native generation was unavailable")
    with patch.object(worker.sys, "platform", "darwin"), patch.object(worker.subprocess, "check_output",
            side_effect=["{ sec = 123, usec = 456 } date in timezone A", "{ sec = 123, usec = 456 } date in timezone B"]):
        assert_true(worker.boot_identity() == worker.boot_identity(),
                    "timezone presentation cannot authorize recovery as a changed boot")
    for current in (
            {**owner.baseline, parent + 1: {"created": 2, "parent": 1}},
            {1: owner.baseline[1], parent: {"created": 3, "parent": 1},
             parent + 1: {"created": 2, "parent": parent}}):
        with patch.object(owner, "inventory", return_value=current):
            try:
                owner.cleanup()
            except OSError:
                pass
            else:
                raise AssertionError("orphan or reused-parent generation was declared safely unrelated")


def _check_guardian_diagnostics():
    from exoma import _update_worker as worker
    from types import SimpleNamespace

    # The same guardian path distinguishes observed child exits from synthetic
    # failure, and retains both cleanup errors instead of just the last one.
    with tempfile.TemporaryDirectory(prefix="guardian-diagnostics-") as tmp:
        root = Path(tmp)
        for observed, fail_stage, fail_wait, fail_owner, projection_failure in (
                (0, False, False, False, False), (7, False, False, False, False),
                (-9, False, False, False, False), (0, True, False, True, False),
                (0, True, True, True, False), (0, False, True, False, False),
                (0, True, False, True, True)):
            child, monitor, owner = MagicMock(), MagicMock(), MagicMock()
            child.pid, child.stdin = 6044, io.BytesIO()
            child.wait.return_value = observed
            if fail_wait:
                child.wait.side_effect = subprocess.TimeoutExpired(["secret-argv"], 5)
            monitor.exited.return_value = True
            owner.diagnostics = None
            if fail_owner:
                owner.cleanup.side_effect = OSError(5, "secret-environment")
            selector = MagicMock()
            selector.__enter__.return_value = selector
            selector.select.return_value = [] if fail_stage else [(1, 1)]
            receipt = root / "receipt.json"
            request = {"argv": ["secret-argv"], "cwd": str(root), "timeout": 5,
                       "log": str(root / "stage.log"), "receipt": str(receipt)}
            stdin = io.StringIO(json.dumps(request) + "\ngo\n")
            output = io.StringIO()
            builder = worker._guardian_diagnostics
            with patch.object(worker, "DescendantOwner", return_value=owner), \
                 patch.object(worker, "ChildExit", return_value=monitor), \
                 patch.object(worker.subprocess, "Popen", return_value=child), \
                 patch.object(worker.Posix, "process", return_value={"pid": 6044, "created": 123, "image": "secret-image"}), \
                 patch.object(worker.selectors, "DefaultSelector", return_value=selector), \
                 patch.object(worker.signal, "signal"), \
                 patch.object(worker.signal, "SIGCHLD", 17, create=True), \
                 patch.object(worker.signal, "SIGKILL", 9, create=True), \
                 patch.object(worker.os, "killpg", create=True), \
                 patch.object(worker.sys, "stdin", stdin), redirect_stdout(output), \
                 patch.object(worker, "_guardian_diagnostics", side_effect=RuntimeError("secret-token")
                              if projection_failure else builder):
                result = worker.guardian()
            saved = json.loads(receipt.read_text())
            assert_true(saved == json.loads(output.getvalue().splitlines()[-1]), "durable and pipe receipts agree")
            assert_true(saved["cleanup_confirmed"] is not (fail_wait or fail_owner) and
                        saved["exit_code"] == (1 if fail_stage or fail_wait else observed) and
                        result == (4 if fail_wait or fail_owner else observed), "legacy guardian outcomes retain their authority")
            diagnostic = saved["diagnostics"]
            encoded = json.dumps(diagnostic)
            assert_true(all(secret not in encoded for secret in
                            ("secret-argv", "secret-environment", "secret-image", "secret-token")),
                        "new diagnostics never copy exception payloads, argv, images or tokens")
            if projection_failure:
                assert_true(diagnostic["unavailable"] and saved["error"] == "[Errno 5] secret-environment",
                            "diagnostic failure cannot replace the original legacy error authority")
                continue
            lifecycle = diagnostic["child"]
            assert_true(lifecycle["child_returncode"] == (None if fail_wait else observed) and
                        lifecycle["reap_outcome"] == ("failed" if fail_wait else "reaped") and
                        lifecycle["synthetic_exit"] is (fail_stage or fail_wait) and
                        lifecycle["release_requested"] is not fail_stage,
                        "observed return/reap and release are independent of a synthetic guardian exit")
            assert_true(lifecycle["exit_observed"] and lifecycle["signals_attempted"] == ["SIGKILL"],
                        "receipt records existing observations/signals without adding lifecycle actions")
            assert_true(bool(diagnostic["stage_error"]) is fail_stage and
                        [error["site"] for error in diagnostic["cleanup_errors"]] ==
                        (["child_reap"] if fail_wait else []) + (["owner_cleanup"] if fail_owner else []),
                        "stage and ordered multiple cleanup errors remain independently recoverable")

        # A child may never be started; its exit code must remain unobserved.
        output = io.StringIO()
        with patch.object(worker, "DescendantOwner", side_effect=OSError("secret-owner-init")), \
             patch.object(worker.signal, "signal"), patch.object(worker.signal, "SIGCHLD", 17, create=True), \
             patch.object(worker.sys, "stdin", io.StringIO(json.dumps(request) + "\n")), redirect_stdout(output):
            assert_true(worker.guardian() == 1, "guardian setup failure retains synthetic failure")
        lifecycle = json.loads(output.getvalue())["diagnostics"]["child"]
        assert_true(lifecycle["child_returncode"] is None and lifecycle["reap_outcome"] == "no_child"
                    and lifecycle["synthetic_exit"], "absent child cannot acquire a fabricated exit code")

    # Negative/enriched receipts do not become successful results; old receipts
    # need no diagnostics to keep their original acceptance/rejection decisions.
    for positive in (False, True):
        legacy = {"cleanup_confirmed": positive, "exit_code": 0, "error": None}
        for receipt in (legacy, {**legacy, "diagnostics": {"schema_version": 1}}, {}, None):
            proc = MagicMock()
            proc.stdin = io.StringIO()
            proc.stdout = io.StringIO(json.dumps(receipt) + "\n")
            job = worker.PosixChildJob(None, "fixture", None)
            job.proc = proc
            accepted = isinstance(receipt, dict) and receipt.get("cleanup_confirmed") is True
            try:
                job.cleanup()
            except OSError:
                assert_true(not accepted and job.cleanup_unconfirmed and job.result is None,
                            "negative or malformed receipt cannot populate the successful result")
            else:
                assert_true(accepted and job.result == receipt, "legacy/enriched positive receipts remain compatible")

    # Use the classifier's original observations, including changed generations,
    # and bound both selection and its serialized envelope under hostile sizes.
    baseline, current = worker._ProcessInventory(), worker._ProcessInventory()
    for pid in range(100, 200):
        baseline[pid] = {"created": pid, "parent": pid - 1}
        current[pid] = {"created": "\U0001f600" * 1000, "parent": pid - 1}
        current.query_status[pid] = {"generation_source": "ps_fallback",
                                     "query_error": worker._diagnostic_error("inventory", PermissionError(13, "secret-token"))}
    ancestry = worker._ancestry_diagnostics(baseline, current, list(range(110, 200)))
    assert_true(len(ancestry["rejected_pids"]) == 8 and len(ancestry["records"]) <= 32 and
                ancestry["omitted_pids"] > 0 and ancestry["depth_truncated"] and ancestry["strings_truncated"],
                "ancestry selection bounds and omissions are explicit")
    assert_true(ancestry["records"][0]["snapshot"] == "current" and
                ancestry["records"][1]["snapshot"] == "baseline" and
                ancestry["records"][0]["created"] != ancestry["records"][1]["created"] and
                ancestry["records"][0]["query_error"]["errno"] == 13,
                "current failures and different baseline generations survive record selection")
    base = {"cleanup_confirmed": False, "exit_code": 1, "error": "\U0001f600" * 512}
    owner = SimpleNamespace(diagnostics=ancestry)
    receipt = worker._guardian_receipt(base, ("wait", OSError("secret-token")), [], {}, owner)
    raw = json.dumps(receipt, ensure_ascii=True, indent=2, allow_nan=False) + "\n"
    assert_true(len(raw.encode()) <= worker.GUARDIAN_RECEIPT_BYTES and len(raw) < 8192 and
                all(receipt[k] == v for k, v in base.items()) and receipt["diagnostics"]["truncated"],
                "full Unicode receipt stays readable without truncating legacy authority or JSON")

    # Failing the diagnostic projection must not mask the classifier's original
    # orphan rejection, or make an uncertain generation look unrelated.
    owner = worker.DescendantOwner.__new__(worker.DescendantOwner)
    owner.subreaper, owner.baseline = False, {}
    with patch.object(owner, "inventory", return_value={6044: {"created": 123, "parent": 1}}), \
         patch.object(worker, "_ancestry_diagnostics", side_effect=RuntimeError("secret-token")):
        try:
            owner.cleanup()
        except OSError as exc:
            assert_true("descendant ancestry cannot be confirmed" in str(exc) and owner.diagnostics["unavailable"],
                        "projection failure preserves original ancestry rejection")
        else:
            raise AssertionError("projection failure changed classifier acceptance")


def _check_guardian_cleanup_attempts():
    from exoma import _update_worker as worker
    import signal

    cases = (
        {"denied": {"SIGKILL"}, "exit": 0},
        {"denied": {"SIGKILL"}, "exit": 7},
        # Darwin: EPERM on a group holding only the exited, unreaped leader is
        # forgiven solely after observed exit + reap + signal-0 ESRCH.
        {"denied": {"SIGKILL"}, "exit": 0, "probe": "absent"},
        {"denied": {"SIGKILL"}, "exit": 7, "probe": "absent"},
        {"denied": {"SIGKILL"}, "exit": 0, "probe": "denied"},
        {"denied": {"SIGKILL"}, "exit": 0, "probe": "error"},
        {"denied": {"SIGKILL"}, "wait_error": "timeout", "probe": "absent"},
        {"denied": {"SIGKILL"}, "observation_error": True, "probe": "absent"},
        {"denied": {"SIGKILL"}, "owner_error": True, "probe": "absent"},
        {"denied": {"SIGTERM", "SIGKILL"}, "term": True, "probe": "absent"},
        {"kill_error": True, "probe": "absent"},
        {"kill_eacces": True, "probe": "absent"},
        {"denied": {"SIGKILL"}, "wait_error": "timeout"},
        {"denied": {"SIGKILL"}, "wait_error": "oserror", "owner_error": True},
        {"denied": {"SIGTERM"}, "term": True},
        {"denied": {"SIGTERM", "SIGKILL"}, "term": True, "owner_error": True},
        {"owner_error": True}, {"observation_error": True}, {"close_error": True}, {"gone": True},
    )
    with tempfile.TemporaryDirectory(prefix="guardian-reap-") as tmp:
        root = Path(tmp)
        for case in cases:
            events, child, monitor, owner = [], MagicMock(), MagicMock(), MagicMock()
            child.pid, child.stdin = 6044, io.BytesIO()
            owner.diagnostics = {"rejected_pids": [6045]} if case.get("owner_error") else None
            term = case.get("term") or case.get("observation_error")
            monitor.exited.return_value = True
            if case.get("observation_error"):
                monitor.exited.side_effect = OSError(5, "fixture observation failure")
            elif term:
                monitor.exited.side_effect = [False, True]
            def killpg(pid, sig):
                if sig == 0:
                    events.append("PROBE")
                    kind = case.get("probe", "present")
                    if kind == "absent":
                        raise ProcessLookupError(3, "fixture group absent")
                    if kind == "denied":
                        raise PermissionError(1, "fixture probe denied")
                    if kind == "error":
                        raise OSError(5, "fixture probe failed")
                    return
                name = "SIGTERM" if sig == signal.SIGTERM else "SIGKILL"
                events.append(name)
                if case.get("gone"):
                    raise ProcessLookupError(3, "fixture group absent")
                if name == "SIGKILL" and case.get("kill_error"):
                    raise OSError(5, "fixture kill failed")
                if name == "SIGKILL" and case.get("kill_eacces"):
                    raise PermissionError(13, "fixture kill access denied")
                if name in case.get("denied", set()):
                    raise PermissionError(1, "fixture signal denied")
            def reap(timeout):
                events.append("wait")
                assert_true(timeout == 5, "reap retains its original bounded deadline")
                if case.get("wait_error") == "timeout":
                    raise subprocess.TimeoutExpired(["fixture-stage"], timeout)
                if case.get("wait_error"):
                    raise OSError(5, "fixture wait failed")
                return case.get("exit", 0)
            def clean_owner():
                events.append("owner")
                if case.get("owner_error"):
                    raise OSError("fixture surviving descendant")
            def close_monitor():
                events.append("close")
                if case.get("close_error"):
                    raise OSError(5, "fixture monitor close failed")
            child.wait.side_effect, owner.cleanup.side_effect = reap, clean_owner
            monitor.close.side_effect = close_monitor
            selector = MagicMock()
            selector.__enter__.return_value = selector
            selector.select.return_value = [] if term else [(1, 1)]
            request = {"argv": ["fixture-stage"], "cwd": str(root), "timeout": 5, "log": str(root / "stage.log")}
            output = io.StringIO()
            with patch.object(worker, "DescendantOwner", return_value=owner), \
                 patch.object(worker, "ChildExit", return_value=monitor), \
                 patch.object(worker.subprocess, "Popen", return_value=child), \
                 patch.object(worker.Posix, "process", return_value={"pid": 6044, "created": 123, "image": "fixture"}), \
                 patch.object(worker.selectors, "DefaultSelector", return_value=selector), \
                 patch.object(worker.signal, "signal"), patch.object(worker.signal, "SIGCHLD", 17, create=True), \
                 patch.object(worker.signal, "SIGKILL", 9, create=True), \
                 patch.object(worker.os, "killpg", side_effect=killpg, create=True), \
                 patch.object(worker.sys, "stdin", io.StringIO(json.dumps(request) + "\ngo\n")), redirect_stdout(output):
                code = worker.guardian()
            receipt = json.loads(output.getvalue().splitlines()[-1])
            expected_errors = []
            if case.get("observation_error"):
                expected_errors.append("exit_observation")
            if "SIGTERM" in case.get("denied", set()):
                expected_errors.append("signal_term")
            if case.get("observation_error"):
                expected_errors.append("exit_observation")
            probe_expected = ("SIGKILL" in case.get("denied", set()) and not case.get("wait_error") and
                              not case.get("observation_error"))
            if case.get("kill_error") or case.get("kill_eacces") or ("SIGKILL" in case.get("denied", set()) and not (
                    probe_expected and case.get("probe") == "absent")):
                expected_errors.append("signal_kill")
            if case.get("wait_error"):
                expected_errors.append("child_reap")
            if case.get("owner_error"):
                expected_errors.append("owner_cleanup")
            if case.get("close_error"):
                expected_errors.append("monitor_close")
            diagnostic = receipt["diagnostics"]
            lifecycle = diagnostic["child"]
            signals = ["SIGTERM", "SIGKILL"] if term else ["SIGKILL"]
            assert_true(events == signals + ["wait"] + (["PROBE"] if probe_expected else []) + ["owner", "close"],
                        f"signal/observation failures cannot skip the final signal, reap or owner cleanup: {events}")
            assert_true([e["site"] for e in diagnostic["cleanup_errors"]] == expected_errors,
                        "ordered errors retain each distinct failure boundary")
            confirmed = not expected_errors
            assert_true(receipt["cleanup_confirmed"] is confirmed and code == (case.get("exit", 0) if confirmed else 4),
                        "reaped child exit zero cannot erase EPERM or surviving-descendant uncertainty")
            assert_true(lifecycle["reap_outcome"] == ("failed" if case.get("wait_error") else "reaped") and
                        lifecycle["child_returncode"] == (None if case.get("wait_error") else case.get("exit", 0)),
                        "actual child exit remains observable even when cleanup is negative")
            assert_true(bool(diagnostic["stage_error"]) is bool(term), "cleanup failures preserve the original stage error")
            for error in diagnostic["cleanup_errors"]:
                if error["site"] in {"signal_term", "signal_kill"} and not (
                        case.get("kill_error") or case.get("kill_eacces")):
                    assert_true(error["type"] == "PermissionError" and error["errno"] == 1,
                                "EPERM remains an explicit failed signal, never missing-group evidence")


def _check_restricted_worker_job():
    """A disposable job proves refusal independently of the test host's ambient jobs."""
    from exoma import _update_worker as worker
    api = worker.Win32()
    with tempfile.TemporaryDirectory(prefix="restricted-update-") as tmp:
        root = Path(tmp)
        operation = root / uuid_hex()
        operation.mkdir()
        script = operation / "worker.py"
        script.write_bytes(Path(worker.__file__).read_bytes())
        key = worker.installation_key(root / "target", api.user())
        worker.atomic_json(operation / "request.json", {
            "schema_version": worker.SCHEMA, "operation_id": operation.name,
            "installation_key": key, "token": uuid_hex(), "prefix": str(root / "target"),
            "python_relative": "Scripts/python.exe", "package": [sys.executable, "-c", "raise SystemExit('must not run')"],
            "cwd": str(root), "timeout": 5, "callers": [],
            "worker_sha256": hashlib.sha256(script.read_bytes()).hexdigest()})
        job = worker.ChildJob(api, key)
        try:
            code = job.run([sys.executable, "-I", "-S", "-B", str(script), str(operation)],
                           root, root / "restricted.log", 10, lambda child: None)
        finally:
            job.cleanup()
            job.close()
        state = worker.read_json(operation / "state.json")
        assert_true(code == 1 and state["host_job"]["in_job"] and state["host_job"]["kill_on_close"]
                    and not state["stages"] and not (operation / "ready.json").exists(),
                    "controlled restrictive job refuses before READY and every mutative instruction")


def _handoff_independence_verified(platform, contained, state):
    return (platform in {"win32", "linux", "darwin"} and not contained and isinstance(state, dict)
            and state.get("phase") == "succeeded" and state.get("cleanup_confirmed") is True
            and state.get("callers_exited_at", float("inf")) <= state.get("stages", {}).get("package", {}).get("started_at", -1)
            and (platform != "win32" or state.get("host_job", {}).get("in_job") is False))


@contextmanager
def _artifact_evidence(report_path):
    """Keep completed transition evidence even if the later no-op or cleanup fails."""
    from exoma import _update_worker as worker

    import platform as host
    evidence = {"kind": "exoma-update-handoff", "schema_version": 1,
                "mode": "contained" if os.environ.get("EXOMA_UPDATE_CONTAINED_SMOKE") == "1" else "strict",
                "passed": False, "platform": sys.platform, "machine": host.machine(),
                "python_version": host.python_version(),
                "actual_version_transition": False, "independent_worker_verified": False, "no_op_verified": False}
    failed = False
    try:
        yield evidence
    except BaseException as exc:
        failed = True
        evidence.update(passed=False, error=str(exc))
        raise
    finally:
        if report_path:
            try:
                worker.atomic_json(report_path, evidence)
            except OSError as exc:
                if not failed:
                    raise
                print(f"Artifact report unavailable: {exc}", file=sys.stderr)


def _checkout_identity():
    """Commit and cleanliness of the tested checkout, bound into the report before any fixture work."""
    try:
        run = lambda *argv: subprocess.run(["git", *argv], cwd=REPO, capture_output=True, text=True,
                                           check=True, timeout=30, **no_console()).stdout.strip()
        return run("rev-parse", "HEAD"), bool(run("status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        return None, None


def _mark_handoff_passed(report, contained):
    assert_true(contained or report.get("independent_worker_verified") is True,
                "handoff acceptance requires verified worker independence")
    report.update(passed=True, no_op_verified=True)


def _check_artifact_reporting():
    proof = {"phase": "succeeded", "cleanup_confirmed": True, "callers_exited_at": 1,
             "stages": {"package": {"started_at": 2}}, "host_job": {"in_job": False}}
    for platform in ("win32", "linux", "darwin"):
        assert_true(_handoff_independence_verified(platform, False, proof)
                    and not _handoff_independence_verified(platform, True, proof)
                    and not _handoff_independence_verified(platform, False, {"phase": "succeeded"}),
                    "all platforms require lifecycle evidence; contained/missing proof never attests independence")
    with tempfile.TemporaryDirectory(prefix="exoma-artifact-evidence-") as tmp:
        report_path = Path(tmp) / "report.json"
        with _artifact_evidence(report_path) as report:
            assert_true(report["kind"] == "exoma-update-handoff" and report["schema_version"] == 1
                        and report["mode"] in {"strict", "contained"} and report["python_version"]
                        and report["platform"] == sys.platform and report["machine"],
                        "report names its kind, schema, mode, Python and platform before any fixture work")
        assert_true(json.loads(report_path.read_text(encoding="utf-8"))["passed"] is False,
                    "a report that never reached acceptance stays unpassed")
        try:
            with _artifact_evidence(report_path) as report:
                report.update(actual_version_transition=True, independent_worker_verified=True,
                              state={"phase": "succeeded", "version_after": "fixture-B"})
                raise AssertionError("subsequent no-op failed")
        except AssertionError:
            pass
        evidence = json.loads(report_path.read_text(encoding="utf-8"))
        assert_true(not evidence["passed"] and not evidence["no_op_verified"]
                    and evidence["actual_version_transition"] and evidence["independent_worker_verified"]
                    and evidence["state"]["version_after"] == "fixture-B"
                    and evidence["error"] == "subsequent no-op failed", "later failure retains first-transition evidence")
        for contained, independent in ((False, False), (False, True), (True, False)):
            try:
                with _artifact_evidence(report_path) as report:
                    report.update(actual_version_transition=True, independent_worker_verified=independent)
                    _mark_handoff_passed(report, contained)
            except AssertionError:
                assert_true(not contained and not independent,
                            "only unverified handoff independence refuses a positive gate")
            evidence = json.loads(report_path.read_text(encoding="utf-8"))
            expected = contained or independent
            assert_true(evidence["passed"] is expected and evidence["no_op_verified"] is expected
                        and evidence["independent_worker_verified"] is independent,
                        "artifact acceptance requires independence while contained results remain explicitly scoped")


def _check_handoff_wheel_upgrade() -> None:
    """A/B are built from disposable source copies containing the candidate updater.

    No historical-updater recovery or no-op is allowed to satisfy this gate.
    The polling harness never runs the executable being replaced.
    """
    from exoma.config.settings import TOOL_VERSION

    contained = os.environ.get("EXOMA_UPDATE_CONTAINED_SMOKE") == "1"
    uv = shutil.which("uv")
    assert_true(uv is not None, "uv is required for the A-to-B artifact gate")
    with _artifact_evidence(os.environ.get("EXOMA_UPDATE_SMOKE_REPORT")) as report, \
         _preserved_handoff_temp(report) as tmp:
        report["commit"], report["commit_dirty"] = _checkout_identity()
        report["manager"] = os.environ.get("EXOMA_UPDATE_SMOKE_MANAGER", "uv")
        root = Path(tmp).resolve()
        stage, wheels, home, project = root / "built", root / "index", root / "home", root / "project"
        for folder in (stage, wheels, home, project):
            folder.mkdir()
        versions = [TOOL_VERSION + ".post1", TOOL_VERSION + ".post2"]
        wheel_paths = []
        for index, version in enumerate(versions):
            source = root / ("source-" + str(index))
            source.mkdir()
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copyfile(REPO / name, source / name)
            shutil.copytree(REPO / "src/exoma", source / "src/exoma", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            if contained:
                # Only a disposable fixture relaxes this assertion. The helper stays
                # inside the test supervisor, which can still kill its entire tree.
                # This exercises real package/refresh stages, NOT host independence.
                worker_path = source / "src/exoma/_update_worker.py"
                worker_path.write_text(worker_path.read_text(encoding="utf-8").replace(
                    "if api.in_job():", "if False:  # contained artifact fixture; not release acceptance"), encoding="utf-8")
            config = source / "src/exoma/config/settings.py"
            config.write_text(config.read_text(encoding="utf-8").replace(
                f'TOOL_VERSION = "{TOOL_VERSION}"', f'TOOL_VERSION = "{version}"'), encoding="utf-8")
            pyproject = source / "pyproject.toml"
            pyproject.write_text(re.sub(r'^version = "[^"]+"', f'version = "{version}"',
                                       pyproject.read_text(encoding="utf-8"), count=1, flags=re.M), encoding="utf-8")
            manifest = source / "src/exoma/_bundle/manifest.json"
            bundle = json.loads(manifest.read_text(encoding="utf-8"))
            bundle.update(version=version, versions={name: version for name in bundle["versions"]})
            manifest.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
            (source / "src/exoma/_handoff_fixture.py").write_text(f"MARKER = {version!r}\n", encoding="utf-8")
            result = subprocess.run([uv, "build", "--out-dir", str(stage), str(source)],
                                    capture_output=True, text=True, timeout=180, **no_console())
            assert_true(result.returncode == 0, "fixture wheel build: " + result.stdout + result.stderr)
            wheel = stage / f"exoma-{version}-py3-none-any.whl"
            assert_true(wheel.is_file(), "versioned candidate fixture wheel exists")
            assert_true((stage / f"exoma-{version}.tar.gz").is_file(), "fixture wheel was built through an sdist")
            wheel_paths.append(wheel)
        report.update(baseline=versions[0], candidate=versions[1], manual_recovery=False,
                      wheel_sha256=[hashlib.sha256(path.read_bytes()).hexdigest() for path in wheel_paths])
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith(("EXOMA_", "WORKFLOW_", "AGENT_", "UV_", "PIP_", "XDG_",
                                              "AI_PROXY_", "PIPX_", "CLAUDE_", "GEMINI_", "OPENCODE_"))
               and key.upper() not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "CODEX_HOME")}
        for key, directory in {"HOME": home, "USERPROFILE": home, "AGENT_HOME": home,
                               "EXOMA_HOME": root / "state", "APPDATA": home / "AppData/Roaming",
                               "LOCALAPPDATA": home / "AppData/Local", "UV_TOOL_DIR": root / "tools",
                               "UV_TOOL_BIN_DIR": root / "bin", "UV_CACHE_DIR": root / "cache",
                               "UV_CONFIG_DIR": root / "uv-config"}.items():
            directory.mkdir(parents=True, exist_ok=True)
            env[key] = str(directory)
        env.update(UV_NO_CONFIG="1", UV_PYTHON_DOWNLOADS="never", UV_LINK_MODE="copy",
                   PATH=str(root / "bin") + os.pathsep + env.get("PATH", ""))
        manager = os.environ.get("EXOMA_UPDATE_SMOKE_MANAGER", "uv")
        assert_true(manager in {"uv", "pipx", "pip"}, "supported artifact manager")
        target = root / ("pipx/venvs/exoma" if manager == "pipx" else "tools/exoma")
        exe = (target / ("Scripts/exoma.exe" if os.name == "nt" else "bin/exoma")) if manager == "pip" else root / "bin" / ("exoma.exe" if os.name == "nt" else "exoma")
        env.update(PIP_NO_INDEX="1", PIP_FIND_LINKS=str(wheels), PIPX_HOME=str(root / "pipx"),
                   PIPX_BIN_DIR=str(root / "bin"), PIPX_DEFAULT_PYTHON=sys._base_executable)
        def run(argv, accepted=False):
            result = subprocess.run([str(item) for item in argv], cwd=project, env=env,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, **no_console())
            expected = 3 if accepted else 0
            assert_true(result.returncode == expected, result.stdout + result.stderr)
            return result.stdout
        shutil.copyfile(wheel_paths[0], wheels / wheel_paths[0].name)
        if manager == "uv":
            run([uv, "tool", "install", "exoma", "--python", sys._base_executable,
                 "--no-index", "--find-links", wheels])
        elif manager == "pipx":
            import shlex
            pipx = shutil.which("pipx")
            assert_true(pipx is not None, "pipx must be installed for its artifact gate")
            # Modern pipx may select uv, which does not consume pip's index env.
            # Persist explicit local-index args for BOTH install and later upgrade.
            run([pipx, "install", "exoma", "--python", sys._base_executable,
                 "--pip-args", "--no-index --find-links " + shlex.quote(str(wheels))])
        else:
            run([sys._base_executable, "-m", "venv", target])
            run([target / ("Scripts/python.exe" if os.name == "nt" else "bin/python"), "-m", "pip", "install", "exoma"])
        report["manager"] = manager
        initial_version = run([exe, "--version"])
        report["initial_version"] = initial_version.strip()
        assert_true(versions[0] in initial_version, "launcher starts fixture A: " + initial_version)
        run([exe, "install", "claude", "--provider", "opencode", "--no-input"])
        run([exe, "init", "--work-dir", project, "--host", "claude"])
        _seed_migration_user_data(home, project)
        shutil.copyfile(wheel_paths[1], wheels / wheel_paths[1].name)
        output = run([exe, "update", "--timeout", "120", "--manager", manager], accepted=True)
        assert_true("accepted; not complete" in output, "launcher reports acceptance without claiming completion")
        state, state_path = _poll_handoff(root / "state", 480)
        assert_true(state["version_before"] == versions[0] and state["version_after"] == versions[1],
                    "actual installed distribution changes A to B")
        assert_true(state["accepted_at"] <= state["callers_exited_at"] <= state["stages"]["package"]["started_at"]
                    < state["stages"]["install"]["started_at"] < state["stages"]["check"]["started_at"],
                    "caller exit precedes package, refresh and check")
        assert_true(all(state["stages"][step]["exit_code"] == 0 for step in ("package", "probe", "install", "check")),
                    "all upgrade stages succeeded without manual recovery")
        package_log = (state_path.parent / "package.log").read_text(encoding="utf-8", errors="replace")
        assert_true(versions[1] in package_log, "package step reports a real version transition")

        interpreter = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        marker = json.loads(run([interpreter, "-I", "-c", "import json,exoma._handoff_fixture as f; print(json.dumps(dict(marker=f.MARKER,module=f.__file__)))"]))
        assert_true(marker["marker"] == versions[1] and str(target) in marker["module"],
                    "B marker imports from upgraded target, not checkout")
        assert_true(versions[1] in run([exe, "--version"]), "console entrypoint starts B")
        _assert_migrated_user_data(run, home, project)
        run([exe, "install", "--check"])
        report.update(actual_version_transition=True,
                      independent_worker_verified=_handoff_independence_verified(sys.platform, contained, state),
                      state=state)
        print(f"TRANSITION EVIDENCE: {versions[0]} -> {versions[1]}; refresh/check and user data checked; "
              f"Independent worker={report['independent_worker_verified']}; full gate still awaits separate no-op", flush=True)
        # A no-op remains a separate case after the strict transition assertion.
        run([interpreter, "-m", "exoma", "update", "--timeout", "120", "--manager", manager], accepted=True)
        second, _ = _poll_handoff(root / "state", 300)
        assert_true(second["version_before"] == second["version_after"] == versions[1], "explicit no-op case")
        _mark_handoff_passed(report, contained)
        label = "CONTAINED WHEEL (not independence acceptance)" if contained else (
            "SHARED HANDOFF WHEEL")
        print(f"{label}: {versions[0]} -> {versions[1]}; no manual recovery; then separate no-op")


@contextmanager
def _preserved_handoff_temp(report):
    """Archive bounded operation evidence before temporary-directory cleanup on failures."""
    with tempfile.TemporaryDirectory(prefix="exoma-handoff-\u00fc-") as tmp:
        try:
            yield tmp
        finally:
            report_path = os.environ.get("EXOMA_UPDATE_SMOKE_REPORT")
            if report_path:
                destination = Path(report_path).with_suffix(".logs")
                for source in (Path(tmp) / "state" / "updates").rglob("*"):
                    if source.is_file() and source.suffix in {".json", ".log"}:
                        relative = source.relative_to(Path(tmp) / "state" / "updates")
                        target = destination / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with source.open("rb") as stream:
                            raw = stream.read(262144)
                        if source.suffix == ".json":
                            try:
                                value = json.loads(raw)
                                value.pop("token", None)
                                raw = json.dumps(value, indent=2).encode()
                            except ValueError:
                                raw = b"unreadable diagnostic metadata"
                        target.write_bytes(raw)
                report["operation_artifacts"] = str(destination)


def _ready_failure_context(operation, process, *, commit, exit_caller):
    """Bounded fixture evidence before its temporary operation is removed."""
    detail = {"operation": str(operation)[:512], "commit": commit, "exit_caller": exit_caller}
    try:
        detail["worker_returncode"] = process.poll()
    except (OSError, ValueError) as exc:
        detail["worker_poll_error"] = type(exc).__name__
    try:
        with (operation / "state.json").open(encoding="utf-8") as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("fixture state exceeds limit")
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise ValueError("fixture state is not an object")
        def selected(value):
            if isinstance(value, str):
                return value[:256]
            if value is None or type(value) in (int, float, bool):
                return value
            return "invalid_type"
        detail["state"] = {key: selected(state[key]) for key in (
            "phase", "error", "cleanup_error", "heartbeat", "completed", "cleanup_confirmed") if key in state}
        if isinstance(state.get("stages"), dict):
            detail["state"]["stages"] = {
                name: {key: selected(state["stages"][name][key]) for key in ("started_at", "exit_code")
                       if key in state["stages"][name]}
                for name in ("package", "probe", "install", "check") if isinstance(state["stages"].get(name), dict)}
    except (OSError, ValueError) as exc:
        detail["state_unavailable"] = type(exc).__name__
    try:
        with (operation / "worker.log").open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 2048))
            detail["worker_log_tail"] = stream.read(2048).decode("utf-8", errors="replace")
    except OSError as exc:
        detail["worker_log_unavailable"] = type(exc).__name__
    text = json.dumps(detail, ensure_ascii=True)
    if len(text) > 8000:
        detail["truncated"] = True
        detail["operation"] = detail["operation"][:256]
        detail["state"] = {"unavailable": "context_budget"}
        detail["worker_log_tail"] = detail.get("worker_log_tail", "")[-512:]
        text = json.dumps(detail, ensure_ascii=True)
    return "POSIX READY failure: " + text


def _check_ready_failure_context():
    with tempfile.TemporaryDirectory(prefix="ready-evidence-") as tmp:
        operation = Path(tmp)
        (operation / "state.json").write_text(json.dumps({
            "phase": "failed", "error": "fixture caller identity changed", "completed": True,
            "token": "secret-request-token", "request": {"argv": ["secret-request-argv"]},
            "stages": {"package": {"exit_code": None, "token": "secret-stage-token"}},
        }), encoding="utf-8")
        (operation / "worker.log").write_text("fixture stderr", encoding="utf-8")
        process = MagicMock()
        process.poll.return_value = 1
        text = _ready_failure_context(operation, process, commit=True, exit_caller=False)
        detail = json.loads(text.split(": ", 1)[1])
        assert_true(detail["commit"] and not detail["exit_caller"] and detail["worker_returncode"] == 1
                    and detail["state"]["phase"] == "failed" and "caller identity" in detail["state"]["error"]
                    and all(secret not in text for secret in ("secret-request-token", "secret-request-argv", "secret-stage-token")),
                    "READY evidence keeps operation/iteration/state/exit context without copying requests or tokens")
        (operation / "worker.log").write_bytes(b"\0" * 10000)
        assert_true(len(_ready_failure_context(operation, process, commit=False, exit_caller=True)) < 8100,
                    "READY failure evidence stays bounded even with escaped log bytes")
        (operation / "state.json").unlink()
        assert_true("state_unavailable" in _ready_failure_context(operation, process, commit=False, exit_caller=True),
                    "missing state remains a named evidence gap instead of hiding READY failure")


def _check_posix_worker_protocol():
    from exoma import _update_worker as worker
    api = worker.Posix()
    own = api.process(os.getpid())
    assert_true(not api.signaled(own), "POSIX current generation remains live")
    assert_true(api.open_identity(os.getpid(), {**own, "created": "not-the-original"}) is None,
                "POSIX reused generation is not waited on")
    with tempfile.TemporaryDirectory(prefix="posix-update-") as tmp:
        root = Path(tmp).resolve()
        for commit, exit_caller in ((False, False), (True, False), (True, True)):
            caller = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **no_console())
            process = None
            try:
                identity = api.process(caller.pid)
                identity["parent"] = os.getpid()
                operation = root / "updates" / uuid_hex()
                operation.mkdir(parents=True)
                script = operation / "worker.py"
                script.write_text(Path(worker.__file__).read_text(encoding="utf-8").replace(
                    "HANDSHAKE_SECONDS = 15", "HANDSHAKE_SECONDS = 1").replace(
                    "CALLER_SECONDS = 60", "CALLER_SECONDS = 1"), encoding="utf-8")
                token = uuid_hex()
                marker = operation / "package-marker"
                code = ("import runpy,os,json; from pathlib import Path; native=runpy.run_path(" + repr(str(script)) +
                        "); Path(" + repr(str(marker)) +
                        ").write_text(json.dumps(native['Posix']().process(os.getpid()))); raise SystemExit(7)")
                request = {"schema_version": worker.SCHEMA, "operation_id": operation.name,
                           "installation_key": worker.installation_key(root / "target", api.user()),
                           "token": token, "prefix": str(root / "target"), "python_relative": "bin/python",
                           "package": [sys.executable, "-c", code], "cwd": str(root), "timeout": 5,
                           "callers": [identity], "worker_sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
                worker.atomic_json(operation / "request.json", request)
                with (operation / "worker.log").open("wb") as log:
                    process = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(script), str(operation)],
                                               stdout=log, stderr=log, start_new_session=True, **no_console())
                until = time.monotonic() + 5
                while not (operation / "ready.json").exists() and process.poll() is None and time.monotonic() < until:
                    time.sleep(0.01)
                if not (operation / "ready.json").exists():
                    raise AssertionError(_ready_failure_context(operation, process, commit=commit, exit_caller=exit_caller))
                if commit:
                    worker.atomic_json(operation / "commit.json", {"token": token})
                if exit_caller:
                    caller.terminate()
                    caller.wait(timeout=5)
                assert_true(process.wait(timeout=12) == 1, "failed or uncommitted update returns failure")
                state = worker.read_json(operation / "state.json")
                assert_true(state["phase"] == "failed" and state["cleanup_confirmed"], "terminal failure follows cleanup")
                if exit_caller:
                    assert_true(marker.exists() and state["stages"]["package"]["exit_code"] == 7 and
                                "install" not in state["stages"] and
                                state["callers_exited_at"] <= state["stages"]["package"]["started_at"],
                                "same package/order/failure boundary as Windows")
                    receipts = list(operation.glob("package.log.cleanup-*.json"))
                    assert_true(len(receipts) == 1, "real failed package stage retains one guardian receipt")
                    receipt = worker.read_json(receipts[0])
                    lifecycle = receipt["diagnostics"]["child"]
                    assert_true(receipt["cleanup_confirmed"] and lifecycle["child_returncode"] == 7 and
                                lifecycle["reap_outcome"] == "reaped" and lifecycle["release_requested"] and
                                lifecycle["exit_observed"] and not lifecycle["synthetic_exit"] and
                                lifecycle["registered_child"] ==
                                {key: json.loads(marker.read_text())[key] for key in ("pid", "created")},
                                "real exec/reap evidence preserves registered generation and observed manager exit")
                else:
                    assert_true(not marker.exists() and not state["stages"], "no mutation before COMMIT and caller exit")
            finally:
                for child in (process, caller):
                    if child and child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
        # Kill the supervisor rather than the stage: the independent guardian must
        # notice EOF and stop both the real stage and its sleeping grandchild.
        ready = root / "guardian-ready.json"
        stage = ("import subprocess,sys,time,json,pathlib,os; "
                 "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                 "pathlib.Path(" + repr(str(ready)) + ").write_text(json.dumps([os.getpid(),p.pid])); time.sleep(60)")
        owner = root / "owner.py"
        owner.write_text("import runpy,types,pathlib\n"
                         "m=runpy.run_path(" + repr(worker.__file__) + ")\n"
                         "f=open(" + repr(str(root / "lease")) + ",'a+b')\n"
                         "m['PosixChildJob'](m['Posix'](),'fixture',types.SimpleNamespace(handle=f)).run(" +
                         repr([sys.executable, "-c", stage]) + "," + repr(str(root)) + "," +
                         repr(str(root / "stage.log")) + ",30,lambda child:None)\n", encoding="utf-8")
        supervisor = subprocess.Popen([sys.executable, str(owner)], **no_console())
        try:
            until = time.monotonic() + 8
            while not ready.exists() and supervisor.poll() is None and time.monotonic() < until:
                time.sleep(0.02)
            assert_true(ready.exists(), "guarded manager and grandchild start after registration")
            identities = [api.process(pid) for pid in json.loads(ready.read_text())]
            supervisor.kill()
            supervisor.wait(timeout=5)
            until = time.monotonic() + 8
            while not all(api.signaled(identity) for identity in identities) and time.monotonic() < until:
                time.sleep(0.02)
            assert_true(all(api.signaled(identity) for identity in identities), "guardian EOF cleans real child tree after supervisor crash")
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=5)

        # The manager exits normally after launching an independently-sessioned
        # descendant. A process-group-only cleanup would report success and leak it.
        from types import SimpleNamespace
        detached_ready = root / "detached.json"
        detached_stage = ("import subprocess,sys,pathlib,json,runpy; "
                          "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True); "
                          "m=runpy.run_path(" + repr(worker.__file__) + "); "
                          "pathlib.Path(" + repr(str(detached_ready)) + ").write_text(json.dumps(m['Posix']().process(p.pid)))")
        for double_fork in (False, True):
            detached_ready.unlink(missing_ok=True)
            program = detached_stage
            if double_fork:
                program = ("import os\nparent=os.fork()\nif parent:\n"
                           "    os.waitpid(parent,0)\n    raise SystemExit(0)\n" + program)
            with (root / "detached-lease").open("a+b") as lease:
                job = worker.PosixChildJob(api, "detached", SimpleNamespace(handle=lease))
                try:
                    code = job.run([sys.executable, "-c", program], root, root / "detached.log", 10, lambda child: None)
                except OSError:
                    assert_true(sys.platform == "darwin" and job.cleanup_unconfirmed,
                                "unverifiable detached ancestry is never a successful cleanup")
                else:
                    assert_true(code == 0 and api.signaled(json.loads(detached_ready.read_text())),
                                "normal stage exit cleans single/double-fork descendants before acknowledging success")
                finally:
                    if detached_ready.exists():
                        identity = json.loads(detached_ready.read_text())
                        try:
                            if not api.signaled(identity):
                                os.kill(identity["pid"], 9)
                        except ProcessLookupError:
                            pass

        # Loss of the guardian is not a cleanup acknowledgement. Keep the stage outcome
        # unknown instead of allowing a failed guardian to release update admission.
        from types import SimpleNamespace
        import signal
        lost_ready = root / "guardian-loss.json"
        lost_stage = stage.replace(str(ready), str(lost_ready))
        owned = []
        with (root / "loss-lease").open("a+b") as lease:
            job = worker.PosixChildJob(api, "loss-fixture", SimpleNamespace(handle=lease))
            def lose_guardian(identity):
                if identity:
                    owned.append(identity)
                elif job.proc.poll() is None:
                    until = time.monotonic() + 3
                    while not lost_ready.exists() and time.monotonic() < until:
                        time.sleep(0.01)
                    assert_true(lost_ready.exists(), "stage started before guardian loss")
                    job.proc.kill()
            try:
                job.run([sys.executable, "-c", lost_stage], root, root / "loss.log", 10, lose_guardian)
            except OSError:
                assert_true(job.cleanup_unconfirmed, "missing guardian acknowledgement leaves cleanup unconfirmed")
            else:
                raise AssertionError("guardian death was accepted as stage completion")
            finally:
                if owned and api.open_identity(owned[0]["pid"], owned[0]):
                    os.killpg(owned[0]["pid"], signal.SIGKILL)

        _check_posix_uncertain_installation(root, api, stage)
        _check_posix_observer(root, api)


def _check_posix_observer(root, api):
    """OS Ctrl+C detaches observation; explicit cancel stops the production worker."""
    from exoma import _update_worker as worker
    import signal
    operation = root / "observer" / uuid_hex()
    operation.mkdir(parents=True)
    script = operation / "worker.py"
    source = Path(worker.__file__).read_text(encoding="utf-8")
    source = source.replace("        time.sleep(0.2)\n\n\ndef callers", (
        "        atomic_json(operation / 'observer-ready.json', {})\n"
        "        time.sleep(0.2)\n\n\ndef callers"))
    assert_true("observer-ready.json" in source, "observer readiness fixture is installed")
    script.write_text(source, encoding="utf-8")
    caller = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **no_console())
    process = observer = None
    marker = operation / "package-ready"
    try:
        token = uuid_hex()
        worker.atomic_json(operation / "request.json", {
            "schema_version": worker.SCHEMA, "operation_id": operation.name,
            "installation_key": worker.installation_key(root / "observer-target", api.user()),
            "token": token, "prefix": str(root / "observer-target"), "python_relative": "bin/python",
            "package": [sys.executable, "-c", "import pathlib,time; pathlib.Path(" + repr(str(marker)) + ").touch(); time.sleep(60)"],
            "cwd": str(root), "timeout": 20, "callers": [api.process(caller.pid)],
            "worker_sha256": hashlib.sha256(script.read_bytes()).hexdigest()})
        with (operation / "worker.log").open("wb") as log:
            process = subprocess.Popen([sys.executable, "-I", "-S", "-B", str(script), str(operation)],
                                       stdout=log, stderr=log, start_new_session=True, **no_console())
        deadline = time.monotonic() + 5
        while not (operation / "ready.json").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert_true((operation / "ready.json").exists(), "production observer fixture reaches READY")
        worker.atomic_json(operation / "commit.json", {"token": token})
        caller.terminate()
        caller.wait(timeout=5)
        deadline = time.monotonic() + 5
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert_true(marker.exists(), "production worker starts the package stage")
        argv = [sys.executable, "-I", "-S", "-B", str(script), "--observe", str(operation), "--wait"]
        observer = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **no_console())
        deadline = time.monotonic() + 5
        while not (operation / "observer-ready.json").exists() and observer.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert_true((operation / "observer-ready.json").exists(), "observer is waiting before the OS signal")
        observer.send_signal(signal.SIGINT)
        stdout, stderr = observer.communicate(timeout=5)
        assert_true(observer.returncode == 3 and json.loads(stdout)["observer_detached"]
                    and not (operation / "cancel.json").exists() and process.poll() is None,
                    "OS Ctrl+C detaches the observer and preserves the running updater: " + stderr)
        cancelled = subprocess.run([*argv, "--cancel"], capture_output=True, text=True, timeout=10, **no_console())
        assert_true(cancelled.returncode == 130 and process.wait(timeout=5) == 130,
                    "explicit observer cancellation stops the production worker: " + cancelled.stderr)
        state = json.loads(cancelled.stdout)
        assert_true(state["phase"] == "interrupted" and state["completed"] and state["cleanup_confirmed"]
                    and state["cancellation"]["source"] == "explicit_observer" and "install" not in state["stages"],
                    "cancelled update reports terminal interruption only after cleanup and skips later stages")
    finally:
        for child in (observer, process, caller):
            if child and child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def _check_posix_uncertain_installation(root, api, stage):
    """Full supervisor failure retains installation ownership across job stores."""
    from exoma import _update_worker as worker
    import signal
    target = root / "uncertain-target"
    key = worker.installation_key(target, api.user())
    first = root / "uncertain" / uuid_hex()
    first.mkdir(parents=True)
    marker = root / "uncertain-children.json"
    original_run = worker.PosixChildJob.run
    identities = []
    probe = worker.PosixLock(api, key)
    claim_path = probe.claim_path
    probe.close()

    def request(operation, store, package):
        store.mkdir()
        reservation = store / ".update-reservation.json"
        worker.atomic_json(reservation, {"token": "fixture"})
        worker.atomic_json(operation / "commit.json", {"token": "fixture"})
        worker.atomic_json(operation / "request.json", {
            "schema_version": worker.SCHEMA, "operation_id": operation.name,
            "installation_key": key, "token": "fixture", "prefix": str(target),
            "reservation": str(reservation), "python_relative": "bin/python",
            "package": package, "cwd": str(root), "timeout": 10,
            "callers": [api.process(os.getpid())],
            "worker_sha256": hashlib.sha256(Path(worker.__file__).read_bytes()).hexdigest()})
        return reservation

    def lose_guardian(job, argv, cwd, log, timeout, tick):
        def race(child):
            tick(child)
            if child is None and job.proc.poll() is None:
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert_true(marker.exists(), "full update started its real package tree")
                identities.extend(api.process(pid) for pid in json.loads(marker.read_text()))
                job.proc.kill()
        return original_run(job, argv, cwd, log, timeout, race)

    reservation = request(first, root / "store-a", [sys.executable, "-c", stage.replace(
        str(root / "guardian-ready.json"), str(marker))])
    try:
        with patch.object(worker, "platform_api", return_value=api), \
             patch.object(api, "signaled", return_value=True), \
             patch.dict(os.environ), \
             patch.object(worker.PosixChildJob, "run", lose_guardian):
            assert_true(worker.run_request(first) == 4, "guardian loss leaves full update unknown")
        state = worker.read_json(first / "state.json")
        assert_true(state["phase"] == "cleanup_pending" and not state["completed"]
                    and claim_path.exists() and reservation.exists() and identities
                    and any(not api.signaled(identity) for identity in identities),
                    "uncertain full update keeps durable installation claim and local admission")
        second = root / "other-store" / uuid_hex()
        second.mkdir(parents=True)
        mutation = root / "second-package-ran"
        request(second, root / "store-b", [sys.executable, "-c",
            "from pathlib import Path; Path(" + repr(str(mutation)) + ").touch()"])
        with patch.object(worker, "platform_api", return_value=api):
            assert_true(worker.run_request(second) == 1, "another store cannot bypass uncertain installation ownership")
        assert_true(not mutation.exists() and not (second / "ready.json").exists()
                    and not worker.read_json(second / "state.json")["stages"],
                    "second updater is refused before READY and package mutation")
        try:
            with patch.object(worker.Posix, "signaled", return_value=True):
                worker.recover_posix(first)
        except OSError:
            pass
        else:
            raise AssertionError("recovery inferred cleanup from guardian loss")
        assert_true(claim_path.exists(), "unproven explicit recovery preserves installation exclusion")
    finally:
        for identity in identities:
            if api.open_identity(identity["pid"], identity):
                os.kill(identity["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 5
        while any(not api.signaled(identity) for identity in identities) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert_true(all(api.signaled(identity) for identity in identities), "fault fixture leaves no live owned processes")
        # Simulate a changed boot only after the fixture's actual children stopped.
        # Same-boot recovery above could not infer cleanup from dead leaders.
        boot = worker.boot_identity()
        with patch.object(worker, "boot_identity", return_value=boot + "-reboot-fixture"), redirect_stdout(io.StringIO()):
            assert_true(worker.recover_posix(first) == 0, "explicit recovery accepts a proven changed boot generation")
        assert_true(not claim_path.exists() and not reservation.exists()
                    and worker.read_json(first / "state.json")["cleanup_confirmed"],
                    "confirmed recovery releases only the bound installation and reservation")
