# Revision Plan: Playwright-Based `verify_mode=e2e`

Status: **Phases 1–4 implemented (1 and 4 verified by `/.verify`; 2 and 3 pass the local suite + Chromium smoke, not yet `/.verify`); Phase 5 tooling exists; Phase 0/5 evaluation awaits a real project dataset** — details in §1.2 and §1.3  
Date: 2026-09-14 (status last updated 2026-09-14, after implementation batches 1–8)  
Project: `dammar01/agent-workflow`

> **Historical note (2026-09-15).** This document records the initial design based on `verify_mode=e2e` and the `commands.e2e` block. The final implementation differs: browser verification is an explicit `/.verify-browser` command that **does not read `config.json`** — all per-run settings live in the session request, and `e2e` is not a `verify_mode` value. The contract in force is in `docs/runtime-contracts.md` §/.verify-browser, including the `allow_side_effects` + `allowed_mutation_paths` write guard that does not exist in this plan. The mentions of `verify_mode=e2e`, `commands.e2e.*`, and `meta.verify_mode = "e2e"` below are left as a decision trail, not as instructions.

## 1. Decision summary

Add `verify_mode=e2e` as a runtime behavior verification layer for web applications. This mode does not use the model as a screenshot-driven browser operator. The model analyzes the codebase and builds a test contract, while Playwright runs the interactions and assertions locally and deterministically.

Core principle:

> **Code-first, browser-second, visual-last.**

Workflow order:

1. Determine the change scope from Git and the user's task.
2. Explore the affected parts of the codebase.
3. Derive the behavior to be proven into `verification claims`.
4. Use the project's existing E2E tests if available and relevant.
5. If there is a coverage gap, generate declarative scenario JSON.
6. Run the scenario through the local Playwright player.
7. Evaluate DOM, URL, browser errors, and network evidence.
8. Send a compact report to the hybrid reviewer.
9. Open screenshots or traces only if structured evidence is insufficient.

Implementation is done as an MVP first. Complex self-healing, application boot, automated visual reasoning, and cross-browser support are deferred until the MVP is shown to deliver value.

### 1.1 Architecture decisions (2026-09-14)

The following three decisions were made after this plan was analyzed against the current verify codebase. These decisions bind every section below.

| # | Decision | Reason |
|---|---|---|
| D1 | **Three-stage orchestration in the Python runtime.** Stage 1: a delegated call to second_agent produces claims + scenario JSON (`[E2E SPEC]`). Stage 2: local Playwright player. Stage 3: delegated hybrid review reads the compact `[E2E EVIDENCE]`, then the normalizer combines the browser report and the review result into a canonical `[VERIFICATION]`. Stages 1 and 3 do **not** re-call `Executor.execute("verify")` with the config as-is — see the dispatch rules in §5.1. | The local `Executor.execute()` branch for `syntax` mode early-returns before the router/provider — there is no place for the model to form claims. Placing the claim builder in the runtime makes the whole lifecycle testable with a fake player, rather than depending on main agent prompt discipline. |
| D2 | **E2E results are normalized into the existing `[VERIFICATION]` contract.** There is no new verdict bypass path like `quick` mode. | `_finalize_verify_result`, the continuation predicate, and the invalid-evidence guard all validate verify output as a delegated contract; the only exception is quick metadata (`meta.mode == "quick"` or `meta.quick_verify`). Raw E2E JSONL/report without normalization would be judged `INCOMPLETE` or trigger continuation. One verdict parser means zero drift and an automatically correct exit code. |
| D3 | **Playwright as an optional extra with lazy import.** The runtime stays stdlib-only; `core/evidence/e2e/` imports Playwright when used; absent → `incomplete` with reason `playwright_missing`. The dependency invariant test only allows guarded imports in the e2e package. Installation: pinned `requirements-e2e.txt` + `python install.py --apply --with-e2e` (pip, then `playwright install chromium`). | `tests/checks/deps.py` enforces a stdlib-only runtime, and the repo has no `requirements.txt` at all. Adding Playwright as a required dependency burdens every installation that does not use E2E; an opt-in flag separates it without changing the default installation. |

Plan risk estimate: before these decisions `high` (collision with the delegated contract + no home for the claim builder); after all three are in, `medium` (remaining risk: new player supervisor, browser dependency, timeout calibration).

Additional decision during implementation (Q1): stage 1 and stage 3 call `Executor._run_delegated()` / `_build_delegated_prompt()` extracted from `execute()`, not `execute()` again. See §5.1.

Additional implementation-batch decision: packaging via `requirements-e2e.txt` + `--with-e2e`, which also downloads Chromium; the project's existing tests are only run via a user-written `commands.e2e.existing_test_command` (§8, §18); the Phase 0/5 evaluation dataset is taken from real projects.

### 1.2 Implementation status

Position as of 2026-09-14. **Verified** = passes delegated `/.verify` and the local suite. **Implemented** = code and tests exist, `python tests/run.py` PASS, `python -m tools.sim.sim_flows` 34/34, real Chromium smoke PASS on Windows (`WORKFLOW_E2E_SMOKE=1`), but has not yet passed `/.verify`. None of the changes are committed yet.

| Phase | Status | Evidence | Notes |
|---|---|---|---|
| 0 — Baseline and requirements validation | **not yet (awaiting data)** | tooling: dataset label `WORKFLOW_E2E_LABEL`, delegated verify baseline in `report` | Needs 3–5 real web changes, a non-production `base_url`, test accounts via `${ENV}`. Cannot be done without a target project. |
| 1 — Contract without browser | **done, verified** | `e2e_spec.py`, `e2e_normalize.py`, `e2e_supervisor.py`, `e2e_routing.py`; sim S22–S22c; re-verify batch 2 DONE | — |
| 2 — Playwright MVP | **implemented** | batches 1–5: policy `spec.py`, `browser.py`, artifact policy, `e2e_browser.py`, `e2e_redact.py`, `e2e_doctor.py`, `installer-e2e`, `e2e_smoke.py` (10 real Chromium cases) | Exit criteria (pass, app failure, harness failure correctly classified) met in smoke. Linux, Firefox, and WebKit not yet tested. |
| 3 — Stage 1: claim builder | **implemented** | batch 6: `source_refs` grounding, `last_spec.json` replay, existing tests via config, test that source is not forwarded; `e2e_existing_tests.py`, sim S22d | Existing tests have only been tested with a substitute Python runner, not real `npx playwright test`. |
| 4 — Stage 3: hybrid review | **done, verified** | `[E2E EVIDENCE]`; fail-closed normalizer; exhaustive agreement between normalizer and validator | The evidence block now also contains probes, existing tests, and the artifact list (batches 4 and 6). |
| 5 — MVP evaluation | **tooling implemented; evaluation not yet** | batch 7: `kind: e2e_run` rows in `quality.jsonl`, `telemetry.e2e_metrics` in `main.py --command report`; `e2e_metrics.py` | Evaluation numbers depend on the Phase 0 dataset. The visual browser loop baseline is neither measured nor estimated. |

### 1.3 Gaps

Gaps G1–G14 from the previous revision and the batches that close them:

| # | Gap | Status | Closed by |
|---|---|---|---|
| G1 | Real browser player, observer, `page_stable` | closed | batch 3 (`browser.py`), batch 5 (smoke) |
| G2 | Bounded DOM probe | partial | batches 3 and 4: `probe` action, automatic probe on selector miss, summary in evidence. Probe cache (§11) not yet present. |
| G3 | Screenshot/HTML/trace policy | closed | batch 4 |
| G4 | Existing tests run | closed (opt-in) | batch 6, via `existing_test_command` |
| G5 | Navigation origin validation | closed | batch 1 (validator), batch 3 (runtime route guard) |
| G6 | Destructive action metadata | closed | batch 1 (`side_effect` + `allow_side_effects`) |
| G7 | Selector candidates | closed | batch 1 (schema), batch 3 (execution) |
| G8 | `source_refs` grounding | closed | batch 1 (format), batch 6 (file and line exist) |
| G9 | Config not yet consumed | closed | batches 3 and 4 |
| G10 | Stage 1 spec replay | **open** — not in the code (`runner.py` records `replay_used: false`, without `last_spec.json`/`commands.e2e.replay`). Partial substitute since v3.6.0: browser knowledge (`core/evidence/e2e/knowledge.py`) offers proven flows to the next draft, not spec replay | batch 6 (initial claim, corrected) |
| G11 | Size-based artifact retention | closed | batch 4 (`artifact_max_mb`) |
| G12 | e2e evaluation metrics | tooling closed, data not yet | batch 7 |
| G13 | Tokens compared with the visual browser loop | partial | batch 7: tokens per run and delegated verify baseline; visual loop baseline not yet measured |
| G14 | §22 tests not yet present | closed | batch 4 (retention), batch 5 (smoke), batch 6 (existing test reuse, source not forwarded) |

