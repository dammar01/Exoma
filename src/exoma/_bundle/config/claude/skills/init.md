# Skill: init
description: Create/regenerate the .workflow/ workspace. Local. Bootstraps through the `exoma` CLI.

## Trigger
/.init

## STEP 1 — Resolve the bootstrap source (REQUIRED — in this order, do NOT skip)
IMPORTANT: the runtime is NOT in the project. It is the Python package `exoma` (`pip install exoma` / `uv tool install exoma`).
Resolve in order:
1. `exoma --version` runs → THIS IS THE SOURCE. Continue to STEP 2 with `exoma`.
2. Not on PATH but `<python> -m exoma --version` runs → use `<python> -m exoma`.
3. Still nothing → ask the user to install it first: `uv tool install exoma` or `pip install exoma`.
`$AGENT_PATH` (an agent-workflow checkout) = the old alias: read only when the user names it, NOT the main source.
Do NOT conclude "package missing" before checking `exoma --version` and `<python> -m exoma --version`.

## STEP 2 — Run init
work_dir = absolute path of the active project.
Every OS (Bash tool): exoma init --work-dir "<work_dir>" --pretty
(fallback: `<python> -m exoma init --work-dir "<work_dir>" --pretty`)
<python> = the first of `python3`, `python`, `py -3` that RUNS and is ≥ 3.10 (Windows often has `python`, mac/linux often only `python3`). Older → exoma refuses with one message; use another interpreter, do not guess "package missing".
init automatically: generates scripts (run/inspect/check .sh, bash on every OS, calling `<python> -m exoma`) + config (interpreter + package root, abs-path) + copies second_agent.json + sessions/ scaffold + .gitignore (.workflow/). state/scope/cache/logs/runtime = per-session, created lazily on the first delegated call (NOT at the root).
init is a SCAFFOLD, not an update: an EXISTING config.json and second_agent.json are not overwritten (init has no active-job guard — that belongs to /.upgrade). A workspace from an old build stays at the old version after init. `meta.upgrade_needed: true` + `meta.versions` in the output = run /.upgrade.

## Output
[INIT]
bootstrap: exoma <version> (<exoma | python -m exoma>)
generated: run/inspect/check.sh + config.json (v0.2.0, python + package_root abs) + second_agent.json (copy) + sessions/ (state/scope/cache/logs/runtime per-session, lazy)
gitignore: .workflow/ ok
upgrade_needed: <meta.upgrade_needed> (true → name the versions from meta.versions + offer /.upgrade)
status: READY
".workflow is ready. Try /.explore or /.doctor."
