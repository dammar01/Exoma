"""Every check in tests/checks/ must actually be reachable.

Written after `_test_agy_provider` shipped with ~230 lines of assertions and zero
callsites: it was absent from `SUITES` and absent from `run_tests()`, so `python
tests/run.py` never touched it. agy was the newest provider and the only one whose
tests were dead, which is the worst possible pairing — the suite reported success
while the least-proven code path was the one being skipped.

The drift is structural rather than careless. The suite names live in `tests/run.py`
and the execution order lives in `tests/scenario.py`, so adding a check means editing
two files that nothing forces to agree. This check is that force.
"""

import ast
import textwrap
from pathlib import Path

from tests.checks.support import assert_true

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKS_DIR = REPO_ROOT / "tests" / "checks"
RUN_PY = REPO_ROOT / "tests" / "run.py"
SCENARIO_PY = REPO_ROOT / "tests" / "scenario.py"

# This module's own check is registered in SUITES like any other; nothing here is exempt.
# `support.py` holds fakes rather than checks, so it defines no `_test_*` to begin with.
_IGNORED_FILES = {"__init__.py"}


def _defined_checks() -> dict[str, str]:
    """`_test_*` function name -> the file that defines it."""
    found: dict[str, str] = {}
    for path in sorted(CHECKS_DIR.glob("*.py")):
        if path.name in _IGNORED_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            # Top-level only. A nested helper named `_test_` something is an
            # implementation detail of the check around it, not a check itself.
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_test_"):
                found[node.name] = f"{path.name}:{node.lineno}"
    return found


def _calls_inside(path: Path, function: str) -> set[str]:
    """Every `name()` called within one named top-level function.

    Scoped to that function rather than the whole file on purpose. Scanning the module
    would count a call sitting in a helper nobody invokes, which is exactly the state
    this check exists to reject: the name is present, the check never runs, and the
    guard reports green.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    body = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == function
        ),
        None,
    )
    assert_true(
        body is not None,
        f"{path.name} defines no top-level `{function}()` — the entry point this check "
        "reads was renamed or moved, so it can no longer prove anything",
    )
    return _function_calls(body)


def _function_calls(function) -> set[str]:
    """Follow direct calls, including invoked local functions, without counting dead bodies."""
    class Calls(ast.NodeVisitor):
        def __init__(self):
            self.names, self.local = set(), {}

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name):
                self.names.add(node.func.id)
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            self.local[node.name] = node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            pass

        def visit_Lambda(self, node):
            pass

    calls = Calls()
    for statement in function.body:
        calls.visit(statement)
    visited = set()
    while pending := (calls.names & calls.local.keys()) - visited:
        for name in pending:
            visited.add(name)
            calls.names.update(_function_calls(calls.local[name]))
    return calls.names - calls.local.keys()


def _unreachable_check_helpers(tree, entrypoint) -> set[str]:
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert_true(entrypoint in functions, f"helper guard entrypoint {entrypoint} exists")
    reached, pending = set(), {entrypoint}
    while pending:
        name = pending.pop()
        if name in reached or name not in functions:
            continue
        reached.add(name)
        pending.update(_function_calls(functions[name]) - reached)
    return {name for name in functions if name.startswith("_check_")} - reached


def _check_self_update_helpers() -> None:
    # A definition, or a call inside another orphan, is not an execution path.
    fixtures = (
        ("direct", """
            def _test_self_update(): _check_leaf()
            def _check_leaf(): pass
        """, set()),
        ("transitive", """
            def _test_self_update(): setup()
            def setup(): _check_leaf()
            def _check_leaf(): pass
        """, set()),
        ("orphan caller", """
            def _test_self_update(): pass
            def unused(): _check_leaf()
            def _check_leaf(): pass
        """, {"_check_leaf"}),
        ("uncalled nested body", """
            def _test_self_update():
                def unused(): _check_leaf()
            def _check_leaf(): pass
        """, {"_check_leaf"}),
        ("called nested body", """
            def _test_self_update():
                def run(): _check_leaf()
                run()
            def _check_leaf(): pass
        """, set()),
        ("uncalled lambda", """
            def _test_self_update():
                unused = lambda: _check_leaf()
            def _check_leaf(): pass
        """, {"_check_leaf"}),
        ("unreachable cycle", """
            def _test_self_update(): pass
            def _check_first(): _check_second()
            def _check_second(): _check_first()
        """, {"_check_first", "_check_second"}),
    )
    for label, source, expected in fixtures:
        actual = _unreachable_check_helpers(ast.parse(textwrap.dedent(source)), "_test_self_update")
        assert_true(actual == expected, f"{label}: orphan helpers {actual}, expected {expected}")
    source = CHECKS_DIR / "self_update.py"
    orphaned = _unreachable_check_helpers(ast.parse(source.read_text(encoding="utf-8")), "_test_self_update")
    assert_true(not orphaned, "self-update helpers unreachable from their entrypoint: " + ", ".join(sorted(orphaned)))


def _registered_in_suites() -> set[str]:
    """Names appearing as the callable in the SUITES dict of tests/run.py."""
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets = getattr(node, "targets", []) or [getattr(node, "target", None)]
        named_suites = any(
            isinstance(target, ast.Name) and target.id == "SUITES"
            for target in targets
            if target is not None
        )
        if not named_suites or not isinstance(node.value, ast.Dict):
            continue
        return {
            element.id
            for entry in node.value.values
            if isinstance(entry, ast.Tuple)
            for element in entry.elts
            if isinstance(element, ast.Name)
        }
    return set()


def _test_every_check_is_registered() -> None:
    _check_self_update_helpers()
    defined = _defined_checks()
    assert_true(
        defined,
        f"no _test_* functions found under {CHECKS_DIR} — the scan itself is broken, "
        "which would make this check pass for the wrong reason",
    )

    suites = _registered_in_suites()
    assert_true(
        suites,
        f"SUITES in {RUN_PY.name} parsed to nothing — the registry moved or changed "
        "shape, and this check would silently stop guarding anything",
    )
    executed = _calls_inside(SCENARIO_PY, "run_tests")

    # Two separate failures, reported separately: one means `--only <name>` cannot reach
    # the check, the other means a default `python tests/run.py` never runs it. A check
    # can be in SUITES and still be dead in CI, which is the case that got missed.
    unregistered = sorted(
        f"{name} ({defined[name]})" for name in defined if name not in suites
    )
    assert_true(
        not unregistered,
        "check(s) defined but absent from SUITES in tests/run.py, so `--only` cannot "
        "reach them:\n  " + "\n  ".join(unregistered),
    )

    uncalled = sorted(
        f"{name} ({defined[name]})" for name in defined if name not in executed
    )
    assert_true(
        not uncalled,
        "check(s) never called by run_tests() in tests/scenario.py — a default "
        "`python tests/run.py` runs `scenario` only, so these never execute in CI:\n  "
        + "\n  ".join(uncalled),
    )
