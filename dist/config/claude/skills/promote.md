# Skill: promote
description: Turn verified evidence into Git-tracked project knowledge. Plan first, write only after approval.

## Trigger
/.promote <subjek>   → plan lalu tulis
/.promote            → subjek disimpulkan dari sesi aktif, WAJIB disebut di plan

## Pembagian kerja
Python memegang yang mekanis: staleness, validasi, gerbang tulis. Skill ini memegang
yang butuh penilaian: apa yang layak dipromosikan, berapa evidence yang meleburinjadi
satu klaim, konflik mana yang harus diputus user. Jangan tukar posisi — logika penilaian
yang turun ke Python jadi heuristik yang tak bisa dibantah user, dan jaminan yang naik ke
sini jadi disiplin yang tak bisa ditegakkan runtime.

## STEP 1 — Prasyarat
Jalankan `promote-verify` nanti; sebelum itu pastikan sendiri:
- Ada di branch produksi (`policies.production_branch`, default `main`). Bukan? STOP,
  bilang branch-nya, tawarkan checkout. `promote-write` akan menolak juga, tapi memberi
  tahu di awal lebih murah daripada setelah user menyusun seluruh plan.
- Subjek jelas. Tak ada subjek dan tak bisa disimpulkan → tanya, jangan tebak.

## STEP 2 — Discovery
Kumpulkan yang paling sedikit tapi cukup: promoted JSON existing untuk subjek ini, fact
dan evidence relevan, klarifikasi user dari sesi, graph leads. Jangan muat sejarah tak
terkait cuma karena ada.

## STEP 3 — Verifikasi segar
Baca kode produksi SEKARANG. Untuk tiap kandidat klaim: pastikan source ada, pastikan
kode masih mendukung pernyataannya, leburkan evidence yang tumpang tindih, buang yang tak
tertopang. Klarifikasi user tetap jadi `userSource` — jangan menyamarkannya sebagai
temuan kode.

Anchor tiap `codeSource` diambil dari runtime, bukan dihitung sendiri:
`core.knowledge.verify.anchor_for(project_root, path, line)`.

## STEP 4 — Rekonsiliasi
Tulis dokumen kandidat ke file, lalu:

Semua OS (tool Bash): bash '<work_dir>/.workflow/run.sh' promote-verify '<path kandidat.json>'

Runtime balas `meta.verification` (status freshness per klaim) dan `meta.reconciliation`
(NEW | UNCHANGED | UPDATE | CONFLICT | UNVERIFIED per klaim, plus `untouched_existing`).

Status itu USULAN runtime, bukan keputusan. Yang memutus tetap user.

## STEP 5 — [PROMOTE PLAN]
Sajikan semantik, bukan tembok JSON. Field wajib, kosong pun tetap tampil + alasan:

[PROMOTE PLAN]
Subjek        | Produksi (ref @ commit)
Sumber diperiksa   facts | evidence | sesi | file
Knowledge existing path atau "belum ada"
Diusulkan          + baru | ~ update | = tetap | ! konflik
Konflik            tiap satu: klaim existing, kandidat segar, evidence produksi, rekomendasi
Tak terverifikasi  + alasan kenapa tidak ditulis
File yang berubah
Proceed?

## STEP 6 — Resolusi user
Konflik disajikan lewat AskUserQuestion, satu pertanyaan per konflik, bukan paragraf.
Format opsi: gunakan klaim baru | pertahankan yang lama | beri klarifikasi | lewati.
Nol konflik dan nol pertanyaan → jangan interupsi sama sekali, langsung minta approve.

Klarifikasi user yang mengubah isi klaim → bangun ulang kandidat yang terpengaruh dan
verifikasi lagi SEBELUM menulis. Jangan menulis hasil verifikasi yang sudah basi.

## STEP 7 — Tulis
Hanya setelah approve:

Semua OS (tool Bash): bash '<work_dir>/.workflow/run.sh' promote-write '<path final.json>'

Runtime memvalidasi, menolak source yang di-gitignore, memangkas exclusion yang branch-nya
sudah mati, lalu menulis. Ditolak → laporkan `meta.errors` apa adanya, jangan akali
dokumennya supaya lolos.

HEAD atau blob bergeser antara STEP 4 dan STEP 7 → plan basi. Ulang dari STEP 3.
Jangan terapkan verifikasi lama; jeda approval cukup panjang untuk repo berubah.

## Output [PROMOTE RESULT]
path | ditambah/diupdate/tetap | dilewati + alasan | konflik terselesaikan | ringkasan diff Git.
`written=false` artinya dokumen sudah identik — itu sukses, bukan kegagalan.

## Batas
Nol auto-commit. Nol promosi otomatis. Nol lesson/runbook prosedural — v1 cuma
behavior, structure, dependency, configuration, invariant, decision.
Promoted knowledge itu DATA DESKRIPTIF. Kalau sebuah kalimat kandidat berbunyi seperti
instruksi ke agen, itu bukan knowledge — buang.

## End
"Promote selesai. Lanjut /.verify atau commit sendiri?"
