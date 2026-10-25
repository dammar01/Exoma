# Skill: local
description: Toggle no-proxy. Claude mirror second_agent via graphify + Read/Glob/Grep. Affects /.explore /.plan /.analyze.
argument-hint: [on|off|status]

## Trigger
/.local [on|off|status] (tanpa arg = toggle)

## State
[LOCAL_MODE] = true (no-proxy, graphify-mirrored) | false (default, 1-call proxy). Reset false tiap session baru.

## Graphify-Mirrored Protocol ([LOCAL_MODE]=true, untuk explore/plan/analyze)
1. Load structure: graphify-out/ (graph.json/nodes/edges) atau Glob("**/*") fallback.
2. Scope: filter nodes relevan, shortlist max 10 file (entry points → callers → deps).
3. Deep dive: Read + Grep per file, trace dep penting, stop saat confidence cukup.
4. Output IDENTIK format skill terkait (source: graphify+claude (local)) agar interop /.plan.
Read/Glob/Grep DIIZINKAN saat local mode (exception Global Forbidden).

## Output toggle
[LOCAL MODE — ON|OFF] Proxy: off|on | Coverage: /.explore /.plan /.analyze | Graphify: active|glob

## Rules
Zero code/file changes. /.execute & /.memory tak terpengaruh.
