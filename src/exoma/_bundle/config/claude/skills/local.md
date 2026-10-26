# Skill: local
description: Toggle no-proxy. Claude mirrors the second_agent via graphify + Read/Glob/Grep. Affects /.explore /.plan /.analyze.
argument-hint: [on|off|status]

## Trigger
/.local [on|off|status] (no argument = toggle)

## State
[LOCAL_MODE] = true (no-proxy, graphify-mirrored) | false (default, 1-call proxy). Reset to false in every new session.

## Graphify-Mirrored Protocol ([LOCAL_MODE]=true, for explore/plan/analyze)
1. Load structure: graphify-out/ (graph.json/nodes/edges) or a Glob("**/*") fallback.
2. Scope: filter relevant nodes, shortlist at most 10 files (entry points → callers → deps).
3. Deep dive: Read + Grep per file, trace the important deps, stop once confidence is enough.
4. Output IDENTICAL to the related skill's format (source: graphify+claude (local)) so it interoperates with /.plan.
Read/Glob/Grep are ALLOWED in local mode (an exception to Global Forbidden).

## Output toggle
[LOCAL MODE — ON|OFF] Proxy: off|on | Coverage: /.explore /.plan /.analyze | Graphify: active|glob

## Rules
Zero code/file changes. /.execute and /.memory are unaffected.
