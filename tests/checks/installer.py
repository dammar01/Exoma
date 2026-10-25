"""Dedicated units for the installer's four concern modules.

Until now the installer was covered only where provider.py happened to pass through it,
which left its most consequential code — the atomic rollback, the drift detection, the
cross-platform hook rewrite — proven by nothing. These checks pin the behaviours a
release actually leans on: an install must be undoable, a check must notice drift, and
a POSIX install must not ship PowerShell commands.

Each check builds its own temp fixture and patches module globals (HOME, _MODE_FILE)
rather than the process environment, so nothing here can touch the real ~/.claude.
"""

import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from exoma.installer import rollback as rollback_mod
from exoma.installer import settings as settings_mod
from exoma.installer.base import (
    _apply_intent_mode,
    _file_sha256,
    _merge_managed,
    _read_text_lenient,
)
from exoma.installer.check import _settings_would_change
from exoma.installer.rollback import _run_rollback
from exoma.installer.base import Plan
from exoma.installer.settings import (
    _install_settings,
    _merge_hook_entries,
    _render_hook_commands,
    hook_bash_command,
)
from tests.checks.support import assert_true


def _test_installer_settings_are_additive() -> None:
    """A key the user already set survives the install. Driven through the real merge.

    The badge is the first thing this installer ships that a user plausibly already has —
    `statusLine` is an obvious name to have configured by hand — and silently replacing it
    would swap out something they look at on every prompt. `hooks` is exercised beside it
    because it takes a different path (entry-level merge) and must stay additive too.

    Written against `_install_settings` rather than against a restatement of its rule: a
    check that reimplements the logic proves the reimplementation.
    """
    root = Path(tempfile.mkdtemp(prefix="aw-inst-additive-"))
    try:
        theirs = {"type": "command", "command": "my-own-statusline --fancy"}
        their_hook = {
            "hooks": [{"type": "command", "command": "echo user-owned"}]
        }
        dest = root / "settings.json"
        dest.write_text(
            json.dumps(
                {
                    "statusLine": json.loads(json.dumps(theirs)),
                    "model": "their-model",
                    "hooks": {"SessionStart": [json.loads(json.dumps(their_hook))]},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        src = root / "template.json"
        src.write_text(
            json.dumps(
                {
                    "statusLine": {
                        "type": "command",
                        "command": (
                            "powershell -NoProfile -ExecutionPolicy Bypass -File "
                            '"/tmp/.claude/hooks/workflow-statusline.ps1"'
                        ),
                    },
                    "model": "shipped-model",
                    "brandNewKey": {"added": True},
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (
                                            "powershell -NoProfile -ExecutionPolicy "
                                            'Bypass -File "/tmp/.claude/hooks/'
                                            'session-bind.ps1"'
                                        ),
                                    }
                                ]
                            }
                        ]
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        plan = Plan()
        with contextlib.redirect_stdout(io.StringIO()):
            _install_settings(src, dest, plan, True, root / "backup")
        merged = json.loads(dest.read_text(encoding="utf-8"))

        assert_true(
            merged["statusLine"] == theirs,
            "the user's own statusLine must survive verbatim; the installer adds the key "
            f"only when it is missing: {merged['statusLine']}",
        )
        assert_true(
            merged["model"] == "their-model",
            "and every other value they set stays theirs too",
        )
        assert_true(
            merged.get("brandNewKey") == {"added": True},
            "a key they do NOT have must still arrive — additive means adding, not "
            "declining to touch the file",
        )
        commands = [
            hook["command"]
            for entry in merged["hooks"]["SessionStart"]
            for hook in entry.get("hooks", [])
        ]
        assert_true(
            "echo user-owned" in commands
            and any("session-bind" in c for c in commands),
            f"a foreign hook must survive alongside the shipped one: {commands}",
        )
        assert_true(
            any("statusLine" in w for w in plan.warnings),
            f"and the collision must be reported, not silently passed over: {plan.warnings}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_installer_text_merging() -> None:
    root = Path(tempfile.mkdtemp(prefix="aw-inst-base-"))
    try:
        # _read_text_lenient: every encoding an install has actually met in the field.
        bom = root / "bom.md"
        bom.write_bytes(b"\xef\xbb\xbfhello")
        assert_true(
            _read_text_lenient(bom) == "hello",
            "a UTF-8 BOM must be stripped, not surface as \\ufeff in the merged doc",
        )
        cp1252 = root / "cp1252.md"
        cp1252.write_bytes(b"caf\x97e")  # 0x97 = cp1252 em dash, invalid UTF-8 start
        assert_true(
            _read_text_lenient(cp1252) == "caf\u2014e",
            "a cp1252 em dash must decode as an em dash, not crash strict UTF-8",
        )
        stray = root / "stray.md"
        stray.write_bytes(b"\x81abc")  # undefined in cp1252; only latin-1 maps it
        assert_true(
            _read_text_lenient(stray) == "\x81abc",
            "a byte no preferred encoding accepts must fall back to latin-1, never raise",
        )
        crlf = root / "crlf.md"
        crlf.write_bytes(b"a\r\nb\rc")
        assert_true(
            _read_text_lenient(crlf) == "a\nb\nc",
            "line endings must be normalised, or every merge diff is whitespace noise",
        )

        # _apply_intent_mode: exactly one stanza survives, and no selector markers ship.
        both = (
            "<!-- AUTO-INTENT:START -->\nauto stanza\n<!-- AUTO-INTENT:END -->\n"
            "<!-- COMMAND-ONLY:START -->\ncommand stanza\n<!-- COMMAND-ONLY:END -->\n"
            "shared tail"
        )
        command_only = _apply_intent_mode(both, True)
        assert_true(
            "auto stanza" not in command_only and "command stanza" in command_only,
            "only_command must keep the COMMAND-ONLY stanza and drop AUTO-INTENT",
        )
        auto = _apply_intent_mode(both, False)
        assert_true(
            "auto stanza" in auto and "command stanza" not in auto,
            "auto-intent must keep the AUTO-INTENT stanza and drop COMMAND-ONLY",
        )
        assert_true(
            "<!--" not in command_only and "<!--" not in auto,
            "the surviving stanza's own markers are selector plumbing and must not ship",
        )
        assert_true(
            "shared tail" in command_only and "shared tail" in auto,
            "content outside both stanzas must survive either mode",
        )

        # _merge_managed: replace in place, append when absent, accept a bare incoming.
        managed = "<!-- M:START v2 -->\nnew body\n<!-- M:END -->"
        existing = f"user prose\n\n<!-- M:START v1 -->\nold body\n<!-- M:END -->\nmore prose"
        merged, how = _merge_managed(existing, managed, "M:START", "M:END")
        assert_true(
            how == "replaced managed block"
            and "new body" in merged
            and "old body" not in merged
            and "user prose" in merged
            and "more prose" in merged,
            "an existing managed block must be replaced without touching the user's prose",
        )
        appended, how = _merge_managed("user prose", managed, "M:START", "M:END")
        assert_true(
            how == "appended managed block" and appended.startswith("user prose\n\n"),
            "a file with no managed block must gain one after the user's content",
        )
        bare, _how = _merge_managed("", "  no markers here  ", "M:START", "M:END")
        assert_true(
            "no markers here" in bare,
            "incoming text without markers must be spliced as-is, not dropped",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_installer_settings_merge() -> None:
    shipped = {
        "hooks": [
            {
                "type": "command",
                "command": 'powershell -NoProfile -ExecutionPolicy Bypass -File '
                '"C:\\Users\\x\\.claude\\hooks\\session-bind.ps1"',
            }
        ]
    }
    shipped_v2 = json.loads(json.dumps(shipped))
    shipped_v2["hooks"][0]["command"] += " -Refreshed"
    foreign = {"hooks": [{"type": "command", "command": 'python "C:\\mine\\my-hook.py"'}]}
    new_shipped = {
        "hooks": [
            {
                "type": "command",
                "command": 'powershell -NoProfile -ExecutionPolicy Bypass -File '
                '"C:\\Users\\x\\.claude\\hooks\\intent-gate-set.ps1"',
            }
        ]
    }

    merged, updated = _merge_hook_entries(
        [foreign, shipped], [shipped_v2, new_shipped]
    )
    assert_true(
        foreign in merged,
        "a user hook that runs none of our scripts must never be modified",
    )
    assert_true(
        shipped_v2 in merged and shipped not in merged,
        "a shipped entry must be refreshed to the template's version, not duplicated",
    )
    assert_true(
        new_shipped in merged and updated == 2,
        "a shipped entry we don't yet have must be appended and counted",
    )

    # An entry mixing our hook with the user's own: ours refreshes, theirs survives.
    mixed = {
        "hooks": [
            dict(shipped["hooks"][0]),
            {"type": "command", "command": "echo user-owned"},
        ]
    }
    merged, _updated = _merge_hook_entries([mixed], [shipped_v2])
    survivors = [
        hook["command"]
        for entry in merged
        for hook in entry.get("hooks", [])
    ]
    assert_true(
        "echo user-owned" in survivors,
        "a user hook sharing an entry with ours must survive the refresh",
    )
    assert_true(
        shipped_v2["hooks"][0]["command"] in survivors
        and shipped["hooks"][0]["command"] not in survivors,
        "the shipped half of a mixed entry must still be refreshed",
    )

    shipped_cmd = {"type": "command", "command": '{{HOOK_BASH}} "C:\\Users\\x/.claude/hooks/session-bind.sh"'}
    template = {
        "statusLine": {"type": "command", "command": '{{HOOK_BASH}} "C:\\Users\\x/.claude/hooks/workflow-statusline.sh"'},
        "hooks": {"SessionStart": [{"hooks": [shipped_cmd]}, json.loads(json.dumps(foreign))]},
    }
    for bash in ("bash", '"C:/Program Files/Git/bin/bash.exe"'):
        rendered = _render_hook_commands(json.loads(json.dumps(template)), bash)
        assert_true(
            rendered["statusLine"]["command"] == f'{bash} "C:/Users/x/.claude/hooks/workflow-statusline.sh"',
            "statusLine sits BESIDE hooks, so a renderer that walks only `hooks` leaves the "
            "statusline naming no interpreter. Claude Code renders a broken statusline as an "
            f"empty one: got {rendered['statusLine']['command']!r}",
        )
        ours = rendered["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        theirs = rendered["hooks"]["SessionStart"][1]["hooks"][0]["command"]
        assert_true(
            ours == f'{bash} "C:/Users/x/.claude/hooks/session-bind.sh"',
            f"a shipped command gets its bash and forward slashes; got {ours!r}",
        )
        assert_true(
            theirs == foreign["hooks"][0]["command"],
            "a foreign hook must keep its command verbatim, backslashes included",
        )
    real_windows, real_find = settings_mod.osutil.IS_WINDOWS, settings_mod.osutil.find_bash
    try:
        settings_mod.osutil.IS_WINDOWS = False
        settings_mod.osutil.find_bash = lambda: "/usr/bin/bash"
        assert_true(hook_bash_command() == "bash", "POSIX names plain bash")
        settings_mod.osutil.find_bash = lambda: None
        assert_true(
            hook_bash_command() is None,
            "POSIX without bash on PATH has no command, like Windows without Git Bash; install refuses --apply",
        )
        settings_mod.osutil.IS_WINDOWS = True
        settings_mod.osutil.find_bash = lambda: "C:\\Program Files\\Git\\bin\\bash.exe"
        assert_true(
            hook_bash_command() == '"C:/Program Files/Git/bin/bash.exe"',
            f"Windows names Git Bash by absolute, quoted path, never a bare bash (WSL's): {hook_bash_command()!r}",
        )
        settings_mod.osutil.find_bash = lambda: None
        assert_true(hook_bash_command() is None, "Windows without Git Bash has no command; install refuses --apply")
    finally:
        settings_mod.osutil.IS_WINDOWS, settings_mod.osutil.find_bash = real_windows, real_find

    _check_hook_ownership_is_by_directory()


def _check_hook_ownership_is_by_directory() -> None:
    """A shipped stem is ours only inside the installed hooks dir.

    Ownership used to be the script's stem alone, so a user's own `~/tools/task-events.sh`
    was refreshed, relocated and stripped as if the workflow had written it.
    """
    from exoma.installer.settings import _hook_script_ids

    def ids(command: str) -> set[str]:
        return _hook_script_ids({"hooks": [{"type": "command", "command": command}]})

    original_home = settings_mod.HOME
    # Deliberately not home-shaped (an AGENT_HOME sandbox): only equality with HOME, or an
    # alias for it, can make these ours.
    settings_mod.HOME = Path("D:\\sandbox\\agent-home")
    try:
        ours = [
            'powershell -NoProfile -ExecutionPolicy Bypass -File "D:\\sandbox\\agent-home\\.claude\\hooks\\session-bind.ps1"',
            'bash "D:/sandbox/agent-home/.claude/hooks/session-bind.sh"',
            "bash 'd:/Sandbox/Agent-Home//.claude/hooks/session-bind.sh'",
            "bash ~/.claude/hooks/session-bind.sh",
            'bash "$HOME/.claude/hooks/session-bind.sh"',
            'bash "${HOME}/.claude/hooks/session-bind.sh"',
            'powershell -File "%USERPROFILE%\\.claude\\hooks\\session-bind.ps1"',
            'powershell -File "$env:USERPROFILE\\.claude\\hooks\\session-bind.ps1"',
            # Rendered by an older install, or synced from another machine: a user's home.
            'powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\Users\\x\\.claude\\hooks\\session-bind.ps1"',
            'bash "/home/old/.claude/hooks/session-bind.sh"',
            'bash "/Users/me/.claude/hooks/session-bind.sh"',
        ]
        for command in ours:
            assert_true(ids(command) == {"session-bind"}, f"a shipped form must stay ours: {command!r} -> {ids(command)}")
        assert_true(
            ids('powershell -NoProfile -ExecutionPolicy Bypass -File "C:\\Users\\x\\.claude\\hooks\\statusline.ps1"')
            == {"statusline"},
            "the statusLine an older release shipped is still recognised (as a retired stem)",
        )
        theirs = [
            'bash "/home/u/tools/task-events.sh"',
            "bash ~/tools/task-events.sh",
            'powershell -File "C:\\tools\\session-bind.ps1"',
            'bash "$CLAUDE_PROJECT_DIR/.claude/hooks/session-bind.sh"',
            'bash "/srv/app/.claude/hooks/session-bind.sh"',
            'bash "D:/sandbox/agent-home/.claude/hooks/extra/session-bind.sh"',
            # Their composition around our script: refreshing or stripping it takes their half.
            "bash ~/.claude/hooks/session-bind.sh && bash ~/tools/notify.sh",
            "my-tool --config session-bind.shell",
        ]
        for command in theirs:
            assert_true(not ids(command), f"a user's command must not read as ours: {command!r} -> {ids(command)}")

        # Through the merge: the user's same-stem hook stays verbatim, ours is added beside it.
        user_entry = {"hooks": [{"type": "command", "command": "bash ~/tools/session-bind.sh"}]}
        shipped = {"hooks": [{"type": "command", "command": 'bash "D:/sandbox/agent-home/.claude/hooks/session-bind.sh"'}]}
        merged, updated = _merge_hook_entries([json.loads(json.dumps(user_entry))], [shipped])
        assert_true(
            merged == [user_entry, shipped] and updated == 1,
            f"a user hook sharing a shipped stem is not refreshed into ours: {merged}",
        )
        relocated, removed = settings_mod._drop_relocated_hooks(
            {"PreToolUse": [json.loads(json.dumps(user_entry))]}, {"PostToolUse": [shipped]}
        )
        assert_true(
            removed == 0 and relocated == {"PreToolUse": [user_entry]},
            f"nor relocated off an event the template no longer uses: {relocated}",
        )
    finally:
        settings_mod.HOME = original_home


def _rollback_fixture(root: Path) -> tuple[Path, Path, Path]:
    """A fake HOME holding one receipted install: one restorable file, one created file."""
    home = root / "home"
    dest_root = root / "dest"
    backup_dir = home / ".claude" / "backups" / "install_20260101_000000"
    (backup_dir / "claude").mkdir(parents=True)
    dest_root.mkdir()

    restored_dest = dest_root / "CLAUDE.md"
    restored_dest.write_text("installed content", encoding="utf-8")
    backup_file = backup_dir / "claude" / "CLAUDE.md"
    backup_file.write_text("original content", encoding="utf-8")

    created_dest = dest_root / "created.md"
    created_dest.write_text("created by install", encoding="utf-8")

    receipt = {
        "schema_version": 2,
        "entries": [
            {
                "action": "replace",
                "key": "claude/CLAUDE.md",
                "dest": str(restored_dest),
                "backup": str(backup_file),
                "pre_sha256": _file_sha256(backup_file),
                "post_sha256": _file_sha256(restored_dest),
            },
            {
                "action": "create",
                "key": "claude/created.md",
                "dest": str(created_dest),
                "backup": None,
                "pre_sha256": None,
                "post_sha256": _file_sha256(created_dest),
            },
        ],
    }
    (backup_dir / "install_receipt.json").write_text(
        json.dumps(receipt, indent=2), encoding="utf-8"
    )
    return home, restored_dest, created_dest


def _run_quiet(which, apply) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = _run_rollback(which, apply)
    return code, out.getvalue()


def _test_installer_rollback_receipt() -> None:
    original_home = rollback_mod.HOME
    original_mode_file = rollback_mod._MODE_FILE
    root = Path(tempfile.mkdtemp(prefix="aw-inst-rb-"))
    try:
        # Dry run: report everything, write nothing.
        home, restored, created = _rollback_fixture(root / "dry")
        rollback_mod.HOME = home
        code, _output = _run_quiet(None, False)
        assert_true(
            code == 0
            and restored.read_text(encoding="utf-8") == "installed content"
            and created.exists(),
            "a dry-run rollback must report cleanly and change nothing",
        )

        # Apply: the backed-up file comes back, the created file goes away.
        code, _output = _run_quiet(None, True)
        assert_true(
            code == 0
            and restored.read_text(encoding="utf-8") == "original content"
            and not created.exists(),
            "an applied rollback must restore backups and delete created files",
        )

        # A destination edited since the install must abort the WHOLE rollback.
        home, restored, created = _rollback_fixture(root / "edited")
        rollback_mod.HOME = home
        restored.write_text("hand edited after install", encoding="utf-8")
        code, output = _run_quiet(None, True)
        assert_true(
            code == 2 and "ABORTED" in output,
            "an edited destination must refuse the rollback, not overwrite the edit",
        )
        assert_true(
            restored.read_text(encoding="utf-8") == "hand edited after install"
            and created.exists(),
            "a refused rollback must leave every file exactly as it found it",
        )

        # An unknown receipt schema is refused rather than replayed unverified.
        home, _restored, _created = _rollback_fixture(root / "schema")
        receipt_path = (
            home / ".claude" / "backups" / "install_20260101_000000" / "install_receipt.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["schema_version"] = 1
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        rollback_mod.HOME = home
        code, output = _run_quiet(None, True)
        assert_true(
            code == 1 and "unsupported receipt schema" in output,
            "an unsupported schema must be refused, never guessed at",
        )

        # A pre-receipt backup dir can only be restored by hand; say so.
        home, _restored, _created = _rollback_fixture(root / "noreceipt")
        (
            home / ".claude" / "backups" / "install_20260101_000000" / "install_receipt.json"
        ).unlink()
        rollback_mod.HOME = home
        code, output = _run_quiet(None, True)
        assert_true(
            code == 1 and "no install_receipt.json" in output,
            "a backup dir without a receipt must be refused with manual instructions",
        )

        # _stored_only_command: the persisted intent mode, with a safe default.
        mode_file = root / "mode.json"
        rollback_mod._MODE_FILE = mode_file
        assert_true(
            rollback_mod._stored_only_command() is False,
            "no stored mode must read as auto-intent, the shipped default",
        )
        mode_file.write_text(json.dumps({"only_command": True}), encoding="utf-8")
        assert_true(
            rollback_mod._stored_only_command() is True,
            "a stored only_command=true must be honoured by later upgrades",
        )
        mode_file.write_text("not json", encoding="utf-8")
        assert_true(
            rollback_mod._stored_only_command() is False,
            "a corrupt mode file must fall back to the default, not crash the install",
        )
    finally:
        rollback_mod.HOME = original_home
        rollback_mod._MODE_FILE = original_mode_file
        shutil.rmtree(root, ignore_errors=True)


def _test_installer_drift_check() -> None:
    root = Path(tempfile.mkdtemp(prefix="aw-inst-check-"))
    try:
        template = {
            "model": "workflow-default",
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'powershell -NoProfile -ExecutionPolicy Bypass '
                                '-File "C:\\Users\\x\\.claude\\hooks\\session-bind.ps1"',
                            }
                        ]
                    }
                ]
            },
        }
        src = root / "settings.template.json"
        src.write_text(json.dumps(template, indent=2), encoding="utf-8")

        missing_dest = root / "missing" / "settings.json"
        assert_true(
            _settings_would_change(src, missing_dest, False) is True,
            "a missing settings.json is drift — an install would create it",
        )

        # A dest already matching the template. This template carries no {{HOOK_BASH}}, so
        # the renderer leaves it as written on every OS.
        current_dest = root / "settings.json"
        current_dest.write_text(json.dumps(template, indent=2), encoding="utf-8")
        assert_true(
            _settings_would_change(src, current_dest, False) is False,
            "a settings.json already carrying the shipped keys and hooks is not drift",
        )

        broken_dest = root / "broken.json"
        broken_dest.write_text("{not json", encoding="utf-8")
        assert_true(
            _settings_would_change(src, broken_dest, False) is True,
            "an unparseable settings.json must count as drift, not as clean",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _seed_fixture(root: Path) -> Path:
    """A data dir with no seed yet, beside a copy of the shipped example."""
    from exoma.config.settings import PROVIDER_EXAMPLE_FILE

    (root / "config").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PROVIDER_EXAMPLE_FILE, root / "config" / "second_agent.example.json")
    return root / "config" / "second_agent.seed.json"


def _point_seed_at(install_mod, root: Path):
    """Point the installer's seed and example at `root`; returns the originals."""
    original = install_mod.SEED_DIR, install_mod.EXAMPLE_FILE
    install_mod.SEED_DIR = root / "config"
    install_mod.EXAMPLE_FILE = root / "config" / "second_agent.example.json"
    return original


@contextlib.contextmanager
def _seed_repo(stdin_text: str | None = None):
    """install's seed location pointed at a temp dir, with stdin optionally a fake tty.

    The globals are patched rather than EXOMA_HOME because `_seed_second_agent_config`
    reads them at call time, and a test that relied on the environment instead would seed
    the real data directory the first time that resolution changed.
    """
    import sys

    from exoma import install as install_mod

    root = Path(tempfile.mkdtemp()) / "repo"
    dest = _seed_fixture(root)
    original_root, original_stdin = _point_seed_at(install_mod, root), sys.stdin
    if stdin_text is not None:

        class _Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        sys.stdin = _Tty(stdin_text)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield install_mod, dest
    finally:
        install_mod.SEED_DIR, install_mod.EXAMPLE_FILE = original_root
        sys.stdin = original_stdin
        shutil.rmtree(root.parent, ignore_errors=True)


def _test_installer_seeds_second_agent_config() -> None:
    """The seed is rebuilt every run; the selection is kept, the rest follows the example."""
    with _seed_repo() as (install_mod, dest):
        plan = Plan()
        install_mod._seed_second_agent_config(plan, True, "codex", "gpt-5.6-sol")
        written = json.loads(dest.read_text(encoding="utf-8"))
        assert_true(
            written["provider"] == "codex"
            and written["default_model"] == "gpt-5.6-sol"
            and written["provider_command"] == "codex",
            "--provider/--model must land in the seeded config",
        )
        assert_true(
            written["routes"]["explore"]["model"] == "gpt-5.6-sol"
            and written["routes"]["sweep"]["model"] is None,
            "the pin follows the selectable routes and leaves sweep alone",
        )
        assert_true(
            "probe_timeout_seconds" in written,
            "the seed is built on the example, so its other keys are inherited",
        )

    with _seed_repo() as (install_mod, dest):
        # A previous seed from an older release: its selection survives, its other keys do not.
        dest.write_text(
            '{"provider": "codex", "default_model": "mine", "stale_knob": 1, '
            '"routes": {"plan": {"model": "planner"}}}',
            "utf-8",
        )
        install_mod._seed_second_agent_config(Plan(), True, None, None)
        written = json.loads(dest.read_text(encoding="utf-8"))
        assert_true(
            written["provider"] == "codex"
            and written["default_model"] == "mine"
            and written["routes"]["plan"]["model"] == "planner"
            and written["routes"]["explore"]["model"] == "mine",
            f"the previous seed's selection is carried onto the new example: {written}",
        )
        assert_true(
            "stale_knob" not in written and "probe_timeout_seconds" in written,
            "everything but the selection is rebuilt from the example, so nothing stale survives",
        )
        install_mod._seed_second_agent_config(Plan(), True, "codex", "gpt-5.6-sol")
        assert_true(
            json.loads(dest.read_text(encoding="utf-8"))["default_model"] == "gpt-5.6-sol",
            "flags override the previous selection",
        )

    with _seed_repo() as (install_mod, dest):
        # The retired tool-level file: renamed aside, never read into the seed.
        legacy = dest.parent / "second_agent.json"
        legacy.write_text('{"provider": "opencode", "default_model": "opencode/mimo-v2.5-free"}', "utf-8")
        plan = Plan()
        install_mod._seed_second_agent_config(plan, True, None, None)
        assert_true(
            not legacy.exists() and (dest.parent / "second_agent.json.retired").exists(),
            "the old config/second_agent.json is retired by --apply",
        )
        assert_true(
            "mimo" not in dest.read_text(encoding="utf-8"),
            "the retired selection must not be carried into the seed — it is the stale state",
        )
        assert_true(
            any("mimo" in warning and "retired" in warning for warning in plan.warnings),
            f"and the user is told what was not carried over: {plan.warnings}",
        )

    with _seed_repo() as (install_mod, dest):
        plan = Plan()
        install_mod._seed_second_agent_config(plan, False, "codex", None)
        assert_true(not dest.exists(), "a dry run plans the seed but writes nothing")


def _test_installer_seed_is_non_interactive_without_a_tty() -> None:
    """CI and tools/e2e run install.py with no stdin; a prompt there would hang the run."""
    import sys

    from exoma import install as install_mod

    root = Path(tempfile.mkdtemp()) / "repo"
    dest = _seed_fixture(root)
    original_root, original_stdin = _point_seed_at(install_mod, root), sys.stdin
    sys.stdin = io.StringIO("2\n1\n")  # readable, but isatty() is False
    try:
        plan = Plan()
        with contextlib.redirect_stdout(io.StringIO()):
            install_mod._seed_second_agent_config(plan, True, None, None)
        assert_true(
            dest.read_text(encoding="utf-8")
            == (root / "config" / "second_agent.example.json").read_text("utf-8"),
            "without a tty the example is copied verbatim, and stdin is not consumed",
        )
        # The second run reads that verbatim copy back as a previous selection and rebuilds
        # it through _second_agent_config. An example that has fallen behind the canonical
        # shape (a route or key the rebuild adds) turns every re-install into a `replace`.
        plan = Plan()
        with contextlib.redirect_stdout(io.StringIO()):
            install_mod._seed_second_agent_config(plan, True, None, None)
        verbs = [verb for verb, target, _ in plan.actions if Path(target) == dest]
        assert_true(
            verbs == ["same"],
            f"re-seeding from the verbatim example is a no-op, so re-install stays idempotent: {verbs}",
        )
    finally:
        install_mod.SEED_DIR, install_mod.EXAMPLE_FILE = original_root
        sys.stdin = original_stdin
        shutil.rmtree(root.parent, ignore_errors=True)


def _test_installer_seed_prompt_answers() -> None:
    """Every answer the picker accepts, including the two ways of declining."""
    with _seed_repo("2\n1\n") as (install_mod, dest):
        install_mod._seed_second_agent_config(Plan(), True, None, None)
        written = json.loads(dest.read_text(encoding="utf-8"))
        assert_true(
            written["provider"] == "codex" and written["default_model"] == "gpt-5.6-sol",
            "picking a provider then a model writes both",
        )

    with _seed_repo("3\n\n") as (install_mod, dest):
        install_mod._seed_second_agent_config(Plan(), True, None, None)
        written = json.loads(dest.read_text(encoding="utf-8"))
        assert_true(
            written["provider"] == "opencode" and written["default_model"] is None,
            "a blank model leaves the pin unset so the CLI keeps its own default",
        )

    with _seed_repo("3\nopencode/not-on-the-menu\n") as (install_mod, dest):
        plan = Plan()
        install_mod._seed_second_agent_config(plan, True, None, None)
        assert_true(
            json.loads(dest.read_text(encoding="utf-8"))["default_model"]
            == "opencode/not-on-the-menu",
            "the shortlist is a picker, not the set of models that exist",
        )
        assert_true(
            any("shortlist" in warning for warning in plan.warnings),
            "an unlisted pin is accepted with a warning, not silently",
        )

    for answer, label in (("\n", "a blank provider"), ("", "stdin ending mid-prompt")):
        with _seed_repo(answer) as (install_mod, dest):
            install_mod._seed_second_agent_config(Plan(), True, None, None)
            assert_true(
                dest.read_text(encoding="utf-8")
                == (dest.parent / "second_agent.example.json").read_text("utf-8"),
                f"{label} declines the question and falls back to the example",
            )


def _test_installer_seed_reports_unenforced_provider() -> None:
    """Choosing a provider with no read-only boundary has to say so at selection time."""
    from exoma.config.providers import bundled_providers, provider_requires_opt_in

    unenforced = [p for p in bundled_providers() if provider_requires_opt_in(p)]
    assert_true(
        bool(unenforced),
        "fixture assumption: at least one bundled provider requires opt-in",
    )
    with _seed_repo() as (install_mod, _dest):
        plan = Plan()
        install_mod._seed_second_agent_config(plan, True, unenforced[0], None)
        assert_true(
            any("OPT_IN" in warning for warning in plan.warnings),
            "the missing opt-in must be named while the choice is being made",
        )


def _test_installer_e2e_deps_are_opt_in() -> None:
    """--with-e2e installs the Playwright extra then its browser; without it, nothing runs.

    `subprocess` is replaced on the settings module only, so no pip or browser download can
    happen whatever the assertions decide.
    """
    shipped = settings_mod.E2E_REQUIREMENTS_FILE
    body = [line for line in shipped.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    assert_true(
        len(body) == 1 and re.match(r"^playwright==\d+\.\d+\.\d+$", body[0]),
        f"the shipped extra pins exactly one playwright version: {body}",
    )

    root = Path(tempfile.mkdtemp(prefix="aw-inst-e2e-"))
    calls: list[list[str]] = []
    failing: set[str] = set()

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        code = 1 if any(tool in argv for tool in failing) else 0
        return SimpleNamespace(returncode=code, stdout="", stderr="simulated failure" if code else "")

    original_root, original_subprocess = settings_mod.E2E_REQUIREMENTS_FILE, settings_mod.subprocess
    settings_mod.E2E_REQUIREMENTS_FILE = root / "requirements-e2e.txt"
    settings_mod.subprocess = SimpleNamespace(run=fake_run)
    requirements = root / "requirements-e2e.txt"
    pip = [sys.executable, "-m", "pip", "install", "-r", str(requirements)]
    browser = [sys.executable, "-m", "playwright", "install", "chromium"]
    try:
        requirements.write_text("playwright==1.60.0\n", encoding="utf-8")

        plan = Plan()
        settings_mod._install_deps(plan, True)
        assert_true(calls == [] and plan.actions == [], f"no flag, no extra: {calls} {plan.actions}")

        plan = Plan()
        settings_mod._install_deps(plan, False, True)
        targets = [target for verb, target, _ in plan.actions if verb == "run"]
        assert_true(calls == [], "a dry run executes nothing")
        assert_true(
            len(targets) == 2 and "requirements-e2e.txt" in targets[0] and targets[1].endswith("playwright install chromium"),
            f"a dry run plans pip, then the browser: {targets}",
        )

        plan = Plan()
        settings_mod._install_deps(plan, True, True)
        assert_true(calls == [pip, browser] and not plan.warnings, f"apply runs pip then the browser, same interpreter: {calls}")

        calls.clear()
        failing = {"pip"}
        plan = Plan()
        settings_mod._install_deps(plan, True, True)
        assert_true(calls == [pip], f"a failed pip install skips the browser download: {calls}")
        assert_true(any("download skipped" in w for w in plan.warnings), f"and says so: {plan.warnings}")

        calls.clear()
        failing = {"playwright"}
        plan = Plan()
        settings_mod._install_deps(plan, True, True)
        assert_true(calls == [pip, browser] and any("playwright install chromium failed" in w for w in plan.warnings), "a failed browser install is a warning")

        calls.clear()
        requirements.unlink()
        plan = Plan()
        settings_mod._install_deps(plan, True, True)
        assert_true(calls == [] and any("missing" in w for w in plan.warnings), "a checkout without the file installs nothing and warns")
    finally:
        settings_mod.E2E_REQUIREMENTS_FILE = original_root
        settings_mod.subprocess = original_subprocess
        shutil.rmtree(root, ignore_errors=True)


def _test_installer_check_reports_second_agent_default() -> None:
    """--check describes the seed without counting its absence as drift."""
    import exoma.installer.check as check_mod

    root = Path(tempfile.mkdtemp()) / "repo"
    dest = _seed_fixture(root)
    original_root = check_mod.SEED_DIR
    check_mod.SEED_DIR = root / "config"
    try:
        assert_true(
            check_mod._second_agent_state().startswith("NOT SET"),
            "a checkout that has never been seeded reports NOT SET",
        )
        dest.write_text('{"provider": "codex", "default_model": "gpt-5.5"}', "utf-8")
        assert_true(
            check_mod._second_agent_state() == "codex, model=gpt-5.5",
            "a seeded checkout reports the provider and model it selected",
        )
        dest.write_text("{not json", encoding="utf-8")
        assert_true(
            check_mod._second_agent_state().startswith("UNREADABLE"),
            "a config that no longer parses is the case this line exists to catch",
        )
    finally:
        check_mod.SEED_DIR = original_root
        shutil.rmtree(root.parent, ignore_errors=True)


def _test_provider_update() -> None:
    """--update-providers (DEC-057): to the stable release only, npm-global installs only,
    nothing changed while planning, each update verified, a failure named with its pin-back."""
    from exoma.core.provider import model_catalog, versions
    from exoma.installer import provider_update as pu

    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "npm"
        modules = prefix / "node_modules"
        (modules / "@openai" / "codex").mkdir(parents=True)
        (modules / "opencode-ai").mkdir()
        foreign = Path(tmp) / "brew" / "opencode"
        foreign.parent.mkdir()
        paths = {
            "codex": str(prefix / "codex.cmd"),
            "opencode": str(foreign),
            "npm": str(prefix / "npm.cmd"),
        }
        installed = {"codex": "codex-cli 0.150.0", "opencode": "1.10.0"}
        registry_has = {"@openai/codex@0.163.0", "opencode-ai@1.19.3"}
        calls: list[list[str]] = []
        forgotten: list[str] = []

        def run(args, timeout):
            calls.append(args)
            if args[1:] == ["root", "-g"]:
                return 0, f"{modules}\n", ""
            if args[1] == "view":
                return (0, args[2].rsplit("@", 1)[1] + "\n", "") if args[2] in registry_has else (1, "", "E404")
            if args[1] == "install" and "codex" in args[3]:
                installed["codex"] = "codex-cli 0.163.0"
                return 0, "", ""
            if args[1] == "install":
                return 1, "", "npm error EACCES: permission denied"
            raise AssertionError(f"unexpected call {args}")

        real = (pu.osutil.provider_callable, pu.osutil.resolve_exe, versions.read_version, versions.forget, model_catalog.forget, pu.osutil.IS_WINDOWS)
        try:
            # The Windows layout: <prefix>/node_modules, shims in <prefix>.
            pu.osutil.IS_WINDOWS = True
            pu.osutil.provider_callable = lambda name: (name in paths, paths.get(name, "missing"))
            pu.osutil.resolve_exe = lambda name: paths.get(name, name)
            fresh_reads: list[str] = []

            def read_version(command, fresh=False):
                if fresh:
                    fresh_reads.append(command)
                return installed.get(command)

            versions.read_version = read_version
            versions.forget = lambda command: forgotten.append(command)
            model_catalog.forget = lambda provider: forgotten.append(f"catalog:{provider}")

            def plan():
                return {e["provider"]: e for e in pu.plan_updates(run=run, getenv=lambda _k, d: d)}

            entries = plan()
            assert_true(entries["agy"]["action"] == "unsupported", f"agy declares no installer: {entries['agy']}")
            codex = entries["codex"]
            assert_true(
                codex["action"] == "update" and codex["argv"][1:] == ["install", "-g", "@openai/codex@0.163.0"],
                f"codex goes to its stable release, never latest: {codex}",
            )
            assert_true(codex["pin_back"] == "npm install -g @openai/codex@0.150.0", f"pin-back names the release before: {codex}")
            assert_true(
                entries["opencode"]["action"] == "skip" and "not npm's global" in entries["opencode"]["reason"],
                f"a CLI npm did not install is left to whoever did: {entries['opencode']}",
            )
            assert_true(not any(a[1] == "install" for a in calls), "planning installs nothing")
            assert_true(not forgotten, f"planning drops no cache, on disk or in memory: {forgotten}")
            assert_true(
                set(fresh_reads) == {"codex", "opencode"},
                f"planning reads each CLI on PATH fresh, past a day-old cached version: {fresh_reads}",
            )

            registry_has.discard("@openai/codex@0.163.0")
            assert_true(plan()["codex"]["action"] == "skip", "no stable release on the registry, no update")
            registry_has.add("@openai/codex@0.163.0")

            paths["opencode"] = str(prefix / "opencode.cmd")
            entries = plan()
            assert_true(entries["opencode"]["action"] == "update", f"an npm-global opencode is updated: {entries['opencode']}")
            forgotten.clear()
            done = {e["provider"]: e for e in pu.apply_updates(list(entries.values()), run=run)}
            assert_true(done["codex"]["result"] == "updated" and done["codex"]["after"] == "0.163.0", f"verified by --version: {done['codex']}")
            assert_true(
                done["opencode"]["result"] == "failed" and "EACCES" in done["opencode"]["error"]
                and done["opencode"]["pin_back"] == "npm install -g opencode-ai@1.10.0",
                f"a failed update is named with npm's error and its pin-back, the other still ran: {done['opencode']}",
            )
            assert_true(
                {"codex", "catalog:codex", "opencode", "catalog:opencode"} <= set(forgotten),
                f"each update drops the cached version and model listing: {forgotten}",
            )
            assert_true("result" not in done["agy"], "an unsupported provider is never run")
            assert_true(plan()["codex"]["action"] == "current", "a CLI on its stable release is current")

            journal = pu.write_journal(list(done.values()), Path(tmp) / "backup")
            data = json.loads(journal.read_text(encoding="utf-8"))
            assert_true(len(data["providers"]) == 3, f"the journal holds every provider: {data}")

            # POSIX: <prefix>/lib/node_modules, shims in <prefix>/bin; <prefix>/lib is no bin.
            pu.osutil.IS_WINDOWS = False
            modules = prefix / "lib" / "node_modules"
            (modules / "@openai" / "codex").mkdir(parents=True)
            (modules / "opencode-ai").mkdir()
            (prefix / "bin").mkdir()
            paths["codex"] = str(prefix / "bin" / "codex")
            paths["opencode"] = str(prefix / "lib" / "opencode")
            installed.update(codex="codex-cli 0.150.0", opencode="1.10.0")
            entries = plan()
            assert_true(entries["codex"]["action"] == "update", f"a shim in <prefix>/bin is npm's: {entries['codex']}")
            assert_true(
                entries["opencode"]["action"] == "skip",
                f"a CLI in <prefix>/lib is not npm's global bin: {entries['opencode']}",
            )
        finally:
            pu.osutil.provider_callable, pu.osutil.resolve_exe, versions.read_version, versions.forget, model_catalog.forget, pu.osutil.IS_WINDOWS = real