Still open:

- Phase 0 dataset and Phase 5 evaluation on real projects (needs user input).
- Visual browser loop baseline for acceptance #13.
- Probe cache (§11); bounded target outer HTML on successful steps (§12).
- Smoke on Linux; Firefox and WebKit; a CI job that installs the extra and sets `WORKFLOW_E2E_SMOKE=1`.
- Existing test command with a real runner (e.g. `npx playwright test`).
- Batches 1–7 have not yet passed `/.verify`; none of the changes are committed; the skills installed in `~/.claude/` have not been upgraded from `dist/` (`python install.py --apply`).
- Self-healing (§15.2) is deferred as planned, not debt.

## 2. Problem to be solved

A verification mode that reads code can judge whether an implementation looks correct, but does not yet prove that the behavior actually happens when the application runs.

Examples of problems that can slip past syntax checks and code review:

- wrong route or redirect;
- button cannot be clicked because of state or an overlay;
- JavaScript runtime exception;
- API request returns HTTP 5xx;
- UI state not updated after a successful response;
- form does not submit the expected values;
- frontend and backend integration not wired up;
- build runs, but the user workflow fails.

The goal of E2E mode is not to replace syntax tests, unit tests, or delegated review. This mode adds runtime evidence when a change actually touches web application behavior.

## 3. Goals and non-goals

### 3.1 MVP goals

- Prove one or more important behaviors through the browser.
- Produce accountable `pass`, `fail`, or `incomplete` results.
- Keep token usage low by moving browser interaction into a local process.
- Produce evidence that is structured, bounded, and re-runnable.
- Distinguish application failures, test harness failures, and ambiguous conditions.
- Reuse the target project's tests if they already exist.
- Keep secrets and sensitive data out of both prompts and reports.

### 3.2 MVP non-goals

- Becoming a general-purpose visual browser agent.
- Running UI exploration through repeated screenshots.
- Writing arbitrary Playwright/Python/JavaScript from the model.
- Starting or stopping the target application.
- Running tests against production by default.
- Supporting every Playwright action.
- Automatically fixing the application based on E2E results.
- Performing free-form self-healing against expected behavior.
- Replacing the target project's entire test suite.

## 4. When E2E mode is used

E2E mode is only relevant if all of the following conditions are met:

1. The target has a web application reachable from the verification environment.
2. The change affects routes, forms, interactions, UI state, or frontend–backend integration.
3. There is user behavior that can be expressed as an assertion.
4. The environment and test data are stable enough for repeated testing.

E2E mode is not required for changes such as:

- documentation only;
- internal refactors with no behavior change;
- non-web libraries;
- changes already sufficiently proven by unit/integration tests;
- applications that have no safe environment for browser tests.

Suggested initial resolver:

| Condition | Primary mode |
|---|---|
| Simple parse/config change | `syntax` |
| Logic change with no web surface | `delegated` + existing tests |
| Web application workflow change | `e2e` |
| Project has relevant E2E tests | run existing tests first |
| Browser/environment unavailable | `incomplete`, not a `pass` fallback |

Runtime note: the `verify_mode()` reader is currently a pure allowlist — unknown values only get a warning and then **silently fall back to `delegated`** (`core/runtime/config_defaults.py`, the `verify_mode` function and `commands.verify_mode` validation). If `e2e` is only added to the allowlist without a preflight, an `e2e` configuration in an environment without Playwright would silently run as a delegated review, not `incomplete`. Therefore:

- the reader stays a pure allowlist (`delegated | syntax | e2e`) and does no dependency checking;
- the dependency/browser/base URL preflight runs in the executor's `e2e` branch, before any stage runs;
- preflight failure → an `incomplete` result with an explicit reason (`playwright_missing`, `browser_missing`, `base_url_unreachable`), never dropping to `delegated` without user instruction.

## 5. Conceptual architecture

Per D1, `e2e` mode is a three-stage runner inside `Executor.execute()`. The `e2e` branch enters next to the `syntax` branch (early local branch), but unlike `syntax`, this branch calls the provider twice (stage 1 and stage 3) through the existing delegated path.

```text
Task + Git scope
      │
      ▼
┌──────────────────────────────────────────────────────────────────┐
│ STAGE 1 — delegated call (second_agent, read-only)               │
│   scope resolver ─▶ code explorer ─▶ claim builder ─▶ coverage   │
│   output: [E2E SPEC] = claims + scenario JSON + existing tests   │
└──────────────────────────────┬───────────────────────────────────┘
                               ▼
                     spec validator (local, deterministic)
                               │
             ┌─────────────────┴─────────────────┐
             ▼                                   ▼
      Existing E2E test                    Scenario JSON
             │                                   │
             └─────────────────┬─────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ STAGE 2 — Playwright player (local child process)                │
│   stdout JSONL: progress, heartbeat, result                      │
│   evidence classifier: app / harness / unknown                   │
└──────────────────────────────┬───────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ STAGE 3 — normalizer + hybrid review                             │
│   normalizer: player result ─▶ [E2E EVIDENCE] compact            │
│   delegated call: reviewer ─▶ [VERIFICATION] canonical           │
│   combine fail-closed ─▶ effective verdict ─▶ existing validator │
└──────────────────────────────────────────────────────────────────┘
                               │
                               ▼
              validate_verification_contract (unchanged)
                               │
                               ▼
                  _finalize_verify_result / exit code
```

The visual fallback (screenshot/trace) still exists in stage 3 and is only opened when structured evidence is insufficient (see §16).

Main components:

| Component | Stage | Responsibility | Status in the codebase |
|---|---|---|---|
| Scope resolver | 1 | Determines the change being verified | reuse — the verify prompt already asks for changed files + consumers (`core/prompt/prompt_builder.py`, verify section) |
| Code explorer | 1 | Finds routes, components, selectors, APIs, and expected behavior | reuse — second_agent's explore capability |
| Claim builder | 1 | Turns exploration results into provable behavior | new — prompt + `[E2E SPEC]` output contract |
| Coverage resolver | 1 | Determines whether existing tests are sufficient | new |
| Spec validator | local | Validates scenarios and restricts the allowed actions | new — `core/evidence/e2e/spec.py` |
| Player supervisor | 2 | Runs the child process, JSONL parsing, heartbeat, idle/total timeout, tree kill | new; reuses `utils.osutil.terminate_tree()` and the bounded capture pattern from the provider adapters |
| Playwright player | 2 | Runs the browser and produces structured events | new — `core/evidence/e2e/player.py`, lazy Playwright import |
| Evidence classifier | 2 | Classifies `app`, `harness`, or `unknown` | new |
| Normalizer | 3 | Maps player results to `[E2E EVIDENCE]` + canonical findings | new — `core/evidence/e2e/normalize.py` |
| Hybrid reviewer | 3 | Matches runtime evidence against code and claims, outputs `[VERIFICATION]` | reuse — existing delegated verify path + new prompt section |
| Contract validator | final | Determines the runtime verdict + exit code | reuse without changes — `core/evidence/contract.py`, `core/evidence/result_shaping.py` |

### 5.1 Stage 1 and stage 3 dispatch rules (anti-recursion)

`Executor.execute()` has only one public entry point, and the verify mode is read from config **before** routing (`mode = verify_mode(project_root) if normalized_command == "verify"`). If stage 1 or stage 3 naively calls `Executor.execute("verify", ...)` while the config is still `verify_mode=e2e`, that call re-enters the E2E branch — recursion, and the provider is never reached. In addition, the invalid-evidence guard rejects `verify` command output that does not contain `[VERIFICATION]`, so stage 1 (output `[E2E SPEC]`) must not run as a `verify` command at all.

