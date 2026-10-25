# Skill: doctor
description: Local .workflow readiness and bundle-integrity check.

## Trigger
/.doctor

## STEP 1 — Bootstrap check
- .workflow/run.sh ADA → local runtime call:
    Semua OS (tool Bash): bash '<work_dir>/.workflow/run.sh' doctor 'check .workflow readiness'
- .workflow belum ada → STEP 2 (local check). JANGAN gagal, JANGAN simpulkan package missing.

## STEP 2 — Local check (fallback, cek langsung)
.workflow/          : EXISTS | MISSING
.workflow/run.*     : EXISTS | MISSING
.gitignore          : CONTAINS .workflow/ | MISSING
exoma               : <versi> (`exoma --version`) | NOT INSTALLED
.workflow/config.json : v0.1.0 (runtime.python set) | old | MISSING
graphify-out/       : EXISTS | MISSING
bundle (~/.claude)  : READY | DRIFTED | not_checked — `exoma install --check` (skills/CLAUDE.md/AGENTS.md vs shipped bundle; DRIFTED → `exoma install --apply`)
second_agent MCP    : SAFE | RISK (<server>) | REVIEW (<server>) | NONE — scan config mcp milik provider AKTIF, bukan selalu opencode (context7=safe read-only; write/exec/fs/db/browser=risk)
read boundary       : ENFORCED (opencode) | NOT_ENFORCEABLE (codex|agy) — codex dan agy tak menegakkan boundary baca (runtime: `checks.second_agent_read_boundary` status `not_enforceable`, `trusted: true`); second_agent-nya bisa membaca tiap file project, `.env` termasuk, dan menerima environment proses penuh. Ini WARNING pilihan user, bukan NOT_READY

## Output
[DOCTOR REPORT]
source: runtime (local) | claude (fallback)
checks: <semua item STEP 2 + status>
mcp_second_agent: <verdict + daftar server + classification> — RISK/REVIEW = second_agent lampaui read-only, WAJIB tampil + alasan
read_boundary: <ENFORCED|NOT_ENFORCEABLE + provider> — NOT_ENFORCEABLE WAJIB tampil, jangan diringkas hilang
status: READY | NEEDS_UPGRADE | NOT_READY
actions: <fix per item MISSING/NOT SET + disable/confirm MCP risky> (kosong → "tidak ada — semua OK")
`checks.workspace_layout.layout_version` 1 → workspace masih layout 3.6; relay rekomendasi `/.upgrade` (migrasi ke `.workflow/data/`, backup dulu). `stray_at_root` → sebut file tak dikenal di root `.workflow`, jangan hapus sendiri.
NOT_READY + workspace missing → "Jalankan /.init". NEEDS_UPGRADE → jalankan `/.upgrade`.
exoma NOT INSTALLED → install dulu (lihat /.init STEP 1).
