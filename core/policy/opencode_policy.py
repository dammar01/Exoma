"""Shared OpenCode config merge policy.

Both writers of an opencode config go through here: `install.py` (global
~/.config/opencode) and `core.runtime.upgrade._install_project_boundary` (the project boundary at
<project_root>/opencode.json). The rules are a security boundary, so they live in one
module rather than being reimplemented on each side — a second copy is a second thing to
forget when the deny-list changes.

Reporting is a `warn(message)` callable instead of install.py's Plan object: the runtime
side has no Plan, and the merge itself has no business knowing how its caller renders.
"""

import json
import re
from pathlib import Path
from typing import Callable

Warn = Callable[[str], None]


def _noop(_message: str) -> None:
    return None


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments from JSONC, string-aware so a `//` inside a URL
    (e.g. "https://...") or a `/*` inside a value is preserved. Trailing commas dropped.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = esc = False
    while i < n:
        char = text[i]
        if in_str:
            out.append(char)
            if esc:
                esc = False
            elif char == "\\":
                esc = True
            elif char == '"':
                in_str = False
            i += 1
            continue
        if char == '"':
            in_str = True
            out.append(char)
            i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if char == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(char)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_json_or_jsonc(path: Path) -> dict:
    """Parse a config that may carry comments (opencode.jsonc) or be plain JSON.

    utf-8-sig, not utf-8: editors and PowerShell on Windows write a BOM, and a leading
    BOM makes json.loads fail on a file that is otherwise perfectly valid — refusing to
    enforce the boundary over a byte the user never typed.
    """
    text = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(strip_jsonc(text))


def deep_merge_additive(
    base: dict, incoming: dict, path: str, warn: Warn = _noop
) -> tuple[dict, int]:
    """Recursively add keys from `incoming` absent in `base`; report scalar conflicts.

    Same posture as _install_settings: a value the user already set is their decision,
    never silently overwritten — reported instead. Nested dicts recurse so unrelated
    sibling keys (the user's MCP servers, providers) are preserved untouched.
    """
    added = 0
    for key, value in incoming.items():
        sub_path = f"{path}.{key}"
        if key not in base:
            base[key] = value
            added += 1
        elif isinstance(base[key], dict) and isinstance(value, dict):
            base[key], sub = deep_merge_additive(base[key], value, sub_path, warn)
            added += sub
        elif base[key] != value:
            warn(
                f"opencode.json[{sub_path}] differs from the workflow default — kept "
                f"yours ({base[key]!r}); workflow wants {value!r} (read-only enforcement)"
            )
    return base, added


def enforce_permissions(
    holder: dict, shipped: dict, label: str, warn: Warn = _noop
) -> int:
    """Overwrite a permission block key-wise. Returns how many keys were enforced.

    Additive merging is right for the rest of the config — a value the user set is their
    decision. Permissions are the exception: they are the boundary, and a boundary that
    yields to whatever was already there is not one.
    """
    permissions = holder.get("permission")
    if not isinstance(permissions, dict):
        permissions = {}
        holder["permission"] = permissions
    enforced = 0
    for key, value in shipped.items():
        current_value = permissions.get(key)
        same_value = current_value == value
        if isinstance(current_value, dict) and isinstance(value, dict):
            # Rule ORDER decides which pattern wins, so equal-but-reordered is not equal.
            same_value = list(current_value.items()) == list(value.items())
        if same_value:
            continue
        if key in permissions:
            warn(f"opencode {label} permission {key!r} replaced by the workflow's policy")
        permissions[key] = json.loads(json.dumps(value))
        enforced += 1
    return enforced


def merge_opencode_policy(
    current: dict, incoming: dict, warn: Warn = _noop
) -> tuple[dict, int, int]:
    """Merge general config additively, but enforce workflow-owned permissions.

    Two blocks are enforced rather than merged: `agent.plan.permission` (the read-only
    primary) and the ROOT `permission` (which every agent inherits, subagents included —
    a secret-file rule that stopped at the primary would leave the fan-out path open).
    """
    incoming_copy = json.loads(json.dumps(incoming))
    incoming_plan = (incoming_copy.get("agent") or {}).get("plan")
    shipped_permissions = None
    if isinstance(incoming_plan, dict):
        shipped_permissions = incoming_plan.pop("permission", None)
    shipped_root = incoming_copy.pop("permission", None)

    merged = json.loads(json.dumps(current))
    merged, added = deep_merge_additive(merged, incoming_copy, "opencode", warn)
    enforced = 0
    if isinstance(shipped_root, dict):
        enforced += enforce_permissions(merged, shipped_root, "root", warn)
    if isinstance(shipped_permissions, dict):
        agent = merged.get("agent")
        if not isinstance(agent, dict):
            warn("opencode agent config was not an object; replaced for workflow policy")
            agent = {}
            merged["agent"] = agent
        plan_agent = agent.get("plan")
        if not isinstance(plan_agent, dict):
            warn("opencode plan agent config was not an object; replaced")
            plan_agent = {}
            agent["plan"] = plan_agent
        enforced += enforce_permissions(plan_agent, shipped_permissions, "plan", warn)
    return merged, added, enforced
