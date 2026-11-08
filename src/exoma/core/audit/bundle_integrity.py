"""Compare the installed ~/.claude bundle against the shipped dist/ manifest.

Answers one question — has anything the workflow ships drifted since it was installed —
and stays clear of judgement about what to do next.
"""

import hashlib
import os
import re
from pathlib import Path

from exoma.config.bundle_keys import HOSTS_DIR, host_dir, provider_dir
from exoma.config.hosts import HOOK_DATA_FILES
from exoma.config.providers import PROVIDER_BUNDLES
from exoma.core.workspace.workspace_paths import read_json_file


def _expand_home(template: str, project_root: Path | str | None = None) -> str:
    """Resolve manifest target placeholders.

    `{{PROJECT_ROOT}}` marks a target that installs into the worktree rather than the
    user's home — now only the opencode.json boundary. With no project root known it
    degrades to home, so a caller without a workspace still resolves to a real path
    instead of a literal `{{PROJECT_ROOT}}` directory.
    """
    text = str(template).replace("{{HOME}}", os.path.expanduser("~"))
    if "{{PROJECT_ROOT}}" in text:
        text = text.replace(
            "{{PROJECT_ROOT}}",
            str(project_root) if project_root else os.path.expanduser("~"),
        )
    return text


def _installed_path_for(
    rel: str, targets: dict, project_root: Path | str | None = None
) -> Path | None:
    """Map a manifest dist-relative path to its installed location via the targets map.

    A targets key is either an exact file (hosts/claude/CLAUDE.md) or a directory
    (hosts/claude/skills).
    The longest matching key wins, so a file under a mapped dir resolves to
    <target_dir>/<remainder>.
    """
    if rel in targets:
        return Path(_expand_home(str(targets[rel]), project_root))
    best_key = None
    for key in targets:
        if rel == key or rel.startswith(key + "/"):
            if best_key is None or len(key) > len(best_key):
                best_key = key
    if best_key is None:
        return None
    remainder = rel[len(best_key) :].lstrip("/")
    return Path(_expand_home(str(targets[best_key]), project_root)) / remainder


# Files installed as a marker-delimited block inside a file the user also owns. Their
# bytes legitimately differ from dist: content outside the markers is the user's, and
# {{HOME}}/{{PROJECT_ROOT}} inside the block are resolved at install time. Comparing whole
# files here reported drift on every single run, which trained the reader to ignore it.
_SECOND_AGENT_MARKERS = ("WORKFLOW-SECOND-AGENT:START", "WORKFLOW-SECOND-AGENT:END")
# The Agy /.doctor fragment: appended to the Agy doctor skill at install (installer/base.py).
_RENDERED_INTO_ANOTHER_FILE = {f"{host_dir('agy')}/doctor.agy.md"}
_MANAGED_MARKERS = {
    f"{host_dir('claude')}/CLAUDE.md": ("WORKFLOW-MAIN-AGENT:START", "WORKFLOW-MAIN-AGENT:END"),
    **{
        f"{provider_dir(name)}/{bundle['instructions'][0]}": _SECOND_AGENT_MARKERS
        for name, bundle in PROVIDER_BUNDLES.items()
        if not bundle.get("instructions_whole")
    },
}


def _is_agent_roster_path(rel) -> bool:
    """True for a shipped subagent-roster file, for ANY provider.

    A roster is installed globally, so a project-scoped lookup that misses must retry in
    the user's home before calling the file absent. Derived from each bundle's
    `agents_dir` rather than the literal `opencode/agents/`: with the literal, a second
    provider's roster failed that retry and doctor reported it missing while it sat
    installed exactly where it belongs.
    """
    text = str(rel)
    for name, bundle in PROVIDER_BUNDLES.items():
        agents_dir = bundle.get("agents_dir")
        if agents_dir and text.startswith(f"{provider_dir(name)}/{agents_dir}/"):
            return True
    return False


def _installed_config_alias(installed: Path):
    """The alternate spelling of a provider config that WINS when it exists, or None.

    OpenCode reads `opencode.json` or `opencode.jsonc`; comparing against the canonical
    name alone reports drift on a user who keeps the other one. The aliases come from the
    bundles so this holds for every provider that declares any, and quietly does nothing
    for the ones that do not.
    """
    for bundle in PROVIDER_BUNDLES.values():
        entry = bundle.get("global_config")
        if not entry or installed.name != entry[1]:
            continue
        for alias in bundle.get("config_aliases", ()):
            candidate = installed.with_name(alias)
            if candidate.is_file():
                return candidate
    return None


