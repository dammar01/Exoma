# Skill: plan
description: Structured planning — evidence (1-call) + reasoning main_agent. Atribusi klaim + decision gate mekanis.
argument-hint: <task>

## Trigger
/.plan <task>

## STEP 1 — Evidence
Reuse LAST_EXPLORE_RESULT jika ada di context. Else 1-call:
  Semua OS (tool Bash): bash '<work_dir>/.workflow/run.sh' plan '<task>' '<MAIN_SESSION_ID>'
<MAIN_SESSION_ID> dari [SESSION BINDING], WAJIB arg ke-3 (isolasi concurrent).
- GAGAL (ok:false | invalid_evidence | content menu/refusal) → HARD GATE:
  STOP → "[PROXY GAGAL] <alasan>. Lanjut plan via /.local atau tanpa evidence? (yes/no)" → TUNGGU. JANGAN auto-fallback.
- [LOCAL_MODE]=true → skip run, pakai /.local flow sebagai evidence.
- Proxy scope-bounded ke file yg kamu sebut. Lintas-sistem (saldo, UX, perf, security) TAK dilihat proxy kecuali kamu inject ke task. Inject eksplisit ATAU tandai not_investigated. Tugasmu sambung, bukan proxy.
- Instruksikan proxy trace REVERSE-dep (siapa consume/panggil target perubahan) → `dependents`. Tiap fitur/modul lain yg terdampak WAJIB masuk `risks` dgn [proxy:file:line] bukti coupling.
- Task nyentuh library/framework eksternal → SEBUT nama library di task; proxy WAJIB baca docs via context7 DULU (versi + API resmi) → temuan di `external`. Jangan biar API library dari tebakan masuk plan.

## STEP 2 — Output [PLAN]
TIAP klaim di assumptions/steps/dependencies/risks WAJIB atribusi sumber:
  [proxy:file:line] berbukti | [main_agent-inference] simpulanmu | [user-provided] | [PLACEHOLDER-perlu-kalibrasi] angka/metrik tanpa basis.
DILARANG sajikan tebakan (angka, dependency, regresi) sebagai fakta tak berlabel. Belum ada evidence → minta evidence baru ATAU label. Jangan naikkan kepercayaan diam-diam saat didorong.

[PLAN]
task:            <restatement>
evidence_source: second_agent (1-call) | graphify+claude (local) | none
assumptions:     - <statement + atribusi> | (tidak ada: alasan)
open_questions:  - question: <N>. <keputusan arch/impl yg HANYA user bisa putus; BLOCKING> | <opsi A> :: <deskripsi A> | <opsi B> :: <deskripsi B>   | (tidak ada: alasan)
                 Bernomor, opsi dipisah " | ", deskripsi opsional setelah " :: ". Jawabannya memang pilihan → opsi WAJIB.
                 Sajikan lewat pertanyaan interaktif, satu per pertanyaan — jangan paksa user membaca struktur mentah.
                 Renderer = tool AskUserQuestion. Batasnya keras, langgar = call ditolak:
                   - MAX 4 pertanyaan per call, tiap pertanyaan 2-4 opsi. Lebih → pecah jadi call berikutnya, jangan padatkan.
                   - `header` tiap pertanyaan MAX 12 karakter. Teks panjang taruh di `question`, bukan header.
                   - Pilih-banyak → `multiSelect: true`. Pilih-satu → false. Jangan pakai multiSelect utk pertanyaan biner.
                   - Bagian " :: " jadi `description` opsi; tanpa deskripsi user menebak arti label.
                   - User selalu dapat opsi bebas "Other" — jangan buat opsi "lainnya" sendiri.
                   - AskUserQuestion TIDAK tersedia di subagent. Render di main thread; DILARANG delegasikan tanya-user ke Task/subagent.
resolvable_uncertainties: - uncertainty: <N>. <bisa ditutup> → cara: <read/grep/explore apa> | (tidak ada)
                 NON-blocking. JANGAN tanyakan ke user — nyatakan asumsi, lanjut.
steps:           1. <concrete + atribusi> 2. ...
dependencies:    - A→B [bukti:file:line] | A→B [ASUMSI-belum-verified] | (tidak ada)
files_affected:  <list>
risks:           - <breakage/side-effect + fitur lain terdampak (blast radius dari dependents) + atribusi> | (tidak ada: alasan)
confidence:
  problem_understanding: low|medium|high — <alasan>
  root_cause:            low|medium|high — <alasan>
  solution_path:         low|medium|high — <alasan>