Implementation decision (Q1 of the Phase 1 plan, 2026-09-14): the provider-call core of `execute()` — handoff, adapter binding, `_adapter_run`, one bounded continuation, call meta archive, snapshot, session persistence, audit, `_record_failed_call`, the `[VERIFICATION]` guard and the evidence guard — is extracted into `Executor._run_delegated(route, command, task, session, session_id, project_root, work_dir, on_progress, session_manager, *, prompt, prompt_meta, lock_claim)`; prompt sizing becomes `Executor._build_delegated_prompt(route, command, task, session_id, project_root, *, ..., e2e_evidence=None)`. The public `execute()` path uses both and then still does the role bookkeeping (facts, index, fan-out, cache). The E2E stages call both helpers **directly** — zero `execute()` re-entry, zero re-locking, zero `_call_metas` reset, zero role side effects. Stage orchestration lives in the `core/evidence/e2e/runner.py` module, called from the `e2e` branch in `execute()` after the lock is held.

Rules:

| Stage | Command | Route | Output contract | Dispatch method |
|---|---|---|---|---|
| 1 | `e2e_spec` (internal, new) | new entry in `COMMAND_ROUTES` (`config/routing.py`), role `ROLE_EXPLORATION` — read-only, without the `[VERIFICATION]` validator; registered in `INTERNAL_COMMANDS` | standard exploration evidence format (`[EVIDENCE]` … `[DIGEST]`) with an additional `[E2E SPEC]` section inside it | `executor._run_delegated(route_e2e_spec, "e2e_spec", ...)` from `runner.py`; not a `verify` command, so it does not touch `verify_mode` |

Stage 1 guard note: the executor has a generic evidence guard for all `exploration|reasoning` roles that rejects output without evidence markers (`_EVIDENCE_MARKERS` in `core/provider/continuation.py`: `[evidence]`, `[digest]`, `entry_points`, `grounded:`, etc.) as `invalid_evidence`, separate from the `[VERIFICATION]` guard. Therefore stage 1 output **must** take the form of standard exploration evidence — `[EVIDENCE]` (grounded/assumptions/scope) + `[DIGEST]` — and `[E2E SPEC]` is only an additional section inside it, not a replacement format. Consequence: the existing guard, exploration continuation, and digest-first apply as-is to stage 1; `_EVIDENCE_MARKERS` does not need to change. The `[E2E SPEC]` parser works on `content` that has already passed the guard; missing section or broken JSON → `incomplete` with reason `spec_invalid` (one structured continuation if `[EVIDENCE]` is present but `[E2E SPEC]` is not).
| 2 | — | local | player JSONL | supervisor, no provider |
| 3 | `verify` | existing `verify` route (already resolved by `execute()`), role `ROLE_VERIFICATION` | canonical `[VERIFICATION]` | `executor._run_delegated(route_verify, "verify", ..., prompt=<prompt verify + [E2E EVIDENCE]>)` from `runner.py`; does not go through `execute()`, so `verify_mode` is not re-read — no override, no recursion |

Internal boundaries:

- `e2e_spec` is not available as a public command: it is not in the `main.py` argparse `choices`, and `main.run()` rejects it via `config.routing.INTERNAL_COMMANDS` (`routing_error`). `COMMAND_ROUTES` contains it because the router only determines the role. `provider_select.SELECTABLE_ROUTES` does not contain it.
- Stages never call `execute()`; there is no override parameter and no recursion guard because there is no path for recursion.
- Phase 1 test (`tests/checks/e2e_routing.py`): a fake adapter records every provider call; stage 1 is recorded as `e2e_spec`, stage 3 as `verify`, there is no third call; stage 3 is skipped when the browser result is `incomplete`.

Final result metadata: `meta.verify_mode = "e2e"`, `meta.verdict` (effective, set before `_finalize_verify_result`), `meta.e2e = {browser_verdict, reason, stages: [{stage, command, prompt_id|returncode, ...}], artifacts, preflight, spec_source, fake}` so that `await`/`result` can show the trail of all three stages.

Stage 1 output contract (proposal; the final form is set in Phase 1 following the `[VERIFICATION]` section parser pattern). The `[EVIDENCE]`/`[DIGEST]` parts follow the existing exploration format; `[E2E SPEC]` is an additional section:

```text
[EVIDENCE]
confidence: high
entry_points:
- src/pages/Login.tsx:12-80
grounded:
- Login form submits to POST /api/login, redirects to /dashboard on success [src/routes/auth.ts:40-58]
assumptions:
- none
scope_covered:
- src/pages, src/routes, e2e/
scope_not_covered:
- backend session store
uncertainties:
- selector heuristic pada ...

[E2E SPEC]
claims:
- id: login-valid-user | severity: blocking | source_refs: src/pages/Login.tsx, src/routes/auth.ts
existing_tests:
- path: e2e/login.spec.ts | covers: login-valid-user | confidence: high
scenario_json: <blok JSON sesuai §9>
coverage_gap:
- claim_id not yet covered by an existing test

[DIGEST]
summary: 1 claim blocking, existing test covers valid login; gap: redirect after an error.
key_findings:
- ...
risk_level: medium
confidence: high
```

## 6. Codebase exploration scope

Exploration must not scan the whole repository without bounds. Scope order:

1. The task or feature name given by the user.
2. The working-tree diff if there are uncommitted changes.
3. The commit range against the merge-base for a feature branch.
4. Changed symbols and their direct dependencies.
5. Related routes, components, templates, handlers, APIs, and tests.
6. APP_MAP, feature registry, or internal documentation if available.

Data sought:

- entry URL;
- prerequisites and authentication;
- user-visible role, label, heading, or `data-testid`;
- main action;
- relevant API requests;
- expected redirect or final state;
- error state;
- existing tests and fixtures;
- required environment variables, names only;
- risk of destructive actions.

Exploration output must be a compact digest, not a copy of whole files:

```text
Feature: login
Scope: login form and redirect changes
Entry route: /login
Primary component: src/pages/Login.tsx
Success route: /dashboard
Stable selector: role=button, name=Sign in
Existing coverage: unit validation only
Runtime gap: redirect and session cookie not yet proven
```

## 7. Verification claims

Before creating browser steps, the system must determine the behavior to be proven.

Example:

```json
{
  "claims": [
    {
      "id": "login-valid-user",
      "description": "A user with valid credentials reaches the dashboard",
      "severity": "blocking",
      "source_refs": [
        "src/pages/Login.tsx",
        "src/routes/auth.ts"
      ]
    }
  ]
}
```

Claim rules:

- Every scenario has at least one claim.
- Every assertion must point to one claim.
- A claim must come from the task, a requirement, a code contract, or an existing test.
- Interaction steps without assertions are not enough to produce `pass`.
- Step count is not a measure of coverage; proven critical claims are the main measure.

## 8. Coverage strategy

Test selection order:

1. Look for matching existing Playwright/Cypress/Webdriver tests.
2. Check whether those tests cover the claim being verified.
3. Run only the relevant tests, not the whole suite without reason.
4. If coverage is partial, run the existing tests and then create a scenario only for the gap.
5. If there is no coverage, create a new scenario in the declarative format.

Existing tests are not automatically trusted. Exit code, report, and relevance to the claim must still be checked.

Implementation (batch 6): the runtime never guesses how to run the project's tests. Tests are only run via a user-written `commands.e2e.existing_test_command` (argv without a shell; `{files}` and `{base_url}` are expanded), for plain-path files that exist and match `existing_test_allowlist`, with a timeout and process tree kill. A passing test proves the claims it covers (those claims need not be asserted in the scenario); a failing test makes its claims `unknown`. Without a command, listed tests are only recorded and their claims must be asserted.

## 9. Declarative scenario

The model produces scenario JSON. The runtime still contains the predefined and tested Playwright implementation.

```json
{
  "version": 1,
  "feature": "login",
  "heal_iteration": 0,
  "claims": [
    {
      "id": "login-valid-user",
      "description": "A valid login opens the dashboard",
      "severity": "blocking"
    }
  ],
  "steps": [
    {
      "action": "goto",
      "url": "/login"
    },
    {
      "action": "fill",
      "selector": {
        "role": "textbox",
        "name": "Email"
      },
      "selector_provenance": {
        "type": "source",
        "ref": "src/pages/Login.tsx",
        "confidence": "high"
      },
      "value": "${E2E_USER}"
    },
    {
      "action": "fill",
      "selector": {
        "testid": "password"
      },
      "value": "${E2E_PASS}"
    },
    {
      "action": "click",
      "selector": {
        "role": "button",
        "name": "Sign in"
      }
    },
    {
      "action": "expect_url",
      "contains": "/dashboard",
      "claim_id": "login-valid-user"
    }
  ]
}
```

