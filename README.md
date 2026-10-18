<div align="center">

# agent-workflow

**An external control plane for coding agents.**

Delegate repository exploration, evidence gathering, and workflow control to a secondary
agent, while reasoning and implementation stay with your primary coding agent.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](docs/installation.md#requirements)
[![Version](https://img.shields.io/badge/version-3.8.4-informational.svg)](CHANGELOG.md)

**[Installation guide](docs/installation.md)** · [Security](docs/security.md) · [Team guide](docs/team-guide/README.md) · [Reference](docs/reference.md)

<br>

<img src="docs/assets/architecture.png" alt="agent-workflow architecture. The developer briefs the primary agent (Claude Code) with an objective and constraints and receives the plan, verdict, and questions back. The primary agent delegates to the runtime control plane, which handles job, session, and lock, routing, context, and prompt, then sends a bounded task to the secondary agent. The secondary agent reads, searches, and traces the repository and returns candidate evidence; its intended role is read-only, and enforcement varies by provider. The runtime redacts it, checks the contract, shapes a digest with file:line anchors for the primary agent, and reads and writes sessions, facts, evidence, usage, audit, and response artifacts in .workflow/data/ with source anchors and hashes. Evidence candidates are not ground truth." width="900">

</div>

<sub>The primary agent reasons and writes the code; the developer answers open questions and
approves implementation. "Read-only" is the secondary agent's role, and how strictly it is
enforced depends on the provider (see [Security](docs/security.md)). Text version, full verified
flow, side paths, and storage: [docs/architecture](docs/architecture/README.md).</sub>

---

## What is agent-workflow?

`agent-workflow` is an external runtime for AI-assisted software development. It separates
work that needs **broad repository inspection** from work that needs the **primary agent's
reasoning and implementation context**.

A coding agent on a mature repository spends much of its effort locating modules, tracing
call sites, and reading conventions before it changes anything. `agent-workflow` moves that
inspection into a separate execution context and returns structured evidence: a digest,
`file:line` anchors, and a reference to the full evidence on disk.

It is **not a replacement** for Claude Code or any other coding agent. It is an additional
control layer around them.

Delegated results are treated as **evidence candidates**, not truth. Every delegated call
returns the same envelope; `digest` appears when the secondary agent's summary parses, and
`evidence_ref` on the explore/plan/analyze evidence path:

```json
{ "ok": true, "content": "...", "meta": {}, "digest": {}, "evidence_ref": {} }
```

The primary agent decides what the evidence means and whether to act on it. A secondary
agent can be incomplete or wrong even when its output looks plausible.

---

## Core responsibilities

| Role | Who | Responsibility |
| --- | --- | --- |
| **Primary agent** | Claude Code (the integration target) | Reasoning, decisions, code changes, and final review |
| **Workflow runtime** | This repository, Python standard library only | Routing, sessions and locks, policy, prompt building, redaction, evidence contracts, digests, persistence, recovery |
| **Secondary agent** | `opencode`, `codex`, or `agy` | Repository exploration, analysis, planning, and verification evidence |

The design keeps code-writing with the primary agent. The secondary agent's role is
read-only; the degree to which each provider **enforces** that differs, and the runtime
states it rather than assuming it. See [Security](docs/security.md).

The principle: let the model do the semantic reasoning, and let deterministic software
enforce state, contracts, policy, provenance, and execution control.

---

## When it is useful

Measured on the maintainer's real use: 97 coding tasks from 167 Claude Code sessions on nine
private projects (Laravel, Next.js and Python codebases), over the two most used stable
releases. A task is solved when the code its session left behind entered `main`, and clean when the
developer did not change that code again within 24 hours.

| Delegated calls per task | Tasks | Solved | Clean | Prompts (median) | Active time (median) | Main-agent final context | Context kept out of the main agent |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 15 | 8 (53%) | 8 | 5 | 19 min | 108k | 0 |
| 1–2 | 20 | 14 (70%) | 12 | 6 | 33 min | 152k | 170k |
| 3–5 | 28 | 23 (82%) | 15 | 9 | 53 min | 201k | 671k |
| 6+ | 34 | 23 (68%) | 12 | 16.5 | 101 min | 354k | 1.4M |
| **All** | **97** | **68 (70%)** | **47 (48%)** | **9** | **53 min** | **210k** | **574k** |

Features were solved in 46 of 68 tasks and bugs in 19 of 26. Larger tasks drew more calls,
so the table shows where delegation was used, not what it caused; it is one developer, with
no run without the workflow to compare against, and acceptance on `main` is not a correctness
check. Per release, per project, prompt kinds and method:
[real-use benchmark](docs/evaluation/real-use-benchmark.md).

What those tasks looked like, from the same sessions (translated, names removed):

| Situation | What was asked | Command |
| --- | --- | --- |
| A feature in an unfamiliar area | "Explore the cash-advance input feature: map its files and code, the permissions it uses, and any explicit or special rules." | `explore` |
| A feature end to end | "Map the project-template feature: a template is created, applied to a project, and then…" | `explore` |
| A question about a mechanism | "When the audit delta runs, are the commits it checks based on the anchor hash of the last audited commit?" | `analyze` |
| Reviewing work in progress | "Analyze the changes made in my worktree on this project." | `analyze` |
| Merging two features | "Make a plan to merge this meetings page with the meetings feature on the Notes page." | `plan` |
| A cost or speed problem | "Each step uses many tokens and is slow; plan how to lower token use without reducing detection quality." | `plan` |
| After an implementation | `/.verify`, straight after `/.execute -y` | `verify` |
| A UI change | "Test it end to end in the browser, several rounds over every category area that changed." | `verify-browser` |
| Before a commit | `/.sweep` | `sweep` |

It is less attractive when the change is already localized, the relevant file is known, or
you need an instant answer: a delegated call takes from under a minute to several minutes,
because the secondary agent actually reads the code. The
[team guide](docs/team-guide/when-to-use.md) covers choosing per task and how to phrase
requests.

---

## Quick start

Install first: [installation guide](docs/installation.md). Then, in a project, run `/.init`
(or `/.upgrade` when it already has a `.workflow/`) and `/.doctor`.

Ask in natural language; the primary agent routes it:

```text
you:          where is the authentication logic in this project?
Claude Code:  [INTENT] explore — location question
              (runs bash .workflow/run.sh explore '...')
```

Or call the runtime directly:

```bash
bash .workflow/run.sh explore 'find the authentication entry point' '<SESSION_ID>'
```

The generated runner refuses a delegated call without a session id: without one,
concurrent sessions would share an identifier and overwrite each other's state.

| Command | Type | Purpose |
| --- | --- | --- |
| `init` / `upgrade` | local | Enable the runtime in a project / refresh it after the tool is updated |
| `doctor` | local | Readiness check; writes a report |
| `sweep` | local | Scan the working tree for changes |
| `clean` | local | Prune jobs, stale facts, and old sessions |
| `explore` | delegated | Code map, entry points, ownership |
| `analyze` | delegated | Causal analysis; no code changes |
| `plan` | delegated | Evidence-backed implementation steps |
| `verify` | delegated | Verdict on completed work, with evidence — produced by the secondary agent, not an independent check |
| `verify-browser` | delegated | Verify a running app in a real browser: the secondary agent drafts the scenario and reviews the evidence; a runtime player drives Playwright |
| `promote-validate` → `promote-verify` → `promote-write` | local | Turn verified evidence into a Git-tracked knowledge document |

Implementation itself (`/.execute` in Claude Code) is a primary-agent skill, not a runtime
command. Asynchronous job commands and the remaining local commands are in the
[full reference](docs/reference.md#command).

Setup problems and their fixes: [installation guide](docs/installation.md#when-something-is-wrong).

---

## Security

What each secondary-agent provider can read and change: [docs/security.md](docs/security.md).

---

## Research & evaluation

> `agent-workflow` explores whether externalizing repository exploration, evidence
> handling, and execution control from a primary coding agent can improve
> software-engineering workflows on complex real-world repositories.

It is developed as an open engineering and research artifact. Design
decisions, hypotheses, experiments, and sanitized real-world observations are recorded in
[docs/research/](docs/research/), with explicit provenance: a related paper is not presented
as the origin of an implementation unless that is known, and an implementation working in
one project is not presented as a general result.

The project does not assume that more orchestration makes a coding agent better. The
controlled benchmark and the maintainer's usage telemetry are kept apart in
[docs/evaluation/](docs/evaluation/), and telemetry is read as evidence for specific
conditions, not as a performance claim.

---

## Documentation

Documentation is layered: the team guide says **how and when**, research says **why**, and
the reference says **exactly what the runtime does** — see [docs/](docs/README.md).

| Document | Contents |
| --- | --- |
| [docs/installation.md](docs/installation.md) | Requirements, install, per-project setup, updating, uninstalling |
| [docs/security.md](docs/security.md) | What each secondary-agent provider is allowed to read and change |
| [docs/team-guide](docs/team-guide/README.md) | For developers: getting started, when to use it, task framing, examples, troubleshooting |
| [docs/architecture](docs/architecture/README.md) | Verified request flow, components, and storage |
| [docs/reference.md](docs/reference.md) | Complete technical reference: configuration, jobs, fact store, evidence reuse, verify modes, sessions, tests, CI *(Bahasa Indonesia)* |
| [docs/runtime-contracts.md](docs/runtime-contracts.md) | Which `.workflow/config.json` keys the runtime obeys, and which it cannot enforce |
| [docs/limitations.md](docs/limitations.md) | Known limitations |
| [docs/research](docs/research/README.md) | Research method, contract, and records |
| [docs/evaluation](docs/evaluation/README.md) | Benchmark and observed usage |
| [CHANGELOG.md](CHANGELOG.md) / [RELEASE.md](RELEASE.md) | Release history and procedure |

[ci.yml](.github/workflows/ci.yml) runs the version stamp, manifest, stdlib-only gate, and
test suites on every push on Linux and Windows, spending no provider quota;
[e2e-full.yml](.github/workflows/e2e-full.yml) is manual because it calls a real secondary
agent.

---

## Limitations

`agent-workflow` adds an execution layer, and with it additional latency, additional
provider calls and compute, new failure modes, provider-specific security limits, and
maintenance complexity. It is not preferable for every task; the project is exploring
**when** external orchestration pays off. Full list: [docs/limitations.md](docs/limitations.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE) for the authoritative text.

Use, modification, and distribution are permitted, including commercially, provided the
copyright notice and licence are retained and modified files are marked as changed. The
licence includes an express patent grant that terminates for any party initiating patent
litigation over the work. The software is provided without warranty of any kind. This
summary is not legal advice.

```text
Copyright 2026 dammar01
```
