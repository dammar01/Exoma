# Skill: doctor
description: Local .workflow readiness and bundle-integrity check.

## Trigger
/.doctor

## STEP 1 — Bootstrap check
- .workflow/run.sh EXISTS → local runtime call:
    Every OS (Bash tool): bash '<work_dir>/.workflow/run.sh' doctor 'check .workflow readiness'
- No .workflow yet → STEP 2 (local check). Do NOT fail, do NOT conclude the package is missing.

## STEP 2 — Local check (fallback, checked directly)
.workflow/          : EXISTS | MISSING
.workflow/run.*     : EXISTS | MISSING
.gitignore          : CONTAINS .workflow/ | MISSING
exoma               : <version> (`exoma --version`) | NOT INSTALLED
.workflow/config.json : v0.2.0 (runtime.python set) | old | MISSING
graphify-out/       : EXISTS | MISSING
bundle (~/.claude)  : READY | DRIFTED | not_checked — `exoma install --check` (skills/CLAUDE.md/AGENTS.md vs the shipped bundle; DRIFTED → `exoma install --apply`)
second_agent MCP    : SAFE | RISK (<server>) | REVIEW (<server>) | NONE — scans the MCP config of the ACTIVE provider, not always opencode (context7=safe read-only; write/exec/fs/db/browser=risk)
read boundary       : ENFORCED (opencode) | NOT_ENFORCEABLE (codex|agy) — codex and agy do not enforce a read boundary (runtime: `checks.second_agent_read_boundary` status `not_enforceable`, `trusted: true`, `secret_files` = the project files matching the secret patterns); their second_agent can read every project file, `.env` included, and receives the full process environment. This is a WARNING about the user's choice, not NOT_READY
legacy env          : NONE | <AI_PROXY_* names> — still read; recommend renaming to EXOMA_*

## Output
[DOCTOR REPORT]
source: runtime (local) | claude (fallback)
checks: <every STEP 2 item + status>
mcp_second_agent: <verdict + server list + classification> — RISK/REVIEW = the second_agent goes beyond read-only, MUST be shown + reason
read_boundary: <ENFORCED|NOT_ENFORCEABLE + provider + secret files found> — NOT_ENFORCEABLE MUST be shown, never summarized away
status: READY | NEEDS_UPGRADE | NOT_READY
actions: <a fix per MISSING/NOT SET item + disable/confirm a risky MCP> (empty → "none — all OK")
`checks.workspace_layout.layout_version` 1 → the workspace still has the 3.6 layout; relay the `/.upgrade` recommendation (migrates to `.workflow/data/`, backs up first). `stray_at_root` → name the unknown files at the `.workflow` root, do not delete them yourself.
NOT_READY + workspace missing → "Run /.init". NEEDS_UPGRADE → run `/.upgrade`.
exoma NOT INSTALLED → install it first (see /.init STEP 1).