MVP actions:

- `goto`
- `click`
- `fill`
- `select`
- `press`
- `wait_dom`
- `expect_dom`
- `expect_url`
- `expect_title`
- `probe`

Not available in the MVP:

- arbitrary `evaluate`;
- raw JavaScript/Python;
- shell commands;
- file upload/download;
- browser extensions;
- cross-origin navigation without an allowlist;
- destructive actions without explicit declaration.

## 10. Selectors and provenance

Recommended selector order:

1. `role + accessible name`
2. `label`
3. `data-testid`
4. stable text
5. CSS as the last fallback

Every selector should have a provenance:

| Provenance | Meaning |
|---|---|
| `source` | Found directly in the codebase |
| `existing_test` | Taken from an existing test |
| `runtime_probe` | Found from the actual DOM |
| `heuristic` | Estimated by the model, low confidence |

Provenance contributes to error classification. A missing stable selector from source carries different weight than a failed heuristic selector.

## 11. DOM probe

A probe is a discovery fallback, not a required step on every run.

A probe runs if:

- a selector from the code is not found;
- the application builds elements dynamically;
- the accessible name cannot be inferred from source;
- an existing test uses a selector that is no longer valid.

Probe output is bounded, for example:

```json
{
  "url": "http://localhost:8000/login",
  "title": "Login",
  "headings": ["Sign in"],
  "buttons": [
    {"role": "button", "name": "Sign in", "testid": null}
  ],
  "inputs": [
    {"role": "textbox", "label": "Email", "type": "email"}
  ]
}
```

Provisions:

- the number of elements is bounded;
- text is truncated;
- hidden elements are ignored unless requested;
- input values are not recorded;
- secrets are redacted before being written;
- the full DOM is not put into the prompt.

Probes can be cached by the combination of commit/tree hash, route, viewport, and auth profile. The cache is not used if the related source or route changed.

## 12. Runtime evidence

Per-step evidence is produced as JSONL so the runner can receive heartbeat and progress without waiting for the process to finish.

```json
{
  "step": 4,
  "action": "expect_url",
  "claim_id": "login-valid-user",
  "status": "failed",
  "expected": {"contains": "/dashboard"},
  "actual": {"url": "/login?error=1"},
  "url_after": "/login?error=1",
  "duration_ms": 420,
  "origin": "app"
}
```

Evidence collected:

- step status and duration;
- the selector that was successfully used;
- URL and title;
- bounded target outer HTML;
- assertion expected vs actual;
- relevant same-origin HTTP 5xx;
- uncaught page errors;
- filtered console errors;
- artifact references.

Evidence not automatically sent to the model:

- full page HTML;
- all of stdout/stderr;
- screenshots;
- traces;
- credential values;
- sensitive request/response bodies.

## 13. Failure classification

Use three origins, not two:

| Origin | Definition | Typical verdict | May be healed? |
|---|---|---|---|
| `app` | Sufficient evidence that the application violates the claim | `fail` | no |
| `harness` | Sufficient evidence that the scenario/test runtime is at fault | `incomplete` | limited |
| `unknown` | Evidence not yet sufficient to determine the cause | `incomplete` | not automatically |

Classification examples:

| Condition | Origin | Result |
|---|---|---|
| Grounded assertion not satisfied | `app` | `fail` |
| HTTP 5xx on the main navigation/request | `app` | `fail` |
| Uncaught same-origin page error | `app` | `fail` |
| Browser not installed | `harness` | `incomplete` |
| Base URL unreachable | `harness` | `incomplete` |
| Heuristic selector not found | `harness` | `incomplete` |
| Stable selector missing but the page is also not yet stable | `unknown` | `incomplete` |
| Background analytics returns 5xx | observation | warning, not automatically `fail` |
| Third-party console error | observation | warning or filter |

An `unknown` value must not be automatically changed to `harness`, because that could hide an application regression.

## 14. Verdict

### 14.1 Browser verdict

- `pass`: all blocking claims are proven, there are no gaps, and there are no app errors.
- `fail`: at least one blocking claim failed with app-origin evidence.
- `incomplete`: verification could not be completed or the origin is still ambiguous.

Reasons for `incomplete` include:

- `playwright_missing`
- `browser_missing`
- `base_url_unreachable`
- `spec_invalid`
- `harness_error`
- `unknown_origin`
- `stuck`
- `timeout`
- `launch_failed`
- `output_truncated`

### 14.2 Hybrid combination

| Browser | Delegated review | Final |
|---|---|---|
| `fail` | anything | `fail` |
| `pass` | clean | `pass` |
| `pass` | finds a blocking issue | `fail` |
| `pass` | incomplete | `incomplete` |
| `incomplete` | anything | `incomplete` |

A browser pass is not proof that coverage is sufficient. The hybrid reviewer's job is to judge whether the claims and scenarios actually represent the change's risk.

### 14.3 Normalization to the canonical contract (D2)

The verify runtime currently has three points that treat verify output as a delegated contract and only exempt quick mode (`meta.mode == "quick"` or `meta.quick_verify`):

1. `_finalize_verify_result` in `core/evidence/result_shaping.py` — calls `validate_verification_contract` and writes `meta.verdict`; called twice (the worker in `main.run` and `await_job` in `core/jobs/job_lifecycle.py`).
2. The continuation predicate in `core/provider/continuation.py` — asks for `[VERIFICATION]` again if the shape does not satisfy the contract.
3. The invalid-evidence guard in `core/provider/executor.py` — rejects verify output without `[VERIFICATION]`.

`e2e` mode does **not** add new exemptions at any of these three points. Instead, the stage 3 normalizer guarantees that the `result.content` leaving the `e2e` branch is already a complete canonical `[VERIFICATION]` (`verdict`, all finding sections, `checks_run`, `not_verified`, `confidence`). Raw player JSONL, the player report, and `[E2E EVIDENCE]` never become the final `result.content`; they are stored as artifacts and only go into the reviewer prompt.

Rules for mapping player results to the canonical contract:

| Player result | To the canonical contract |
|---|---|
| Blocking claim failed, origin `app` | finding in the blocking section, `severity` per the claim, **temporal** `origin` tag (`introduced`/`regression`/`pre_existing`/`unknown`, determined by the reviewer from code + git scope), plus the field `evidence_source: e2e_runtime` |
| Non-blocking claim failed, origin `app` | non-blocking finding with the same fields |
| Step/claim failed, origin `harness` | `not_verified` entry with a reason (`selector_missing`, `harness_error`, ...) — the runtime automatically holds the verdict at `incomplete` |
| Step/claim failed, origin `unknown` | `not_verified` entry with reason `unknown_origin` — must not be promoted to `harness` |
| Preflight failed (`playwright_missing`, `browser_missing`, `base_url_unreachable`, `spec_invalid`) | all claims go to `not_verified` with that reason; `checks_run` empty with a reason |
| Step succeeded / claim proven | a line in `checks_run` (`e2e:<claim_id>: pass`) |
| Observation (background 5xx, third-party console error) | non-blocking warning, not a finding |

The `origin` field on a finding keeps its current temporal meaning; the player's `app/harness/unknown` classification is **never** written to `origin`. That classification lives in `[E2E EVIDENCE]` and in `evidence_source`/the `not_verified` reason.

The §14.2 combine matrix is applied by the normalizer after the reviewer answers: if the browser result is `fail` but the reviewer writes `verdict: pass`, the normalizer lowers the effective verdict to `fail` and records the warning `reviewer_verdict_overridden`. This must happen **before** `validate_verification_contract` and `_verify_exit_code` run, because the current contract allows `DONE` with only verification-gap warnings to still exit `0` (`core/evidence/contract.py`, exit status section). An effective verdict that is already `fail`/`incomplete` guarantees a non-zero exit code without changing the existing exit mapping.

## 15. Self-healing

### 15.1 MVP decision

The MVP has no repeated reasoning-based healing. The runtime only tries the deterministic candidate selectors already in the spec.

