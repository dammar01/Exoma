"""Read-only drift detection: what WOULD change if an install ran now.

Depends on the write layers on purpose — the only honest way to answer "would this
change?" is to ask the same code that performs the change.
"""

import json
from pathlib import Path

from exoma.config.bundle_keys import provider_dir
from exoma.installer.base import (
    AGY_HOST,
    AGY_HOST_HOOK_NAME,
    CLAUDE_HOST,
    CLAUDE_SETTINGS,
    AGY_HOST_HOOKS,
    AGY_HOST_INSTRUCTIONS,
    AGY_DOCTOR_FRAGMENT,
    AGY_HOST_PERMISSIONS,
    DIST_CONFIG,
    HOME,
    MARKERS,
    Plan,
    _hash,
    _managed_block,
    _marker_problem,
    _read_text_lenient,
    _render_text,
    _targets,
    agy_hook_entry,
    agy_hooks_dest,
    agy_hooks_state,
    agy_permission_rules,
    agy_settings_dest,
    bundle_key,
)
from exoma.config.providers import PROVIDER_BUNDLES
from exoma.installer.rollback import (
    _installed_hosts,
    _stored_agy_permissions,
    _stored_only_command,
)
from exoma.installer.settings import (
    _install_provider_config,
    _install_settings,
    _provider_config_path,
)


from exoma.config.settings import PROVIDER_SEED_FILE  # noqa: E402

# Where `exoma install` writes the provider seed. A module global so tests can point it
# at a temp directory.
SEED_DIR = PROVIDER_SEED_FILE.parent

def _settings_would_change(src: Path, dest: Path, only_command: bool) -> bool:
    plan = Plan()
    _install_settings(src, dest, plan, False, Path("."), only_command)
    writes = {"create", "merge", "replace", "mode"}
    return any(action in writes for action, _target, _detail in plan.actions) or any(
        "not valid JSON" in warning or "root is not a JSON object" in warning
        for warning in plan.warnings
    )


def _provider_config_would_change(
    provider: str, src: Path, dest: Path, project_root: Path | None
) -> bool:
    plan = Plan()
    _install_provider_config(provider, src, dest, plan, False, Path("."), project_root)
    writes = {"create", "merge", "replace"}
    return any(action in writes for action, _target, _detail in plan.actions) or any(
        "not valid JSON/JSONC" in warning or "root is not a JSON object" in warning
        for warning in plan.warnings
    )


def _second_agent_state() -> str:
    """One line describing config/second_agent.seed.json — the template every `init` copies.

    Informational on purpose; see the call site in `_run_check` for why it stays out of
    the drift tally. Reads the file rather than trusting it exists, because a hand-edited
    one that no longer parses is the failure this line is worth printing for. A retired
    config/second_agent.json still on disk is named too: --apply renames it, and until then
    it is a stale selection sitting next to the one init actually copies.
    """
    import json

    from exoma.core.workspace.workspace_paths import PROVIDER_CONFIG_NAME, PROVIDER_SEED_NAME

    path = SEED_DIR / PROVIDER_SEED_NAME
    legacy = SEED_DIR / PROVIDER_CONFIG_NAME
    retire_note = (
        f"; {PROVIDER_CONFIG_NAME} is retired and will be renamed by --apply"
        if legacy.exists()
        else ""
    )
    if not path.exists():
        return (
            f"NOT SET — {PROVIDER_SEED_NAME} absent; init falls back to "
            "second_agent.example.json (opencode, no model pinned). "
            f"`exoma install --apply` asks, or pass --provider{retire_note}"
        )
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"UNREADABLE — {path} ({exc})"
    if not isinstance(config, dict):
        return f"UNREADABLE — {path} (root is not a JSON object)"
    provider = config.get("provider") or "unset"
    model = config.get("default_model") or "unset"
    return f"{provider}, model={model}{retire_note}"


def _detect_project_root() -> Path | None:
    """The workspace this install run is standing in, or None.

    Derived from an existing `.workflow/config.json` walking up from the cwd, never from a
    flag. A flag let `--check` run with a narrower scope than doctor uses and report READY
    while the project-scoped boundary was missing — the exact state a check exists to catch.
    Requiring the marker also means running the installer from an unrelated directory never
    writes project files into it.
    """
    for candidate in [Path.cwd().resolve(), *Path.cwd().resolve().parents]:
        if (candidate / ".workflow" / "config.json").exists():
            return candidate
    return None


