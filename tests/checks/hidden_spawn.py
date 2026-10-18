"""Every subprocess the runtime starts hides its console window on Windows.

The runtime's work happens in a detached worker that has no console of its own. On
Windows, a console program (git, node, a `.cmd` shim) started from such a process gets a
fresh console window — it opens and closes in a blink, once per call. That flashing was
fixed once for OpenCode and again with the shared `osutil.hidden_run_kwargs()`; it came
back in v3.7.3 through a new module that called `git` with a bare `subprocess.run`. The
fix was never the problem — nothing said a new spawn site had to use it. This does.

Scope is the code the worker runs: `core/`, `adapters/`, `utils/`, and the shipped hooks
under `dist/config/claude/hooks/`, which Claude Code starts without a console too (v3.8.3:
the Bash hook port brought the flash back through task-events' bare `git`). `tools/` and
`installer/` run in the user's own terminal, where a child shares that console and
nothing flashes.

A call passes when it carries `**hidden_run_kwargs()`, `**detached_popen_kwargs()`, an
explicit `creationflags=`, or `**<name>` where the same file puts a `creationflags` key in
that dict (the hooks are stdlib-only and cannot import the helpers). A call that can only
run off Windows goes in `_POSIX_ONLY`
with the reason, keyed by file and the command's first argument — not by line number,
so the allowance cannot drift onto a different call.
"""

import ast
from pathlib import Path

from tests.checks.support import assert_true

REPO_ROOT = Path(__file__).resolve().parents[2]

_SCOPE = ("core", "adapters", "utils", "dist/config/claude/hooks")
_SKIP_PARTS = {"__pycache__"}
_SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}
_HIDING_HELPERS = {"hidden_run_kwargs", "detached_popen_kwargs"}

# (file, first element of the argv list literal) -> why it never runs on Windows.
_POSIX_ONLY = {
    ("utils/osutil.py", "ps"): "the `else` branch of the Windows process-tree lookup",
}


def _is_subprocess_spawn(call: ast.Call) -> bool:
    """`subprocess.<spawner>(...)`, however `subprocess` is reached (`_main().subprocess`)."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in _SPAWNERS:
        return False
    owner = func.value
    if isinstance(owner, ast.Name):
        return owner.id == "subprocess"
    return isinstance(owner, ast.Attribute) and owner.attr == "subprocess"


def _hiding_dicts(tree: ast.AST) -> set[str]:
    """Names the file gives a `creationflags` key: `x = {"creationflags": ...}` or
    `x["creationflags"] = ...`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
                if any(isinstance(k, ast.Constant) and k.value == "creationflags" for k in node.value.keys):
                    names.add(target.id)
            elif (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "creationflags"
            ):
                names.add(target.value.id)
    return names


def _hides_console(call: ast.Call, dicts: set[str] = frozenset()) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "creationflags":
            return True
        if keyword.arg is None and isinstance(keyword.value, ast.Name) and keyword.value.id in dicts:
            return True
        if keyword.arg is None and isinstance(keyword.value, ast.Call):
            target = keyword.value.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name in _HIDING_HELPERS:
                return True
    return False


def _argv_head(call: ast.Call) -> str:
    if call.args and isinstance(call.args[0], ast.List) and call.args[0].elts:
        head = call.args[0].elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return ""


def _python_files() -> list[Path]:
    found: list[Path] = []
    for entry in _SCOPE:
        for path in (REPO_ROOT / entry).rglob("*.py"):
            if set(path.relative_to(REPO_ROOT).parts) & _SKIP_PARTS:
                continue
            found.append(path)
    return sorted(found)


def _check_scanner_sees_the_known_shapes() -> None:
    # A scanner that matches nothing passes for the wrong reason; pin what it must catch.
    source = (
        "import subprocess\n"
        "subprocess.run(['git', 'status'])\n"
        "subprocess.run(['git'], **hidden_run_kwargs())\n"
        "_main().subprocess.Popen(['x'], **osutil.detached_popen_kwargs())\n"
        "subprocess.Popen(['y'], creationflags=0)\n"
        "_main().subprocess.Popen(['z'])\n"
        "hide = {'creationflags': 0x08000000}\n"
        "flags = {}\n"
        "flags['creationflags'] = 0x08000000\n"
        "subprocess.Popen(['a'], **hide)\n"
        "subprocess.Popen(['b'], **flags)\n"
        "other = {'cwd': '.'}\n"
        "subprocess.run(['c'], **other)\n"
    )
    tree = ast.parse(source)
    dicts = _hiding_dicts(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_subprocess_spawn(node)]
    assert_true(len(calls) == 8, f"scanner found {len(calls)} spawn calls in the sample, expected 8")
    bare = sorted(call.lineno for call in calls if not _hides_console(call, dicts))
    assert_true(bare == [2, 6, 13], f"scanner flagged lines {bare} as bare, expected [2, 6, 13]")


def _test_every_spawn_hides_its_console() -> None:
    _check_scanner_sees_the_known_shapes()

    files = _python_files()
    assert_true(
        len(files) > 20,
        f"only {len(files)} runtime Python files found under {REPO_ROOT} — the scan is broken",
    )

    offenders: list[str] = []
    allowed_seen: set[tuple[str, str]] = set()
    spawns = 0
    for path in files:
        relative = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        dicts = _hiding_dicts(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_subprocess_spawn(node):
                continue
            spawns += 1
            if _hides_console(node, dicts):
                continue
            key = (relative, _argv_head(node))
            if key in _POSIX_ONLY:
                allowed_seen.add(key)
                continue
            offenders.append(f"{relative}:{node.lineno}")

    assert_true(spawns > 5, f"only {spawns} subprocess calls found in the runtime — the scan is broken")
    stale = sorted(set(_POSIX_ONLY) - allowed_seen)
    assert_true(
        not stale,
        f"_POSIX_ONLY lists calls that no longer exist, so it no longer says what it allows: {stale}",
    )
    assert_true(
        not offenders,
        "a runtime subprocess call does not hide its console, so on Windows every call "
        "flashes a window from the detached worker:\n  "
        + "\n  ".join(offenders)
        + "\n\nPass **osutil.hidden_run_kwargs() (or detached_popen_kwargs() for a worker), "
        "or add the call to _POSIX_ONLY with the reason it never runs on Windows.",
    )
