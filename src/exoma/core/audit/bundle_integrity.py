"""Compare the installed ~/.claude bundle against the shipped dist/ manifest.

Answers one question — has anything the workflow ships drifted since it was installed —
and stays clear of judgement about what to do next.
"""

import hashlib
import os
import re
from pathlib import Path

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

    A targets key is either an exact file (claude/CLAUDE.md) or a directory (claude/skills).
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
_MANAGED_MARKERS = {
    "claude/CLAUDE.md": ("WORKFLOW-MAIN-AGENT:START", "WORKFLOW-MAIN-AGENT:END"),
    **{
        f"{name}/{bundle['instructions'][0]}": _SECOND_AGENT_MARKERS
        for name, bundle in PROVIDER_BUNDLES.items()
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
        if agents_dir and text.startswith(f"{name}/{agents_dir}/"):
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


def _installed_intent_mode() -> bool:
    """True when the last install chose command-only (auto-intent stanza stripped)."""
    try:
        path = Path(os.path.expanduser("~")) / ".claude" / ".workflow-install-mode.json"
        return bool(read_json_file(path).get("only_command"))
    except (OSError, ValueError, AttributeError):
        return False


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
    for entry in files:
        rel = entry.get("path")
        installed = _installed_path_for(rel, targets, project_root)
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
        except OSError:
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
            except OSError:
                result["drift"].append({"path": rel, "reason": "dist_source_unreadable"})
                continue
            expected = expected.replace("{{HOME}}", home)
            if project_root:
                expected = expected.replace("{{PROJECT_ROOT}}", str(project_root))
            if rel == "claude/CLAUDE.md":
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

        if hashlib.sha256(text.encode("utf-8")).hexdigest() != entry.get("sha256"):
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
    hook_target = _installed_path_for("claude/hooks", targets, project_root)
    if hook_target is not None:
        result["hooks_installed"] = all(
            (hook_target / h).is_file() for h in required_hooks
        )
    return result
