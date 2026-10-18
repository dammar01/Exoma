"""settings.json and the second_agent provider config: hook merging and policy enforcement.

Kept apart from the check layer because these WRITE. The distinction matters for
rollback: everything here appends to the receipt, and a check must not.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

from utils import osutil
from config.providers import (
    bundle_for,
    provider_config_path,
    provider_install_module,
)
from installer.base import (
    HOME,
    REPO_ROOT,
    SETTINGS_REQUIRED,
    Plan,
    _backup,
    _file_sha256,
    _read_text_lenient,
    _record,
    _resolve_in_json,
    _resolve_placeholders,
)

# A script path inside a command: quoted (the shape every shipped command uses) or a bare
# token. The trailing guard keeps `tool.shell` or `x.sh2` from reading as a script.
_SCRIPT_PATH = re.compile(
    r"""(?:"([^"]*?\.(?:ps1|sh))"|'([^']*?\.(?:ps1|sh))'|([^\s"'`;|&<>()]+\.(?:ps1|sh)))(?![\w.-])""",
    re.IGNORECASE,
)
# Spellings of the home directory a hand-written or older command may use in place of the
# absolute path the installer renders `{{HOME}}` to.
_HOME_ALIASES = ("~", "$home", "${home}", "%userprofile%", "$env:userprofile", "$env:home")
# The parent of `.claude` when it is SOME user's home: a settings.json synced from another
# machine, or a renamed profile, still names our hooks dir under its old home.
_HOME_SHAPED = re.compile(r"^(?:(?:[a-z]:|/[a-z]|/mnt/[a-z])/users/[^/]+|/users/[^/]+|/home/[^/]+|/root)$")


def _norm_path(path: str) -> str:
    """One spelling for a path as it appears in a command: forward slashes, no repeated or
    trailing slash, lower case (Windows paths are case-insensitive, and a POSIX home that
    differs only in case is not a collision worth guarding)."""
    path = re.sub(r"/+", "/", path.strip().replace("\\", "/")).lower()
    return path.rstrip("/") if len(path) > 1 else path


def _is_our_hooks_dir(directory: str) -> bool:
    """True when `directory` (normalised) is the hooks dir the installer writes to."""
    home, sep, tail = directory.rpartition("/.claude/hooks")
    if not sep or tail:
        return False
    return home in {_norm_path(str(HOME)), *_HOME_ALIASES} or bool(_HOME_SHAPED.match(home))


def _command_script_ids(cmd: str) -> set[str]:
    """Stems of OUR hook scripts one command runs; empty when it runs anything else.

    A script counts as ours only when it sits in the installed hooks dir (`<home>/.claude/
    hooks/`); a user's `~/tools/task-events.sh` shares a stem and nothing else. A command
    that also runs a script from anywhere else is the user's own composition and is left
    whole — refreshing or stripping it would take their half with ours.
    """
    ids: set[str] = set()
    for match in _SCRIPT_PATH.finditer(cmd if isinstance(cmd, str) else ""):
        directory, _sep, name = _norm_path(next(g for g in match.groups() if g)).rpartition("/")
        if not _is_our_hooks_dir(directory):
            return set()
        ids.add(name.rsplit(".", 1)[0])
    return ids


def _hook_script_ids(entry: dict) -> set[str]:
    """Stems of the workflow hook scripts an entry invokes (e.g. `intent-gate-check`).

    Script stems define workflow ownership, but only for a script inside the installed
    hooks dir (see `_command_script_ids`); callers then intersect with the stems a release
    ships. Extensions are ignored so `.ps1` and `.sh` variants collapse to one logical
    hook during a cross-platform install.
    """
    ids: set[str] = set()
    hooks = entry.get("hooks", []) if isinstance(entry, dict) else []
    for h in hooks if isinstance(hooks, list) else []:
        cmd = h.get("command", "") if isinstance(h, dict) else ""
        ids |= _command_script_ids(cmd)
    return ids


