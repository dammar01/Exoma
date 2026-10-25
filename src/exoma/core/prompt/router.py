from exoma.config.roles import VALID_ROLES
from exoma.core.policy.governance import check_provider, tools_for
from exoma.config.routing import COMMAND_ROUTES
from exoma.config.settings import (
    DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    default_provider_config,
)


class Router:
    """Role is code-authoritative (config/routing.py); opencode.json only tunes model.

    So opencode.json needs no `role` key, and local commands (e.g. execute) are
    absent from COMMAND_ROUTES and correctly reject delegation.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or default_provider_config()

    def _effort_for(self, model: str | None) -> str | None:
        """The configured effort, dropped when the model on this route takes none.

        Global only, on purpose: a per-route effort is expressible in the same shape as
        the per-route model, but nothing has shown that explore wants a cheaper setting
        than analyze badly enough to pay for a second place where effort can be wrong.

        The drop is the point. `provider_select` refuses this combination at write time,
        but a hand-edited second_agent.json never passes through it, and the symptom
        would be a delegated call dying on an argument the model does not take — minutes
        in, with the flag nowhere in the error. A model whose shortlist entry declares an
        empty effort set is saying it takes none; an unlisted model declares nothing and
        keeps whatever the user pinned.
        """
        effort = self.config.get("effort")
        if not effort:
            return None
        from exoma.config.providers import model_efforts, model_is_listed

        provider = str(self.config.get("provider") or "")
        try:
            if model_is_listed(provider, model) and not model_efforts(provider, model):
                return None
        except ValueError:  # unregistered provider: nothing here can say it is wrong
            return effort
        return effort

    def route(self, command: str, model_override: str | None = None) -> dict:
        normalized = command.strip().lower()
        base = COMMAND_ROUTES.get(normalized)
        if not base:
            raise ValueError(f"unsupported command: {command}")
        role = base.get("role")
        if role not in VALID_ROLES:
            raise ValueError(f"unsupported role for command {command}: {role}")
        # Refused here rather than at spawn: this is the last point where the answer is
        # still "no call was made". Past it a disallowed provider has already been given
        # the prompt, and refusing afterwards governs nothing.
        denial = check_provider(self.config.get("provider"), self.config)
        if denial:
            raise ValueError(denial)
        cfg_route = self.config.get("routes", {}).get(normalized, {})
        model = model_override or cfg_route.get("model") or base.get("model") or self.config.get("default_model")
        effort = self._effort_for(model)
        # A per-route timeout wins over the global one, but only when it is set —
        # `null` in opencode.json means "inherit", never "no limit".
        timeout = cfg_route.get("timeout_seconds")
        if timeout is None:
            timeout = self.config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        return {
            "command": normalized,
            "role": role,
            "model": model,
            # The selected provider by NAME, distinct from `provider_command` above, which
            # is the binary and may be renamed or shimmed. Prompt sizing keys off the name
            # because the transport — argv or stdin — is a property of the provider, not of
            # whatever the binary happens to be called on this machine.
            "provider": self.config.get("provider"),
            # No literal provider name as a fallback: `provider_command` is always present
            # (default_provider_config fills it for the SELECTED provider), and a hardcoded
            # one here would mean a config that somehow lost the key silently runs a
            # different provider's binary.
            "provider_command": self.config.get("provider_command"),
            # `agent` may legitimately be None — codex has no persona to select — so the
            # `or` chain must not fall through to opencode's `plan` for every provider.
            "provider_agent": cfg_route.get("agent") or self.config.get("provider_agent"),
            "effort": effort,
            # Declared, not enforced here. The hard boundary is the provider's own
            # permission config; this says what THIS command legitimately needs, so the
            # prompt can state it and the audit trail can record it.
            "declared_tools": tools_for(normalized, self.config),
            "timeout_seconds": timeout,
            "bootstrap_timeout_seconds": self.config.get(
                "bootstrap_timeout_seconds", DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS
            ),
            "poll_interval_seconds": self.config.get(
                "job_poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS
            ),
        }
