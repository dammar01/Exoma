"""agy-specific workspace installation.

The three functions `config/providers.py` dispatches through — `load_config`,
`merge_policy`, `install_project_config` — are the provider-facing contract. This module
exists to answer them, and its answer is that agy has no boundary to install.

That is a finding, not a stub. Codex's equivalent reports `not_enforceable` because its
permission map parses and then fails to stop a shell read. agy's case is one step worse:
there is nothing to install anywhere, and nothing that would hold if there were.

What was probed against the shipped binary, in `--output-format stream-json` where the
`init` event states the mode and the tool list outright:

* `--sandbox`, whose help text promises "terminal restrictions", changes neither. 56 tools
  before, 56 after, `permission_mode: always-proceed` in both.
* `--mode plan`, which reads like the counterpart to opencode's read-only `plan` persona,
  changes neither either. `write_to_file`, `replace_file_content`, `multi_replace_file_content`,
  `sed_file`, `notebook_edit`, `delete_knowledge` and `run_command` are all live under it.
* Dropping `--dangerously-skip-permissions` DOES change the mode, to `request-review` — and
  that mode refuses every tool. A write test left no file behind, which sounds like the
  answer until the same run refuses a plain READ of a source file and returns an empty
  response. A second_agent that cannot read is not a second_agent.
* There is no config this installer can write a second-agent policy into. Releases up to
  1.1.13 kept only `bin/` under the home; 1.4.7 reads ~/.gemini (permissions, hooks), but
  that config is global — it binds the user's own agy sessions as much as the second
  agent's — and the Exoma host hooks there let a process the adapter marked with
  WORKFLOW_AGENT_ROLE=second_agent through.

So `permissions_enforced` is 0 and `permissions_declared` is 0 as well — unlike codex,
there is not even a flag being sent hopefully. What stands in for a boundary is
`core/agy_guard.py`, which diffs the working tree around every call and names what
changed. That detects a write after it has happened. It does not prevent one, it cannot
see through `.gitignore`, and outside a git repository it reports itself blind.

Point an agy second_agent at a repository only if a process with full read AND write
access to it is acceptable. Where that is not acceptable, opencode is the provider whose
read boundary is actually enforced.
"""

from pathlib import Path

from exoma.core.workspace.workspace_paths import read_json_file


def load_config(path: Path):
    """Read a JSON config file this provider manages.

    Kept because the contract names it and `installer/settings.py` calls it before any
    merge. Nothing is ever routed here in practice: the bundle ships no template, so the
    caller's `.exists()` guard is never satisfied.
    """
    return read_json_file(path)


def merge_policy(current: dict, incoming: dict, warn) -> tuple[dict, int, int]:
    """Additive merge. Returns (merged, keys_added, permissions_enforced).

    `permissions_enforced` is 0 and structurally so: agy exposes no permission surface in
    any file, so there is nothing here to repair or to count. An existing value is always
    the user's — this only backfills keys that are absent.
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
    """Nothing is installed and no boundary holds — reported as `not_enforceable`.

    OpenCode's `<project_root>/opencode.json` denies reading `.env` and friends, and that
    denial is what makes its second agent safe to point at a repository holding secrets.
    agy cannot be given the same file: it reads no project-root config layer, and it reads
    no global one either.

    `path` stays None because nothing was written. Claiming a path would invite someone to
    go looking for a boundary file that does not exist and conclude they are covered.
    """
    warnings = [
        "agy enforces NO boundary, read or write: probed against the shipped binary, "
        "`--sandbox` and `--mode plan` both leave all 56 tools enabled with "
        "`permission_mode: always-proceed`, `write_to_file` and `run_command` among them. "
        "Treat an agy second_agent as able to read AND modify every file in the project it "
        "is pointed at, `.env` included",
        "removing `--dangerously-skip-permissions` is not the fix: that yields "
        "`request-review`, which refuses every tool including reads, leaving a provider "
        "that cannot gather evidence at all. The choice is all tools or none",
        "agy's own config (~/.gemini: settings permissions, config/hooks.json) is global: a "
        "deny written there for the second agent would bind the user's own agy sessions too, "
        "and the Exoma host hooks installed there let the second agent's process "
        "(WORKFLOW_AGENT_ROLE=second_agent) through untouched. Neither bounds the second agent",
        "`core/agy_guard.py` diffs the working tree around each call and reports what "
        "changed. That is DETECTION: the write has already happened when it is seen, it "
        "cannot see files matched by .gitignore, and outside a git repository it reports "
        "itself unavailable rather than clean",
        "opencode is the provider whose read boundary is enforced; switch to it for a "
        "project whose secrets must stay unreadable by the second agent",
    ]
    warnings.extend(_workspace_config_warnings(project_root))
    return {
        "path": None,
        "status": "not_enforceable",
        "keys_added": 0,
        "permissions_enforced": 0,
        # Zero DECLARED as well, which is where agy differs from codex: codex still sends
        # permission flags that would start working the day it gates shell reads. There is
        # no such flag here to send.
        "permissions_declared": 0,
        "warnings": warnings,
    }


def _workspace_config_warnings(project_root: Path) -> list[str]:
    """Settings this workspace carries that agy will not act on.

    Init is the moment a user is actually looking, so it is the moment worth saying it. A
    workspace switched over from another provider keeps every key it had — the backfill
    only adds what is missing — so a `provider_agent` and a foreign-namespaced model sit
    there reading like configuration while agy ignores both.

    Nothing is rewritten. A value the user set stays theirs; what changes is that they are
    told it is inert instead of discovering it by tuning a knob attached to nothing.
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
            "agy ignores these keys in second_agent.json: "
            + ", ".join(inert_set)
            + " (`agy agents` lists no persona to select, there is no bootstrap call to "
            "time, and the stream is read as it arrives rather than polled)"
        )
    notes.extend(
        f"{key} looks stale: {why}"
        for key, why in foreign_provider_values(config).items()
    )
    return notes


def inert_config_keys() -> tuple[str, ...]:
    """Keys `second_agent.json` accepts that mean nothing under agy.

    They are not errors and they are not removed — a workspace switched back to opencode
    needs them intact. They are reported so a user who tunes one and sees no effect learns
    why from doctor instead of from a long afternoon.

    * `bootstrap_timeout_seconds` — there is no bootstrap call to time out. The
      conversation id arrives in the first line of the run itself.
    * `job_poll_interval_seconds` / `job_poll_timeout_seconds` — nothing is polled; the
      adapter drains the stream as it arrives.
    """
    return (
        "bootstrap_timeout_seconds",
        "job_poll_interval_seconds",
        "job_poll_timeout_seconds",
    )
