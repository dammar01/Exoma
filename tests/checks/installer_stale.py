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

from exoma.installer.base import _RECEIPT, Plan
from tests.checks.support import assert_true


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextlib.contextmanager
def _sandbox():
    """A fake ~/.claude with its install history, wired into the installer modules."""
    import exoma.installer.rollback as rollback
    import exoma.installer.stale as stale

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
    import exoma.installer.stale as stale

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
            (install_dir / "stale" / "hosts" / "claude" / "skills" / "retired.md").read_text(encoding="utf-8") == "an old skill\n",
            "every removal is backed up first",
        )

        # --rollback restores a removed file from its receipt.
        from exoma.installer.rollback import _run_rollback

        (install_dir / "install_receipt.json").write_text(
            json.dumps({"schema_version": 2, "entries": list(_RECEIPT)}), encoding="utf-8"
        )
        with contextlib.redirect_stdout(io.StringIO()):
            code = _run_rollback(install_dir.name, True)
        assert_true(code == 0 and old.read_text(encoding="utf-8") == "an old skill\n" and hook.exists(),
                    f"rollback puts removed leftovers back: code={code}")


def _check_ledger_is_evidence_too() -> None:
    import exoma.installer.stale as stale

    with _sandbox() as (home, _backups):
        skill = home / "skills" / "from-ledger.md"
        skill.write_text("recorded by the ledger\n", encoding="utf-8")
        stale.write_ledger([(skill, skill, "hosts/claude/skills/from-ledger.md")], True)
        assert_true(str(skill) in stale.previously_installed(), "the ledger alone identifies an installed file")
        removable, _ = stale.stale_files(set())
        assert_true([p for p, _ in removable] == [skill], "and makes it removable once no longer shipped")


def _check_legacy_opencode_agents_migrate() -> None:
    """1.0.0 renamed the OpenCode agents wf-* to exm-*: an older install's wf-* files go,
    recorded under the old provider key, unless the user edited them."""
    import exoma.installer.stale as stale

    with _sandbox() as (home, backups):
        agents = home.parent / ".config" / "opencode" / "agents"
        agents.mkdir(parents=True)
        old = agents / "wf-map.md"
        old.write_text("an old agent\n", encoding="utf-8")
        edited = agents / "wf-trace.md"
        edited.write_text("as installed\n", encoding="utf-8")
        edited_sha = _sha(edited)
        edited.write_text("as installed, then changed by the user\n", encoding="utf-8")
        current = agents / "exm-map.md"
        current.write_text("the renamed agent\n", encoding="utf-8")
        # The ledger an older release wrote: provider keys before the providers/ layout.
        stale.LEDGER.write_text(json.dumps({"schema": 1, "files": {
            str(old): {"key": "opencode/agents/wf-map.md", "sha256": _sha(old)},
            str(edited): {"key": "opencode/agents/wf-trace.md", "sha256": edited_sha},
        }}), encoding="utf-8")

        known = stale.previously_installed()
        assert_true(
            known.get(str(old), {}).get("key") == "providers/opencode/agents/wf-map.md",
            f"a legacy OpenCode agent key is read in the current layout: {known}",
        )

        plan = Plan()
        install_dir = backups / "install_29990101_000000"
        stale.prune({str(current)}, plan, True, install_dir)
        assert_true(not old.exists(), "an unedited legacy wf-* agent is removed")
        assert_true(edited.exists() and current.exists(), "the edited wf-* agent and the shipped exm-* agent stay")
        assert_true(any("wf-trace.md" in w and "kept" in w for w in plan.warnings),
                    f"the kept wf-* agent is named: {plan.warnings}")
        assert_true(
            (install_dir / "stale" / "providers" / "opencode" / "agents" / "wf-map.md").read_text(encoding="utf-8")
            == "an old agent\n",
            "the removed wf-* agent is backed up first",
        )


