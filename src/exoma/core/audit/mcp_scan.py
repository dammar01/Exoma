"""Classify the MCP servers a second_agent would be exposed to.

second_agent is a read-only evidence gatherer, so a write- or exec-capable MCP server
in its config is a capability it should not have. This module only reports; acting on
the verdict is doctor's job.
"""

import json
import os
import re
import shutil
from pathlib import Path

from exoma.core.workspace.workspace_paths import PROVIDER_CONFIG_NAME, WORKFLOW_DIRNAME


_MCP_SAFE = ("context7", "docs", "documentation", "read-only", "readonly", "search")
_MCP_RISK = (
    "shell",
    "exec",
    "bash",
    "run-command",
    "runcommand",
    "filesystem",
    "file-system",
    "write",
    "postgres",
    "mysql",
    "mongo",
    "sqlite",
    "git",
    "playwright",
    "puppeteer",
    "browser",
    "selenium",
    "kubernetes",
    "docker",
    "ssh",
)
# DB/data-inspection families that ARE permitted for second_agent (read-only evidence
# extended to DB). Matched by NAME so a scoped inspector like laravel-boost is not caught
# by the generic mysql/postgres RISK keywords. Permission is behavioral: the AGENTS.md
# contract restricts second_agent to read queries + forbids the write tools below — same
# way file-write is forbidden by prompt, not sandbox.
_MCP_INSPECT = ("laravel-boost", "laravel_boost", "laravelboost")
# Write/exec tools that force RISK even inside an inspect family: they break read-only.
_MCP_WRITE_TOOLS = ("tinker", "migrate", "seed", "db:wipe", "eval")


def _mcp_config_candidates(project_root: Path, provider: str | None = None) -> list[Path]:
    """Config files that could declare MCP servers, project-local first.

    The provider half comes from that provider's bundle rather than a literal path, so a
    project running a second_agent other than opencode is scanned instead of silently
    reported clean.
    """
    from exoma.adapters.contract.registry import selected_provider
    from exoma.config.providers import PROVIDER_BUNDLES, provider_config_candidates

    try:
        home = Path.home()
    except RuntimeError:
        home = Path(
            os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(project_root)
        )
    # Same resolution the executor uses, or the scan reads one provider's config files
    # while another provider is the one actually spawned.
    resolved = provider or selected_provider(project_root)
    candidates = [project_root / WORKFLOW_DIRNAME / PROVIDER_CONFIG_NAME]
    # An unbundled provider is a config shape nothing here knows how to locate. Scanning
    # nothing is honest; guessing opencode's paths would report another provider clean.
    if resolved in PROVIDER_BUNDLES:
        candidates.extend(provider_config_candidates(resolved, home))
    return candidates


def _classify_mcp(name: str, spec) -> tuple[str, bool, str]:
    """Classify one MCP server for second_agent (read-only evidence) safety.

    Tiers: risk (write/exec — disable), inspect (read-only DB/data — PERMITTED), safe
    (docs/search), unknown (review). Order matters: a write TOOL forces risk even for an
    inspect family, and the inspect family is checked BEFORE the generic mysql/postgres
    RISK keywords so a scoped inspector is not mislabelled by a keyword its command names.
    """
    enabled = (
        bool(spec["enabled"]) if isinstance(spec, dict) and "enabled" in spec else True
    )
    payload = json.dumps(spec) if isinstance(spec, (dict, list)) else str(spec)
    blob = f"{name} {payload}".lower()
    # (1) A write/exec tool named in the spec breaks read-only regardless of family.
    wtool = next((k for k in _MCP_WRITE_TOOLS if k in blob), None)
    if wtool:
        return (
            "risk",
            enabled,
            f"exposes write/exec tool '{wtool}' — exceeds read-only role (disable it or the server)",
        )
    # (2) Named DB/data-inspection family — permitted, behavioral read-only contract.
    if any(k in name.lower() for k in _MCP_INSPECT):
        return (
            "inspect",
            enabled,
            "read-only DB/data inspection (laravel-boost family) — PERMITTED for second_agent; "
            "AGENTS.md contract limits it to read queries + forbids tinker/migrate/seed",
        )
    # (3) Generic write/exec/raw-DB keyword — still risk (an unscoped SQL MCP can write).
    risk = next((k for k in _MCP_RISK if k in blob), None)
    if risk:
        return (
            "risk",
            enabled,
            f"matches write/exec keyword '{risk}' — exceeds read-only second_agent role",
        )
    if any(k in blob for k in _MCP_SAFE):
        return "safe", enabled, "read-only (docs/search)"
    return "unknown", enabled, "capability unknown — review manually"


