import os
import re
from datetime import datetime, timezone


def generate_main_session_id() -> str:
    """Generate a collision-resistant session ID for the main agent session.

    Microsecond timestamp + process id: two concurrent main agents (even started
    in the same second on the same project) never collide, because distinct
    processes have distinct pids. Mirrors the hook generator's ms+entropy intent.
    """
    now = datetime.now(timezone.utc)
    return f"main_{now.strftime('%Y%m%d_%H%M%S%f')}_{os.getpid():x}"


def ensure_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def first_non_empty(*values) -> str:
    for value in values:
        text = ensure_text(value).strip()
        if text:
            return text
    return ""


# Session-id extraction and log cleaning moved to the adapter in v3.4.3
# (adapters/opencode_adapter.py). Both were OpenCode-shaped — `ses_` tokens and
# OpenCode's own log prefixes — so keeping them here would have forced every future
# provider to emit OpenCode's output format. What stays in this module is the part
# that is genuinely provider-independent.
