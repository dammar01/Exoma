# Skill: execute
description: Controlled implementation with an approval gate.
argument-hint: [-y]

## Trigger
{{CMD:execute}} -y → PROCEED | {{CMD:execute}} → GATE only

## Gate
No active plan (LAST_PLAN_RESULT with decision=proceed) → zero edits. Output one line
"No active plan yet — run {{CMD:plan}} first?" → STOP. This also applies to requests like
"fix/add ..." that map to execute.
No -y → output [EXECUTION SCOPE] → "Add -y to confirm" → STOP.
Approval = ONLY `{{CMD:execute}} -y` from the USER. An execute sentence without `-y` falls to the gate above.
{{QUESTION_TOOL}} answers to the plan's open_questions are NOT approval — they are input for
updating the plan.

## Pre-Execution
[EXECUTION SCOPE] allowed: <files> | forbidden: <files> | reason: <limits>
With `-y`: BEFORE the first edit, open the execution in the runtime:
  bash '<work_dir>/.workflow/run.sh' execute-begin{{MAIN_HOST_OPTION}} --session '<MAIN_SESSION_ID>'
The runtime checks for a recorded plan (`plan-record`, decision=proceed) AND the `-y` token from
the user's own prompt (written by the host prompt hook, valid for 15 minutes, single use).
Refused (`execute_no_plan` | `execute_plan_not_proceed` | `execute_not_approved`) → relay
`next_action`, zero edits, STOP. Never write the token yourself: that forges the user's approval.

## During
Touch ONLY allowed. Need something forbidden → STOP → report the conflict → ask for instructions.
allowed is only inside the session's project root. A path in another project → STOP, ask for
explicit approval for that path. The rule is the same for every write path; what enforces it differs:
- Edit/Write → the host write gate blocks: outside the plan's `files` while an execution is
  active; outside the root (other than temp and {{HOST_CONFIG}}) unless the path is in
  `policies.write_allowed_outside_root`.
- Bash/script patches → NOT checked by a hook. Nothing blocks them; only you enforce the rule.

## Tests after the edits
Select the smallest tier that covers the changed behavior and its callers.
Use `targeted` for a bounded change, `integration` when a value or action crosses a component
boundary, and `full` when affected callers cannot be bounded or project policy requires it.
Use `none` with a reason for a change requiring no behavior test. When a behavior needs a
test and none exists, record that behavior as an open obligation. Identify each obligation
by a stable ID, the behavior, when it is due (verify or release), and all exact commands
needed as evidence. Include a known removed assertion until equivalent coverage is established.
Run the useful early tests now and report what still needs checking at verify.
Run the project's tests through the runtime, not a plain shell call:
  bash '<work_dir>/.workflow/run.sh' test{{MAIN_HOST_OPTION}} --session '<MAIN_SESSION_ID>' --task '<allowlisted command>'
(one command, or a JSON list of up to 5; same allowlist as {{CMD:verify}}: `commands.verify_test_commands`).
The runtime records each pass with a reuse identity: the Git-visible tree's content as Git stores it,
`.workflow/config.json`, env vars matching `commands.verify_reuse_env`, and ignored files matching
`commands.verify_reuse_files` (default `.env`, `.env.*`). A later {{CMD:verify}} that requests the EXACT same
command string while that identity is unchanged reuses the pass instead of running it again; once a
command in that verify actually runs, the commands after it run too (it may have written the tree).
A change to those inputs after the run → the pass no longer counts; run them again after the LAST edit.
Not covered: installed dependencies/venv, submodule contents, ignored inputs nobody listed, and edits
Git's own conversions undo (`text`/`eol` attributes, `core.autocrlf`, clean filters: LF→CRLF in a
normalized file keeps the identity) — a test that depends on them must be listed in
`verify_reuse_files`, or simply not run through `run.sh test`.
A test run is evidence, not verification: `verification` stays `not_run` until {{CMD:verify}}.

## Post (runtime policy and next step)
Close execution first: `bash '<work_dir>/.workflow/run.sh' execute-end{{MAIN_HOST_OPTION}} --session '<MAIN_SESSION_ID>'`
- When `execute-begin` returned `meta.selection_version=1`, pass `--task-file '<selection-json-path>'` (under this session's runtime dir):
  `{"version":1,"execution_id":"<from execute-begin>","tier":"integration","reason":"...","obligations":[{"id":"...","behavior":"...","due":"verify","commands":["<exact command>"]}]}` (empty obligation commands record a gap; on older runtimes omit `--task-file` and list outstanding behaviors explicitly).
- `meta.changed_files` lists post-`execute-begin` Git changes; `meta.outside_scope` lists files changed outside plan `files` (including shell writes) — name each one and why it changed (`null` = no Git snapshot, say so).
- Use fresh `meta.policy` and `meta.next_step` from THIS `execute-end` response (never cached or read from `config.json`; absent/invalid `auto_verify_after_execute` defaults to `false` with warning). Report `meta.config_warnings`. `next_step` has `automatic: false`: runtime has not started verification.

Output `[EXECUTION RESULT] files_changed | confidence | uncertainties | verification | status`:
- `next_step.action=verify_prepare` (`auto_verify_after_execute=true`) → follow the verify skill; it owns the single local preparation step and the subsequent {{CMD:verify}} invocation. This chain is PART OF {{CMD:execute}} — stopping before verify finishes leaves {{CMD:execute}} unfinished. Status once verified: `done|partial|blocked`.
- `next_step.action=offer_verify` (`false`, default) → do NOT auto-run {{CMD:verify}}. REQUIRED: `verification: not_run`, `status: implemented | partial | blocked`, then offer `"{{CMD:verify}} now?"`.

### Words FORBIDDEN while `verification: not_run`
`"done"` | `"finished"` | `"working"` | `"succeeded"` | `"fixed"` | `"works now"` | `"safe now"` | `"tests pass"` (when not run) | ✅ as completion marker — in any language. Use `"implemented, not yet verified"`.

### Enforcement (`policies.enforcement` = hybrid|warn|off)
Runtime checks proceed plan + user `-y` token (`execute-begin`), host write gate blocks out-of-scope/out-of-root Edit/Write, `git commit` requires explicit commit request, and host completion guard warns on `[EXECUTION RESULT]` fields claiming more than `not_run` (prose wording stays your responsibility).
