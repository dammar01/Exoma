# Agent-Workflow Research

This directory documents the research, reasoning, experimentation, and design evolution
behind `agent-workflow`.

It does not archive private conversations or confidential project data. It records the
**generalizable reasoning and evidence** that can be safely shared as part of the
open-source project.

How significant changes must be recorded: [CONTRACT.md](CONTRACT.md). The record format:
[schema.yaml](schema.yaml), checked by `tools/maintain/check_research.py`.
How the project treats evidence and AI-assisted reasoning: [methodology.md](methodology.md).
The research boundary, adopted 2026-10-05: [CHARTER.md](CHARTER.md).

## Current thesis and positioning

### Primary research thesis

Since 2026-10-05 the research direction is set by the [charter](CHARTER.md), and this is the
project's one thesis: agent delegation is studied as **resource-aware agent decomposition**,
a resource allocation problem. It is a hypothesis until a controlled experiment tests it
(draft H-016):

> Agent decomposition is not inherently beneficial. Its value depends on the marginal task
> improvement obtained relative to the additional token, latency, human, and coordination
> costs introduced by the decomposition.

Its primary question is RQ-16 (the degree of decomposition that is optimal for a task under
those costs), with RQ-17 (how outcome changes with the number of delegated executions). The
costs are measured one by one and combined only under an experiment's stated normalization and
weights (charter §2). No figure in this repository is an optimal number of agents: the
real-use table by calls per task (CASE-012) is a correlation with task size, not a range.

### Supporting hypotheses

Mechanisms tested inside that thesis. Each is a hypothesis under its own question, not a rival
thesis; one that holds makes a delegated execution worth more, and says nothing by itself about
how many are worth their cost.

- **External exploration** (RQ-01): coding agents may work more effectively on complex tasks
  when repository exploration, evidence acquisition and workflow control are externalized from
  the primary reasoning context. This was the project's working thesis before the charter.
- **Context allocation** (RQ-01; charter CQ5): externalization is context *allocation*, not
  context reduction, and is worth it only if relevant information is not lost.
- **Human orchestration** (RQ-02; charter CQ6 and the human part of CQ4): whether delegation
  reduces intervention, moves it, or adds it through coordination and correction.
