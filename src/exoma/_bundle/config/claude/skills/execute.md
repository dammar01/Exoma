# Skill: execute
description: Controlled implementation dengan approval gate.
argument-hint: [-y]

## Trigger
/.execute -y → PROCEED | /.execute → GATE only

## Gate
Tanpa plan aktif (LAST_PLAN_RESULT dengan decision=proceed) → nol edit. Output satu baris
"Belum ada plan aktif — jalankan /.plan dulu?" → STOP. Berlaku juga untuk permintaan
"perbaiki/tambahkan ..." yang dipetakan ke execute.
Tanpa -y → output [EXECUTION SCOPE] → "Tambah -y untuk konfirmasi" → STOP.
Persetujuan = HANYA `/.execute -y` dari USER. Kalimat execute tanpa `-y` jatuh ke gate di atas.
Jawaban AskUserQuestion atas open_questions plan BUKAN persetujuan — itu input untuk
memperbarui plan.

## Pre-Execution
[EXECUTION SCOPE] allowed: <files> | forbidden: <files> | reason: <batasan>
Dengan `-y`: SEBELUM edit pertama, buka execution di runtime:
  bash '<work_dir>/.workflow/run.sh' execute-begin '' '<MAIN_SESSION_ID>'
Runtime memeriksa plan tercatat (`plan-record`, decision=proceed) DAN token `-y` dari prompt
user sendiri (ditulis hook `intent-gate-set`, berlaku 15 menit, sekali pakai). Ditolak
(`execute_no_plan` | `execute_plan_not_proceed` | `execute_not_approved`) → relay
`next_action`, nol edit, STOP. Jangan tulis token sendiri: itu memalsukan persetujuan user.

## During
ONLY sentuh allowed. Butuh forbidden → STOP → report conflict → minta instruksi.
allowed hanya di project root sesi. Path di project lain → STOP, minta persetujuan eksplisit
untuk path itu. Aturan itu sama untuk semua jalur tulis; penegaknya yang beda:
- Edit/Write → hook `execution-gate` memblok: di luar `files` plan selama execution aktif;
  di luar root (selain temp dan ~/.claude) kecuali path itu ada di
  `policies.write_allowed_outside_root`.
- Bash/skrip patch → TIDAK dicek hook. Tak ada yang memblok; yang menegakkan cuma kamu.

## Post (baca .workflow/config.json → commands.auto_verify_after_execute)
Tutup execution dulu: bash '<work_dir>/.workflow/run.sh' execute-end '' '<MAIN_SESSION_ID>'
(status `implemented`, verification `not_run`; /.verify yang selesai menulis verdict-nya ke sini).
[EXECUTION RESULT] files_changed | confidence | uncertainties | verification | status
true  → auto-trigger /.verify. status: done|partial|blocked. JANGAN declare done sebelum verify selesai.
false (default) → JANGAN auto-jalankan /.verify (hindari test berat tak diminta). WAJIB:
  verification: not_run
  status: implemented | partial | blocked  ← DILARANG pakai "done"
  lalu tawarkan "/.verify sekarang?"
"implemented" ≠ "done". Tanpa verifikasi kamu TAK TAHU ini bekerja — jangan bilang tahu.

### Baca key ini SETIAP kali, jangan dari ingatan
`commands.auto_verify_after_execute` dibaca dari `.workflow/config.json` di akhir TIAP
/.execute. Key TIDAK ADA = default `false` (config.json berisi override saja). Nilai dari
sesi lain, project lain, atau dari ingatanmu TIDAK berlaku.
`true` → chain ke /.verify adalah bagian dari /.execute, bukan langkah opsional sesudahnya.
Berhenti sebelum verify selesai = /.execute yang belum selesai, apa pun isi diff-nya.

### Kalimat yang DILARANG saat verification: not_run
"done" | "selesai" | "sudah jalan" | "berhasil" | "fixed" | "works now" | "aman sekarang"
| "test lolos" (kalau kamu tak menjalankannya) | ✅ sebagai penanda selesai.
Yang BOLEH: "implemented, belum diverifikasi". Perbedaannya bukan gaya bahasa — user
memutuskan langkah berikutnya dari kata itu.

### Yang ditegakkan runtime, dan yang tidak
Runtime (`core/runtime/execution.py`, DEC-067): plan proceed + `-y` user untuk membuka
execution; scope dan root untuk Edit/Write (hook `execution-gate`, blok); `git commit` hanya
di giliran user meminta commit. Hook `status-guard` (Stop) MEMPERINGATKAN bila field
`[EXECUTION RESULT]` balasanmu mengklaim lebih dari not_run — `status` di luar
implemented|partial|blocked, atau `verification` selain not_run (DEC-068). Hook TIDAK membaca
prosa, jadi kalimat terlarang di atas sepenuhnya tanggung jawabmu.
`policies.enforcement` = hybrid (default) | warn | off.
