# Skill: analyze
description: Deep analysis via second_agent (1-call). --local: Claude only.
argument-hint: [--local] <topic>

## Trigger
/.analyze <topic>          → 1-call
/.analyze --local <topic>  → Claude langsung (skip proxy)

## Run (1-call)
Semua OS (tool Bash): bash '<work_dir>/.workflow/run.sh' analyze '<topic>' '<MAIN_SESSION_ID>'
<MAIN_SESSION_ID> dari [SESSION BINDING], WAJIB arg ke-3 (isolasi concurrent). Reuse LAST_EXPLORE_RESULT jika relevan.
Task nyentuh library eksternal → sebut nama library; proxy baca context7 docs dulu → temuan di external (bukan tebakan API).
GAGAL (ok:false | invalid_evidence | content menu/refusal) → HARD GATE:
  STOP → "[PROXY GAGAL] <alasan>. Lanjut /.local? (yes/no)" → TUNGGU user. JANGAN auto-fallback, JANGAN reasoning dari garbage.
--local atau [LOCAL_MODE]=true → skip run, /.local flow (bukan fallback diam-diam — mode eksplisit user).

## Output [ANALYSIS RESULT]
Relay digest + isi dari content. confidence (3 sub) + uncertainties WAJIB.
source: second_agent (1-call) | claude (local)
confidence: { problem_understanding, root_cause, solution_path } — masing low|medium|high — <alasan>
findings: <dari content, atribusi grounded/assumption> | (kosong: alasan)
implications: <dampak> | (kosong: alasan)
impacted_features: <fitur/modul lain terdampak — dari dependents/reverse-dep> [file:line] | (tidak ada: alasan)
uncertainties: - uncertainty: <N>. <tak terkonfirmasi> → cara tutup: <apa> | (tidak ada)
               NON-blocking: nyatakan asumsi, lanjut. Butuh keputusan user → naikkan jadi
               `question: <N>. <pertanyaan> | <opsi A> | <opsi B>` dan sajikan interaktif.

## Rules: zero code changes, zero file mods.
