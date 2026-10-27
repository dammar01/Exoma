"""Install the agent-workflow config onto this machine.

Consumes `dist/` (produced by tools/maintain/extract_config.py) and applies it to the local
agent directories.

    python install.py              # DRY RUN — show every change, write nothing
    python install.py --apply      # actually write

Project scope is detected, not passed: run it from anywhere inside a directory that has a
.workflow/ and that workspace is refreshed in place too (scripts regenerated, new config
keys backfilled, opencode.json boundary re-enforced, sessions/ untouched). Scaffolding a
NEW workspace belongs to init — `python main.py --command init --work-dir DIR` — so there
is one bootstrap path rather than a flag that half-duplicates it.

Dry run is the default on purpose. This writes into the user's global agent config,
which every project on the machine reads; a mistake here is not contained to one repo.

Safety:
- everything it would overwrite is backed up first, under a timestamped folder
- managed blocks are replaced BETWEEN markers, so hand-written config around them survives
- settings.json gains missing keys and refreshes only workflow-owned hook entries;
  unrelated user hooks and values are preserved
- rollback verifies destination and backup hashes before restoring or deleting anything
"""

import argparse
import json
import os
import shutil
import sys

# Before any import that the newer syntax would break: the oldest supported Python is
# utils/osutil.py MIN_PYTHON, repeated here because importing it would fail on the very
# interpreter this names.
if sys.version_info < (3, 10):
    sys.exit(
        f"Exoma needs Python 3.10 or newer; this is {sys.version.split()[0]} "
        f"({sys.executable}). Run it with a newer python."
    )

from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from exoma.utils import osutil  # noqa: E402

# Split out in v3.4.3; re-exported so `main()` below and any external caller keep
# addressing install.py exactly as before.
from exoma.installer.base import (  # noqa: E402,F401
    DIST,
    DIST_CONFIG,
    HOME,
    MANIFEST,
    MARKERS,
    SETTINGS_REQUIRED,
    Plan,
    _RECEIPT,
    _begin_receipt,
    _discard_empty_receipt,
    _flush_receipt,
    _reset_receipt,
    _apply_intent_mode,
    _backup,
    _env_values,
    _file_sha256,
    _hash,
    _install_text,
    _load_env_file,
    _managed_block,
    _merge_managed,
    _read_text_lenient,
    _record,
    _resolve_in_json,
    _resolve_placeholders,
    _scan_missing_env,
    _strip_section,
    _targets,
)

from exoma.config.providers import PROVIDER_BUNDLES  # noqa: E402
from exoma.config.settings import PROVIDER_EXAMPLE_FILE, PROVIDER_SEED_FILE  # noqa: E402
from exoma.installer.check import (  # noqa: E402,F401
    _bundle_stale,
    _detect_project_root,
    _provider_config_would_change,
    _run_check,
    _settings_would_change,
)
from exoma.installer.rollback import (  # noqa: E402,F401
    _backup_dirs,
    _migrated_hosts,
    _run_rollback,
    _store_install_mode,
    _stored_agy_permissions,
    _stored_hosts,
    _stored_only_command,
)
from exoma.config.bundle_keys import provider_dir  # noqa: E402
from exoma.installer.base import (  # noqa: E402
    CLAUDE_SETTINGS,
    HOSTS,
    _install_agy_permissions,
    agy_host_present,
)
from exoma.installer.settings import (  # noqa: E402,F401
    _drop_intent_hook,
    _hook_script_ids,
    _install_deps,
    _pin_python,
    _install_provider_config,
    _install_settings,
    _merge_hook_entries,
    _provider_config_path,
    _remove_intent_hook_entries,
    _render_hook_commands,
    hook_bash_command,
)



# Where the provider seed is written, and the shipped example it is built from. Module
# globals so the installer tests can point them at a temp directory.
SEED_DIR = PROVIDER_SEED_FILE.parent
EXAMPLE_FILE = PROVIDER_EXAMPLE_FILE


