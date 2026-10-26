# Documentation

Exoma documentation is split into two layers. Each answers a different question, and each
has one job.

| Layer | Answers | Audience | Where |
| --- | --- | --- | --- |
| **Team guide** | How and when do I use this? What does a good request look like? | Developers using Exoma day to day | [team-guide/](team-guide/README.md) |
| **Reference** | Exactly what does the runtime do? | Anyone who needs precise behavior: commands, config keys, contracts, storage | [reference.md](reference.md), [runtime-contracts.md](runtime-contracts.md), [architecture/](architecture/README.md) |

Getting it on a machine: [installation.md](installation.md) (requirements, install,
per-project setup, updating, uninstalling; it carries the exact commands so a new user finds
them in one place) and [security.md](security.md) (what each provider can read and change).

Working on Exoma itself: [development.md](development.md) (the checkout, tests, keeping a
development build away from your daily `~/.claude`, and staging a release in
agent-workflow).

Supporting documents: [limitations.md](limitations.md),
[why-not-subagent.md](why-not-subagent.md) (why evidence goes to a second agent rather than
a native subagent, with the figures behind it), the
[metrics registry](evaluation/metrics.md) behind every figure `report` prints, and the
release history: the index in [CHANGELOG.md](../CHANGELOG.md), per-version notes in
[releases/](releases/).

## Rules between layers

- **Reference is authoritative.** When the team guide disagrees with the reference, the
  reference describes what the code does; fix the team guide.
- **The team guide links, it does not copy.** Command flags, config keys, provider security
  details, and storage layouts stay in the reference. The team guide explains when and why
  to use them and links there.
- **Release notes say what changed, not how it works.** They link to the reference for
  behavior instead of restating it, so there is one description to keep current.
- **Neither layer is runtime input.** The runtime reads `.workflow/` and
  `docs/project-knowledge/` (written only by `promote-write`), never these documents.
