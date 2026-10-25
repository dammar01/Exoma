"""Workspace version reporting and the upgrade path between them."""

from exoma.adapters.contract.registry import provider_for
from exoma.adapters.install.opencode_install import _merge_provider_config
from exoma.core.workspace.workspace_paths import CONFIG_VERSION
from exoma.core.workspace.workspace_paths import WORKFLOW_DIRNAME
from exoma.core.workspace.workspace_paths import LEGACY_RUNTIME_KEYS, _tool_paths
from exoma.core.workspace.workspace_paths import atomic_write_json
from exoma.core.workspace.workspace_paths import atomic_write_text
from exoma.core.workspace.workspace_paths import detect_project_root
from exoma.core.workspace.workspace_paths import read_json_file
from exoma.core.workspace.workspace_paths import workflow_paths
from pathlib import Path
import json
import os
from exoma.core.runtime.config_defaults import default_config, diverged_defaults, merge_config_defaults
from exoma.core.runtime.scripts import _generate_run_scripts


def workspace_versions(project_root: Path) -> dict:
    """What the workspace was built by vs what is running now.

    `installed`/`installed_config` are what .workflow/config.json records; `current` is
    what this process is. They drift the moment the tool is updated without re-running
    init, and nothing else in the runtime notices — the generated run scripts keep the
    paths and flags they were written with.
    """
    from exoma.config.settings import COMPONENT_VERSIONS, TOOL_VERSION

    installed = None
    installed_config = None
    installed_runtime = None
    try:
        config = read_json_file(workflow_paths(project_root)["config"])
        runtime = config.get("runtime")
        if isinstance(runtime, dict):
            installed = runtime.get("tool_version")
            installed_runtime = runtime.get("runtime_version")
        installed_config = config.get("version")
    except (OSError, ValueError):
        pass
    return {
        "installed_tool_version": installed,
        "installed_config_version": installed_config,
        "installed_runtime_version": installed_runtime,
        "current_tool_version": TOOL_VERSION,
        "current_config_version": CONFIG_VERSION,
        "current_runtime_version": COMPONENT_VERSIONS["runtime"],
    }

def needs_upgrade(project_root: Path) -> bool:
    """True when the workspace was scaffolded by a different build than this one.

    Unknown (config unreadable or version absent) counts as needing an upgrade: a
    workspace we cannot identify is exactly the one most likely to be stale.
    """
    if not (project_root / WORKFLOW_DIRNAME).exists():
        return False  # not initialized at all — that is `init`, not `upgrade`
    versions = workspace_versions(project_root)
    # All three are compared, not two. The runtime version was reported by
    # `workspace_versions` and then ignored here, which is harmless only for as long as
    # COMPONENT_VERSIONS["runtime"] keeps deriving from TOOL_VERSION — the moment the
    # runtime is versioned on its own, a runtime-only bump would ship to workspaces that
    # never learned they were stale. Comparing it now costs nothing and closes that.
    from exoma.core.runtime.migrations import pending_migration
    from exoma.core.workspace.workspace_paths import is_legacy_layout

    return (
        versions["installed_tool_version"] != versions["current_tool_version"]
        or versions["installed_config_version"] != versions["current_config_version"]
        or versions["installed_runtime_version"] != versions["current_runtime_version"]
        # A workspace still keeping its data at the .workflow root is stale whatever its
        # stamps say: only upgrade moves it to the data layout.
        or is_legacy_layout(project_root)
        # Nor is one whose migration moved the files but left post-rename steps undone.
        or pending_migration(project_root) is not None
        # Nor one whose migration died after staging and before the rename — whether or not
        # a writer has since created data/: upgrade recovers the first and refuses, loudly,
        # the second, which is better than neither ever being looked at again.
        or (project_root / WORKFLOW_DIRNAME / "data.migrating").is_dir()
    )

def _install_project_boundary(project_root: Path) -> dict:
    """Install the second_agent's project boundary through whichever provider owns it.

    Dispatched rather than called directly: the boundary is a provider-shaped file
    (opencode's is <project_root>/opencode.json), so hardcoding one provider's installer
    here would scaffold an opencode boundary into a workspace configured for something
    else. A provider with no bundle or no installer is reported, not guessed at — a
    boundary that silently did not install is the gap doctor exists to catch.

    The provider config is resolved and passed in the same way `main.py` resolves it
    before building an adapter. Reading only the workspace config.json here would let a
    project whose second_agent.json names another provider RUN that provider while being
    installed opencode's boundary — the exact mismatch this dispatch exists to prevent.
    """
    from exoma.config.providers import PROVIDER_BUNDLES, provider_install_module
    from exoma.config.settings import load_provider_config_for

    provider = provider_for(project_root, load_provider_config_for(project_root))
    skipped = {
        "path": None,
        "status": "provider_unsupported",
        "keys_added": 0,
        "permissions_enforced": 0,
        "warnings": [
            f"provider '{provider}' ships no project boundary installer; "
            f"<project_root> was left untouched"
        ],
    }
    if provider not in PROVIDER_BUNDLES:
        return skipped
    module = provider_install_module(provider)
    installer = getattr(module, "install_project_config", None)
    if installer is None:
        return skipped
    return installer(project_root)

