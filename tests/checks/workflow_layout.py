"""`.workflow/` holds what a person edits; `.workflow/data/` holds everything else.

One rule decides where a stream, store, cache or session lives (`workspace_paths.data_dir`),
and the shipped hooks and run scripts apply that same rule on their own. These checks pin
both halves: a new workspace keeps its root to editable files, a v3.5.x-layout workspace that has not
been migrated keeps working where its data already is, and the hooks cannot drift from the
Python definition of "legacy".
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from core.workspace.workspace_paths import _LEGACY_MARKERS, data_dir, is_legacy_layout, workflow_paths
from tests.checks.support import assert_true

_REPO = Path(__file__).resolve().parents[2]
_EDITABLE_ROOT = {"config.json", "second_agent.json", ".gitignore", "data", "e2e", "current"}
_SCRIPT = re.compile(r"^(run|check|inspect)\.(ps1|sh)$")


def _check_new_workspace_keeps_its_root_editable() -> None:
    import os

    from core.evidence.contracts import UsageRecord
    from core.evidence.runtime_io import write_audit_record, write_usage_record
    from core.runtime.state import ensure_workflow_workspace

    root = Path(tempfile.mkdtemp(prefix="aw-layout-new-"))
    try:
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
        write_usage_record(root, UsageRecord(command="explore").to_dict())
        write_audit_record(root, {"command": "explore"})
        paths = workflow_paths(root, "sid-layout")
        assert_true(paths["data_dir"] == root / ".workflow" / "data" and not is_legacy_layout(root),
                    f"a new workspace uses the data directory: {paths['data_dir']}")
        assert_true((root / ".workflow" / "data" / "usage.jsonl").exists() and (root / ".workflow" / "data" / "audit.jsonl").exists(),
                    "streams land in data/")
        assert_true(paths["session_dir"] == root / ".workflow" / "data" / "sessions" / "sid-layout",
                    f"sessions live under data/: {paths['session_dir']}")
        assert_true(paths["config"] == root / ".workflow" / "config.json" and paths["secrets"] == root / ".workflow" / "e2e" / "secrets.json",
                    "editable files stay at the .workflow root")
        stray = sorted(
            entry.name for entry in (root / ".workflow").iterdir()
            if entry.name not in _EDITABLE_ROOT and not _SCRIPT.match(entry.name)
        )
        assert_true(not stray, f"nothing internal is written beside the editable files: {stray}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_unmigrated_workspace_stays_where_its_data_is() -> None:
    from core.evidence.contracts import UsageRecord
    from core.evidence.runtime_io import write_usage_record

    root = Path(tempfile.mkdtemp(prefix="aw-layout-legacy-"))
    try:
        (root / ".workflow" / "sessions").mkdir(parents=True)
        (root / ".workflow" / "usage.jsonl").write_text("", encoding="utf-8")
        assert_true(is_legacy_layout(root) and data_dir(root) == root / ".workflow",
                    "a v3.5.x-layout workspace keeps reading its data at the root until it is migrated")
        write_usage_record(root, UsageRecord(command="explore").to_dict())
        assert_true((root / ".workflow" / "usage.jsonl").read_text(encoding="utf-8").strip(),
                    "and keeps appending to the history it already has, not a new empty one")
        (root / ".workflow" / "data").mkdir()
        assert_true(not is_legacy_layout(root) and data_dir(root) == root / ".workflow" / "data",
                    "once data/ exists, it is the layout")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_hooks_share_the_rule() -> None:
    hooks = _REPO / "dist" / "config" / "claude" / "hooks"
    for name in ("intent-gate-set", "intent-gate-check", "workflow-statusline"):
        for suffix in ("py",):
            text = (hooks / f"{name}.{suffix}").read_text(encoding="utf-8")
            listed = re.findall(r"['\"]([\w.-]+(?:\.jsonl)?)['\"]", text[text.find("data") :])
            assert_true(
                all(marker in listed for marker in _LEGACY_MARKERS),
                f"{name}.{suffix} must name every legacy marker workspace_paths uses: {_LEGACY_MARKERS}",
            )
            assert_true(
                ('".workflow\\sessions' not in text) and ('".workflow", "sessions"' not in text)
                and ("'.workflow\\usage.jsonl'" not in text) and ('".workflow", "usage.jsonl"' not in text),
                f"{name}.{suffix} still spells a path at the .workflow root instead of the data directory",
            )


def _check_consumers_do_not_spell_the_root_layout() -> None:
    """Messages and tools that name a data path must name the one a migrated workspace has."""
    for relative in ("adapters/providers/opencode_adapter.py", "adapters/providers/codex_adapter.py"):
        text = (_REPO / relative).read_text(encoding="utf-8")
        assert_true(
            ".workflow/sessions/" not in text and '".workflow" / "usage.jsonl"' not in text,
            f"{relative} still points at a .workflow root path instead of .workflow/data/",
        )


def _test_workflow_layout() -> None:
    _check_new_workspace_keeps_its_root_editable()
    _check_unmigrated_workspace_stays_where_its_data_is()
    _check_hooks_share_the_rule()
    _check_consumers_do_not_spell_the_root_layout()
