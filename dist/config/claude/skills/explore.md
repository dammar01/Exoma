# Skill: explore
description: Codebase evidence via second_agent (1-call). Fallback: /.local.

## Trigger
/.explore <hint>

## Run (1-call)
Semua OS (tool Bash): bash '<work_dir>/.workflow/run.sh' explore '<hint>' '<MAIN_SESSION_ID>'
- <MAIN_SESSION_ID> = nilai dari blok [SESSION BINDING]. WAJIB diteruskan (arg ke-3) — concurrent same-project butuh isolasi per-session. Absent → run script fallback (single-agent OK).
- Blocking sampai selesai. Return JSON {ok, content, meta, digest}.
- Tak karang command, tak check.py, tak AGENT_PATH.
- .workflow belum ada / run script hilang → /.init dulu.
- GAGAL (ok:false | invalid_evidence | content menu/refusal, no [EVIDENCE]/[DIGEST]) → HARD GATE:
  STOP → output "[PROXY GAGAL] <alasan>. Lanjut /.local? (yes/no)" → TUNGGU user. JANGAN auto-fallback.

## Output (RELAY mode)
digest ada → relay: summary, key_findings, risk_level, recommended_next_action, confidence.
digest absen (fallback) → [EXPLORATION RESULT] penuh dari content:
  source | session | confidence | entry_points | ownership_hints | related_modules | uncertainties
  (tiap field kosong → tampilkan + alasan). Butuh detail → buka content.

## End
"Lanjut /.plan, atau cukup?"
