"""The suite runs the same on every machine, whatever provider the machine picked."""

import json
import shutil
import tempfile
from pathlib import Path

from tests.checks.support import EXAMPLE_SEED, assert_true


def _test_suite_ignores_the_machine_seed() -> None:
    """A workspace a test builds is seeded from the shipped example, never the local seed."""
    from exoma.adapters.install.opencode_install import _copy_provider_config
    from exoma.core.workspace.workspace_paths import PROVIDER_CONFIG_NAME, WORKFLOW_DIRNAME

    tool = Path(tempfile.mkdtemp(prefix="seed-tool-"))
    project = Path(tempfile.mkdtemp(prefix="seed-project-"))
    try:
        (tool / "config").mkdir()
        (tool / "config" / "second_agent.seed.json").write_text(
            json.dumps({"provider": "codex"}), encoding="utf-8"
        )
        (tool / "config" / EXAMPLE_SEED).write_text(
            json.dumps({"provider": "opencode"}), encoding="utf-8"
        )
        (project / WORKFLOW_DIRNAME).mkdir()
        _copy_provider_config(project, tool / "config", tool / "config")
        copied = json.loads(
            (project / WORKFLOW_DIRNAME / PROVIDER_CONFIG_NAME).read_text(encoding="utf-8")
        )
        assert_true(
            copied.get("provider") == "opencode",
            f"a test workspace must be seeded from the shipped example, got {copied}",
        )
    finally:
        shutil.rmtree(tool, ignore_errors=True)
        shutil.rmtree(project, ignore_errors=True)