def _install_mode() -> dict:
    try:
        mode = read_json_file(Path(os.path.expanduser("~")) / ".claude" / ".workflow-install-mode.json")
    except (OSError, ValueError):
        return {}
    return mode if isinstance(mode, dict) else {}


def _installed_intent_mode() -> bool:
    """True when the last install chose command-only (auto-intent stanza stripped)."""
    return bool(_install_mode().get("only_command"))


def _installed_hosts() -> set[str]:
    """The hosts the last install chose; Claude alone for an install from before the choice."""
    hosts = _install_mode().get("hosts")
    return {h for h in hosts if isinstance(h, str)} if isinstance(hosts, list) else {"claude"}


def _select_intent_section(text: str, only_command: bool) -> str:
    """Drop whichever of the two intent stanzas the installer would have dropped.

    Without this the dist copy carries both and the installed copy carries one, so the
    managed-block comparison reports a local edit on every command-only install — the
    exact false alarm this comparison was rewritten to remove.
    """
    drop = "AUTO-INTENT" if only_command else "COMMAND-ONLY"
    text = re.sub(
        rf"<!--\s*{drop}:START\s*-->.*?<!--\s*{drop}:END\s*-->\s*",
        "",
        text,
        flags=re.DOTALL,
    )
    # Mirror install._apply_intent_mode: the kept stanza's markers are stripped there too,
    # so comparing against a copy that still had them would report a phantom edit.
    return re.sub(
        r"[ \t]*<!--\s*(?:AUTO-INTENT|COMMAND-ONLY):(?:START|END)\s*-->\n?", "", text
    )


def _marker_block(text: str, start: str, end: str) -> str | None:
    """The marker-delimited region, markers included. None when absent."""
    match = re.search(
        rf"<!--\s*{re.escape(start)}.*?-->.*?<!--\s*{re.escape(end)}\s*-->",
        text or "",
        re.DOTALL,
    )
    return match.group(0) if match else None


def _matches_rendered(
    dist_config_dir: Path, rel: str, text: str, project_root: Path | str | None, only_command: bool
) -> bool:
    """True when `text` is exactly what an install writes for `rel`.

    Some shipped files are rendered at install, not copied: a skill names its host
    (`{{HOST}}`), the OpenCode plugin shim carries this machine's interpreter and hook path.
    Their bytes never match the manifest hash, so hashing alone reported every such file as
    drift on a clean install. The installer's own renderer is the reference, the one
    `exoma install --check` uses, so doctor and --check cannot disagree about a file.
    """
    try:
        from exoma.installer.base import _render_text

        root = Path(project_root) if project_root else None
        return _render_text(dist_config_dir / rel, rel, root, only_command) == text
    except Exception:
        return False


def _project_scoped_elsewhere(rel: str, targets: dict, project_root: Path | str | None) -> bool:
    """True for another provider's project-root file (`{{PROJECT_ROOT}}` target).

    init installs only the boundary of the provider the project runs
    (core.runtime.upgrade._install_project_boundary), so the others are absent by design;
    reporting them as `not_installed` named a drift no command could repair. Mirrors
    `exoma install --check`.
    """
    if not project_root or "{{PROJECT_ROOT}}" not in str(targets.get(rel, "")):
        return False
    from exoma.adapters.contract.registry import selected_provider

    selected = selected_provider(project_root)
    return any(
        rel.startswith(provider_dir(name) + "/") and name != selected for name in PROVIDER_BUNDLES
    )


_CODEX_AUTO_REVIEW_POLICY = f"{host_dir('codex')}/auto-review-policy.md"


