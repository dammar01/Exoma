from exoma.config.roles import ROLE_EXPLORATION, ROLE_REASONING, ROLE_VERIFICATION

COMMAND_ROUTES = {
    "init": {"role": ROLE_VERIFICATION, "model": None},
    "doctor": {"role": ROLE_VERIFICATION, "model": None},
    "explore": {"role": ROLE_EXPLORATION, "model": None},
    "plan": {"role": ROLE_REASONING, "model": None},
    "analyze": {"role": ROLE_REASONING, "model": None},
    "verify": {"role": ROLE_VERIFICATION, "model": None},
    # /.verify-browser: the request-driven browser pipeline (core/evidence/e2e/runner.py).
    "verify-browser": {"role": ROLE_VERIFICATION, "model": None},
    # Internal: stage 1 of /.verify-browser. Read-only exploration that yields claims and
    # a browser scenario ([E2E SPEC]) — never a CLI command; see INTERNAL_COMMANDS.
    "e2e_spec": {"role": ROLE_EXPLORATION, "model": None},
    "submit": {"role": ROLE_VERIFICATION, "model": None},
    "status": {"role": ROLE_EXPLORATION, "model": None},
    "result": {"role": ROLE_EXPLORATION, "model": None},
}

# Routes that exist so the executor can run a delegated call on their behalf, but that
# no user-facing entry point may dispatch. `main.run()` refuses them; the CLI never
# lists them; provider selection never offers them a model.
INTERNAL_COMMANDS = frozenset({"e2e_spec"})