Reasons:

- reduce complexity;
- prevent expected behavior from silently changing;
- make it easier to measure real false failures;
- keep token usage low.

### 15.2 Later phase

If MVP data shows significant selector failures, add limited healing.

What may change:

- selector;
- candidate order;
- wait strategy;
- timeout within configured limits;
- bootstrap route with the same semantic result.

What must not change:

- claim;
- expected URL;
- expected text/state;
- severity;
- assertion type;
- expected business outcome.

Every healing must have a before/after, a reason, an evidence source, and an iteration counter. The default maximum is set after smoke test data is available, not as a permanent assumed number.

## 16. Screenshots, HTML, and traces

A distinction is needed between storing artifacts and asking the model to analyze artifacts.

| Condition | Screenshot stored | Read by the model automatically |
|---|---:|---:|
| Pass | no | no |
| Clear app failure | optionally one | no |
| Harness selector failure | optional | no; use a probe first |
| Unknown/stuck | yes | only if DOM/report is insufficient |
| Navigation timeout | yes | if needed for classification |

Full HTML is only stored on failure, stuck, or unknown. On successful steps it is enough to store a digest and the bounded target outer HTML.

A trace may be started from the beginning but is only kept when the run fails and other evidence is insufficient. Artifact retention must be bounded by run count or size.

Location and indexing (MVP decision):

- E2E artifacts are stored run-local in `sessions/<session>/logs/<run>/e2e/` (redacted final spec, events JSONL, report, screenshot/HTML/trace per policy), using the existing archive and redaction audit in `core/evidence/runtime_io.py`;
- E2E artifacts are **not** put into `evidence.jsonl`. The evidence index currently only accepts immutable artifacts in the format `sessions/<session>/logs/<run>/output.raw.md` with a hash and anchor freshness (`core/evidence/evidence_store.py`), and the executor only does `record()`/reuse for `exploration|reasoning` roles — verify is not indexed today either;
- `output.raw.md` for an `e2e` run contains the canonical `[VERIFICATION]` from stage 3, the same as a delegated run;
- pruning follows the existing archive retention; the `e2e/` directory is deleted together with its run;
- implementation (batch 4): bounded HTML and one screenshot per failed step, `trace.zip` only for failed runs; input values are cleared before capture; no screenshots or traces if the scenario resolves `${ENV}`; `commands.e2e.artifact_max_mb` trims traces, then screenshots, then HTML.

Reuse/indexing of E2E artifacts is re-evaluated after Phase 5 if there is a need for cross-run replay.

## 17. Token-efficient hybrid review

The hybrid reviewer receives:

- the task and change scope;
- the list of verification claims;
- changed symbols or source references;
- a step summary;
- assertion results;
- error classification;
- artifact paths;
- bounded relevant code excerpts.

The hybrid reviewer does not automatically receive the full DOM, raw logs, screenshots, or traces.

Mechanism (D1 + D2): the reviewer is an ordinary delegated verify call. The verify prompt builder (`core/prompt/prompt_builder.py`, the verify section insertion branch) gets one additional `[E2E EVIDENCE]` section containing the summary above in compact form. The output format requested from the reviewer **does not change**: canonical `[VERIFICATION]` with the same finding tags. The reviewer is not asked to redo the `app/harness/unknown` classification; it receives that classification as input and adds the temporal dimension (`origin`) and a coverage assessment.

Compact form of `[E2E EVIDENCE]` (proposal):

```text
[E2E EVIDENCE]
browser_verdict: fail
claims:
- login-valid-user | blocking | status: failed | origin: app
steps: 5 total, 4 passed, 1 failed (step 5 expect_url)
assertion_failed:
- claim: login-valid-user | expected: url contains /dashboard | actual: /login?error=1
app_errors:
- none
harness_issues:
- none
not_verified:
- none
artifacts: sessions/<session>/logs/<run>/e2e/
```

Reviewer responsibilities:

1. Check whether the claims match the change.
2. Check whether blocking behavior is covered.
3. Match browser failures with the related code.
4. Detect false passes caused by assertions that are too weak.
5. Produce findings using the existing verify contract.

If the `origin` field on a finding already has a temporal meaning such as `introduced`, `regression`, `pre_existing`, or `unknown`, do not use `origin=behavior`. Add a separate evidence field, for example:

```json
{
  "origin": "regression",
  "evidence_source": "e2e_runtime"
}
```

## 18. Security and isolation

Security defaults:

- only allow loopback/local development;
- remote hosts require opt-in and an allowlist;
- production is rejected by default;
- cross-origin navigation is rejected unless allowed;
- credentials only via `${ENV_NAME}` or local auth state;
- environment values do not enter the final spec, report, log, or prompt;
- sensitive inputs are cleared before screenshots;
- storage state is stored in a Git-ignored runtime area;
- request/response bodies are not recorded by default;
- destructive actions require metadata and explicit configuration;
- no start commands or shell commands from the target project are executed, except an existing test command written by the user themselves in `commands.e2e.existing_test_command` (argv without a shell, file allowlist, timeout, scrubbed output).

For actions that can change data, the scenario must declare:

```json
{
  "side_effect": "creates_test_data",
  "test_environment_required": true,
  "cleanup": "external_fixture"
}
```

## 19. Initial configuration

The final names and structure must follow the project's configuration pattern at implementation time. Conceptual form:

```json
{
  "commands": {
    "verify_mode": "e2e",
    "e2e": {
      "base_url": "http://localhost:8000",
      "browser": "chromium",
      "headless": true,
      "spec_path": ".workflow/e2e/spec.json",
      "nav_timeout_ms": 15000,
      "step_timeout_ms": 8000,
      "idle_timeout_s": 30,
      "total_timeout_s": 300,
      "probe_max_elements": 200,
      "hybrid_review": true,
      "allow_remote": false,
      "allowed_origins": [],
      "allow_side_effects": false,
      "fail_on_console_error": false,
      "artifact_max_mb": 25,
      "replay": false,
      "existing_test_command": [],
      "existing_test_allowlist": [],
      "existing_test_timeout_s": 300
    }
  }
}
```

Timeout values and probe limits are initial values for smoke testing, not permanent decisions. Calibrate based on data from several real projects.

Configuration reader: `verify_mode` in `core/runtime/config_defaults.py` is extended to `delegated | syntax | e2e` (pure allowlist, see the note in §4). The `commands.e2e` block needs a new typed reader with per-field defaults and nested validation (there is currently no reader for nested structures under `commands`); invalid values get a warning and fall back to the field default, rather than disabling the mode.

### 19.1 Dependency policy (D3)

- Playwright is an **optional extra**, pinned in `requirements-e2e.txt` (`playwright==1.60.0`) and only installed by `python install.py --apply --with-e2e`, which also runs `python -m playwright install chromium`. The default installation does not change.
- The core runtime stays stdlib-only. Only modules in `core/evidence/e2e/` import `playwright`, and only lazily inside the functions that run the player — not at module import.
- The dependency invariant test (`tests/checks/deps.py`) excludes `core/evidence/e2e/` from the stdlib-only scan, or allows `playwright` as an optional import wrapped in `try/except ImportError`.
- `e2e` branch preflight: (1) import Playwright, (2) check that the Chromium browser is installed, (3) check that `base_url` is reachable. Failure on any of them → `incomplete` with the related reason; no automatic installation.
- The installation command (`--with-e2e`) is documented in the `verify.md` skill and `docs/runtime-contracts.md`; `/.doctor` reports `e2e_readiness` (interpreter, installed version compared with the pinned version, package, browser, URL). Version 1.60.0 is used in the Windows Chromium smoke.
- The manifest/bundle does not carry browser binaries.

## 20. Process model

The player runs as a child process:

```text
runner
  └─ python -m core.evidence.e2e.player
       ├─ stdout JSONL: progress, heartbeat, result
       ├─ stderr bounded
       ├─ browser process tree
       └─ artifact directory
```

The runner is responsible for:

- path and config validation;
- environment redaction;
- bounded stdout/stderr capture;
- heartbeat;
- idle timeout;
- total timeout;
- process tree termination;
- report parsing;
- classifying broken or truncated output.

The player is responsible for:

- opening the browser;
- running actions;
- evaluating assertions;
- collecting browser events;
- creating probes;
- creating artifacts per policy;
- writing JSONL events.

