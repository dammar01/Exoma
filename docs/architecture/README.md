# Architecture

How a request moves through `agent-workflow`, verified against the source tree. The
README image (`docs/assets/architecture.png`) is the illustrated overview; the diagram
below is its text version, and the rest of this page is the authoritative detail (its
steps 1–7 compress the 19 of the delegated path).

## Overview

The developer states the objective and approves the result; the primary agent reasons and
writes code; the runtime owns state, contracts, and execution control; the secondary agent
gathers repository evidence.

```text
                           DEVELOPER
          objective + constraints │  ▲ plan · verdict · questions
                                  ▼  │
┌──────────────────────────────────────────────────────────────┐
│ PRIMARY AGENT                                   Claude Code  │
│ reasoning · decisions · writes code · final review           │
└────────────┬──────────────────────────────────────▲──────────┘
             │ .workflow/run <cmd> <task> <session> │ digest
             ▼                                      │ + file:line anchors
┌───────────────────────────────────────────────────┴──────────┐
│ AGENT-WORKFLOW RUNTIME            deterministic · stdlib     │
│                                                              │
│  1 job · session · lock     ┌─► 5 redact · contract check    │
│  2 route · policy           │   6 digest · anchors · shape   │
│  3 context · prompt ───┐    │   7 persist                    │
└────────────────────────┼────┼────────────────────────┬───────┘
                   task  │    │ raw evidence           │
                         ▼    │                        ▼
┌─────────────────────────────┴─────┐  ┌───────────────────────┐
│ SECONDARY AGENT                   │  │ STATE  .workflow/data │
│ opencode · codex · agy            │  │ facts · evidence      │
│ 4 read · search · trace callers   │  │ usage · audit         │
│ role: read-only                   │  │ provenance: anchors   │
│ enforcement varies by provider    │  │ + hashes              │
└───────────────────────────────────┘  └───────────────────────┘
```

"Read-only" is the secondary agent's role in the contract. How much of it is enforced
depends on the provider: see [Security](../security.md) and
[limitations](../limitations.md).

## Delegated path (explore, plan, analyze, verify)

```text
 PRIMARY AGENT                    RUNTIME                                 SECONDARY AGENT
 ─────────────                    ───────                                 ───────────────
 1 user asks
 2 intent detected
   (hook: intent-gate-set
    writes marker; gate-check
    blocks gather tools)
 3 .workflow/run <cmd> <task> <session_id>
        │
        └──────────► 4  runner ► main.py await --job-command
                     5  submit ► JobManager (job record, heartbeat, recovery)
                        ► detached worker
                     6  main.run: session + runtime lock
                     7  Executor
                     8  Router: command / role / provider / model / timeout
                     9  context (conditional):
                          graph leads   ◄ graphify-out/graph.json
                          facts         ◄ .workflow/data/facts
                          knowledge     ◄ docs/project-knowledge
                    10  PromptBuilder: prompt + sidecars
                    11  adapter from registry ─────────────────────► 12 read / search / trace
                                                                        (optional sub-agent fan-out)
                    13  redact output        ◄───────────────────────   raw [EVIDENCE]+[DIGEST] text
                    14  contract check (shape only)
                          truncated? ► max 1 continuation
                    15  digest extraction + result shaping
                          (heavy meta dropped; content ► preview +
                           content_mode=ref_only when AI_PROXY_SLIM_CONTENT)
                    16  persist:
                          explore/plan/analyze ► facts + evidence (immutable, anchored)
                          all delegated        ► usage + audit
                          progress             ► .workflow/current
                    17  job done ► await returns JSON
        ┌──────────────────┘
 18 read digest
 19 reason, write code, verify
```

The secondary agent returns plain text. `digest` (only when a `[DIGEST]` block parses),
`evidence_ref` (only on the explore/plan/analyze evidence path, step 16), and every
validation result are added by the runtime after the adapter returns.

| Step | Component | Source |
| --- | --- | --- |
| 2 | intent-gate hooks (Claude hook layer, not the Python runtime) | `dist/config/claude/hooks/intent-gate-{set,check}.*` |
| 4 | generated runner | `core/runtime/scripts.py` |
| 5 | job submit, detached worker, await | `core/jobs/job_lifecycle.py`, `core/jobs/job_manager.py` |
| 6 | session and runtime lock | `core/workspace/session_manager.py`, `core/workspace/runtime_lock.py` |
| 7 | Executor | `core/provider/executor.py` |
| 8 | Router | `core/prompt/router.py` |
| 9 | graph leads, fact store, knowledge retrieval | `core/graph/graph_index.py`, `core/evidence/fact_store.py`, `core/knowledge/retrieve.py` |
| 10 | PromptBuilder | `core/prompt/prompt_builder.py` |
| 11 | provider registry and adapters | `adapters/contract/registry.py`, `adapters/providers/*_adapter.py` |
| 13 | redaction | `utils/redact.py` |
| 14 | contract check, continuation | `core/evidence/contract.py`, `core/provider/continuation.py` |
| 15 | digest and result shaping | `core/evidence/contract.py`, `core/evidence/result_shaping.py` |
| 16 | evidence store, runtime I/O, progress mirror | `core/evidence/evidence_store.py`, `core/evidence/runtime_io.py`, `core/workspace/current.py` |

## Side paths

```text
 local command (init/doctor/upgrade/clean/
   inspect/provider/sweep)                  ► main.run                  (before Executor; no job, no await)
 verify, verify_mode=syntax                 ► Executor ► quick_verify   (no Router, no provider)
 verify-browser                             ► Executor ► e2e runner     (Playwright pipeline)
 promote-validate ► promote-verify ► promote-write ► docs/project-knowledge   (manual; write only on production_branch, default main)
 graphify update (external hook, fail-open) ► graphify-out/graph.json
 runtime ► graph-meta.json / graph-stale.json   (runtime only marks staleness)
```

## Storage

| Location | Tracked | Written by | Contents |
| --- | --- | --- | --- |
| `.workflow/data/` | no | runtime | facts, evidence, usage, audit, redactions, graph-meta, graph-stale, sessions |
| `.workflow/current/` | no | runtime | what is running now: session, progress, live browser-run events |
| `storage/jobs/` in the tool checkout (`AGENT_PATH`) | no | JobManager | job records, heartbeat, logs |
| `graphify-out/graph.json` | project choice | external Graphify hook | candidate-file graph, read-only for the runtime |
| `docs/project-knowledge/` | yes | `promote-write` only | verified operational knowledge, injected into exploration and reasoning prompts |

Facts and promoted knowledge are separate systems. A fact enters the fact store after
recurring in 5 distinct other sessions (`fact_recurrence_threshold`, default in
`core/runtime/config_defaults.py`), or directly as a `durable_facts` entry once its anchor
validates — every anchor the claim cites, not only the first. Nothing moves from the fact store into `docs/project-knowledge/` automatically.
