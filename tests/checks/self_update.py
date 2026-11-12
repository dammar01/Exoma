"""The public update command refreshes the package before using the fresh CLI."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from exoma import self_update
from tests.checks.support import assert_true, no_console

REPO = Path(__file__).resolve().parents[2]


def _test_self_update() -> None:
    if os.environ.get("EXOMA_UPDATE_UPGRADE_SMOKE") == "1":
        _check_installed_upgrade()
        return
    _check_update_deadlines_and_interruption()
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
            second = run([exe, "update", "--timeout", "60"])
            assert_true("completed in" in second and "[UPDATE] complete" in second,
                        "a second invocation exercises the newly installed supervisor")
            run([exe, "install", "--check"])
            installed_python = root / "tools/exoma" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            smoke = run([installed_python, "-c", "import exoma, sys; print(exoma.__file__); print(sys.executable)"])
            assert_true(str(root / "tools/exoma") in smoke, "imports and interpreter come from the isolated tool")
        report = {
            "baseline": "1.0.1", "candidate": TOOL_VERSION, "uv": uv_version,
            "python": sys.version.split()[0], "platform": sys.platform,
            "old_updater_exit": old_code, "manual_recovery_checked": bool(old_code),
            "new_updater_exit": 0, "install_check_exit": 0,
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
