"""The bundle's two hand-maintained bijections must hold.

`tools/maintain/sync_skills.py` and `tools/maintain/sync_intent_map.py` were both written to be run "for
CI / doctor" — their own docstrings say so — and neither had a caller. `.github/workflows/
ci.yml` gates `stamp_version` and `gen_manifest` and nothing else, so a skill file added
without its registry entry, or an intent-map pattern added without the matching NL-map
trigger, merged green. `tools/e2e/e2e_installer.py` checks that skills and `intent-map.json`
are *installed*, not that they *agree* with CLAUDE.md, which reads like coverage without
being it.

This is the caller. It runs both tools through their real entry points rather than
re-implementing the comparison: a check that reimplements the tool proves the
reimplementation, and the tool is what CI and a maintainer actually invoke.

The tools write nothing under `--check`, so this stays a read-only check with no fixture.
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

from tests.checks.support import assert_true

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "src"))


def _run_check(module_name: str) -> tuple[int, str]:
    """Invoke one sync tool's `main()` under `--check`. Returns (exit code, its output).

    `main()` reads `sys.argv` and prints its report, both of which belong to a CLI rather
    than to a test. argv is swapped for the duration and stdout captured so the tool's own
    wording — including the `Fix:` line it prints — reaches the failure message instead of
    being swallowed. `SystemExit` is the tool's hard path (a source file missing entirely)
    and is reported as a failure rather than allowed to abort the run.
    """
    module = __import__(f"tools.maintain.{module_name}", fromlist=["main"])
    buffer = io.StringIO()
    original_argv = sys.argv
    sys.argv = [f"{module_name}.py", "--check"]
    try:
        with contextlib.redirect_stdout(buffer):
            code = module.main()
    except SystemExit as exc:
        return 1, f"{buffer.getvalue()}{exc}".strip()
    finally:
        sys.argv = original_argv
    return int(code), buffer.getvalue().strip()


def _test_every_shipped_hook_is_a_launcher_and_its_module() -> None:
    """Each hook is `<stem>.sh` (the launcher Claude Code runs on every OS) beside
    `<stem>.py` (its logic), and workflow_hooks.py knows every one of them.

    A launcher whose module is missing, or a module workflow_hooks.py does not list, fails
    open: the hook exits 0 and the feature is gone with nothing in the install output to say
    so — a statusline that renders nothing, a gate that stops gating. No `.ps1` ships any
    more; one coming back would be a second copy of the logic to drift.
    """
    import ast

    hooks = REPO_ROOT / "src" / "exoma" / "_bundle" / "config" / "hosts" / "claude" / "hooks"
    launchers = {p.stem for p in hooks.glob("*.sh")}
    # Not Claude Code hooks: workflow_policy.py is the library the hooks import, and
    # agy-host.py is Agy's entry point, registered in ~/.gemini/config/hooks.json and run
    # without a launcher (tests/checks/agy_host.py).
    modules = {p.stem for p in hooks.glob("*.py")} - {"workflow_hooks", "workflow_policy", "agy-host"}
    assert_true(bool(launchers), f"no hook launchers found under {hooks}")
    assert_true(
        launchers == modules,
        f"every launcher needs its module and every module its launcher: launchers only "
        f"{sorted(launchers - modules)}, modules only {sorted(modules - launchers)}",
    )
    tree = ast.parse((hooks / "workflow_hooks.py").read_text(encoding="utf-8"))
    listed = next(
        set(ast.literal_eval(node.value))
        for node in tree.body
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "HOOKS" for t in node.targets)
    )
    assert_true(listed == launchers, f"workflow_hooks.HOOKS names exactly the shipped hooks: {sorted(listed ^ launchers)}")
    retired = sorted(p.name for p in hooks.glob("*.ps1"))
    assert_true(not retired, f"no PowerShell flavour ships: {retired}")
    # Nor an instruction to run one: a branch cut before the bash runner merged its
    # `& "<work_dir>\.workflow\run.ps1" ...` back into the provider skill, a command for a
    # file init no longer writes. Naming run.ps1 as retired stays allowed.
    import re

    invokes = re.compile(r"&\s*\"[^\"\n]*\.ps1\"")
    calling = sorted(
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "src" / "exoma" / "_bundle" / "config").rglob("*.md")
        if invokes.search(p.read_text(encoding="utf-8"))
    )
    assert_true(not calling, f"no shipped instruction invokes a retired .ps1 runner: {calling}")


def _test_bundle_registry_bijection() -> None:
    """skills/ <-> CLAUDE.md registry, and intent-map.json <-> CLAUDE.md NL-map."""
    # Reported one at a time. A skills mismatch and an intent-map drift have different
    # fixes, and collapsing them into one assertion would hide the second behind the first.
    for module_name, subject in (
        ("sync_skills", "skills/ and the CLAUDE.md command registry"),
        ("sync_intent_map", "intent-map.json and the CLAUDE.md NL-map"),
    ):
        code, output = _run_check(module_name)
        assert_true(
            code == 0,
            f"{subject} disagree — the runtime gate and the prompt would classify "
            f"commands differently.\n{output}\n"
            f"Run: python tools/maintain/{module_name}.py --check",
        )
    _check_task_events_local_skills()


def _check_task_events_local_skills() -> None:
    """task-events' LOCAL_SKILLS is the registry's LOCAL: line, copied.

    The hook is standalone and cannot import the registry, so the list is a hand copy; it
    went on naming refactor, review and compress after DEC-069 removed those skills, until
    073eb82. Read
    with `ast`, not imported: importing a hook runs it.
    """
    import ast

    from tools.maintain.sync_skills import CLAUDE_MD, _COMMAND_RE

    hook = REPO_ROOT / "src" / "exoma" / "_bundle" / "config" / "hosts" / "claude" / "hooks" / "task-events.py"
    shipped: set[str] | None = None
    for node in ast.parse(hook.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "LOCAL_SKILLS" for t in node.targets
        ):
            shipped = set(ast.literal_eval(node.value))
    registry = {
        name
        for line in CLAUDE_MD.read_text(encoding="utf-8").splitlines()
        if line.startswith("LOCAL:")
        for name in _COMMAND_RE.findall(line)
    }
    assert_true(
        shipped == registry,
        f"task-events LOCAL_SKILLS disagrees with the CLAUDE.md LOCAL: registry — "
        f"only in the hook: {sorted((shipped or set()) - registry)}, "
        f"only in the registry: {sorted(registry - (shipped or set()))}",
    )
