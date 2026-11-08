"""Install the agent-workflow config onto this machine.

Consumes `dist/` (produced by tools/maintain/extract_config.py) and applies it to the local
agent directories.

    exoma install [claude|agy|codex|opencode|all]   # install (writes)
    exoma install --dry-run                          # show every change, write nothing

Project scope is detected, not passed: run it from anywhere inside a directory that has a
.workflow/ and that workspace is refreshed in place too (scripts regenerated, new config
keys backfilled, opencode.json boundary re-enforced, sessions/ untouched). Scaffolding a
NEW workspace belongs to init — `python main.py --command init --work-dir DIR` — so there
is one bootstrap path rather than a flag that half-duplicates it.

A bare install writes. This changes the user's global agent config, which every project
on the machine reads, so a mistake here is not contained to one repo: run `--dry-run`
first to see every change, and never install from a development checkout onto the real
home (point HOME/AGENT_HOME/EXOMA_HOME at a temporary directory instead).

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
import re
import shutil
import sys

# Before any import that the newer syntax would break: the oldest supported Python is
# utils/osutil.py MIN_PYTHON, repeated here because importing it would fail on the very
# interpreter this names.
if sys.version_info < (3, 11):
    sys.exit(
        f"Exoma needs Python 3.11 or newer; this is {sys.version.split()[0]} "
        f"({sys.executable}). Reinstall it under Python 3.11+, e.g. "
        "`uv tool install --python 3.11 exoma`."
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
    manifest_error,
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
    _stored_agy_opt_out,
    _stored_agy_permissions,
    _stored_codex_auto_review_context,
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
    effort: str | None = None,
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
    if effort:
        config["effort"] = effort
    agent = provider_agent_default(provider, os.getenv)
    if agent:
        config["provider_agent"] = agent
    return config


def _seed_selection(path: Path) -> dict | None:
    """`{provider, model, effort, routes}` from an existing seed, or None when there is none to keep.

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
    effort = loaded.get("effort")
    return {
        "provider": loaded["provider"],
        "model": model if isinstance(model, str) else None,
        "effort": effort if isinstance(effort, str) and effort else None,
        "routes": {
            name: entry.get("model")
            for name, entry in routes.items()
            if isinstance(entry, dict) and isinstance(entry.get("model"), str)
        },
    }


class InstallInterrupted(Exception):
    """Ctrl+C at a question: the install stops before anything is written."""


def _ask(prompt: str) -> str:
    """One line from the operator, or "" when the stream ends. Ctrl+C raises
    InstallInterrupted: it means stop, not "take the default for everything left"."""
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        return ""
    except KeyboardInterrupt:
        print()
        raise InstallInterrupted from None


def _ask_choices(args, host_choice, apply: bool, plan, interactive: bool, intent_stated) -> tuple:
    """The hosts, then (from a terminal) every remaining choice a flag did not state:
    `(hosts, asked)`, `asked` mapping each host to the one second-agent answer given for
    all of them. Each answer is written back onto `args`. Raises InstallInterrupted on
    Ctrl+C, before any write."""
    hosts = _resolve_hosts(host_choice, apply, plan, ask=interactive)
    asked: dict = {}
    if not interactive:
        return hosts, asked
    if not intent_stated:
        print()
        print("  Intent mode: auto-intent maps plain sentences to workflow commands;")
        print("  command-only requires the /. prefix and drops the intent hook.")
        args.only_command = not _ask_yes_no(
            f"use auto-intent? (current: {'command-only' if args.only_command else 'auto-intent'})",
            not args.only_command,
        )
    if not args.with_e2e:
        args.with_e2e = _ask_yes_no(
            "install the browser-verification extra (Playwright + Chromium, large download)?",
            False,
        )
    if "agy" in hosts and args.agy_permissions is None:
        args.agy_permissions = _ask_yes_no(
            "add Agy allow rules for the .workflow runner and Exoma's data directory?",
            not _stored_agy_opt_out(),
        )
    if "codex" in hosts and getattr(args, "codex_auto_review_context", False) is None:
        args.codex_auto_review_context = _ask_yes_no(
            "add Exoma authorization context to Codex Auto-review (keeps your reviewer and sandbox)?",
            _stored_codex_auto_review_context(),
        )
    if not args.update_providers:
        args.update_providers = _ask_yes_no(
            "bring provider CLIs to their tested stable_version (npm-global only)?", False
        )
    if args.provider is None and hosts:
        from exoma.core.workspace.workspace_paths import PROVIDER_SEED_NAME

        # Asked once, for every chosen host, as --provider already does. The default is the
        # first host's seed (else the shared one); a project that needs another second agent
        # changes it with /.provider.
        shared = _seed_selection(SEED_DIR / PROVIDER_SEED_NAME)
        current = {host: _seed_selection(SEED_DIR / seed_name(host)) or shared for host in hosts}
        previous = current[hosts[0]]
        answer = _prompt_second_agent("/".join(hosts), previous)
        # (None, None, None) changes no seed, so there is nothing to say about replacing.
        # A host with no seed had no second agent to differ from: only existing seeds count.
        picks = {(sel["provider"], sel["model"]) for sel in current.values() if sel}
        if answer[0] is not None and len(picks) > 1:
            print(f"  {', '.join(hosts)} had different second agents; this answer is written to all of them.")
        for host in hosts:
            asked[host] = answer
    return hosts, asked


