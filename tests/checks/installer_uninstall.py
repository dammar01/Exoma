"""The install stays additive at both ends: written as one block, removed as one block.

A first install used to write the whole shipped CLAUDE.md, header included, and nothing
could later tell that header from the user's own text. Uninstall had no entry point at all.
These checks pin the block-only first write, the uninstall cut that keeps the user's text
around it, the settings strip that keeps the user's hooks, and the install-time drop of a
shipped hook from an event the release no longer registers it on.
"""

import json
import re
import shutil
import tempfile
from pathlib import Path

import contextlib
import io

from exoma.installer import rollback as rollback_mod
from exoma.installer import uninstall as uninstall_mod
from exoma.installer.base import (
    ManagedBlockError,
    Plan,
    _begin_receipt,
    _file_sha256,
    _flush_receipt,
    _install_text,
    _merge_managed,
    _reset_receipt,
)
from exoma.installer.settings import _drop_relocated_hooks
from exoma.installer.uninstall import _remove_receipted, _strip_settings, _uninstall_text, _without_block
from tests.checks.support import assert_true

_START = "WORKFLOW-MAIN-AGENT:START"
_END = "WORKFLOW-MAIN-AGENT:END"
_BLOCK = f"<!-- {_START} — v9, do not edit manually -->\nworkflow rules\n<!-- {_END} -->"


def _cmd(stem: str) -> dict:
    return {"type": "command", "command": f'powershell -File "C:\\Users\\h\\.claude\\hooks\\{stem}.ps1"'}


def _elsewhere(stem: str) -> dict:
    """The user's own script that happens to share a shipped stem."""
    return {"type": "command", "command": f'bash "$HOME/tools/{stem}.sh"'}


def _test_installer_uninstall() -> None:
    root = Path(tempfile.mkdtemp(prefix="installer-uninstall-"))
    try:
        # --- a first install writes the managed block alone --------------------------------
        src = root / "CLAUDE.md"
        src.write_text(f"# Shipped header outside the markers\n\n{_BLOCK}\n", encoding="utf-8")
        dest = root / "home" / "CLAUDE.md"
        _reset_receipt()
        plan = Plan()
        _install_text(src, dest, "claude/CLAUDE.md", plan, True, root / "backup", None)
        written = dest.read_text(encoding="utf-8")
        assert_true(
            "Shipped header" not in written and _START in written and _END in written,
            f"a first install writes only the managed block: {written!r}",
        )
        assert_true(
            plan.actions and plan.actions[0][0] == "create",
            f"and reports it as created: {plan.actions}",
        )
        _reset_receipt()

        # --- uninstall cuts the block and keeps the user's text around it --------------------
        mine = f"# My rules\nalways answer in English\n\n{_BLOCK}\n\n## More of mine\nno emoji\n"
        rest = _without_block(mine, _START, _END)
        assert_true(
            rest is not None
            and "always answer in English" in rest
            and "no emoji" in rest
            and _START not in rest
            and "workflow rules" not in rest,
            f"the user's text above and below the block survives: {rest!r}",
        )
        assert_true(_without_block(f"{_BLOCK}\n", _START, _END) == "", "a file holding only the block is left empty")
        # Only the seam where the block was cut is normalised; blank runs elsewhere are the user's.
        spaced = f"# My rules\n\n\n\nthree blank lines above\n\n{_BLOCK}\n\n## More\n\n\n\nand here\n\n\n"
        assert_true(
            _without_block(spaced, _START, _END)
            == "# My rules\n\n\n\nthree blank lines above\n\n## More\n\n\n\nand here\n\n\n",
            f"blank runs away from the block stay byte-identical: {_without_block(spaced, _START, _END)!r}",
        )
        assert_true(
            _without_block(f"{_BLOCK}\n\n# mine\n\n\n\nx\n", _START, _END) == "# mine\n\n\n\nx\n",
            "a block at the top takes only its own separator",
        )
        assert_true(
            _without_block(f"# a\n\n\n\nb\n\n\n{_BLOCK}\n", _START, _END) == "# a\n\n\n\nb\n",
            "a block at the end takes only its own separator",
        )
        assert_true(_without_block("# no block here\n", _START, _END) is None, "a file without the block is not ours to touch")

        # --- settings: workflow hooks and statusLine go, the user's stay ---------------------
        settings = {
            "model": "the-users-choice",
            "statusLine": {"type": "command", "command": _cmd("workflow-statusline")["command"]},
            "hooks": {
                "SessionStart": [{"matcher": "startup", "hooks": [_cmd("session-bind"), _cmd("my-own-hook")]}],
                "Stop": [{"hooks": [_cmd("graph-refresh")]}],
                "PostToolUse": [{"matcher": "Edit", "hooks": [_elsewhere("task-events")]}],
            },
        }
        stripped, changes = _strip_settings(
            settings, {"session-bind", "graph-refresh", "workflow-statusline", "task-events"}
        )
        assert_true(
            stripped.get("model") == "the-users-choice" and "statusLine" not in stripped,
            f"keys the user owns stay; the workflow statusLine goes: {stripped}",
        )
        assert_true(
            stripped["hooks"] == {
                "SessionStart": [{"matcher": "startup", "hooks": [_cmd("my-own-hook")]}],
                "PostToolUse": [{"matcher": "Edit", "hooks": [_elsewhere("task-events")]}],
            },
            "only the user's hooks survive, in their entries, a shipped stem outside the hooks "
            f"dir included: {stripped.get('hooks')}",
        )
        their_status = {"statusLine": {"type": "command", "command": "bash ~/bin/workflow-statusline.sh"}}
        kept, status_changes = _strip_settings(their_status, {"workflow-statusline"})
        assert_true(
            kept == their_status and not status_changes,
            f"a statusLine running the user's own same-named script stays: {kept}",
        )
        assert_true(len(changes) == 3, f"each removal is named: {changes}")

        # --- install: a shipped script leaves the events the template moved it off -----------
        current = {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [_cmd("task-events")]},
                {"matcher": "Bash", "hooks": [_cmd("my-own-hook")]},
            ],
            "PostToolUse": [{"matcher": "Edit", "hooks": [_cmd("task-events")]}],
            "Stop": [{"hooks": [_elsewhere("task-events")]}],
        }
        template = {"PostToolUse": [{"matcher": "Edit", "hooks": [_cmd("task-events")]}]}
        moved, removed = _drop_relocated_hooks(current, template)
        assert_true(
            removed == 1
            and moved["PreToolUse"] == [{"matcher": "Bash", "hooks": [_cmd("my-own-hook")]}]
            and moved["PostToolUse"] == current["PostToolUse"]
            and moved["Stop"] == current["Stop"],
            "task-events leaves PreToolUse; the user's hooks (their own task-events.sh included) "
            f"and the new registration stay: {moved}",
        )
        # --- bookkeeping the uninstall removes comes back with --rollback -----------------
        mode = root / "home" / ".workflow-install-mode.json"
        mode.write_text('{"only_command": true}\n', encoding="utf-8")
        fake_home = root / "fake-home"
        backup_dir = fake_home / ".claude" / "backups" / "install_uninstall"
        _reset_receipt()
        _begin_receipt(backup_dir / "install_receipt.json", {"uninstall": True})
        _remove_receipted(mode, "claude/.workflow-install-mode.json", Plan(), True, backup_dir, "bookkeeping")
        _flush_receipt(complete=True)
        _reset_receipt()
        assert_true(not mode.exists(), "uninstall removes the mode file")
        original = rollback_mod.HOME
        rollback_mod.HOME = fake_home
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = rollback_mod._run_rollback(None, True)
        finally:
            rollback_mod.HOME = original
        assert_true(
            code == 0 and mode.read_text(encoding="utf-8") == '{"only_command": true}\n',
            f"--rollback restores the intent mode an uninstall removed: code={code}",
        )

        _check_unpaired_markers(root)
        _check_settings_before_hooks(root)
    finally:
        _reset_receipt()
        shutil.rmtree(root, ignore_errors=True)


