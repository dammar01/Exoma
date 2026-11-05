"""The public update command refreshes the package before using the fresh CLI."""

import os
import io
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from exoma import self_update
from tests.checks.support import assert_true, no_console

REPO = Path(__file__).resolve().parents[2]


def _test_self_update() -> None:
    with tempfile.TemporaryDirectory(prefix="exoma-update-") as tmp:
        # update resolves its cwd; Windows runners hand out 8.3 short temp paths that differ.
        root = Path(tmp).resolve()
        nested = root / "src" / "app"
        nested.mkdir(parents=True)
        (root / ".workflow").mkdir()
        assert_true(self_update._workspace_root(nested) == root,
                    "a workspace is found when update runs inside a project subdirectory")

        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update.subprocess, "run") as not_called:
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
                 patch.object(self_update.subprocess, "run") as not_called:
                assert_true(self_update.main(["--manager", "pip"]) == 1,
                            "invalid workspace config stops before the package update")
                not_called.assert_not_called()
        (root / ".workflow" / "config.json").write_text("{}", encoding="utf-8")

        from exoma.core.runtime import upgrade as workspace_upgrade
        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(workspace_upgrade, "active_jobs_for_workspace", return_value=[{"job_id": "active"}]), \
             patch.object(self_update.subprocess, "run") as not_called:
            assert_true(self_update.main(["--manager", "pip"]) == 1,
                        "update refuses a workspace with an active job before touching the package")
            not_called.assert_not_called()

        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs.get("cwd")))
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update.subprocess, "run", side_effect=fake_run):
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
        def workspace_failure(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1 if argv[1:4] == ["-m", "exoma", "install"] and "--check" not in argv else 0)
        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update.subprocess, "run", side_effect=workspace_failure):
            assert_true(self_update.main(["--manager", "pip"]) == 1 and len(calls) == 2,
                        "a failed installer workspace upgrade stops before the final check")

        receipts_home = root / "receipt-home"
        receipt = receipts_home / ".claude" / "backups" / "install_retry" / "install_receipt.json"
        calls.clear()
        def failed_with_receipt(argv, **kwargs):
            calls.append(argv)
            if argv[1:4] == ["-m", "exoma", "install"] and "--check" not in argv:
                receipt.parent.mkdir(parents=True)
                receipt.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 1)
            return subprocess.CompletedProcess(argv, 0)
        errors = io.StringIO()
        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update, "agent_home", return_value=receipts_home), \
             patch.object(self_update.subprocess, "run", side_effect=failed_with_receipt), \
             redirect_stderr(errors):
            assert_true(self_update.main(["--manager", "pip"]) == 1 and len(calls) == 2,
                        "failed install stops before check")
        assert_true("--rollback install_retry --apply" in errors.getvalue()
                    and str(receipt) in errors.getvalue()
                    and "exoma upgrade --work-dir" in errors.getvalue(),
                    "failed install names only its new global receipt and workspace recovery")

        calls.clear()
        errors = io.StringIO()
        def failed_check(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1 if "--check" in argv else 0)
        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update, "agent_home", return_value=receipts_home), \
             patch.object(self_update.subprocess, "run", side_effect=failed_check), \
             redirect_stderr(errors):
            assert_true(self_update.main(["--manager", "pip"]) == 1 and len(calls) == 3,
                        "failed check also reports an incomplete update")
        assert_true("check failed" in errors.getvalue() and "--rollback" not in errors.getvalue(),
                    "an older receipt is never offered as recovery for a later failure")

        calls.clear()
        with patch.object(self_update.Path, "cwd", return_value=nested), \
             patch.object(self_update.subprocess, "run", side_effect=lambda argv, **kw: (
                 calls.append(argv) or subprocess.CompletedProcess(argv, 1))):
            assert_true(self_update.main(["--manager", "pip"]) == 1 and len(calls) == 1,
                        "a failed package update stops before changing installed config")

        with tempfile.TemporaryDirectory(prefix="exoma-update-plain-") as plain:
            empty = Path(plain)
            calls.clear()
            with patch.object(self_update.Path, "cwd", return_value=empty), \
                 patch.object(self_update.subprocess, "run", side_effect=fake_run):
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
                    "a workspace upgrade failure returns nonzero after the global install report")

        uv_dir = root / "uv-tools"
        with patch.object(self_update.shutil, "which", return_value="uv"), \
             patch.object(self_update.sys, "prefix", str(uv_dir / "exoma")), \
             patch.object(self_update.subprocess, "run", return_value=subprocess.CompletedProcess(
                 ["uv", "tool", "dir"], 0, stdout=str(uv_dir))):
            assert_true(self_update._package_command("auto") == ("uv", ["uv", "tool", "upgrade", "exoma"]),
                        "uv tool installs update through uv")
        with patch.object(self_update.shutil, "which", return_value="uv"), \
             patch.object(self_update.sys, "prefix", str(root / "other-python")), \
             patch.object(self_update.subprocess, "run", return_value=subprocess.CompletedProcess(
                 ["uv", "tool", "dir"], 0, stdout=str(uv_dir))):
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
                 patch.object(self_update.subprocess, "run", return_value=subprocess.CompletedProcess(
                     ["pip"], 1)) as pip_run, \
                 redirect_stderr(errors):
                assert_true(not self_update._run_step("package", [sys.executable, "-m", "pip"], root, pip=True),
                            "a failed pip step fails")
            assert_true("stderr" not in pip_run.call_args.kwargs and "capture_output" not in pip_run.call_args.kwargs,
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
        assert_true(result.returncode == 0 and "--manager" in result.stdout and "pipx" in result.stdout,
                    f"public CLI routes {spelling} to update help, pipx among the managers")
