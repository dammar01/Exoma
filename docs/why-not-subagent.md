# Why a second agent, not a native subagent

Claude Code can already hand work to its own subagents. Exoma sends evidence gathering to a
separate CLI (the second agent) instead. This page explains why, separates what was measured
from what is reported, and says where the evidence is thin.

The rule itself, and its exceptions, live in the bundled instructions
(`src/exoma/_bundle/config/claude/CLAUDE.md`, "Division of Labor"): the second agent does the
bulk reading, the primary agent does the reasoning, and a native subagent is used only when the
proxy fails or for a small slice that needs high precision.

## The three reasons

### 1. A native subagent spends the primary agent's quota

A native subagent runs on the same Claude account as the primary agent. Everything it reads is
billed to that quota, even though its context is separate. The second agent runs on another
provider's subscription (`codex`, `opencode`, or `agy`), so bulk reading does not draw from the
Claude quota at all.

Measured on the maintainer's machine on 2026-10-25, from local Claude Code transcripts (26
project folders) and the `usage.jsonl` of 15 projects:

| | Native subagents | Second agent (proxy) |
| --- | --- | --- |
| Sample | 78 subagent runs | 2,496 calls with provider-reported counts (of 2,894 rows) |
| Input processed per run, median | 3,280,115 tokens | 2,041,423 tokens |
| Input processed per run, p90 | 9,571,858 tokens | 11,577,117 tokens |
| Share of input served from cache | 96.5% | not separated in this count |
| Billed to | the Claude account | the provider account |
| Returned to the primary agent | the subagent's final message | the digest first (median 889 characters); full output on demand |

How to read the table:

- **Both columns count tokens processed, not cost.** Cache reads dominate both, and a cached
  token is priced differently from a fresh one. The table does not say which is cheaper in
  money; it says whose quota pays.
- **The two samples are different work.** The subagent runs and the proxy calls were not the
  same tasks, so per-run figures are not a head-to-head comparison.
- **The proxy figures are provider-reported** (`token_source: provider`). Rows marked
  `estimated` (`chars // 4`) are excluded from the medians. See
  [reference, "Statusline"](reference.md#statusline-second-agent-badge) for how usage rows are
  recorded.
- **Only the subagent's final message returns to the primary agent**, and the scan could not
  measure that size reliably, so this page does not claim the proxy returns less context than a
  native subagent.

### 2. The proxy thread survives across calls

The second agent keeps one provider thread per primary session: a later call resumes it
(`meta.resumed: true` with the same `meta.provider_session_id`), so follow-up questions
build on what it already read. A native subagent's context ends with its task; the next
subagent starts from nothing.

The maintainer has also reported native subagent sessions that could not be loaded again. The
transcript scan found no tool error recording it (no failed `SendMessage`, `Agent`, or task
lookups), so this stays a report, not a measured result.

### 3. Delegated evidence is checked by the runtime, not only trusted

A primary agent tends to accept what a helper hands back. With a native subagent there is
nothing between the two: the subagent's answer is the evidence. With the second agent, the
runtime checks every cited `file:line` against the project, records the ones it cannot verify,
and caps the reported confidence when citations fail
([reference, "Evidence reuse and boundary"](reference.md#evidence-reuse-and-boundary)).

What this does not do: it proves that a cited file and line exist, not that the claim about
them is true. Reading the anchor that a decision rests on is still the primary agent's job.
The transcript scan found no recorded case of the primary agent accepting a wrong subagent
result, so the bias is a design concern here, not a measured incidence.

## When a native subagent is the right tool

- The proxy failed, or the project runs in local mode.
- A small, high-precision slice: one or two anchors the primary agent must read exactly.

For breadth (mapping where something is, collecting callers, sweeping many files), use the
second agent. Doing the map with native subagents and then verifying it again pays twice.

## Limits of this evidence

- One maintainer, one machine, daily work on private projects; not a general result.
- The proxy rows include calls from earlier releases of the same runtime.
- No controlled comparison of the same task with and without native subagents exists.
