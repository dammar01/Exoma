"""Readiness checks: doctor, sweep, bundle integrity, MCP scan, prune."""

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from exoma.core.workspace.workspace_paths import (
    LOCK_TTL_SECONDS,
    atomic_write_json,
    atomic_write_text,
    now_iso,
    read_json_file,
    workflow_paths,
)
from exoma.utils import osutil
from exoma.utils.osutil import provider_callable

# Split out of this module; re-exported for callers that still import them from here.
from exoma.core.audit.bundle_integrity import (  # noqa: E402,F401
    _bundle_integrity,
    _host_status,
    _expand_home,
    _installed_intent_mode,
    _installed_path_for,
    _marker_block,
    _select_intent_section,
)
from exoma.config.providers import bundled_providers as _bundled_providers  # noqa: E402
from exoma.config.providers import provider_boundary as _provider_boundary  # noqa: E402
from exoma.core.audit.mcp_scan import (  # noqa: E402,F401
    _classify_mcp,
    _mcp_config_candidates,
    _mcp_reachable,
    _scan_mcp,
)

# Providers whose install reports `not_enforceable`: no read boundary holds, and the adapter
# passes the full environment. Selecting one means trusting it; doctor says so every run.
_UNBOUNDED_READ_PROVIDERS = tuple(
    name for name in _bundled_providers() if _provider_boundary(name)["reads"] == "unbounded"
)


def check_writable(path: Path) -> tuple[bool, str | None]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8"):
            pass
        return True, None
    except OSError as exc:
        return False, str(exc)


def python_callable() -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            [shutil.which("python") or "python", "--version"],
            capture_output=True,
            text=True,
            check=False,
            **osutil.hidden_run_kwargs(),  # Windows: no console flash on readiness probe
        )
    except OSError as exc:
        return False, str(exc)
    output = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode == 0, output


def _free_tier_models(config: dict) -> list[tuple[str, str]]:
    """`(where, model)` for every configured model id that names a free tier.

    Reported, never rewritten. The shape is the evidence: `opencode/mimo-v2.5-free` came
    from a tool-level seed written once and copied into every new project, so a project
    running a `-free` model is usually running a default nobody re-examined. Some are
    deliberate, which is why this is a recommended fix and not an issue.
    """
    found: list[tuple[str, str]] = []
    default = config.get("default_model")
    if isinstance(default, str) and default.lower().endswith("-free"):
        found.append(("default_model", default))
    routes = config.get("routes") if isinstance(config.get("routes"), dict) else {}
    for name, entry in sorted(routes.items()):
        model = entry.get("model") if isinstance(entry, dict) else None
        if isinstance(model, str) and model.lower().endswith("-free") and model != default:
            found.append((f"routes.{name}", model))
    return found


_OLD_GLOBAL_HEADER = "# Claude Code — Personal Global Config (v"


def _claude_md_leftover() -> str | None:
    """~/.claude/CLAUDE.md when it holds the old global header outside the managed block."""
    path = Path(_expand_home("{{HOME}}")) / ".claude" / "CLAUDE.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    while (block := _marker_block(text, "WORKFLOW-MAIN-AGENT:START", "WORKFLOW-MAIN-AGENT:END")):
        text = text.replace(block, "")
    found = any(line.startswith(_OLD_GLOBAL_HEADER) for line in text.splitlines())
    return str(path) if found else None


# What a v3.5.x layout legitimately keeps at the .workflow root, so doctor does not call a
# not-yet-migrated workspace's own data "stray".
_LEGACY_ROOT_ITEMS = frozenset(
    {
        "sessions", "provider-sessions", "reports", "audit.jsonl", "usage.jsonl", "quality.jsonl",
        "redactions.jsonl", "evidence.jsonl", "facts.jsonl", "recurrence-cache.json",
        "capabilities.json", "graph-meta.json", "graph-stale.json", "e2e-knowledge.jsonl",
        "state.json", "scope.json", "command-cache.json", "runtime", "logs",
        "facts.jsonl.lock", "evidence.jsonl.lock", "e2e-knowledge.jsonl.lock", "promote.lock",
    }
)


