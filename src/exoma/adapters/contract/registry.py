"""Name -> adapter resolution for second_agent.

Before v3.4.3 there was no selection step at all: `core/executor.py` named
`OpenCodeAdapter` in its own constructor, and config.json's `second_agent` key was
written but never read. This module is the missing half — the key is now the input to
a lookup, so adding a provider means registering a class here rather than editing the
executor.

Selection order, first hit wins:
  1. explicit `name` argument (tests, CLI overrides)
  2. `provider` in the workspace .workflow/second_agent.json
  3. EXOMA_PROVIDER (legacy AI_PROXY_PROVIDER) / the built-in default

`runtime.second_agent` in .workflow/config.json is a HINT, not a choice. v3.4.3 let it
select, and the result was a project that named codex there running every call against
`provider_command: "opencode"` — the two keys live in different files and nothing made
them agree, so the adapter and the binary it was pointed at came from different answers.
One file now owns the decision: second_agent.json carries `provider` AND the command that
matches it, and a config.json naming something else is reported through
`provider_hint_ignored` rather than acted on.
"""

from pathlib import Path

from exoma.adapters.providers.agy_adapter import AgyAdapter
from exoma.adapters.providers.codex_adapter import CodexAdapter
from exoma.adapters.providers.opencode_adapter import OpenCodeAdapter
from exoma.config.settings import DEFAULT_PROVIDER

_ADAPTERS: dict[str, type] = {
    OpenCodeAdapter.adapter: OpenCodeAdapter,
    CodexAdapter.adapter: CodexAdapter,
    # agy enforces no read-only boundary — see the note in config/providers.py and the
    # module docstring of adapters/agy_adapter.py before selecting it for a project.
    AgyAdapter.adapter: AgyAdapter,
}


class UnknownProviderError(ValueError):
    """Raised for a configured provider with no registered adapter.

    Deliberately loud rather than falling back to the default: a typo in
    `second_agent` that silently ran OpenCode would look like the config took effect.
    """


def register(adapter_cls: type) -> type:
    """Register an adapter class under its own `adapter` name. Usable as a decorator."""
    name = getattr(adapter_cls, "adapter", None)
    if not name:
        raise ValueError(f"{adapter_cls!r} has no `adapter` name to register under")
    _ADAPTERS[name] = adapter_cls
    return adapter_cls


def available_providers() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def provider_for(project_root=None, config: dict | None = None) -> str:
    """Resolve which provider a project uses, without building it.

    Only `config` decides. `project_root` is still accepted because every caller has one
    and `provider_hint(project_root)` is the paired call, but reading config.json here is
    exactly what made the adapter and `provider_command` disagree.
    """
    if isinstance(config, dict):
        configured = config.get("provider")
        if configured:
            return str(configured)
    return DEFAULT_PROVIDER


def selected_provider(project_root=None) -> str:
    """The provider a project actually runs — the one answer every caller must use.

    The rule is small and the reason it lives in one function is not: doctor, probe, the
    MCP scan and the executor each used to resolve this their own way, so a project could
    be diagnosed as opencode, scanned as codex, and executed as whichever the last file
    read happened to name. Only a PROJECT-local second_agent.json that names a `provider`
    counts; a machine-wide tool default carries the same key and must not outrank a
    workspace.
    """
    from exoma.config.settings import resolve_provider_config_for

    try:
        resolved = resolve_provider_config_for(project_root)
    except Exception:
        return DEFAULT_PROVIDER
    chose = resolved.get("provider_explicit") and str(
        resolved.get("source", "")
    ).startswith("project")
    name = resolved["config"].get("provider") if chose else None
    return str(name or DEFAULT_PROVIDER)


def provider_hint(project_root=None) -> str | None:
    """`runtime.second_agent` from .workflow/config.json, or None.

    Read to REPORT, never to select. A project that still names a provider here has a
    config the runtime is knowingly not honouring, and silence about it would look like
    the key took effect — the same inertness v3.4.3 set out to fix, just quieter.
    """
    if project_root is None:
        return None
    from exoma.core.workspace.workspace_paths import WORKFLOW_DIRNAME, read_json_file

    workspace = Path(project_root) / WORKFLOW_DIRNAME / "config.json"
    try:
        runtime = read_json_file(workspace).get("runtime") or {}
    except (OSError, ValueError):
        return None
    configured = runtime.get("second_agent")
    return str(configured) if configured else None


def hint_conflict(
    project_root=None, config: dict | None = None, selected: str | None = None
) -> dict | None:
    """The config.json hint when it names a DIFFERENT provider than the one selected.

    `selected` is what the caller actually resolved. Pass it — re-deriving the choice here
    would report against a second answer to a question the caller has already answered,
    and the two can differ (a tool-default config still carries a `provider` key).

    Returns {'provider_hint_ignored', 'provider_selected'} or None when the two agree.
    """
    hint = provider_hint(project_root)
    chosen = selected or provider_for(project_root, config)
    if not hint or hint == chosen:
        return None
    return {"provider_hint_ignored": hint, "provider_selected": chosen}


def adapter_class(name: str) -> type:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise UnknownProviderError(
            f"unknown second_agent provider '{name}'; registered: "
            f"{', '.join(available_providers())}"
        ) from None


def resolve_adapter(
    name: str | None = None,
    project_root=None,
    config: dict | None = None,
    **kwargs,
):
    """Build the adapter this project's config selects."""
    resolved = name or provider_for(project_root, config)
    return adapter_class(resolved)(**kwargs)
