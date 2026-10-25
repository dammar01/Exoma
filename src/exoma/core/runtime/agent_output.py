"""Parsing of the second agent's reply into structured fields."""

import re


def extract_lines_by_prefix(text: str, prefixes: tuple[str, ...]) -> list[str]:
    results: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("-"):
            continue
        value = line[1:].strip()
        for prefix in prefixes:
            if value.startswith(prefix):
                results.append(value[len(prefix) :].strip())
                break
    return [item for item in results if item]

_QUESTION_NUM = re.compile(r"^(\d+)\s*[.)]\s*(.+)$", re.DOTALL)

# ` | A) ... | B) ...` — enumerated answers on the same line as the question.
_QUESTION_OPT = re.compile(r"\s\|\s")

# ` label :: what it means` — the option's own explanation, split from its label.
_QUESTION_OPT_DESC = re.compile(r"\s::\s")

def parse_questions(text: str) -> dict:
    """Split the agent's questions into the two kinds that need different handling.

    `question:` blocks — the answer changes what gets built, so the user must see it.
    `uncertainty:` does not — it is closed by stating an assumption and carrying on.
    Both used to land in one `open_questions` list, which made every uncertainty look
    like it needed a decision and buried the ones that actually did.

    Numbering and ` | ` options are parsed out when present so a caller can render a real
    choice instead of a paragraph. Unnumbered lines still parse — older state files and
    any agent that ignores the format keep working, they just get positional ids.

    An option may carry its own explanation after ` :: `. A bare label tells the reader what
    to click, not what it costs them, and the renderers this feeds (Claude Code's
    AskUserQuestion among them) have a description slot that would otherwise sit empty.
    Options with no ` :: ` keep an empty description rather than changing shape, so callers
    never have to branch on which form they got.

    Returns {'open_questions': [...], 'resolvable_uncertainties': [...]} where each entry
    is {'id', 'text', 'options'} and each option is {'label', 'description'}.
    """

    def _collect(prefixes: tuple[str, ...]) -> list[dict]:
        out: list[dict] = []
        for raw in extract_lines_by_prefix(text, prefixes):
            body = raw
            number = None
            match = _QUESTION_NUM.match(body)
            if match:
                number = int(match.group(1))
                body = match.group(2).strip()
            parts = [p.strip() for p in _QUESTION_OPT.split(body) if p.strip()]
            question, raw_options = (parts[0], parts[1:]) if parts else (body, [])
            if not question:
                continue
            options = []
            for raw_option in raw_options:
                halves = _QUESTION_OPT_DESC.split(raw_option, 1)
                label = halves[0].strip()
                description = halves[1].strip() if len(halves) > 1 else ""
                options.append({"label": label, "description": description})
            out.append(
                {
                    "id": number if number is not None else len(out) + 1,
                    "text": question,
                    "options": options,
                }
            )
        return out

    return {
        "open_questions": _collect(("question:",)),
        "resolvable_uncertainties": _collect(("uncertainty:",)),
    }

def maybe_extract_plan_readiness(text: str) -> str:
    lowered = text.lower()
    if "ready" in lowered and "not ready" not in lowered:
        return "ready"
    if "partial" in lowered:
        return "partial"
    if "not ready" in lowered:
        return "not_ready"
    return "unknown"
