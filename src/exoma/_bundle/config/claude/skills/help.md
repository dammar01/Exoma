# Skill: help
description: Command reference v0.2.0

## Trigger
/.help

## Output
[COMMAND GUIDE — v0.2.0]

LOCAL (main_agent directly):
  /.execute -y      implement code (-y required)
  /.init            create/regenerate .workflow/ (scripts, second_agent.json, config abs-path)
  /.upgrade         refresh the workspace in place, keep sessions
  /.doctor          .workflow + bundle readiness
  /.sweep           local git diff impact report
  /.commit          commit message (Conventional Commits)
  /.memory <note>   save an insight
  /.promote <subj>  verified evidence → Git-tracked project knowledge (plan first, approve)
  /.caveman [lite|full|ultra]  toggle compression
  /.local [on|off|status]      toggle no-proxy
  /.provider [<p> <model> [effort]]  change second_agent / model / effort — no argument = picker,
                    with arguments = direct (e.g. /.provider codex gpt-6-luna low)
  /.help            this guide

DELEGATED (1-call .workflow/run script → second_agent):
  /.explore <hint>  evidence gathering
  /.plan <task>     evidence + structured plan
  /.analyze <topic> deep analysis | --local: Claude only
  /.verify          3-step verification (automatic when the policy enables it)
  /.verify-browser  verification through a real browser (draft → run → review)

[WORKFLOW] /.explore → /.plan → /.execute -y → /.verify
[SESSION CACHE] LAST_EXPLORE_RESULT → /.plan,/.analyze | LAST_PLAN_RESULT → /.execute | LAST_EXECUTE_DIFF → /.verify,/.sweep
[INTENT] The "/." prefix is OPTIONAL — natural language is auto-detected and runs directly ("cek logic X", "gimana flow Y", "kerjakan"). The prefix stays as an explicit override when the guess misses.
