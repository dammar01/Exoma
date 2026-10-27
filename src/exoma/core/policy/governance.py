"""Who may run a delegated call, with what tools, and how much it may spend.

Three controls that were previously either documented or enforced somewhere the runtime
could not see. Each one states plainly where its teeth actually are, because a governance
control that is weaker than it sounds is worse than an absent one — it gets trusted.

  * Provider allowlist — REAL enforcement. A route naming an unregistered provider is
    refused here, before anything spawns.
  * Budget ceiling — REAL enforcement, on recorded history. Bounded by what the usage
    stream knows: a call in flight has not been recorded yet, so a ceiling can be crossed
    by at most the calls running concurrently.
  * Per-command tool permission — DECLARATION, not enforcement. The hard boundary is the
    provider's own permission config (see _bundle/config/providers/opencode/opencode.project.json);
    this narrows it per command and writes down what was asked for. Named a declaration
    everywhere it appears so nobody reads it as a sandbox.
"""

from exoma.config.providers import bundled_providers
from exoma.core.evidence.contracts import billable_input, billable_output

# Tools each command legitimately needs. Read-only everywhere: the second agent gathers
# evidence and never writes, which is the actual invariant this expresses. Verify gets
# `bash` because checking that something works means running it.
COMMAND_TOOL_POLICY: dict[str, tuple[str, ...]] = {
    "explore": ("read", "grep", "glob", "list"),
    "analyze": ("read", "grep", "glob", "list"),
    "plan": ("read", "grep", "glob", "list"),
    "verify": ("read", "grep", "glob", "list", "bash"),
    "sweep": ("read", "grep", "glob", "list", "bash"),
}
_DEFAULT_TOOL_POLICY = ("read", "grep", "glob", "list")

# Tools no delegated command may ask for, whatever a config says. The second agent is
# read-only by design; a write tool reaching it would move the boundary that makes
# delegation safe to begin with.
FORBIDDEN_TOOLS = frozenset({"write", "edit", "patch", "apply", "delete", "rm"})


def tools_for(command: str, config: dict | None = None) -> list[str]:
    """The declared tool set for one command, forbidden entries removed.

    A project may narrow the default via `command_tools` in its provider config. It may
    not widen it past FORBIDDEN_TOOLS — a config is a preference, and this one is an
    invariant.
    """
    normalized = (command or "").strip().lower()
    configured = (config or {}).get("command_tools") or {}
    declared = configured.get(normalized)
    if not isinstance(declared, (list, tuple)) or not declared:
        declared = COMMAND_TOOL_POLICY.get(normalized, _DEFAULT_TOOL_POLICY)
    seen: list[str] = []
    for tool in declared:
        name = str(tool).strip().lower()
        if name and name not in FORBIDDEN_TOOLS and name not in seen:
            seen.append(name)
    return seen


def allowed_providers(config: dict | None = None) -> list[str]:
    """Providers this workspace may dispatch to.

    Defaults to the set the build actually ships an adapter for, which is the honest
    ceiling: a provider with no adapter cannot run whatever a config claims. A project
    may narrow it further via `provider_allowlist`, and entries outside the shipped set
    are dropped rather than honoured — an allowlist that can grant access to something
    unimplemented is not an allowlist.
    """
    shipped = list(bundled_providers())
    configured = (config or {}).get("provider_allowlist")
    if not isinstance(configured, (list, tuple)) or not configured:
        return shipped
    narrowed = [str(name).strip().lower() for name in configured]
    return [name for name in shipped if name in narrowed]


def check_provider(provider: str | None, config: dict | None = None) -> str | None:
    """None when the provider may run, otherwise why it may not.

    A missing provider name passes. It is resolved later from the bundle, and refusing it
    here would break every workspace whose config predates the key — a governance control
    that breaks working setups gets switched off, and then it governs nothing.
    """
    if not provider:
        return None
    name = str(provider).strip().lower()
    allowed = allowed_providers(config)
    if name in allowed:
        return None
    return (
        f"provider '{name}' is not in this workspace's allowlist "
        f"({', '.join(allowed) or 'empty'})"
    )


def budget_limit(config: dict | None = None) -> int | None:
    """Token ceiling per session, or None when unlimited.

    Off by default and deliberately so. A ceiling nobody chose would start refusing work
    on the first long day, and the failure mode of a wrongly-set budget (real work
    blocked) is worse than the failure mode of an absent one (spend visible in the report
    that now exists).
    """
    raw = (config or {}).get("session_token_budget")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def budget_state(rows, session_id: str, limit: int | None) -> dict:
    """Where this session stands against its ceiling.

    Counts only rows belonging to `session_id`: a budget shared across every session in a
    project would let yesterday's work block today's.
    """
    spent = 0
    for row in rows:
        if row.session_id != session_id:
            continue
        # Measured counts when the provider gave them, estimates otherwise. Worth knowing
        # when reading a ceiling that suddenly bites sooner: a provider's output count
        # includes reasoning the chars//4 estimate never saw, so the same work measures
        # larger than it used to. The spend did not grow; the blindfold came off.
        spent += billable_input(row) + billable_output(row)
    return {
        "limit": limit,
        "spent": spent,
        "remaining": None if limit is None else max(0, limit - spent),
        "exceeded": bool(limit is not None and spent >= limit),
        # The measurement is chars//4, and a ceiling enforced on an estimate must say so
        # before someone reads a refusal as a billing fact.
        "token_source": "estimated",
    }
