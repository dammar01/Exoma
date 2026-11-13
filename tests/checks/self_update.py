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
    _check_worker_protocol()
    # Existing tests cover the synchronous contract on every OS. Native Windows
    # handoff is exercised separately, including real launcher/process lifetimes.
    with patch.object(self_update, "IS_WINDOWS", False):
        _check_synchronous_update()


def _check_synchronous_update() -> None:
    if os.environ.get("EXOMA_UPDATE_UPGRADE_SMOKE") == "1":
        _check_installed_upgrade()
        return
    _check_update_deadlines_and_interruption()
    _check_update_migrates_workflow()
    with tempfile.TemporaryDirectory(prefix="exoma-update-") as tmp:
        # update resolves its cwd; Windows runners hand out 8.3 short temp paths that differ.
        root = Path(tmp).resolve()
        nested = root / "src" / "app"
        nested.mkdir(parents=True)
        (root / ".workflow").mkdir()
        assert_true(self_update._workspace_root(nested) == root,
                    "a workspace is found when update runs inside a project subdirectory")

        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update, "run_bounded") as not_called:
            assert_true(self_update.main(["--manager", "pip"]) == 1,
                        "update refuses an incomplete workspace before changing the package")
            not_called.assert_not_called()
        (root / ".workflow" / "config.json").write_text("{}", encoding="utf-8")
        (nested / ".workflow").mkdir()
        assert_true(self_update._workspace_root(nested) == root,
                    "an incomplete nested .workflow does not hide the configured workspace")

        for invalid in ("{broken", "[]", '{"runtime": []}'):
            (root / ".workflow" / "config.json").write_text(invalid, encoding="utf-8")
            with patch.object(self_update.Path, "cwd", return_value=nested), \
                 patch.object(self_update, "run_bounded") as not_called:
                assert_true(self_update.main(["--manager", "pip"]) == 1,
                            "invalid workspace config stops before the package update")
                not_called.assert_not_called()
        (root / ".workflow" / "config.json").write_text("{}", encoding="utf-8")

        from exoma.core.runtime import upgrade as workspace_upgrade
        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(workspace_upgrade, "active_jobs_for_workspace", return_value=[{"job_id": "active"}]), \
             patch.object(self_update, "run_bounded") as not_called:
            assert_true(self_update.main(["--manager", "pip"]) == 1,
                        "update refuses a workspace with an active job before touching the package")
            not_called.assert_not_called()

        calls = []

        def fake_run(argv, timeout, **kwargs):
            calls.append((argv, kwargs.get("cwd")))
            return 0, "", ""

        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update, "run_bounded", side_effect=fake_run):
            assert_true(self_update.main(["--manager", "pip"]) == 0, "update succeeds")
        assert_true([argv[1:4] for argv, _cwd in calls] ==
                    [["-m", "pip", "install"], ["-m", "exoma", "install"],
                     ["-m", "exoma", "install"]],
                    f"package, installer with workspace upgrade, and check run in order: {calls}")
        assert_true(len(calls) == 3,
                    f"update uses the installer's workspace upgrade only once: {calls}")
        assert_true("--no-input" in calls[1][0] and "--check" in calls[2][0],
                    f"the fresh installer asks nothing (an update is unattended): {calls}")
        assert_true(all(Path(cwd) == nested for _argv, cwd in calls),
                    f"every update step runs in the resolved workspace cwd: {calls}")

        calls.clear()
        def workspace_failure(argv, timeout, **kwargs):
            calls.append(argv)
            return (1 if argv[1:4] == ["-m", "exoma", "install"] and "--check" not in argv else 0), "", ""
        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update, "run_bounded", side_effect=workspace_failure):
            assert_true(self_update.main(["--manager", "pip"]) == 1 and len(calls) == 2,
                        "a failed installer workspace upgrade stops before the final check")

        receipts_home = root / "receipt-home"
        receipt = receipts_home / ".claude" / "backups" / "install_retry" / "install_receipt.json"
        calls.clear()
        def failed_with_receipt(argv, timeout, **kwargs):
            calls.append(argv)
            if argv[1:4] == ["-m", "exoma", "install"] and "--check" not in argv:
                receipt.parent.mkdir(parents=True)
                receipt.write_text("{}", encoding="utf-8")
                return 1, "", ""
            return 0, "", ""
        errors = io.StringIO()
        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update, "agent_home", return_value=receipts_home), \
             patch.object(self_update, "run_bounded", side_effect=failed_with_receipt), \
             redirect_stderr(errors):
            assert_true(self_update.main(["--manager", "pip"]) == 1 and len(calls) == 2,
                        "failed install stops before check")
        assert_true("--rollback install_retry --apply" in errors.getvalue()
                    and str(receipt) in errors.getvalue()
                    and "exoma upgrade --work-dir" in errors.getvalue(),
                    "failed install names only its new global receipt and workspace recovery")

        calls.clear()
        errors = io.StringIO()
        def failed_check(argv, timeout, **kwargs):
            calls.append(argv)
            return (1 if "--check" in argv else 0), "", ""
        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update, "agent_home", return_value=receipts_home), \
             patch.object(self_update, "run_bounded", side_effect=failed_check), \
             redirect_stderr(errors):
            assert_true(self_update.main(["--manager", "pip"]) == 1 and len(calls) == 3,
                        "failed check also reports an incomplete update")
        assert_true("check failed" in errors.getvalue() and "--rollback" not in errors.getvalue(),
                    "an older receipt is never offered as recovery for a later failure")

        calls.clear()
        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update, "run_bounded", side_effect=lambda argv, timeout, **kw: (
                 calls.append(argv) or (1, "", ""))):
            assert_true(self_update.main(["--manager", "pip"]) == 1 and len(calls) == 1,
                        "a failed package update stops before changing installed config")

        with tempfile.TemporaryDirectory(prefix="exoma-update-plain-") as plain:
            empty = Path(plain)
            calls.clear()
            with patch.object(self_update.Path, "cwd", return_value=empty), \
                 patch.object(self_update, "run_bounded", side_effect=fake_run):
                assert_true(self_update.main(["--manager", "pip"]) == 0
                            and not any("upgrade" in argv for argv, _cwd in calls),
                            "without .workflow, update skips the workspace step")

        # The installer may finish global writes before the workspace rejects an upgrade.
        # That partial result must have a failing exit code for update to stop here.
        isolated = root / "isolated-home"
        isolated.mkdir()
        broken = root / "broken-project"
        (broken / ".workflow").mkdir(parents=True)
        (broken / ".workflow" / "config.json").write_text("{invalid", encoding="utf-8")
        env = dict(os.environ)
        env["AGENT_HOME"] = str(isolated)
        env["PYTHONPATH"] = str(REPO / "src")
        installed = subprocess.run(
            [sys.executable, "-m", "exoma.install", "claude",
             "--provider", "opencode"],
            cwd=broken, env=env, capture_output=True, text=True, timeout=60, **no_console(),
        )
        assert_true(installed.returncode == 1 and "workspace not upgraded" in installed.stdout,
                    "a workspace upgrade failure returns nonzero after the global install report: "
                    + installed.stdout[-1200:] + installed.stderr[-500:])

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
        for failed_stage in range(3):
            for failure, expected in ((None, 1), (KeyboardInterrupt(), 130), (OSError("blocked"), 1)):
                calls = []

                def run(argv, timeout, **kwargs):
                    calls.append((argv, timeout, kwargs))
                    if len(calls) - 1 == failed_stage:
                        kwargs["on_tick"](0.1)
                        if isinstance(failure, BaseException):
                            raise failure
                        return failure, "", ""
                    return 0, "", ""

                out, err = io.StringIO(), io.StringIO()
                with patch.object(self_update.Path, "cwd", return_value=root), \
                     patch.object(self_update, "run_bounded", side_effect=run), \
                     patch.object(self_update, "_receipt_names", side_effect=[set(), {"install_timeout"}]), \
                     redirect_stdout(out), redirect_stderr(err):
                    status = self_update.main(["--manager", "pip", "--timeout", "0.25"])
                assert_true(status == expected and len(calls) == failed_stage + 1,
                            "timeout, launch failure and cancellation skip all later stages")
                assert_true(all(timeout == 0.25 and kw["inherit_output"] for _, timeout, kw in calls),
                            "the explicit deadline applies to every stage with live inherited output")
                assert_true("still waiting; elapsed" in err.getvalue() and "[UPDATE] complete\n" not in out.getvalue(),
                            "a waiting diagnostic never claims the failed update completed")
                if failed_stage == 0:
                    assert_true("package state is uncertain" in err.getvalue()
                                and "--rollback" not in err.getvalue(),
                                "an interrupted package command reports uncertain package state")
                else:
                    assert_true("update is incomplete" in err.getvalue()
                                and "--rollback install_timeout --apply" in err.getvalue()
                                and "exoma upgrade --work-dir" in err.getvalue(),
                                "refresh/check failure and cancellation retain receipt/workspace recovery")
                if failure is None:
                    assert_true("timed out after 0.25s" in err.getvalue(), "timeouts name their stage deadline")

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
             patch.object(worker, "Win32", return_value=API()), \
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


