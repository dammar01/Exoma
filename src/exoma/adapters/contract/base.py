"""The contract a second_agent adapter answers to.

Written as a Protocol rather than a base class on purpose. The adapter surface was
already substitutable before it was ever written down — `tools/sim/sim_flows.py` swaps a
plain object in and the executor never notices — and turning it into an ABC now would
break exactly those stand-ins for no gain. A Protocol records the shape without
demanding inheritance, so a test double may implement `run()` alone while a real
provider implements everything.

Two halves:
  * attributes  — tuning the executor pushes in before each call (command, agent,
                  timeouts, progress callback). The executor sets these by name, so a
                  provider that lacks one is a provider the executor cannot drive.
  * methods     — `run()` is the whole job and `probe()` is the liveness check used
                  when a job stalls. Those two are all the runtime ever calls.

Deliberately NOT in the contract: `init_session` and `run_agent`. OpenCode splits a
call into bootstrap-then-prompt, so its adapter has both — but that split is one
provider's shape, not a requirement. Demanding them here would force every future
provider to expose OpenCode's internal structure whether or not its CLI works that way.
An adapter may define them; nothing outside the adapter calls them.

Output parsing lives here too (`extract_session_id`, `clean_output`). It used to sit in
utils/parser.py as OpenCode-shaped regexes that every provider would have had to match;
as adapter methods, a provider whose session ids or log format differ just answers
differently.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecondAgentAdapter(Protocol):
    """Structural type — `isinstance` works, inheritance is not required."""

    #: Registry key. Also what config.json's `second_agent` is matched against.
    adapter: str
    #: Executable name or path the provider is invoked through.
    command: str
    #: Provider-side agent/persona the delegated call runs as; None if not applicable.
    agent: str | None
    #: Whole-call budget in seconds. 0 or None means unbounded (see `no_timeout`).
    timeout_seconds: int | None
    no_timeout: bool
    #: Seconds between liveness ticks while the provider blocks.
    poll_interval: float
    #: Separate, shorter budget for session bootstrap.
    bootstrap_timeout_seconds: int | None
    #: Called with (phase, elapsed, idle) so a blocked call still reports progress.
    on_progress: object | None
    #: Called with a freshly captured provider session id, for persistence.
    on_session_created: object | None
    #: Diagnostics from the most recent call: argv, duration, exit code, stderr tail.
    last_call_meta: dict

    def run(
        self,
        prompt: str,
        session: dict,
        model: str | None = None,
        work_dir: str | None = None,
    ) -> dict:
        """Execute one delegated call. Returns {ok, content, meta[, digest]}.

        `session` carries `provider_session_id` when a session already exists; the
        adapter bootstraps one when it does not, and reports the new id through
        `on_session_created` so a crash mid-call still leaves a resumable session.
        """
        ...

    def probe(
        self,
        session_id: str | None = None,
        model: str | None = None,
        work_dir: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        """Cheap liveness check used when a job looks stalled."""
        ...

    @staticmethod
    def extract_session_id(text: str) -> str | None:
        """Pull the provider's session id out of its own output, or None."""
        ...

    @staticmethod
    def clean_output(text: str) -> str:
        """Strip the provider's log noise, leaving the answer."""
        ...
