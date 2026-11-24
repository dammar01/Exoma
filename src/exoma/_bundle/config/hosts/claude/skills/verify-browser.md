# Skill: verify-browser
description: Verification through a real Playwright browser. DELEGATED: draft spec (second_agent) → browser run (runtime player) → review (second_agent). Interview only after the draft reports the project is not configured.
argument-hint: [feature/flow]

For delegated work, select `verify-browser` using the session-bound `delegate-select` call before preparation. Wait for the original runner, return its result to the main turn, then use `delegate-ack` with its receipt before reading project evidence or synthesizing.

## Trigger & Gate
{{CMD:verify-browser}} [<feature/flow to test>]
Different from {{CMD:verify}} (code review): {{CMD:verify-browser}} drives Playwright in the running app.
DELEGATED command: Pre-flight gate applies — until the selected result has been acknowledged, Read/Grep/Glob/Bash/MCP (including reading `config.json`) are FORBIDDEN; writing `request.json` is allowed.
Division of work: second_agent drafts spec (stage 1) and reviews evidence (stage 3); runtime player drives Playwright (stage 2). Neither main_agent nor second_agent opens the browser.
Use `phase: auto`: ready preparation runs player + review in ONE invocation. Never ask "run it or not" or copy `draft.json` into a second request.

## Prerequisites
- Playwright: `exoma install --with-e2e` (check {{CMD:doctor}} → `e2e_readiness`).
- Target app is ALREADY running; the runtime does not start servers.
- Old runner answering `unsupported command` → run {{CMD:upgrade}} first.

## STEP 0 — Draft first + core safety boundaries
The runtime reads `<project>/.workflow/config.json` `e2e` defaults during STEP 3 (main_agent does NOT read `config.json` before dispatch).
- Go straight to STEP 2 (minimal `phase: auto` request) + STEP 3 (single pipeline dispatch). If the user named a `base_url`, set `settings.base_url`; otherwise omit so config/defaults apply (`http://localhost:8000`, headed, retry 2).
- Ready preparation runs end-to-end → skip STEP 1 completely.
- Draft returns `blocked` (`base_url_unreachable` or wrong target) / `invalid` for missing flow info → acknowledge the returned draft first, `config.json` may be read → read `{{SKILL_REF:verify-browser/interview.md}}` for STEP 1 interview, offer to save project defaults in `config.json`, then select `verify-browser` again. Use the new generation for STEP 2–3.
- Relay `meta.e2e.config_warnings` (wrong-type/unknown config knobs are ignored and fall back to defaults).

Non-negotiable safety boundaries (always apply):
- **Config-only policy (`CONFIG_ONLY_SETTINGS`)**: `allow_remote`, `allowed_origins`, `existing_test_command`, `existing_test_allowlist`, `existing_test_timeout_s` belong ONLY in `config.json`, NEVER in `request.json` (`request_invalid`).
- **Origins**: loopback (`localhost`/`127.0.0.1`/`::1`/`*.localhost`) and `.test` pass without `allow_remote`. Real domains require user-written `allow_remote: true` + exact `allowed_origins` in `config.json`; cross-origin navigation always requires `allowed_origins`.
- **Credentials**: ONLY fixed placeholders `${E2E_USER}` / `${E2E_PASS}` (profiles in gitignored `.workflow/e2e/secrets.json`). NEVER write, repeat, or pass literal credentials in chat, `config.json`, `request.json`, or `<task>`. If `meta.e2e.secret_literals` appears, report it as a patched leak. Password `fill` without a placeholder → `spec_invalid`.
- **Writes & destructive guard**: `allow_local_side_effects` (default `true`) allows POST/PUT/PATCH to loopback/`.test` only; non-local writes stay blocked unless `allow_side_effects: true` on private/pinned IP. `DELETE` and any `_method` override ALWAYS require user-approved routes in `.workflow/e2e/permissions.json` (`allowed_destructive_requests`); `blocked_requests` wins over every setting for both writes and navigation. Uninspectable write bodies are unconditionally refused (`uninspectable_body`).

## STEP 2 — Write the automatic request
Write `<project>/.workflow/data/sessions/<MAIN_SESSION_ID>/e2e/request.json` (legacy workspace without `.workflow/data/`: `<project>/.workflow/sessions/<MAIN_SESSION_ID>/e2e/request.json`):
```json
{"version": 1, "phase": "auto", "settings": {"base_url": "http://app.test"}}
```
- `settings` holds ONLY per-run overrides (not a copy of `config.json`); if `config.json` already configures `e2e`, write `{"version": 1, "phase": "auto"}`. Do not supply `scenario`, `existing_tests`, or `spec_notes` in `auto` mode (`draft`/`run` remain available when explicitly needed).
- Valid request keys: `base_url`, `browser`, `headless`, `slow_mo_ms`, `nav_timeout_ms`, `step_timeout_ms`, `idle_timeout_s`, `total_timeout_s`, `probe_max_elements`, `allow_side_effects`, `allow_local_side_effects`, `allowed_read_only_requests`, `allowed_destructive_requests`, `secrets_profile`, `secrets_template_profiles`, `fail_on_console_error`, `max_retries`, `ignore_repeat_brake`, `keep_created_data`, `artifact_max_mb`. Unknown/wrong-type keys or removed `allowed_mutation_paths` → `request_invalid`.

## STEP 3 — Run the pipeline once
Headed (default) → tell the user once before dispatching that a browser will open (do not wait for confirmation).
Every OS ({{SHELL_TOOL}}): `bash '<work_dir>/.workflow/run.sh' verify-browser{{MAIN_HOST_OPTION}} --session '<MAIN_SESSION_ID>' --request '<generation>' --task '<task>'`
{{DELEGATED_RUN}} `<task>` ≤3000 chars: flow + expected claims, `base_url`, credential key names (`${E2E_USER}`...), side effects allowed or not, changed files when known. Do NOT paste code.

