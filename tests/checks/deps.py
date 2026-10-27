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

# Shipped modules imported by their bare name from the directory they are installed into:
# the hooks run standalone from ~/.claude/hooks and reach workflow_policy.py beside them.
# First-party, but only from that directory, and only while the sibling exists.
_HOOK_SIBLINGS = {
    "workflow_policy": "src/exoma/_bundle/config/claude/hooks/",
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


def _imported_roots(path: Path) -> set[str]:
    """Top-level module names this file imports, by any import form.

    An optional extra imported in its allowed package under an ImportError guard is
    not reported; the same import anywhere else, or unguarded, is.
    """
    roots: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded = _guarded_imports(tree)
    relative = path.relative_to(REPO_ROOT).as_posix()
    for node in ast.walk(tree):
        for root in _import_root(node):
            prefix = _OPTIONAL_EXTRAS.get(root)
            if prefix and relative.startswith(prefix) and id(node) in guarded:
                continue
            roots.add(root)
    return roots


def _test_runtime_is_stdlib_only() -> None:
    files = _python_files()
    assert_true(
        len(files) > 20,
        f"only {len(files)} shipped Python files found under {REPO_ROOT} — the scan is "
        "broken, and a broken scan passes for the wrong reason",
    )

    stdlib = set(sys.stdlib_module_names)
    offenders: list[str] = []
    for path in files:
        for root in sorted(_imported_roots(path)):
            if root in stdlib or root in _INTERNAL:
                continue
            sibling_dir = _HOOK_SIBLINGS.get(root)
            if (sibling_dir and path.relative_to(REPO_ROOT).as_posix().startswith(sibling_dir)
                    and (REPO_ROOT / sibling_dir / f"{root}.py").is_file()):
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {root}")

    assert_true(
        not offenders,
        "shipped code imports a third-party module, so the runtime is no longer pure "
        "stdlib and the absence of a lockfile has stopped being correct:\n  "
        + "\n  ".join(offenders)
        + "\n\nEither drop the import or add a pinned dependency file and a CI install step.",
    )
