"""Codex-specific workspace installation.

The three functions `config/providers.py` dispatches through — `load_config`,
`merge_policy`, `install_project_config` — are the provider-facing contract. Everything
else here is codex's own business.

Codex's answer to those three is mostly "not applicable", and that is a design choice
rather than a stub:

* Codex keeps its config in `~/.codex/config.toml`. Every value in it can be overridden
  per invocation with `-c key=value`, and the sandbox has its own `-s` flag, so
  `adapters/codex_adapter.py` asserts the read-only boundary on EVERY call instead of
  writing it into a file once. A merged config file can be edited afterwards and nothing
  would notice; an argv flag cannot. There is therefore no global config to merge, and
  the bundle ships no template for one.
* Codex has no project-root config LAYER at all, so there is no project file to install
  either. `[permissions.<profile>.filesystem]` parses and is reachable through a `-c`
  override, so the read boundary was written to follow the same route as the sandbox:
  asserted on every call.

  It does not work. Probed against codex-cli 0.147.0 in `exec` mode: denying `**` and
  `**/*` for `:workspace_roots` and then asking for a file returns that file's contents,
  exit 0. Codex reads by running a shell command, `--sandbox read-only` governs writes
  rather than reads (its own `--help` says the sandbox policy applies "when executing
  model-generated shell commands"), and nothing routes a shell read through the permission
  map. So `install_project_config` reports `not_enforceable` with a zero count. The flags
  are still sent — they cost four argv elements and would start working the day codex gates
  shell reads — but a count of patterns SENT was being read as a count of patterns
  ENFORCED, and a security report that overstates is worse than one that is absent.
"""

from pathlib import Path

from exoma.core.policy.secret_patterns import (
    CODEX_PERMISSION_PROFILE,
    codex_filesystem_permissions,
)
from exoma.core.workspace.workspace_paths import read_json_file


def load_config(path: Path):
    """Read a JSON config file this provider manages.

    Plain JSON only. Codex's own `config.toml` is deliberately outside what the installer
    manages (see the module docstring), so nothing routed here is ever TOML.
    """
    return read_json_file(path)


def merge_policy(current: dict, incoming: dict, warn) -> tuple[dict, int, int]:
    """Additive merge. Returns (merged, keys_added, permissions_enforced).

    `permissions_enforced` is always 0: codex's permissions are argv flags on every call,
    not file contents, so there is nothing in a config file for this layer to repair. An
    existing value is always the user's — this only backfills keys that are absent.
    """
    merged = dict(current or {})
    added = 0
    for key, value in (incoming or {}).items():
        if key not in merged:
            merged[key] = value
            added += 1
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            nested, nested_added, _ = merge_policy(merged[key], value, warn)
            merged[key] = nested
            added += nested_added
    return merged, added, 0


def install_project_config(project_root: Path) -> dict:
    """No file is installed, and no read boundary holds — reported as `not_enforceable`.

    OpenCode's `<project_root>/opencode.json` denies reading `.env` and friends, and that
    denial is what makes its second agent safe to point at a repository holding secrets.
    Codex cannot be given the same file: a `.codex/config.toml` sitting in a project root is
    not part of any config layer codex loads, verified by planting an unknown key in one and
    watching `--strict-config` accept the run without complaint.

    `[permissions.<profile>.filesystem]` looked like the way back to the same guarantee. It
    parses — `--strict-config` accepts the override, and dropping `default_permissions`
    produces a specific complaint about it — so this once reported `enforced_per_call` and
    counted the patterns. Probing 0.147.0 showed the count described nothing: with `**` and
    `**/*` denied for `:workspace_roots`, `codex exec` still returned the contents of a file
    in that root at exit 0. Codex reads by spawning a shell, and `--sandbox read-only` bounds
    what a shell may WRITE.

    So the two numbers below describe different things on purpose. `permissions_declared` is
    what rides on the argv; `permissions_enforced` is what any of it is known to stop, and it
    is 0 until a codex release gates shell reads. Collapsing the two is what made a workflow
    that reads `.env` freely look, in doctor output, like one that cannot.

    `path` stays None: nothing was written, and claiming a path would invite someone to go
    looking for it.
    """
    denies = codex_filesystem_permissions()
    warnings = [
        "codex does NOT enforce a read boundary: verified against codex-cli 0.147.0 in "
        "`exec` mode, denying `**` for `:workspace_roots` still returns file contents at "
        "exit 0, because codex reads via shell and `--sandbox read-only` bounds writes "
        "rather than reads. Treat a codex second_agent as able to read every file in the "
        "project, `.env` included, and point it at a repository only if that is acceptable",
        f"the `-c permissions.{CODEX_PERMISSION_PROFILE}.filesystem` flags are still sent on "
        "every call so the boundary starts working the day codex gates shell reads, but "
        "nothing today depends on them holding",
        "those flags override `default_permissions` for the duration of a workflow call, so "
        "a permission profile configured in ~/.codex/config.toml is not the one in effect "
        "while the second agent runs",
        "opencode is the provider whose read boundary is enforced; switch to it for a "
        "project whose secrets must stay unreadable by the second agent",
    ]
    warnings.extend(_workspace_config_warnings(project_root))
    return {
        "path": None,
        "status": "not_enforceable",
        "keys_added": 0,
        "permissions_enforced": 0,
        "permissions_declared": len(denies),
        "warnings": warnings,
    }


def _workspace_config_warnings(project_root: Path) -> list[str]:
    """Settings this workspace carries that codex will not act on.

    Init is the moment a user is actually looking, so it is the moment worth saying it. A
    workspace switched over from opencode keeps every key it had — the backfill only adds
    what is missing — so `provider_agent: "plan"` and an `opencode/`-namespaced model sit
    there reading like configuration while codex ignores both.

    Nothing is rewritten here. A value the user set stays theirs; what changes is that they
    are told it is inert instead of discovering it by tuning a knob attached to nothing.
    """
    from exoma.config.settings import foreign_provider_values
    from exoma.core.workspace.workspace_paths import WORKFLOW_DIRNAME

    config_path = Path(project_root) / WORKFLOW_DIRNAME / "second_agent.json"
    try:
        config = read_json_file(config_path)
    except (OSError, ValueError):
        return []
    if not isinstance(config, dict):
        return []

    notes: list[str] = []
    inert_set = [key for key in inert_config_keys() if config.get(key) is not None]
    if inert_set:
        notes.append(
            "codex ignores these keys in second_agent.json: "
            + ", ".join(inert_set)
            + " (no persona to select, no bootstrap call to time, and the JSONL stream is "
            "read as it arrives rather than polled)"
        )
    notes.extend(
        f"{key} looks stale: {why}"
        for key, why in foreign_provider_values(config).items()
    )
    return notes


def inert_config_keys() -> tuple[str, ...]:
    """Keys `second_agent.json` accepts that mean nothing under codex.

    They are not errors and they are not removed — a workspace switched back to opencode
    needs them intact. They are reported so a user who tunes one and sees no effect learns
    why from doctor instead of from a long afternoon.

    * `provider_agent` — codex `exec` runs the model directly; it selects no named persona.
    * `bootstrap_timeout_seconds` — there is no bootstrap call to time out. Codex hands over
      its thread id in the first event of the run itself.
    * `job_poll_interval_seconds` / `job_poll_timeout_seconds` — nothing is polled; the
      adapter reads a JSONL stream as it arrives.
    """
    return (
        "provider_agent",
        "bootstrap_timeout_seconds",
        "job_poll_interval_seconds",
        "job_poll_timeout_seconds",
    )