Handle result:
- `ok:false` (stage 1 failed) → STOP rule `[PROXY FAILED]`; never invent a scenario yourself.
- Completed `[VERIFICATION]` (`meta.command=verify`, `meta.phase=run`) → relay under Output below. If `meta.e2e.destructive_pending` is non-empty or `meta.e2e.tags` has `ready` entries, read `{{SKILL_REF:verify-browser/interactive-and-tags.md}}`.
- Paused `[E2E DRAFT]` (`meta.phase=draft`):
  - `blocked` (`playwright_missing` | `browser_missing` | `base_url_unreachable` | `spec_invalid` URL policy) → fix environment or run STEP 1 interview (`{{SKILL_REF:verify-browser/interview.md}}`).
  - `env_missing` or `read_only_approval_required` (`read_only_pending`) → read `{{SKILL_REF:verify-browser/interactive-and-tags.md}}` (STEP 4–5).
  - `invalid` → report `errors` + `meta.e2e.diagnosis` (`first_error`, `categories`, `repair`), fix task/context, and rerun `auto` (editing `draft.json` alone never affects `auto`).
- `meta.e2e.knowledge.offered > 0` → report how many entries were offered; actual use needs separate evidence.

## Conditional references (load ONLY on these branches)
<!-- HOST:opencode:START -->
Use the native skill tool with the exact reference name below only after dispatch. The Exoma plugin checks the selected directory and installed content before returning a managed reference. If the reference is missing, denied, or shadowed, report the branch as incomplete and stop. Read the ready-path instructions above directly.
<!-- HOST:opencode:END -->
- Unconfigured/unreachable target needing setup interview (STEP 1) → `{{SKILL_REF:verify-browser/interview.md}}`
- `[BROWSER TEST PLAN]` format, `env_missing` / `read_only_pending` / `destructive_pending` approvals, `permissions.json`, STEP 5 resume, STEP 6 `data-e2e` tags, or browser knowledge/promotion → `{{SKILL_REF:verify-browser/interactive-and-tags.md}}`

## Output (RELAY)
Relay canonical `[VERIFICATION]` (`meta.command=verify`, `meta.invocation=verify-browser`) as is:
`verdict (pass | fail | incomplete) | browser_verdict | reason | cleanup (meta.e2e.cleanup.status) | blocking_findings | not_verified | artifacts (meta.e2e.artifacts)`
- `pass` ONLY from `meta.verdict`; exit nonzero ≠ pass; `ok:true` alone ≠ pass.
- Relay `report.json` `trail` (selector, match count, readiness, request, result) and `requests` (writes with `planned`/extra/`attribution: uncertain`). HTTP 2xx alone ≠ CRUD proof — assertions prove it.
- Cleanup is separate: `failed`/`not_run` → `incomplete` (exit nonzero) even when claims passed; warn that local test data may remain.
- `meta.e2e.diagnosis` (`cause`, `next_step`, `failed`, `fix_hint`): relay all fields verbatim; `reusable: true` saves a `failure_hint`.
- `scenario_error`: scenario bug (bad/ambiguous selector or policy refusal), not retried, not counted toward 3x brake (`meta.e2e.repeat_not_counted`).
- `mutation_blocked`:
  - `blocked_requests` detail → project deny list; offering to remove it from `permissions.json` is FORBIDDEN unless the user asks.
  - `a delete needs its exact endpoint...` → follow `destructive_pending` in `{{SKILL_REF:verify-browser/interactive-and-tags.md}}` (do not suggest `allow_side_effects: true`).
  - `allow_local_side_effects` / non-local / redirect detail → explain guard block (not an app bug) and valid options.
- `repeat_failure`: 3 consecutive failures on the same origin; report bucket + last reason from `meta.e2e.repeat`. For localized `app` failure, materially change action/selector/readiness/prerequisite (`meta.e2e.repeat_released` `by: strategy_changed`) or use `settings.ignore_repeat_brake: true` after an external fix.
- Always check `meta.e2e.coverage_gaps` and `not_verified` even on pass; quote any removed/replaced assertion across retries.
- Retries (`max_retries`, default 2) apply ONLY to environment `incomplete` (`harness_error`, `unknown_origin`, `stuck`, `timeout`, `output_truncated`, `launch_failed`); `fail` with `origin=app` is NEVER retried; 2 identical attempts stop (`stable`). `allow_side_effects: true` disables retries completely (never auto-repeat writes on timeout). State attempt count (`meta.e2e.attempts`); `passed only on attempt N` is `incomplete`, NOT `pass`.
- Relay `meta.e2e.knowledge` (`added | confirmed | weakened | retired`).
- Screenshots/traces are omitted when `${ENV}` is used (unscrubbable); HTML omitted if a secret < 4 chars (`meta.e2e.secrets.unscrubbable`).
- Origin interpretation: `fail` `origin=app` → app bug (offer {{CMD:analyze}}/fix); `scenario` → fix scenario; `harness`/`unknown` → environment or indistinguishable.

## Limits
- `config.json` only sets defaults; no knob weakens origin/write policy. Real domains still require `allow_remote` + exact `allowed_origins` in `config.json`.
- Known gaps: private hosts controlled by others on the same network, GET mutations, WebSockets, and service workers are outside the guard; resolver pinning is Chromium-only.
- Credentials never pass through chat, request, prompts, or artifacts. When the second_agent provider is `codex`, warn that `secrets.json` read boundary is `NOT_ENFORCEABLE`.
- One session = one request; sessions share only `config.json` `e2e`. Metrics log `e2e_run` / `e2e_draft` rows in `.workflow/data/quality.jsonl`.
