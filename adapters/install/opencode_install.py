"""OpenCode-specific workspace installation.

Held apart from core/ on purpose: this is the only place that knows
the provider's config filenames and CLI name, so a second provider
gets a sibling module instead of edits scattered through the runtime.

`config/providers.py` names this module as opencode's `install_module`, so the three
functions dispatched through it — `load_config`, `merge_policy`, and
`install_project_config` — are the provider-facing contract. Everything else here is
opencode's own business."""

import json
import shutil
from pathlib import Path

from core.workspace.workspace_paths import (
    JSON_INDENT,
    PROVIDER_CONFIG_NAME,
    PROVIDER_SEED_NAME,
    WORKFLOW_DIRNAME,
    atomic_write_json,
    atomic_write_text,
    read_json_file,
)

def load_config(path: Path):
    """Read one of this provider's config files. Tolerates JSONC, because opencode does."""
    from core.policy.opencode_policy import load_json_or_jsonc

    return load_json_or_jsonc(path)


def merge_policy(current: dict, incoming: dict, warn) -> tuple[dict, int, int]:
    """Merge shipped config into an existing one, enforcing workflow-owned permissions.

    The installer reaches this by name, never by importing `core.policy.opencode_policy`
    directly — that module encodes opencode's `agent.plan.permission` + root `permission`
    shape, which is one provider's answer rather than a contract every provider can meet.
    """
    from core.policy.opencode_policy import merge_opencode_policy

    return merge_opencode_policy(current, incoming, warn)


def _copy_provider_config(project_root: Path, tool_dir: str) -> str | None:
    """Copy the tool seed into .workflow so the project has its own, overridable config.

    The source is `config/second_agent.seed.json`, which install.py rebuilds from the
    shipped example on every --apply. The old source, `config/second_agent.json`, was
    written once and never refreshed, so whatever it held on the day it was created was
    copied into every project initialised after — including `opencode/mimo-v2.5-free`.
    """
    dest = project_root / WORKFLOW_DIRNAME / PROVIDER_CONFIG_NAME
    if dest.exists():
        return str(dest)  # already project-local; never overwrite user edits
    src = Path(tool_dir) / "config" / PROVIDER_SEED_NAME
    if not src.exists():
        src = Path(tool_dir) / "config" / "second_agent.example.json"
    if src.exists():
        shutil.copyfile(src, dest)
        return str(dest)
    return None


def install_project_config(project_root: Path, tool_dir: str) -> dict:
    """Install/refresh <project_root>/opencode.json — the secret-file boundary the
    second_agent runs under.

    Init owns this rather than install.py: the boundary belongs to a workspace, and a
    project scaffolded without it is exactly the gap doctor reports. Permissions are
    ENFORCED on every call (init and upgrade alike), so a boundary someone edited loose is
    repaired; every other key in the file stays the user's.

    Reached by name through the bundle's `install_module`, never called directly by the
    runtime: which file the boundary lives in, and what a permission looks like inside it,
    is opencode's answer rather than a shape every provider shares.
    """
    from config.providers import bundle_for

    bundle = bundle_for("opencode")
    src_name, dest_name = bundle["project_config"]
    src = Path(tool_dir) / "dist" / "config" / "opencode" / src_name
    dest = project_root / dest_name
    result: dict = {
        "path": str(dest),
        "status": "source_missing",
        "keys_added": 0,
        "permissions_enforced": 0,
        "warnings": [],
    }
    if not src.exists():
        result["path"] = None
        return result

    warnings: list[str] = result["warnings"]
    incoming = json.loads(src.read_text(encoding="utf-8"))
    current: dict = {}
    if dest.exists():
        try:
            current = load_config(dest)
        except json.JSONDecodeError:
            warnings.append(
                f"{dest} is not valid JSON/JSONC — left untouched (fix or remove it, then rerun)"
            )
            result["status"] = "invalid_json"
            return result
        if not isinstance(current, dict):
            warnings.append(
                f"{dest} root is not a JSON object — left untouched (replace it with an object, then rerun)"
            )
            result["status"] = "invalid_root"
            return result

    merged, added, enforced = merge_policy(current, incoming, warnings.append)
    result["keys_added"] = added
    result["permissions_enforced"] = enforced
    if merged == current and enforced == 0:
        result["status"] = "unchanged"
        return result
    # Trailing newline to match what install.py writes for the global config: the two
    # produce the same bytes for the same content, so moving this writer does not show up
    # as drift.
    atomic_write_text(dest, json.dumps(merged, indent=JSON_INDENT) + "\n")
    result["status"] = "created" if not current else "merged"
    return result



def _merge_provider_config(project_root: Path, tool_dir: str) -> list[str]:
    """Backfill adapter keys the running build knows about into an existing
    .workflow/second_agent.json. Additive only — an existing value is the user's tuning.

    Without this, keys introduced by a later build (idle_stall_seconds, probe cadence)
    never reach a project that was initialized once and left alone.
    """
    from config.settings import default_provider_config

    dest = project_root / WORKFLOW_DIRNAME / PROVIDER_CONFIG_NAME
    if not dest.exists():
        copied = _copy_provider_config(project_root, tool_dir)
        return ["(created)"] if copied else []
    try:
        current = read_json_file(dest)
    except (OSError, ValueError):
        return []  # malformed user config: report nothing, never clobber it
    # Backfill against the defaults of the provider THIS file selects. Against the global
    # ones, a codex workspace missing `provider_command` had opencode's binary written
    # into it permanently — the backfill turning an absent key into a wrong value.
    defaults = default_provider_config(current.get("provider"))
    added = [key for key in defaults if key not in current]
    if not added:
        return []
    current.update({key: defaults[key] for key in added})
    atomic_write_json(dest, current)
    return added


# Moved to utils/osutil.py: the check is provider-independent, and core/audit/diagnostics.py
# should not import one provider's module to test another provider's binary. Re-exported
# so existing importers keep working.
from utils.osutil import provider_callable  # noqa: E402,F401