def _mcp_reachable(spec) -> tuple[bool | None, str]:
    """Light liveness for one MCP server: is its launch command resolvable on PATH?

    A real signal short of invoking the server (which spends quota and can hang): a local
    server whose command is missing can never answer, so doctor should say so. Remote
    servers and command-less specs are reported unprobed, never faked as reachable — an
    unrun check is not a pass.
    """
    if not isinstance(spec, dict):
        return None, "spec not an object — cannot resolve command"
    if spec.get("type") == "remote" or spec.get("url"):
        return None, "remote server (liveness not probed)"
    cmd = spec.get("command")
    if isinstance(cmd, list) and cmd:
        exe = str(cmd[0])
    elif isinstance(cmd, str) and cmd.strip():
        exe = cmd.split()[0]
    else:
        return None, "no launch command in spec — cannot probe"
    resolved = shutil.which(exe) or shutil.which(f"{exe}.cmd")
    if resolved:
        return True, resolved
    return False, f"'{exe}' not found on PATH — second_agent cannot start this server"


def _scan_mcp(project_root: Path) -> dict:
    """Enumerate MCP servers the provider exposes to second_agent + a safety verdict."""
    servers: list[dict] = []
    sources: list[str] = []
    unreadable: list[dict] = []
    seen: set[str] = set()
    for path in _mcp_config_candidates(project_root):
        # exists() is inside the try: on Windows a path the process may not stat raises
        # PermissionError from it (a sandboxed host denies ~/.codex, CASE-033), and that
        # took doctor down with it. A file that cannot be read is reported, not skipped:
        # an MCP list built without it is not known to be complete.
        try:
            if not path.exists():
                continue
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            unreadable.append({"path": str(path), "error": f"{type(exc).__name__}: {exc.strerror or exc}"})
            continue
        except UnicodeDecodeError as exc:
            # Not UTF-8: as unreadable to the scan as a denied file, and reported the same way.
            unreadable.append({"path": str(path), "error": f"UnicodeDecodeError: {exc.reason}"})
            continue
        try:
            cleaned = re.sub(r"(?m)^\s*//.*$", "", raw)  # tolerate // comments (jsonc)
            data = json.loads(cleaned)
        except ValueError:
            continue
        mcp = data.get("mcp") if isinstance(data, dict) else None
        if not isinstance(mcp, dict) or not mcp:
            continue
        sources.append(str(path))
        for name, spec in mcp.items():
            if name in seen:  # first config wins (project overrides global)
                continue
            seen.add(name)
            cls, enabled, reason = _classify_mcp(name, spec)
            reachable, reach_detail = _mcp_reachable(spec)
            servers.append(
                {
                    "name": name,
                    "enabled": enabled,
                    "classification": cls,
                    "reason": reason,
                    "reachable": reachable,
                    "reachable_detail": reach_detail,
                }
            )
    active = [s for s in servers if s["enabled"]]
    if not servers:
        verdict = "none"
    elif any(s["classification"] == "risk" for s in active):
        verdict = "risk"
    elif any(s["classification"] == "unknown" for s in active):
        verdict = "review"
    else:
        verdict = "safe"
    return {"sources": sources, "servers": servers, "verdict": verdict, "unreadable": unreadable}
