# When to use it

You do not have to route every task through Exoma. Choose per task.

## Use the coding agent directly when

- the change is small and local,
- you already know the exact file or code path,
- it is a trivial bug fix, rename, or copy change,
- you need an answer in seconds.

## Use Exoma when

- the repository, or this part of it, is unfamiliar to you,
- several modules are involved,
- the feature crosses domains (data, API, UI, jobs, permissions…),
- it is a large refactor,
- you are looking for a root cause,
- you do not know all the areas a change will affect.

## Three modes

| Mode | You provide | What happens | Available |
| --- | --- | --- | --- |
| **Direct** | the exact change | the coding agent edits right away | yes |
| **Assisted** | an objective and constraints | the workflow explores and plans; you answer open questions and explicitly approve `/.execute`; then `/.verify` | yes — this is how Exoma works today |
| **Autonomous** | an outcome and constraints | explore, plan, implement, and verify without a checkpoint | **no** — `/.execute` always needs your explicit approval; by design |

## Benefits and costs

| Benefit | Cost |
| --- | --- |
| Broader awareness of the repository before code is written | Additional latency: a delegated call takes from under a minute to several minutes |
| Exploration happens outside your main conversation, so its context stays focused | Additional provider calls and compute |
| Findings come back as evidence with `file:line` anchors you can check | One more layer that can fail (see [troubleshooting](troubleshooting.md)) |
| Structured planning: risks, open questions, and options before implementation | More steps than a direct request |

The decision is yours to make per task: *"This change touches parts of the system I don't
know; a few extra minutes of exploration is worth it"*, or *"This is a two-line fix, I'll ask
the agent directly."* Both are correct uses.

Why the exploration goes to a separate second agent instead of one of Claude Code's own
subagents: [why-not-subagent.md](../why-not-subagent.md).

## A quick check

If you can already write the list of files you are going to change, go direct. If you
cannot, start with the workflow.

These are heuristics, not measured thresholds. Known limitations of the runtime:
[limitations.md](../limitations.md).