def _merge_hook_entries(cur_entries: list, tmpl_entries: list) -> tuple[list, int]:
    """Refresh OUR shipped hook entries (identified by the script they call), append any
    shipped entry we don't yet have, and leave every foreign entry untouched.

    Every entry sharing a shipped script stem collapses into one current entry. A user hook
    that runs none of those scripts is never modified.
    """
    result = list(cur_entries)
    updated = 0
    for tmpl_entry in tmpl_entries:
        tids = _hook_script_ids(tmpl_entry)
        if not tids:
            if tmpl_entry not in result:
                result.append(tmpl_entry)
                updated += 1
            continue
        matches = [i for i, e in enumerate(result) if _hook_script_ids(e) & tids]
        if not matches:
            result.append(tmpl_entry)
            updated += 1
            continue
        first = matches[0]
        already_current = len(matches) == 1 and result[first] == tmpl_entry
        preserved: list[dict] = []
        for i in matches:
            entry = result[i]
            if not isinstance(entry, dict):
                continue
            kept_hooks = [
                hook
                for hook in entry.get("hooks", [])
                if not (_hook_script_ids({"hooks": [hook]}) & tids)
            ]
            if kept_hooks:
                kept_entry = json.loads(json.dumps(entry))
                kept_entry["hooks"] = kept_hooks
                preserved.append(kept_entry)
        for i in reversed(matches):
            del result[i]
        result[first:first] = [tmpl_entry, *preserved]
        if not already_current:
            updated += 1
    return result, updated


# Where a shipped command names its interpreter. The template is the same on every OS
# (`{{HOOK_BASH}} "{{HOME}}/.claude/hooks/<stem>.sh"`); only the bash differs.
_HOOK_BASH = "{{HOOK_BASH}}"


def hook_bash_command() -> str | None:
    """How a shipped hook command names bash, or None when this machine has none.

    POSIX: `bash`. Windows: the absolute path of Git Bash, quoted. A bare `bash` there can
    resolve to WSL's System32\\bash.exe, which cannot read the Windows paths these
    commands carry, and Claude Code shows a hook that cannot start as nothing at all.
    """
    if not osutil.IS_WINDOWS:
        return "bash"
    found = osutil.find_bash()
    return '"' + found.replace("\\", "/") + '"' if found else None


def _render_command(cmd: str, bash: str) -> str:
    """One shipped command with its interpreter filled in and the home path in forward
    slashes (bash reads `C:/Users/x` but takes `\\U` in `C:\\Users` as an escape)."""
    return cmd.replace("\\", "/").replace(_HOOK_BASH, bash) if _HOOK_BASH in cmd else cmd


def _render_hook_commands(template: dict, bash: str | None = None) -> dict:
    """Fill in `{{HOOK_BASH}}` in every shipped command, `statusLine` included.

    `statusLine` sits beside `hooks` rather than inside it, but it is the same shipped
    script under a different key: a renderer that walked only `hooks` would install a
    statusline that names no interpreter, and Claude Code renders a broken statusline as an
    empty one. A user's own command carries no placeholder and is left untouched. With no
    bash on the machine the placeholder becomes `bash`, so a dry run still shows a command;
    install refuses --apply before that matters (install.py).
    """
    bash = bash or hook_bash_command() or "bash"
    status = template.get("statusLine")
    if isinstance(status, dict) and isinstance(status.get("command"), str):
        status["command"] = _render_command(status["command"], bash)
    hooks = template.get("hooks")
    if not isinstance(hooks, dict):
        return template
    for entries in hooks.values():
        for entry in entries if isinstance(entries, list) else []:
            for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    hook["command"] = _render_command(hook["command"], bash)
    return template


def _remove_intent_hook_entries(settings: dict) -> tuple[dict, int]:
    out = json.loads(json.dumps(settings))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out, 0
    entries = hooks.get("UserPromptSubmit")
    if not isinstance(entries, list):
        return out, 0
    kept: list = []
    removed = 0
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        hooks_in = entry.get("hooks")
        if not isinstance(hooks_in, list):
            kept.append(entry)
            continue
        hooks_out = []
        for hook in hooks_in:
            if "intent-gate-set" in _hook_script_ids({"hooks": [hook]}):
                removed += 1
            else:
                hooks_out.append(hook)
        if hooks_out:
            retained = json.loads(json.dumps(entry))
            retained["hooks"] = hooks_out
            kept.append(retained)
    if kept:
        hooks["UserPromptSubmit"] = kept
    else:
        hooks.pop("UserPromptSubmit", None)
    return out, removed