def _second_agent_config(
    template: dict,
    provider: str,
    model: str | None,
    route_models: dict | None = None,
) -> dict:
    """The example config with one provider and model selected into it.

    Built ON the example rather than from scratch so every key the example gains later —
    timeouts, probe windows — is inherited by a seeded file instead of silently missing.
    Only the keys a selection owns are replaced; `_merge_routes` edits `model` inside each
    route entry rather than replacing the entry, because a route also carries
    `timeout_seconds` and `agent`. `route_models` carries per-route picks forward from the
    previous seed; a route it does not name follows `model`.
    """
    from exoma.config.providers import provider_agent_default, provider_command_default
    from exoma.core.provider.provider_select import SELECTABLE_ROUTES, _merge_routes

    config = dict(template)
    config["provider"] = provider
    config["provider_command"] = provider_command_default(provider, os.getenv)
    config["default_model"] = model
    picks = {name: model for name in SELECTABLE_ROUTES}
    for name, value in (route_models or {}).items():
        if name in picks and value:
            picks[name] = value
    config["routes"] = _merge_routes(template.get("routes"), picks)
    agent = provider_agent_default(provider, os.getenv)
    if agent:
        config["provider_agent"] = agent
    return config


def _seed_selection(path: Path) -> dict | None:
    """`{provider, model, routes}` from an existing seed, or None when there is none to keep.

    Only the SELECTION is read. Everything else in the file is rebuilt from the example, so
    a key the release changed can never be carried forward from an older seed.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("provider"), str):
        return None
    routes = loaded.get("routes") if isinstance(loaded.get("routes"), dict) else {}
    model = loaded.get("default_model")
    return {
        "provider": loaded["provider"],
        "model": model if isinstance(model, str) else None,
        "routes": {
            name: entry.get("model")
            for name, entry in routes.items()
            if isinstance(entry, dict) and isinstance(entry.get("model"), str)
        },
    }


def _ask(prompt: str) -> str:
    """One line from the operator, or "" when the stream ends or they interrupt."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _prompt_second_agent() -> tuple[str | None, str | None]:
    """Ask which second agent to use. Returns (provider, model); (None, None) to skip.

    Only reached on --apply from a terminal — see `_seed_second_agent_config`. Skipping is
    a first-class answer at both steps: an empty provider seeds the example unchanged, and
    an empty model leaves the pin unset so the provider CLI keeps its own default.
    """
    from exoma.config.providers import (
        bundled_providers,
        provider_boundary,
        provider_command_default,
        provider_models,
    )

    providers = sorted(bundled_providers())
    print()
    print("  config/second_agent.seed.json has no selection yet. Which second agent should new projects use?")
    for index, name in enumerate(providers, start=1):
        command = provider_command_default(name, os.getenv)
        notes = ["on PATH" if shutil.which(command) else f"{command} not on PATH"]
        if provider_boundary(name)["reads"] == "unbounded":
            notes.append("no enforced read boundary — trusted provider")
        print(f"    {index}) {name:<10} ({'; '.join(notes)})")
    choice = _ask(f"  provider [1-{len(providers)}, blank = keep the example default]: ")
    if not choice.isdigit() or not 1 <= int(choice) <= len(providers):
        return None, None
    provider = providers[int(choice) - 1]

    models = provider_models(provider)
    if not models:
        return provider, None
    print()
    for index, entry in enumerate(models, start=1):
        efforts = ", ".join(entry.get("efforts") or ()) or "no reasoning effort"
        print(f"    {index}) {entry['id']:<42} ({efforts})")
    choice = _ask(
        f"  model [1-{len(models)}, or a model id, blank = let the CLI decide]: "
    )
    if not choice:
        return provider, None
    if choice.isdigit() and 1 <= int(choice) <= len(models):
        return provider, models[int(choice) - 1]["id"]
    # A pin the shortlist never mentioned still has to work: the menu is a picker, not
    # the set of models that exist (config/providers.model_is_listed).
    return provider, choice


