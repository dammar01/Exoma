<div align="center">

<img src="https://raw.githubusercontent.com/dammar01/Exoma/main/docs/assets/banner.svg" alt="Exoma — Evidence-driven problem solving for AI agents. Context over prescription. Evidence over expectation. Effective coordination over unnecessary complexity." width="900">

# Exoma

**Evidence-driven problem solving for AI agents.**

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](docs/installation.md#requirements)
[![PyPI](https://img.shields.io/badge/pip-exoma-blue.svg)](docs/installation.md)
[![Version](https://img.shields.io/badge/version-0.2.0-informational.svg)](CHANGELOG.md)

**[Installation guide](docs/installation.md)** · [Security](docs/security.md) · [Team guide](docs/team-guide/README.md) · [Reference](docs/reference.md)

</div>

---

## What is Exoma?

Exoma is a library that supports problem solving by AI agents through context management,
exploration, tool use, work delegation, and evidence gathering.

It starts from the idea that how well an AI solves a problem is not decided by the model
alone, but also by how information is provided, how work is organized, and how the results
of an investigation are evaluated.

A person who hands a problem to an AI should not have to prescribe every step, or steer it
toward a conclusion they already had in mind. The person sets the goal, the initial context,
and the constraints; the AI then uses the capabilities and resources available to it to
investigate the problem, weigh the possibilities, and reach a solution grounded in evidence.

Exoma supports that process as an external system that organizes the flow of context, work,
and information between an AI agent and the resources it uses.

---

## Core philosophy

**People define the problem, not the answer to be found.**

An AI is not only expected to produce the answer a person asked for. It also needs room to
check assumptions, weigh alternatives, find what was not yet known, and reach a conclusion
that may differ from what whoever assigned the task expected.

Exoma focuses on three principles:

1. **Context over prescription.** Provide the relevant context and resources so the AI can
   investigate the problem, rather than over-specifying the steps and conclusions it must
   follow.
2. **Evidence over expectation.** Favor problem solving grounded in evidence and evaluation,
   not in confirming a person's assumptions.
3. **Effective coordination over unnecessary complexity.** Organize agents, tools, and
   compute by what the task needs, without assuming that more agents or more process always
   produce better quality.

These principles are a design direction to be realized and evaluated through the
implementation, not a guarantee that any AI agent will always be objective or correct.

---

## How Exoma works

Technically, Exoma is a supporting layer around an AI agent. Depending on the need and the
implementation, it can manage context retrieval, delegate exploration to another agent,
gather evidence from external sources, retain relevant information, and control the flow of
execution.

The AI agent still does the semantic reasoning and makes the decisions its role calls for.
Exoma organizes the processes and resources that support that work. The aim is not to
replace the model or to make the whole process fully automatic; it is to let the AI complete
a task more effectively without a person directing every step or conclusion.

The current implementation targets **coding agents**, with Claude Code or Agy as the primary
agent ([hosts](docs/reference.md#hosts)).
It is installed as a Python package (`pip install exoma`) with a CLI (`exoma`), and separates
work that needs **broad repository inspection** from work that needs the **primary agent's
reasoning and implementation context**. A coding agent on a mature repository spends much of
its effort locating modules, tracing call sites, and reading conventions before it changes
anything; Exoma moves that inspection into a separate execution context and returns
structured evidence: a digest, `file:line` anchors, and a reference to the full evidence on
disk.

<p align="center">
<img src="https://raw.githubusercontent.com/dammar01/Exoma/main/docs/assets/architecture.svg" alt="Exoma architecture. The developer briefs the primary agent (Claude Code or Agy) with an objective and constraints and receives the plan, verdict, and questions back. The primary agent delegates to the Exoma runtime, which handles job, session, and lock, routing, context, and prompt, then sends a bounded task to the secondary agent. The secondary agent reads, searches, and traces the repository and returns candidate evidence; its intended role is read-only, and enforcement varies by provider. The runtime redacts it, checks the contract, shapes a digest with file:line anchors for the primary agent, and reads and writes sessions, facts, evidence, usage, audit, and response artifacts in .workflow/data/ with source anchors and hashes. Evidence candidates are not ground truth." width="900">
</p>

<sub>The primary agent reasons and writes the code; the developer answers open questions and
approves implementation. "Read-only" is the secondary agent's role, and how strictly it is
enforced depends on the provider (see [Security](docs/security.md)). Text version, full verified
flow, side paths, and storage: [docs/architecture](docs/architecture/README.md).</sub>

Delegated results are treated as **evidence candidates**, not truth. Every delegated call
returns the same envelope; `digest` appears when the secondary agent's summary parses, and
`evidence_ref` on the explore/plan/analyze evidence path:

```json
{ "ok": true, "content": "...", "meta": {}, "digest": {}, "evidence_ref": {} }
```

The primary agent decides what the evidence means and whether to act on it. A secondary
agent can be incomplete or wrong even when its output looks plausible.

### Core responsibilities

| Role | Who | Responsibility |
| --- | --- | --- |
| **Primary agent** | Claude Code, or Agy ([hosts](docs/reference.md#hosts)) | Reasoning, decisions, code changes, and final review |
| **Workflow runtime** | The `exoma` package, Python standard library only | Routing, sessions and locks, policy, prompt building, redaction, evidence contracts, digests, persistence, recovery |
| **Secondary agent** | `opencode`, `codex`, or `agy` | Repository exploration, analysis, planning, and verification evidence |

The design keeps code-writing with the primary agent. The secondary agent's role is
read-only; the degree to which each provider **enforces** that differs, and the runtime
states it rather than assuming it. See [Security](docs/security.md).

The principle: let the model do the semantic reasoning, and let deterministic software
enforce state, contracts, policy, provenance, and execution control.

---

## Relationship to agent-workflow

[agent-workflow](https://github.com/dammar01/agent-workflow) is the implementation and
research foundation Exoma grew from. It explores separating repository exploration from the
primary agent's reasoning and implementation context, and how delegation, evidence
management, and runtime control affect the outcome of the work. That research, including its
hypotheses and experiments, stays in agent-workflow.

This first release of Exoma repackages that runtime as a library: the same commands,
contracts and workspace, installed and upgraded like any other Python tool. Exoma is meant to
grow into a broader library identity, developed from mechanisms that have been tested and
needs that have been measured.

---

## The name

**Exoma** is a distinctive name for the project. Conceptually, it is inspired by the idea of
cognitive resources and processes that reach beyond the individual, including discussions of
*extrasomatic resources* and the *Extended Mind Thesis*.

That inspiration reflects the view that problem solving can be supported by the interplay of
an AI model, context, tools, memory, and external systems. It does not assume that tools or
external components literally have minds, nor that an AI becomes smarter simply by using
more resources.

---

## Vision

Exoma sets out to explore how external systems can help AI agents solve problems more
effectively: with the right context, a directed investigation, traceable evidence, and a
compute cost proportionate to its benefit.

Its value is not measured by how many agents it uses or how complex its architecture is, but
by how far it actually helps an AI solve problems with better quality and conclusions that
can be accounted for.

**Give agents the context to solve, not the answer to repeat.**

---

## When it is useful

The idea behind the design: on a large codebase, keeping exploration out of the primary
agent's context should leave it more room to reason. That is a design premise, not a measured
result; Exoma does not claim a benchmark.

Typical requests (translated, names removed):

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

```bash
uv tool install exoma            # or: pip install exoma
exoma install                    # dry run: read what it will change in ~/.claude
exoma install --apply
```

Then, in a project, run `/.init` in the primary agent (or `exoma init --work-dir .`), and `/.doctor`.
A project that already has a `.workflow/` from agent-workflow is taken over by `/.upgrade`.
Details, modes and undo: [installation guide](docs/installation.md).

Ask in natural language; the primary agent routes it (shown in Claude Code):

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

Implementation itself (`/.execute`) is a primary-agent skill, not a delegated
command; it opens and closes a runtime execution (`plan-record`, `execute-begin`,
`execute-end`) whose scope the host's hooks enforce for file writes. Asynchronous job commands and the remaining local commands are in the
[full reference](docs/reference.md#command).

Setup problems and their fixes: [installation guide](docs/installation.md#when-something-is-wrong).

---

## Security

What each secondary-agent provider can read and change: [docs/security.md](docs/security.md).

---

## Documentation

Documentation is layered: the team guide says **how and when**, and the reference says
**exactly what the runtime does** — see [docs/](docs/README.md).

| Document | Contents |
| --- | --- |
| [docs/installation.md](docs/installation.md) | Requirements, install, per-project setup, updating, uninstalling |
| [docs/security.md](docs/security.md) | What each secondary-agent provider is allowed to read and change |
| [docs/team-guide](docs/team-guide/README.md) | For developers: getting started, when to use it, task framing, examples, troubleshooting |
| [docs/architecture](docs/architecture/README.md) | Verified request flow, components, and storage |
| [docs/reference.md](docs/reference.md) | Complete technical reference: configuration, jobs, fact store, evidence reuse, verify modes, sessions, tests, CI |
| [docs/runtime-contracts.md](docs/runtime-contracts.md) | Which `.workflow/config.json` keys the runtime obeys, and which it cannot enforce |
| [docs/limitations.md](docs/limitations.md) | Known limitations |
| [docs/evaluation/metrics.md](docs/evaluation/metrics.md) | How every figure `exoma report` prints is computed |
| [CHANGELOG.md](CHANGELOG.md) / [RELEASE.md](RELEASE.md) | Release history and procedure |

[ci.yml](.github/workflows/ci.yml) runs the version stamp, manifest, stdlib-only gate, and
test suites, plus a clean install of the built wheel, on every push on Linux and Windows,
spending no provider quota; [publish.yml](.github/workflows/publish.yml) publishes a release
tag to PyPI; [e2e-full.yml](.github/workflows/e2e-full.yml) is manual because it calls a real
secondary agent.

---

## Limitations

Exoma adds an execution layer, and with it additional latency, additional provider calls
and compute, new failure modes, provider-specific security limits, and maintenance
complexity. It is not preferable for every task. Full list: [docs/limitations.md](docs/limitations.md).

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
