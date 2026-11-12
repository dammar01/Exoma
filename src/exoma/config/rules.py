"""The contract's rules, by id, with how each host enforces them.

Every rule in the shared contract (hosts/claude/CLAUDE.md) carries a `<!-- rule:<id> -->`
marker; this registry holds one entry per id. The rule text stays prose in the contract;
what lives here is the fact the prose cannot keep honest across hosts: whether something
blocks it on a given host.

Levels:
- enforced: a hook or the runtime can refuse the covered call on this host;
- detected: recorded or reported after the fact (execute-end, the end of a turn), not blocked;
- advised: nothing checks it on this host; it is the agent's responsibility.

Each rule names what makes its level true, so a test can hold the registry to the code:
- `hook_symbol`: the workflow_policy function every host's hook files (HOOK_FILES) call
  where the rule is enforced or detected on that host;
- `runtime`: (module, attribute) — host-independent, the runtime itself refuses or records.
A level above `advised` must be backed by one of the two (tests/checks/host_profiles.py).
Levels describe capability, not session observation. Hooks also require registration,
trust, an applicable mode and trigger, and delivery on the tool path. A configured warn
mode reports rather than blocks; absent observations do not establish trust or activity.
"""

from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("enforced", "detected", "advised")

# The file in each host's hook set that answers PreToolUse / UserPromptSubmit / Stop.
HOOK_FILES = {
    "claude": ("hosts/claude/hooks/intent-gate-check.py", "hosts/claude/hooks/intent-gate-set.py",
               "hosts/claude/hooks/execution-gate.py", "hosts/claude/hooks/status-guard.py"),
    "agy": ("hosts/claude/hooks/agy-host.py",),
    "codex": ("hosts/codex/codex-host.py",),
    "opencode": ("hosts/opencode/opencode-host.py",),
}

_ALL = ("claude", "agy", "codex", "opencode")


@dataclass(frozen=True)
class Rule:
    id: str
    summary: str
    levels: dict
    hook_symbol: str | None = None  # the workflow_policy function each enforcing adapter calls
    runtime: tuple | None = None  # (module, attribute) for a host-independent check


def _every(level: str) -> dict:
    return {host: level for host in _ALL}


RULES: tuple[Rule, ...] = (
    Rule("output.contract", "skill output format, required fields always present", _every("advised")),
    Rule("intent.route", "map a message to a command (auto-intent) or require the /. prefix", _every("advised")),
    Rule("delegate.preflight", "an explicit delegated command runs the runner before any gathering",
         _every("enforced"), hook_symbol="preflight_refusal"),
    Rule("delegate.precision_read", "at most 2 reads of at most 200 lines while a delegation is armed",
         _every("enforced"), hook_symbol="preflight_refusal"),
    Rule("session.bind", "every stateful runner call passes --session", _every("enforced"),
         runtime=("exoma.core.runtime.scripts", "_build_run_scripts")),
    Rule("division.labor", "bulk gathering goes to second_agent; quality work stays with main_agent",
         _every("advised")),
    Rule("delegate.call", "one plain runner call, named options, single quotes", _every("enforced"),
         hook_symbol="preflight_refusal"),
    Rule("delegate.digest", "read the digest first; judge capped confidence", _every("advised")),
    # The runtime derives and records the verdict; it refuses no call, so `detected`.
    Rule("verify.verdict", "pass only with zero failures and zero unchecked files", _every("detected"),
         runtime=("exoma.core.evidence.contract", "validate_verification_contract")),
    Rule("proxy.failure", "stop and ask on a failed proxy; no automatic local fallback", _every("advised")),
    Rule("next.suggest", "at most 3 relevant next steps", _every("advised")),
    Rule("plan.output", "confidence, attribution, open questions apart from uncertainties, [OPTIONS]",
         _every("advised")),
    Rule("plan.record", "a proceed plan is recorded with plan-record; the runtime refuses an invalid one",
         _every("enforced"), runtime=("exoma.core.runtime.execution", "record_plan")),
    Rule("execute.approval", "only the user's /.execute -y opens an execution", _every("enforced"),
         hook_symbol="record_approvals", runtime=("exoma.core.runtime.execution", "begin_execution")),
    Rule("execute.scope", "Edit/Write only inside the plan's files and the project root", _every("enforced"),
         hook_symbol="write_refusal"),
    Rule("execute.shell_writes", "shell writes are not blocked; execute-end names those outside scope",
         _every("detected"), runtime=("exoma.core.runtime.execution", "end_execution")),
    Rule("commit.gate", "git commit only in a turn where the user asked for it", _every("enforced"),
         hook_symbol="commit_refusal"),
    Rule("completion.claim", "no completion claim while verification is not_run",
         {"claude": "detected", "agy": "detected", "codex": "detected", "opencode": "advised"},
         hook_symbol="claims_completion"),
    Rule("session.cache", "results reused only within one session and project root", _every("advised")),
    Rule("graphify", "read graphify-out first; never run init/build/watch", _every("advised")),
    Rule("global.forbidden", "the forbidden list", _every("advised")),
)

BY_ID = {rule.id: rule for rule in RULES}


def level_of(rule_id: str, host: str) -> str:
    """The rule's level on `host`; an id the registry does not know reads `advised`, and the
    parity test fails on it."""
    rule = BY_ID.get(rule_id)
    return rule.levels.get(host, "advised") if rule else "advised"