def _entries_by_path(manifest: dict) -> dict[str, dict]:
    """Well-formed manifest entries by path. A malformed one is left out here and comes
    back through `_orphaned_entries` as stale, instead of raising out of the check."""
    return {
        entry["path"]: entry
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }


def _orphaned_entries(manifest: dict) -> list[str]:
    """Manifest keys whose dist/ source no longer exists.

    The stale checks walk the sources that exist and look each one up in the manifest, so
    a file deleted from dist/ without regenerating the manifest was never visited: the
    manifest kept vouching for a file the bundle no longer ships, and both checks passed.
    """
    orphaned: list[str] = []
    for entry in manifest.get("files", []):
        key = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(key, str) or not (DIST_CONFIG / key).is_file():
            orphaned.append(str(key))
    return orphaned


def _bundle_stale(manifest: dict) -> list[str]:
    """Manifest keys whose dist/ file no longer matches, or that dist/ has but the manifest lacks.

    Shared by `--check` and by `--apply`, which refuses to install a bundle nobody stamped:
    a dist/ edited without regenerating the manifest is exactly the half-finished state that
    must not reach ~/.claude.
    """
    by_path = _entries_by_path(manifest)
    stale: list[str] = []
    # Every host's sources, chosen or not, by their manifest key: one source rendered for
    # two hosts is stamped once.
    sources = list({bundle_key(source): (source, bundle_key(source)) for source, _d, _k in _targets()}.values())
    for key in (AGY_HOST_PERMISSIONS, AGY_DOCTOR_FRAGMENT):
        if (DIST_CONFIG / key).exists():
            sources.append((DIST_CONFIG / key, key))
    settings_src = DIST_CONFIG / CLAUDE_SETTINGS
    if settings_src.exists():
        sources.append((settings_src, CLAUDE_SETTINGS))
    for provider, bundle in PROVIDER_BUNDLES.items():
        for template in (bundle["global_config"][0], bundle["project_config"][0]):
            source = DIST_CONFIG / provider_dir(provider) / template
            if source.exists():
                sources.append((source, f"{provider_dir(provider)}/{template}"))
    for source, key in sources:
        entry = by_path.get(key)
        if not entry or _hash(source.read_text(encoding="utf-8")) != entry.get("sha256"):
            stale.append(key)
    stale.extend(key for key in _orphaned_entries(manifest) if key not in stale)
    return stale


