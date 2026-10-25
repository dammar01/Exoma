"""User-facing text must not carry internal accessor syntax.

Written after a real escape: the v3.4.3 split of main.py rewrote `run` -> `_main().run`
with a word-boundary regex, and the regex could not tell code from prose. Four
`next_action` messages shipped telling the user to run ``opencode _main().run``. Every
one of the 87 e2e checks, 30 simulated flows and 211 unit assertions passed — none of
them reads the words, only the behaviour.

`next_action` is the line a user follows when something has already gone wrong, so it is
the worst possible place for a leaked expression. This scan is deliberately narrow: it
looks for accessor syntax that has no business inside a sentence, not for style.
"""

import ast
from pathlib import Path

from tests.checks.support import assert_true

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories whose strings reach a user or an agent.
SCANNED = ("src/exoma/core", "src/exoma/adapters", "src/exoma/installer", "src/exoma/utils")
ROOT_FILES = ("src/exoma/cli.py", "src/exoma/install.py", "src/exoma/check.py")

# Substrings that are code, never prose. `_main().` is the one that actually shipped;
# the others are the same mistake waiting in the modules that use self-accessors.
LEAKS = ("_main().", "self.adapter.", "self.opencode.")


def _string_constants(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node


def _sources():
    for directory in SCANNED:
        yield from sorted((REPO_ROOT / directory).rglob("*.py"))
    for name in ROOT_FILES:
        path = REPO_ROOT / name
        if path.exists():
            yield path


def _test_no_code_in_messages() -> None:
    offenders = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        if not any(leak in text for leak in LEAKS):
            continue
        tree = ast.parse(text)
        for node in _string_constants(tree):
            # A docstring may legitimately discuss the accessor it documents.
            if node.col_offset == 0:
                continue
            for leak in LEAKS:
                if leak in node.value:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {node.value[:70]!r}"
                    )
    assert_true(
        not offenders,
        "internal accessor syntax leaked into a string a user reads — a regex rewrite "
        "hit prose instead of code:\n  " + "\n  ".join(offenders),
    )
