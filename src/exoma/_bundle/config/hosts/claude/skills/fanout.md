# Skill: fanout
description: Parallel sub-agent fan-out via {{CMD:explore}}/{{CMD:analyze}}/{{CMD:plan}} when policies.subagent_fanout_enabled is true.
argument-hint: <explore|analyze|plan> <task>

## Trigger
{{CMD:fanout}} <explore|analyze|plan> <task>

## Pre-Check (config gate)
1. Check `.workflow/config.json` → `policies.subagent_fanout_enabled` (default `false`).
2. If `subagent_fanout_enabled` is NOT `true` → STOP and inform the user:
   `Fan-out is disabled (policies.subagent_fanout_enabled=false). Enable it in .workflow/config.json to use {{CMD:fanout}}, or run {{CMD:explore}} / {{CMD:analyze}} / {{CMD:plan}} directly.`
3. Do NOT force retries when `second_agent` declines fan-out (`fanout_mode: "declined"` is valid).

## Run (when enabled)
Invoke the requested delegated command (`explore`, `analyze`, or `plan`) using the standard 1-call protocol in `CONTRACT.md`, prepending `[FANOUT] ` to `--task` (the runtime strips this marker and enables fan-out mode for this call) and instructing `second_agent` to fan out across `leads.json` clusters (or role slices) and tag merged claims `[cN]` with `subagents: ...`.
- Standard {{CMD:explore}} / {{CMD:analyze}} / {{CMD:plan}} calls without `[FANOUT]` stay single-worker even when `policies.subagent_fanout_enabled` is `true`.