def _seed_second_agent_config(
    plan, apply: bool, provider: str | None = None, model: str | None = None
) -> None:
    """Rebuild config/second_agent.seed.json, the template every `init` copies.

    Rebuilt on EVERY run, from the shipped example, so the seed can never outlive the
    release that wrote it. Its predecessor, config/second_agent.json, was created once and
    left alone forever after; init copied it into each new project, and one pick made on
    one day — `opencode/mimo-v2.5-free` — kept arriving in projects nobody chose it for.

    The selection the seed carries (provider, default_model, per-route models) comes from,
    in order:
      --provider/--model     taken as given;
      the previous seed      the last deliberate choice, re-applied to the new example;
      a terminal on --apply  asked interactively, with skipping as a valid answer;
      anything else          the example, verbatim.

    The old file is retired rather than read: it is exactly the state that went stale.
    It is renamed aside and its selection printed, so re-choosing it is one flag away.

    Nothing here touches a project's own .workflow/second_agent.json — that is the user's.

    Deliberately NOT recorded in the install receipt: a receipt entry without a backup is
    DELETED on rollback, and the seed is the user's selection, not an installed file.
    """
    from exoma.config.providers import (
        model_is_listed,
        provider_boundary_warning,
    )
    from exoma.core.workspace.workspace_paths import (
        JSON_INDENT,
        PROVIDER_CONFIG_NAME,
        PROVIDER_SEED_NAME,
    )

    dest = SEED_DIR / PROVIDER_SEED_NAME
    legacy = SEED_DIR / PROVIDER_CONFIG_NAME
    src = EXAMPLE_FILE
    if not src.exists():
        plan.warn(f"cannot seed {dest.name}: {src} is missing from the package")
        return
    template = json.loads(src.read_text(encoding="utf-8"))

    if legacy.exists():
        retired = legacy.with_name(f"{legacy.name}.retired")
        old = _seed_selection(legacy)
        described = (
            f"provider={old['provider']}, model={old['model'] or 'unset'}"
            if old
            else "unreadable"
        )
        plan.add(
            "retire",
            legacy,
            f"renamed to {retired.name}; its selection ({described}) is not carried into "
            f"{dest.name}",
        )
        plan.warn(
            f"{legacy.name} is retired: init now copies {dest.name}. Its selection "
            f"({described}) was not carried over — re-select with --provider/--model if "
            "you still want it. Existing projects keep their own .workflow/second_agent.json."
        )
        if apply:
            os.replace(legacy, retired)

    route_models: dict = {}
    previous = _seed_selection(dest)
    if provider is None and previous is not None:
        provider, model, route_models = previous["provider"], previous["model"], previous["routes"]
    elif provider is None:
        if apply and sys.stdin.isatty():
            provider, model = _prompt_second_agent()
        elif not apply:
            plan.add(
                "create",
                dest,
                "rebuilt from the example; --apply from a terminal asks which provider "
                "and model, or pass --provider/--model",
            )
            return

    if provider is None:
        detail = f"from {src.name} (provider=opencode, model unset; pass --provider to choose)"
        payload = src.read_text(encoding="utf-8")
    else:
        boundary_warning = provider_boundary_warning(provider)
        if boundary_warning:
            plan.warn(boundary_warning)
        if model and not model_is_listed(provider, model):
            plan.warn(
                f"{model!r} is not on {provider}'s shortlist — pinned anyway, but "
                "verify the id is one the CLI accepts"
            )
        if not model and provider == "opencode":
            plan.warn(
                "opencode without a model is refused at call time (model_unset): without "
                "-m it would answer with whatever model it used last. Pass --model."
            )
        detail = f"provider={provider}, model={model or 'unset'}"
        payload = (
            json.dumps(
                _second_agent_config(template, provider, model, route_models),
                indent=JSON_INDENT,
            )
            + "\n"
        )

    current = dest.read_text(encoding="utf-8") if dest.exists() else None
    if current == payload:
        plan.add("same", dest, detail)
        return
    plan.add("replace" if current is not None else "create", dest, detail)
    if apply:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(payload, encoding="utf-8")


