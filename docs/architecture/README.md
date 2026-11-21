# Architecture

How Exoma currently works, verified against the source tree and test suite.

- **Illustrated overview:** [`docs/assets/architecture.svg`](../assets/architecture.svg) (used
  in `README.md`).
- **Philosophy & conceptual boundaries:** [docs/philosophy.md](../philosophy.md).
- **Authoritative keys, flags, and contracts:** [docs/reference.md](../reference.md) and
  [docs/runtime-contracts.md](../runtime-contracts.md).

Diagrams below are explicitly labeled **`[Implementation]`** (verified against current code).

---

## 1. System overview `[Implementation]`

The developer defines the objective and approves execution; the primary agent reasons and edits
code; the Exoma runtime owns deterministic state, contracts, evidence persistence, and
governance; the secondary agent gathers bounded repository evidence.

```text
                           DEVELOPER (Human)
          objective + constraints │  ▲ plan · verdict · questions
                                  ▼  │
┌────────────────────────────────────────────────────────────────┐
│ PRIMARY AGENT            Claude Code · Agy · Codex · OpenCode  │
│ reasoning · decisions · writes code · final review             │
└────────────┬────────────────────────────────────────▲──────────┘
             │ .workflow/run <cmd> --session ...      │ digest
             ▼                                        │ + file:line anchors
┌─────────────────────────────────────────────────────┴──────────┐
│ EXOMA RUNTIME                     deterministic · stdlib only  │
│                                                                │
│  1 job · session · lock     ┌─► 5 redact · contract check      │
│  2 route · policy           │   6 digest · anchors · shape     │
│  3 context · prompt ───┐    │   7 persist                      │
└────────────────────────┼────┼──────────────────────────┬───────┘
                   task  │    │ raw candidate evidence   │
                         ▼    │                          ▼
┌─────────────────────────────┴─────┐  ┌─────────────────────────┐
│ SECONDARY AGENT                   │  │ STATE  .workflow/data/  │
│ opencode · codex · agy · claude   │  │ sessions · facts        │
│ 4 read · search · trace callers   │  │ evidence · e2e-knowledge│
│ role: read-only                   │  │ usage · audit           │
│ enforcement varies by provider    │  │ provenance: anchors +   │
└───────────────────────────────────┘  │ hashes                  │
                                       └─────────────────────────┘
```

### Hosts and boundary enforcement

