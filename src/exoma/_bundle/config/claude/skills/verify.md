# Skill: verify
description: 3-step verification — logic, falsification, reality. Depth from commands.verify_mode.

## Trigger
/.verify (automatic after /.execute -y when `commands.auto_verify_after_execute=true`)

## Mode (the runtime reads .workflow/config.json → commands.verify_mode)
delegated (default) → full second_agent verification, Protocol below.
syntax → QUICK: the runtime parse-checks the changed files locally (git diff HEAD + untracked). ZERO tests, ZERO second_agent.
  The [QUICK VERIFY] output is final. pass = the file parses, NOT that the feature works — state this limit.
  Relay not_checked/skipped as they are; do NOT count them as pass.
  A DONE verdict for a runtime/behavior problem is FORBIDDEN; at most "syntax OK, behavior not yet verified" + suggest setting verify_mode=delegated.

## Pick the tests FIRST (verify_mode=delegated — REQUIRED before the run)
The second agent only reads and traces; the runtime runs the tests. You choose them:
1. From the diff + dependents (who calls what changed), pick the tests that cover the change.
   Targeted — related files/suites only, NOT the whole suite. No relevant test (docs, pure
   config) OR the project has no test suite at all → STILL write tests.json with
   `commands: []` + a reason (e.g. "project without a test suite"). That is an explicit request
   for "zero tests", NOT a gap: the runtime only records `runtime: no test requested` and the
   verdict rests on the review. Skipping tests.json = a runtime gap, exit nonzero.
2. Write `.workflow/data/sessions/<MAIN_SESSION_ID>/verify/tests.json`:
   `{"commands": ["<cmd>", ...], "reason": "<why these tests cover the change>"}` (max 5).
3. The runtime only runs commands starting with a prefix in `commands.verify_test_commands`
   (`.workflow/config.json`), without a shell (`&&`/pipe/redirect refused). Outside the allowlist →
   `not_verified`. Empty allowlist → tell the user and suggest the prefixes to add;
   a project without a test suite → there is no prefix to suggest, write `commands: []` (step 1).
Gate: while the verify marker is active (before `.workflow/run verify`) ONLY these pass: `git [-C <root>] diff --name-only|--name-status|--stat [ref] [-- path]`
(may open with `cd <dir inside the root> && `; otherwise no pipe/`&&`/redirect), Read of `.workflow/config.json` + this session's tests.json, and Write of that tests.json. Reading src/test file contents → blocked; the file names from the diff are enough.
No tests.json → verify becomes `incomplete` (gap `tests:`, exit nonzero). An explicit `commands: []` ≠ no tests.json. A failing test → blocking.
tests.json belongs to ONE verify job (the runtime claims it when the job is created). A rerun after a failed verify = a new job → write tests.json again; the old choice is not reused.

## Protocol (verify_mode=delegated)
1. Logic: does it solve the problem? are the assumptions valid? consistent with codebase patterns? → PASS/FAIL + reason
2. Falsification: failure conditions? edge cases? malformed input? → list
3. Reality: the tests you chose are run by the runtime (see above); their results land in `checks_run`
   as `runtime: ...` and, when failing, in `blocking_findings`. Relay as is.

## 3-dimension gate (verify_mode=delegated — REQUIRED)
EVERY finding carries THREE tags:
severity:       critical | high | medium | low
origin:         introduced | regression | pre_existing | unknown
scope_relation: in_scope | out_of_scope

critical = data loss | security | silently wrong results | every command broken
high     = a feature's normal path broken | an existing caller regresses | a contract violated
medium   = edge case | degradation | a defect with a workaround
low      = naming/style/doc drift | hypothetical with no trigger

SEVERITY ALONE DOES NOT DECIDE BLOCKING. Route with the table:
introduced/regression + in_scope     + critical|high → BLOCKING
introduced/regression + out_of_scope + critical|high → BLOCKING (+ scope violation)
introduced/regression + out_of_scope + medium|low    → ESCALATION
unknown               + anything     + critical|high → BLOCKING (fail closed)
pre_existing          + anything     + critical|high → ESCALATION
anything else                                        → NOTE

`unknown` is not an exit: moving off unknown REQUIRES naming the proof (diff/git history/previous version). Cannot → it still blocks.
An ESCALATION does not change the verdict BUT is NOT a note — a real critical/high the user must decide on. Hiding it in notes is FORBIDDEN.
Without file:line + a concrete failure scenario → it MAY NOT be critical/high; lower it to a note + say the evidence is missing.
That rule is about EVIDENCE QUALITY, not about damping systemic problems: a defect spread across many places STAYS critical/high — cite representative file:line + state how widespread it is.
Do NOT raise severity to get attention / lower it to get through.

## Output
[VERIFICATION]
mode: delegated | syntax          ← you fill this in; the second_agent does not know the mode (syntax never goes through it)
verdict: DONE | NEEDS FIX  ← NEEDS FIX ONLY when blocking_findings exist
blocking_findings: - severity|origin|scope_relation — <problem> [file:line] — trigger — impact — fix | (none: what was checked clean)
escalations: - severity|origin|scope_relation — <problem> [file:line] — why it does not block | (none)
notes: - severity|origin|scope_relation — <problem> [file:line] | (none)
checks_run: <what was run/read + result>
not_verified: <what could not be checked + reason> | (none)
confidence: low|medium|high — <reason>
NEEDS FIX → fix the blocking ones first → re-run /.verify. Do NOT output a final before done.
Do NOT drop escalations + notes — show them; the user decides whether to work on them now or later.
The runtime verdict (`meta.verdict`) is written automatically into the session's execution (`runtime/execution.json`): pass | fail | incomplete, and `syntax_only` for syntax mode — parse OK is never recorded as pass.
