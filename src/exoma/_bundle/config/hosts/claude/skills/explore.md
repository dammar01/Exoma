# Skill: explore
description: Codebase evidence via second_agent (1-call). Fallback: {{CMD:local}}.
argument-hint: <hint>

For delegated work, select `explore` using the session-bound `delegate-select` call before preparation. Wait for the original runner, return its result to the main turn, then use `delegate-ack` with its receipt before reading project evidence or synthesizing.

## Trigger
{{CMD:explore}} <hint>

## Run (1-call)
Every OS ({{SHELL_TOOL}}): bash '<work_dir>/.workflow/run.sh' explore{{MAIN_HOST_OPTION}} --session '<MAIN_SESSION_ID>' --request '<generation>' --task '<hint>'
- <MAIN_SESSION_ID> = the value from the [SESSION BINDING] block. REQUIRED (`--session`) — concurrent sessions on the same project need per-session isolation. Absent → the run script's fallback (fine for a single agent).
- Blocks until done. Returns JSON {ok, content, meta, digest}.
- Do not invent commands, do not use check.sh, do not call `exoma` directly.
- No .workflow yet / run script missing → {{CMD:init}} first.
- FAILED (ok:false | invalid_evidence | content is a menu/refusal, no [EVIDENCE]/[DIGEST]) → STOP rule (yours to hold, no hook blocks it):
  STOP → output "[PROXY FAILED] <reason>. Continue with {{CMD:local}}? (yes/no)" → WAIT for the user. Do NOT auto-fallback.

## Output (RELAY mode)
digest present → relay: summary, key_findings, risk_level, recommended_next_action, confidence.
digest absent (fallback) → a full [EXPLORATION RESULT] from content:
  source | session | confidence | entry_points | ownership_hints | related_modules | uncertainties
  (every empty field → show it + the reason). Need detail → open content.

## End
"Continue with {{CMD:plan}}, or is this enough?"
