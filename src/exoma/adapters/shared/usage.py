"""Provider-reported token counts, reduced to one shape.

Every provider names these differently and nests them differently, and the runtime has
exactly one thing to do with them: write four integers onto a usage row. That mapping is
here rather than in each adapter so the adapters stay parsers of their own stream format
and nothing else, and so the one rule that matters is stated once.

THE RULE: `reasoning` is part of `output`, and `cached_input` is part of `input`. Both of
the major provider APIs report them that way — a nested `*_details` breakdown OF the total
sitting next to it, never a sibling to add. Summing all four here would bill the same
token twice, which is why this module returns them separately and adds nothing.

Deliberately alias-driven rather than pinned to one schema. The exact key spelling a given
provider build emits is not something this repository can prove — the fixtures it owns show
only `output_tokens` — so the reader accepts the spellings the major APIs use, takes the
first that is present, and leaves anything it does not recognise as None. An unmapped key
produces a missing measurement, which telemetry already knows how to say. A guessed one
would produce a wrong number, which it does not.
"""

from typing import Any

# First match wins, so the most specific spelling comes first. A value nested one level
# down is expressed as a (container, key) pair.
_INPUT_KEYS = ("input_tokens", "prompt_tokens", "in_tokens")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "out_tokens")
_REASONING_KEYS = ("reasoning_tokens", "reasoning_output_tokens", "thinking_tokens")
_CACHED_KEYS = (
    "cached_input_tokens",
    "cache_read_input_tokens",
    "cached_tokens",
    "cache_read_tokens",
)
# Where a provider tucks the breakdown instead of leaving it at the top level.
_DETAIL_CONTAINERS = (
    "output_tokens_details",
    "completion_tokens_details",
    "input_tokens_details",
    "prompt_tokens_details",
)


def _as_int(value: Any) -> int | None:
    """A count, or nothing. A bool is not a count even though Python says it is an int."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if number >= 0 else None


def _first(payload: dict, keys: tuple[str, ...]) -> int | None:
    """The first of `keys` that carries a usable count, top level then details."""
    for key in keys:
        found = _as_int(payload.get(key))
        if found is not None:
            return found
    for container in _DETAIL_CONTAINERS:
        nested = payload.get(container)
        if not isinstance(nested, dict):
            continue
        for key in keys:
            found = _as_int(nested.get(key))
            if found is not None:
                return found
    return None


def normalize_usage(raw: Any) -> dict | None:
    """One provider usage object, reduced to the four counts a usage row holds.

    Returns None when nothing recognisable is present, which is the honest answer for a
    provider that reports no usage at all — and the answer the callers are built around,
    since a row with no measurement keeps its estimate rather than gaining a zero.
    """
    if not isinstance(raw, dict):
        return None
    normalized = {
        "input_tokens": _first(raw, _INPUT_KEYS),
        "output_tokens": _first(raw, _OUTPUT_KEYS),
        "reasoning_tokens": _first(raw, _REASONING_KEYS),
        "cached_input_tokens": _first(raw, _CACHED_KEYS),
    }
    if all(value is None for value in normalized.values()):
        return None
    return normalized


def merge_usage(left: dict | None, right: dict | None) -> dict | None:
    """Two usage objects from the same invocation, added together.

    For a stream that reports per turn rather than per run: one `adapter.run` can cover
    several turns, and keeping only the last one would silently drop everything before it.
    Addition is correct HERE, where both sides are the same field of different turns, and
    wrong ACROSS fields, where reasoning already sits inside output.

    A count present on one side and absent on the other is carried through rather than
    treated as zero: absent means unreported, and inventing a zero to add to it would turn
    a partial measurement into a confident wrong one.
    """
    if left is None:
        return right
    if right is None:
        return left
    merged: dict = {}
    for key in ("input_tokens", "output_tokens", "reasoning_tokens", "cached_input_tokens"):
        values = [side.get(key) for side in (left, right) if side.get(key) is not None]
        merged[key] = sum(values) if values else None
    return merged


def token_source_for(usage: dict | None) -> str:
    """What a usage row can honestly claim about where its numbers came from.

    Three states, because two would force a lie. A provider reporting only its output
    count — the one shape this repository has an actual fixture for — leaves the input
    side estimated, and calling that row either "provider" or "estimated" misdescribes
    half of it.
    """
    if not usage:
        return "estimated"
    measured_input = usage.get("input_tokens") is not None
    measured_output = usage.get("output_tokens") is not None
    if measured_input and measured_output:
        return "provider"
    if measured_input or measured_output:
        return "mixed"
    return "estimated"