The primary agent runs inside one of four supported host CLIs — **Claude Code**, **Agy**,
**Codex**, or **OpenCode** (`src/exoma/installer/base.py`, `src/exoma/_bundle/config/hosts/`).
All four host integrations delegate policy decisions to one shared module, `workflow_policy.py`
(what each host can actually enforce is listed in [reference.md — Hosts](../reference.md#hosts)).

Agy, Codex, Claude, and OpenCode also have separate secondary-provider bundles
(`src/exoma/config/providers.py`). When the runtime spawns a secondary agent, it marks the
child process with `WORKFLOW_AGENT_ROLE=second_agent` so host hooks skip primary-agent gating
and block recursive delegation; the prompt's `[WORKFLOW_AGENT]` header is only a model cue.

"Read-only" is the secondary agent's contract role — actual write/read enforcement varies by
provider. See [Security](../security.md) and [Known limitations](../limitations.md).

---

## 2. Delegated evidence path (`explore`, `plan`, `analyze`, `verify`) `[Implementation]`

```text
 PRIMARY AGENT                    RUNTIME                                 SECONDARY AGENT
 ─────────────                    ───────                                 ───────────────
 1 user asks
 2 intent detected
   (host hook: intent-gate-set
    writes marker; gate-check
    blocks bulk gather tools)
 3 .workflow/run <cmd> --session <id> --task <task>
        │
        └──────────► 4  runner ► exoma await --job-command
                     5  submit ► JobManager (job record, heartbeat, recovery)
                        ► detached worker
                     6  main.run: session + runtime lock
                     7  Executor
                     8  Router: command / role / provider / model / timeout
                     9  context (conditional, separate retrieval paths):
                          graph leads   ◄ graphify-out/graph.json
                          facts         ◄ .workflow/data/facts.jsonl
                          knowledge     ◄ docs/project-knowledge/
                    10  PromptBuilder: prompt + sidecars
                    11  adapter from registry ─────────────────────► 12 read / search / trace
                                                                        (optional sub-agent fan-out)
                    13  redact output        ◄───────────────────────   raw [EVIDENCE]+[DIGEST] text
                    14  contract check (shape only)
                          truncated? ► max 1 continuation
                    15  digest extraction + result shaping
                          (heavy meta dropped; content ► preview +
                           content_mode=ref_only when EXOMA_SLIM_CONTENT)
                    16  persist:
                          explore/plan/analyze ► facts + evidence (immutable, anchored)
                          all delegated        ► usage + audit
                          progress             ► .workflow/current/
                    17  job done ► await returns JSON envelope
        ┌──────────────────┘
 18 read digest (verify critical anchors directly)
 19 reason, write code, verify
```

The secondary agent returns plain text. `digest` (when a `[DIGEST]` block parses),
`evidence_ref` (on the `explore`/`plan`/`analyze` evidence path), anchor certification, and
contract validation are produced by the runtime after the adapter returns.

| Step | Component | Source |
| --- | --- | --- |
| 2 | Host intent-gate & policy hooks (Claude Code, Agy, Codex, OpenCode) | `src/exoma/_bundle/config/hosts/{claude,agy,codex,opencode}/`, `workflow_policy.py` |
| 4 | Generated runner (`run.sh` / `check.sh` / `inspect.sh`) | `core/runtime/scripts.py` |
| 5 | Job submit, detached worker, heartbeat, recovery, await | `core/jobs/job_lifecycle.py`, `core/jobs/job_manager.py` |
| 6 | Session isolation and runtime lock | `core/workspace/session_manager.py`, `core/workspace/runtime_lock.py` |
| 7 | Executor | `core/provider/executor.py` |
| 8 | Router | `core/prompt/router.py` |
| 9 | Graph leads, fact store, knowledge retrieval | `core/graph/graph_index.py`, `core/evidence/fact_store.py`, `core/knowledge/retrieve.py` |
| 10 | PromptBuilder | `core/prompt/prompt_builder.py` |
| 11 | Provider registry and adapters | `adapters/contract/registry.py`, `adapters/providers/*_adapter.py` |
| 13 | Redaction | `utils/redact.py` |
| 14 | Contract check and continuation | `core/evidence/contract.py`, `core/provider/continuation.py` |
| 15 | Digest extraction and result shaping | `core/evidence/contract.py`, `core/evidence/result_shaping.py` |
| 16 | Evidence store, runtime I/O, progress mirror | `core/evidence/evidence_store.py`, `core/evidence/runtime_io.py`, `core/workspace/current.py` |

### Operational continuity vs. problem-solving continuity

Job lifecycle recovery (`core/jobs/`), session persistence (`core/workspace/session_manager.py`),
and runtime locks provide **operational continuity** — surviving timeouts, detached workers,
and concurrent sessions without corrupting workspace state.

Operational continuity does **not** automatically establish **problem-solving (semantic)
continuity**. Resuming a provider session or attaching to a running job does not guarantee that
an agent retained full semantic understanding of the task or every earlier trade-off.

---

## 3. Execution governance and verification `[Implementation]`

Code changes stay with the primary agent, governed by a deterministic execution lifecycle
(`core/runtime/execution.py`) and runtime-executed test verification
(`core/evidence/verify_tests.py`).

<p align="center">
<img src="../assets/execution-governance.svg" alt="Implementation diagram of execution governance and verification: plan-record validates proceed and snapshots base_tree; execute-begin requires a proceed plan and single-use -y token and opens the scope gate; Primary Agent edits with host hooks guarding supported file-write tools (enforcement varies by host; shell writes are not blocked by hooks); execute-end snapshots end_tree, flags outside_scope including shell edits, and closes with status implemented and verification not_run; verify-prepare and delegated verify combine review, allowlisted runtime tests, and fingerprinted reuse into a derived pass, fail, or incomplete verdict." width="900">
</p>
<sub><strong>[Implementation]</strong> Verified against <code>core/runtime/execution.py</code> and <code>core/evidence/verify_tests.py</code>.</sub>

Key implementation behaviors:

- **Plan and approval binding:** `plan-record` refuses `decision: proceed` when open questions
  exist, `solution_path < high`, `files` is empty, or `[OPTIONS]` violates bounds.
  `execute-begin` requires a recorded `proceed` plan plus a single-use approval token written
  by the host prompt hook from the user's `/.execute -y` turn.
- **Scope snapshots:** `plan-record`, `execute-begin`, and `execute-end` snapshot the Git
  working tree (`core/evidence/tree_fingerprint.py`) so the runtime can isolate `changed_files`
  and `outside_scope` edits (including shell edits that file-write hooks do not intercept).
  When Git snapshots are unavailable, scope comparison reports `null` (unknown), never "clean".
- **Selection tiers & obligations:** execution records track test selection tiers (`targeted`,
  `integration`, `full`, `none`) and obligations (`verify` vs. `release`).
- **Runtime test execution & reuse:** during delegated `/.verify`, the secondary agent reviews
  code without running tests; the runtime runs the primary agent's allowlisted commands from
  `verify/tests.json` (no shell syntax, per-command timeout) and merges outcomes into
  `[VERIFICATION]`. Unmet obligations or refused/missing requests become `runtime_gap` items
  (failing closed). Previously passed runs executed via `.workflow/run.sh test` are reused only
  when the exact command text matches the latest passing record in the same session under an
  unchanged `reuse_identity` (Git-normalized tree, `.workflow/config.json`, listed env vars,
  and listed ignored files).
- **Planned ≠ implemented ≠ verified:** `execute-end` closes with `status: implemented` and
  `verification: not_run`. Only a completed verification updates the verdict, and `pass`
  requires zero blocking findings and zero unchecked/gap items.

---

## 4. Browser verification and bounded browser experience `[Implementation]`

`/.verify-browser` (`core/evidence/e2e/runner.py`) separates scenario drafting, browser
execution, and evidence review across three stages. The `auto` phase runs all three stages
under one runtime lock; a standalone `draft` phase stops after Stage 1 before launching a
browser, and an incomplete Stage 2 browser run skips Stage 3 delegated review:

<p align="center">
<img src="../assets/browser-verification.svg" alt="Implementation diagram of /.verify-browser: Preflight and request gate checks pinned e2e config; Stage 1 secondary agent drafts E2E SPEC using origin-scoped browser knowledge hints (standalone draft phase stops here); Stage 2 supervised Playwright player runs with origin, write, and read-only guards plus redaction; Stage 3 classifies four failure origins (app, harness, scenario, unknown) and normalizes canonical verification (skipped when Stage 2 is incomplete); Bounded Browser Experience (.workflow/data/e2e-knowledge.jsonl) records clean first-attempt proof and failure hints with placeholders only." width="900">
</p>
<sub><strong>[Implementation]</strong> Verified against <code>core/evidence/e2e/{runner,classify,knowledge}.py</code>.</sub>

- **Four failure origins (`core/evidence/e2e/classify.py`):** `app` (application broke claim),
  `harness` (runtime/environment problem; eligible for bounded retry when side effects are
  disabled), `scenario` (drafted selector/navigation error; not retried or counted by the
  repeat brake), and `unknown` (never silently downgraded to `harness`).
- **Bounded browser experience (`core/evidence/e2e/knowledge.py`):** persists origin-scoped
  `auth.login`, `auth.logout`, `navigation`, `page_ready`, `selector`, `failure_hint`, and
  `repeat_failure` entries in `.workflow/data/e2e-knowledge.jsonl`. Positive entries require a
  passing step on a clean first attempt; literal typed values are dropped (placeholders such as
  `${E2E_USER}` only); two consecutive failures retire an entry; source anchors are rechecked
  and pruned; entries with 3 passes and a valid source anchor can be offered to `/.promote`.

---

## 5. Side paths `[Implementation]`

```text
 local command (init/doctor/upgrade/clean/
   inspect/provider/sweep/verify-prepare)   ► main.run                  (before Executor; no job, no await)
 verify, verify_mode=syntax                 ► Executor ► quick_verify   (local parse check; no provider, no tests)
 promote-validate ► promote-verify ► promote-write ► docs/project-knowledge   (manual; write gated to production_branch)
 graphify update (external hook, fail-open) ► graphify-out/graph.json
 runtime ► graph-meta.json / graph-stale.json   (runtime only marks staleness)
```

---

## 6. Storage and persistence boundaries `[Implementation]`

| Location | Git-tracked | Written by | Contents & role |
| --- | --- | --- | --- |
| `.workflow/data/sessions/<id>/` | No | Runtime | **Task state:** session metadata, recorded plans, active execution state, verify requests, per-run logs |
| `.workflow/data/evidence.jsonl` | No | Runtime | **Evidence index:** artifact paths and exact line-anchor hashes pointing to immutable raw output artifacts in `.workflow/data/sessions/<id>/logs/` |
| `.workflow/data/facts.jsonl` | No | Runtime | **Project facts:** anchored recurring claims (default: 5 other sessions) or `durable_facts`; checked for freshness at ingest and read |
| `.workflow/data/e2e-knowledge.jsonl` | No | Runtime | **Bounded browser experience:** origin-scoped verified browser flows, selectors, and failure hints (placeholder-only, no credentials) |
| `.workflow/data/` streams | No | Runtime | Usage, quality, audit, task events, task-hook diagnostics, redactions, graph staleness metadata |
| `.workflow/current/` | No | Runtime | Live execution progress and browser-run event mirror |
| `<EXOMA_HOME>/storage/jobs/` | No | JobManager | Per-user async job records, heartbeats, and worker logs |
| `graphify-out/graph.json` | Project choice | External Graphify hook | Candidate-file graph (read-only leads for the runtime; never authoritative proof) |
| `docs/project-knowledge/` | Yes | `promote-write` only | **Project knowledge:** manually reviewed operational docs, checked for schema, branch applicability, and claim freshness before injection |

**Strict separation between stores:**

- **Evidence reuse vs. fact retrieval:** whole-artifact reuse (`evidence_store.py`) requires an
  exact command/task/context match with every anchor still at its recorded line; fact retrieval
  (`fact_store.py`) ranks individual claims and can relocate a shifted line while dropping
  vanished anchors.
- **Facts & browser knowledge vs. promoted knowledge:** nothing moves automatically from
  `.workflow/data/facts.jsonl` or `.workflow/data/e2e-knowledge.jsonl` into
  `docs/project-knowledge/`. Promotion is explicit, reviewed, and branch-gated.
