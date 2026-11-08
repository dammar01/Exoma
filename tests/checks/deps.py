"""The runtime imports nothing it does not ship with.

This is the check that replaces a lockfile rather than the check that postpones one.
"No third-party dependencies" is what makes a missing requirements.txt correct instead
of careless — but it was only ever an observation someone made once, and a single
`import requests` added in good faith would have quietly converted this repo into one
that needs pinning, with nothing to say so. A constraints file listing nothing would
document the claim without testing it.

Scope is the shipped runtime. tests/ may import whatever it likes: a test dependency
does not travel to a user's machine, and pretending otherwise would just push real test
tooling out of reach for no gain.
"""

import ast
import re
import sys
from pathlib import Path

from tests.checks.support import assert_true

REPO_ROOT = Path(__file__).resolve().parents[2]

# Everything a user actually runs. `tools/` is included because init/upgrade shell out to
# it, and `installer/` because it runs on the machine being set up.
_SHIPPED = ("main.py", "check.py", "install.py", "src/exoma", "tools")

# First-party package roots. Absolute imports of these are internal, not external.
_INTERNAL = {
    "exoma", "main", "check", "install", "tools", "tests",
}

_SKIP_PARTS = {"__pycache__", ".git", ".workflow", "graphify-out", "dist", "build"}

# Optional extras: a third-party module the runtime may import ONLY inside the named
# package AND only inside a `try: ... except ImportError` — so the module's absence is a
# handled condition (`incomplete` with a reason), never a crash at import time. Anything
# outside that shape is an offender like any other. The shape is the whole point: the
# check that used to read "nothing third-party" now reads "nothing third-party that a
# user could be surprised by", and a guarded lazy import is the one form that cannot
# surprise anyone.
_OPTIONAL_EXTRAS = {
    "playwright": "src/exoma/core/evidence/e2e/",
}
_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError"}

# Stdlib modules newer than the oldest Python the package supports, by the version that
# added them. While `requires-python` still admits an older interpreter, such a module is
# absent there exactly like a missing extra, so it takes the same shape: imported only
# under an ImportError guard, in any shipped file. The rule is judged against the
# package's floor, not the interpreter running this check — an unguarded `import tomllib`
# is found on 3.13 too, where the module exists, because it would still crash on 3.10.
_VERSIONED_STDLIB = {
    "tomllib": (3, 11),
}

# Shipped modules imported by their bare name from the directory they are installed into.
# The Codex and OpenCode hosts install the Claude source beside their hook at its destination.
_HOOK_SIBLINGS = {
    "workflow_policy": (
        "src/exoma/_bundle/config/hosts/claude/hooks/",
        "src/exoma/_bundle/config/hosts/codex/",
        "src/exoma/_bundle/config/hosts/opencode/",
    ),
}


def _python_files() -> list[Path]:
    found: list[Path] = []
    for entry in _SHIPPED:
        target = REPO_ROOT / entry
        if target.is_file():
            found.append(target)
            continue
        for path in target.rglob("*.py"):
            if set(path.relative_to(REPO_ROOT).parts) & _SKIP_PARTS:
                continue
            found.append(path)
    return sorted(found)


def _import_root(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".", 1)[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        # A relative import resolves inside this repo by definition.
        if node.level or not node.module:
            return []
        return [node.module.split(".", 1)[0]]
    return []


def _guarded_imports(tree: ast.AST) -> set[int]:
    """Import nodes (by id) that sit inside a `try` whose handlers catch ImportError.

    Node identity, not module name: the guard has to cover THIS import, and a second,
    bare import of the same module elsewhere in the file must still be reported.
    """
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches = False
        for handler in node.handlers:
            names = []
            if isinstance(handler.type, ast.Name):
                names = [handler.type.id]
            elif isinstance(handler.type, ast.Tuple):
                names = [elt.id for elt in handler.type.elts if isinstance(elt, ast.Name)]
            if set(names) & _IMPORT_ERRORS:
                catches = True
        if not catches:
            continue
        for inner in node.body:
            for sub in ast.walk(inner):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(sub))
    return guarded