def _poll_handoff(root, timeout):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value, state_path = _read_handoff_state(root)
        except (FileNotFoundError, PermissionError) as exc:
            last_error = exc
        else:
            last_error = None
        if last_error is None and value is not None:
            if value["phase"] in ("succeeded", "failed", "interrupted"):
                if value["phase"] != "succeeded":
                    try:
                        logs = "\n".join(path.name + ":\n" + path.read_text(encoding="utf-8", errors="replace")[-6000:]
                                         for path in state_path.parent.glob("*.log"))
                    except OSError as exc:
                        logs = f"logs unavailable: {exc}"
                    raise AssertionError(f"handoff outcome: {value}\n{logs}")
                return value, state_path
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))
    detail = f"; last read error: {last_error}" if last_error is not None else ""
    raise AssertionError(f"handoff did not finish within {timeout}s: {root}{detail}") from last_error


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
        api = MagicMock()
        api.user.return_value, api.in_job.return_value = "fixture-user", False
        descriptor = {"prefix": str(root / "target"), "runtime": sys.executable,
                      "python_relative": "Scripts/python.exe", "entrypoints": []}
        for committed in (False, True):
            state_root = root / str(committed)
            effects = [None, TimeoutError("accepted.json access denied")] if committed else [TimeoutError("ready.json access denied")]
            errors = io.StringIO()
            with patch.object(job_manager, "JobManager", return_value=jobs), \
                 patch.object(self_update.install_method, "update_descriptor", return_value=descriptor), \
                 patch.object(worker, "Win32", return_value=api), patch.object(worker, "callers", return_value=[]), \
                 patch.object(self_update, "_update_root", return_value=state_root), \
                 patch.object(update_metadata, "distribution_info", return_value={"installed": "fixture-A"}), \
                 patch.object(self_update, "_receipt_names", return_value=set()), \
                 patch.object(self_update, "_workspace_root", return_value=None), \
                 patch.object(self_update.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                 patch.object(self_update.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, create=True), \
                 patch.object(self_update.subprocess, "Popen"), \
                 patch.object(self_update, "_wait_for_handshake", side_effect=effects), redirect_stderr(errors):
                code = self_update._windows_handoff("pip", [sys.executable, "-m", "pip"], root, 1)
            assert_true(code == 1 and bool(list(state_root.rglob("commit.json"))) == committed,
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
        for value in (ValueError("corrupt journal"), ({"phase": "failed"}, state_path)):
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
        return
    from exoma import _update_worker as worker

    api = worker.Win32()
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


def uuid_hex():
    import uuid
    return uuid.uuid4().hex


def _handoff_independence_verified(platform, contained, state):
    return platform == "win32" and not contained and isinstance(state, dict) and state.get("phase") == "succeeded"


@contextmanager
def _artifact_evidence(report_path):
    """Keep completed transition evidence even if the later no-op or cleanup fails."""
    from exoma import _update_worker as worker

    evidence = {"passed": False, "platform": sys.platform, "actual_version_transition": False,
                "independent_worker_verified": False, "no_op_verified": False}
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


def _check_artifact_reporting():
    assert_true(not _handoff_independence_verified("linux", False, {"phase": "succeeded"})
                and not _handoff_independence_verified("darwin", False, {"phase": "succeeded"})
                and not _handoff_independence_verified("win32", True, {"phase": "succeeded"})
                and not _handoff_independence_verified("win32", False, None)
                and not _handoff_independence_verified("win32", False, {"phase": "failed"})
                and _handoff_independence_verified("win32", False, {"phase": "succeeded"}),
                "only successful strict Windows handoff can attest independent worker survival")
    with tempfile.TemporaryDirectory(prefix="exoma-artifact-evidence-") as tmp:
        report_path = Path(tmp) / "report.json"
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
         tempfile.TemporaryDirectory(prefix="exoma-handoff-ü-") as tmp:
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
                                              "AI_PROXY_", "CLAUDE_", "GEMINI_", "OPENCODE_"))
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
        exe = root / "bin" / ("exoma.exe" if os.name == "nt" else "exoma")
        def run(argv):
            result = subprocess.run([str(item) for item in argv], cwd=project, env=env,
                                    capture_output=True, text=True, timeout=180, **no_console())
            assert_true(result.returncode == 0, result.stdout + result.stderr)
            return result.stdout
        shutil.copyfile(wheel_paths[0], wheels / wheel_paths[0].name)
        run([uv, "tool", "install", "exoma", "--python", sys._base_executable,
             "--no-index", "--find-links", wheels])
        assert_true(versions[0] in run([exe, "--version"]), "launcher starts fixture A")
        run([exe, "install", "claude", "--provider", "opencode", "--no-input"])
        run([exe, "init", "--work-dir", project, "--host", "claude"])
        _seed_migration_user_data(home, project)
        shutil.copyfile(wheel_paths[1], wheels / wheel_paths[1].name)
        output = run([exe, "update", "--timeout", "120"])
        state = None
        if os.name == "nt":
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
        else:
            assert_true("[UPDATE] complete" in output, "POSIX retains synchronous completion")
        interpreter = root / "tools/exoma" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        marker = run([interpreter, "-I", "-c", "import exoma._handoff_fixture as f; print(f.MARKER); print(f.__file__)"])
        assert_true(versions[1] in marker and str(root / "tools/exoma") in marker,
                    "B marker imports from upgraded target, not checkout")
        assert_true(versions[1] in run([exe, "--version"]), "console entrypoint starts B")
        _assert_migrated_user_data(run, home, project)
        run([exe, "install", "--check"])
        report.update(actual_version_transition=True,
                      independent_worker_verified=_handoff_independence_verified(sys.platform, contained, state),
                      state=state)
        print(f"TRANSITION EVIDENCE: {versions[0]} -> {versions[1]}; refresh/check and user data checked; "
              f"Windows independent worker={report['independent_worker_verified']}; full gate still awaits separate no-op", flush=True)
        # A no-op remains a separate case after the strict transition assertion.
        run([exe, "update", "--timeout", "120"])
        if os.name == "nt":
            second, _ = _poll_handoff(root / "state", 300)
            assert_true(second["version_before"] == second["version_after"] == versions[1], "explicit no-op case")
        report.update(passed=True, no_op_verified=True)
        label = "CONTAINED WHEEL (not independence acceptance)" if contained else (
            "HANDOFF WHEEL" if os.name == "nt" else "POSIX SYNCHRONOUS WHEEL (not Windows independence acceptance)")
        print(f"{label}: {versions[0]} -> {versions[1]}; no manual recovery; then separate no-op")