def run_doctor(
    project_root: Path, provider_command: str, session_id: str | None = None
) -> dict:
    from exoma.core.runtime.config_defaults import validate_config
    from exoma.core.runtime.scripts import script_drift
    from exoma.core.runtime.upgrade import needs_upgrade, resolve_runtime, workspace_versions

    paths = workflow_paths(project_root, session_id)
    issues: list[str] = []
    recommended_fixes: list[str] = []
    checks: dict[str, object] = {}

    checks["project_root_valid"] = project_root.exists()
    if not paths["workflow_dir"].exists():
        issues.append(".workflow directory missing")
        recommended_fixes.append("Run init first")
    else:
        from exoma.core.runtime.migrations import LAYOUT_VERSION, _EDITABLE, _SCRIPT_STEMS, pending_migration
        from exoma.core.workspace.workspace_paths import DATA_DIRNAME, is_legacy_layout

        legacy = is_legacy_layout(project_root)
        staging_name = f"{DATA_DIRNAME}.migrating"
        stray = sorted(
            entry.name
            for entry in paths["workflow_dir"].iterdir()
            if entry.name not in _EDITABLE
            and entry.name != staging_name
            and not (entry.name.partition(".")[0] in _SCRIPT_STEMS and entry.name.partition(".")[2] in ("ps1", "sh"))
            and not (legacy and entry.name in _LEGACY_ROOT_ITEMS)
        )
        pending = None if legacy else pending_migration(project_root)
        staging_left = (paths["workflow_dir"] / staging_name).is_dir()
        checks["workspace_layout"] = {
            "layout_version": 1 if legacy else LAYOUT_VERSION,
            "data_dir": str(paths["data_dir"]),
            "stray_at_root": stray or "none",
            "migration_pending": pending["pending"] if pending else "none",
            "migration_staging_leftover": staging_left,
        }
        if pending:
            # An issue, unlike the legacy layout: an unconverted secrets.json stops every
            # browser run, and stale evidence paths quietly defeat reuse.
            issues.append(f"workspace migration incomplete: still to run {', '.join(pending['pending']) or 'finalize'}")
            recommended_fixes.append("Run `--command upgrade` to resume the migration from the step that failed")
        if staging_left:
            issues.append(f".workflow/{staging_name}/ is left from an interrupted migration")
            recommended_fixes.append(
                f"Run `--command upgrade`: it restores .workflow/{staging_name}/ to the root, or names the "
                "entries that exist in both places for you to reconcile first"
                if legacy
                else f"Run `--command upgrade`: it carries backups-only .workflow/{staging_name}/ into data/, "
                "and otherwise refuses, naming the entries to compare with data/ and reconcile by hand"
            )
        if legacy:
            # A recommendation, not an issue: the runtime reads a v3.5.x layout correctly.
            recommended_fixes.append(
                "Run `--command upgrade` to move .workflow's internal files into .workflow/data/ "
                "(backed up first; config.json becomes overrides only)"
            )
        if stray:
            recommended_fixes.append(
                f"Unrecognised files at the .workflow root: {', '.join(stray)} — nothing reads "
                "them; move or delete them if they are not yours"
            )

    # config.json is static (strict); per-session state/scope/cache are created lazily
    # on first delegated call, so absence is normal, not a failure.
    try:
        checks["config_json_valid"] = paths["config"].exists() and isinstance(
            read_json_file(paths["config"]), dict
        )
    except ValueError as exc:
        checks["config_json_valid"] = False
        issues.append(str(exc))
        recommended_fixes.append(f"Fix invalid JSON at {paths['config']}")

    for key, path in {
        "state_json_valid": paths["state"],
        "scope_json_valid": paths["scope"],
        "command_cache_json_valid": paths["command_cache"],
    }.items():
        if not path.exists():
            checks[key] = "lazy (created on first delegated call)"
            continue
        try:
            checks[key] = isinstance(read_json_file(path), dict)
        except ValueError as exc:
            checks[key] = False
            issues.append(str(exc))
            recommended_fixes.append(f"Fix invalid JSON at {path}")

    runtime_writable, runtime_error = check_writable(paths["runtime_dir"] / ".touch")
    try:
        (paths["runtime_dir"] / ".touch").unlink()
    except FileNotFoundError:
        pass
    checks["runtime_folder_writable"] = runtime_writable
    if runtime_error:
        issues.append(f"runtime folder not writable: {runtime_error}")

    prompt_writable, prompt_error = check_writable(paths["prompt"])
    checks["prompt_writable"] = prompt_writable
    if prompt_error:
        issues.append(f"prompt.txt not writable: {prompt_error}")

    try:
        if paths["prompt_meta"].exists():
            read_json_file(paths["prompt_meta"])
        checks["prompt_meta_valid_or_creatable"] = True
    except ValueError as exc:
        checks["prompt_meta_valid_or_creatable"] = False
        issues.append(str(exc))

    lock_state = "missing"
    if paths["lock"].exists():
        try:
            lock_data = read_json_file(paths["lock"])
            created = datetime.fromisoformat(lock_data.get("created_at"))
            lock_state = (
                "active"
                if datetime.now(timezone.utc) - created
                <= timedelta(seconds=LOCK_TTL_SECONDS)
                else "stale"
            )
        except Exception:
            lock_state = "stale"
    checks["lock_state"] = lock_state
    if lock_state == "stale":
        # "stale" alone is a diagnosis that stops at the adjective: it names the state and
        # leaves the user to guess the remedy. A stale lock blocks every delegated call in
        # this session, so the way out belongs next to the finding, not in the docs.
        recommended_fixes.append(
            f"Session lock older than {LOCK_TTL_SECONDS}s and blocking delegated calls: "
            f"run `--command clean` for this session, or delete {paths['lock']} "
            "once no worker is still running"
        )

    # The graph refresh lock. graphify runs without a time limit (DEC-042), so a hung one
    # holds the graph for as long as its worker lives: readers fall back to no graph leads.
    # Reported, never a readiness issue, because delegated work goes on without the graph.
    from exoma.core.graph import graph_index

    refresh = graph_index.refresh_lock_state(project_root)
    checks["graph_refresh_lock"] = {k: refresh[k] for k in ("state", "age", "beat_age", "pid", "graphify_pid") if k in refresh}
    if refresh["state"] == "active" and refresh.get("age", 0) >= 3600:
        recommended_fixes.append(
            f"A graph refresh has held {refresh['path']} for {refresh['age'] // 60} min (worker pid "
            f"{refresh.get('pid')}, graphify pid {refresh.get('graphify_pid')}); the graph is unread meanwhile. "
            "If graphify is stuck, stop that process; its lock then goes stale and `--command clean` removes it"
        )
    elif refresh["state"] == "stale":
        recommended_fixes.append(
            f"Graph refresh lock {refresh['path']} has no live worker behind it: run `--command clean` to remove it"
        )

    reports_writable, reports_error = check_writable(paths["doctor_report"])
    checks["reports_folder_writable"] = reports_writable
    if reports_error:
        issues.append(f"reports folder not writable: {reports_error}")

    gitignore_ok = False
    gitignore_path = project_root / ".gitignore"
    if gitignore_path.exists():
        gitignore_ok = ".workflow/" in [
            line.strip()
            for line in gitignore_path.read_text(encoding="utf-8").splitlines()
        ]
    checks["root_gitignore_ignores_workflow"] = gitignore_ok
    if not gitignore_ok:
        issues.append("root .gitignore does not ignore .workflow/")
        recommended_fixes.append("Add .workflow/ to root .gitignore or rerun init")

    # --- promote readiness ---
    # Doctor used to answer READY for a workspace where /.promote could not run at all.
    # A readiness report that is silent about a command's preconditions is worse than one
    # that omits the command: the user reads READY and blames the feature.
    from exoma.core.knowledge import schema as knowledge_schema
    from exoma.core.knowledge import store as knowledge_store
    from exoma.core.runtime.config_defaults import knowledge_relevant_limit, production_branch
    from exoma.utils import git as git_util

    try:
        knowledge_dir = knowledge_store.knowledge_dir(project_root)
    except knowledge_store.KnowledgePathError as exc:
        knowledge_dir = None
        checks["knowledge_folder_writable"] = False
        issues.append(f"knowledge folder refused: {exc}")
        recommended_fixes.append("Point policies.knowledge_dir at a directory inside the project")
    if knowledge_dir is None:
        pass
    elif knowledge_dir.exists():
        knowledge_writable, knowledge_error = check_writable(knowledge_dir / ".touch")
        try:
            (knowledge_dir / ".touch").unlink()
        except FileNotFoundError:
            pass
        checks["knowledge_folder_writable"] = knowledge_writable
        if knowledge_error:
            issues.append(f"knowledge folder not writable: {knowledge_error}")
            recommended_fixes.append(f"Make {knowledge_dir} writable, or repoint policies.knowledge_dir")
    else:
        # Absence is the normal state for a project that has never promoted. Reported as
        # lazy rather than missing so it does not read as a fault to fix.
        checks["knowledge_folder_writable"] = "lazy (created on first promotion)"

    expected_branch = production_branch(project_root)
    current_branch = git_util.current_branch(project_root)
    checks["promote_branch"] = {
        "production_branch": expected_branch,
        "current_branch": current_branch,
        "promotable": current_branch == expected_branch,
    }
    if current_branch != expected_branch:
        # NOT an issue. Working on a feature branch is the normal case, and flagging it
        # would make doctor cry wolf on every branch. It is reported so that a later
        # refusal from promote-write is something the user was already told about.
        recommended_fixes.append(
            f"/.promote only writes from {expected_branch!r}"
            + (f"; currently on {current_branch!r}" if current_branch else "; currently on no branch")
        )

    malformed: list[str] = []
    invalid: list[str] = []
    for doc_id in knowledge_store.list_docs(project_root):
        loaded = knowledge_store.load(project_root, doc_id)
        if not loaded.get("ok"):
            malformed.append(doc_id)
            continue
        if knowledge_schema.validate(loaded.get("doc")):
            invalid.append(doc_id)
    checks["knowledge_documents"] = {
        "count": len(knowledge_store.list_docs(project_root)),
        "malformed": malformed,
        "schema_invalid": invalid,
    }
    if malformed or invalid:
        # These are shared, Git-tracked files. A broken one is somebody's merge landing
        # badly, and the next promotion of that subject refuses rather than repairs it.
        issues.append(
            "knowledge documents unreadable or schema-invalid: "
            + ", ".join(sorted(malformed + invalid))
        )
        recommended_fixes.append(
            f"Repair or remove the reported file(s) under {knowledge_dir}"
        )

    checks["knowledge_relevant_limit"] = knowledge_relevant_limit(project_root)

    # The scripts call the running build; what agent-workflow recorded (a checkout's
    # main.py, $AGENT_PATH) is reported under `legacy` and never followed.
    checks["runtime_resolver"] = resolve_runtime(project_root)

    python_ok, python_output = python_callable()
    checks["python_callable"] = {"ok": python_ok, "output": python_output}
    if not python_ok:
        issues.append("python not callable")
        recommended_fixes.append("Ensure python is installed and available in PATH")

    provider_ok, provider_output = provider_callable(provider_command)
    checks["provider_callable"] = {"ok": provider_ok, "output": provider_output}
    if not provider_ok:
        # Named, not hardcoded: reporting "opencode not callable" while the project runs
        # codex sends the reader to install the wrong CLI.
        issues.append(f"{provider_command} not callable")
        recommended_fixes.append(
            f"Ensure the {provider_command} CLI is installed and available in PATH, or "
            "fix provider_command in .workflow/second_agent.json"
        )

    checks["graphify_out_exists"] = (project_root / "graphify-out").exists()

    # Config knob sanity: unknown keys / wrong types silently do nothing. Surfaced as a
    # recommended fix, not an issue — the runtime readers fall back safely, so a typo must
    # not make an otherwise-working workspace report NOT_READY.
    try:
        parsed_config = read_json_file(paths["config"])
    except (OSError, ValueError):
        parsed_config = None
    config_warnings = (
        validate_config(parsed_config) if isinstance(parsed_config, dict) else []
    )
    checks["config_warnings"] = config_warnings or "none"
    if config_warnings:
        recommended_fixes.append(
            f"Review .workflow/config.json: {len(config_warnings)} knob warning(s) "
            "(unknown key or wrong type — silently ignored)"
        )

    # /.verify runs only the test commands the project allowlists. With none listed it runs
    # no test at all and every verify carries that gap, so the user is told here — as a
    # warning, since a project may choose review-only verification.
    from exoma.core.evidence import verify_tests

    test_allowlist = verify_tests.configured(project_root)
    checks["verify_test_commands"] = "configured" if test_allowlist else "empty"
    if not test_allowlist:
        recommended_fixes.append(
            "WARNING: commands.verify_test_commands in .workflow/config.json is empty, so "
            "/.verify runs no test and every verify reports that gap — list the test command "
            'prefixes the runtime may run, e.g. ["python -m pytest", "npm test"]'
        )

    # A pre-marker install wrote the main-agent config as plain text at the top of
    # ~/.claude/CLAUDE.md. Install now owns only the marked block, so that copy is no longer
    # updated or removed by anything: stale instructions read beside the current ones.
    leftover = _claude_md_leftover()
    checks["claude_md_leftover_header"] = leftover or "none"
    if leftover:
        recommended_fixes.append(
            f"WARNING: {leftover} has a '# Claude Code — Personal Global Config (v...' header "
            "outside the WORKFLOW-MAIN-AGENT markers — leftover from an old install. It is "
            "your text now (install never touches it); delete that section by hand"
        )

    # /.verify-browser readiness, metadata only and never an issue: the browser and the
    # app URL are checked by each run's preflight against the URL that run was given, and
    # a project that never opens a browser has nothing to fix here.
    from exoma.core.evidence.e2e.preflight import readiness as e2e_readiness

    e2e = e2e_readiness()
    checks["e2e_readiness"] = e2e
    if e2e["fix"]:
        recommended_fixes.append(e2e["fix"])

    # Version drift: the workspace still works, but its generated scripts and config
    # defaults are the previous build's. Reported as its own status rather than as an
    # issue — calling a working workspace NOT_READY would block flows over staleness.
    checks["workspace_versions"] = workspace_versions(project_root)
    workspace_stale = needs_upgrade(project_root)
    checks["workspace_upgrade_needed"] = workspace_stale
    if workspace_stale:
        recommended_fixes.append(
            "Run `--command upgrade` to regenerate .workflow scripts, restamp config.json and migrate the layout if needed"
        )

    # Script drift: the entry scripts are the only way in, so one that no longer matches the
    # generator routes commands the CLI has since stopped accepting. An issue, not a note —
    # a workspace whose front door rejects its own commands is not ready.
    drifted = script_drift(project_root)
    checks["run_script_drift"] = drifted or "none"
    if drifted:
        issues.append(
            "entry script drift: "
            + ", ".join(f"{d['script']} ({d['state']})" for d in drifted)
        )
        if any(d["state"] != "retired_leftover" for d in drifted):
            recommended_fixes.append(
                "Run `--command upgrade` to rewrite the drifted .workflow entry script(s)"
            )
        # upgrade deletes these, but it cannot always: a read-only mount, a network
        # share, another process holding the handle. Naming the manual route as well
        # keeps a workspace from sitting at NOT_READY with only advice that failed.
        leftovers = [
            d["script"] for d in drifted if d["state"] == "retired_leftover"
        ]
        if leftovers:
            recommended_fixes.append(
                "Run `--command upgrade` to remove the retired PowerShell "
                f"script(s): {', '.join(f'.workflow/{name}' for name in leftovers)} "
                "— delete them by hand if upgrade has already run and they remain"
            )

    try:
        from exoma.config.settings import resolve_provider_config_for
        from exoma.core.workspace.workspace_paths import PROVIDER_CONFIG_NAME

        resolved = resolve_provider_config_for(project_root)
        provider_config = {
            "source": resolved.get("source"),
            "path": resolved.get("path"),
            "default_model": resolved.get("config", {}).get("default_model"),
        }
        if resolved.get("error"):
            provider_config["error"] = resolved["error"]
        provider_warnings = list(resolved.get("warnings") or [])
        free_models = _free_tier_models(resolved.get("config") or {})
        if free_models:
            provider_config["free_tier_models"] = free_models
            provider_warnings.append(
                "free-tier model(s) selected: "
                + ", ".join(f"{where}={model}" for where, model in free_models)
                + " — often an old seed default rather than a choice; re-pick with /.provider"
            )
        provider_config["warnings"] = provider_warnings or "none"
        checks["provider_config"] = provider_config
        if provider_warnings:
            # A recommended fix, not an issue: the file parsed and its valid keys applied.
            # Only the flagged knobs are inert, so this must not read as NOT_READY.
            recommended_fixes.append(
                f"Review {resolved.get('path')}: {len(provider_warnings)} knob "
                f"warning(s) — {'; '.join(provider_warnings)}"
            )

        # The read boundary is opencode's alone: codex and agy report `not_enforceable` at
        # install (adapters/install/{codex,agy}_install.py), and they inherit the full process
        # environment. Choosing one is accepted as trusting it with every file and env var —
        # a warning the user must see here, not a NOT_READY, because the choice is theirs.
        active = str((resolved.get("config") or {}).get("provider") or "")
        if active in _UNBOUNDED_READ_PROVIDERS:
            # Name the files, not only the risk: a warning that reads the same in a repo
            # with no secrets and in one holding three `.env` files gets skimmed in both.
            # Names only — `find_secret_files` never opens a file.
            from exoma.core.policy.secret_patterns import find_secret_files

            secrets = find_secret_files(project_root)
            checks["second_agent_read_boundary"] = {
                "provider": active,
                "status": "not_enforceable",
                "trusted": True,
                "secret_files": {
                    "count": len(secrets["files"]),
                    "sample": secrets["files"][:10],
                    "truncated": secrets["truncated"],
                },
            }
            if secrets["files"]:
                shown = ", ".join(secrets["files"][:5])
                more = len(secrets["files"]) - 5
                found = f"{shown}{f' and {more} more' if more > 0 else ''}"
                exposure = (
                    f"and {len(secrets['files'])} file(s) here match the secret patterns "
                    f"({found})"
                )
            else:
                exposure = "although no file here matches the secret patterns"
            recommended_fixes.append(
                f"WARNING: second_agent provider '{active}' can read every file in this project "
                f"(`.env` and key files included), {exposure}, and receives the full "
                "environment — it is treated as a trusted provider. Switch to opencode with "
                "/.provider if the project's secrets must stay unreadable to the second agent"
            )
        elif active:
            checks["second_agent_read_boundary"] = {"provider": active, "status": "enforceable"}
        if active:
            # The write side, apart from the read side: codex reads everything but its
            # sandbox stops writes; agy stops neither, and `agy_guard` only reports a write
            # after it happened. Trusting a reader is not the same as trusting a writer.
            writes = _provider_boundary(active)["writes"]
            checks["second_agent_write_boundary"] = {"provider": active, "writes": writes}
            if writes == "unbounded":
                recommended_fixes.append(
                    f"WARNING: second_agent provider '{active}' can WRITE to this project — "
                    "no sandbox or permission map stops it, and the working-tree guard only "
                    "detects a write after it happened. Review `git status` after delegated "
                    "calls, or switch to codex/opencode with /.provider"
                )
        from exoma.config.settings import legacy_env_in_use

        legacy = legacy_env_in_use()
        if legacy:
            checks["legacy_env"] = legacy
            recommended_fixes.append(
                "Rename the pre-rename environment variable(s) "
                + ", ".join(legacy)
                + " to their EXOMA_* spelling; they are still read, and EXOMA_* wins when both are set"
            )
        if active:
            # Which provider CLI release this project runs, against the release this workflow
            # version was tested on. codex's Windows sandbox broke between releases (CASE-015);
            # an untested release is a warning, never an issue (core/provider/versions.py).
            from exoma.config.providers import PROVIDER_BUNDLES
            from exoma.core.provider import versions

            command = (resolved.get("config") or {}).get("provider_command") or (
                PROVIDER_BUNDLES.get(active, {}).get("default_command") or active
            )
            found = versions.describe(active, str(command))
            checks["provider_version"] = {
                "provider": active, "version": found["version"] or "unreadable",
                "status": found["status"], "stable": found["stable"],
            }
            if found["version"] is None:
                recommended_fixes.append(
                    f"`{command} --version` did not answer; check that the {active} CLI on PATH runs"
                )
            elif found["status"] == "untested" and found["stable"]:
                recommended_fixes.append(
                    f"WARNING: {active} {versions.release(found['version'])} is not the release this "
                    f"workflow version was tested on ({found['stable']}). It still runs; if delegated "
                    f"calls fail, pin {active} back to {found['stable']} (see docs/reference.md, "
                    "\"Provider releases\")"
                )

        project_file = paths["workflow_dir"] / PROVIDER_CONFIG_NAME
        if resolved.get("error"):
            issues.append(
                f"provider config unreadable: {resolved.get('path')} — {resolved['error']}; "
                "delegated commands are refused until it parses (provider_config_invalid)"
            )
            recommended_fixes.append(
                f"Fix the JSON in {resolved.get('path')}, or delete it and run --command init"
            )
        elif resolved.get("source") == "missing":
            issues.append(
                f"provider config missing: {resolved.get('path')} — delegated commands are "
                "refused (provider_config_missing); there is no machine-wide fallback"
            )
            recommended_fixes.append(
                "Run --command init to create it from the tool seed, then /.provider to "
                "choose provider and model"
            )
        elif project_file.exists() and resolved.get("source") != "project":
            issues.append(
                f"provider config ignored: {project_file} exists but the runtime resolved "
                f"'{resolved.get('source')}' ({resolved.get('path')}) — model/timeout tuning "
                "in this project is not being applied"
            )
            recommended_fixes.append(
                "Provider config resolution is picking the wrong file — report this; "
                "as a stopgap, tune the resolved file instead"
            )
    except Exception as exc:
        checks["provider_config"] = f"SKIPPED — {exc}"

    # second_agent MCP safety: enumerate opencode MCP servers, flag any that exceed
    # the read-only evidence role (write/exec/fs/db/browser/etc).
    mcp = _scan_mcp(project_root)
    checks["mcp_second_agent"] = mcp
    for entry in mcp.get("unreadable") or []:
        issues.append(
            f"provider config unreadable: {entry['path']} — {entry['error']}; the MCP list "
            "above is built without it (a sandboxed host denies files outside the workspace, "
            "or the file is not UTF-8)"
        )
        recommended_fixes.append(
            f"Run doctor where {entry['path']} can be read (outside the host's sandbox), "
            "grant read access to it, or save it as UTF-8"
        )
    active_risky = [
        s["name"]
        for s in mcp["servers"]
        if s["enabled"] and s["classification"] == "risk"
    ]
    active_unknown = [
        s["name"]
        for s in mcp["servers"]
        if s["enabled"] and s["classification"] == "unknown"
    ]
    if active_risky:
        issues.append(
            f"second_agent MCP risk: {', '.join(active_risky)} — write/exec-capable, exceeds read-only role"
        )
        recommended_fixes.append(
            f"Disable write/exec-capable MCP for {provider_command} (second_agent = read-only evidence), or confirm intended"
        )
    if active_unknown:
        issues.append(
            f"second_agent MCP unknown: {', '.join(active_unknown)} — capability unverified for read-only safety"
        )
        recommended_fixes.append(
            "Review the flagged MCP server(s); ensure second_agent stays read-only"
        )
    # Permitted DB/data-inspection servers (laravel-boost family) — reported, not flagged:
    # second_agent MAY use these for read-only DB evidence.
    active_inspect = [
        s["name"]
        for s in mcp["servers"]
        if s["enabled"] and s["classification"] == "inspect"
    ]
    if active_inspect:
        checks["mcp_inspect_permitted"] = active_inspect
    # Liveness: an enabled server whose launch command is missing can never answer.
    unreachable = [
        s["name"]
        for s in mcp["servers"]
        if s["enabled"] and s.get("reachable") is False
    ]
    if unreachable:
        issues.append(
            f"second_agent MCP unreachable: {', '.join(unreachable)} — declared but launch command not on PATH"
        )
        recommended_fixes.append(
            f"Install/fix the server command, or remove the dead MCP entry from the {provider_command} config"
        )

    # Session continuation: is the current main session linked to a thread of the provider
    # that is selected NOW? An unlinked session re-bootstraps every call (breaks 1 main =
    # 1 second); a thread issued by another provider cannot be resumed at all.
    import re as _re
    from exoma.config.settings import SESSION_DIR, resolve_provider_config_for

    try:
        active_provider = (resolve_provider_config_for(project_root).get("config") or {}).get("provider")
    except Exception:
        active_provider = None

    session_id = None
    try:
        session_block = read_json_file(paths["state"]).get("session")
        if isinstance(session_block, dict):
            session_id = session_block.get("id")
    except (ValueError, OSError):
        session_id = None

    if not session_id:
        checks["session_continuation"] = "no active session (state.json)"
    else:
        safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
        # The runtime reads the project-local store (`main._session_manager_for`); the
        # global SESSION_DIR is only its fallback. Checking the fallback alone reported a
        # healthy session as BROKEN and a broken one as linked.
        candidates = [
            workflow_paths(Path(project_root))["provider_sessions_dir"] / f"{safe}.json",
            Path(SESSION_DIR) / f"{safe}.json",
        ]
        session_file = next((path for path in candidates if path.exists()), None)
        if session_file is None:
            checks["session_continuation"] = (
                f"no session record for {session_id} (first delegated call will bootstrap)"
            )
        else:
            try:
                record = read_json_file(session_file)
            except (ValueError, OSError):
                record = {}
            threads = record.get("provider_sessions")
            if isinstance(threads, dict) and active_provider:
                linked_id = threads.get(active_provider)
            else:
                linked_id = record.get("provider_session_id")
            if linked_id:
                checks["session_continuation"] = (
                    f"linked: {session_id} -> {active_provider or 'provider'} {linked_id}"
                )
            elif isinstance(threads, dict) and threads:
                # Not broken: the next call on this provider bootstraps its own thread and
                # the other providers' threads stay resumable for when they are selected.
                checks["session_continuation"] = (
                    f"no {active_provider} thread yet for {session_id} "
                    f"(threads held for: {', '.join(sorted(threads))}); next call bootstraps one"
                )
            else:
                checks["session_continuation"] = (
                    f"BROKEN: {session_id} has no provider_session_id — continuation re-bootstraps each call"
                )
                issues.append(
                    "session continuation broken: provider_session_id not captured for active session"
                )
                recommended_fixes.append(
                    f"Re-run a delegated command; if it keeps failing, {provider_command} session capture is failing (check its output for a session id)"
                )

    # Release integrity: the installed bundle must match the package's _bundle/manifest.json
    # exactly, the manifest must not be older than its sources, and required hooks must be
    # installed. A drifted/stale/incomplete bundle still "runs" but ships behaviour nobody
    # reviewed.
    from exoma.config.settings import BUNDLE_CONFIG_DIR, BUNDLE_MANIFEST

    integrity: object = _bundle_integrity(BUNDLE_CONFIG_DIR, BUNDLE_MANIFEST, project_root)
    checks["bundle_integrity"] = integrity
    if isinstance(integrity, dict):
        if integrity.get("error"):
            issues.append(f"bundle integrity uncheckable: {integrity['error']}")
            recommended_fixes.append(
                "Regenerate the manifest: python tools/maintain/gen_manifest.py"
            )
        # Name the file AND why it drifted. "2 files differ" is the same sentence whether
        # the user edited a block on purpose or an install never ran, and those need
        # opposite responses.
        drift_by_reason: dict[str, list[str]] = {}
        for item in integrity.get("drift") or []:
            drift_by_reason.setdefault(item["reason"], []).append(item["path"])
        for reason, paths_ in sorted(drift_by_reason.items()):
            issues.append(
                f"bundle {reason}: {len(paths_)} file(s) — {', '.join(paths_[:5])}"
            )
        if "locally_edited" in drift_by_reason or "content_differs" in drift_by_reason:
            recommended_fixes.append(
                "Re-run `exoma install --apply` to reinstall the shipped bundle; if the bundle "
                "changed on purpose, regenerate the manifest (python tools/maintain/gen_manifest.py)"
            )
        if "not_installed" in drift_by_reason:
            recommended_fixes.append("Re-run install to place the missing bundle files")
        if integrity.get("manifest_fresh") is False:
            issues.append(
                "stale manifest: bundle sources are newer than _bundle/manifest.json"
            )
            recommended_fixes.append("Run: python tools/maintain/gen_manifest.py")
        if integrity.get("hooks_installed") is False:
            issues.append(
                "required hooks missing from install "
                "(session-bind/intent-gate-set/intent-gate-check)"
            )
            recommended_fixes.append("Re-run install to place the hook scripts")

    # Each chosen host's own files (`both` reports both). Loading them is the host's to
    # prove: the Agy /.doctor skill asks the model in that session to look.
    hosts = _host_status()
    checks["hosts"] = hosts
    for host, state in hosts["hosts"].items():
        if state["missing"]:
            issues.append(f"host {host}: missing {', '.join(state['missing'][:5])}")
            recommended_fixes.append(f"Re-run `exoma install --apply` to restore the {host} host's files")

    if issues:
        status = "NOT_READY"
    elif workspace_stale:
        status = "NEEDS_UPGRADE"
    else:
        status = "READY"
    # `checked_at` is the only field that changes between two runs over identical state.
    # Kept out of `result` so `doctor` twice can be diffed directly — with it inline, every
    # comparison showed a difference and proved nothing.
    payload = {
        "meta": {"checked_at": now_iso()},
        "result": {
            "status": status,
            "project_root": str(project_root),
            "issues": issues,
            "recommended_fixes": recommended_fixes,
            "checks": checks,
        },
        # Flat copies kept so existing readers of doctor.json keep working.
        "status": status,
        "checked_at": now_iso(),
        "project_root": str(project_root),
        "issues": issues,
        "recommended_fixes": recommended_fixes,
        "checks": checks,
    }
    atomic_write_json(paths["doctor_report"], payload)
    return {
        # NEEDS_UPGRADE is still ok: the workspace runs, it is just built by an older
        # build. Only real issues make doctor fail.
        "ok": status != "NOT_READY",
        "content": f"{status}: {len(issues)} issue(s), {len(recommended_fixes)} recommended fix(es)",
        "meta": {
            "status": status,
            "issues": issues,
            "recommended_fixes": recommended_fixes,
            "doctor_report": str(paths["doctor_report"]),
            "project_root": str(project_root),
        },
    }