- **Latency** (RQ-13; the charter's L and the runtime part of CQ4): externalization costs extra
  execution and waiting (maintainer latency: CASE-004, a draft).
- **Persistent knowledge** (RQ-06): stored repository knowledge may reduce repeated
  exploration.

### Architecture the experiments run on

`agent-workflow` is an external harness, a control plane, for coding agents. It separates
repository-oriented work from the primary agent's reasoning and implementation. Semantic
reasoning stays with the model; deterministic software handles state, policy, contracts,
provenance, and execution control. The secondary agent is an evidence-producing specialist,
not an equally trusted second brain.

The right label is agent harness with a delegated specialist, not a multi-agent
collaboration system: a primary intelligence, a specialized external worker, and a
deterministic control plane. How responsibility should be divided among them is still a
draft (SYN-002, see "Drafts" below).

LIT-001, a harness of harnesses that composes many specialized agents across domains, is kept
as the reference for that broader abstraction; this project does not follow it and stays with
the charter's questions and hypothesis. Feature parity with it is not a goal.

A direction, not a present claim: with its provider abstraction, audit trail, evidence
records, and experiment records (`EXP-XXX`, where benchmark method now lives), the project can
also serve as experimental infrastructure for studying agentic software engineering.

Claims this project does **not** make: that it makes coding agents smarter, that it is a
state-of-the-art coding agent, that the workflow is better for every task, or that any number
of agents is optimal. It is an
engineering exploration of harness architecture, not a proven research result.

Open questions: [questions.md](questions.md). Period summaries: [logs/](logs/).

## Research model

The project evolves through a cycle of observation, question, literature, synthesis,
hypothesis, experiment or implementation, real-world observation, decision, and knowledge,
which in turn raises the next question.

Not every development follows this cycle completely. Some decisions originate from direct
engineering experience, some from literature, and some from synthesis across multiple
sources.

## Evidence sources

Research records may use the following source types:

- `literature` — published papers or other documented research
- `synthesis` — conclusions derived from multiple sources
- `observation` — direct observation from real-world usage
- `experiment` — controlled or semi-controlled evaluation
- `implementation` — evidence produced through actual system implementation
- `discussion` — exploratory reasoning during research
- `unknown` — historical origin cannot be reconstructed reliably

## Provenance

Each research claim or design decision should distinguish its provenance:

- `direct` — directly derived from a known source
- `adapted` — a known source was adapted into the project
- `synthesized` — multiple sources and/or reasoning contributed
- `observed` — primarily derived from real-world observation
- `unknown` — historical origin is no longer known

Unknown provenance is valid. Historical uncertainty must not be replaced with an invented
citation.

## Confidentiality

Private company data, internal project names, source code, credentials, business
information, private transcripts, or other confidential material must not be committed to
this repository.

Real-world observations may be documented only after abstraction and sanitization. The
public record describes the engineering phenomenon, not the confidential environment in
which it occurred.

## Record types

| Directory | ID | Contents |
| --- | --- | --- |
| [literature/](literature/) | `LIT-XXX` | What a source actually claims and how it relates to `agent-workflow` |
| [synthesis/](synthesis/) | `SYN-XXX` | Conclusions produced by combining multiple sources |
| [hypotheses/](hypotheses/) | `H-XXX` | Claims proposed for testing but not yet established |
| [experiments/](experiments/) | `EXP-XXX` | How a hypothesis or design assumption is evaluated |
| [real-cases/](real-cases/) | `CASE-XXX` | Sanitized real-world usage and implementation observations |
| [decisions/](decisions/) | `DEC-XXX` | Architectural or behavioral decisions and their rationale |
| [design-archaeology/](design-archaeology/) | `ARC-XXX` | Historical decisions whose original provenance may be incomplete |
| [questions/](questions/) | `RQ-XX` | Research questions, what exists for each today, and the records serving them |

Every record is YAML. Each directory holds a `_TEMPLATE.yaml`: copy it, name the file
`<ID>-<kebab-slug>.yaml`, fill it in, and run `python tools/maintain/check_research.py`. The
fields, their enums, and the rules the check enforces are in [schema.yaml](schema.yaml) and
[CONTRACT.md](CONTRACT.md) §16. `H` and `EXP` records are external to the code and carry no
commit; an implemented `DEC` names the commit(s) that implement it. A record at schema
version 2 or later names the research question(s) it serves under `questions`, and the `RQ`
lists it back (DEC-017); records tracked at version 1 are frozen without it, and records
tracked at version 2 are frozen at that version (DEC-019).

[questions.md](questions.md) is the index of the `RQ` records, generated like the inventory
below, and [logs/](logs/) holds fixed summaries of research periods that link to the records
they produced.

## Epistemic status

Research statements use explicit status where relevant:

```text
proposed | observed | implemented | validated | partially_validated | rejected | deprecated | unknown
```

Every record states two dimensions separately in its `disposition` block, next to its
type-specific `status` or `outcome`:

```text
implementation_status   not_implemented | partially_implemented | implemented | not_applicable
validation_status       not_validated | observed | partially_validated | validated | rejected
```

Only the maintainer marks a record validated or rejected. The fields, their values, and when the corpus counts as final:
[CONTRACT.md](CONTRACT.md) §14.

A design being implemented does not mean that its underlying hypothesis has been
validated. Likewise, a literature source being related to a design does not imply that the
source directly caused that design.

## Record inventory

Only records that describe something that actually happened stay here. Everything else is
a draft (see below).

Generated from the records by `python tools/maintain/check_research.py --write-inventory`;
the check fails when it is stale. Research questions per record: [questions.md](questions.md).

<!-- research-inventory:begin (generated by tools/maintain/check_research.py --write-inventory) -->
| ID | Title | Type status | Implementation | Validation | Commit |
| --- | --- | --- | --- | --- | --- |
| [CASE-005](real-cases/CASE-005-verify-browser-drafts-never-ready.yaml) | `/.verify-browser` drafts never reached `ready` | negative | implemented | observed | — |
| [CASE-006](real-cases/CASE-006-latency-and-cached-input-in-maintainer-use.yaml) | Latency by project and cached input share in maintainer use | inconclusive | implemented | observed | — |
| [CASE-007](real-cases/CASE-007-accumulating-one-byte-lock-guards.yaml) | One-byte lock guards accumulate and look like a defect | mixed | implemented | observed | — |
| [CASE-008](real-cases/CASE-008-graph-refresh-hook-cost-on-two-projects.yaml) | On two projects whose source differs sevenfold, the graph-refresh hook scan costs about 2 s on each, and graphify update fails slowly on the large graph | mixed | implemented | observed | — |
| [CASE-009](real-cases/CASE-009-verify-rarely-passes-and-evidence-is-never-reused.yaml) | Across fourteen projects, 2% of verify rows pass and no command is served from reused evidence | negative | implemented | observed | — |
| [CASE-010](real-cases/CASE-010-verify-latency-spread-and-graph-lookup-cost.yaml) | Verify latency varies by up to 3x its median within a project, and the graph lookup grows with node count to 9 s | mixed | implemented | observed | — |
| [CASE-011](real-cases/CASE-011-real-use-baseline-of-3-7-3.yaml) | Real-use baseline of 3.7.3 — prompts, corrections, changes, and time per session | inconclusive | implemented | observed | 414d073 |
| [CASE-012](real-cases/CASE-012-real-use-task-outcomes-on-nine-projects.yaml) | Real-use task outcomes on nine projects — what reached main, what needed a fix, and what it cost | mixed | implemented | observed | — |
| [CASE-013](real-cases/CASE-013-runtime-intent-gate-never-armed.yaml) | The runtime half of the pre-flight gate never armed — its hook read a prompt field Claude Code does not send | negative | implemented | observed | 8d2d42b |
| [CASE-014](real-cases/CASE-014-provider-less-browser-runs-counted-as-verifies.yaml) | Browser runs that never reached a provider were recorded as verify usage rows — 197 of 836 in the CASE-009 window | negative | implemented | observed | — |
| [CASE-015](real-cases/CASE-015-codex-windows-sandbox-fails-on-thread-resume.yaml) | Codex on Windows failed a resumed verify at its elevated sandbox, and the runtime called it `unknown` | negative | implemented | observed | 8d2d42b |
| [CASE-016](real-cases/CASE-016-codex-resume-sandbox-regression-bisect.yaml) | Codex resumes a thread on Windows through 0.154.0 and fails every resume from 0.155.0 to 0.160.0 | negative | implemented | observed | — |
| [CASE-017](real-cases/CASE-017-verify-browser-usage-recount-after-dec-022.yaml) | Verify-browser usage is one row per provider invocation after DEC-022; no miscount found, and no real run yet to confirm it | positive | implemented | observed | 8d2d42b |
| [CASE-018](real-cases/CASE-018-real-use-execute-without-plan-or-approval.yaml) | Real use of 3.8.1 — feature requests executed without a plan, plan answers taken as approval | negative | implemented | observed | 4a09c04 |
| [CASE-019](real-cases/CASE-019-codex-writes-through-apply-patch-under-read-only-sandbox.yaml) | codex wrote a file through apply_patch under --sandbox read-only | negative | implemented | observed | 895aa6f |
| [CASE-020](real-cases/CASE-020-headless-simulation-of-dec-046-scenarios.yaml) | Headless simulation of DEC-046 scenarios A, C and D — routing holds, delegated calls cannot finish | inconclusive | implemented | observed | 58db760, 895aa6f |
| [DEC-001](decisions/DEC-001-verify-browser-delegated.yaml) | `/.verify-browser` is a delegated command | accepted | implemented | validated | dd70afd |
| [DEC-004](decisions/DEC-004-data-e2e-only-test-attribute.yaml) | `data-e2e` is the only test attribute | accepted | implemented | observed | dd70afd |
| [DEC-005](decisions/DEC-005-repeat-brake-releases-on-change.yaml) | The repeat brake releases on a project change and is remembered as knowledge | accepted | implemented | observed | dd70afd |
| [DEC-006](decisions/DEC-006-stage-one-repair-continuation.yaml) | A stage-1 spec that fails validation gets one repair in the same thread | accepted | implemented | observed | dd70afd |
| [DEC-007](decisions/DEC-007-poll-waits-inside-playwright.yaml) | The browser player's poll waits inside Playwright, not beside it | accepted | implemented | validated | dd70afd |
| [DEC-008](decisions/DEC-008-app-repeat-streak-keyed-by-failure-signature.yaml) | An app repeat streak is keyed by failure signature, and a runtime change releases it | accepted | implemented | observed | dd70afd |
| [DEC-009](decisions/DEC-009-literal-credentials-scrubbed-by-lookup.yaml) | Literal credentials in the task, draft and request are found by value and put back to placeholders | accepted | implemented | observed | c584748 |
| [DEC-010](decisions/DEC-010-scenario-origin-and-structured-diagnosis.yaml) | Scenario-caused failures get their own origin, stay out of the repeat brake, and every non-pass carries a structured diagnosis | accepted | implemented | validated | c584748 |
| [DEC-011](decisions/DEC-011-research-records-yaml-changelog-split-bench-removed.yaml) | Research records are schema-checked YAML, release notes live per version, and bench/ is retired | accepted | implemented | validated | 665bb4c, c584748 |
| [DEC-012](decisions/DEC-012-graph-refresh-hook-ownership-and-timing.yaml) | The Stop hook is the only graph-refresh trigger, prunes before it walks, and records its cost with project size | accepted | implemented | observed | 98449fe |
| [DEC-013](decisions/DEC-013-safe-orphan-guard-cleanup.yaml) | Document the one-byte lock guards, and let `clean` remove those of dead sessions | accepted | implemented | not_validated | 2dd7d7e |
| [DEC-014](decisions/DEC-014-verify-browser-network-metadata.yaml) | verify-browser records ordered request metadata, compared across runs, never bodies or headers | accepted | implemented | not_validated | 60263ba |
| [DEC-015](decisions/DEC-015-tasks-as-skill-sequences-closed-by-commit.yaml) | A task is a recorded sequence of skill calls, completed by a commit of its edits after a DONE verify | accepted | implemented | not_validated | 2dd7d7e |
| [DEC-016](decisions/DEC-016-proven-selectors-outrank-role-and-label.yaml) | Proven selectors — source data-e2e and knowledge-store matches — outrank role and label | accepted | implemented | not_validated | f6edbef |
| [DEC-017](decisions/DEC-017-research-questions-as-yaml-records.yaml) | Research questions become YAML records, linked to records both ways | accepted | implemented | observed | 7150b0d |
| [DEC-018](decisions/DEC-018-verify-none-sentinel-grammar.yaml) | A verify section that says nothing was found is read as empty in the spellings agents write, and the re-prompt names `- none` | accepted | implemented | not_validated | cb7ee4e, 23250f1 |
| [DEC-019](decisions/DEC-019-binary-questions-and-measured-validation.yaml) | Research questions are answered or unanswered, and a validated decision is used, consistent, decided, and measured | accepted | implemented | not_validated | 72c2ac7 |
| [DEC-020](decisions/DEC-020-task-closed-by-derived-pass.yaml) | A task is closed by a verify the runtime derives as pass; a commit is recommended, not required | accepted | implemented | not_validated | 72c2ac7 |
| [DEC-021](decisions/DEC-021-task-events-at-turn-boundaries.yaml) | Task events are read at turn boundaries — skills from the prompt, commits from HEAD — not from every Read and Bash call | accepted | implemented | not_validated | 8d2d42b |
| [DEC-022](decisions/DEC-022-browser-runs-without-a-provider-write-no-usage-row.yaml) | A browser run that reached no provider writes no usage row; browser rows name their stage and the browser's own time | accepted | implemented | not_validated | 8d2d42b |
| [DEC-023](decisions/DEC-023-bundle-scoped-to-its-commands.yaml) | The installed bundle stays out of the user's own configuration — command-only scope, provider-call scope, block-only writes, uninstall | accepted | implemented | not_validated | 8d2d42b |
| [DEC-024](decisions/DEC-024-codex-sandbox-refusal-fails-named.yaml) | A codex Windows sandbox refusal fails as `sandbox_unavailable` with ordered fix steps; the runtime never lowers the sandbox | accepted | implemented | not_validated | 7351846 |
| [DEC-025](decisions/DEC-025-verify-tests-chosen-by-main-agent-run-by-runtime.yaml) | Verify tests are chosen by the main agent and run by the runtime; the second agent only reads and traces | accepted | implemented | observed | 3a378c7 |
| [DEC-026](decisions/DEC-026-benchmark-calculation-contract.yaml) | Benchmark figures follow a versioned metric registry and carry a provenance stamp; recounting is maintainer-only | accepted | implemented | not_validated | 1301a3e |
| [DEC-027](decisions/DEC-027-benchmark-tools-for-every-user.yaml) | Any user measures their own use with the offline tools; a sanitized export may leave the machine, and a usage row belongs to one transcript | accepted | implemented | not_validated | 3cf2858, b4d7ad6 |
| [DEC-028](decisions/DEC-028-provider-stable-releases.yaml) | Each provider names the CLI release the workflow was tested on; any other release is recorded and warned about, never blocked | accepted | implemented | not_validated | 23bf6e7 |
| [DEC-029](decisions/DEC-029-verify-test-request-hardening.yaml) | The verify test request cannot forge the verdict, a timed-out test cannot hang the run, and the gate lets the main agent choose tests | accepted | implemented | not_validated | b1badd7, b4d7ad6 |
| [DEC-030](decisions/DEC-030-hook-and-installer-failure-paths.yaml) | Hooks and the installer fail without losing events, mislabelling a graph, or orphaning hooks | accepted | implemented | not_validated | 0513baa |
| [DEC-031](decisions/DEC-031-proven-selector-misses-only-when-tried.yaml) | A proven selector misses only when the browser tried it; a missing proven selector is grounded | accepted | implemented | not_validated | 2d970d1 |
| [DEC-032](decisions/DEC-032-gate-parses-runner-calls.yaml) | The pre-flight gate parses runner calls, covers the PowerShell tool, and refuses writes to the runner scripts | accepted | implemented | observed | 3e50003 |
| [DEC-033](decisions/DEC-033-verify-runtime-gaps-are-the-runtimes.yaml) | A runtime test gap exits nonzero, a no-test request is not a check, and runtime tests keep the job alive | accepted | implemented | observed | 77a6f86 |
| [DEC-034](decisions/DEC-034-provider-release-on-every-row.yaml) | Every usage row names its provider release, the version is cached per user, and codex sandbox refusals are read from stderr | accepted | implemented | observed | 77a6f86 |
| [DEC-035](decisions/DEC-035-proven-selectors-scoped-by-route.yaml) | Proven selectors are scoped by route and within, weakened once per run, and recorded only from runs that passed | accepted | implemented | observed | 0c387f9 |
| [DEC-036](decisions/DEC-036-export-allowlist-and-commit-claims.yaml) | A shared export passes an allowlist of value shapes, and a commit is taken only by the task that claims it | accepted | implemented | not_validated | 93a01d2 |
| [DEC-037](decisions/DEC-037-hook-ownership-by-path.yaml) | The installer owns a hook by the folder its script sits in, and graph-refresh keeps its lock honest | accepted | implemented | not_validated | 687a702 |
| [DEC-038](decisions/DEC-038-export-names-at-any-length-and-label-keys.yaml) | An export checks project names at any length, and keys under a per-project map must be project labels | accepted | implemented | not_validated | 56c2a64 |
| [DEC-039](decisions/DEC-039-gate-lets-skill-definitions-be-read.yaml) | The pre-flight gate lets a skill definition under ~/.claude/skills be read while a delegation is pending | accepted | implemented | observed | 56c2a64 |
| [DEC-040](decisions/DEC-040-proven-selector-route-folds-identifiers.yaml) | A proven selector's route folds identifier segments to :id | accepted | implemented | not_validated | 56c2a64 |
| [DEC-041](decisions/DEC-041-check-asks-only-the-project-providers-boundary.yaml) | install --check asks only for the project boundary of the provider the project runs | accepted | implemented | observed | 8dcc672 |
| [DEC-042](decisions/DEC-042-graphify-runs-unbounded-under-a-beating-lock.yaml) | graphify runs without a time limit, and the refresh lock is held while its worker beats | accepted | implemented | not_validated | f23fe24 |
| [DEC-043](decisions/DEC-043-gate-lets-a-precision-read-through.yaml) | The pre-flight gate lets two small project reads through per delegation | accepted | implemented | not_validated | a06846a |
| [DEC-044](decisions/DEC-044-side-effect-none-is-not-a-write.yaml) | Browser knowledge reads side_effect "none" as no write, so explicit drafts record selectors | accepted | implemented | observed | e9c63d7 |
| [DEC-045](decisions/DEC-045-task-notifications-do-not-arm-the-gate.yaml) | A background task's notification neither arms nor clears the pre-flight gate | accepted | implemented | not_validated | 2110262 |
| [DEC-046](decisions/DEC-046-new-behavior-goes-through-plan-and-only-the-user-approves-execution.yaml) | New behavior goes through plan, and only the user's execute approves execution | accepted | implemented | not_validated | 58db760 |
| [DEC-047](decisions/DEC-047-key-pattern-needs-a-left-boundary.yaml) | The openai-style key pattern needs a left boundary | accepted | implemented | not_validated | 58db760 |
| [DEC-048](decisions/DEC-048-provider-write-boundary-declared-and-codex-approval-never.yaml) | Providers declare their enforced write boundary; codex runs with approval_policy never | accepted | implemented | observed | 895aa6f |
| [DEC-049](decisions/DEC-049-provider-thread-kept-or-loss-named-and-integrity-fails-closed.yaml) | A provider thread id is kept or its loss is named; integrity paths fail closed | accepted | implemented | not_validated | 895aa6f |
| [DEC-050](decisions/DEC-050-every-writer-retries-the-replace-and-a-dead-worker-frees-its-session.yaml) | Every temp-and-replace writer retries the replace; a dead worker frees its session for a different request | accepted | implemented | not_validated | c46d0ac |
| [LIT-001](literature/LIT-001-raven-harness-of-harnesses.yaml) | Raven: The Harness of Harnesses for Composable Agentic Intelligence | — | not_applicable | not_validated | — |
<!-- research-inventory:end -->

No `H-XXX`, `EXP-XXX`, or `SYN-XXX` record is tracked: none has been run or has a result.

### Drafts

Proposed hypotheses, planned experiments, syntheses, and decisions without a result are
kept in `docs/research-drafts/` (same subfolders). That directory is git-ignored and exists
only on the maintainer's machine; tracked records mention draft IDs as plain text, never as
links. A draft moves back here only when it has been carried out and has a recorded result.
Drafts currently held (2026-10-05): H-004, H-008..H-016 and EXP-004..EXP-009 (YAML, each
naming its research question; H-016 is the charter's CH1), CASE-004 (moved back on
2026-09-28: the records it relates to are all drafts), and H-005, EXP-002, CASE-002, SYN-002,
DEC-002 (Markdown, still referenced).
H-001..H-003, H-006, H-007, EXP-001, EXP-003, CASE-001, CASE-003, SYN-001 and DEC-003 are
kept in `docs/research-drafts/_archive/`, which the check does not read. Drafts use the
same YAML format; the Markdown ones are listed by `check_research.py --drafts` until
converted. A draft returns here only as a YAML record that passes the check.

LIT-001 (2026-10-05) is the first literature record: a reference for a broader agent
abstraction that this project does not follow, recorded from its verified arXiv metadata and
abstract. Papers remembered during earlier research were not recorded because their metadata
was not available ([logs/2026-09-26.md](logs/2026-09-26.md), "Deliberately not recorded").
No `ARC-XXX` record exists yet: no historical decision has been reconstructed as one.

## Relationship to other documentation

- `docs/research/` is a human-readable research record. It is **not** read by the runtime.
- `docs/project-knowledge/` is where `promote-write` stores verified operational knowledge,
  and the runtime injects it into exploration and reasoning prompts. Research records never go there
  directly; see "Knowledge promotion" in [CONTRACT.md](CONTRACT.md).
- `prompt/` holds historical release notes and prompt snapshots up to v3.7.2; later release
  notes are in [docs/releases/](../releases/).
- Benchmark method is recorded as `H-XXX` + `EXP-XXX` records. The earlier frozen harness
  (`bench/`, SUT v3.4.5) was removed in 3.7.3 (DEC-011) without having produced results;
  see [docs/evaluation/benchmark.md](../evaluation/benchmark.md).

## Principle

The project should preserve not only **what was built**, but also why it was built, what
informed it, what evidence supported it, what happened in practice, and what remained
uncertain.