def _prompt_second_agent(
    host: str | None = None, previous: dict | None = None
) -> tuple[str | None, str | None, str | None]:
    """Ask which second agent `host` should use: (provider, model, effort). `host` is a
    label only: install asks once for every chosen host and passes them joined by `/`.

    Asked on every install from a terminal, with `previous` (the first host's current seed
    selection) as the default at each step, so Enter keeps it. (None, None, None) means
    "no choice": the seed keeps its previous selection, or the example when there is none.

    The model menu is the live catalog (core/provider/model_catalog.py): what the installed
    provider CLI lists now, newest first, falling back to the bundled shortlist when the
    CLI cannot be asked (missing, offline, or a provider with no listing, like claude).
    A blank model leaves the pin unset so the provider CLI keeps its own default; a blank
    effort keeps the previous one for the same model, else the provider's default. A model
    whose variants carry the effort in the id (agy) gets that id, and no separate effort.
    """
    from exoma.config.providers import (
        bundled_providers,
        provider_boundary,
        provider_command_default,
    )
    from exoma.core.provider.model_catalog import catalog, compose_model_id, lookup

    providers = sorted(bundled_providers())
    print()
    who = f"{host} projects" if host else "new projects"
    current = (
        f"current: {previous['provider']}, model={previous['model'] or 'unset'}, "
        f"effort={previous.get('effort') or 'default'}"
        if previous
        else "none chosen yet"
    )
    print(f"  Which second agent should {who} use? ({current})")
    for index, name in enumerate(providers, start=1):
        command = provider_command_default(name, os.getenv)
        notes = ["on PATH" if shutil.which(command) else f"{command} not on PATH"]
        if provider_boundary(name)["reads"] == "unbounded":
            notes.append("no enforced read boundary — trusted provider")
        print(f"    {index}) {name:<10} ({'; '.join(notes)})")
    keep = f"keep {previous['provider']}" if previous else "keep the example default"
    choice = _ask(f"  provider [1-{len(providers)}, blank = {keep}]: ")
    if choice.isdigit() and 1 <= int(choice) <= len(providers):
        provider = providers[int(choice) - 1]
    elif previous:
        provider = previous["provider"]
    else:
        return None, None, None
    same_provider = bool(previous) and previous["provider"] == provider
    previous_model = previous["model"] if same_provider else None

    listing = catalog(provider)
    models = listing["models"]
    model = previous_model
    if models:
        print()
        print(f"  models for {provider} ({'live from the CLI' if listing['source'] in ('live', 'cache') else 'bundled list'}):")
        for index, entry in enumerate(models, start=1):
            efforts = ", ".join(entry.get("efforts") or ()) or "no reasoning effort"
            print(f"    {index}) {entry['id']:<42} ({efforts})")
        keep = f"keep {previous_model}" if previous_model else "let the CLI decide"
        choice = _ask(f"  model [1-{len(models)}, or a model id, blank = {keep}]: ")
        if choice.isdigit() and 1 <= int(choice) <= len(models):
            model = models[int(choice) - 1]["id"]
        elif choice:
            # A pin the shortlist never mentioned still has to work: the menu is a picker,
            # not the set of models that exist (config/providers.model_is_listed).
            model = choice

    entry = lookup(provider, model) if model else None
    efforts = list((entry or {}).get("efforts") or ())
    same_model = same_provider and model == previous_model
    previous_effort = previous.get("effort") if same_model else None
    if not efforts:
        # Nothing to ask. A pin the catalog does not list keeps the effort stored for that
        # same model (the router passes it on as configured, core/prompt/router.py), as a
        # --no-input install does; a listed model without efforts takes none.
        return provider, model, previous_effort if model and entry is None else None
    keep = f"keep {previous_effort}" if previous_effort else "provider default"
    if entry.get("effort_in_id"):
        keep = "keep the model as given"
    for index, name in enumerate(efforts, start=1):
        print(f"    {index}) {name}")
    choice = _ask(f"  effort [1-{len(efforts)}, blank = {keep}]: ")
    effort = (
        efforts[int(choice) - 1]
        if choice.isdigit() and 1 <= int(choice) <= len(efforts)
        else (choice if choice in efforts else previous_effort)
    )
    if entry.get("effort_in_id"):
        # agy: the effort names a variant of the model, written into the id.
        return provider, (compose_model_id(model, effort) if effort else model), None
    return provider, model, effort


