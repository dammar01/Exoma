# Skill: sweep
description: Local Git diff scan → impact report. Does not call second_agent.

## Trigger
{{CMD:sweep}}

## Run (local)
Every OS ({{SHELL_TOOL}}): bash '<work_dir>/.workflow/run.sh' sweep{{MAIN_HOST_OPTION}} --session '<MAIN_SESSION_ID>'
<MAIN_SESSION_ID> from [SESSION BINDING], REQUIRED as `--session` (concurrent isolation; the runner refuses sweep without it).
Runtime reads tracked and untracked changes, writes `reports/sweep.last.md`, and returns
`meta.verdict`, `meta.changed_files`, plus the report path. It does not call second_agent.
Run script missing → fallback: `git diff HEAD`, `git status` directly, source: claude (direct).

## Output [SWEEP RESULT]
Relay runtime content + meta. changed_files | impact | risks | uncertainties (empty → reason).

## End
"Impact report ready. Continue with {{CMD:verify}} or {{CMD:plan}}?"