_HOST_CHOICES = {"claude": ("claude",), "agy": ("agy",), "both": HOSTS}


def _prompt_hosts() -> tuple[str, ...]:
    """Ask which main-agent host to install. Only reached on --apply from a terminal."""
    print()
    print("  Which main agent should Exoma install for?")
    print("    1) claude   (Claude Code: ~/.claude)")
    print(f"    2) agy      (Antigravity CLI: ~/.gemini{'' if agy_host_present() else ', not found on this machine'})")
    print("    3) both")
    choice = _ask("  host [1-3, blank = claude]: ")
    return {"2": ("agy",), "3": HOSTS}.get(choice, ("claude",))


def _prompt_agy_permissions() -> bool:
    print()
    print("  Agy asks before every runner call unless a rule allows it. Exoma can add two rules")
    print("  to ~/.gemini/antigravity-cli/settings.json: the .workflow runner through this")
    print("  machine's bash, and writes to Exoma's data directory. They apply to every Agy")
    print("  session; --uninstall removes them, and your own rules are never touched.")
    return _ask("  add them? [y/N]: ").lower() in ("y", "yes")


def _resolve_hosts(flag: str | None, apply: bool, plan: Plan) -> tuple[str, ...]:
    """--host, else the last install's choice, else the 0.3.0 migration, else asked on
    --apply from a terminal, else Claude — what every install did before the choice."""
    if flag:
        return _HOST_CHOICES[flag]
    hosts = _stored_hosts() or _migrated_hosts()
    if hosts:
        return hosts
    if apply and sys.stdin.isatty():
        return _prompt_hosts()
    plan.warn("no host chosen yet: installing for claude; --apply from a terminal asks, or pass --host")
    return ("claude",)


def _resolve_agy_permissions(flag: bool | None, hosts: tuple[str, ...], apply: bool) -> bool:
    """--agy-permissions / --no-agy-permissions, else the last choice, else asked on --apply
    from a terminal, else off. Opt-in: the rules reach every Agy session."""
    if "agy" not in hosts:
        return False
    if flag is not None:
        return flag
    stored, _rules = _stored_agy_permissions()
    if stored is not None:
        return stored
    if apply and sys.stdin.isatty():
        return _prompt_agy_permissions()
    return False