def _drop_intent_hook(template: dict, plan: Plan) -> dict:
    """Remove the UserPromptSubmit entries that run intent-gate-set from the template.

    Only entries that invoke OUR script go: a user's own UserPromptSubmit hook on the same
    event is theirs, and an installer that removed it would be doing exactly what this
    flag exists to prevent.
    """
    out, removed = _remove_intent_hook_entries(template)
    if not removed:
        return template
    plan.warn(
        "only-command: UserPromptSubmit intent-gate-set hook not registered "
        "(auto-intent runtime gate stays off)"
    )
    return out


def _drop_retired_hooks(hooks: dict, retired: set[str]) -> tuple[dict, int]:
    """Remove hook commands that run a script an earlier release shipped and this one does not.

    Only commands naming a RETIRED stem go — a stem an install recorded writing and dist no
    longer carries. A user's hook calling their own script is never touched, and an entry
    that also runs something else keeps its other commands.
    """
    if not retired:
        return hooks, 0
    removed = 0
    out: dict = {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            out[event] = entries
            continue
        kept_entries = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept_entries.append(entry)
                continue
            kept_hooks = []
            for hook in entry["hooks"]:
                stems = _hook_script_ids({"hooks": [hook]})
                if stems and stems <= retired:
                    removed += 1
                else:
                    kept_hooks.append(hook)
            if kept_hooks:
                kept_entries.append({**entry, "hooks": kept_hooks})
        if kept_entries:
            out[event] = kept_entries
    return out, removed


def _drop_relocated_hooks(hooks: dict, tmpl_hooks: dict) -> tuple[dict, int]:
    """Remove commands for a shipped script from events the template no longer runs it on.

    `_merge_hook_entries` works one event at a time, so a script that moved — task-events
    left PreToolUse in 3.8.0 — kept its old entry forever. A shipped script is ours to
    place: on an event whose template entries do not name it, its command goes. A user's
    own script is never shipped, so it is never touched.
    """
    def _ids(entries) -> set[str]:
        out: set[str] = set()
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                out |= _hook_script_ids(entry)
        return out

    shipped: set[str] = set()
    for entries in (tmpl_hooks or {}).values():
        shipped |= _ids(entries)
    removed = 0
    out: dict = {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            out[event] = entries
            continue
        allowed = _ids((tmpl_hooks or {}).get(event))
        kept_entries = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept_entries.append(entry)
                continue
            kept_hooks = []
            for hook in entry["hooks"]:
                stems = _hook_script_ids({"hooks": [hook]})
                if stems and stems <= shipped and not stems & allowed:
                    removed += 1
                else:
                    kept_hooks.append(hook)
            if kept_hooks:
                kept_entries.append({**entry, "hooks": kept_hooks})
        if kept_entries:
            out[event] = kept_entries
    return out, removed


def _install_settings(
    src: Path,
    dest: Path,
    plan: Plan,
    apply: bool,
    backup_root: Path,
    only_command: bool = False,
) -> None:
    """Add missing keys only. An existing value is the user's decision, not a conflict
    for this script to resolve — except `hooks`, without which the workflow cannot bind
    a session at all, and which is therefore reported loudly when it differs.

    Two shipped things are refreshed rather than kept: hook commands for scripts a release
    retired (they would call a file that is gone), and a `statusLine` that runs one of the
    workflow's own scripts (a changed command never reached existing installs)."""
    from installer.stale import retired_hook_stems, shipped_hook_stems

    retired = retired_hook_stems()
    owned_stems = shipped_hook_stems() | retired
    template = _resolve_in_json(json.loads(src.read_text(encoding="utf-8")), None)
    template = _render_hook_commands(template)
    if only_command:
        # The runtime half of auto-intent. Dropping the prompt stanza while leaving this
        # hook registered would keep the gate blocking gather tools on a classification
        # the prompt no longer performs.
        template = _drop_intent_hook(template, plan)
    if not dest.exists():
        plan.add("create", dest, f"{len(template)} key(s)")
        if apply:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(template, indent=2) + "\n", encoding="utf-8")
            _record("create", dest, "claude/settings.json", None, None)
        return

    try:
        current = json.loads(_read_text_lenient(dest))
    except json.JSONDecodeError as exc:
        plan.warn(f"{dest} is not valid JSON ({exc}); left untouched")
        return
    if not isinstance(current, dict):
        plan.warn(f"{dest} root is not a JSON object; left untouched")
        return

    added = [k for k in template if k not in current]
    differing = [k for k in template if k in current and current[k] != template[k]]

    status_refresh = False
    if "statusLine" in differing:
        current_status = current.get("statusLine")
        stems = _hook_script_ids({"hooks": [current_status]}) if isinstance(current_status, dict) else set()
        if stems and stems <= owned_stems:
            differing.remove("statusLine")
            status_refresh = True

    # Refresh shipped hook entries while preserving hooks owned by the user.
    hook_changes: list[str] = []
    merged_hooks: dict | None = None
    if "hooks" in differing:
        differing.remove("hooks")
        tmpl_hooks = (
            template.get("hooks") if isinstance(template.get("hooks"), dict) else {}
        )
        cur_hooks = (
            current.get("hooks") if isinstance(current.get("hooks"), dict) else {}
        )
        merged_hooks = dict(cur_hooks or {})
        if only_command:
            cleaned, removed = _remove_intent_hook_entries({"hooks": merged_hooks})
            merged_hooks = cleaned.get("hooks", {})
            if removed:
                hook_changes.append(
                    f"hooks.UserPromptSubmit (removed {removed} shipped intent entr"
                    f"{'y' if removed == 1 else 'ies'})"
                )
        for event, tmpl_entries in (tmpl_hooks or {}).items():
            if event not in merged_hooks:
                merged_hooks[event] = tmpl_entries
                hook_changes.append(f"hooks.{event} (added)")
                continue
            current_entries = merged_hooks[event]
            if not isinstance(tmpl_entries, list) or not isinstance(
                current_entries, list
            ):
                # Non-list shape we do not understand: keep the user's, report it.
                if current_entries != tmpl_entries:
                    plan.warn(
                        f"settings.json[hooks.{event}] differs from the shipped template — "
                        "kept yours (your hook wins)"
                    )
                continue
            new_entries, updated = _merge_hook_entries(current_entries, tmpl_entries)
            if updated:
                merged_hooks[event] = new_entries
                hook_changes.append(
                    f"hooks.{event} (refreshed {updated} shipped entr"
                    f"{'y' if updated == 1 else 'ies'})"
                )
        merged_hooks, relocated = _drop_relocated_hooks(merged_hooks, tmpl_hooks or {})
        if relocated:
            hook_changes.append(
                f"hooks (removed {relocated} shipped command{'s' if relocated != 1 else ''} "
                "from events this release no longer registers them on)"
            )
        if merged_hooks == (cur_hooks or {}):
            merged_hooks = None

    retire_base = merged_hooks if merged_hooks is not None else (
        current.get("hooks") if isinstance(current.get("hooks"), dict) else None
    )
    if retire_base:
        cleaned, dropped = _drop_retired_hooks(retire_base, retired)
        if dropped:
            merged_hooks = cleaned
            hook_changes.append(
                f"hooks (removed {dropped} command{'s' if dropped != 1 else ''} for retired scripts: "
                f"{', '.join(sorted(retired))})"
            )
    if status_refresh:
        hook_changes.append("statusLine (refreshed the shipped command)")

    for key in differing:
        level = "REQUIRED" if key in SETTINGS_REQUIRED else "kept"
        plan.warn(
            f"settings.json[{key}] differs from the shipped template — {level} yours "
            f"({'the workflow may not bind sessions without the shipped hook' if key in SETTINGS_REQUIRED else 'your value wins'})"
        )

    if not added and not hook_changes:
        plan.add("unchanged", dest, "no missing keys")
        return

    pre_sha256 = _file_sha256(dest)
    saved = _backup(dest, backup_root, plan, apply, "claude/settings.json")
    detail = ", ".join([*added, *hook_changes])
    plan.add("merge", dest, f"update {detail}")
    if apply:
        current.update({k: template[k] for k in added})
        if status_refresh:
            current["statusLine"] = template["statusLine"]
        if merged_hooks is not None:
            current["hooks"] = merged_hooks
        dest.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        _record("merge", dest, "claude/settings.json", saved, pre_sha256)


def _provider_config_path(provider: str) -> Path:
    """The native config file to merge into, resolved from the provider's bundle."""
    return provider_config_path(provider, HOME)


def _install_provider_config(
    provider: str,
    src: Path,
    dest: Path,
    plan: Plan,
    apply: bool,
    backup_root: Path,
    project_root: Path | None,
    key: str | None = None,
) -> None:
    """Preserve unrelated provider config while enforcing workflow permissions.

    MCP servers, providers, and other agents remain additive. Environment placeholders
    are resolved after preflight proves the required values exist.

    Reading and merging both go through the provider's `install_module`: what counts as a
    permission, and where it lives in the file, is the provider's shape rather than
    something this layer can assume.

    `key` names the receipt/backup slot, defaulting to the global config's slot. The
    global and the project config both come through here, and a shared key would have
    them overwrite each other's backup — the second install would then be unrollbackable.
    """
    if key is None:
        key = f"{provider}/{bundle_for(provider)['global_config'][1]}"
    policy = provider_install_module(provider)
    incoming = json.loads(
        _resolve_placeholders(src.read_text(encoding="utf-8"), project_root)
    )
    current: dict = {}
    if dest.exists():
        try:
            current = policy.load_config(dest)
        except json.JSONDecodeError:
            plan.warn(
                f"{dest} is not valid JSON/JSONC — skipped (fix or remove it, then rerun)"
            )
            return
        if not isinstance(current, dict):
            plan.warn(
                f"{dest} root is not a JSON object; skipped (replace it with an object, then rerun)"
            )
            return
    merged, added, enforced = policy.merge_policy(current, incoming, plan.warn)
    if merged == current and enforced == 0:
        plan.add("unchanged", dest)
        return
    if dest.exists():
        pre_sha256 = _file_sha256(dest)
        saved = _backup(dest, backup_root, plan, apply, key)
        plan.add(
            "merge",
            dest,
            f"add {added} workflow key(s), enforce {enforced} permission key(s)",
        )
    else:
        pre_sha256 = None
        saved = None
        plan.add("create", dest)
    if apply:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        _record("merge" if saved else "create", dest, key, saved, pre_sha256)


E2E_REQUIREMENTS = "requirements-e2e.txt"
E2E_BROWSER = "chromium"


def _pip_install(plan: Plan, apply: bool, requirements: Path, empty_detail: str) -> str:
    """One `pip install -r`. Returns "empty", "planned", "installed", or "failed"."""
    body = [
        line.strip()
        for line in requirements.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not body:
        plan.add("skip", "pip install", empty_detail)
        return "empty"
    plan.add("run", f"pip install -r {requirements}", f"{len(body)} package(s)")
    if not apply:
        return "planned"
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        plan.warn(f"pip install failed: {result.stderr.strip()[-300:]}")
        return "failed"
    return "installed"


def _install_deps(plan: Plan, apply: bool, with_e2e: bool = False) -> None:
    requirements = REPO_ROOT / "requirements.txt"
    if requirements.exists():
        _pip_install(plan, apply, requirements, "no runtime dependencies declared")
    if with_e2e:
        _install_e2e_deps(plan, apply)


def _install_e2e_deps(plan: Plan, apply: bool) -> None:
    """--with-e2e: the optional Playwright package, then the browser it drives.

    Opt-in because only /.verify-browser uses it and the browser is a large download; the
    runtime itself stays stdlib-only. The browser step runs through the same interpreter
    pip installed into, so `playwright` resolves to the package just installed.
    """
    requirements = REPO_ROOT / E2E_REQUIREMENTS
    if not requirements.exists():
        plan.warn(f"--with-e2e: {E2E_REQUIREMENTS} is missing from this checkout; nothing installed")
        return
    outcome = _pip_install(plan, apply, requirements, f"{E2E_REQUIREMENTS} declares no packages")
    if outcome in ("empty", "failed"):
        plan.warn(f"--with-e2e: {E2E_BROWSER} download skipped because the playwright package is not installed")
        return
    plan.add(
        "run",
        f"python -m playwright install {E2E_BROWSER}",
        "browser for /.verify-browser (large download, needs network)",
    )
    if apply:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", E2E_BROWSER],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-300:]
            plan.warn(f"playwright install {E2E_BROWSER} failed: {detail}")