def upgrade_workflow_workspace(
    project_root: Path,
    agent_workflow_path: str | None = None,
    _capacity_guarded: bool = False,
) -> dict:
    """Bring an existing .workflow/ up to the running build.

    `agent_workflow_path` is the legacy alias agent-workflow callers pass; the workspace
    is always pointed at the running build (`_tool_paths`).

    Run on demand, and also by ensure_workflow_workspace when init lands on a workspace
    stamped by an older build — leaving that to the user meant every later fix reached
    only the workspaces whose owner remembered a second command.

    Regenerates the derived parts (run/inspect/check scripts, config defaults, adapter
    config keys) and leaves everything owned by the user or by a live flow alone —
    sessions/ above all: a job may be running against it right now, and rewriting its
    state mid-flight would lose the very evidence the caller is waiting for.
    """
    if not _capacity_guarded:
        from exoma.core.jobs.job_manager import JobManager

        with JobManager().capacity_guard():
            return upgrade_workflow_workspace(
                project_root, agent_workflow_path, _capacity_guarded=True
            )

    paths = workflow_paths(project_root)
    if not paths["workflow_dir"].exists():
        raise ValueError(
            f"no {WORKFLOW_DIRNAME}/ at {project_root} — run init first, upgrade only "
            "refreshes an existing workspace"
        )

    active = active_jobs_for_workspace(project_root)
    if active:
        # Refuse rather than warn. The regenerated scripts and rewritten config.json are
        # read by the very flow that is mid-call, and its session state is the evidence
        # the caller is currently waiting on. "Preserved sessions/" is only true if
        # nothing is writing to them while this runs.
        listed = ", ".join(f"{j['job_id']} ({j['command']})" for j in active[:3])
        raise ValueError(
            f"{len(active)} job(s) still running against {project_root}: {listed}. "
            "Wait for them to finish (or fail them) before upgrading — regenerating the "
            "workspace under a live call can lose its session state."
        )

    before = workspace_versions(project_root)
    tool = _tool_paths(agent_workflow_path)

    # Layout migrations first: every path below is resolved against the layout they leave.
    from exoma.core.runtime import migrations

    try:
        migration_reports = migrations.run(project_root)
    except migrations.MigrationConflict:
        raise
    except migrations.MigrationIncomplete as exc:
        # Past the rename nothing is moved back: the workspace is already on data/ and
        # working. Saying "rolled back" here would send the user looking for a
        # migration-backup-* that does not exist and hide the step that still has to run.
        raise ValueError(
            f"workspace migration incomplete: the files moved into {WORKFLOW_DIRNAME}/data/, "
            f"but {exc}. Fix the cause (a file held open by an editor or antivirus is the "
            "usual one) and rerun upgrade — it resumes from that step. "
            f"Backup: {exc.backup or 'none recorded'}."
        ) from exc
    except migrations.MigrationRollbackIncomplete as exc:
        raise ValueError(
            f"workspace migration failed and the rollback is INCOMPLETE: {exc}. The entries "
            f"that could not move back are still in {exc.staging}; move them to "
            f"{WORKFLOW_DIRNAME}/ by hand (or rerun upgrade, which recovers a leftover staging "
            "directory first). A pre-migration copy may also be under .workflow/ "
            "(migration-backup-*)."
        ) from exc
    except Exception as exc:
        raise ValueError(
            f"workspace migration failed and was rolled back: {type(exc).__name__}: {exc}. "
            "The pre-migration copy is under .workflow/ (migration-backup-*); nothing else changed."
        ) from exc
    paths = workflow_paths(project_root)

    # Directories init scaffolds, re-made here because upgrade is what people run on a
    # workspace that has been lived in. `reports/` and `sessions/` were created by init and
    # by nothing else, so a workspace that lost one — cleaned by hand, restored from a
    # partial backup, checked out without empty dirs — stayed broken through every upgrade
    # and only recovered by running init again. mkdir with exist_ok is free when they exist.
    restored_dirs = [
        str(directory)
        for directory in (paths["reports_dir"], paths["sessions_dir"])
        if not directory.exists()
    ]
    paths["reports_dir"].mkdir(parents=True, exist_ok=True)
    paths["sessions_dir"].mkdir(parents=True, exist_ok=True)

    # Ahead of every read below: v3.4.3 renamed the provider keys with no read-side
    # alias, so a v3.4.2 workspace must be translated before anything interprets it.
    # Idempotent, so a workspace already on the new names passes straight through.
    from exoma.core.workspace.provider_migration import migrate_provider_keys

    provider_migration = migrate_provider_keys(project_root)

    config_changed = False
    if not paths["config"].exists():
        config = default_config(project_root, agent_workflow_path)
        config_changed = True
    else:
        try:
            config = read_json_file(paths["config"])
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"refusing to overwrite unreadable workflow config {paths['config']}: {exc}"
            ) from exc
        if not isinstance(config, dict):
            raise ValueError(
                f"workflow config must be a JSON object: {paths['config']}"
            )
        if "runtime" in config and not isinstance(config.get("runtime"), dict):
            raise ValueError(
                f"workflow config runtime must be an object: {paths['config']}"
            )
        config, config_changed = merge_config_defaults(config)
        runtime = config.setdefault("runtime", {})
        # Tool paths are absolute and machine-specific: an upgrade after the interpreter or
        # the install moved must repoint them, or the regenerated scripts call one that is gone.
        for key in ("python", "package_root", "tool_version", "runtime_version"):
            if runtime.get(key) != tool[key]:
                runtime[key] = tool[key]
                config_changed = True
        # A workspace agent-workflow wrote points at a checkout's main.py/check.py. Exoma
        # runs as `-m exoma`; leaving the old paths would only mislead whoever reads them.
        for key in LEGACY_RUNTIME_KEYS:
            if key in runtime:
                del runtime[key]
                config_changed = True
    if config_changed:
        atomic_write_json(paths["config"], config)

    opencode_added = _merge_provider_config(project_root)
    # Refresh the project boundary too. Skipping it here would mean a deny-rule added in a
    # newer build never reaches a workspace that was scaffolded on an older one.
    project_boundary = _install_project_boundary(project_root)

    # Always render; the generator writes only what actually differs. The old gate keyed on
    # the runtime version bumping, which meant a generator change shipped without a version
    # bump never reached an existing workspace — the script on disk stayed wrong until
    # someone re-ran init. Content is the honest signal; the version number was a proxy for
    # it that could silently disagree.
    scripts = _generate_run_scripts(project_root)
    gitignore_updated = ensure_root_gitignore_entry(project_root)

    return {
        "project_root": str(project_root),
        "from": before,
        "to": workspace_versions(project_root),
        "config_updated": config_changed,
        "provider_migration": provider_migration,
        "opencode_keys_added": opencode_added,
        "project_opencode": project_boundary,
        "diverged_from_defaults": diverged_defaults(config),
        "regenerated_scripts": scripts,
        "gitignore_updated": gitignore_updated,
        "restored_dirs": restored_dirs,
        "migrations": migration_reports,
        "preserved": [str(paths["sessions_dir"])],
        "tool": tool,
    }

