# Skill: help
description: Command reference v3.8.3

## Trigger
/.help

## Output
[COMMAND GUIDE — v3.8.3]

LOCAL (main_agent langsung):
  /.execute -y      implement code (wajib -y)
  /.init            buat/regenerate .workflow/ (scripts, second_agent.json, config abs-path)
  /.upgrade         refresh workspace in-place, preserve sessions
  /.doctor          .workflow + bundle readiness
  /.sweep           local git diff impact report
  /.refactor <s>    structural, zero behavior change
  /.commit          commit message (Conventional Commits)
  /.review <f>      one-line per issue review
  /.compress <f>    compress prose ke caveman
  /.memory <note>   simpan insight
  /.promote <subj>  evidence terverifikasi → project knowledge ter-Git (plan dulu, approve)
  /.caveman [lite|full|ultra]  toggle compression
  /.local [on|off|status]      toggle no-proxy
  /.provider [<p> <model> [effort]]  ganti second_agent / model / effort — tanpa arg = picker,
                    dengan arg = langsung (mis. /.provider codex gpt-6-luna low)
  /.help            panduan ini

DELEGATED (1-call .workflow/run script → second_agent):
  /.explore <hint>  evidence gathering
  /.plan <task>     evidence + rencana terstruktur
  /.analyze <topic> deep analysis | --local: Claude only
  /.verify          3-step verification (auto bila policy mengaktifkan)
  /.verify-browser  verifikasi lewat browser sungguhan (draft → run → review)

[WORKFLOW] /.explore → /.plan → /.execute -y → /.verify
[SESSION CACHE] LAST_EXPLORE_RESULT → /.plan,/.analyze | LAST_PLAN_RESULT → /.execute | LAST_EXECUTE_DIFF → /.verify,/.sweep
[INTENT] Prefix "/." OPSIONAL — bahasa natural auto-detect lalu langsung jalan ("cek logic X", "gimana flow Y", "kerjakan"). Prefix tetap dipakai sebagai override eksplisit saat tebakan meleset.
