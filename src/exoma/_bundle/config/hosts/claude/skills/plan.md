# Skill: plan
description: Structured planning — evidence (1-call) + main_agent reasoning. Claim attribution + a mechanical decision gate.
argument-hint: <task>

## Trigger
/.plan <task>

## STEP 1 — Evidence
Reuse LAST_EXPLORE_RESULT when it is in context. Otherwise 1-call:
  Every OS (Bash tool): bash '<work_dir>/.workflow/run.sh' plan --session '<MAIN_SESSION_ID>' --task '<task>'
<MAIN_SESSION_ID> from [SESSION BINDING], REQUIRED as `--session` (concurrent isolation).
- FAILED (ok:false | invalid_evidence | content is a menu/refusal) → HARD GATE:
  STOP → "[PROXY FAILED] <reason>. Continue the plan via /.local or without evidence? (yes/no)" → WAIT. Do NOT auto-fallback.
- [LOCAL_MODE]=true → skip the run, use the /.local flow as evidence.
- The proxy is scope-bounded to the files you name. Cross-system concerns (balances, UX, perf, security) are NOT seen by the proxy unless you inject them into the task. Inject them explicitly OR mark them not_investigated. Connecting them is your job, not the proxy's.
- Instruct the proxy to trace REVERSE deps (who consumes/calls the change target) → `dependents`. Every other affected feature/module MUST go into `risks` with [proxy:file:line] proof of the coupling.
- The task touches an external library/framework → NAME the library in the task; the proxy MUST read the docs through context7 FIRST (version + official API) → findings in `external`. Never let a guessed library API into the plan.

## STEP 2 — Output [PLAN]
EVERY claim in assumptions/steps/dependencies/risks MUST carry a source:
  [proxy:file:line] proven | [main_agent-inference] your conclusion | [user-provided] | [PLACEHOLDER-needs-calibration] numbers/metrics without a basis.
Do NOT present guesses (numbers, dependencies, regressions) as unlabeled fact. No evidence yet → ask for new evidence OR label it. Do not quietly raise confidence when pushed.
Read the anchor a step rests on yourself before committing to it: the runtime proves a cited
line exists, not that the claim about it is true. A [PLAN] written with no cited anchor opened
gets an [ANCHOR CHECK] note at the end of the turn.

[PLAN]
task:            <restatement>
evidence_source: second_agent (1-call) | graphify+claude (local) | none
assumptions:     - <statement + attribution> | (none: reason)
open_questions:  - question: <N>. <an architecture/implementation decision ONLY the user can make; BLOCKING> | <option A> :: <description A> | <option B> :: <description B>   | (none: reason)
                 Numbered, options separated by " | ", an optional description after " :: ". The answer is a choice → options REQUIRED.
                 Present them as interactive questions, one per question — do not make the user read raw structure.
                 Renderer = the AskUserQuestion tool. Its limits are hard; break one = the call is refused:
                   - MAX 4 questions per call, 2-4 options per question. More → split into the next call, do not cram.
                   - Each question's `header` MAX 12 characters. Long text goes in `question`, not the header.
                   - Multi-choice → `multiSelect: true`. Single choice → false. Do not use multiSelect for a yes/no question.
                   - The " :: " part becomes the option's `description`; without one the user guesses what the label means.
                   - The user always gets a free "Other" option — do not add an "other" option yourself.
                   - AskUserQuestion is NOT available in subagents. Render in the main thread; do NOT delegate asking the user to a Task/subagent.
resolvable_uncertainties: - uncertainty: <N>. <can be closed> → how: <what read/grep/explore> | (none)
                 NON-blocking. Do NOT ask the user — state the assumption, continue.
steps:           1. <concrete + attribution> 2. ...
dependencies:    - A→B [proof:file:line] | A→B [ASSUMPTION-not-verified] | (none)
files_affected:  <list>
risks:           - <breakage/side effect + other affected features (blast radius from dependents) + attribution> | (none: reason)
confidence:
  problem_understanding: low|medium|high — <reason>
  root_cause:            low|medium|high — <reason>
  solution_path:         low|medium|high — <reason>
decision:        proceed | clarify | re-explore
  MECHANICAL: open_questions exist OR solution_path<high OR a heavy critical path rests on [main_agent-inference] → clarify (proceed/offering execute FORBIDDEN). Low root_cause → re-explore. Otherwise → proceed.

## STEP 2b — Output [OPTIONS] (REQUIRED, after [PLAN])
The plan above = ONE path. Present the alternatives; do not hide choices in your head.

[OPTIONS]
Option A — <name>  (= the plan above)
  plus:   <a real advantage>
  minus:  <a real cost/risk>
  effort: small|medium|large · main risk: <one thing>
  attribution: [proxy:file:line] | [main_agent-inference]
Option B — <a DIFFERENT approach> (same)
Option C — <optional, same>
recommendation: <ONE option> — <a short reason, naming the trade-off you accept>

BOUNDED (break it = invalid output):
- MAX 3 options. At most 5 lines per option.
- An option is VALID only when it differs in ARCHITECTURE, DEPENDENCY, or overall IMPLEMENTATION DIRECTION. Cosmetic/order/naming differences are NOT options. A variant / subset / parametric version of the same plan is NOT an option either (e.g. "full plan" vs "half the plan", "cap 3000 vs 4000" = one plan, not two directions). No real fork in direction → a single option (see below); do NOT split the plan into fake options to fill the block.
- Must be WITHIN the task's scope. Do NOT propose a rewrite, a stack change, or an architecture change when that is not the task. An option outside the scope = scope creep, not a choice.
- Attribution as strict as in [PLAN]. An option without an evidence basis is still labeled [main_agent-inference].
- `minus` MUST be filled honestly, including for the option you recommend. An option without a minus = you have not thought it through.
- An option the evidence contradicts MUST be marked ❌ + name the evidence against it. Do not present it as an equal.
- recommendation MUST be ONE. Cannot choose → that is an open_question, not an option. Raise it into [PLAN].
- Only one sensible option → write "Option A only — <why no bounded alternative exists>". Do not invent an option B to fill the format.

## STEP 3
You MUST try to close resolvable_uncertainties FIRST before asking the user; leave only open_questions for the user.
Record the plan in the runtime — every time a [PLAN] is issued or changes (including after open_questions answers):
  write `<work_dir>/.workflow/data/sessions/<MAIN_SESSION_ID>/runtime/plan.input.json`:
  {"task", "decision", "confidence": {problem_understanding, root_cause, solution_path},
   "open_questions": [...], "options": [{"name", "plus", "minus", "recommended"}], "files": [<execution scope>]}
  then: bash '<work_dir>/.workflow/run.sh' plan-record --session '<MAIN_SESSION_ID>' --task '<that path>'
The runtime REFUSES a `proceed` that still has open_questions, solution_path < high, empty
`files`, an option without `minus`, or a recommendation count ≠ one (`plan_invalid`). Refused → it is not
proceed: fix the plan or write decision clarify. /.execute -y only opens a recorded plan.
decision=proceed → "Agree? Run /.execute -y". clarify → ask the open_questions. Do NOT auto-execute.
open_questions answers arrive → update [PLAN] (assumptions, confidence, decision) → new decision
proceed → "Agree? Run /.execute -y" → STOP. Those answers are NOT execution approval;
zero edits until the user gives /.execute -y.
The user picks a non-recommended option → CARRY OUT their choice, do not argue it again. You already named its minus; the decision is theirs.
