# Documentation

Exoma documentation is organized by audience and responsibility so each topic has one
authoritative home.

## Quick navigation

| What you need | Where to look |
| --- | --- |
| **Philosophy & design principles** | [philosophy.md](philosophy.md) — 5 core principles, human/agent/runtime authority, and evidence vs. facts vs. knowledge vs. task state vs. experience |
| **Installation & security** | [installation.md](installation.md) (install, per-project init, update, uninstall) · [security.md](security.md) (per-provider read/write boundaries) |
| **Day-to-day usage (Team guide)** | [team-guide/](team-guide/README.md) — getting started, when to use delegation, task framing, examples, troubleshooting |
| **Architecture** | [architecture/](architecture/README.md) — verified request flow, execution governance, browser pipeline, and storage boundaries |
| **Runtime reference & contracts** | [reference.md](reference.md) (authoritative commands, config, jobs, hosts, sessions) · [runtime-contracts.md](runtime-contracts.md) (enforced vs. non-enforced config keys) |
| **Known limitations** | [limitations.md](limitations.md) — epistemic, host/provider, security, and operational boundaries |
| **Evaluation & metrics** | [evaluation/metrics.md](evaluation/metrics.md) — implemented metric categories, versioned registry, and interpretation limits |
| **Contributing & development** | [CONTRIBUTING.md](../CONTRIBUTING.md) (external contributor guide) · [development.md](development.md) (maintainer workflow, Exoma ↔ agent-workflow relationship, release staging) |
| **Background & history** | [why-not-subagent.md](why-not-subagent.md) · [CHANGELOG.md](../CHANGELOG.md) · [releases/](releases/) · [roadmap.md](roadmap.md) (deferred backlog, non-binding) |

## Rules between layers

- **Reference is authoritative.** Exact runtime behavior, command flags, config keys,
  provider security details, and storage layouts belong in [reference.md](reference.md),
  [runtime-contracts.md](runtime-contracts.md), and [architecture/](architecture/README.md).
  When another page disagrees with the reference, fix that page.
- **Link instead of duplicating.** `README.md`, `philosophy.md`, and `team-guide/` explain
  purpose, principles, and usage, and link to the reference rather than copying contracts.
- **Release notes say what changed, not how it works.** Per-version notes link to the
  reference for current behavior.
- **Public docs are not runtime input.** The runtime reads `.workflow/` and
  `docs/project-knowledge/` (written only by `promote-write`), never these pages.