_LEGACY_RETIRED: set = set()


def seed_name(host: str | None = None) -> str:
    """`second_agent.seed.<host>.json` for a host, the shared seed without one."""
    from exoma.core.workspace.workspace_paths import PROVIDER_SEED_NAME

    return PROVIDER_SEED_NAME.replace(".json", f".{host}.json") if host else PROVIDER_SEED_NAME


def _seed_second_agent_config(
    plan, apply: bool, provider: str | None = None, model: str | None = None,
    host: str | None = None, asked: tuple | None = None,
) -> None:
    """Rebuild config/second_agent.seed.<host>.json, the template `init` copies into a
    project initialised from that host (`exoma init --host <host>`, rendered into each
    host's /.init). One seed per host, so Claude Code and Codex on the same machine can
    delegate to different second agents. Without a host: the shared seed, which init
    falls back to when a host has none.

    Rebuilt on EVERY run, from the shipped example, so the seed can never outlive the
    release that wrote it. Its predecessor, config/second_agent.json, was created once and
    left alone forever after; init copied it into each new project, and one pick made on
    one day — `opencode/mimo-v2.5-free` — kept arriving in projects nobody chose it for.

    The selection the seed carries (provider, default_model, per-route models) comes from,
    in order:
      --provider/--model     taken as given;
      the previous seed      the last deliberate choice, re-applied to the new example;
      `asked`                the answer main() collected from a terminal (provider, model,
                             effort), the previous selection being its default;
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

    dest = SEED_DIR / seed_name(host)
    shared = SEED_DIR / PROVIDER_SEED_NAME
    legacy = SEED_DIR / PROVIDER_CONFIG_NAME
    src = EXAMPLE_FILE
    if not src.exists():
        plan.warn(f"cannot seed {dest.name}: {src} is missing from the package")
        return
    template = json.loads(src.read_text(encoding="utf-8"))

    # Once per install run, not once per host: main() clears this before the host loop.
    if legacy.exists() and not (host and legacy in _LEGACY_RETIRED):
        _LEGACY_RETIRED.add(legacy)
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
    effort = None
    # A host seed that does not exist yet starts from the shared seed's selection: the
    # choice made before seeds were per host carries over instead of being asked again.
    previous = _seed_selection(dest) or (_seed_selection(shared) if host else None)
    if provider is None and asked and asked[0] is not None:
        provider, model, effort = asked
        # Per-route picks survive an answer that kept the provider and model.
        if previous and (previous["provider"], previous["model"]) == (provider, model):
            route_models = previous["routes"]
    elif provider is None and previous is not None:
        provider, model, route_models = previous["provider"], previous["model"], previous["routes"]
        effort = previous.get("effort")
    elif provider is None:
        if not apply:
            plan.add(
                "create",
                dest,
                "rebuilt from the example; an install from a terminal asks which provider "
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
            from exoma.core.provider.model_catalog import lookup

            if lookup(provider, model) is None:
                plan.warn(
                    f"{model!r} is not in {provider}'s model catalog — pinned anyway, but "
                    "verify the id is one the CLI accepts"
                )
        if not model and provider == "opencode":
            plan.warn(
                "opencode without a model is refused at call time (model_unset): without "
                "-m it would answer with whatever model it used last. Pass --model."
            )
        detail = f"provider={provider}, model={model or 'unset'}" + (f", effort={effort}" if effort else "")
        payload = (
            json.dumps(
                _second_agent_config(template, provider, model, route_models, effort),
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


_HOST_CHOICES = {"claude": ("claude",), "agy": ("agy",), "codex": ("codex",),
                 "opencode": ("opencode",), "all": HOSTS}


_HOST_MENU = ("claude", "agy", "codex", "opencode")


def _prompt_hosts(default: tuple[str, ...] = ("claude",)) -> tuple[str, ...]:
    """Ask which main-agent hosts to install, any combination. Only from a terminal.

    `1,3` picks Claude Code and Codex, `5` all of them; blank keeps `default` (the last
    install's choice). An answer that names no host also keeps it rather than guessing.
    """
    print()
    print(f"  Which main agents should Exoma install for? (current: {', '.join(default)})")
    print("    1) claude   (Claude Code: ~/.claude)")
    print(f"    2) agy      (Antigravity CLI: ~/.gemini{'' if agy_host_present() else ', not found on this machine'})")
    print("    3) codex    (Codex CLI: ~/.codex)")
    print("    4) opencode (OpenCode: ~/.config/opencode)")
    print("    5) all      (Claude Code, Agy, Codex, and OpenCode)")
    choice = _ask(f"  hosts [numbers or names, e.g. 1,3; blank = {', '.join(default)}]: ")
    picked: set[str] = set()
    for token in re.split(r"[\s,]+", choice.lower()):
        if token in ("5", "all"):
            picked.update(HOSTS)
        elif token.isdigit() and 1 <= int(token) <= len(_HOST_MENU):
            picked.add(_HOST_MENU[int(token) - 1])
        elif token in HOSTS:
            picked.add(token)
    return tuple(h for h in HOSTS if h in picked) or default


def _ask_yes_no(question: str, default: bool) -> bool:
    """A y/n question from a terminal; blank keeps `default`."""
    answer = _ask(f"  {question} [{'Y/n' if default else 'y/N'}]: ").lower()
    if answer in ("y", "yes", "ya"):
        return True
    if answer in ("n", "no", "tidak"):
        return False
    return default


def _resolve_hosts(
    flag: str | None, apply: bool, plan: Plan, ask: bool | None = None
) -> tuple[str, ...]:
    """The positional host, else asked from a terminal with the last choice as default,
    else the last install's choice, else the 0.3.0 migration, else Claude — what every
    install did before the choice. `ask` None keeps the earlier rule: ask only when
    nothing is stored."""
    if flag:
        return _HOST_CHOICES[flag]
    hosts = _stored_hosts() or _migrated_hosts()
    if ask is None:
        ask = apply and sys.stdin.isatty() and not hosts
    if ask:
        return _prompt_hosts(hosts or ("claude",))
    if hosts:
        return hosts
    plan.warn("no host chosen yet: installing for claude; a terminal install asks, or name one: "
              "exoma install <claude|agy|codex|opencode|all>")
    return ("claude",)


def _resolve_agy_permissions(flag: bool | None, hosts: tuple[str, ...]) -> tuple[bool, bool]:
    """(rules on, opt-out to store). On whenever Agy is a chosen host: without them Agy asks
    before every runner call and its sandbox refuses the runtime's data directory.
    --no-agy-permissions turns them off and is kept; --agy-permissions clears that."""
    opt_out = (flag is False) or (flag is None and _stored_agy_opt_out())
    return ("agy" in hosts and not opt_out), opt_out


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
    parser = argparse.ArgumentParser(
        prog="exoma install",
        description="Install Exoma for a main-agent host and its second agent. Writes by "
        "default; --dry-run previews.",
    )
    parser.add_argument(
        "host",
        nargs="?",
        choices=sorted(_HOST_CHOICES),
        help="the main-agent host to install for: claude, agy, codex, opencode, or all. "
        "Without it a terminal asks (any combination, the last choice as the default); a "
        "non-interactive run keeps the last choice, or installs claude when there is none. "
        "A host dropped from the choice has Exoma's files taken out of it",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show every change, write nothing"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the changes of --rollback or --uninstall (both preview without it). "
        "For an install it is the default and the flag is accepted with a notice",
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
        "Only needed for /.verify-browser; never installed without this flag",
    )
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDER_BUNDLES),
        help="which second agent each chosen host's seed (second_agent.seed.<host>.json, the "
        "template that host's new projects copy) should select. Without it a terminal asks "
        "once for every chosen host from the live model catalog (provider, model, effort; the "
        "first host's previous selection as the default); a non-interactive run keeps each "
        "host's previous selection, or "
        "copies the example when there is none",
    )
    parser.add_argument(
        "--host",
        dest="host_flag",
        choices=sorted(_HOST_CHOICES),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--agy-permissions",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="the allow rules for the .workflow runner and Exoma's data directory in "
        "~/.gemini/antigravity-cli/settings.json are added whenever the agy host is chosen. "
        "--no-agy-permissions removes them and is kept across upgrades; --agy-permissions "
        "undoes that",
    )
    parser.add_argument(
        "--codex-auto-review-context", action=argparse.BooleanOptionalAction, default=None,
        help="add owned Exoma authorization context to Codex HOST auto_review.extra_policy; "
             "keeps your policy, reviewer and sandbox. Choice persists across updates; default off",
    )
    parser.add_argument(
        "--model",
        metavar="ID",
        help="pin this model in each chosen host's seed (second_agent.seed.<host>.json). "
        "Requires --provider. "
        "An id outside the provider's shortlist is accepted with a warning",
    )
    parser.add_argument(
        "--update-providers",
        action="store_true",
        help="also bring each provider CLI to the release this workflow version was tested "
        "on (stable_version), never a newer one. Only npm-global installs are touched; agy "
        "and anything else are reported, not changed",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift (installed config vs bundle, dist vs manifest, and the "
        "project-scoped files when run from a directory that has a .workflow/)",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="ask nothing: keep the stored choices (host, second agent, intent mode, Agy "
        "permissions) and take the rest from flags; `exoma update` passes it. Without it an "
        "install from a terminal asks every choice, the current one as the default",
    )
    args = parser.parse_args()
    _reset_receipt()
    _LEGACY_RETIRED.clear()

    # Undoing an install keeps its preview-first default: it removes or restores files,
    # and the command that does it is typed far less often than the one that installs.
    if args.rollback is not None:
        return _run_rollback(args.rollback or None, args.apply and not args.dry_run)
    if args.uninstall:
        from exoma.installer.uninstall import run_uninstall

        return run_uninstall(args.apply and not args.dry_run)

    if args.host and args.host_flag and args.host != args.host_flag:
        print(f"[INSTALL] host named twice ({args.host} and --host {args.host_flag}); name it once")
        return 2
    host_choice = args.host or args.host_flag
    apply = not args.dry_run
    if args.apply and args.dry_run:
        print("[INSTALL] --apply and --dry-run are opposites; an install writes unless --dry-run")
        return 2

    if args.only_command and args.auto_intent:
        print("[INSTALL] --only-command and --auto-intent are opposites; pick one")
        return 2
    if args.model and not args.provider:
        print("[INSTALL] --model needs --provider: a model id alone names no adapter")
        return 2
    # From a terminal every choice is asked, the current one as the default; a flag that
    # states a choice skips its question. Not from a terminal, or --no-input (`exoma
    # update`): nothing is asked and the stored choices stand.
    interactive = apply and sys.stdin.isatty() and not args.no_input
    intent_stated = args.only_command or args.auto_intent
    # No flag = keep whatever the last install chose. An upgrade must not change a
    # deliberate choice just because it was not restated.
    if not args.only_command and not args.auto_intent:
        args.only_command = _stored_only_command()

    if not MANIFEST.exists():
        print("[INSTALL] exoma/_bundle/manifest.json missing — the package is incomplete; reinstall exoma")
        return 1
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[INSTALL] manifest unreadable or invalid ({type(exc).__name__}); reinstall exoma. Nothing was written.")
        return 1
    error = manifest_error(manifest)
    if error:
        print(f"[INSTALL] {error}; reinstall exoma. Nothing was written.")
        return 1

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
    # Asked before anything is written, like the provider: Ctrl+C at any question stops
    # the install (InstallInterrupted) with nothing written.
    if args.apply:
        plan.warn("--apply is no longer needed: `exoma install` writes by default (--dry-run previews)")
    if args.host_flag:
        plan.warn(f"--host is deprecated: name the host directly, `exoma install {args.host_flag}`")
    try:
        hosts, asked = _ask_choices(args, host_choice, apply, plan, interactive, intent_stated)
    except InstallInterrupted:
        print("[INSTALL] ABORTED — interrupted at a question. Nothing was written.")
        return 130
    if "codex" in hosts:
        plan.warn("Codex host hooks must be reviewed and trusted in Codex with /hooks before the host gates run")
    if "opencode" in hosts:
        plan.warn("OpenCode loads its plugins at startup: restart any running opencode session to load Exoma's host plugin")
    agy_permissions, agy_opt_out = _resolve_agy_permissions(args.agy_permissions, hosts)
    plan.codex_auto_review_context = (args.codex_auto_review_context
                                     if args.codex_auto_review_context is not None
                                     else _stored_codex_auto_review_context())
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
            plan.warn(f"env value not set (dry run, would block the install): {name}")

    for key in stale_bundle:
        plan.warn(f"the bundle does not match the manifest for {key} (dry run; an install refuses)")

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
        plan.warn(f"{message} (dry run, would block the install)")

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

    # A requested extra that did not install fails the install (exit 1) after the report,
    # like a workspace upgrade: /.verify-browser would otherwise stop on playwright_missing
    # behind an install that said nothing was wrong.
    e2e_failure = _install_deps(plan, apply, args.with_e2e)

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
    from exoma.installer.base import (
        AGY_HOOKS_PREFIX, CLAUDE_HOOKS_PREFIX, CODEX_HOOKS_PREFIX, OPENCODE_HOOKS_PREFIX,
    )
    from exoma.installer.uninstall import retire_agy_dot_dir, withdraw_host

    retire_agy_dot_dir(plan, apply, backup_root)

    keep_prefixes: list[str] = []
    for host in HOSTS:
        if host not in hosts:
            clean, owned_rules = withdraw_host(host, owned_rules, plan, apply, backup_root)
            if not clean:
                keep_prefixes.append({"claude": CLAUDE_HOOKS_PREFIX, "agy": AGY_HOOKS_PREFIX,
                                      "codex": CODEX_HOOKS_PREFIX, "opencode": OPENCODE_HOOKS_PREFIX}[host])
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
    for host in hosts:
        _seed_second_agent_config(
            plan, apply, args.provider, args.model, host=host, asked=asked.get(host)
        )

    workspace_upgrade_failed = False
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
                # Finish the global install and its receipt, but report the incomplete
                # workspace upgrade through the process status as well as the warning.
                plan.warn(f"workspace not upgraded: {exc}")
                workspace_upgrade_failed = True

    if "agy" in hosts:
        # The refusal itself is generic (its callers do not all know the choice); here the
        # choice is known, so the report names the exact command that drops Agy from it.
        from exoma.installer.base import agy_hooks_dest, agy_hooks_refusal, deselect_hint

        if agy_hooks_refusal(agy_hooks_dest()):
            plan.warn(f"to drop the Agy host from this choice: {deselect_hint('agy', hosts)}")

    _store_install_mode(
        args.only_command, hosts, agy_permissions, owned_rules, agy_opt_out, plan, apply, backup_root,
        codex_auto_review_context=plan.codex_auto_review_context,
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
    if args.with_e2e:
        print(f"  e2e: failed — {e2e_failure}" if e2e_failure else
              ("  e2e: installed" if apply else "  e2e: planned"))


    if "agy" in hosts:
        # --check compares files; only Agy itself can say whether it loads them.
        print()
        print("  agy: open Agy in a project that has a .workflow/ and run /.doctor — in Agy it")
        print("       also reports skills, hooks, the runner permission and the sandbox.")

    if not apply:
        print()
        print("  DRY RUN — nothing was written. Re-run without --dry-run to install.")
    return 1 if workspace_upgrade_failed or e2e_failure or plan.codex_policy_problem else 0


if __name__ == "__main__":
    raise SystemExit(main())
