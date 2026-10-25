# Skill: verify-browser
description: Verifikasi lewat browser Playwright sungguhan. DELEGATED: draft spec (second_agent) → run browser (player runtime) → review (second_agent). Wawancara hanya sesudah draft melaporkan project belum dikonfigurasi.
argument-hint: [fitur/alur]

## Trigger
/.verify-browser [<fitur/alur yang mau diuji>]
Beda dari /.verify: /.verify = review kode (delegated|syntax). /.verify-browser = klik dan assert di app yang jalan.
DELEGATED command: Pre-flight gate berlaku. Sebelum `.workflow/run verify-browser` jalan, DILARANG Read/Grep/Glob/Bash/MCP (termasuk membaca config.json) — hook `intent-gate-check` memblokirnya. Write request.json boleh (bukan gather). Pembagian kerja: second_agent = reasoning + penyusunan spec (stage 1) dan review bukti (stage 3); player runtime = proses anak yang dijalankan runtime untuk menggerakkan Playwright (stage 2). main_agent tak menjalankan browser, second_agent juga tidak (sandbox read-only). Draft yang `ready` LANGSUNG dilanjut ke run — DILARANG bertanya "jalankan atau tidak". Yang tetap menghentikan: `blocked`/`invalid` dari draft, `missing_env`, dan usulan `read_only_requests` yang belum pernah diizinkan (itu keputusan izin, bukan gate jalan).

## Prasyarat
- Playwright: `exoma install --apply --with-e2e`. /.doctor → `e2e_readiness`.
- App target SUDAH jalan di URL yang user sebut. Runtime tak menyalakan server.
- Run script lama (belum kenal `verify-browser`) balas job tanpa hasil / `unsupported command` → jalankan /.upgrade dulu.

## STEP 0 — Draft dulu, config dibaca runtime (SEBELUM bertanya apa pun)
Section `e2e` di `<project>/.workflow/config.json` = default terpasang milik project, dan RUNTIME yang membacanya saat draft — main_agent tidak membacanya sebelum run (Pre-flight gate). Isinya key yang sama dengan settings request, DITAMBAH key config-only yang request tak boleh sebut: `allow_remote`, `allowed_origins`, `existing_test_command`, `existing_test_allowlist`, `existing_test_timeout_s` (registry `CONFIG_ONLY_SETTINGS`). Request ditulis agent, jadi policy yang mengizinkan origin remote atau command yang dieksekusi tak boleh tinggal di request itu sendiri.
- Langsung ke STEP 2 (request minimal) + STEP 3 (draft). User menyebut base_url di pesannya → taruh di `settings.base_url` request; tidak → biarkan config/default yang dipakai.
- Draft `ready` → **LEWATI STEP 1 sepenuhnya**. Jangan wawancara.
- Draft `blocked` (`base_url_unreachable`, atau target salah) / `invalid` karena info alur kurang → marker gate sudah dilepas run script, jadi config.json kini boleh dibaca → STEP 1, lalu tawarkan menuliskan hasilnya ke `config.json` supaya run berikutnya nol wawancara, dan ulang STEP 3.
- config.json berisi OVERRIDE saja: section `e2e` hanya ada bila project pernah dikonfigurasi. Key yang tak ada = default bawaan (`base_url: http://localhost:8000`, headed, retry 2). Workspace lama yang belum di-upgrade bisa masih punya section berisi default hasil backfill lama — base_url yang tak cocok dengan app user = belum dikonfigurasi → STEP 1.
Nilai config yang salah tipe/tak dikenal TIDAK menggagalkan run: dibuang, dipakai default bawaan, dan namanya muncul di `meta.e2e.config_warnings`. Relay peringatan itu — knob yang diabaikan terbaca sama seperti knob yang rusak.
Credential TIDAK PERNAH masuk config.json — tetap di `.workflow/e2e/secrets.json`.

