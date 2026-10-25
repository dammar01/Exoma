"""Upgrading a v3.5.x-layout workspace to the v3.6.0 data/ layout, and what that layout adds.

The migration moves a workspace's whole history, so each property that makes that safe is
pinned: everything moves and nothing is lost (a backup first, archived evidence still
found), a second upgrade does nothing, a live job blocks it, a failure puts everything
back. Beside it: config.json holds overrides only, and `.workflow/current/` mirrors the
latest dispatch without ever carrying a resolved credential.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from exoma.core.workspace.workspace_paths import is_legacy_layout, workflow_paths
from tests.checks.support import assert_true


def _legacy_workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="aw-migrate-"))
    wf = root / ".workflow"
    wf.mkdir()
    (wf / "config.json").write_text(json.dumps({
        "version": "3.6.0",
        "commands": {"verify_mode": "delegated", "auto_verify_after_execute": True},
        "policies": {"graph_leads_enabled": True},
        "e2e": {"base_url": "http://localhost:8000", "headless": True, "allowed_mutation_paths": ["/x"]},
        "runtime": {"tool_version": "3.6.0"},
    }), encoding="utf-8")
    (wf / "second_agent.json").write_text(json.dumps({"provider": "codex", "default_model": "gpt-5.6-luna"}), encoding="utf-8")
    run = wf / "sessions" / "s1" / "logs" / "r1"
    run.mkdir(parents=True)
    (run / "output.raw.md").write_text("[EVIDENCE]\n", encoding="utf-8")
    (wf / "usage.jsonl").write_text('{"command": "explore"}\n', encoding="utf-8")
    (wf / "audit.jsonl").write_text('{"command": "explore"}\n', encoding="utf-8")
    (wf / "facts.jsonl").write_text("", encoding="utf-8")
    (wf / "evidence.jsonl").write_text(
        json.dumps({"artifact_path": str(run / "output.raw.md"), "artifact_hash": "x", "session": "s1"}) + "\n", encoding="utf-8"
    )
    (wf / "evidence.jsonl.lock").write_text("x", encoding="utf-8")
    (wf / "state.json").write_text("{}", encoding="utf-8")
    (wf / "runtime").mkdir()
    (wf / "provider-sessions").mkdir()
    (wf / "notes.txt").write_text("the user's", encoding="utf-8")
    (wf / "e2e").mkdir()
    (wf / "e2e" / "secrets.json").write_text(
        json.dumps({"default": "qa", "profiles": {"qa": {"E2E_USER": "u", "E2E_PASS": "p"}}}), encoding="utf-8"
    )
    return root


def _check_migration_moves_everything_once() -> None:
    from exoma.core.runtime.upgrade import needs_upgrade, upgrade_workflow_workspace

    root = _legacy_workspace()
    wf = root / ".workflow"
    try:
        assert_true(is_legacy_layout(root) and needs_upgrade(root), "fixture assumption: a v3.5.x-layout workspace needs an upgrade")
        result = upgrade_workflow_workspace(root, None)
        report = result["migrations"][0]
        data = wf / "data"
        assert_true(not is_legacy_layout(root) and data.is_dir(), "the workspace is on the data layout")
        for name in ("sessions", "provider-sessions", "usage.jsonl", "audit.jsonl", "facts.jsonl", "evidence.jsonl"):
            assert_true((data / name).exists() and not (wf / name).exists(), f"{name} moved into data/")
        assert_true(
            (data / "usage.jsonl").read_text(encoding="utf-8") == '{"command": "explore"}\n',
            "history moves intact, not recreated empty",
        )
        assert_true(not (wf / "state.json").exists() and not (wf / "runtime").exists() and not (wf / "evidence.jsonl.lock").exists(),
                    f"pre-session leftovers and old locks are removed: {report['removed']}")
        backup = Path(report["backup"])
        assert_true((backup / "usage.jsonl").exists() and (backup / "state.json").exists() and (backup / "sessions").is_dir(),
                    "the backup holds the workspace as it was")
        index = json.loads((data / "evidence.jsonl").read_text(encoding="utf-8"))
        assert_true(Path(index["artifact_path"]).is_file() and "data" in Path(index["artifact_path"]).parts,
                    f"archived evidence still resolves after the move: {index['artifact_path']}")
        config = json.loads((wf / "config.json").read_text(encoding="utf-8"))
        assert_true(
            config.get("commands") == {"auto_verify_after_execute": True} and config.get("e2e") == {"headless": True}
            and "policies" not in config and config["runtime"]["layout_version"] == 2,
            f"config.json keeps only real overrides: {config}",
        )
        assert_true(any("allowed_mutation_paths" in item for item in report["config_stripped"]), "a retired key is removed and reported")
        secrets = json.loads((wf / "e2e" / "secrets.json").read_text(encoding="utf-8"))
        assert_true(
            secrets["profiles"] == [{"name": "qa", "credentials": {"E2E_USER": "u", "E2E_PASS": "p"}}] and report["secrets_converted"],
            f"the pre-list secrets.json is converted with its values: {secrets}",
        )
        assert_true(report["stray"] == ["notes.txt"] and (wf / "notes.txt").exists(), f"an unrecognised file is named and left alone: {report['stray']}")
        again = upgrade_workflow_workspace(root, None)
        assert_true(again["migrations"] == [] and not is_legacy_layout(root),
                    f"a second upgrade migrates nothing: {again['migrations']}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_live_job_blocks_migration() -> None:
    import exoma.core.runtime.upgrade as upgrade

    root = _legacy_workspace()
    saved = upgrade.active_jobs_for_workspace
    upgrade.active_jobs_for_workspace = lambda project_root: [{"job_id": "job-1", "command": "explore"}]
    try:
        try:
            upgrade.upgrade_workflow_workspace(root, None)
            refused = False
        except ValueError:
            refused = True
        assert_true(refused and is_legacy_layout(root) and not (root / ".workflow" / "data").exists(),
                    "a live job refuses the upgrade before anything moves")
    finally:
        upgrade.active_jobs_for_workspace = saved
        shutil.rmtree(root, ignore_errors=True)


def _check_failed_migration_is_rolled_back() -> None:
    import exoma.core.runtime.migrations as migrations

    from exoma.core.runtime.upgrade import upgrade_workflow_workspace

    root = _legacy_workspace()
    wf = root / ".workflow"
    saved = migrations.os.replace

    def failing_replace(src, dst):
        if str(src).endswith("data.migrating"):
            raise OSError("simulated rename failure")
        return saved(src, dst)

    migrations.os.replace = failing_replace
    try:
        try:
            upgrade_workflow_workspace(root, None)
            raised = False
        except ValueError as exc:
            raised = "rolled back" in str(exc)
        assert_true(raised, "a failed migration is reported as rolled back")
        assert_true(
            is_legacy_layout(root) and (wf / "usage.jsonl").exists() and (wf / "sessions" / "s1").is_dir()
            and not (wf / "data").exists() and not (wf / "data.migrating").exists(),
            "and everything is back where it was",
        )
        assert_true(any(p.name.startswith("migration-backup-") for p in wf.iterdir()), "with its backup kept beside it")
    finally:
        migrations.os.replace = saved
        shutil.rmtree(root, ignore_errors=True)


def _check_post_rename_failure_resumes() -> None:
    """Each post-rename step, failing once, leaves a workspace a second upgrade finishes."""
    import exoma.core.runtime.migrations as migrations

    from exoma.core.runtime.upgrade import needs_upgrade, upgrade_workflow_workspace

    targets = {
        "rewrite_evidence": "_rewrite_evidence_paths",
        "remove_obsolete": "_remove_obsolete",
        "strip_config": "_strip_config",
        "convert_secrets": "_convert_secrets",
        "prune_backups": "_prune_backups",
    }
    assert_true(set(targets) == set(migrations._POST_STEPS), f"every post-rename step is exercised: {migrations._POST_STEPS}")
    for step, attribute in targets.items():
        root = _legacy_workspace()
        wf = root / ".workflow"
        saved = getattr(migrations, attribute)

        def failing(*args, **kwargs):
            raise PermissionError("simulated: file held open by another process")

        setattr(migrations, attribute, failing)
        try:
            try:
                upgrade_workflow_workspace(root, None)
                message = ""
            except ValueError as exc:
                message = str(exc)
            assert_true("incomplete" in message and "rolled back" not in message and step in message,
                        f"{step}: a post-rename failure is reported as resumable, not rolled back: {message!r}")
            pending = migrations.pending_migration(root)
            assert_true((wf / "data").is_dir() and not is_legacy_layout(root) and pending and pending["pending"][0] == step,
                        f"{step}: data/ is live and the marker names the failed step first: {pending}")
            assert_true(needs_upgrade(root), f"{step}: an unfinished migration still needs an upgrade")
            if step == "convert_secrets":
                from exoma.core.audit.diagnostics import run_doctor
                from exoma.core.workspace.workspace_paths import read_json_file

                meta = run_doctor(root, "does-not-exist", None)["meta"]
                layout = read_json_file(Path(meta["doctor_report"]))["checks"]["workspace_layout"]
                assert_true(layout["migration_pending"] == ["convert_secrets", "prune_backups"],
                            f"doctor names the steps still to run: {layout}")
        finally:
            setattr(migrations, attribute, saved)
        try:
            again = upgrade_workflow_workspace(root, None)
            report = again["migrations"][0] if again["migrations"] else {}
            assert_true(report.get("resumed") and migrations.pending_migration(root) is None
                        and not (wf / "data" / migrations.PENDING_FILENAME).exists(),
                        f"{step}: the next upgrade resumes and clears the marker: {again['migrations']}")
            config = json.loads((wf / "config.json").read_text(encoding="utf-8"))
            secrets = json.loads((wf / "e2e" / "secrets.json").read_text(encoding="utf-8"))
            index = json.loads((wf / "data" / "evidence.jsonl").read_text(encoding="utf-8"))
            assert_true(
                config["runtime"]["layout_version"] == 2 and isinstance(secrets["profiles"], list)
                and Path(index["artifact_path"]).is_file() and not (wf / "state.json").exists(),
                f"{step}: the resumed workspace ends where an uninterrupted migration does",
            )
            assert_true(upgrade_workflow_workspace(root, None)["migrations"] == [], f"{step}: a third upgrade is a no-op")
        finally:
            shutil.rmtree(root, ignore_errors=True)


def _check_failed_backup_leaves_no_staging() -> None:
    import exoma.core.runtime.migrations as migrations

    from exoma.core.runtime.upgrade import upgrade_workflow_workspace

    root = _legacy_workspace()
    wf = root / ".workflow"
    saved = migrations._backup

    def failing_backup(workflow_dir, target):
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.json").write_text("{}", encoding="utf-8")  # half-copied
        raise OSError("simulated: disk full")

    migrations._backup = failing_backup
    try:
        try:
            upgrade_workflow_workspace(root, None)
            message = ""
        except ValueError as exc:
            message = str(exc)
        assert_true("rolled back" in message, f"a failed backup is a pre-rename failure: {message!r}")
        assert_true(
            is_legacy_layout(root) and not (wf / "data.migrating").exists() and not (wf / "data").exists()
            and not any(p.name.startswith("migration-backup-") for p in wf.iterdir()),
            "no staging directory is left, and a half-copied backup is not kept as if it were one",
        )
    finally:
        migrations._backup = saved
        shutil.rmtree(root, ignore_errors=True)


def _check_staging_conflict_is_refused() -> None:
    from exoma.core.runtime.upgrade import upgrade_workflow_workspace

    root = _legacy_workspace()
    wf = root / ".workflow"
    (wf / "data.migrating" / "sessions" / "s-left").mkdir(parents=True)
    (wf / "data.migrating" / "reports").mkdir()
    try:
        try:
            upgrade_workflow_workspace(root, None)
            message = ""
        except ValueError as exc:
            message = str(exc)
        assert_true("sessions" in message and "reports" not in message.split("holds")[-1].split("which")[0],
                    f"the refusal names exactly the conflicting entries: {message!r}")
        assert_true(
            (wf / "data.migrating" / "sessions" / "s-left").is_dir() and (wf / "data.migrating" / "reports").is_dir()
            and (wf / "sessions" / "s1").is_dir() and not (wf / "sessions" / "sessions").exists()
            and not (wf / "reports").exists() and is_legacy_layout(root) and not (wf / "data").exists(),
            "nothing moves: both copies stay as they were, and no directory is nested into another",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_stranded_staging_is_recovered() -> None:
    """Every internal item staged, then death before the rename: no root marker, no data/."""
    from exoma.core.runtime.upgrade import needs_upgrade, upgrade_workflow_workspace

    root = _legacy_workspace()
    wf = root / ".workflow"
    staging = wf / "data.migrating"
    (staging / "backups").mkdir(parents=True)
    for entry in list(wf.iterdir()):
        if entry.name in ("sessions", "provider-sessions", "usage.jsonl", "audit.jsonl", "facts.jsonl", "evidence.jsonl"):
            shutil.move(str(entry), str(staging / entry.name))
    (staging / ".migration-pending.json").write_text('{"stamp": "20260101_000000_1", "pending": []}', encoding="utf-8")
    try:
        assert_true(not is_legacy_layout(root) and needs_upgrade(root),
                    "fixture assumption: the workspace looks migrated, yet still needs an upgrade")
        upgrade_workflow_workspace(root, None)
        data = wf / "data"
        assert_true(
            not staging.exists() and (data / "sessions" / "s1").is_dir()
            and (data / "usage.jsonl").read_text(encoding="utf-8") == '{"command": "explore"}\n'
            and not (data / ".migration-pending.json").exists() and not (wf / ".migration-pending.json").exists(),
            "the stranded history is recovered and migrated, not skipped beside an empty data/",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_staging_beside_live_data() -> None:
    """data/ created after the crash: empty scaffolding yields, real records refuse, backups carry over."""
    from exoma.core.runtime.upgrade import needs_upgrade, upgrade_workflow_workspace

    def stranded() -> Path:
        root = _legacy_workspace()
        wf = root / ".workflow"
        (wf / "data.migrating").mkdir()
        for name in ("sessions", "provider-sessions", "usage.jsonl", "audit.jsonl", "facts.jsonl", "evidence.jsonl"):
            shutil.move(str(wf / name), str(wf / "data.migrating" / name))
        return root

    root = stranded()
    wf = root / ".workflow"
    (wf / "data" / "sessions").mkdir(parents=True)
    (wf / "data" / "reports").mkdir()
    try:
        upgrade_workflow_workspace(root, None)
        assert_true(not (wf / "data.migrating").exists() and (wf / "data" / "sessions" / "s1").is_dir()
                    and (wf / "data" / "usage.jsonl").read_text(encoding="utf-8") == '{"command": "explore"}\n',
                    "an empty scaffolded data/ yields to the stranded history")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    root = stranded()
    wf = root / ".workflow"
    (wf / "data").mkdir()
    (wf / "data" / "usage.jsonl").write_text('{"command": "plan"}\n', encoding="utf-8")
    try:
        assert_true(needs_upgrade(root), "a staging directory beside live data still needs attention")
        try:
            upgrade_workflow_workspace(root, None)
            message = ""
        except ValueError as exc:
            message = str(exc)
        assert_true("already live" in message and "sessions" in message, f"live data beside staged history is refused: {message!r}")
        assert_true((wf / "data.migrating" / "sessions" / "s1").is_dir()
                    and (wf / "data" / "usage.jsonl").read_text(encoding="utf-8") == '{"command": "plan"}\n',
                    "and neither copy is touched")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    root = Path(tempfile.mkdtemp(prefix="aw-staged-backups-"))
    wf = root / ".workflow"
    (wf / "data.migrating" / "backups" / "20260101_000000_1").mkdir(parents=True)
    (wf / "data.migrating" / "backups" / "20260101_000000_1" / "config.json").write_text("{}", encoding="utf-8")
    (wf / "data").mkdir()
    (wf / "data" / "usage.jsonl").write_text("", encoding="utf-8")
    (wf / "config.json").write_text(json.dumps({"runtime": {}}), encoding="utf-8")
    try:
        import exoma.core.runtime.migrations as migrations

        migrations.migrate_to_data_layout(root)
        assert_true(not (wf / "data.migrating").exists()
                    and (wf / "data" / "backups" / "20260101_000000_1" / "config.json").is_file(),
                    "a backups-only staging directory is carried into data/backups and removed")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_empty_tree_removal_never_deletes_a_file() -> None:
    import exoma.core.runtime.migrations as migrations
    root = Path(tempfile.mkdtemp(prefix="aw-empty-tree-"))
    try:
        (root / "data" / "sessions" / "a").mkdir(parents=True)
        (root / "data" / "reports").mkdir()
        assert_true(migrations._remove_empty_tree(root / "data") and not (root / "data").exists(), "directories only: removed")

        (root / "data" / "sessions" / "sid" / "runtime").mkdir(parents=True)
        (root / "data" / "reports").mkdir()
        (root / "data" / "sessions" / "sid" / "runtime" / "delegated.marker").write_text("x", encoding="utf-8")
        assert_true(not migrations._remove_empty_tree(root / "data")
                    and (root / "data" / "sessions" / "sid" / "runtime" / "delegated.marker").is_file(),
                    "a file anywhere stops the removal and survives it")

        saved = os.walk

        def racing_walk(top, topdown=True):
            for entry in saved(top, topdown=topdown):
                (Path(top) / "late.json").write_text("{}", encoding="utf-8")  # a writer lands mid-walk
                yield entry

        (root / "race" / "sessions").mkdir(parents=True)
        migrations.os.walk = racing_walk
        try:
            removed = migrations._remove_empty_tree(root / "race")
        finally:
            migrations.os.walk = saved
        assert_true(not removed and (root / "race" / "late.json").is_file(), "a file created after the scan is never deleted")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_linked_data_dir_is_never_touched() -> None:
    import exoma.core.runtime.migrations as migrations
    root = Path(tempfile.mkdtemp(prefix="aw-linked-data-"))
    wf = root / ".workflow"
    target = root / "elsewhere"
    (target / "sessions").mkdir(parents=True)
    (wf / "data.migrating" / "backups" / "20260101_000000_1").mkdir(parents=True)
    try:
        try:
            (wf / "data").symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            return  # no symlink privilege here (Windows without developer mode): nothing to exercise
        try:
            migrations.migrate_to_data_layout(root)
            message = ""
        except ValueError as exc:
            message = str(exc)
        assert_true("link" in message, f"staging beside a linked data/ is refused: {message!r}")
        assert_true((target / "sessions").is_dir() and not (target / "backups").exists()
                    and (wf / "data.migrating" / "backups" / "20260101_000000_1").is_dir(),
                    "the link's target is neither emptied nor written into, and staging stays")

        # The other side: data.migrating/ itself a link, data/ a real live directory.
        (wf / "data").unlink()
        shutil.rmtree(wf / "data.migrating")
        (wf / "data").mkdir()
        (wf / "data" / "usage.jsonl").write_text("", encoding="utf-8")
        (target / "backups" / "20260101_000000_1").mkdir(parents=True)
        (wf / "data.migrating").symlink_to(target, target_is_directory=True)
        try:
            migrations.migrate_to_data_layout(root)
            message = ""
        except ValueError as exc:
            message = str(exc)
        assert_true("link" in message, f"a linked data.migrating/ is refused: {message!r}")
        assert_true((target / "backups" / "20260101_000000_1").is_dir() and (target / "sessions").is_dir()
                    and not (wf / "data" / "backups").exists(),
                    "nothing is read through the link, moved out of its target, or removed")
    finally:
        for link in (wf / "data", wf / "data.migrating"):
            if link.is_symlink():
                with contextlib.suppress(OSError):
                    link.unlink()
        shutil.rmtree(root, ignore_errors=True)


def _check_untrusted_marker_reruns_everything() -> None:
    import exoma.core.runtime.migrations as migrations
    root = Path(tempfile.mkdtemp(prefix="aw-marker-"))
    data = root / ".workflow" / "data"
    data.mkdir(parents=True)
    marker = data / migrations.PENDING_FILENAME
    try:
        for payload, why in (
            ({"stamp": "20260101_000000_1", "pending": ["unknown_step"]}, "an unknown step name"),
            ({"stamp": "20260101_000000_1", "pending": "strip_config"}, "a pending that is not a list"),
            ("{not json", "an unreadable marker"),
        ):
            marker.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
            state = migrations.pending_migration(root)
            assert_true(state["pending"] == list(migrations._POST_STEPS), f"{why} reruns every step: {state}")
        for stamp in ("../../outside", str(root), "", 7):
            marker.write_text(json.dumps({"stamp": stamp, "pending": ["convert_secrets"]}), encoding="utf-8")
            state = migrations.pending_migration(root)
            assert_true(
                migrations._STAMP_PATTERN.fullmatch(state["stamp"]) and state["pending"] == ["convert_secrets"],
                f"a stamp that is not one this module writes is replaced, never used as a path: {stamp!r} -> {state}",
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_evidence_paths_match_any_spelling() -> None:
    from exoma.core.runtime.upgrade import upgrade_workflow_workspace

    root = _legacy_workspace()
    wf = root / ".workflow"
    spelled = str(wf).replace("\\", "/") + "/./sessions/s1/logs/r1/output.raw.md"
    if os.name == "nt":
        spelled = spelled.upper()
    rows = [
        {"artifact_path": spelled, "artifact_hash": "x", "session": "s1"},
        {"artifact_path": str(wf / "sessions-other" / "x.md"), "artifact_hash": "y", "session": "s2"},
    ]
    (wf / "evidence.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    try:
        report = upgrade_workflow_workspace(root, None)["migrations"][0]
        first, second = (json.loads(line) for line in (wf / "data" / "evidence.jsonl").read_text(encoding="utf-8").splitlines())
        assert_true(report["evidence_paths_rewritten"] == 1 and Path(first["artifact_path"]).is_file(),
                    f"a row spelled with other separators or case is still rewritten: {first}")
        assert_true(second["artifact_path"] == rows[1]["artifact_path"],
                    f"a sibling that merely shares the prefix is left alone: {second}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_config_holds_overrides_only() -> None:
    from exoma.core.runtime.config_defaults import effective_section, merge_config_defaults
    from exoma.core.runtime.state import ensure_workflow_workspace

    root = Path(tempfile.mkdtemp(prefix="aw-overrides-"))
    try:
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
        config = json.loads(workflow_paths(root)["config"].read_text(encoding="utf-8"))
        assert_true(config.get("commands") == {} and config.get("policies") == {} and "e2e" not in config,
                    f"a new config.json copies no defaults: {config.get('commands')} {config.get('policies')}")
        merged, _ = merge_config_defaults({"commands": {"verify_mode": "syntax"}})
        assert_true(merged["commands"] == {"verify_mode": "syntax"}, f"loading never backfills defaults: {merged}")
        effective = effective_section(merged, "commands")
        assert_true(effective["verify_mode"] == "syntax" and effective["auto_verify_after_execute"] is False,
                    f"the effective view is defaults plus overrides: {effective}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_current_mirrors_the_latest_dispatch() -> None:
    from exoma.core.workspace import current
    from tests.checks.e2e_routing import _adapter, _flow, _workspace

    root = _workspace("aw-current-")
    try:
        _flow(root, _adapter(), "pass", {"E2E_USER": "user@example.test"})
        directory = root / ".workflow" / "current"
        session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        assert_true(session["status"] == "done" and session["command"] == "verify-browser", f"the mirror shows the finished run: {session}")
        events = (directory / "e2e" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        assert_true(any(json.loads(line).get("type") == "progress" for line in events), "browser steps arrive in current/e2e/events.jsonl")
        assert_true((directory / "e2e" / "report.json").is_file() and (directory / "e2e" / "verification.md").is_file(),
                    "the run's readable results are copied in")
        mirrored = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in directory.rglob("*") if p.is_file())
        assert_true("user@example.test" not in mirrored, "the mirror never carries a resolved credential")

        current.start(root, "another-session", "explore", "map it")
        current.e2e_event(root, "e2e-session", {"type": "progress", "step": 9})
        assert_true(
            json.loads((directory / "session.json").read_text(encoding="utf-8"))["session_id"] == "another-session"
            and not (directory / "e2e").exists(),
            "a newer dispatch takes the mirror over, and the older session stops writing into it",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_workspace_migration() -> None:
    _check_migration_moves_everything_once()
    _check_live_job_blocks_migration()
    _check_failed_migration_is_rolled_back()
    _check_post_rename_failure_resumes()
    _check_failed_backup_leaves_no_staging()
    _check_staging_conflict_is_refused()
    _check_stranded_staging_is_recovered()
    _check_staging_beside_live_data()
    _check_empty_tree_removal_never_deletes_a_file()
    _check_linked_data_dir_is_never_touched()
    _check_untrusted_marker_reruns_everything()
    _check_evidence_paths_match_any_spelling()
    _check_config_holds_overrides_only()
    _check_current_mirrors_the_latest_dispatch()