def _audit_codex_auto_review(
    result: dict, rel: str, installed: Path | None, dist_config_dir: Path, home: Path
) -> None:
    """The owned Auto-review block's installation state, through the installer's classifier.

    installed/disabled are clean; missing goes to `missing`; outdated, removed, conflict,
    support-unverified, an unreadable template and a config that cannot be opened are
    drift with that reason. An absent config.toml is read as empty: with the context off
    that is clean, as it is for `install --check`. The config is decoded the way the
    installer reads it (`_read_text_lenient`), so an ANSI-written file gets the same
    verdict from both. Read-only.
    """
    from exoma.installer import codex_policy
    from exoma.installer.base import _read_text_lenient

    text = ""
    if installed is not None and installed.is_file():
        try:
            text = _read_text_lenient(installed)
        except OSError:
            result["mismatched"].append(rel)
            result["drift"].append({"path": rel, "reason": "unreadable"})
            return
    enabled = _install_mode().get("codex_auto_review_context") is True
    try:
        status = codex_policy.installation_status(text, dist_config_dir / rel, enabled, home)
    except Exception as exc:  # noqa: BLE001 — doctor reports, never raises
        status = f"unavailable ({type(exc).__name__})"
    result["checked"] += 1
    if status in ("installed", "disabled"):
        return
    if status == "missing":
        result["missing"].append(rel)
        result["drift"].append({"path": rel, "reason": "auto_review_context_missing"})
        return
    result["mismatched"].append(rel)
    result["drift"].append({"path": rel, "reason": f"auto_review_context: {status}"})


