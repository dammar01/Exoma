# Skill: provider
description: Pilih second_agent (provider → model → effort → model per route) lewat pertanyaan interaktif, lalu terapkan.

## Trigger
/.provider

## Batas
LOCAL. Nol second_agent, nol job, nol lock. Runtime yang menulis config — kamu TIDAK pernah menulis
`.workflow/second_agent.json` sendiri. Kamu cuma merender pilihan dan meneruskan satu string.

## STEP 1 — Baca katalog
    Semua OS (tool Bash): bash '<work_dir>/.workflow/run.sh' provider '' '<MAIN_SESSION_ID>'

Balasannya `meta.providers[]` (`name`, `installed`, `detail`, `models[] {id, efforts}`) plus
`meta.current` (provider/model/effort yang aktif, dan `routes` = model tiap route sekarang),
`meta.selectable_routes` (route yang boleh dipilih) dan `meta.source`.

`.workflow/run.*` MISSING → /.init dulu. Jangan panggil `python main.py` langsung.

## STEP 2 — Tanya, empat langkah berurutan
Renderer = AskUserQuestion, satu pertanyaan per call, jawaban sebelumnya memotong opsi berikutnya.

1. **Provider** — opsi dari `providers[]`. `installed:false` → tetap tampilkan, tulis di `description`
   bahwa CLI-nya tak ada di PATH; jangan jadikan opsi utama.
   `codex` dan `agy` → `description` WAJIB menyebut: tanpa batas baca (bisa membaca semua file
   project, `.env` termasuk) dan menerima seluruh environment — dianggap trusted provider. Batas
   baca yang ditegakkan hanya milik `opencode`. Ini peringatan, bukan penolakan: pilihannya milik user.
2. **Model** — opsi dari `models[]` provider terpilih. Lebih dari 4 → tampilkan 4 paling relevan;
   user selalu punya "Other" untuk mengetik id lain.
3. **Effort** — opsi dari `efforts` MILIK MODEL yang barusan dipilih, bukan milik provider.
   Nilai berbeda per model, jadi memakai daftar provider akan menawarkan yang upstream tolak.
   - `efforts` KOSONG (`[]`) → model itu tak menerima effort sama sekali. LEWATI pertanyaan
     ketiga, langsung ke pertanyaan keempat tanpa field effort. Memaksakan nilai akan ditolak runtime.
   - Model dari "Other" → `efforts` tak diketahui; tanyakan effort sebagai pertanyaan terbuka
     dan sebutkan bahwa nilainya tak bisa divalidasi.
4. **Route** — "Semua route pakai model yang sama?" Dua opsi: ya (semua ikut model di atas)
   dan tidak (pilih satu per satu). Sebutkan di `description` model yang berlaku sekarang per
   route dari `meta.current.routes` bila ada yang berbeda dari `default_model` — itu justru
   keadaan yang membuat pertanyaan ini ada.
   - **ya** → langsung STEP 3 dengan field routes `same`.
   - **tidak** → STEP 2b.

## STEP 2b — Model per route (hanya bila user jawab "tidak")
Satu pertanyaan untuk TIAP route di `meta.selectable_routes` (explore, plan, analyze, verify,
e2e_spec — lima). Batas tool 4 pertanyaan per call, jadi pecah jadi DUA call: call pertama
explore/plan/analyze/verify, call kedua e2e_spec (draft spec `/.verify-browser`). Opsi tiap
pertanyaan = `models[]` provider terpilih, model dari STEP 2 ditandai sebagai default di `description`.

Effort TIDAK ditanya per route: runtime memakai satu effort global, dan model yang tak menerima
effort sudah dijatuhkan sendiri oleh Router.

Batas keras tool: MAX 4 pertanyaan per call, 2-4 opsi per pertanyaan, `header` MAX 12 karakter.
Jangan bikin opsi "lainnya" sendiri — "Other" sudah otomatis.

## STEP 3 — Terapkan
    bash '<work_dir>/.workflow/run.sh' provider '<provider>|<model>|<effort>|<routes>' '<MAIN_SESSION_ID>'

Pemisah `|` — bukan `/`, karena id model opencode mengandung `/`.
Model atau effort dikosongkan → field itu di-clear. Untuk opencode, model kosong DITOLAK saat
call (`model_unset`): tanpa `-m` opencode memakai model terakhir yang dipakai di mesin ini.

Field `<routes>`:
- `same` (atau dikosongkan) → semua route memakai `<model>`.
- `explore=A,plan=B,analyze=C,verify=D,e2e_spec=E` → per route. Pemisah pasangan `,`, penetapan `=`.
  Route yang tak disebut ikut `<model>`. Nilai kosong (`explore=`) → route itu di-clear.
- Nama route di luar explore/plan/analyze/verify/e2e_spec → ditolak, nol yang ditulis.

Runtime menulis lima field plus `routes` sekaligus (`provider`, `provider_command`,
`provider_agent`, `default_model`, `effort`, `routes`) dan menyinkronkan hint
`runtime.second_agent` di config.json. Route ditulis key-wise: `timeout_seconds` dan `agent`
per-route yang sudah ada di file TIDAK hilang.
`ok:false` → NOL yang ditulis; relay `content` + `meta.next_action` apa adanya, jangan coba
memperbaiki sendiri, jangan menulis file.

## Output
[PROVIDER]
selected: <provider> / <model | provider default> / effort <effort | provider default>
routes: <isi meta.written.routes — tiap route + modelnya; sebut "semua ikut default" bila
  meta.routes_uniform true>
installed: <daftar provider terpasang>
written: <enam field dari meta.written>
config_hint: <updated true|false + alasan>
foreign_values: <isi meta.foreign_values, atau "kosong">  — TIDAK kosong = config menyebut provider
  lain di `provider_command`/`provider_agent`/model; tampilkan penuh, jangan diringkas hilang.
warnings: <meta.warnings, atau "tidak ada">
read_boundary: <opencode → "ditegakkan (opencode.json)" | codex/agy → "TIDAK ada — second_agent bisa baca semua file termasuk `.env` dan menerima seluruh environment (trusted provider)">
status: APPLIED | REFUSED

REFUSED → sebut alasan + next_action, jangan tawarkan lanjut ke command lain.
APPLIED → efeknya mulai panggilan delegated berikutnya; job yang sedang jalan tak berubah.
