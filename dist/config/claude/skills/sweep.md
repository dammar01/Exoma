# Skill: sweep
description: Local Git diff scan → impact report. Does not call second_agent.

## Trigger
/.sweep

## Run (local)
Semua OS (tool Bash): bash '<work_dir>/.workflow/run.sh' sweep 'scan git diff, identify impact' '<MAIN_SESSION_ID>'
<MAIN_SESSION_ID> dari [SESSION BINDING], WAJIB arg ke-3 (isolasi concurrent).
Runtime reads tracked and untracked changes, writes `reports/sweep.last.md`, and returns
`meta.verdict`, `meta.changed_files`, plus the report path. It does not call second_agent.
run script hilang → fallback: `git diff HEAD`, `git status` langsung, source: claude (direct).

## Output [SWEEP RESULT]
Relay runtime content + meta. changed_files | impact | risks | uncertainties (kosong → alasan).

## End
"Impact selesai. Lanjut /.verify atau /.plan?"