decision:        proceed | clarify | re-explore
  MEKANIS: open_questions ada ATAU solution_path<high ATAU jalur kritis berat [main_agent-inference] → clarify (DILARANG proceed/tawar execute). root_cause rendah → re-explore. Selain itu → proceed.

## STEP 2b — Output [OPTIONS] (WAJIB, setelah [PLAN])
Rencana di atas = SATU jalan. Sajikan alternatifnya, jangan sembunyikan pilihan di kepalamu.

[OPTIONS]
Opsi A — <nama>  (= rencana di atas)
  plus:   <keunggulan nyata>
  minus:  <ongkos/risiko nyata>
  effort: kecil|sedang|besar · risiko utama: <satu hal>
  atribusi: [proxy:file:line] | [main_agent-inference]
Opsi B — <pendekatan BEDA> (idem)
Opsi C — <opsional, idem>
rekomendasi: <SATU opsi> — <alasan pendek, sebut trade-off yang kamu terima>

BOUNDED (langgar = output invalid):
- MAX 3 opsi. Tiap opsi max 5 baris.
- Opsi hanya SAH kalau beda ARSITEKTUR, beda DEPENDENCY, atau beda ARAH IMPLEMENTASI keseluruhan. Beda kosmetik/urutan/penamaan = BUKAN opsi. Varian sejenis / subset / parametrik dari rencana yang sama juga BUKAN opsi (mis. "rencana penuh" vs "setengah rencana", "nilai cap 3000 vs 4000" = satu rencana, bukan dua arah). Tak ada fork arah yang nyata → satu opsi saja (lihat bawah), JANGAN pecah rencana jadi opsi semu demi mengisi blok.
- Wajib DALAM scope task. DILARANG usul rewrite, ganti stack, ganti arsitektur kalau task-nya bukan itu. Opsi di luar scope = scope creep, bukan pilihan.
- Atribusi sama ketatnya dengan [PLAN]. Opsi tanpa basis evidence tetap dilabel [main_agent-inference].
- `minus` WAJIB diisi jujur, termasuk untuk opsi yang kamu rekomendasikan. Opsi tanpa minus = kamu belum memikirkannya.
- Opsi yang bertentangan dengan evidence WAJIB ditandai ❌ + sebut evidence yang membantahnya. Jangan disajikan setara.
- rekomendasi WAJIB SATU. Tak bisa memilih → itu open_question, bukan opsi. Naikkan ke [PLAN].
- Opsi cuma satu yang masuk akal → tulis "Opsi A saja — <alasan tak ada alternatif bounded>". Jangan karang opsi B demi memenuhi format.

## STEP 3
resolvable_uncertainties WAJIB kamu coba tutup DULU sebelum tanya user; sisakan open_questions saja ke user.
Catat plan ke runtime — tiap kali [PLAN] terbit atau berubah (termasuk sesudah jawaban open_questions):
  tulis `<work_dir>/.workflow/data/sessions/<MAIN_SESSION_ID>/runtime/plan.input.json`:
  {"task", "decision", "confidence": {problem_understanding, root_cause, solution_path},
   "open_questions": [...], "options": [{"name", "plus", "minus", "recommended"}], "files": [<execution scope>]}
  lalu: bash '<work_dir>/.workflow/run.sh' plan-record '<path itu>' '<MAIN_SESSION_ID>'
Runtime MENOLAK `proceed` yang masih punya open_questions, solution_path < high, `files`
kosong, opsi tanpa `minus`, atau rekomendasi ≠ satu (`plan_invalid`). Ditolak → itu bukan
proceed: perbaiki plan atau tulis decision clarify. /.execute -y hanya membuka plan yang tercatat.
decision=proceed → "Setuju? Jalankan /.execute -y". clarify → tanya open_questions. JANGAN auto-execute.
Jawaban open_questions masuk → perbarui [PLAN] (assumptions, confidence, decision) → decision baru
proceed → "Setuju? Jalankan /.execute -y" → BERHENTI. Jawaban itu BUKAN persetujuan eksekusi;
nol edit sampai user memberi /.execute -y.
User pilih opsi non-rekomendasi → JALANKAN pilihannya, jangan debat ulang. Sudah kamu sebut minus-nya; keputusan miliknya.