def _check_settings_drop_retired_hooks_and_refresh_statusline() -> None:
    import exoma.installer.stale as stale
    from exoma.installer.base import DIST_CONFIG
    from exoma.installer.settings import _drop_retired_hooks, _install_settings

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

        src = DIST_CONFIG / "hosts" / "claude" / "settings.template.json"
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
        from exoma.installer.settings import _render_hook_commands, _resolve_in_json

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
    from exoma.installer.check import _bundle_stale
    from exoma.installer.base import MANIFEST

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert_true(_bundle_stale(manifest) == [], "fixture assumption: the shipped bundle is stamped")
    tampered = {**manifest, "files": [f for f in manifest["files"] if f["path"] != "hosts/claude/skills/plan.md"]}
    assert_true(_bundle_stale(tampered) == ["hosts/claude/skills/plan.md"],
                "a dist file the manifest does not describe is stale, and --apply refuses on it")
    # The other direction: the manifest still vouches for a file dist/ no longer ships.
    # The stale walk only visits sources that exist, so this passed both checks before.
    ghost = {**manifest, "files": [*manifest["files"], {"path": "hosts/claude/skills/deleted.md", "sha256": "0" * 64}]}
    assert_true(_bundle_stale(ghost) == ["hosts/claude/skills/deleted.md"],
                "a manifest entry whose dist source was deleted is stale too")
    _check_policy_template_is_stamped(manifest)


def _check_policy_template_is_stamped(manifest: dict) -> None:
    """An Auto-review template edited without regenerating the manifest fails both checks."""
    import contextlib
    import io
    from unittest.mock import patch

    from exoma.installer import base, check, codex_policy, rollback

    key = "hosts/codex/auto-review-policy.md"
    assert_true(any(entry["path"] == key for entry in manifest["files"]), "fixture: the template is stamped")
    edited = {**manifest, "files": [
        {**entry, "sha256": "0" * 64} if entry["path"] == key else entry for entry in manifest["files"]
    ]}
    dropped = {**manifest, "files": [entry for entry in manifest["files"] if entry["path"] != key]}
    with tempfile.TemporaryDirectory(prefix="aw-policy-stale-") as tmp:
        home = Path(tmp)
        for mode in ({"hosts": ["codex"]},
                     {"hosts": ["codex"], "codex_auto_review_context": False},
                     {"hosts": ["codex"], "codex_auto_review_context_opt_out": True}):
            for tampered in (edited, dropped):
                assert_true(key in check._bundle_stale(tampered), "install refuses the unstamped template")
                output = io.StringIO()
                with patch.object(base, "HOME", home), patch.object(check, "HOME", home), \
                        patch.object(check, "_installed_hosts", return_value=("codex",)), \
                        patch.object(rollback, "_stored_mode", return_value=mode), \
                        patch.object(codex_policy, "cli_supported", return_value=True), \
                        patch.object(codex_policy, "location_supported", return_value=True), \
                        contextlib.redirect_stdout(output):
                    code = check._run_check(tampered)
                assert_true(code != 0 and f"    - {key}" in output.getvalue(),
                            f"--check reports the unstamped template (mode={mode}): "
                            f"{output.getvalue()[-600:]}")


