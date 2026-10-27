# Installation

The steps (requirements, install, per-project setup, updating, uninstalling) are in the
[installation guide](../installation.md), which also lists what the installer changes on
your machine. This page covers the one decision it asks of you.

## Choose a mode: always on, or only when called

This is the decision that changes how Claude Code feels day to day. It is a Claude Code
install option; Agy reads the same workflow instructions through its own block, and what its
hooks enforce is in the [reference, Hosts](../reference.md#hosts).

| | Auto-intent (default) | Command-only |
| --- | --- | --- |
| How you start a workflow command | Plain language ("where is…", "why does…") or `/.<command>` | `/.<command>` only |
| Ordinary chat | Follows the workflow's rules (style, output format, next-step hints) | Follows **your own** instructions; the workflow stays out of it |
| Evidence gate | Enforced by a hook as well as by the instructions | Applies once a delegated command is called |
| Good for | People who use the workflow for most coding work | People with their own Claude Code setup who want the workflow as a tool they pick up |

Pick command-only if you already have a CLAUDE.md you rely on, or if you share the machine's
Claude Code with work that has nothing to do with this workflow. You can switch at any time;
the installer remembers your choice across upgrades. The two install lines are in the
[installation guide](../installation.md#2-read-the-dry-run-then-apply).

The secondary agent's instructions are scoped the same way in both modes: they apply only to
calls the workflow makes. Using codex or opencode directly in your own terminal is unaffected.

## Which secondary agent

opencode is the only provider whose read-only boundary is enforced; codex and agy are
trusted to read every file in the project, `.env` included. If unsure, choose opencode.
Details: [security](../security.md).
