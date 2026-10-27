# Troubleshooting

## Setup and runtime errors

Start with `doctor` (`bash .workflow/run.sh doctor`, the same on every OS) and
read its `recommended_fixes`. The common cases are in the [README
troubleshooting table](../../README.md#quick-start); commands and their options are in the
[reference](../reference.md#command).

| Symptom | What to do |
| --- | --- |
| A warning that `.workflow` was built by an older tool version | Run `upgrade` for that project |
| The provider CLI is not found | Install it and make sure it is on `PATH` |
| codex on Windows fails with `sandbox_unavailable` | Codex's own Windows sandbox would not start. Start a new session (`/clear`) and repeat the request; if new sessions fail too, update codex and run `codex` once by itself to approve its sandbox setup; meanwhile switch the secondary agent with `/.provider`. The error's next action lists these steps, and `doctor` shows the codex version to quote in a bug report ([reference](../reference.md)) |
| Commands fail after you moved the tool checkout | Run `upgrade` from the new location |
| Empty-looking `.lock` files in `.workflow/data` | `evidence.jsonl.lock` and the `.guard`/`.reclaim` files hold one invisible byte and stay between uses: normal. Other lock files are removed on release, so an empty one left behind is a writer that crashed and is reclaimed later. Do not delete any while something runs ([which ones and why](../runtime-contracts.md)) |

## "PROXY FAILED" (before v0.2.0: "PROXY GAGAL")

The primary agent stops and asks before doing anything else when the secondary agent returns no
usable evidence (an error, empty output, or a refusal instead of findings). This is
deliberate: it will not quietly fall back to reading the code itself. Answer `yes` to
continue locally without the secondary agent, or `no` to stop and fix the provider first.

## "EXECUTION GATE" / an edit or commit was blocked

Since 3.9.0 the approval rules are checked by hooks (Claude Code's, or Agy's wrapper), not
only asked of the agent. A block
means one of three things: the agent tried to edit a file its approved plan does not list,
to write into another project, or to commit in a turn where you did not ask for a commit.
The message says which. The usual fix is a revised plan and a new `/.execute -y`, or asking
for the commit yourself. To relax it for one project, see `policies.enforcement` and
`policies.write_allowed_outside_root` in the
[configuration reference](../reference.md#workflowconfigjson). A "STATUS GUARD" note is a
warning, not a block: the agent said "done" before anything was verified.

## It is slow

It is slow because the secondary agent reads the code. That is the work you are delegating,
not overhead around it. What you can do:

**Measure first.** Your own numbers are better than anyone's estimate. The `report` command
summarizes how long delegated calls took in your project (mean, median, and how many calls
were not measured); per-call detail is kept in the workspace too. How to run it and where
the per-call files live: [reference, command table](../reference.md#command) and
[workspace layout](../reference.md#layout-workspace).

**Then reduce it:**

- **Skip the workflow for small tasks.** See [when-to-use.md](when-to-use.md). This is the
  biggest saving.
- **Keep verify's tests targeted.** Verify no longer lets the secondary agent run your test
  suite: the primary agent picks the tests that cover the change and the runtime runs only those.
  Tell the project which test commands may run (an allowlist in the project's workflow
  config, see the [reference](../reference.md), "Mode verify"); without it no test runs and
  verify reports the tests as not run.
- **Do something else while it runs.** A delegated call runs as a background job, and
  the primary agent hands control back to you while it works. You cannot continue *that* line of
  reasoning until the result arrives, but you can review, write, or start another task.
- **Use the local commands.** `sweep` and `doctor` never call the secondary agent.
- **Scope the request.** "How does checkout validate addresses" finishes sooner than "explain
  checkout".
- **Let reuse work.** Repeated or similar questions can reuse earlier evidence and facts.
  Forcing a fresh session turns that off; do it only when you know the earlier evidence is
  stale. The option is in the [CLI reference](../reference.md#cli-langsung).
- **Slow right after an implementing turn, on a large project?** That used to be the graph
  refresh running inside the turn. It now runs in the background, so the turn ends first
  and the graph catches up a little later; the `report` command shows what each refresh
  cost. How it works: [Graphify in the reference](../reference.md#graphify).
- **Use the quick verify mode when parsing is all you need.** A project can switch
  `/.verify` to parse checks only, without the secondary agent. It is much faster, but it
  proves only that files parse — **not** that the behavior is correct. The setting and what
  each mode checks: [verify modes in the reference](../reference.md#mode-verify).

If a task is worth delegating but still too slow to be practical, say so in an issue: that
feedback is worth more than a workaround.

## "It feels the same as the normal agent"

Usually a sign that the request was procedural: you had already decided the steps, so there
was nothing left for the workflow to discover. Try the same task with an objective-oriented
request — [task-framing.md](task-framing.md), and the side-by-side in
[examples.md](examples.md). If it still adds nothing for that kind of task, that is a valid
result: use the agent directly for it.