def _run_check(manifest: dict, project_root: Path | None = None) -> int:
    """Report drift without writing anything.

    Two independent questions: (1) does dist/ still match its manifest — catches a dist
    edit that skipped `python tools/maintain/gen_manifest.py`; (2) does the installed ~/.claude match
    dist/ — catches a stale or hand-edited install. Full-overwrite targets (skills, hooks)
    compare whole-file; managed targets (CLAUDE.md, AGENTS.md) compare only the marker block,
    since the rest of those files is legitimately the user's own content.
    """
    by_path = _entries_by_path(manifest)
    bundle_stale: list[str] = []
    installed_drift: list[str] = []
    installed_missing: list[str] = []

    # Installed state is checked for the hosts the last install chose; the bundle for all.
    hosts = _installed_hosts()
    only_command = _stored_only_command()
    agy = "agy" in hosts
    # The Agy host installs both files or neither, so a refused hook makes GEMINI.md's
    # absence the expected state, not a missing file.
    # One read of hooks.json for the refusal and the entry check below (no second read to race).
    agy_current, agy_refusal = agy_hooks_state(agy_hooks_dest()) if agy else ({}, None)
    # hooks.json is a merge of one hook name, like settings.json: its `exoma` entry is
    # checked below rather than the whole file here.
    installed_targets = _targets(hosts)
    checks = [
        (s, None if k == AGY_HOST_HOOKS or (k == AGY_HOST_INSTRUCTIONS and agy_refusal) else d, k)
        for s, d, k in installed_targets
    ]
    for source, _dest, key in _targets():
        if all(k != key for _s, _d, k in checks):
            checks.append((source, None, key))
    for key in (AGY_HOST_PERMISSIONS, AGY_DOCTOR_FRAGMENT):
        if (DIST_CONFIG / key).exists():
            checks.append((DIST_CONFIG / key, None, key))
    settings_src = DIST_CONFIG / CLAUDE_SETTINGS
    if settings_src.exists():
        # settings.json is a key-wise JSON merge, not a copy — bundle-check only.
        checks.append((settings_src, None, CLAUDE_SETTINGS))
    # Provider configs are merged, not copied, so their installed state is checked below
    # rather than compared byte-for-byte here.
    provider_checks = []
    for provider, bundle in PROVIDER_BUNDLES.items():
        global_src = DIST_CONFIG / provider_dir(provider) / bundle["global_config"][0]
        project_src = DIST_CONFIG / provider_dir(provider) / bundle["project_config"][0]
        if global_src.exists():
            checks.append((global_src, None, f"{provider_dir(provider)}/{bundle['global_config'][0]}"))
        if project_src.exists():
            checks.append(
                (project_src, None, f"{provider_dir(provider)}/{bundle['project_config'][0]}")
            )
        provider_checks.append((provider, bundle, global_src, project_src))

    for source, dest, key in checks:
        dist_text = source.read_text(encoding="utf-8")
        stamp_key = bundle_key(source)
        entry = by_path.get(stamp_key)
        if (not entry or _hash(dist_text) != entry.get("sha256")) and stamp_key not in bundle_stale:
            bundle_stale.append(stamp_key)
        if dest is None:
            continue
        resolved = _render_text(source, key, project_root, only_command)
        if not dest.exists():
            installed_missing.append(key)
            continue
        installed = _read_text_lenient(dest)
        if key in MARKERS:
            start, end = MARKERS[key]
            want = _managed_block(resolved, start, end)
            have = _managed_block(installed, start, end)
            if want is None or have is None or want != have:
                installed_drift.append(key)
        elif installed != resolved:
            installed_drift.append(key)
    bundle_stale.extend(key for key in _orphaned_entries(manifest) if key not in bundle_stale)

    agy_permissions, _owned = _stored_agy_permissions()
    if agy and agy_permissions:
        try:
            agy_settings = json.loads(_read_text_lenient(agy_settings_dest()))
            allow = agy_settings.get("permissions", {}).get("allow", [])
        except (OSError, ValueError, AttributeError):
            allow = []
        if not isinstance(allow, list) or any(rule not in allow for rule in agy_permission_rules()):
            installed_missing.append(f"{AGY_HOST}/settings.json (permission rules)")
    if agy:
        gemini_md = HOME / ".gemini" / "GEMINI.md"
        if agy_refusal:
            # An install now would take out a block an earlier one left (a half install), or
            # warn about markers that do not pair up and so cannot be taken out.
            start, end = MARKERS[AGY_HOST_INSTRUCTIONS]
            text = _read_text_lenient(gemini_md) if gemini_md.is_file() else ""
            if _managed_block(text, start, end) or _marker_problem(text, start, end):
                installed_drift.append(AGY_HOST_INSTRUCTIONS)
        else:
            installed_hook = agy_current.get(AGY_HOST_HOOK_NAME)
            if installed_hook is None:
                installed_missing.append(AGY_HOST_HOOKS)
            elif installed_hook != agy_hook_entry(DIST_CONFIG / AGY_HOST_HOOKS):
                installed_drift.append(AGY_HOST_HOOKS)

    settings_dest = HOME / ".claude" / "settings.json"
    if settings_src.exists() and "claude" in hosts:
        if not settings_dest.exists():
            installed_missing.append(f"{CLAUDE_HOST}/settings.json")
        elif _settings_would_change(settings_src, settings_dest, only_command):
            installed_drift.append(f"{CLAUDE_HOST}/settings.json")

    # The project config carries the secret-file denial. Checking only the global one let
    # --check report OK while the boundary was missing or had been edited away — the exact
    # state a check exists to catch.
    project_scope_note = None
    # Only the boundary of the provider the project runs. init installs that one alone
    # (core.runtime.upgrade._install_project_boundary), so requiring opencode's file in a
    # codex project reported a drift no command could repair. A project that names no
    # provider resolves to the default, which keeps the check fail-closed.
    project_provider = None
    if project_root:
        from exoma.adapters.contract.registry import selected_provider

        project_provider = selected_provider(project_root)
    for provider, bundle, global_src, project_src in provider_checks:
        if global_src.exists():
            installed_key = f"{provider_dir(provider)}/{bundle['global_config'][1]}"
            global_dest = _provider_config_path(provider)
            if not global_dest.exists():
                installed_missing.append(installed_key)
            elif _provider_config_would_change(
                provider, global_src, global_dest, project_root
            ):
                installed_drift.append(installed_key)
        if not project_src.exists():
            continue
        project_key = f"{provider_dir(provider)}/{bundle['project_config'][0]}"
        if project_root and provider != project_provider:
            project_scope_note = (
                f"the project runs {project_provider}, so the {provider} boundary "
                f"(<project_root>/{bundle['project_config'][1]}) was not checked"
            )
        elif project_root:
            project_dest = project_root / bundle["project_config"][1]
            if not project_dest.exists():
                installed_missing.append(project_key)
            elif _provider_config_would_change(
                provider, project_src, project_dest, project_root
            ):
                installed_drift.append(project_key)
        else:
            # Say so rather than pass quietly: an unchecked boundary is not a clean one.
            project_scope_note = (
                "no .workflow/ found from this directory — the project boundary "
                f"(<project_root>/{bundle['project_config'][1]}) was NOT checked. "
                "Run init there first."
            )

    from exoma.installer.stale import stale_files

    # A host dropped from the choice shows here: its files are no longer current targets.
    stale_removable, stale_edited = stale_files({str(dest) for _src, dest, _key in installed_targets})

    print("[INSTALL CHECK]")
    print(
        f"  hosts: {', '.join(hosts)}"
        + (f" (agy permissions: {'on' if agy_permissions else 'off'})" if agy else "")
    )
    print(
        f"  bundle (dist vs manifest): {'OK' if not bundle_stale else f'STALE ({len(bundle_stale)})'}"
    )
    for key in bundle_stale:
        print(f"    - {key}")
    installed_issues = len(installed_drift) + len(installed_missing)
    if installed_issues == 0:
        print("  installed (host config vs dist): READY")
    else:
        print(
            f"  installed (host config vs dist): DRIFTED "
            f"(drift {len(installed_drift)}, missing {len(installed_missing)})"
        )
        for key in installed_drift:
            print(f"    - DRIFTED {key}")
        for key in installed_missing:
            print(f"    - MISSING {key}")
    if stale_removable or stale_edited:
        print(
            f"  leftovers (installed by an earlier release, no longer shipped): "
            f"{len(stale_removable)} removable, {len(stale_edited)} edited since"
        )
        for path, _entry in stale_removable:
            print(f"    - STALE {path}")
        for path in stale_edited:
            print(f"    - STALE (edited, --apply keeps it) {path}")
    if project_scope_note:
        print(f"  project scope: SKIPPED — {project_scope_note}")
    if agy_refusal:
        print(f"  agy host: NOT INSTALLABLE — {agy_refusal[1]}")

    # Reported beside the drift list rather than inside it. This file has no shipped
    # counterpart to drift FROM, and its absence is not a fault: init falls back to
    # second_agent.example.json and works. Folding it into `installed_missing` would turn
    # a working checkout into DRIFTED and send the user to --apply for nothing.
    print(f"  second agent default: {_second_agent_state()}")

    # Report component ownership so the required version bump is explicit.
    versions = manifest.get("versions") or {}
    if versions:
        changed_keys = set(bundle_stale) | set(installed_drift) | set(installed_missing)
        if f"{CLAUDE_HOST}/settings.json" in changed_keys:
            changed_keys.add(CLAUDE_SETTINGS)
        # A drifted installed config means its template is what the version bump covers.
        for provider, bundle, _global_src, _project_src in provider_checks:
            template, installed = bundle["global_config"]
            if f"{provider_dir(provider)}/{installed}" in changed_keys:
                changed_keys.add(f"{provider_dir(provider)}/{template}")
        print("  components:")
        for comp, ver in versions.items():
            comp_keys = [k for k, e in by_path.items() if e.get("component") == comp]
            drifted = [k for k in comp_keys if k in changed_keys]
            state = "OK" if not drifted else f"CHANGED ({len(drifted)})"
            print(f"    {comp} v{ver}: {state}")

    if bundle_stale:
        status = "STALE"
    elif installed_issues or stale_removable:
        status = "DRIFTED"
    else:
        status = "READY"
    print(f"  status: {status}")
    if status != "READY":
        print(
            "  fix: python tools/maintain/gen_manifest.py (bundle) | python install.py --apply (installed)"
        )
    return 0 if status == "READY" else 1