def _update_providers(plan: Plan, apply: bool, backup_root: Path) -> None:
    """--update-providers: each provider CLI to its `stable_version`, npm-global installs
    only (installer/provider_update.py, DEC-057). After every file write, and outside the
    receipt: --rollback restores files, and a provider release is not one."""
    from exoma.installer.provider_update import apply_updates, plan_updates, write_journal

    entries = plan_updates()
    if apply and any(e["action"] == "update" for e in entries):
        apply_updates(entries)
        plan.add("journal", write_journal(entries, backup_root), "provider releases before and after")
    for e in entries:
        target = f"provider {e['provider']}"
        result = e.get("result")
        if e["action"] == "current":
            plan.add("current", target, e["reason"])
        elif e["action"] != "update":
            plan.warn(f"{e['provider']} not updated ({e['action']}): {e['reason']}")
        elif result is None:
            plan.add("update", target, f"{e['reason']} ({' '.join(e['argv'][1:])})")
        elif result == "updated":
            plan.add("updated", target, e["reason"])
        else:
            back = f"; pin back: {e['pin_back']}" if e["pin_back"] else ""
            plan.warn(
                f"{e['provider']} update failed (rc={e['returncode']}, now "
                f"{e['after'] or 'unreadable'}): {e.get('error') or 'no output'}{back}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(prog="exoma install", description="Install the Exoma bundle into ~/.claude and the provider configs")
    parser.add_argument(
        "--apply", action="store_true", help="write changes (default: dry run)"
    )
    parser.add_argument(
        "--only-command",
        action="store_true",
        help="install the main-agent block WITHOUT natural-language auto-intent: commands "
        "must be invoked by their /. prefix, and the UserPromptSubmit intent hook is not "
        "registered",
    )
    parser.add_argument(
        "--auto-intent",
        action="store_true",
        help="undo --only-command: restore natural-language auto-intent on the next install",
    )
    parser.add_argument(
        "--rollback",
        nargs="?",
        const="",
        metavar="BACKUP_ID",
        help="undo an install from its receipt (default: the most recent). Dry run unless "
        "--apply is also given",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="remove what the workflow installed: its block in CLAUDE.md and each provider "
        "AGENTS.md (your own text stays), unedited skills/commands/hooks/agents, and its "
        "settings.json hooks and statusLine. Receipted, so --rollback undoes it. Dry run "
        "unless --apply is also given",
    )
    parser.add_argument(
        "--with-e2e",
        action="store_true",
        help="also install the optional browser-verification extra: pip install -r "
        "the bundled requirements-e2e.txt, then python -m playwright install chromium (large download). "
        "Only needed for /.verify-browser. Dry run unless --apply is also given",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDER_BUNDLES),
        help="which second agent the user's second_agent.seed.json (the template new projects "
        "copy) should select. Without it the previous seed's selection is kept; with none, "
        "--apply from a terminal asks and a non-interactive run copies the example",
    )
    parser.add_argument(
        "--host",
        choices=sorted(_HOST_CHOICES),
        help="which main-agent host to install for: claude (~/.claude), agy (~/.gemini), or "
        "both. Without it the last install's choice is kept; with none, --apply from a "
        "terminal asks and a non-interactive run installs claude. A host dropped from the "
        "choice has Exoma's files taken out of it",
    )
    parser.add_argument(
        "--agy-permissions",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="with the agy host: add (or, --no-agy-permissions, remove) the allow rules for "
        "the .workflow runner and Exoma's data directory in "
        "~/.gemini/antigravity-cli/settings.json. Without it the last choice is kept; with "
        "none, --apply from a terminal asks and a non-interactive run leaves them out",
    )
    parser.add_argument(
        "--model",
        metavar="ID",
        help="pin this model in config/second_agent.seed.json. Requires --provider. "
        "An id outside the provider's shortlist is accepted with a warning",
    )
    parser.add_argument(
        "--update-providers",
        action="store_true",
        help="also bring each provider CLI to the release this workflow version was tested "
        "on (stable_version), never a newer one. Only npm-global installs are touched; agy "
        "and anything else are reported, not changed. Dry run unless --apply is also given",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift (installed config vs bundle, dist vs manifest, and the "
        "project-scoped files when run from a directory that has a .workflow/)",
    )
    args = parser.parse_args()
    apply = args.apply
    _reset_receipt()

    if args.rollback is not None:
        return _run_rollback(args.rollback or None, apply)
    if args.uninstall:
        from exoma.installer.uninstall import run_uninstall

        return run_uninstall(apply)

    if args.only_command and args.auto_intent:
        print("[INSTALL] --only-command and --auto-intent are opposites; pick one")
        return 2
    if args.model and not args.provider:
        print("[INSTALL] --model needs --provider: a model id alone names no adapter")
        return 2
    # No flag = keep whatever the last install chose. An upgrade must not change a
    # deliberate choice just because it was not restated.
    if not args.only_command and not args.auto_intent:
        args.only_command = _stored_only_command()

    if not MANIFEST.exists():
        print("[INSTALL] exoma/_bundle/manifest.json missing — the package is incomplete; reinstall exoma")
        return 1
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    project_root = _detect_project_root()
    if args.check:
        return _run_check(manifest, project_root)

    stale_bundle = _bundle_stale(manifest)
    if stale_bundle and apply:
        # Installing a dist/ that no longer matches its manifest ships files nobody
        # stamped and records a version that does not describe them.
        print("[INSTALL] ABORTED — the bundle does not match exoma/_bundle/manifest.json:")
        for key in stale_bundle:
            print(f"  !! {key}")
        print("Run: python tools/maintain/gen_manifest.py, then rerun. Nothing was written.")
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    backup_root = HOME / ".claude" / "backups" / f"install_{stamp}"

    plan = Plan()
    print(
        f"[INSTALL] Exoma v{manifest.get('version')} "
        f"({'APPLY' if apply else 'DRY RUN'})"
    )
    print(f"  home:   {HOME}")
    print(f"  backup: {backup_root}")
    # Asked before anything is written, like the provider: an answer cut short by Ctrl+C
    # leaves nothing half installed.
    hosts = _resolve_hosts(args.host, apply, plan)
    agy_permissions = _resolve_agy_permissions(args.agy_permissions, hosts, apply)
    print(f"  hosts:  {', '.join(hosts)}")
    print()

    # Env-secret preflight: every {{ENV:NAME}} a shipped file references must resolve
    # before we touch anything. Missing on --apply = abort whole run (no partial config).
    missing_env = _scan_missing_env()
    if missing_env:
        if apply:
            print("[INSTALL] ABORTED — required environment values are missing:")
            for name in sorted(missing_env):
                print(f"  !! {name}")
            print(
                "Set them in the environment (or AGENT_ENV_FILE / <data dir>/.env), then rerun."
                " Nothing was written."
            )
            return 5
        for name in sorted(missing_env):
            plan.warn(f"env value not set (dry run, would block --apply): {name}")

    for key in stale_bundle:
        plan.warn(f"the bundle does not match the manifest for {key} (dry run; --apply refuses)")

    # Every hook is a bash launcher on every OS. Without a bash the settings would register
    # commands that cannot start, and Claude Code shows a hook that cannot start as nothing.
    if hook_bash_command() is None:
        message = (
            "no Git Bash found: the workflow's hooks run through bash on every OS. Install "
            "Git for Windows (its bash is found next to git.exe), then rerun."
            if osutil.IS_WINDOWS
            else "no bash on PATH: the workflow's hooks run through bash on every OS. "
            "Install bash, then rerun."
        )
        if apply:
            print(f"[INSTALL] ABORTED — {message} Nothing was written.")
            return 6
        plan.warn(f"{message} (dry run, would block --apply)")

    if apply:
        # Begun after every abort-before-writing check and before the first write,
        # flushed after each one: see installer/base.py.
        # Receipt goes down with the backups, not beside the code: it is only meaningful
        # paired with them, and --rollback refuses to act without it.
        _begin_receipt(
            backup_root / "install_receipt.json",
            {
                "installed_at": datetime.now(timezone.utc).isoformat(),
                "version": manifest.get("version"),
                "only_command": bool(args.only_command),
                "hosts": list(hosts),
                "project_root": str(project_root) if project_root else None,
            },
        )

    _install_deps(plan, apply, args.with_e2e)

    targets = _targets(hosts)
    for source, dest, key in targets:
        _install_text(
            source, dest, key, plan, apply, backup_root, project_root, args.only_command
        )
    _stored_enabled, owned_rules = _stored_agy_permissions()
    if "agy" in hosts:
        owned_rules = _install_agy_permissions(agy_permissions, owned_rules, plan, apply, backup_root)
    # A host no longer chosen: its merged entries come out first, so no registration is
    # left pointing at a script the prune below removes.
    from exoma.installer.base import AGY_HOOKS_PREFIX, CLAUDE_HOOKS_PREFIX
    from exoma.installer.uninstall import retire_agy_dot_dir, withdraw_host

    retire_agy_dot_dir(plan, apply, backup_root)

    keep_prefixes: list[str] = []
    for host in HOSTS:
        if host not in hosts:
            clean, owned_rules = withdraw_host(host, owned_rules, plan, apply, backup_root)
            if not clean:
                keep_prefixes.append(CLAUDE_HOOKS_PREFIX if host == "claude" else AGY_HOOKS_PREFIX)
    # Before settings: the hook commands for scripts removed here are dropped there.
    from exoma.installer.stale import prune as _prune_stale, write_ledger as _write_ledger

    _prune_stale({str(dest) for _src, dest, _key in targets}, plan, apply, backup_root, tuple(keep_prefixes))

    settings_src = DIST_CONFIG / CLAUDE_SETTINGS
    if "claude" not in hosts:
        # The generated .workflow scripts still run the pinned interpreter.
        _pin_python(HOME / ".claude" / "settings.json", plan, apply)
    elif settings_src.exists():
        _install_settings(
            settings_src,
            HOME / ".claude" / "settings.json",
            plan,
            apply,
            backup_root,
            args.only_command,
        )
    for provider, bundle in PROVIDER_BUNDLES.items():
        provider_src = DIST_CONFIG / provider_dir(provider) / bundle["global_config"][0]
        if provider_src.exists():
            _install_provider_config(
                provider,
                provider_src,
                _provider_config_path(provider),
                plan,
                apply,
                backup_root,
                project_root,
            )
    # <project_root>/opencode.json (the secret-file boundary) is NOT installed here. It
    # belongs to a workspace, so init/upgrade owns it — see
    # core.runtime.upgrade._install_project_boundary. The upgrade call below reaches it.
    _seed_second_agent_config(plan, apply, args.provider, args.model)

    if project_root:
        # project_root is only set when a .workflow/ already exists here, so this is always
        # an in-place upgrade. Scaffolding a NEW workspace is `init`'s job (main.py
        # --command init --work-dir DIR) — one bootstrap path instead of two.
        from exoma.core.runtime.upgrade import upgrade_workflow_workspace, workspace_versions

        versions = workspace_versions(project_root)
        plan.add(
            "upgrade",
            project_root / ".workflow",
            f"tool {versions['installed_tool_version']} -> {versions['current_tool_version']}"
            " (migrate to .workflow/data if needed, regenerate scripts, restamp config, refresh opencode.json, keep sessions/)",
        )
        if apply:
            try:
                upgrade_workflow_workspace(project_root)
            except ValueError as exc:
                # Refuses while a delegated job is live. Reported, not raised: the
                # global config install above already succeeded, and aborting here
                # would leave the user unsure which half of the run took effect.
                plan.warn(f"workspace not upgraded: {exc}")

    _store_install_mode(
        args.only_command, hosts, agy_permissions, owned_rules, plan, apply, backup_root
    )
    _write_ledger(targets, apply)

    # The last write marks the receipt complete; every earlier one left it `complete:
    # false`, which is what an interrupted install leaves behind.
    if apply and _RECEIPT:
        _flush_receipt(complete=True)
    elif apply:
        _discard_empty_receipt()

    if args.update_providers:
        _update_providers(plan, apply, backup_root)

    counts: dict[str, int] = {}
    for verb, target, detail in plan.actions:
        counts[verb] = counts.get(verb, 0) + 1
        suffix = f"  — {detail}" if detail else ""
        print(f"  {verb:9} {target}{suffix}")

    print()
    print("[INSTALL REPORT]")
    print(
        "  " + " | ".join(f"{verb}: {n}" for verb, n in sorted(counts.items()))
        or "  nothing to do"
    )
    if plan.warnings:
        print("  warnings:")
        for warning in plan.warnings:
            print(f"    ! {warning}")
    else:
        print("  warnings: none")


    if "agy" in hosts:
        # --check compares files; only Agy itself can say whether it loads them.
        print()
        print("  agy: open Agy in a project that has a .workflow/ and run /.agy-check — it")
        print("       reports skills, hooks, the runner permission and the sandbox in one block.")

    if not apply:
        print()
        print("  DRY RUN — nothing was written. Re-run with --apply to install.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