def _bundle_integrity(
    dist_config_dir: Path, manifest_path: Path, project_root: Path | str | None = None
) -> dict:
    """Verify the installed bundle matches the package's _bundle/manifest.json.

    For each file the manifest records, hash the INSTALLED copy the same way gen_manifest
    does (sha256 of read_text().encode(), CRLF-normalised by universal newlines) and compare.
    Also flags a manifest that no longer describes its bundle sources (stale) and any
    missing required hook.
    """
    result: dict = {
        "manifest": str(manifest_path),
        "checked": 0,
        "mismatched": [],
        "missing": [],
        # Every entry that is neither clean nor silently ignored, with WHY. A bare list of
        # paths says a file differs but not whether that is a real problem, and the two
        # need opposite responses: reinstall, or leave it alone.
        "drift": [],
        "skipped": [],
        "manifest_fresh": None,
        "hooks_installed": None,
    }
    try:
        manifest = read_json_file(manifest_path)
    except (OSError, ValueError):
        result["error"] = "manifest missing or invalid"
        return result
    files = manifest.get("files") if isinstance(manifest, dict) else None
    targets = manifest.get("targets") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not isinstance(targets, dict):
        result["error"] = "manifest has no files/targets"
        return result
    # Every entry is checked for shape before any is used. The loop below dereferences
    # `entry.get` and compares `sha256`, so a non-dict entry raised out of doctor and an
    # entry without a hash compared unequal and read as local drift — a broken manifest
    # reported as an edited install. Neither is: the manifest itself cannot be trusted.
    malformed = [
        index
        for index, entry in enumerate(files)
        if not isinstance(entry, dict)
        or not isinstance(entry.get("path"), str)
        or not entry.get("path")
        or not isinstance(entry.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
    ]
    if malformed:
        result["malformed"] = malformed
        result["error"] = (
            f"manifest has {len(malformed)} malformed file entr"
            f"{'y' if len(malformed) == 1 else 'ies'}"
        )
        return result

    home = os.path.expanduser("~")
    only_command = _installed_intent_mode()
    result["intent_mode"] = "command_only" if only_command else "auto_intent"
    hosts = _installed_hosts()
    # A refused Agy hook leaves GEMINI.md out too (both or neither, installer/check.py);
    # their absence is the refusal doctor reports under the host, not two missing files.
    agy_refused = "agy" in hosts and _agy_refusal() is not None
    for entry in files:
        rel = entry.get("path")
        # A host the install did not choose has nothing installed to compare.
        parts = rel.split("/")
        if parts[0] == HOSTS_DIR and len(parts) > 1 and parts[1] not in hosts:
            result["skipped"].append({"path": rel, "reason": "host_not_chosen"})
            continue
        if agy_refused and rel in _AGY_REFUSED_KEYS:
            result["skipped"].append({"path": rel, "reason": "host_refused"})
            continue
        # A fragment rendered into another installed file has no copy of its own.
        if rel in _RENDERED_INTO_ANOTHER_FILE:
            result["skipped"].append({"path": rel, "reason": "rendered_into_another_file"})
            continue
        if _project_scoped_elsewhere(rel, targets, project_root):
            result["skipped"].append({"path": rel, "reason": "provider_not_selected"})
            continue
        installed = _installed_path_for(rel, targets, project_root)
        # An Agy skill directory is installed dotted, so it answers to `/.<name>`.
        if installed is not None and rel.startswith(f"{host_dir('agy')}/skills/"):
            installed = installed.parent.parent / f".{installed.parent.name}" / installed.name
        if (
            _is_agent_roster_path(rel)
            and (installed is None or not installed.is_file())
            and project_root is not None
        ):
            global_agent = _installed_path_for(rel, targets, None)
            if global_agent is not None and global_agent.is_file():
                installed = global_agent
        if installed is not None:
            alias = _installed_config_alias(installed)
            if alias is not None:
                installed = alias
        if rel == _CODEX_AUTO_REVIEW_POLICY:
            # Merged into config.toml, so the generic merge branch below would accept any
            # file that exists. Classified like `install --check` instead.
            _audit_codex_auto_review(result, rel, installed, dist_config_dir, Path(home))
            continue
        if installed is None or not installed.is_file():
            result["missing"].append(rel)
            result["drift"].append({"path": rel, "reason": "not_installed"})
            continue
        # *.template.json is installed via {{HOME}} placeholder substitution + key-merge
        # into the user's settings.json/opencode.json, so its bytes legitimately diverge
        # from the shipped template. Verify presence only — an exact hash would false-alarm.
        if str(rel).endswith(".template.json"):
            result["checked"] += 1
            continue
        # Same reasoning for anything the manifest marks as merged rather than copied: the
        # installed file is dist merged INTO the user's, so it matches the shipped bytes only
        # while the user has added nothing. Hashing it reports drift for using the file.
        if entry.get("merge") == "merge":
            result["checked"] += 1
            continue
        try:
            text = installed.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            result["missing"].append(rel)
            result["drift"].append({"path": rel, "reason": "unreadable"})
            continue
        result["checked"] += 1

        if rel in _MANAGED_MARKERS:
            # Compare the managed block alone, against a dist copy put through the same
            # placeholder substitution the installer applies. What is left after that is
            # a genuine local edit.
            start, end = _MANAGED_MARKERS[rel]
            source = dist_config_dir / rel
            try:
                expected = source.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                result["drift"].append({"path": rel, "reason": "dist_source_unreadable"})
                continue
            expected = expected.replace("{{HOME}}", home)
            if project_root:
                expected = expected.replace("{{PROJECT_ROOT}}", str(project_root))
            if rel == f"{host_dir('claude')}/CLAUDE.md":
                # The contract is rendered for the host at install (config/hosts.py:
                # host sections, placeholders, rule labels): compare against the
                # installer's own render, the one `install --check` uses.
                try:
                    from exoma.installer.base import _render_text

                    root = Path(project_root) if project_root else None
                    expected = _render_text(source, rel, root, only_command)
                except Exception:
                    expected = _select_intent_section(expected, only_command)
            want = _marker_block(expected, start, end)
            got = _marker_block(text, start, end)
            if got is None:
                result["mismatched"].append(rel)
                result["drift"].append({"path": rel, "reason": "managed_block_absent"})
            elif want is not None and got.strip() != want.strip():
                result["mismatched"].append(rel)
                result["drift"].append({"path": rel, "reason": "locally_edited"})
            continue

        if hashlib.sha256(text.encode("utf-8")).hexdigest() != entry.get(
            "sha256"
        ) and not _matches_rendered(dist_config_dir, rel, text, project_root, only_command):
            result["mismatched"].append(rel)
            result["drift"].append({"path": rel, "reason": "content_differs"})

    # Fresh = every source the manifest lists still hashes to what it records, the same way
    # gen_manifest hashes it. By content, not mtime: an installed package's mtimes are
    # whatever order the wheel was unpacked in, and read as stale on a clean install.
    try:
        result["manifest_fresh"] = all(
            hashlib.sha256(
                (dist_config_dir / entry["path"]).read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest()
            == entry["sha256"]
            for entry in files
        )
    except (OSError, UnicodeDecodeError):
        result["manifest_fresh"] = False

    # The same on every OS: each gate hook is a bash launcher plus the .py it runs.
    required_hooks = [
        "workflow_hooks.py",
        *(f"{stem}.{ext}" for stem in ("session-bind", "intent-gate-set", "intent-gate-check") for ext in ("sh", "py")),
    ]
    hook_target = _installed_path_for(f"{host_dir('claude')}/hooks", targets, project_root)
    if hook_target is not None:
        result["hooks_installed"] = all(
            (hook_target / h).is_file() for h in required_hooks
        )
    return result


# What each host's install leaves, by host. File presence only: whether the host CLI LOADS
# them is something only a session in that host can observe (the Agy `/.doctor` skill asks
# the model to look).
_AGY_HOOK_FILES = ("agy-host.py", *HOOK_DATA_FILES)


def _has_block(path: Path, start: str) -> bool:
    try:
        return f"<!-- {start}" in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


# The two Agy files the installer writes together or not at all (installer/base.py).
_AGY_REFUSED_KEYS = ("hosts/agy/GEMINI.md", "hosts/agy/hooks.template.json")
_AGY_REFUSED_MISSING = ("~/.gemini/GEMINI.md", "~/.gemini/config/hooks.json")


def _agy_refusal(dest: Path | None = None, home: Path | None = None) -> str | None:
    """Why the installer would not put Exoma's Agy hook in on this machine, else None.

    Asked of the installer itself (agy_hooks_refusal), so doctor and `exoma install --check`
    give one answer: a spaced interpreter with no plain fallback, a spaced home, or a
    hooks.json that is not Exoma's to rewrite.
    """
    from exoma.installer.base import agy_hooks_dest, agy_hooks_refusal

    try:
        refusal = agy_hooks_refusal(dest or agy_hooks_dest(), home)
    except (OSError, ValueError):
        return None
    return refusal[1] if refusal else None


def _host_status(home: Path | None = None) -> dict:
    """`{chosen, hosts: {<host>: {missing: [...], ...}}}` for the hosts the last install chose.

    Read from the install mode file, so `both` reports both and a host not chosen is not
    reported missing. An install from before the choice recorded none: Claude, plus Agy when
    its hooks.json carries Exoma's hook (installer/rollback._migrated_hosts, restated here).
    A lost mode file also adds Codex when ~/.codex/hooks.json registers Exoma's lifecycle
    hooks, so a Codex install is not dropped from the report.
    """
    from exoma.utils.osutil import agent_home

    home = Path(home or agent_home())
    try:
        mode = read_json_file(home / ".claude" / ".workflow-install-mode.json")
    except (OSError, ValueError):
        mode = {}
    mode = mode if isinstance(mode, dict) else {}
    chosen = mode.get("hosts")
    if isinstance(chosen, list):
        chosen = [h for h in chosen if isinstance(h, str)]
    else:
        try:
            legacy_hooks = read_json_file(home / ".gemini" / "config" / "hooks.json")
        except (OSError, ValueError):
            legacy_hooks = {}
        chosen = ["claude", "agy"] if isinstance(legacy_hooks, dict) and "exoma" in legacy_hooks else ["claude"]
        try:
            codex_hooks = read_json_file(home / ".codex" / "hooks.json")
        except (OSError, ValueError):
            codex_hooks = {}
        if isinstance(codex_hooks, dict):
            from exoma.installer.base import codex_hook_events_present

            if codex_hook_events_present(codex_hooks.get("hooks"), home):
                chosen.append("codex")
        if (home / ".config" / "opencode" / "plugins" / "exoma.js").is_file():
            chosen.append("opencode")
    result: dict = {"chosen": chosen, "hosts": {}}
    if "claude" in chosen:
        claude = home / ".claude"
        missing = []
        if not _has_block(claude / "CLAUDE.md", "WORKFLOW-MAIN-AGENT:START"):
            missing.append("~/.claude/CLAUDE.md (WORKFLOW-MAIN-AGENT block)")
        if not (claude / "skills" / "doctor.md").is_file():
            missing.append("~/.claude/skills")
        if not (claude / "hooks" / "session-bind.sh").is_file():
            missing.append("~/.claude/hooks")
        result["hosts"]["claude"] = {"missing": missing}
    if "agy" in chosen:
        gemini = home / ".gemini"
        cli = gemini / "antigravity-cli"
        missing = []
        if not _has_block(gemini / "GEMINI.md", "EXOMA-HOST:START"):
            missing.append("~/.gemini/GEMINI.md (EXOMA-HOST block)")
        try:
            hooks = read_json_file(gemini / "config" / "hooks.json")
        except (OSError, ValueError):
            hooks = {}
        if not (isinstance(hooks, dict) and "exoma" in hooks):
            missing.append("~/.gemini/config/hooks.json (exoma hook)")
        if not (cli / "exoma" / "CONTRACT.md").is_file():
            missing.append("~/.gemini/antigravity-cli/exoma/CONTRACT.md")
        for name in _AGY_HOOK_FILES:
            if not (cli / "exoma" / "hooks" / name).is_file():
                missing.append(f"~/.gemini/antigravity-cli/exoma/hooks/{name}")
        if not (cli / "skills" / ".doctor" / "SKILL.md").is_file():
            missing.append("~/.gemini/antigravity-cli/skills")
        permissions = mode.get("agy_permissions") is True
        if permissions:
            try:
                settings = read_json_file(cli / "settings.json")
                allow = settings.get("permissions", {}).get("allow", [])
            except (OSError, ValueError, AttributeError):
                allow = []
            # Every rule this machine needs, not only the ones Exoma recorded adding: a rule
            # the user already had is not Exoma's to remove, but the runner still needs it.
            from exoma.installer.base import agy_permission_rules

            if not isinstance(allow, list) or any(rule not in allow for rule in agy_permission_rules()):
                missing.append("~/.gemini/antigravity-cli/settings.json (permission rules)")
        result["hosts"]["agy"] = {"missing": missing, "permissions": "on" if permissions else "off"}
        refusal = _agy_refusal(gemini / "config" / "hooks.json", home)
        if refusal:
            result["hosts"]["agy"]["missing"] = [
                m for m in missing if not m.startswith(_AGY_REFUSED_MISSING)
            ]
            result["hosts"]["agy"]["refused"] = refusal
            # What `exoma install --check` reports as drift (installer/check.py): an earlier
            # install's block that the next install takes out, since no hook enforces it.
            # Same test as --check (one helper): a whole block, or markers that do not pair
            # up (an END alone, a START alone, more of one than the other).
            from exoma.installer.base import agy_leftover_block

            if agy_leftover_block(gemini / "GEMINI.md"):
                result["hosts"]["agy"]["leftover_block"] = "~/.gemini/GEMINI.md (EXOMA-HOST block)"
    if "codex" in chosen:
        from exoma.installer.base import codex_hook_events_present

        codex = home / ".codex"
        missing = []
        if not _has_block(codex / "AGENTS.md", "EXOMA-CODEX-HOST:START"):
            missing.append("~/.codex/AGENTS.md (EXOMA-CODEX-HOST block)")
        if not (codex / "exoma" / "CONTRACT.md").is_file():
            missing.append("~/.codex/exoma/CONTRACT.md")
        if not (codex / "exoma" / "hooks" / "codex-host.py").is_file():
            missing.append("~/.codex/exoma/hooks/codex-host.py")
        if not (codex / "skills" / "exoma-doctor" / "SKILL.md").is_file():
            missing.append("~/.codex/skills/exoma-doctor/SKILL.md")
        try:
            hooks = read_json_file(codex / "hooks.json")
        except (OSError, ValueError):
            hooks = {}
        events = hooks.get("hooks") if isinstance(hooks, dict) else None
        if not codex_hook_events_present(events, home):
            missing.append("~/.codex/hooks.json (Exoma lifecycle hooks)")
        result["hosts"]["codex"] = {"missing": missing, "hook_trust": "check with /hooks"}
    if "opencode" in chosen:
        opencode = home / ".config" / "opencode"
        missing = []
        if not _has_block(opencode / "AGENTS.md", "EXOMA-OPENCODE-HOST:START"):
            missing.append("~/.config/opencode/AGENTS.md (EXOMA-OPENCODE-HOST block)")
        for rel in ("plugins/exoma.js", "exoma/CONTRACT.md", "exoma/hooks/opencode-host.py",
                    "exoma/hooks/workflow_policy.py", "skills/exoma-doctor/SKILL.md",
                    "commands/exoma-doctor.md"):
            if not (opencode / rel).is_file():
                missing.append("~/.config/opencode/" + rel)
        result["hosts"]["opencode"] = {"missing": missing, "plugin_load": "restart opencode after an install"}
    return result
