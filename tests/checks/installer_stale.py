"""`install.py --apply` leaves no stale code behind, and never takes the user's files with it.

A release that renames or drops a skill, command, hook or provider agent used to leave the
old file installed forever, and a settings.json hook kept calling a script nobody shipped.
These checks pin the removal rule: only a file an install recorded writing, still exactly
as written, in a managed family, and no longer shipped — backed up, receipted, and undone
by --rollback. Plus the two neighbours of that rule: a dist/ that does not match its
manifest is refused, and an upgrade repoints a workspace at the build running it.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
from pathlib import Path

from installer.base import _RECEIPT, Plan
from tests.checks.support import assert_true


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextlib.contextmanager
def _sandbox():
    """A fake ~/.claude with its install history, wired into the installer modules."""
    import installer.rollback as rollback
    import installer.stale as stale

    root = Path(tempfile.mkdtemp(prefix="aw-install-stale-"))
    home = root / "home" / ".claude"
    (home / "skills").mkdir(parents=True)
    (home / "hooks").mkdir(parents=True)
    backups = home / "backups"
    backups.mkdir()
    saved = (stale.LEDGER, rollback._backup_dirs)
    stale.LEDGER = home / ".workflow-installed.json"
    rollback._backup_dirs = lambda: sorted((p for p in backups.iterdir() if p.is_dir()), reverse=True)
    _RECEIPT.clear()
    try:
        yield home, backups
    finally:
        stale.LEDGER, rollback._backup_dirs = saved
        _RECEIPT.clear()
        shutil.rmtree(root, ignore_errors=True)


def _receipt(backups: Path, name: str, entries: list[dict]) -> Path:
    directory = backups / name
    directory.mkdir()
    (directory / "install_receipt.json").write_text(
        json.dumps({"schema_version": 2, "entries": entries}), encoding="utf-8"
    )
    return directory


def _check_only_our_unedited_leftovers_go() -> None:
    import installer.stale as stale

    with _sandbox() as (home, backups):
        old = home / "skills" / "retired.md"
        old.write_text("an old skill\n", encoding="utf-8")
        edited = home / "skills" / "edited.md"
        edited.write_text("as installed\n", encoding="utf-8")
        edited_sha = _sha(edited)
        edited.write_text("as installed, then changed by the user\n", encoding="utf-8")
        current = home / "skills" / "plan.md"
        current.write_text("still shipped\n", encoding="utf-8")
        mine = home / "skills" / "my-own.md"
        mine.write_text("the user's skill\n", encoding="utf-8")
        settings = home / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        hook = home / "hooks" / "old-gate.sh"
        hook.write_text("echo old\n", encoding="utf-8")
        _receipt(backups, "install_20250101_000000", [
            {"action": "create", "key": "claude/skills/retired.md", "dest": str(old), "post_sha256": _sha(old)},
            {"action": "create", "key": "claude/skills/edited.md", "dest": str(edited), "post_sha256": edited_sha},
            {"action": "create", "key": "claude/skills/plan.md", "dest": str(current), "post_sha256": _sha(current)},
            {"action": "create", "key": "claude/settings.json", "dest": str(settings), "post_sha256": _sha(settings)},
            {"action": "create", "key": "claude/hooks/old-gate.sh", "dest": str(hook), "post_sha256": _sha(hook)},
        ])

        removable, kept = stale.stale_files({str(current)})
        assert_true(
            sorted(p.name for p, _ in removable) == ["old-gate.sh", "retired.md"] and [p.name for p in kept] == ["edited.md"],
            f"only recorded, unedited, unshipped managed files are removable: {removable} kept={kept}",
        )
        assert_true(
            str(settings) not in {str(p) for p, _ in removable},
            "settings.json is merged, not a managed family file: an install never deletes it",
        )

        plan = Plan()
        install_dir = backups / "install_29990101_000000"
        removed_stems = stale.prune({str(current)}, plan, True, install_dir)
        assert_true(not old.exists() and not hook.exists(), "the stale files are gone")
        assert_true(mine.exists() and edited.exists() and current.exists() and settings.exists(),
                    "the user's skill, the edited leftover, the shipped file and settings.json are untouched")
        assert_true(removed_stems == ["old-gate"], f"a removed hook names its script stem: {removed_stems}")
        assert_true(any("edited.md" in w and "kept" in w for w in plan.warnings), f"the kept leftover is named: {plan.warnings}")
        assert_true(
            (install_dir / "stale" / "claude" / "skills" / "retired.md").read_text(encoding="utf-8") == "an old skill\n",
            "every removal is backed up first",
        )

        # --rollback restores a removed file from its receipt.
        from installer.rollback import _run_rollback

        (install_dir / "install_receipt.json").write_text(
            json.dumps({"schema_version": 2, "entries": list(_RECEIPT)}), encoding="utf-8"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            code = _run_rollback(install_dir.name, True)
        assert_true(code == 0 and old.read_text(encoding="utf-8") == "an old skill\n" and hook.exists(),
                    f"rollback puts removed leftovers back: code={code}")


def _check_ledger_is_evidence_too() -> None:
    import installer.stale as stale

    with _sandbox() as (home, _backups):
        skill = home / "skills" / "from-ledger.md"
        skill.write_text("recorded by the ledger\n", encoding="utf-8")
        stale.write_ledger([(skill, skill, "claude/skills/from-ledger.md")], True)
        assert_true(str(skill) in stale.previously_installed(), "the ledger alone identifies an installed file")
        removable, _ = stale.stale_files(set())
        assert_true([p for p, _ in removable] == [skill], "and makes it removable once no longer shipped")


def _check_settings_drop_retired_hooks_and_refresh_statusline() -> None:
    import installer.stale as stale
    from installer.base import DIST_CONFIG
    from installer.settings import _drop_retired_hooks, _install_settings

    hooks = {
        "Stop": [
            {"hooks": [{"type": "command", "command": 'powershell -File "C:/Users/h/.claude/hooks/old-gate.ps1"'},
                       {"type": "command", "command": "python my_tool.py"}]},
            {"hooks": [{"type": "command", "command": 'bash "/home/h/.claude/hooks/mine.sh"'}]},
        ]
    }
    cleaned, dropped = _drop_retired_hooks(hooks, {"old-gate"})
    assert_true(
        dropped == 1 and cleaned["Stop"][0]["hooks"] == [{"type": "command", "command": "python my_tool.py"}]
        and cleaned["Stop"][1] == hooks["Stop"][1],
        f"only commands running a retired script go; the rest of the entry and the user's hooks stay: {cleaned}",
    )

    with _sandbox() as (home, backups):
        old_hook = home / "hooks" / "old-gate.ps1"
        old_hook.write_text("x", encoding="utf-8")
        _receipt(backups, "install_20250101_000000", [
            {"action": "create", "key": "claude/hooks/old-gate.ps1", "dest": str(old_hook), "post_sha256": _sha(old_hook)},
        ])
        assert_true(stale.retired_hook_stems() == {"old-gate"}, f"a recorded, unshipped hook is retired: {stale.retired_hook_stems()}")

        src = DIST_CONFIG / "claude" / "settings.template.json"
        template = json.loads(src.read_text(encoding="utf-8"))
        dest = home / "settings.json"
        current = json.loads(json.dumps(template))
        current["statusLine"] = {"type": "command", "command": 'powershell -File "C:/Users/old/.claude/hooks/workflow-statusline.ps1" -Legacy'}
        current["hooks"].setdefault("Stop", []).append(
            {"hooks": [{"type": "command", "command": 'powershell -NoProfile -File "C:/Users/h/.claude/hooks/old-gate.ps1"'}]}
        )
        dest.write_text(json.dumps(current), encoding="utf-8")
        plan = Plan()
        _install_settings(src, dest, plan, True, backups / "install_29990101_000001")
        written = json.loads(dest.read_text(encoding="utf-8"))
        from installer.settings import _render_hook_commands, _resolve_in_json

        expected_status = _render_hook_commands(_resolve_in_json(json.loads(src.read_text(encoding="utf-8")), None))["statusLine"]
        assert_true(written["statusLine"] == expected_status, f"a workflow statusLine is refreshed to the shipped command: {written['statusLine']}")
        assert_true("old-gate" not in json.dumps(written["hooks"]), "the hook entry for a retired script is dropped")

        their_same_name = {"type": "command", "command": "bash ~/bin/workflow-statusline.sh"}
        current = json.loads(dest.read_text(encoding="utf-8"))
        current["statusLine"] = their_same_name
        current["hooks"].setdefault("Stop", []).append({"hooks": [{"type": "command", "command": "bash ~/bin/old-gate.sh"}]})
        dest.write_text(json.dumps(current), encoding="utf-8")
        _install_settings(src, dest, Plan(), True, backups / "install_29990101_000003")
        after = json.loads(dest.read_text(encoding="utf-8"))
        assert_true(
            after["statusLine"] == their_same_name and "bash ~/bin/old-gate.sh" in json.dumps(after["hooks"]),
            "a script outside the installed hooks dir is the user's even under a shipped or retired name: "
            f"{after['statusLine']}",
        )

        user_status = {"type": "command", "command": "node ~/my-statusline.js"}
        current = json.loads(dest.read_text(encoding="utf-8"))
        current["statusLine"] = user_status
        dest.write_text(json.dumps(current), encoding="utf-8")
        _install_settings(src, dest, Plan(), True, backups / "install_29990101_000002")
        assert_true(json.loads(dest.read_text(encoding="utf-8"))["statusLine"] == user_status,
                    "a statusLine running the user's own script is theirs and is kept")


def _check_unstamped_dist_is_refused() -> None:
    from installer.check import _bundle_stale
    from installer.base import MANIFEST

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert_true(_bundle_stale(manifest) == [], "fixture assumption: the shipped bundle is stamped")
    tampered = {**manifest, "files": [f for f in manifest["files"] if f["path"] != "claude/skills/plan.md"]}
    assert_true(_bundle_stale(tampered) == ["claude/skills/plan.md"],
                "a dist file the manifest does not describe is stale, and --apply refuses on it")
    # The other direction: the manifest still vouches for a file dist/ no longer ships.
    # The stale walk only visits sources that exist, so this passed both checks before.
    ghost = {**manifest, "files": [*manifest["files"], {"path": "claude/skills/deleted.md", "sha256": "0" * 64}]}
    assert_true(_bundle_stale(ghost) == ["claude/skills/deleted.md"],
                "a manifest entry whose dist source was deleted is stale too")


def _check_upgrade_points_at_the_running_build() -> None:
    from config.settings import MAIN_PY
    from core.runtime.upgrade import resolve_agent_workflow_path

    root = Path(tempfile.mkdtemp(prefix="aw-resolve-"))
    try:
        old_clone = root / "old-clone" / "main.py"
        old_clone.parent.mkdir()
        old_clone.write_text("# an old build\n", encoding="utf-8")
        (root / ".workflow").mkdir()
        (root / ".workflow" / "config.json").write_text(
            json.dumps({"runtime": {"agent_workflow_path": str(old_clone)}}), encoding="utf-8"
        )
        resolved = resolve_agent_workflow_path(root)
        assert_true(
            resolved["source"] == "running_build" and Path(resolved["path"]) == MAIN_PY.resolve(),
            f"an old clone recorded in config.json must not outrank the build doing the upgrade: {resolved}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_installer_leaves_no_stale_files() -> None:
    _check_only_our_unedited_leftovers_go()
    _check_ledger_is_evidence_too()
    _check_settings_drop_retired_hooks_and_refresh_statusline()
    _check_unstamped_dist_is_refused()
    _check_upgrade_points_at_the_running_build()
