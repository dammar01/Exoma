# Skill: explore
description: Codebase evidence via second_agent (1-call). Fallback: /.local.
argument-hint: <hint>

## Trigger
/.explore <hint>

## Run (1-call)
Every OS (Bash tool): bash '<work_dir>/.workflow/run.sh' explore '<hint>' '<MAIN_SESSION_ID>'
- <MAIN_SESSION_ID> = the value from the [SESSION BINDING] block. REQUIRED (3rd argument) — concurrent sessions on the same project need per-session isolation. Absent → the run script's fallback (fine for a single agent).
- Blocks until done. Returns JSON {ok, content, meta, digest}.
- Do not invent commands, do not use check.sh, do not call `exoma` directly.
- No .workflow yet / run script missing → /.init first.
- FAILED (ok:false | invalid_evidence | content is a menu/refusal, no [EVIDENCE]/[DIGEST]) → HARD GATE:
  STOP → output "[PROXY FAILED] <reason>. Continue with /.local? (yes/no)" → WAIT for the user. Do NOT auto-fallback.

## Output (RELAY mode)
digest present → relay: summary, key_findings, risk_level, recommended_next_action, confidence.
digest absent (fallback) → a full [EXPLORATION RESULT] from content:
  source | session | confidence | entry_points | ownership_hints | related_modules | uncertainties
  (every empty field → show it + the reason). Need detail → open content.

## End
"Continue with /.plan, or is this enough?"