def active_jobs_for_workspace(project_root: Path) -> list[dict]:
    """Pending/running jobs whose work_dir is this project. [] on any failure.

    Best-effort by design: this exists to stop an upgrade from landing under a live
    call, and a job store it cannot read is not a reason to block one.
    """
    try:
        from exoma.core.jobs.job_manager import DEAD, JobManager

        manager = JobManager()
        target = str(Path(project_root).resolve())
        out: list[dict] = []
        for path in manager.job_dir.glob("job_*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") not in {"pending", "running", "recovering"}:
                continue
            work_dir = job.get("work_dir")
            if work_dir:
                try:
                    job_root = str(detect_project_root(work_dir).resolve())
                except (OSError, ValueError):
                    job_root = str(Path(work_dir).resolve())
                if job_root != target:
                    continue
            # A job whose worker is gone is not "active", it is unreaped. Blocking the
            # upgrade on one would make a crashed worker permanently jam the command.
            if manager.liveness(job) == DEAD:
                continue
            out.append(job)
        return out
    except Exception:
        return []

def ensure_root_gitignore_entry(project_root: Path) -> bool:
    gitignore_path = project_root / ".gitignore"
    desired = ".workflow/"
    if not gitignore_path.exists():
        atomic_write_text(gitignore_path, f"{desired}\n")
        return True

    content = gitignore_path.read_text(encoding="utf-8")
    lines = [line.strip() for line in content.splitlines()]
    if desired in lines:
        return False
    suffix = "" if not content or content.endswith("\n") else "\n"
    atomic_write_text(gitignore_path, f"{content}{suffix}{desired}\n")
    return True

def resolve_runtime(project_root: Path) -> dict:
    """The runtime a workspace's scripts should call: always the build running this call.

    agent-workflow chose between the running checkout, `$AGENT_PATH` and the main.py
    recorded in config.json. Exoma is invoked as an installed package, so the build that
    is running is the one being installed or upgraded; `$AGENT_PATH` and the recorded
    path are reported as `legacy` for doctor and never followed.
    """
    tool = _tool_paths()
    legacy: dict = {}
    env_candidate = os.getenv("AGENT_PATH")
    if env_candidate:
        legacy["env"] = env_candidate
    config_path = workflow_paths(project_root)["config"]
    if config_path.exists():
        try:
            runtime = read_json_file(config_path).get("runtime")
        except ValueError:
            runtime = None
        if isinstance(runtime, dict):
            recorded = {key: runtime[key] for key in LEGACY_RUNTIME_KEYS if runtime.get(key)}
            if recorded:
                legacy["workflow_config"] = recorded
    return {
        "ok": True,
        "source": "running_build",
        "python": tool["python"],
        "package_root": tool["package_root"],
        "legacy": legacy,
    }
