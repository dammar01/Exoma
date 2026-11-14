# Skill: help
description: Command reference v1.1.1

## Trigger
{{CMD:help}}

## Output
[COMMAND GUIDE — v1.1.1]

LOCAL (main_agent directly):
  {{CMD:execute}} -y      implement code (-y required)
  {{CMD:init}}            create/regenerate .workflow/ (scripts, second_agent.json, config abs-path)
  {{CMD:upgrade}}         refresh the workspace in place, keep sessions
  {{CMD:doctor}}          .workflow + bundle readiness
  {{CMD:sweep}}           local git diff impact report
  {{CMD:commit}}          commit message (Conventional Commits)
  {{CMD:memory}} <note>   save an insight
  {{CMD:promote}} <subj>  verified evidence → Git-tracked project knowledge (plan first, approve)
  {{CMD:caveman}} [lite|full|ultra]  toggle compression
  {{CMD:local}} [on|off|status]      toggle no-proxy
  {{CMD:provider}} [<p> <model> [effort]]  change second_agent / model / effort — no argument = picker,
                    with arguments = direct (e.g. {{CMD:provider}} codex gpt-6-luna low)
  {{CMD:help}}            this guide

DELEGATED (1-call .workflow/run script → second_agent):
  {{CMD:explore}} <hint>  evidence gathering
  {{CMD:plan}} <task>     evidence + structured plan
  {{CMD:analyze}} <topic> deep analysis | --local: main_agent local mode
  {{CMD:verify}}          3-step verification (automatic when the policy enables it)
  {{CMD:verify-browser}}  verification through a real browser (draft → run → review)

[WORKFLOW] {{CMD:explore}} → {{CMD:plan}} → {{CMD:execute}} -y → {{CMD:verify}}
[SESSION CACHE] LAST_EXPLORE_RESULT → {{CMD:plan}},{{CMD:analyze}} | LAST_PLAN_RESULT → {{CMD:execute}} | LAST_EXECUTE_DIFF → {{CMD:verify}},{{CMD:sweep}}
<!-- AUTO-INTENT:START -->
[INTENT] The "{{CMD_PREFIX}}" prefix is OPTIONAL — natural language is auto-detected and runs directly ("cek logic X", "gimana flow Y", "kerjakan"). The prefix stays as an explicit override when the guess misses.

<!-- AUTO-INTENT:END -->
<!-- COMMAND-ONLY:START -->
[INTENT] Invoke a workflow skill explicitly with {{CMD_PREFIX}}<name>. Natural language does not auto-run workflow commands in command-only mode.
<!-- COMMAND-ONLY:END -->