def _check_upgrade_points_at_the_running_build() -> None:
    from exoma.config.settings import PACKAGE_PARENT
    from exoma.core.runtime.upgrade import resolve_runtime

    root = Path(tempfile.mkdtemp(prefix="aw-resolve-"))
    try:
        old_clone = root / "old-clone" / "main.py"
        old_clone.parent.mkdir()
        old_clone.write_text("# an old build\n", encoding="utf-8")
        (root / ".workflow").mkdir()
        (root / ".workflow" / "config.json").write_text(
            json.dumps({"runtime": {"agent_workflow_path": str(old_clone)}}), encoding="utf-8"
        )
        resolved = resolve_runtime(root)
        assert_true(
            resolved["source"] == "running_build"
            and Path(resolved["package_root"]) == PACKAGE_PARENT
            and resolved["legacy"]["workflow_config"] == {"agent_workflow_path": str(old_clone)},
            f"an old clone recorded in config.json must not outrank the build doing the upgrade: {resolved}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_nested_reference_stale_dir_cleanup() -> None:
    """Pruning a retired nested skill reference removes its empty references/ and skill dirs."""
    import exoma.installer.stale as stale

    with _sandbox() as (home, backups):
        claude_ref = home / "skills" / "verify-browser" / "old-ref.md"
        claude_ref.parent.mkdir(parents=True)
        claude_ref.write_text("# old claude ref\n", encoding="utf-8")
        codex_ref = home.parent / ".codex" / "skills" / "exoma-verify-browser" / "references" / "old-ref.md"
        codex_ref.parent.mkdir(parents=True)
        codex_ref.write_text("# old codex ref\n", encoding="utf-8")
        _receipt(backups, "install_20250101_000001", [
            {"action": "create", "key": "hosts/claude/skills/verify-browser/old-ref.md",
             "dest": str(claude_ref), "post_sha256": _sha(claude_ref)},
            {"action": "create", "key": "hosts/codex/skills/exoma-verify-browser/references/old-ref.md",
             "dest": str(codex_ref), "post_sha256": _sha(codex_ref)},
        ])
        plan = Plan()
        stale.prune(set(), plan, True, backups / "install_29990101_000001")
        assert_true(not claude_ref.exists() and not claude_ref.parent.exists(),
                    "removing retired nested Claude reference removes empty skill subdir")
        assert_true(not codex_ref.exists() and not codex_ref.parent.exists() and not codex_ref.parent.parent.exists(),
                    "removing retired nested Codex reference removes empty references/ and skill dirs")


def _check_clean_legacy_exact_match() -> None:
    """`--clean-legacy` previews by default, removes ONLY exact historical headers with --apply,
    preserves user bytes verbatim, is idempotent, and restores cleanly via --rollback."""
    import exoma.installer.base as base
    import exoma.installer.rollback as rollback

    with _sandbox() as (home, backups):
        saved_base_home = base.HOME
        base.HOME = home.parent
        claude_md = home / "CLAUDE.md"
        user_prefix = b"User note before\r\nwith CRLF and utf8: caf\xc3\xa9\r\n\r\n"
        exact_header = (
            b"# Claude Code \xe2\x80\x94 Personal Global Config (v3.8.0)\r\n"
            b"# Skills: ~/.claude/skills/   Memory: ~/.claude/memory/\r\n\r\n"
        )
        managed = (
            b"<!-- WORKFLOW-MAIN-AGENT:START \xe2\x80\x94 v1.2.1, do not edit manually -->\r\n"
            b"body\r\n"
            b"<!-- WORKFLOW-MAIN-AGENT:END -->\r\n"
        )
        user_suffix = b"\r\nUser note after\r\n"
        original = user_prefix + exact_header + managed + user_suffix
        claude_md.write_bytes(original)
        try:
            # Preview does not modify the file.
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = base.run_clean_legacy(apply=False)
            assert_true(code == 0 and claude_md.read_bytes() == original and "Preview only" in out.getvalue(),
                        f"clean-legacy preview leaves file untouched: code={code} out={out.getvalue()}")

            # Apply removes ONLY exact header bytes and records a finalized rollbackable receipt.
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = base.run_clean_legacy(apply=True)
            expected_after = user_prefix + managed + user_suffix
            assert_true(code == 0 and claude_md.read_bytes() == expected_after,
                        f"clean-legacy --apply preserves user bytes verbatim: code={code}")
            receipt_dirs = [p for p in backups.iterdir() if p.is_dir()]
            assert_true(len(receipt_dirs) == 1, f"clean-legacy writes one backup receipt dir: {receipt_dirs}")
            receipt_data = json.loads((receipt_dirs[0] / "install_receipt.json").read_text(encoding="utf-8"))
            assert_true(receipt_data.get("complete") is True,
                        f"successful clean-legacy finalizes receipt complete=True: {receipt_data}")

            # Rerun is idempotent.
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = base.run_clean_legacy(apply=True)
            assert_true(code == 0 and "clean" in out.getvalue() and claude_md.read_bytes() == expected_after,
                        f"clean-legacy rerun is clean: {out.getvalue()}")

            # Rollback restores original bytes verbatim.
            with contextlib.redirect_stdout(io.StringIO()):
                rb_code = rollback._run_rollback(receipt_dirs[0].name, True)
            assert_true(rb_code == 0 and claude_md.read_bytes() == original,
                        f"rollback restores original CLAUDE.md bytes verbatim: code={rb_code}")

            # Negative matcher probes: post-marker fenced example, bogus marker, extra blank lines.
            post_example = managed + b"\n```md\n" + exact_header + b"```\n"
            cleaned_post, matched_post = base.strip_exact_legacy_claude_header_bytes(post_example)
            assert_true(matched_post is None and cleaned_post == post_example,
                        "post-marker fenced example must never match legacy cleanup")

            bogus_marker = (
                exact_header
                + b"<!-- WORKFLOW-MAIN-AGENT:START_BOGUS -->\nbody\n<!-- WORKFLOW-MAIN-AGENT:END -->\n"
            )
            cleaned_bogus, matched_bogus = base.strip_exact_legacy_claude_header_bytes(bogus_marker)
            assert_true(matched_bogus is None and cleaned_bogus == bogus_marker,
                        "malformed START_BOGUS marker must never match legacy cleanup")

            fenced_pre = b"```md\n" + exact_header + b"```\n\n" + managed
            cleaned_fenced, matched_fenced = base.strip_exact_legacy_claude_header_bytes(fenced_pre)
            assert_true(matched_fenced is None and cleaned_fenced == fenced_pre,
                        "fenced pre-marker example must never match legacy cleanup")

            fenced_markers_only = exact_header + b"```md\n" + managed + b"```\n"
            cleaned_fm, matched_fm = base.strip_exact_legacy_claude_header_bytes(fenced_markers_only)
            assert_true(matched_fm is None and cleaned_fm == fenced_markers_only,
                        "header above fenced-only marker pair must never match legacy cleanup")

            mixed_fence = b"```md\n~~~\n" + exact_header + managed + b"```\n"
            cleaned_mf, matched_mf = base.strip_exact_legacy_claude_header_bytes(mixed_fence)
            assert_true(matched_mf is None and cleaned_mf == mixed_fence,
                        "backtick fence containing literal ~~~ line must stay fenced and never match")

            multiline_start = exact_header + b"<!--\nWORKFLOW-MAIN-AGENT:START -->\nbody\n<!-- WORKFLOW-MAIN-AGENT:END -->\n"
            cleaned_ml, matched_ml = base.strip_exact_legacy_claude_header_bytes(multiline_start)
            assert_true(matched_ml is None and cleaned_ml == multiline_start,
                        "multiline START marker must never match legacy cleanup")

            trailing_prose_start = (
                exact_header
                + b"<!-- WORKFLOW-MAIN-AGENT:START --> user prose -->\nbody\n<!-- WORKFLOW-MAIN-AGENT:END -->\n"
            )
            cleaned_tp, matched_tp = base.strip_exact_legacy_claude_header_bytes(trailing_prose_start)
            assert_true(matched_tp is None and cleaned_tp == trailing_prose_start,
                        "START marker with trailing prose and second --> must never match legacy cleanup")

            commented_excerpt = b"<!--\n" + exact_header + managed + b"-->\n"
            cleaned_ce, matched_ce = base.strip_exact_legacy_claude_header_bytes(commented_excerpt)
            assert_true(matched_ce is None and cleaned_ce == commented_excerpt,
                        "header and markers inside an enclosing HTML comment must never match legacy cleanup")

            extra_blanks = exact_header + b"\r\n\r\n" + managed
            cleaned_blanks, matched_blanks = base.strip_exact_legacy_claude_header_bytes(extra_blanks)
            assert_true(matched_blanks is not None and cleaned_blanks == b"\r\n\r\n" + managed,
                        f"extra user blank lines beyond exact 1-line separator stay intact: {cleaned_blanks!r}")

            # Edited / non-exact legacy header is refused without touching the file.
            edited = b"# Claude Code \xe2\x80\x94 Personal Global Config (v3.8.0)\n# Custom user line\n\n" + managed
            claude_md.write_bytes(edited)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = base.run_clean_legacy(apply=True)
            assert_true(code == 1 and "refused" in out.getvalue() and claude_md.read_bytes() == edited,
                        f"non-exact legacy header is refused without edits: code={code} out={out.getvalue()}")

            # Concurrent modification DURING _flush_receipt aborts without clobbering user save
            # AND cleans up its no-op backup dir so default rollback still targets prior install.
            from unittest.mock import patch
            claude_md.write_bytes(original)
            concurrent_save = original + b"concurrent edit during flush\n"
            before_race_dirs = {p.name for p in backups.iterdir() if p.is_dir()}
            orig_flush_race = base._flush_receipt
            def _save_during_flush(complete: bool = False, allow_empty: bool = False):
                orig_flush_race(complete=complete, allow_empty=allow_empty)
                if not complete:
                    claude_md.write_bytes(concurrent_save)
            out = io.StringIO()
            with patch.object(base, "_flush_receipt", _save_during_flush), contextlib.redirect_stdout(out):
                race_code = base.run_clean_legacy(apply=True)
            after_race_dirs = {p.name for p in backups.iterdir() if p.is_dir()}
            assert_true(
                race_code == 1
                and "concurrently" in out.getvalue()
                and claude_md.read_bytes() == concurrent_save
                and after_race_dirs == before_race_dirs,
                f"concurrent edit during flush aborts safely and discards no-op backup: code={race_code} dirs={after_race_dirs - before_race_dirs}",
            )

            # Destination deleted after pending receipt flush (zero rename attempts) discards
            # the no-op backup directory unconditionally so default rollback stays unaffected.
            claude_md.write_bytes(original)
            before_del_dirs = {p.name for p in backups.iterdir() if p.is_dir()}
            def _delete_during_flush(complete: bool = False, allow_empty: bool = False):
                orig_flush_race(complete=complete, allow_empty=allow_empty)
                if not complete:
                    claude_md.unlink()
            deleted_raised = False
            with patch.object(base, "_flush_receipt", _delete_during_flush), contextlib.redirect_stdout(io.StringIO()):
                try:
                    base.run_clean_legacy(apply=True)
                except FileNotFoundError:
                    deleted_raised = True
            after_del_dirs = {p.name for p in backups.iterdir() if p.is_dir()}
            assert_true(
                deleted_raised and after_del_dirs == before_del_dirs,
                f"deleted destination after flush discards no-op backup unconditionally: dirs={after_del_dirs - before_del_dirs}",
            )

            # Replacement failure (sharing violation on target rename AFTER pending receipt flush)
            # leaves original intact and discards the no-op backup dir.
            claude_md.write_bytes(original)
            before_fail_dirs = {p.name for p in backups.iterdir() if p.is_dir()}
            import os as _os
            orig_replace = _os.replace
            def _deny_target_replace(src, dst):
                if Path(dst) == claude_md:
                    raise PermissionError("simulated sharing violation")
                return orig_replace(src, dst)
            replace_failed = False
            with patch.object(_os, "replace", _deny_target_replace), \
                    patch("exoma.core.workspace.workspace_paths.REPLACE_RETRY_DELAYS", ()), \
                    contextlib.redirect_stdout(io.StringIO()):
                try:
                    base.run_clean_legacy(apply=True)
                except PermissionError:
                    replace_failed = True
            after_fail_dirs = {p.name for p in backups.iterdir() if p.is_dir()}
            assert_true(
                replace_failed and claude_md.read_bytes() == original and after_fail_dirs == before_fail_dirs,
                f"pre-replace failure leaves original intact and discards no-op backup: dirs={after_fail_dirs - before_fail_dirs}",
            )

            # Hard crash after pending receipt flush BEFORE rename: rollback reconciles pending
            # entry whose destination still matches pre_sha256 as a safe no-op, while an unexpected
            # third hash still aborts as a conflict.
            pending_dir = backups / "install_29990101_000099"
            pending_dir.mkdir()
            pending_backup = pending_dir / "hosts" / "claude" / "CLAUDE.md"
            pending_backup.parent.mkdir(parents=True)
            pending_backup.write_bytes(original)
            (pending_dir / "install_receipt.json").write_text(
                json.dumps({
                    "schema_version": 2,
                    "complete": False,
                    "entries": [{
                        "action": "merge",
                        "key": "hosts/claude/CLAUDE.md",
                        "dest": str(claude_md),
                        "backup": str(pending_backup),
                        "pre_sha256": hashlib.sha256(original).hexdigest(),
                        "post_sha256": hashlib.sha256(expected_after).hexdigest(),
                        "pending": True,
                    }],
                }),
                encoding="utf-8",
            )
            claude_md.write_bytes(original)
            with contextlib.redirect_stdout(io.StringIO()):
                rb_noop = rollback._run_rollback(pending_dir.name, True)
            assert_true(rb_noop == 0 and claude_md.read_bytes() == original,
                        f"pending receipt interrupted before rename rolls back as safe no-op: code={rb_noop}")
            claude_md.write_bytes(b"unrelated third state\n")
            with contextlib.redirect_stdout(io.StringIO()):
                rb_conflict = rollback._run_rollback(pending_dir.name, True)
            assert_true(rb_conflict == 2 and claude_md.read_bytes() == b"unrelated third state\n",
                        f"pending receipt with unexpected hash still aborts as conflict: code={rb_conflict}")
            shutil.rmtree(pending_dir)

            # Interrupted apply right after atomic replace stays recoverable through --rollback.
            claude_md.write_bytes(original)
            orig_flush = base._flush_receipt
            def _interrupt_finalize(complete: bool = False, allow_empty: bool = False):
                if complete:
                    raise RuntimeError("simulated crash after replace")
                return orig_flush(complete=complete, allow_empty=allow_empty)
            before_dirs = {p.name for p in backups.iterdir() if p.is_dir()}
            crashed = False
            with patch.object(base, "_flush_receipt", _interrupt_finalize), contextlib.redirect_stdout(io.StringIO()):
                try:
                    base.run_clean_legacy(apply=True)
                except RuntimeError:
                    crashed = True
            after_dirs = sorted(p for p in backups.iterdir() if p.is_dir() and p.name not in before_dirs)
            assert_true(crashed and len(after_dirs) == 1 and claude_md.read_bytes() == expected_after,
                        "simulated crash after replace leaves atomic cleaned file and pre-flushed receipt")
            with contextlib.redirect_stdout(io.StringIO()):
                rb_int = rollback._run_rollback(after_dirs[0].name, True)
            assert_true(rb_int == 0 and claude_md.read_bytes() == original,
                        f"interrupted clean-legacy receipt rolls back cleanly: code={rb_int}")

            # Symlinked destination is refused without modifying linked target.
            linked_target = home.parent / "shared-claude.md"
            linked_target.write_bytes(original)
            claude_md.unlink()
            try:
                claude_md.symlink_to(linked_target)
            except OSError:
                # Windows without Developer Mode / symlink privilege: test helper via patch.
                claude_md.write_bytes(original)
                with patch.object(base, "_has_symlink_component", return_value=True), contextlib.redirect_stdout(io.StringIO()):
                    sym_code = base.run_clean_legacy(apply=True)
                assert_true(sym_code == 1 and claude_md.read_bytes() == original,
                            f"symlinked CLAUDE.md refused cleanly (patched): code={sym_code}")
            else:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    sym_code = base.run_clean_legacy(apply=True)
                assert_true(sym_code == 1 and "symlink" in out.getvalue() and linked_target.read_bytes() == original,
                            f"symlinked CLAUDE.md refused without editing target: code={sym_code} out={out.getvalue()}")
        finally:
            base.HOME = saved_base_home


def _test_installer_leaves_no_stale_files() -> None:
    _check_only_our_unedited_leftovers_go()
    _check_ledger_is_evidence_too()
    _check_legacy_opencode_agents_migrate()
    _check_settings_drop_retired_hooks_and_refresh_statusline()
    _check_unstamped_dist_is_refused()
    _check_upgrade_points_at_the_running_build()
    _check_nested_reference_stale_dir_cleanup()
    _check_clean_legacy_exact_match()
