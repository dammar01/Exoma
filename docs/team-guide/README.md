# Team guide

Practical guidance for developers using Exoma with Claude Code or Agy as the primary agent
(the "host"; what each one enforces: [reference, Hosts](../reference.md#hosts)). It explains how
and when to use it, not how it works inside; for exact behavior, follow the links into the
[reference](../reference.md).

Read in this order:

1. [getting-started.md](getting-started.md) — set it up and make your first request.
2. [installation.md](installation.md) — choosing a mode (always on or only when called); the
   steps are in the [installation guide](../installation.md).
3. [whats-included.md](whats-included.md) — every command and hook, and when to reach for it.
4. [when-to-use.md](when-to-use.md) — the workflow or the agent directly? Benefits and costs.
5. [task-framing.md](task-framing.md) — how to phrase a request so the workflow can help.
   **The most important page.**
6. [examples.md](examples.md) — the same task done two ways.
7. [troubleshooting.md](troubleshooting.md) — errors, slowness, and what to do about them.
8. [measure-your-use.md](measure-your-use.md) — summarize weeks of your own sessions, and
   share the figures without sharing your projects.

The one idea to take away: Exoma changes **who does the decomposition and
repository discovery**. If you still break every task into small instructions yourself, it
will mostly feel like the agent you already had, only slower.

That is the project's philosophy in practice: define the problem, not the answer to be
found. Give the goal, the context you have, and the constraints that matter, then judge the
result by its evidence rather than by whether it matches what you expected. The principles
behind it are in the [README](../../README.md#core-philosophy).