## STEP 1 — Wawancara (AskUserQuestion, main thread) — HANYA bila draft di STEP 3 bilang perlu
Isi dari konteks dulu (diff, pesan user); tanya HANYA yang belum pasti. Max 4 pertanyaan per call, header ≤12 char.
1. Alur/fitur yang diuji + hasil yang dianggap benar (claims). Terbuka → opsi dari konteks bila ada.
2. base_url: `http://localhost:8000` | `http://127.0.0.1:8000` | virtual host (mis. `http://app.test`) → user isi lewat "Other".
   Tiga kelas, jangan dicampur:
   - loopback (localhost/127.0.0.1/::1/*.localhost) → lolos apa adanya.
   - nama `.test` (suffix cadangan RFC 6761 untuk development lokal) → diperlakukan PERSIS seperti localhost: lolos TANPA `allow_remote`, dan write ke sana tanpa lookup DNS maupun patokan.
   - domain sungguhan (`staging.example.com`) → WAJIB `allow_remote: true` + `allowed_origins: ["<scheme://host[:port]> persis"]` di section `e2e` config.json — BUKAN di request. Itu opt-in yang harus diketik user, bukan default. Tunjukkan snippet-nya, user yang menulis/menyetujui edit config.json.
   Navigasi ke origin LAIN (termasuk `.test` lain) tetap butuh entri di `allowed_origins`, apa pun kelas base_url-nya.
3. Perlu login? tidak | ya, akun sudah ada.
   Nama key credential DITETAPKAN runtime: `E2E_USER`, `E2E_PASS` (registry `CREDENTIAL_KEYS`). Scenario hanya boleh `${E2E_USER}` / `${E2E_PASS}`, huruf persis; nama lain atau salah kapital → `spec_invalid`. JANGAN tanya/karang nama key lain.
   Ya → tanya NAMA profil akun (mis. `qa`, `admin`), BUKAN nilainya. Taruh di settings `secrets_template_profiles` (dipakai hanya bila file belum ada) dan pilih satu lewat `secrets_profile`.
   Nilai diisi user sendiri di `.workflow/e2e/secrets.json` (gitignored, di bawah `.workflow/`):
   `{"default": "qa", "profiles": [{"name": "qa", "credentials": {"E2E_USER": "...", "E2E_PASS": "..."}}, {"name": "admin", "credentials": {...}}]}`.
   Beberapa akun = beberapa profil; satu run memakai satu profil lewat settings `secrets_profile` (kosong = `default`). Coba akun lain = run lain dengan nama profil lain.
   File belum ada → draft (STEP 3) membuatnya (struktur + key dari kode, satu profil per nama di settings) dengan slot kosong; string kosong = belum diisi. File yang sudah ada tak pernah ditulis ulang, juga saat dua draft jalan bersamaan.
   Format lama (`"profiles": {"qa": {...}}`) DITOLAK (`secrets_invalid`), tak dikonversi → minta user tulis ulang ke format list di atas.
   User menempel password di chat → JANGAN dipakai/diulang/ditulis ke file; minta taruh di secrets.json.
4. Tampilan: **default headed** (`headless: false`) — tanya hanya bila user tampak ingin lain. headed + slow_mo_ms 700 (bisa ditonton) | headed tanpa jeda | headless (CI/tanpa display). Ini knob config, bukan pertanyaan wajib tiap run.
5. Side effect data (buat/ubah/hapus data test)? tidak (default) | boleh — hanya app LOKAL (loopback, `.test`, atau host privat), tiap perubahan wajib punya cleanup yang bisa dijalankan.
   `tidak` TIDAK lagi memblok write ke host LOKAL. `allow_local_side_effects` (default `true`) mengizinkan POST/PUT/PATCH — tiga itu saja, verb lain (`PURGE`, `PROPFIND`, apa pun yang distack karang) tetap diblok walau host-nya lokal — ke loopback, `*.localhost`, dan `.test` tanpa opt-in — login lewat form POST jalan sendiri. Satu saklar global dulu menyamakan `localhost:8000` dengan host produksi, jadi membuktikan satu form bekerja menuntut izin yang sama besarnya dengan menulis ke mana saja. Set `allow_local_side_effects: false` untuk mengembalikan blokir menyeluruh.
   DELETE TIDAK ikut. Ia butuh endpoint-nya ada di `allowed_destructive_requests`, berapa pun saklar lain — lokal tak menjadikannya aman, karena runtime tak bisa membedakan record yang dibuat scenario dari record yang sudah ada sebelumnya. Begitu juga request yang MEMBAWA field `_method`, nilai apa pun. Runtime tidak membaca nilainya: request yang membawa field itu tidak bisa dinilai dari verb-nya, dan bagaimana framework tujuan menafsirkannya bukan sesuatu yang bisa diketahui dari sisi browser. Jadi field-nya sendiri yang jadi pertanyaan — refusal `override_unapproved`, dijawab dengan menyetujui endpoint-nya di `allowed_destructive_requests`, sama seperti DELETE.
Konsekuensinya jujur: POST yang kebetulan membawa field itu, atau body yang sekadar mengejanya (termasuk nama file), ikut minta persetujuan. Itu harga yang diambil sengaja — aturan kasar yang bisa dibuktikan benar, ditukar dengan aturan halus yang enam ronde verifikasi tak pernah selesai membenarkan.
Deteksi mencakup body, query, dan header (`X-HTTP-Method-Override`, `X-Method-Override`), pada tiap bentuk decoding: percent (termasuk berlapis), escape JSON `_`, envelope multipart, dan body UTF-16. Body multipart dibaca lewat `post_data_buffer` saat `post_data` menolak, jadi upload file biasa tetap jalan. Write yang body-nya tak satu pun accessor bisa baca DITOLAK (reason `uninspectable_body`), termasuk di bawah `allow_side_effects` — body itu tak bisa dibuktikan bebas `_method`. Tak ada setting yang meloloskannya, termasuk `allowed_destructive_requests`. Observation `write_uninspected` = laporan penolakan itu (per method+origin), bukan write yang terkirim. Relay sebagai penolakan: scenario yang wajib upload dijalankan lewat test command project sendiri (`existing_test_command`), bukan lewat browser run.
   `tidak` masih DITEGAKKAN runtime untuk host non-lokal: player abort semua request selain GET/HEAD/OPTIONS di origin mana pun yang bukan host pengembangan. POST yang cuma BACA (search, filter, GraphQL query): second_agent mengusulkannya di draft (`read_only_requests`, wajib bukti handler file:line), user konfirmasi di STEP 4, lalu masuk `allowed_read_only_requests`. Body GraphQL `mutation`/`subscription`/persisted query tetap diblok runtime.
   `boleh` → settings `allow_side_effects: true`. Loopback dan `.test` langsung boleh menerima write. Host LAIN dinilai dari ALAMAT, bukan nama: preflight me-resolve base_url dan tiap origin di `allowed_origins`, dan menolak run (`spec_invalid`) kalau salah satu punya alamat publik, link-local (`169.254.0.0/16` = metadata service cloud), atau tak bisa di-resolve. Semua alamat loopback/privat → host itu boleh menerima write, dan alamatnya DIPATOK (`--host-resolver-rules`) supaya nama itu tak bisa berpindah IP di tengah run — termasuk nama yang resolve ke loopback seperti `127.0.0.1.nip.io`. Patokan cuma didukung chromium → browser lain + write ke host terpatok DITOLAK; write https ke host terpatok juga ditolak. Tidak ada allow-list path lagi: `allowed_mutation_paths` DIHAPUS → `request_invalid` bila masih ditulis.
   Redirect: write yang dijawab 307/308 diikuti guard sendiri, tiap hop dinilai dulu — redirect ke tujuan yang tak boleh ditulis → diblok sebelum body terkirim ulang.
   Batas: GET yang mengubah data, WebSocket, service worker tidak terlihat guard maupun ledger request.
6. Existing test project (opsional): argv command tanpa shell (`{files}`, `{base_url}`) + allowlist glob — config-only, ditulis ke section `e2e` config.json, bukan ke request (command itu jalan dengan environment user). Tak ada → lewati.
Hybrid review SELALU jalan (codex menilai bukti browser) — bukan pertanyaan. Sebut: tiap run = 2 panggilan second_agent (draft + review).

## STEP 2 — Tulis request draft
File: `<project>/.workflow/data/sessions/<MAIN_SESSION_ID>/e2e/request.json` (Write tool, buat folder bila perlu). Workspace lama yang belum di-upgrade (belum ada folder `.workflow/data/`) → `<project>/.workflow/sessions/<MAIN_SESSION_ID>/e2e/request.json`. Salah folder = `request_missing`.
```json
{"version": 1, "phase": "draft",
 "settings": {"base_url": "http://app.test", "headless": false, "slow_mo_ms": 700, "allow_side_effects": false}}
```
`settings` = SELISIH terhadap config project, bukan salinannya. Project sudah punya section `e2e` → tulis `{"version": 1, "phase": "draft"}` saja; key yang sah di request menang atas config, dan menyalin nilai yang sama cuma bikin dua tempat yang bisa berbeda diam-diam.
Key settings sah di request: base_url browser headless slow_mo_ms nav_timeout_ms step_timeout_ms idle_timeout_s total_timeout_s probe_max_elements allow_side_effects allow_local_side_effects allowed_read_only_requests allowed_destructive_requests secrets_profile secrets_template_profiles fail_on_console_error max_retries ignore_repeat_brake keep_created_data artifact_max_mb. Key lain/tipe salah → `request_invalid` (`allowed_mutation_paths` → error migrasi). Key config-only (`allow_remote` `allowed_origins` `existing_test_command` `existing_test_allowlist` `existing_test_timeout_s`) di request → `request_invalid` yang menyuruh pindah ke config.json; JANGAN tulis di request. `base_url` tetap boleh per-run, tapi dinilai policy origin dari config. Key yang sama di config.json: salah tipe → diabaikan + warning, bukan error.
DILARANG menulis nilai credential ke request, task, atau scenario — hanya `${E2E_USER}`/`${E2E_PASS}`. Runtime mencocokkan nilai secrets.json ke task, draft, dan request.json: yang cocok diganti placeholder (request.json ditulis ulang), run tetap jalan, dan `meta.e2e.secret_literals` + `config_warnings` menyebut NAMA key-nya. Muncul → laporkan ke user sebagai kebocoran yang sudah ditambal, bukan sukses diam-diam. `fill` ke field password tanpa placeholder → `spec_invalid`.

## STEP 3 — Draft (background)
Semua OS (tool Bash): bash '<work_dir>/.workflow/run.sh' verify-browser '<task>' '<MAIN_SESSION_ID>'
`run_in_background: true`, ambil hasil task ID yang sama. <task> ≤3000 char, isi: alur + claims yang diharapkan, base_url, nama key credential (`${E2E_USER}`...), boleh/tidak side effect, file yang berubah bila tahu. JANGAN tempel isi kode.
Hasil `meta.phase=draft`, `content` = [E2E DRAFT], detail di `meta.e2e.draft` + file `draft.json` di folder yang sama.
- ok:false (stage 1 gagal) → HARD GATE [PROXY GAGAL] seperti delegated. JANGAN karang scenario sendiri diam-diam.
- status `blocked` → preflight gagal (playwright_missing | browser_missing | base_url_unreachable | spec_invalid policy URL). Laporkan + fix, STOP.
- status `invalid` → tampilkan `errors` + `meta.e2e.diagnosis` (`first_error`, `categories`, `repair`) — kategori itu yang dilaporkan, bukan tebakan penyebab; tawarkan: perbaiki scenario bersama (main_agent edit JSON sesuai error) | ulang draft dengan task lebih jelas | batal.
- status `ready` → STEP 4.
- `meta.e2e.knowledge.offered` > 0 → draft membaca `e2e_knowledge.json` (hasil run sebelumnya untuk origin yang sama: alur login/logout, route, readiness, selector yang cocok). Sebut satu baris di rencana: "memakai N knowledge dari run sebelumnya". Draft tetap wajib mencocokkan selector ke kode.

## STEP 4 — Rencana (tampilkan, JANGAN minta izin jalan)
Cetak rencana supaya user tahu apa yang sedang berjalan, lalu LANGSUNG ke STEP 5. Tidak ada AskUserQuestion "Jalankan | Batal" — user yang memanggil /.verify-browser sudah menyatakan mau menjalankannya.
[BROWSER TEST PLAN]
target: <base_url> (loopback | .test | remote allow-listed: <origin>)
tampilan: headless | headed slow_mo <n> ms
auth: <nama key> — secrets.json profil <nama>: set | BELUM (missing_env; `secrets_file` di draft menyebut path + apakah template baru dibuat)
claims: - <id> | <severity> | <description> | <source_refs>
steps: <step id. aksi → target (within <container>) → siap bila <ready> → request <METHOD path> → claim_id> (ringkas, bahasa manusia)
existing_tests: <path covers id> | tidak ada
side_effects: tidak ada (write diblokir runtime) | <step id + side_effect + test_data.marker + request yang diharapkan>
cleanup: <cleans step id: langkah → assertion hasil> | no_cleanup_reason <alasan> | tidak ada
read_only_requests: <usulan draft: METHOD endpoint | source_refs | reason> | tidak ada
risiko: <data berubah (hanya app lokal), akun nyata, URL remote, missing_env, spec_uncertainties, cleanup tanpa rencana>
biaya: 1 panggilan review second_agent
retry: <n> percobaan maksimum bila run berakhir incomplete karena lingkungan | mati (allow_side_effects true)
knowledge: <N entri dari run sebelumnya dipakai draft> | belum ada
Dua hal — dan hanya dua — yang masih menghentikan langkah ini. Keduanya soal IZIN atau bahan yang belum ada, bukan soal "yakin mau jalan":
- missing_env tak kosong → minta user isi secrets.json (profil yang dipakai) dulu; jangan run sampai user bilang sudah. Tanpa credential, run pasti `env_missing`.
- read_only_requests tak kosong DAN belum tercakup `allowed_read_only_requests` dari config/request → AskUserQuestion multiSelect satu entri per opsi (description = source_refs + reason). HANYA yang dicentang ditulis ke `allowed_read_only_requests` saat STEP 5 sebagai `"POST <endpoint>"`. Tak dicentang = tetap diblok. DILARANG menambah entri yang tak diusulkan draft atau tak dikonfirmasi user. Sudah tercakup config → jangan tanya lagi.
Rencana salah → user bilang sendiri (Esc/instruksi baru); jangan pancing dengan pertanyaan.

Yang ketiga muncul SESUDAH run, bukan sebelumnya, dan dijawab dengan cara yang sama: `meta.e2e.destructive_pending` tak kosong → run tadi menemui DELETE atau request ber-`_method` yang belum diizinkan. AskUserQuestion multiSelect, satu entri per endpoint (`method` + `endpoint`; description = step yang memicunya bila diketahui). Endpoint datang dalam bentuk rute (`/api/items/:id`), bukan id sungguhan — menyetujuinya berarti menyetujui rute itu, dan katakan begitu saat bertanya. DILARANG menambah entri yang tak ada di `destructive_pending`, dan DILARANG menyetujui atas nama user.

Yang dicentang ditulis ke **`.workflow/e2e/permissions.json`**, bukan ke request. File itu berdampingan dengan `secrets.json` dan bertahan lintas sesi — jadi endpoint yang sudah disetujui sekali TIDAK ditanya lagi, di sesi mana pun. Bentuknya:
```json
{
  "allowed_destructive_requests": ["DELETE http://localhost:8000/api/items/:id"],
  "blocked_requests": ["* /api/payments", "DELETE /api/users/:id"]
}
```
Tulis persis string di field `entry`. Kalau file belum ada, buat dengan kedua key (`blocked_requests` boleh kosong). Jangan hapus atau susun ulang entri yang sudah ada; tambahkan saja. File rusak = run DITOLAK (`request_invalid`), bukan run dengan deny list kosong: JSON invalid, top level bukan object, `blocked_requests` bukan list, atau satu entry tak parse → error, tiap entry buruk disebut namanya. Dulu satu entry rusak membuang SELURUH deny list dengan satu warning dan run tetap jalan. `allowed_destructive_requests` rusak tetap warning + dibuang — membuang approval bikin run lebih ketat, membuang deny bikin lebih longgar. File tak ada = nol permissions, bukan error.

`blocked_requests` = daftar endpoint yang TIDAK BOLEH disentuh run, verb apa pun (`*` = semua method). Dicek paling awal, sebelum GET sekalipun dilepas, dan menang atas segala setting termasuk `allow_side_effects` dan `allowed_destructive_requests`. Dicek ulang di SETIAP hop redirect 307/308 — hop itu difetch guard, nol lewat route handler, jadi tanpa cek ulang write bisa didorong server ke endpoint yang ada di daftar ini. Redirect 301/302/303 dikirim ulang browser sebagai request baru yang lewat guard seperti biasa. Refusal-nya `blocked_by_policy`. Runtime nol menulis ke daftar ini dan request nol bisa menamainya — hanya user yang mengisinya. Kalau user menyebut endpoint yang "jangan pernah disentuh", tawarkan menambahkannya ke sini.

Daftar itu juga mengatur NAVIGASI, bukan cuma write. Tiap `goto`, tiap navigasi top-level di guard, dan tiap perubahan URL (`framenavigated` — termasuk `history.pushState`, meta refresh, dan redirect yang diikuti Chromium sendiri tanpa request baru) dicek ke dua kebijakan: origin (`allowed_origins`/`allow_remote`) dan deny list. Dulu cuma origin yang ditanya, jadi redirect dari origin yang diizinkan ke path yang di-deny pada origin sama lolos keduanya. `goto` ditolak sebelum mengirim apa pun; landing yang baru ketahuan setelah terjadi menggagalkan step-nya dengan `navigation_blocked` + alasan kebijakan mana yang menolak. Entry khusus method tidak memblokir navigasi: `DELETE /api/users/:id` melarang menghapus record itu, membuka alamatnya tetap read — cuma entry `GET` dan `*` yang berlaku.

## STEP 5 — Run (background, langsung setelah STEP 4)
Tulis ulang request.json: `"phase": "run"`, settings sama (atau yang diubah user), `"scenario"`, `"existing_tests"`, `"spec_notes"` disalin dari draft.json (scenario hasil edit bila ada). Panggil run script yang sama dengan <task> yang sama.
Headed (default) → browser muncul di layar user; beri tahu sebelum dispatch, sekali, tanpa menunggu jawaban.

## STEP 6 — Tag elemen (opsional, SATU konfirmasi batch)
Run mengusulkan `data-e2e` untuk elemen yang benar-benar terpakai. Usulan ada di `meta.e2e.tags` + file `tag-proposals.json`. Runtime TIDAK PERNAH menulis ke template — ini satu-satunya gate interaktif yang tersisa, karena ini satu-satunya langkah yang mengubah file milik user.
Batas usulan (runtime yang menegakkan, jangan ditawar): step `passed`, selector pemenang ber-provenance `source`, dan `selector_provenance.ref` menyebut `path:line` — file template di dalam project, bukan git-ignored, baris berisi PERSIS SATU opening tag dan nol markup dalam komentar (`<div><button>` ditolak, bukan ditebak sebagai div), belum punya `data-e2e`. Selector heuristic/runtime_probe nol alamat → nol usulan. DILARANG menambal dari DOM (`page.content()`): itu keluaran framework, bukan source project.
Alur:
1. Tampilkan entri `status: ready` sebagai diff (`old_line` → `new_line`) plus entri `skipped` beserta `reason` — yang skipped biasanya berarti `ref` draft sudah melenceng dari kode.
2. SATU AskUserQuestion untuk seluruh batch: Tulis semua | Pilih sebagian | Lewati.
3. Disetujui → edit tiap file memakai `old_line`/`new_line` PERSIS dari plan. Baris berubah sejak plan dibuat → lewati baris itu, jangan timpa. Path yang berubah jadi symlink keluar project juga ditolak — cek diulang saat tulis, bukan dipercaya dari plan.
4. Setelah tertulis → tawarkan /.promote dengan satu claim per tag (`e2e-tag-<tag>`, anchor `path:line` baru). `/.promote` yang memverifikasi anchor dan gate branch; jangan tulis ke knowledge dari sini.
Nol entri `ready` → lewati STEP 6 diam-diam, jangan tanya.

## Knowledge browser (otomatis, setara fact)
Tiap run mencatat apa yang TERBUKTI ke `.workflow/data/e2e-knowledge.jsonl`, per origin base_url: `auth.login` (goto halaman login s/d assertion pertama setelah `${E2E_PASS}`), `auth.logout`, `navigation`, `page_ready`, `selector` (cocok tepat satu elemen). Tanpa konfirmasi, seperti facts — tidak ter-Git.

Urutan selector di draft: `data-e2e` yang sudah ada di kode → label → cari elemennya di template. Yang boleh memimpin daftar kandidat, di depan role/label: `e2e` ber-provenance `source` dengan ref `path:line`, dan selector yang tercantum di `proven` pada `e2e_knowledge.json` (provenance `proven` — pernah cocok di run bersih dan belum meleset sejak itu), HANYA pada step dengan route dan `within` yang sama dengan entry-nya; di route/scope lain selector itu kandidat biasa. Sisanya tetap role+name > label > e2e > text > css. Runtime memeriksa `proven` ke knowledge store; mengaku `proven` tanpa dasar → `spec_invalid`. Selector `proven` yang meleset sekali turun ke urutan biasa, meleset dua kali dipensiunkan.
- Yang dicatat: step `passed`, bukan write, dari run yang lolos di percobaan PERTAMA. Pass hasil retry tidak dicatat sebagai bukti.
- Step gagal melemahkan entri yang cocok; 2 kali beruntun → entri pensiun (tak ditawarkan lagi). `--command clean` membuang entri pensiun dan yang anchor `path:line`-nya hilang; anchor yang cuma bergeser ikut pindah.
- Isi selalu bentuk placeholder (`${E2E_USER}`), nilai `fill` literal dibuang. Nol credential.
- Relay `meta.e2e.knowledge` hasil run: `added | confirmed | weakened | retired`. Jangan klaim knowledge "dipakai" bila `offered` 0.
- Promote: entri ber-anchor yang terbukti ≥3 run bisa diangkat jadi knowledge ter-Git lewat `/.promote` (claim `e2e-<kind>-<id>`). Tawarkan hanya bila user minta dokumentasi alur; `/.promote` yang memverifikasi anchor dan gate branch.

## Output (RELAY)
Hasil run = [VERIFICATION] canonical (meta.command=verify, meta.invocation=verify-browser). Relay apa adanya:
verdict (pass | fail | incomplete) | browser_verdict | reason | cleanup (`meta.e2e.cleanup.status`: passed | not_needed | not_planned | failed | not_run) | blocking_findings | not_verified | artifacts (`meta.e2e.artifacts`: report.json, events.jsonl, evidence.md, verification.md; saat gagal stepNN.html/png, trace.zip).
- `pass` HANYA dari runtime meta.verdict. Exit nonzero = bukan pass. `ok:true` saja bukan bukti lolos.
- Laporan per step (`report.json` → `trail`, dan `trail:` di evidence.md): step id → selector yang dipakai + jumlah match + fallback → readiness (kondisi, lama tunggu, yang tak tercapai) → request → hasil. Relay bagian yang relevan, jangan karang.
- Request (`report.json` → `requests`): tiap write (POST/PUT/PATCH/DELETE) dengan step, endpoint tersanitasi, status/kegagalan, durasi, `planned` (cocok `request` step) atau tambahan, `attribution: uncertain` bila terkirim di luar jendela step. HTTP 2xx saja BUKAN bukti CRUD berhasil — yang membuktikan assertion.
- Cleanup dilaporkan TERPISAH dari tes. `failed`/`not_run` → verdict `incomplete`, exit nonzero, walau semua claim terbukti; sebut data test mungkin tertinggal di app lokal. Jangan tampilkan sebagai sukses penuh.
- incomplete reason: request_missing | request_invalid | secrets_invalid | env_missing | spec_invalid | playwright_missing | browser_missing | base_url_unreachable | harness_error | unknown_origin | stuck | timeout | launch_failed | output_truncated | scenario_error | repeat_failure. Sebut artinya + langkah perbaikan.
- `meta.e2e.diagnosis` (tiap run non-pass + refusal brake): `cause` (enum) | `next_step` (fix_scenario | fix_app | fix_environment | raise_limit | investigate) | `failed` (step_id, action, claim, error_kind) | `fix_hint`. Relay ketiganya apa adanya — itu jawaban kenapa gagal, apa yang gagal, dan langkah berikutnya. `reusable: true` → tercatat sebagai knowledge `failure_hint` untuk draft berikutnya.
- `scenario_error` → kesalahan scenario sendiri (selector tebakan tak cocok, selector apa pun yang cocok >1 elemen, write/navigasi ditolak policy run). TIDAK di-retry, TIDAK dihitung rem 3x (`meta.e2e.repeat_not_counted`). Perbaiki scenario (edit JSON sesuai `diagnosis.failed` atau ulang draft), lalu run lagi.
- Failure `mutation_blocked` dengan detail `blocked_requests` → endpoint ada di deny list project. BUKAN sesuatu yang bisa dibuka dengan setting, dan DILARANG menawarkan menghapusnya dari `permissions.json` kecuali user yang minta. Laporkan endpoint-nya, lalu ubah scenario supaya tidak menyentuhnya.
- `repeat_failure` → runtime MENOLAK memulai browser: 3 run beruntun di origin yang sama berakhir dengan cara yang sama (`meta.e2e.repeat`, field `bucket`: `timeout` | `harness` | `app`). Untuk `app`, "sama" berarti signature sama (`meta.e2e.repeat.signature`): halaman, field/selector, dan kondisi gagal identik — kegagalan app yang berbeda memulai hitungan baru. Penyebabnya di luar scenario — lingkungan, data, atau credential. DILARANG mengedit scenario lalu mencoba lagi: itu persis yang tiga run sebelumnya lakukan, dan edit scenario TIDAK melepas rem. Laporkan bucket + reason terakhir, sebut apa yang perlu diperbaiki di luar scenario. Bucket `app` berarti aplikasinya yang gagal, bukan lingkungan — perbaiki app-nya. JANGAN sarankan sesi baru. Pemulihan: (1) perbaikan berupa perubahan file project (kode, config, fixture) → jalankan lagi; runtime melihat project berubah sejak kegagalan terakhir dan melepas rem sendiri (`meta.e2e.repeat_released.by = change_detected`). Runtime Exoma yang berubah (versi atau kode) juga melepas rem sendiri (`by = runtime_changed`). (2) perbaikan tanpa perubahan file (server dinyalakan, data direset, credential di luar repo) → run berikutnya pakai `settings.ignore_repeat_brake: true` di request, SEKALI, lalu kembalikan. Project di luar Git tak punya fingerprint → hanya jalur (2). Kegagalan berulang ini tercatat sebagai knowledge `repeat_failure` untuk draft berikutnya (lintas sesi) dan pensiun sendiri setelah satu run `pass`.
- Step gagal `not_ready` → indikator kesiapan app (loader, tombol aktif, konten AJAX) tak tercapai dalam step_timeout; origin unknown. Detail menyebut kondisi yang belum terpenuhi.
- Retry otomatis (`settings.max_retries`, default 2) hanya untuk `incomplete` berlatar lingkungan: harness_error, unknown_origin, stuck, timeout, output_truncated, launch_failed. `fail` origin=app TIDAK PERNAH diulang — mengulang bug nyata sampai ia sembunyi persis yang dicegah paket ini. Dua percobaan beruntun dengan hasil identik per step → berhenti, ditandai `stable`. Tiap percobaan ulang menulis artifact ke `retry<n>/`; `meta.e2e.attempts` merangkumnya. Sebut jumlah percobaan saat merelay.
- `allow_side_effects: true` → retry MATI total. Aksi write tak diulang saat timeout (request pertama mungkin sudah diproses server). Jangan sarankan "jalankan ulang otomatis"; periksa data dulu.
- Screenshot + trace tak diambil bila scenario pakai `${ENV}` (tak bisa di-scrub); HTML juga tak disimpan bila nilai credential < 4 karakter (`meta.e2e.secrets.unscrubbable`). Jangan minta buka screenshot bila bukti terstruktur cukup.
- fail origin=app → bug app, tawarkan /.analyze atau fix. origin scenario → scenario yang salah; origin harness/unknown → lingkungan/runtime atau belum bisa dibedakan. Ketiganya bukan bukti bug.
- Failure `mutation_blocked` dengan detail `a delete needs its exact endpoint in settings.allowed_destructive_requests` → BUKAN penolakan final, melainkan pertanyaan. Ikuti alur `destructive_pending` di STEP 4. Jangan tawarkan `allow_side_effects: true` sebagai jalan keluar — saklar itu tidak membuka DELETE.
- Failure `mutation_blocked` (origin scenario) → write diblok guard. Bukan bug app. Detail `allow_side_effects is false, settings.allow_local_side_effects does not cover this host` → host bukan host pengembangan, ATAU `allow_local_side_effects` dimatikan. Tawarkan: POST yang cuma baca → ulang draft agar diusulkan di `read_only_requests` dengan bukti handler | nyalakan kembali `allow_local_side_effects` bila host memang lokal | aktifkan side effect (hanya app lokal) | ubah alur jadi read-only. Detail `writes go only to a loopback or .test host, or one preflight approved and pinned` → write menuju host non-lokal; tak bisa diizinkan. Detail `redirect re-sends the body to ...` / `redirect navigates off policy` → server me-redirect write ke tujuan terlarang; body TIDAK terkirim ulang. Failure `navigation_blocked` dengan `followed by the browser` → redirect navigasi keluar origin sudah diikuti browser sebelum terdeteksi; step digagalkan, bukan dicegah.
- Verdict `incomplete` dengan gap `passed only on attempt N` → lolos hanya setelah retry. JANGAN relay sebagai pass: bisa lingkungan flaky atau bug app yang sesekali muncul. Detail `allowed_read_only_requests covers the endpoint, but the body is ...` → body GraphQL write; bukan kandidat read-only. Observation `mutation_blocked` (beacon/async) = warning saja.

## Batas
- Config project hanya memindahkan DEFAULT; tak satu pun knob di `config.json` melonggarkan policy origin atau write. Domain sungguhan tetap butuh `allow_remote` + `allowed_origins` persis — dua key itu HANYA dibaca dari config.json — dan write tetap dinilai dari alamat hasil resolve.
- Sisa risiko yang diketahui: patokan resolver menutup celah antara preflight dan run untuk chromium. Yang tak tertutup — host privat yang memang dikuasai pihak lain di jaringan yang sama, dan GET yang mengubah data (nol guard, seperti sebelumnya).
- Nilai credential tak pernah lewat chat, request, prompt, atau artifact. secrets.json bisa dibaca second_agent codex (read boundary NOT_ENFORCEABLE) — sebut ke user bila provider codex.
- Satu session = satu request. Session lain di project yang sama tak berbagi setting request — tapi berbagi section `e2e` di config.json.
- Metrik: tiap run = satu baris `kind: e2e_run` di `.workflow/data/quality.jsonl`; tiap draft = satu baris `kind: e2e_draft` (status, kategori error, hasil repair) — bukan run.