def run_sweep(project_root: Path, session_id: str | None = None) -> dict:
    from exoma.core.runtime.state import load_workspace_state, update_command_cache

    paths = workflow_paths(project_root, session_id)
    changed_files: list[str] = []
    diff_summary = ""

    def git_error(detail: str, operation: str) -> dict:
        from exoma.core.evidence.contract import make_error

        return make_error(
            "sweep_git_error",
            detail or f"{operation} failed",
            next_action=(
                "Run the reported Git command in the project, fix the repository or "
                "Git installation, then retry sweep."
            ),
            meta={"project_root": str(project_root), "operation": operation},
        )

    def git_run(argv: list[str]):
        return subprocess.run(
            argv,
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
            **osutil.hidden_run_kwargs(),
        )

    try:
        name_results = [
            ("unstaged diff", git_run(["git", "diff", "--name-only"])),
            ("staged diff", git_run(["git", "diff", "--cached", "--name-only"])),
            (
                "untracked files",
                git_run(["git", "ls-files", "--others", "--exclude-standard"]),
            ),
        ]
        for operation, result in name_results:
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                return git_error(detail, operation)
        changed_files = list(
            dict.fromkeys(
                line.strip()
                for _operation, result in name_results
                for line in result.stdout.splitlines()
                if line.strip()
            )
        )
        stat_results = [
            ("unstaged stat", git_run(["git", "diff", "--stat"])),
            ("staged stat", git_run(["git", "diff", "--cached", "--stat"])),
        ]
        for operation, result in stat_results:
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                return git_error(detail, operation)
        diff_summary = "\n".join(
            result.stdout.strip()
            for _operation, result in stat_results
            if result.stdout.strip()
        )
    except OSError as exc:
        return git_error(str(exc), "launch git")

    loaded = load_workspace_state(project_root, session_id)
    scope = loaded["scope"]
    impact_radius = scope.get("impact_radius") or []
    risk_hits = []
    for file_path in changed_files:
        lower = file_path.lower()
        if any(
            token in lower
            for token in (
                "config",
                "auth",
                "payment",
                "schema",
                "migration",
                ".env",
                "secret",
                "credential",
            )
        ):
            risk_hits.append(file_path)
        if impact_radius and any(
            target and target in file_path for target in impact_radius
        ):
            risk_hits.append(file_path)

    if not changed_files:
        verdict = "skipped"
        reason = "no file changes detected"
    elif risk_hits:
        verdict = "repair_required"
        reason = f"risk indicators found in {len(set(risk_hits))} changed file(s)"
    else:
        verdict = "pass"
        reason = "no obvious impact issues detected"

    lines = [
        f"# Sweep Report",
        "",
        f"- verdict: {verdict}",
        f"- reason: {reason}",
        f"- checked_at: {now_iso()}",
        "",
        "## Changed Files",
    ]
    if changed_files:
        lines.extend(f"- {item}" for item in changed_files)
    else:
        lines.append("- none")
    lines.extend(["", "## Diff Summary", diff_summary or "(empty)"])
    if impact_radius:
        lines.extend(
            ["", "## Scope Impact Radius", *[f"- {item}" for item in impact_radius]]
        )
    if risk_hits:
        lines.extend(
            ["", "## Risk Signals", *[f"- {item}" for item in sorted(set(risk_hits))]]
        )
    report = "\n".join(lines).strip() + "\n"
    atomic_write_text(paths["sweep_report"], report)
    update_command_cache(
        project_root,
        "last_sweep_result",
        {
            "verdict": verdict,
            "reason": reason,
            "changed_files": changed_files,
            "diff_summary": diff_summary,
        },
        (loaded["state"].get("session") or {}).get("id"),
    )
    return {
        "ok": True,
        "content": (
            f"sweep {verdict}: {reason}; " f"{len(changed_files)} changed file(s)"
        ),
        "meta": {
            "verdict": verdict,
            "reason": reason,
            "changed_files": changed_files,
            "report": str(paths["sweep_report"]),
            "project_root": str(project_root),
        },
    }


def prune_sessions(project_root: Path, ttl_days: int = 7, keep_last: int = 20) -> dict:
    """Delete per-session dirs older than ttl_days, always keeping the newest keep_last.
    Recent (active) sessions survive the TTL, so this never reaps a live session."""
    paths = workflow_paths(project_root)
    sessions_dir = paths["sessions_dir"]
    provider_dir = paths["provider_sessions_dir"]
    if not sessions_dir.exists() and not provider_dir.exists():
        return {"removed": 0, "kept": 0}
    dirs = sorted(
        (
            (p for p in sessions_dir.iterdir() if p.is_dir())
            if sessions_dir.exists()
            else ()
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    removed = 0
    for index, path in enumerate(dirs):
        if index < keep_last:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    provider_files = sorted(
        (
            (p for p in provider_dir.glob("*.json") if p.is_file())
            if provider_dir.exists()
            else ()
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    provider_removed = 0
    for index, path in enumerate(provider_files):
        if index < keep_last:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                path.unlink()
                provider_removed += 1
        except OSError:
            continue
    return {
        "removed": removed + provider_removed,
        "kept": min(len(dirs), keep_last) + min(len(provider_files), keep_last),
    }
