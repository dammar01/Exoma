# Skill: execute
description: Controlled implementation with an approval gate.
argument-hint: [-y]

## Trigger
/.execute -y → PROCEED | /.execute → GATE only

## Gate
No active plan (LAST_PLAN_RESULT with decision=proceed) → zero edits. Output one line
"No active plan yet — run /.plan first?" → STOP. This also applies to requests like
"fix/add ..." that map to execute.
No -y → output [EXECUTION SCOPE] → "Add -y to confirm" → STOP.
Approval = ONLY `/.execute -y` from the USER. An execute sentence without `-y` falls to the gate above.
AskUserQuestion answers to the plan's open_questions are NOT approval — they are input for
updating the plan.

## Pre-Execution
[EXECUTION SCOPE] allowed: <files> | forbidden: <files> | reason: <limits>
With `-y`: BEFORE the first edit, open the execution in the runtime:
  bash '<work_dir>/.workflow/run.sh' execute-begin --session '<MAIN_SESSION_ID>'
The runtime checks for a recorded plan (`plan-record`, decision=proceed) AND the `-y` token from
the user's own prompt (written by the `intent-gate-set` hook, valid for 15 minutes, single use).
Refused (`execute_no_plan` | `execute_plan_not_proceed` | `execute_not_approved`) → relay
`next_action`, zero edits, STOP. Never write the token yourself: that forges the user's approval.

## During
Touch ONLY allowed. Need something forbidden → STOP → report the conflict → ask for instructions.
allowed is only inside the session's project root. A path in another project → STOP, ask for
explicit approval for that path. The rule is the same for every write path; what enforces it differs:
- Edit/Write → the `execution-gate` hook blocks: outside the plan's `files` while an execution is
  active; outside the root (other than temp and ~/.claude) unless the path is in
  `policies.write_allowed_outside_root`.
- Bash/script patches → NOT checked by a hook. Nothing blocks them; only you enforce the rule.

## Tests after the edits
Run the project's tests through the runtime, not a plain shell call:
  bash '<work_dir>/.workflow/run.sh' test --session '<MAIN_SESSION_ID>' --task '<allowlisted command>'
(one command, or a JSON list of up to 5; same allowlist as /.verify: `commands.verify_test_commands`).
The runtime records each pass with a reuse identity: the Git-visible tree's content as Git stores it,
`.workflow/config.json`, env vars matching `commands.verify_reuse_env`, and ignored files matching
`commands.verify_reuse_files` (default `.env`, `.env.*`). A later /.verify that requests the EXACT same
command string while that identity is unchanged reuses the pass instead of running it again; once a
command in that verify actually runs, the commands after it run too (it may have written the tree).
A change to those inputs after the run → the pass no longer counts; run them again after the LAST edit.
Not covered: installed dependencies/venv, submodule contents, ignored inputs nobody listed, and edits
Git's own conversions undo (`text`/`eol` attributes, `core.autocrlf`, clean filters: LF→CRLF in a
normalized file keeps the identity) — a test that depends on them must be listed in
`verify_reuse_files`, or simply not run through `run.sh test`.
A test run is evidence, not verification: `verification` stays `not_run` until /.verify.

## Post (read .workflow/config.json → commands.auto_verify_after_execute)
Close the execution first: bash '<work_dir>/.workflow/run.sh' execute-end --session '<MAIN_SESSION_ID>'
(status `implemented`, verification `not_run`; a finished /.verify writes its verdict here).
Its `meta.changed_files` lists what changed since execute-begin (Git tree snapshots, so edits the tree
already carried are not counted) and `meta.outside_scope` those outside the plan's `files` — Bash
writes included. Non-empty `outside_scope` → name each file in [EXECUTION RESULT] and say why it
changed; never omit it. `null` = no Git snapshot: changed files unknown, say so.
[EXECUTION RESULT] files_changed | confidence | uncertainties | verification | status
true  → auto-trigger /.verify. status: done|partial|blocked. Do NOT declare done before verify finishes.
false (default) → do NOT auto-run /.verify (avoid heavy tests nobody asked for). REQUIRED:
  verification: not_run
  status: implemented | partial | blocked  ← "done" is FORBIDDEN
  then offer "/.verify now?"
"implemented" ≠ "done". Without verification you DO NOT KNOW it works — do not say you do.

### Read this key EVERY time, never from memory
`commands.auto_verify_after_execute` is read from `.workflow/config.json` at the end of EVERY
/.execute. Key ABSENT = default `false` (config.json holds overrides only). A value from
another session, another project, or your memory does NOT apply.
`true` → the chain to /.verify is part of /.execute, not an optional step after it.
Stopping before verify finishes = an unfinished /.execute, whatever the diff contains.

### Words FORBIDDEN while verification: not_run
"done" | "finished" | "working" | "succeeded" | "fixed" | "works now" | "safe now"
| "tests pass" (when you did not run them) | ✅ as a completion marker — in any language.
ALLOWED: "implemented, not yet verified". The difference is not style — the user
decides the next step from that word.

### What the runtime enforces, and what it does not
Runtime (`core/runtime/execution.py`): a proceed plan + the user's `-y` to open an
execution; scope and root for Edit/Write (`execution-gate` hook, blocks); `git commit` only
in a turn where the user asked to commit. The `status-guard` hook (Stop) WARNS when the
`[EXECUTION RESULT]` fields of your reply claim more than not_run — a `status` outside
implemented|partial|blocked, or a `verification` other than not_run. The hook does NOT read
prose, so the forbidden words above are entirely your responsibility.
`policies.enforcement` = hybrid (default) | warn | off.
