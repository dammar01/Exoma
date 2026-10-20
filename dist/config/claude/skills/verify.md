# Skill: verify
description: 3-step verification — logic, falsification, reality. Kedalaman dari commands.verify_mode.

## Trigger
/.verify (auto setelah /.execute -y atau /.refactor)

## Mode (runtime baca .workflow/config.json → commands.verify_mode)
delegated (default) → verifikasi penuh second_agent, Protocol di bawah.
syntax → QUICK: runtime check parse lokal file berubah (git diff HEAD + untracked). NOL test, NOL second_agent.
  Output [QUICK VERIFY] sudah final. pass = file parse, BUKAN fitur bekerja — sebut batas ini.
  not_checked/skipped relay apa adanya, JANGAN dihitung pass.
  DILARANG verdict DONE untuk masalah runtime/behavior; maksimal "syntax OK, behavior belum diverifikasi" + saran set verify_mode=delegated.

## Pilih test DULU (verify_mode=delegated — WAJIB sebelum run)
Second agent cuma baca + telusur; runtime yang menjalankan test. Kamu yang memilih:
1. Dari diff + dependents (siapa memanggil yang berubah), pilih test yang menutup perubahan.
   Targeted — file/suite terkait saja, BUKAN seluruh suite. Tak ada test relevan (docs, config
   murni) ATAU project tak punya test suite sama sekali → TETAP tulis tests.json berisi
   `commands: []` + alasan (mis. "project tanpa test suite"). Itu permintaan eksplisit
   "nol test", BUKAN gap: runtime cuma mencatat `runtime: no test requested` dan verdict
   bertumpu pada review. Melewatkan tests.json = gap runtime, exit nonzero.
2. Write `.workflow/data/sessions/<MAIN_SESSION_ID>/verify/tests.json`:
   `{"commands": ["<cmd>", ...], "reason": "<kenapa test ini menutup perubahan>"}` (max 5).
3. Runtime hanya menjalankan command yang diawali prefix di `commands.verify_test_commands`
   (`.workflow/config.json`), tanpa shell (`&&`/pipe/redirect ditolak). Di luar allowlist →
   `not_verified`. Allowlist kosong → sebut ke user, sarankan prefix yang perlu ditambahkan;
   project tanpa test suite → tak ada prefix untuk disarankan, tulis `commands: []` (langkah 1).
Gate: selama marker verify aktif (sebelum `.workflow/run verify`) yang lolos HANYA `git [-C <root>] diff --name-only|--name-status|--stat [ref] [-- path]`
(boleh diawali `cd <dir di dalam root> && `; selain itu nol pipe/`&&`/redirect), Read `.workflow/config.json` + tests.json sesi ini, dan Write tests.json itu. Baca isi file src/test → ke-block; cukup nama file dari diff.
Tanpa tests.json → verify jadi `incomplete` (gap `tests:`, exit nonzero). `commands: []` eksplisit ≠ tanpa tests.json. Test gagal → blocking.
tests.json milik SATU job verify (runtime mengklaimnya saat job dibuat). Rerun sesudah verify gagal = job baru → tulis tests.json lagi; pilihan lama tidak dipakai ulang.

## Protocol (verify_mode=delegated)
1. Logic: solve problem? assumptions valid? konsisten pola codebase? → PASS/FAIL + reason
2. Falsification: kondisi gagal? edge case? malformed input? → list
3. Reality: test pilihanmu dijalankan runtime (lihat di atas); hasilnya masuk `checks_run`
   sebagai `runtime: ...` dan, bila gagal, `blocking_findings`. Relay apa adanya.

## Gate 3-dimensi (verify_mode=delegated — WAJIB)
TIAP temuan bawa TIGA tag:
severity:       critical | high | medium | low
origin:         introduced | regression | pre_existing | unknown
scope_relation: in_scope | out_of_scope

critical = data loss | security | hasil salah diam-diam | semua command rusak
high     = jalur normal fitur rusak | caller existing regresi | kontrak dilanggar
medium   = edge case | degradasi | defect dgn workaround
low      = naming/style/doc drift | hipotetis tanpa trigger

SEVERITY SENDIRIAN TAK MENENTUKAN BLOCKING. Rute pakai tabel:
introduced/regression + in_scope     + critical|high → BLOCKING
introduced/regression + out_of_scope + critical|high → BLOCKING (+ pelanggaran scope)
introduced/regression + out_of_scope + medium|low    → ESCALATION
unknown               + apa pun      + critical|high → BLOCKING (fail closed)
pre_existing          + apa pun      + critical|high → ESCALATION
selain itu                                          → NOTE

`unknown` bukan pintu keluar: turun dari unknown WAJIB sebut bukti (diff/git history/versi sebelum). Tak bisa → tetap memblokir.
ESCALATION tak ubah verdict TAPI BUKAN note — critical/high nyata yang user harus putuskan. DILARANG disembunyikan di notes.
Tanpa file:line + skenario gagal konkret → TAK BOLEH critical/high, turunkan jadi note + sebut evidence kurang.
Aturan itu soal MUTU EVIDENCE, bukan meredam masalah sistemik: defect tersebar banyak tempat TETAP critical/high — kutip file:line perwakilan + sebut luasnya.
DILARANG naikkan sever biar diperhatikan / turunkan biar lolos.

## Output
[VERIFICATION]
mode: delegated | syntax          ← kamu yang isi; second_agent tak tahu mode (syntax nol lewat dia)
verdict: DONE | NEEDS FIX  ← NEEDS FIX HANYA bila blocking_findings ada
blocking_findings: - severity|origin|scope_relation — <problem> [file:line] — trigger — impact — fix | (tidak ada: apa yg dicek bersih)
escalations: - severity|origin|scope_relation — <problem> [file:line] — kenapa tak memblokir | (tidak ada)
notes: - severity|origin|scope_relation — <problem> [file:line] | (tidak ada)
checks_run: <yang dijalankan/dibaca + hasil>
not_verified: <tak bisa dicek + alasan> | (tidak ada)
confidence: low|medium|high — <alasan>
NEEDS FIX → fix blocking dulu → re-run /.verify. JANGAN output final sebelum done.
escalations + notes JANGAN dibuang — tampilkan, user yang putuskan garap sekarang/nanti.
Verdict runtime (`meta.verdict`) ditulis otomatis ke execution sesi (`runtime/execution.json`): pass | fail | incomplete, dan `syntax_only` untuk mode syntax — parse OK tak pernah tercatat sebagai pass.
