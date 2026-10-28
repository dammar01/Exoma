# Skill: analyze
description: Deep analysis via second_agent (1-call). --local: Claude only.
argument-hint: [--local] <topic>

## Trigger
/.analyze <topic>          → 1-call
/.analyze --local <topic>  → Claude directly (skips the proxy)

## Run (1-call)
Every OS (Bash tool): bash '<work_dir>/.workflow/run.sh' analyze --session '<MAIN_SESSION_ID>' --task '<topic>'
<MAIN_SESSION_ID> from [SESSION BINDING], REQUIRED as the 3rd argument (concurrent isolation). Reuse LAST_EXPLORE_RESULT when relevant.
The task touches an external library → name the library; the proxy reads the context7 docs first → findings in external (not a guessed API).
FAILED (ok:false | invalid_evidence | content is a menu/refusal) → HARD GATE:
  STOP → "[PROXY FAILED] <reason>. Continue with /.local? (yes/no)" → WAIT for the user. Do NOT auto-fallback, do NOT reason from garbage.
--local or [LOCAL_MODE]=true → skip the run, /.local flow (not a silent fallback — an explicit user mode).

## Output [ANALYSIS RESULT]
Relay the digest + fill from content. confidence (3 parts) + uncertainties REQUIRED.
source: second_agent (1-call) | claude (local)
confidence: { problem_understanding, root_cause, solution_path } — each low|medium|high — <reason>
findings: <from content, grounded/assumption attribution> | (empty: reason)
implications: <impact> | (empty: reason)
impacted_features: <other features/modules affected — from dependents/reverse deps> [file:line] | (none: reason)
uncertainties: - uncertainty: <N>. <unconfirmed> → how to close: <what> | (none)
               NON-blocking: state the assumption, continue. Needs a user decision → raise it as
               `question: <N>. <question> | <option A> | <option B>` and present it interactively.
Before acting on a finding, read the anchor it rests on yourself: the runtime proves a cited
line exists, not that the claim about it is true. A synthesis written with no cited anchor
opened gets an [ANCHOR CHECK] note at the end of the turn.

## Rules: zero code changes, zero file modifications.