### 20.1 Reuse vs new

What already exists and is reused:

| Need | Existing source |
|---|---|
| Cross-platform process-tree termination | `utils.osutil.terminate_tree()`, `hidden_run_kwargs()` |
| Bounded stdout/stderr capture pattern, polling timeout, heartbeat callback | provider adapters (`adapters/providers/opencode_adapter.py`, the subprocess run section) — the pattern is copied, not called directly |
| Outer heartbeat, idle stall detection, recovery worker | `core/jobs/job_manager.py`, `core/jobs/job_lifecycle.py` — wraps the whole verify job, including stage 2 |
| Artifact archive, pruning, redaction audit, call metadata | `core/evidence/runtime_io.py` |
| Delegated call for stage 1 and stage 3 | the existing provider path in `Executor.execute()` |

What is new (`core/evidence/e2e/`):

| Module | Contents |
|---|---|
| `spec.py` | scenario + claims schema, action allowlist, selector provenance, `${ENV_NAME}` substitution, validation |
| `preflight.py` | lazy Playwright import, browser check, base URL check, safe-URL/allowed-origin policy |
| `supervisor.py` | spawn player, parse JSONL, child heartbeat, idle timeout, total timeout, call `terminate_tree`, classify broken/truncated output |
| `player.py` | Playwright implementation: actions, assertions, console/pageerror/response observers, probe, artifact policy |
| `classify.py` | `app/harness/unknown` taxonomy |
| `normalize.py` | player result → `[E2E EVIDENCE]` + canonical finding/`not_verified`/`checks_run`; fail-closed combine |
| `redact.py` | recursive redaction of spec/event/report (may use the runtime_io redaction helper if suitable) |

The player heartbeat is reported to JobManager through the existing job heartbeat mechanism, so outer stall detection still applies during stage 2 without adding a new path.

## 21. Phased implementation plan

The position of each phase is in §1.2; implementation batches 1–8 are recorded in §28.

### Phase 0 — Baseline and requirements validation

1. Pick 3–5 real web changes that were previously verified only through code.
2. Record the expected behavior and runtime bugs that have slipped through.
3. Establish the current baseline for tokens, time, and verdict quality.
4. Make sure there is a testable non-production environment.

Output: a small dataset to judge whether E2E mode delivers value.

### Phase 1 — Contract without browser

1. Add `e2e` to the `verify_mode` allowlist and create a typed `commands.e2e` reader with defaults + nested validation (`core/runtime/config_defaults.py`).
2. Add an `e2e` branch in `Executor.execute()` next to the `syntax` branch, with a preflight that produces a reasoned `incomplete`; test that `e2e` mode without dependencies does **not** fall to `delegated`.
2a. Extract `Executor._run_delegated()` and `Executor._build_delegated_prompt()` from `execute()` without behavior change (suite + sim green before continuing); add the internal route `e2e_spec` (`config/routing.py`, exploration role) and `INTERNAL_COMMANDS`; `e2e_spec` is not in the `main.py` argparse `choices` and `main.run()` rejects it; test the §5.1 dispatch order with a fake adapter (`e2e_spec` → `verify`, no third call).
3. Create the scenario and verification claims schema (`spec.py`), including selector provenance.
4. Create the stage 1 output contract: the `e2e_spec` prompt asks for the standard exploration evidence format + an `[E2E SPEC]` section; section parser + fixture; test that the fixture passes the generic evidence guard (`_EVIDENCE_MARKERS` unchanged), that `[EVIDENCE]` without `[E2E SPEC]` triggers one structured continuation and then `incomplete:spec_invalid`, and that the parser does not collide with the existing `[VERIFICATION]` parser.
5. Create the `app/harness/unknown` taxonomy (`classify.py`).
6. Create the normalizer (`normalize.py`): the §14.3 mapping, compact `[E2E EVIDENCE]`, fail-closed combine matrix, setting the effective verdict before the validator.
7. Create the redaction contract (`redact.py`).
8. Create a fake player that emits deterministic JSONL (pass, app fail, harness fail, unknown, malformed, stall, crash).
9. Create the supervisor (`supervisor.py`) and test it with the fake player: heartbeat, idle timeout, total timeout, process-tree termination, large stdout/stderr, truncated report.
10. Test the final path: `result.content` from the `e2e` branch always passes `validate_verification_contract` without shape warnings; raw JSONL/report never becomes `content`; the continuation predicate returns `None`; the invalid-evidence guard does not reject; the exit code matches the effective verdict for every §14.2 combination.
11. Exclude `core/evidence/e2e/` from the stdlib-only invariant test and make sure `python -c "import core"` still does not touch Playwright.

Exit criteria: the whole lifecycle can be tested without a browser dependency, and there are no changes to `core/evidence/contract.py`, `core/evidence/result_shaping.py` (other than meta), or `core/provider/continuation.py`.

### Phase 2 — Playwright MVP

1. Implement the MVP actions.
2. Implement the MVP assertions.
3. Add filtered console, page error, and network observation.
4. Implement the bounded DOM probe.
5. Apply the screenshot/HTML/trace policy.
6. Apply the same-origin and secret policies.
7. Run a smoke test on a real local application.

Exit criteria: at least one passing flow, one app failure, and one harness failure correctly classified.

### Phase 3 — Stage 1: claim builder via delegated call

Per D1, the claim builder is a second_agent delegated call, not a separate local module.

1. Add the stage 1 prompt section in `core/prompt/prompt_builder.py`: scope from task + Git, instructions to find routes, components, APIs, selectors, existing tests, and to produce `[E2E SPEC]`.
2. Reuse the existing scope mechanism in the verify prompt (changed files + consumers).
3. Build verification claims and the coverage resolver inside the `[E2E SPEC]` contract.
4. Generate scenarios only for coverage gaps.
5. Validate `[E2E SPEC]` locally (`spec.py`); invalid spec → `incomplete` with reason `spec_invalid`, with one structured continuation if the shape is almost correct (existing continuation pattern).
6. Test that full source is not forwarded to the player or reviewer; only digest + source references.

Exit criteria: the origin of a scenario can be explained through claims and source references; stage 1 can be simulated with a fake adapter that returns an `[E2E SPEC]` fixture.

### Phase 4 — Stage 3: hybrid review

1. Add the compact `[E2E EVIDENCE]` section to the verify prompt (§17).
2. The reviewer is asked for canonical `[VERIFICATION]` output; no new output format.
3. The normalizer applies the fail-closed combine matrix and sets the effective verdict before `_finalize_verify_result`.
4. Test browser `fail` + reviewer `pass` → final `fail` with warning `reviewer_verdict_overridden`.
5. Test browser `pass` + incomplete reviewer → `incomplete`; browser `incomplete` + anything → `incomplete`.
6. Test that `_finalize_verify_result` is called twice (worker + `await`) without duplicating warnings or changing the verdict.

Exit criteria: the final verdict is consistent for every browser/reviewer combination, with the existing validator unmodified.

### Phase 5 — MVP evaluation

Measure on the Phase 0 dataset:

- additional runtime bugs found;
- false failures caused by the harness;
- number of `incomplete` results;
- number of probes;
- number of screenshots actually read by the model;
- tokens per verification;
- duration per verification;
- ability to re-run scenarios;
- stability on the supported Windows/Linux.

Decisions after evaluation:

- continue if E2E finds meaningful gaps with an acceptable false failure rate;
- fix the classifier/probe if `incomplete` is too high;
- stop, or keep as opt-in, if the benefit is small;
- only consider self-healing once there is data on the causes of harness failures.

## 22. Test strategy

### Unit tests

- valid and invalid specs;
- action allowlist;
- required claims;
- assertions must have a `claim_id`;
- selector provenance;
- `${ENV_NAME}` substitution;
- recursive redaction;
- error classification;
- verdict matrix;
- hybrid combination;
- safe URL and allowed origin;
- immutable assertions during later-phase healing;
- normalizer: every origin × severity × preflight reason combination produces the correct finding/`not_verified`/`checks_run`;
- normalizer: output always passes `validate_verification_contract` without shape warnings;
- `[E2E SPEC]` parser: valid, missing section, broken JSON;
- `verify_mode=e2e` + failed preflight → `incomplete`, not `delegated`;
- the `core/evidence/e2e/` exclusion in the stdlib-only invariant, and importing `core` without Playwright installed.