def _python_floor() -> tuple[int, int]:
    """The oldest Python the package admits, read from pyproject's `requires-python`."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^requires-python\s*=\s*">=\s*(\d+)\.(\d+)', text, re.M)
    assert_true(match is not None, 'pyproject.toml has no `requires-python = ">=X.Y"`; the stdlib floor is unknown')
    return int(match[1]), int(match[2])


def _late_stdlib(floor: tuple[int, int]) -> frozenset[str]:
    """Stdlib modules the floor interpreter does not have."""
    return frozenset(name for name, since in _VERSIONED_STDLIB.items() if since > floor)


def _roots_in(tree: ast.AST, relative: str, late: frozenset[str]) -> set[str]:
    """Top-level module names this tree imports, by any import form.

    An optional extra imported in its allowed package under an ImportError guard is
    not reported, nor is a late stdlib module imported under one anywhere; the same
    import unguarded, or an extra outside its package, is.
    """
    roots: set[str] = set()
    guarded = _guarded_imports(tree)
    for node in ast.walk(tree):
        for root in _import_root(node):
            if id(node) in guarded:
                if root in late:
                    continue
                prefix = _OPTIONAL_EXTRAS.get(root)
                if prefix and relative.startswith(prefix):
                    continue
            roots.add(root)
    return roots


def _imported_roots(path: Path, late: frozenset[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return _roots_in(tree, path.relative_to(REPO_ROOT).as_posix(), late)


def _scanner_judges_late_stdlib_by_guard(late: frozenset[str]) -> None:
    """The exemption is the guard, not the module name: prove both directions."""
    assert_true("tomllib" in _late_stdlib((3, 10)) and "tomllib" not in _late_stdlib((3, 11)),
                "tomllib is late stdlib below 3.11 and plain stdlib from 3.11")
    guarded = ast.parse("def f():\n    try:\n        import tomllib\n    except ImportError:\n        return None\n")
    bare = ast.parse("import tomllib\n")
    late_ok = frozenset({"tomllib"})
    assert_true(_roots_in(guarded, "src/exoma/anywhere.py", late_ok) == set(),
                "a guarded late-stdlib import passes in any shipped file")
    assert_true(_roots_in(bare, "src/exoma/anywhere.py", late_ok) == {"tomllib"},
                "an unguarded late-stdlib import is reported")
    mixed = ast.parse("try:\n    import tomllib\nexcept ImportError:\n    pass\nimport tomllib as again\n")
    assert_true(_roots_in(mixed, "src/exoma/anywhere.py", late_ok) == {"tomllib"},
                "a second, bare import of a guarded late module is still reported")
    stdlib = set(sys.stdlib_module_names) - late
    assert_true(not (late & stdlib), "late stdlib never counts as stdlib, whatever interpreter runs the check")


def _test_runtime_is_stdlib_only() -> None:
    files = _python_files()
    assert_true(
        len(files) > 20,
        f"only {len(files)} shipped Python files found under {REPO_ROOT} — the scan is "
        "broken, and a broken scan passes for the wrong reason",
    )

    late = _late_stdlib(_python_floor())
    _scanner_judges_late_stdlib_by_guard(late)
    # A late module is judged by its guard alone, so it is never stdlib here, even on an
    # interpreter that has it.
    stdlib = set(sys.stdlib_module_names) - late
    offenders: list[str] = []
    for path in files:
        for root in sorted(_imported_roots(path, late)):
            if root in stdlib or root in _INTERNAL:
                continue
            sibling_dirs = _HOOK_SIBLINGS.get(root, ())
            relative = path.relative_to(REPO_ROOT).as_posix()
            if (any(relative.startswith(folder) for folder in sibling_dirs)
                    and (REPO_ROOT / "src/exoma/_bundle/config/hosts/claude/hooks" / f"{root}.py").is_file()):
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {root}")

    assert_true(
        not offenders,
        "shipped code imports a third-party module, so the runtime is no longer pure "
        "stdlib and the absence of a lockfile has stopped being correct:\n  "
        + "\n  ".join(offenders)
        + "\n\nEither drop the import or add a pinned dependency file and a CI install step.",
    )