def _check_unpaired_markers(root: Path) -> None:
    """A START without its END (or the reverse) is refused, never answered with a 2nd block."""
    src = root / "markers-src.md"
    src.write_text(f"{_BLOCK}\n", encoding="utf-8")
    broken = {
        "start only": f"# mine\n<!-- {_START} — v1 -->\nold rules\n",
        "end only": f"# mine\nold rules\n<!-- {_END} -->\n",
        "end first": f"<!-- {_END} -->\n# mine\n<!-- {_START} — v1 -->\n",
    }
    for name, text in broken.items():
        dest = root / f"markers-{name.replace(' ', '-')}.md"
        dest.write_text(text, encoding="utf-8")
        _reset_receipt()
        plan = Plan()
        _install_text(src, dest, "claude/CLAUDE.md", plan, True, root / "backup-markers", None)
        assert_true(
            dest.read_text(encoding="utf-8") == text,
            f"[{name}] a file with unpaired markers is left untouched: {dest.read_text(encoding='utf-8')!r}",
        )
        assert_true(
            [verb for verb, _t, _d in plan.actions] == ["refused"]
            and any(str(dest) in w and _START in w and _END in w for w in plan.warnings),
            f"[{name}] the refusal names the file and both markers: {plan.actions} {plan.warnings}",
        )
        _reset_receipt()
        try:
            _merge_managed(text, _BLOCK, _START, _END)
        except ManagedBlockError:
            pass
        else:
            raise AssertionError(f"[{name}] _merge_managed must raise on unpaired markers")
        # Uninstall does not guess where a broken block ends either.
        plan = Plan()
        _uninstall_text(dest, "claude/CLAUDE.md", plan, True, root / "backup-markers")
        assert_true(
            dest.read_text(encoding="utf-8") == text and plan.warnings,
            f"[{name}] uninstall leaves a file with unpaired markers as is: {plan.warnings}",
        )
    # A well-formed block still splices in place.
    merged, how = _merge_managed(f"# mine\n\n{_BLOCK}\n", _BLOCK.replace("workflow rules", "new"), _START, _END)
    assert_true(how == "replaced managed block" and "new" in merged, f"paired markers still merge: {how}")


