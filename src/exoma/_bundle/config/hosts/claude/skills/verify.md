# Skill: verify
description: 3-step verification — logic, falsification, reality. Depth from commands.verify_mode.

For delegated work, select `verify` using the session-bound `delegate-select` call before preparation. Wait for the original runner, return its result to the main turn, then use `delegate-ack` with its receipt before reading project evidence or synthesizing.

## Trigger
{{CMD:verify}} (invoked after {{CMD:execute}} -y when `execute-end` returns `meta.next_step.action=verify_prepare`)

## Mode (from `verify-prepare` → `meta.policy.verify_mode`)
- `delegated` (default) → full second_agent verification (Protocol below).
- `syntax` → `[QUICK VERIFY]` local parse check only (zero tests, zero second_agent). Final output: relay `not_checked`/`skipped` honestly; NEVER emit DONE for runtime/behavior changes — at most `"syntax OK, behavior not yet verified"` + suggest `verify_mode=delegated`.

## Prepare FIRST (local runtime, before selecting tests)
Call `bash '<work_dir>/.workflow/run.sh' verify-prepare{{MAIN_HOST_OPTION}} --session '<MAIN_SESSION_ID>'`.
Run this as ONE plain standalone command — no pipe (`|`), redirect (`>`), chaining (`;`, `&&`), `python`, `head`, or double quotes. Its stdout is already a compact single-line JSON object small enough to inspect directly; piping or filtering it is refused by the pre-flight gate.
Read `meta.policy`, `meta.request`, `meta.config_warnings`, and `meta.next_step`; do NOT read `config.json` or `tests.json` directly. Preparation runs no provider or tests, consumes no request, and leaves the delegated marker armed. Invoke verify afterwards; the runtime rechecks current policy on dispatch.
- `policy.verify_mode=syntax` → invoke verify directly; do not request tests.
- `delegated` → missing request needs test selection; invalid needs correction; valid pending request may be kept if it still covers the change.

## Pick the tests (verify_mode=delegated — REQUIRED before the run)
The second agent only reads/traces; the runtime runs tests. You select them:
1. From diff + dependents, choose targeted, integration, or full tests covering the change. No relevant behavior test (e.g. docs) → STILL write `tests.json` with `commands: []` + a reason (explicit zero-test request, not a gap; omitting `tests.json` → `incomplete`, exit nonzero).
2. Write `meta.request.path` (normally `.workflow/data/sessions/<MAIN_SESSION_ID>/verify/tests.json`):
   `{"commands": ["<cmd>", ...], "reason": "<why these tests cover the change>"}` (max 5).
3. Commands must start with an argv prefix from `meta.policy.test_command_prefixes` (no shell/`&&`/pipe/redirect; outside allowlist → `not_verified`). Empty allowlist → notify user and suggest prefixes (or `commands: []` if project has no test suite).
While the verify marker is armed, select tests from existing evidence, `git [-C <root>] diff --name-only|--name-status|--stat [ref] [-- path]` (optional `cd <dir inside root> && `; no pipe/`&&`/redirect), the plain standalone `verify-prepare` runner call, and Write of `tests.json`; delegate source inspection.
Already ran them via `run.sh test` after the last edit? STILL list the EXACT same command string in `tests.json`: unchanged reuse identity (Git tree, `config.json`, `verify_reuse_env`, `verify_reuse_files`) reuses the recorded pass (`... reused, not rerun` in `checks_run`; filtered commands never stand in for full suites, and any command running invalidates reuse for later commands). Relay reused lines as reused.
`meta.changed_since_execute` non-empty → name post-execute files and scope limits. Each `tests.json` belongs to ONE verify job; write it again on rerun.

With `meta.selection_version=1`, add `selection_input` from preparation and a `selection`
object to the request. Start with `meta.execution_selection`; for standalone verify use
`{"version":1,"execution_id":null,"tier":"targeted","reason":"...","obligations":[]}`
and fill the applicable tier, reason, and obligations. Always use the returned execution ID
(including null). Each obligation has `id`, `behavior`, `due` (verify/release) and
`commands` (all exact commands required). Preserve existing obligations, add newly
identified risks, and select runnable commands in the top-level list. Where no test
exists for a required behavior, leave its command list empty and explain the gap.
Use tier `none` with a reason and no commands when appropriate. Review a valid pending
request with the current diff before replacing it. If the input fingerprint changed,
prepare again and reassess the selection. On older runtimes relay any outstanding coverage.
Relay `meta.runtime_tests.coverage`, including release-only work. In syntax mode relay
`meta.unchecked_selection` if present. A known removed assertion remains open until its
replacement is tested.

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

SEVERITY ALONE DOES NOT DECIDE BLOCKING. The runtime and rendered prompts share this policy:
{{VERIFICATION_ROUTING}}

`unknown` is not an exit: moving off unknown REQUIRES naming the proof (diff/git history/previous version). Cannot → it still blocks.
An ESCALATION does not change the verdict BUT is NOT a note — a real critical/high the user must decide on. Hiding it in notes is FORBIDDEN.
Without file:line + a concrete failure scenario → it MAY NOT be critical/high; lower it to a note + say the evidence is missing.
That rule is about EVIDENCE QUALITY, not about damping systemic problems: a defect spread across many places STAYS critical/high — cite representative file:line + state how widespread it is.
Do NOT raise severity to get attention / lower it to get through.

## Output
[VERIFICATION]
mode: delegated | syntax          ← you fill this in; the second_agent does not know the mode (syntax never goes through it)
verdict: DONE | NEEDS FIX  ← NEEDS FIX ONLY when blocking_findings exist
blocking_findings: - severity|origin|scope_relation — <problem> [file:line] — trigger — impact — fix | - none
escalations: - severity|origin|scope_relation — <problem> [file:line] — why it does not block | (none)
notes: - severity|origin|scope_relation — <problem> [file:line] | (none)
checks_run: <what was run/read + result>
not_verified: <what could not be checked + reason> | (none)
confidence: low|medium|high — <reason>
For an empty section write exactly `- none`. Describe what was checked clean in `checks_run`.
Legacy neutral sentinel explanations remain supported only by a bounded grammar; arbitrary
parenthetical prose or continuation findings are meaningful and cannot be discarded.
NEEDS FIX → fix the blocking ones first → re-run {{CMD:verify}}. Do NOT output a final before done.
Do NOT drop escalations + notes — show them; the user decides whether to work on them now or later.
The runtime verdict (`meta.verdict`) is written automatically into the session's execution (`runtime/execution.json`): pass | fail | incomplete, and `syntax_only` for syntax mode — parse OK is never recorded as pass.
