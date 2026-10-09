# Skill: doctor
description: Local .workflow readiness and bundle-integrity check.

## Trigger
/.doctor

## STEP 1 — Bootstrap check
- .workflow/run.ps1|sh ADA → local runtime call:
    Windows:   & "<work_dir>\.workflow\run.ps1" doctor "check .workflow readiness"
    mac/linux: "<work_dir>/.workflow/run.sh" doctor "check .workflow readiness"
- .workflow belum ada → STEP 2 (local check). JANGAN gagal, JANGAN simpulkan package missing.

## STEP 2 — Local check (fallback, cek langsung)
.workflow/          : EXISTS | MISSING
.workflow/run.*     : EXISTS | MISSING
.gitignore          : CONTAINS .workflow/ | MISSING
$AGENT_PATH         : SET (<path>, exists) | NOT SET
.workflow/config.json : v3.8.3 (main_py_path set) | old | MISSING
graphify-out/       : EXISTS | MISSING
bundle (~/.claude)  : READY | DRIFTED | not_checked — `python "<dir($AGENT_PATH)>/install.py" --check` (skills/CLAUDE.md/AGENTS.md vs shipped dist bundle; DRIFTED → `install.py --apply`)
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
$AGENT_PATH NOT SET → set dulu (lihat /.init STEP 1).