def _check_settings_before_hooks(root: Path) -> None:
    """settings.json is cleaned before the hook scripts go, read leniently, and when it cannot
    be read the scripts stay (it still runs them)."""
    home = root / "uninstall-home"
    hooks_dir = home / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    hook = hooks_dir / "session-bind.ps1"
    hook.write_text("# hook\n", encoding="utf-8")
    settings = home / ".claude" / "settings.json"
    body = {
        "model": "mine",
        "hooks": {"SessionStart": [{"hooks": [_cmd("session-bind"), _cmd("my-own-hook"), _elsewhere("session-bind")]}]},
    }
    saved = {
        name: getattr(uninstall_mod, name)
        for name in ("HOME", "_targets", "previously_installed", "retired_hook_stems", "agy_hooks_dest")
    }
    uninstall_mod.HOME = home
    agy_hooks = home / ".gemini" / "config" / "hooks.json"
    uninstall_mod.agy_hooks_dest = lambda: agy_hooks
    uninstall_mod._targets = lambda: [(hook, hook, "claude/hooks/session-bind.ps1")]
    uninstall_mod.previously_installed = lambda: {
        str(hook): {"key": "claude/hooks/session-bind.ps1", "hashes": {_file_sha256(hook)}}
    }
    uninstall_mod.retired_hook_stems = lambda: set()
    try:
        # A BOM (Notepad, PowerShell 5.1 Out-File) is still JSON: the workflow hook goes.
        settings.write_bytes(b"\xef\xbb\xbf" + json.dumps(body).encode("utf-8"))
        _reset_receipt()
        _begin_receipt(root / "backup-settings" / "install_receipt.json", {"uninstall": True})
        plan = Plan()
        clean = uninstall_mod._uninstall_settings(plan, True, root / "backup-settings")
        after = json.loads(settings.read_text(encoding="utf-8"))
        assert_true(
            clean and after["model"] == "mine"
            and after["hooks"] == {"SessionStart": [{"hooks": [_cmd("my-own-hook"), _elsewhere("session-bind")]}]},
            f"a BOM-prefixed settings.json is cleaned, the user's keys kept: {after} {plan.warnings}",
        )
        _reset_receipt()

        # Unreadable settings: reported, and the hook scripts it still runs are kept.
        settings.write_text('{"hooks": ', encoding="utf-8")
        calls: list[str] = []
        real_settings, real_files = uninstall_mod._uninstall_settings, uninstall_mod._uninstall_files

        def spy_settings(*args):
            calls.append("settings")
            return real_settings(*args)

        def spy_files(*args, **kwargs):
            calls.append("files")
            return real_files(*args, **kwargs)

        uninstall_mod._uninstall_settings, uninstall_mod._uninstall_files = spy_settings, spy_files
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                uninstall_mod.run_uninstall(False)
        finally:
            uninstall_mod._uninstall_settings, uninstall_mod._uninstall_files = real_settings, real_files
        report = out.getvalue()
        assert_true(calls == ["settings", "files"], f"settings are cleaned before files are removed: {calls}")
        assert_true(
            hook.exists() and "kept 1 hook script" in report and "not readable JSON" in report
            and not re.search(rf"^\s*remove\s+{re.escape(str(hook))}\s*(—|$)", report, re.M),
            f"an unreadable settings.json keeps the hook scripts and says so:\n{report}",
        )

        # Readable again: the same dry run would remove the script.
        settings.write_text(json.dumps(body), encoding="utf-8")
        plan = Plan()
        uninstall_mod._uninstall_files(plan, False, root / "backup-settings", keep_hooks=False)
        assert_true(
            [(verb, target) for verb, target, _d in plan.actions if verb == "remove"] == [("remove", str(hook))],
            f"with settings clean the hook script is removed: {plan.actions}",
        )

        # Settings clean but Agy's hooks.json unreadable: Agy still runs agy-host.py from the
        # same directory, so the scripts stay here too.
        agy_hooks.parent.mkdir(parents=True)
        agy_hooks.write_text("{not json", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            uninstall_mod.run_uninstall(False)
        report = out.getvalue()
        assert_true(
            hook.exists() and "kept 1 hook script" in report and "remove the `exoma` hook by hand" in report
            and agy_hooks.read_text(encoding="utf-8") == "{not json",
            f"an unreadable Agy hooks.json keeps the hook scripts and says so:\n{report}",
        )
    finally:
        for name, value in saved.items():
            setattr(uninstall_mod, name, value)
        _reset_receipt()