### Process tests with a fake player

- progress JSONL;
- heartbeat;
- large stdout/stderr;
- malformed event;
- idle timeout;
- total timeout;
- process tree termination;
- missing dependency;
- browser launch failure.

### Real browser smoke tests

- a simple page succeeds;
- DOM assertion fails;
- wrong redirect;
- heuristic selector fails;
- page error;
- main request HTTP 5xx;
- third-party errors do not cause a false fail;
- secrets do not appear in artifacts;
- screenshots are only created per policy.

### Integration tests

- E2E standalone pass/fail/incomplete;
- E2E + hybrid reviewer;
- invalid evidence guard;
- exit code;
- artifact retention;
- existing target test reuse;
- a coverage gap produces an additional scenario.

## 23. MVP acceptance criteria

The MVP is declared successful if all the criteria below are met. Status column = position as of 2026-09-14 after batches 1–8; "smoke" means proven with real Chromium against `tests/fixtures/e2e_app/` on Windows.

| # | Criterion | Status | Basis |
|---|---|---|---|
| 1 | A passing test can complete without screenshot analysis. | met (smoke) | A passing run stores no screenshot or trace; no stage sends screenshots to the model (`screenshots_sent_to_model` is always 0). |
| 2 | Every scenario has at least one grounded claim. | met | `source_refs` is required and must point to an existing file and line, or `req:<id>` (`ground_claims`). |
| 3 | Every assertion is linked to a claim. | met | `validate_scenario`. |
| 4 | Existing E2E tests are used first if relevant. | met (opt-in) | Runnable tests replace assertions on the claims they cover and run before the player, only if the user fills in `existing_test_command`. |
| 5 | A failed heuristic selector does not immediately become an app failure. | met (smoke) | Classified as `harness`, with a probe instead of a screenshot. |
| 6 | A failed grounded assertion cannot be healed into a pass. | met | No healing; app failure is fail-closed. |
| 7 | Browser/dependency/environment failures produce `incomplete`. | met (smoke) | Preflight, launch, navigation outside the origin, hung page (`stuck`). |
| 8 | Full DOM and screenshots do not automatically enter the prompt. | met | The stage 3 prompt only contains structured `[E2E EVIDENCE]`; a test checks that source markers appear in neither the prompt nor artifacts. |
| 9 | Secrets do not appear in the inspected report or artifacts. | met, with limits | Scrub of raw, URL-encoded, HTML- and JSON-escaped values; HTML artifacts are scrubbed; no screenshot/trace if `${ENV}` is used; smoke scans the bytes of all artifacts. Limits: values under 4 characters and other encodings (e.g. base64). |
| 10 | A browser `fail` cannot be changed to `pass` by the reviewer. | met | Fail-closed combine. |
| 11 | A scenario can be re-run on the same application state. | partial | a `phase: run` request with the same scenario can be re-run; replay (`last_spec.json`) is not implemented; reproducibility is measured per `scenario_hash`. |
| 12 | The browser process is stopped correctly on timeout. | met (Windows) | Smoke: hung page, idle timeout, zero Chromium processes left. Linux not yet tested. |
| 13 | Token usage is lower than a visual browser loop for the same flow. | not yet | Tokens per run and the ratio against delegated verify are available in `report`; the visual loop baseline is not yet measured. |

## 24. Evaluation metrics

Do not set success numbers without a baseline. Collect first:

| Metric | Purpose |
|---|---|
| Runtime bugs found | Measures added value compared with code review |
| False failure rate | Measures selector and classifier quality |
| Incomplete rate | Measures environment and harness readiness |
| Tokens per verification | Compares with visual browser |
| Browser runs per claim | Measures probe/rerun efficiency |
| Screenshot-analysis rate | Must be the exception |
| Median execution time | Measures impact on the workflow |
| Reproducibility rate | Measures scenario determinism |

Metrics must be separated by cause: app, harness, environment, and unknown.

Implementation (batch 7): each e2e run writes one `kind: e2e_run` row to `.workflow/quality.jsonl` containing raw counts; all rates are derived at read time (`core/audit/telemetry.py`, the `e2e` part of `main.py --command report`). Runs are split by `run_kind` (`project`, `smoke`, `fake`) before aggregation, so that test suite runs are not read as evidence of value. Mapping:

- false failure rate → candidates are `harness_failure_rate` and `unknown_origin_rate`; judging false failures still needs a human;
- incomplete rate → `incomplete_rate` and `incomplete_by_reason`;
- tokens per verification → `tokens_per_run` (joins stage prompt ids to `usage.jsonl`) and `project_vs_baseline_token_ratio` against delegated verify in the same workspace;
- browser runs per claim → `browser_runs_per_claim`;
- screenshot-analysis rate → `screenshot_analysis_rate`;
- median execution time → `duration_seconds.median`;
- reproducibility rate → repeated runs per `scenario_hash` with the same browser verdict;
- runtime bugs found → needs a human label per dataset change (`WORKFLOW_E2E_LABEL`).

## 25. Main risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Brittle selectors | false failure | provenance, role/testid, bounded probe |
| Assertions too weak | false pass | verification claims + hybrid coverage review |
| Healing changes expectations | hidden bugs | MVP without reasoning heal; immutable claims |
| Third-party errors | false fail | same-origin filtering and warning policy |
| Secrets enter artifacts | data leak | env references, redaction, bounded evidence |
| Tests touch production | data damage | local-only default, allowlist, destructive guard |
| Hybrid wastes tokens | increased cost | compact evidence, pointer-first |
| Unstable app/environment | many incompletes | environment preflight and separate reasons |
| Hung browser process | job hang | heartbeat, idle/total timeout, terminate tree |
| DSL too limited | low coverage | expand actions based on data, not assumptions |

Residual risks as of 2026-09-14 (after batches 1–8):

| Risk | Impact | Status / mitigation |
|---|---|---|
| `${ENV}` values under 4 characters, or echoes in encodings other than raw, URL, HTML, JSON (e.g. base64), are not scrubbed | the value can reach events, HTML, or the stage 3 prompt | Accepted: scrubbing short substrings breaks URLs and selectors. Screenshots and traces are not created if `${ENV}` is used. |
| Replay uses an old spec after code in the same file changed | stale selectors or flows, `harness` or `unknown` result | Opt-in (`replay: false` default); `code_changed_since_saved` is reported. |
| `existing_test_command` runs target project code | target code runs on the user's machine | Only from user config; argv without a shell, plain paths + allowlist, timeout + kill tree, scrubbed output. Not yet tested with a real runner. |
| Clicks or actions that change data without a `side_effect` declaration | test data changes without permission | Cannot be detected automatically; mitigation: loopback default, stage 1 prompt instructions, `allow_side_effects: false`. |
| Smoke only on Windows + Chromium | Linux, Firefox, WebKit behavior not yet proven | Test when there is a CI job or a Linux machine. |
| `_guarded_imports` in `tests/checks/deps.py` treats an import inside a function in a `try` block as guarded | false negative for non-stdlib deferred imports | Not yet fixed (since Phase 1). |
| The verify contract parser counts `- none — <explanation>` as a finding | false `fail` verdict on review | Pre-existing, outside this plan. |
| The e2e run usage row is recorded at finalization with the last stage's `prompt_id`, `command`, and `role`; the stage 1 row is labeled `verify` (found in batch 7) | `calls_by_command` and per-role metrics are mislabeled for e2e runs; total tokens per run remain correct | The e2e metrics token join uses all stage prompt ids so each row is counted once. The per-invocation attribution fix belongs in `core/provider/executor.py` (Phase 1 code), not yet done. |

Fixed during the implementation batches: resolved values echoed by the page leaked into the stage 3 prompt (verify batch 2, Phase 1); the normalizer did not route reviewer findings by tag; failed stage usage was recorded twice; typed values returned in URL-encoded form slipped past the scrub (found in batch 3 smoke, fixed in batch 4); third-party script page errors were attributed to the page origin and so became a false fail (found in batch 5 smoke).

## 26. Affected files

Working tree state after batches 1–8 (not yet committed). **new** = new file, **changed** = modified, **unchanged** = intentionally not touched.

| File | Status | Change |
|---|---|---|
| `core/runtime/config_defaults.py` | changed | `VERIFY_MODES` + `e2e`; `default_e2e()` including `allow_side_effects`, `artifact_max_mb`, `replay`, `existing_test_*`; nested validation; `e2e_config()` |
| `core/provider/executor.py` | changed | extract `_run_delegated()` + `_build_delegated_prompt()`; `e2e` branch to `runner.run()` |
| `config/routing.py` | changed | internal route `e2e_spec` + `INTERNAL_COMMANDS` |
| `main.py` | changed | `main.run()` rejects internal commands |
| `core/prompt/prompt_builder.py` | changed | `e2e_spec` prompt (source_refs, selector candidates, navigation, side effect, existing test); `[E2E EVIDENCE]` for verify |
| `core/audit/diagnostics.py` | changed | `e2e_readiness` in doctor |
| `core/audit/telemetry.py` | changed | `e2e_metrics()` in `report()` |
| `core/evidence/e2e/runner.py` | new | preflight, spec source (`spec_path`, replay, stage 1), validation + grounding, existing test, player, scrub + artifact budget, review, normalization, `e2e_run` row |
| `core/evidence/e2e/spec.py` | new | `[E2E SPEC]` parser; `validate_scenario` (origin, side effect, candidates, source_refs, `covered`); `ground_claims`; `step_selectors`; `navigation_error` |
| `core/evidence/e2e/browser.py` | new | Playwright player: `Session` (actions, candidates, polling, route guard, observer, capture) and `run_scenario` (launch, trace, close) |
| `core/evidence/e2e/player.py` | new | child process; fake modes; non-fake to `browser.run_scenario` |
| `core/evidence/e2e/existing_tests.py` | new | `select`, `build_argv`, `run` (no shell, timeout, scrub) |
| `core/evidence/e2e/classify.py` | new | `classify_step`, `build_report` (artifact, probe, existing test, console enforced) |
| `core/evidence/e2e/normalize.py` | new | `evidence_block` (probe, existing test, artifact), `to_verification` |
| `core/evidence/e2e/redact.py` | new | `scrub_resolved` (encoded variants), `scrub_text_files`, artifact writer |
| `core/evidence/e2e/supervisor.py` | new | JSONL child process, timeout, `terminate_tree` |
| `core/evidence/e2e/preflight.py` | new | `base_url` policy, dependency, reachability, `readiness()` for doctor |
| `install.py`, `installer/settings.py` | changed | `--with-e2e`: pip `requirements-e2e.txt`, then `playwright install chromium` |
| `requirements-e2e.txt` | new | `playwright==1.60.0` |
| `tests/checks/deps.py` | changed | allows guarded `playwright` imports only in the e2e package |
| `tests/checks/installer.py` | changed | `installer-e2e` check |
| `tests/checks/e2e_spec.py`, `e2e_normalize.py`, `e2e_supervisor.py`, `e2e_routing.py` | new | contract, classification, supervisor, orchestration (including grounding, replay, existing test, metrics row) |
| `tests/checks/e2e_browser.py`, `e2e_redact.py`, `e2e_doctor.py`, `e2e_existing_tests.py`, `e2e_metrics.py`, `e2e_smoke.py` | new | player with a substitute page, scrub, doctor, existing test, metrics, opt-in Chromium smoke |
| `tests/fixtures/e2e_app/*.html` | new | fixture application for smoke |
| `tests/run.py`, `tests/scenario.py` | changed | new check registration |
| `tools/sim/sim_flows.py` | changed | S22, S22b, S22c, S22d (replay) |
| `docs/runtime-contracts.md` | changed | `verify_mode=e2e` section |
| `dist/config/claude/skills/verify.md`, `dist/config/claude/CLAUDE.md` | changed | `e2e` mode documentation |
| `dist/manifest.json` | changed | regen |
| `core/evidence/contract.py`, `core/evidence/result_shaping.py`, `core/provider/continuation.py`, `core/evidence/runtime_io.py`, `utils/osutil.py` | unchanged | used as-is |

## 27. Final decision

E2E mode is worth building as a measured experiment because it closes the gap between “the code looks correct” and “the runtime behavior is proven”. The implementation must not immediately cover every browser automation capability.

Initial scope approved for the MVP:

- one browser: Chromium;
- app already running;
- declarative scenario JSON;
- claims and selector provenance;
- basic actions/assertions;
- existing-test-first;
- DOM-first evidence;
- `app/harness/unknown` classification;
- `pass/fail/incomplete`;
- compact hybrid review;
- screenshot/trace as fallback;
- no reasoning-based self-healing;
- D1: three-stage in the runtime (delegated `[E2E SPEC]` → local player → delegated review → normalizer);
- D2: results normalized to canonical `[VERIFICATION]`, existing validator/continuation/guard unchanged;
- D3: Playwright optional extra + lazy import; absent → `incomplete:playwright_missing`;
- E2E artifacts run-local, not in `evidence.jsonl`.

After the MVP is tested on several real changes, the measured data becomes the basis for whether the feature is continued, expanded, or kept opt-in.

## 28. Revision history

| Date | Change |
|---|---|
| 2026-09-14 | Initial draft |
| 2026-09-14 | Revision after analysis against the verify codebase: added §1.1 decisions D1–D3, `verify_mode` fallback note (§4), three-stage architecture + `[E2E SPEC]` contract (§5), canonical normalization + exit code (§14.3), artifact location (§16), reviewer mechanism + `[E2E EVIDENCE]` (§17), dependency policy (§19.1), reuse vs new (§20.1), Phases 1/3/4 made concrete (§21), additional tests (§22), concrete file list (§26) |
| 2026-09-14 | After verification: added §5.1 anti-recursion dispatch rules (internal command `e2e_spec` for stage 1, `_verify_mode_override="delegated"` for stage 3, recursion guard + test), D2 names both quick metadata fields, §21 Phase 1 item 2a, §26 adds `config/routing.py`, `main.py`, concrete docs paths |
| 2026-09-14 | After the 2nd verification: stage 1 output must use the standard exploration evidence format (`[EVIDENCE]` … `[DIGEST]`) with `[E2E SPEC]` as an additional section so it passes the exploration/reasoning generic evidence guard without changing `_EVIDENCE_MARKERS` (§5.1, contract example, Phase 1 item 4); `main.run()` guard for internal commands (§5.1, item 2a) |
| 2026-09-14 | Phase 1 implemented. Decision Q1: stages use `Executor._run_delegated()`/`_build_delegated_prompt()` extracted from `execute()` — `_verify_mode_override` and the recursion guard were dropped because there is no re-entry (§5.1, §21 item 2a, §26). Modules `core/evidence/e2e/{spec,classify,normalize,redact,supervisor,preflight,player,runner}.py`; tests `tests/checks/e2e_{spec,normalize,supervisor,routing}.py`; sim S22/S22b/S22c; `tests/checks/deps.py` only allows guarded `playwright` imports in the e2e package. Real browser player = Phase 2; non-fake runs end in `incomplete: player_unavailable`. |
| 2026-09-14 | Batch 2 verification found 3 blocking issues: (1) resolved `${ENV}` values could enter the stage 3 prompt via player events, (2) the normalizer copied reviewer sections without routing by tag so the verdict differed from the validator, (3) failed stage usage was recorded twice. Fixed: `redact.scrub_resolved` + redact the evidence block before prompt/artifact; `_reviewer_sections` routes by tag + adopts the validator verdict when stricter; `_run_delegated(record_failure=False)` for e2e stages. Regression tests + mutation tests added. Re-verify: DONE. |
| 2026-09-14 | Document status update: header, §1.2 per-phase status, §1.3 open gaps G1–G14 + housekeeping, §23 per-acceptance-criterion status, §25 residual risks, §26 per-file status. Design content §2–§22 unchanged. |
| 2026-09-14 | Implementation batches 1–8: spec policy (origin, side effect, selector candidates, source_refs); packaging `requirements-e2e.txt` + `--with-e2e` + doctor; real Playwright player; artifacts and privacy (scrub of encoded variants, no screenshot/trace if `${ENV}`, `artifact_max_mb`); Chromium smoke and the fix for third-party page error attribution; grounding, replay, existing tests via config; `e2e_run` metrics in `report`; documentation. D1 corrected (review before normalizer), D3 updated (packaging). Batches 1–7 not yet `/.verify`. |
