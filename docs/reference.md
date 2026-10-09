# agent-workflow v3.8.3

Runtime orkestrasi mandiri untuk alur kerja dua-agent. Tanpa dependency pihak ketiga.

## Ringkas

Dua peran dipisah tegas:

- **main_agent** — agent yang kamu pakai (Claude Code, Codex, Cursor, dll). Orchestrator, antarmuka user, dan **satu-satunya** yang boleh menulis file.
- **second_agent** — provider terpilih (OpenCode, Codex, atau Agy), pengumpul bukti **read-only**. Bukan jawaban akhir. Batas tulis tiap provider berbeda; lihat [Security](security.md).

Runtime ini duduk di antara keduanya: menerima command, merakit prompt terstruktur, menjalankan CLI provider terpilih (mis. `opencode run`), memvalidasi bentuk output, menyimpan state per-sesi, lalu mengembalikan JSON contract yang stabil:

```json
{ "ok": true, "content": "...", "meta": {} }
```

`digest` ditambahkan hanya bila output second_agent membawa blok `[DIGEST]` yang dapat diparse; ia bukan field wajib.

Evidence, config, state per-sesi, dan binding sesi provider (`.workflow/data/provider-sessions/`) hidup project-local di `.workflow/` pada project target. Antrean job dan cache lintas project milik tool tetap berada di `storage/` repo ini (lihat [Layout workspace](#layout-workspace)).

---

## Alur prompt sampai response

```text
Prompt user
→ main_agent: auto-detect intent atau pakai override /.
→ .workflow/run.{ps1|sh}
→ main.py await → JobManager → detached worker
→ Executor → Router
→ Graphify leads + fact store → PromptBuilder
→ adapter provider terpilih (OpenCode/Codex/Agy) → second_agent
→ sub-agent fan-out bila aktif
→ validasi contract + digest + penyimpanan evidence
→ await mengembalikan {ok, content, meta, digest?}
→ main_agent relay/synthesis
→ response user
```

Auto-intent, output `[OPTIONS]` pada `/.plan`, dan keputusan menjalankan `/.verify` setelah `/.execute` berada di lapisan prompt **main_agent**. Runtime Python tidak menerima pesan user mentah dan tidak melihat response final yang ditulis main_agent. Runtime hanya mengelola delegasi, evidence, job, dan policy metadata.

Diagram ini menggambarkan jalur delegated penuh. `sweep` dipotong lebih awal oleh `main.run()` dan selesai lokal tanpa `Executor`; `verify` dengan `verify_mode: syntax` berhenti di `quick_verify` sebelum Router, Graphify, PromptBuilder, OpenCode, dan second_agent.

---

## Envelope response

Setiap hasil berbentuk `{ok, content, meta}`. Field lain bersifat kondisional:

| Field | Kapan ada | Isi |
|---|---|---|
| `digest` | output second_agent membawa blok `[DIGEST]` yang dapat diparse | ringkasan untuk direlay. `digest.anchors = {certified, total}` ditambahkan bila evidence mencatat anchor; confidence yang diturunkan karena anchor/graph menyimpan nilai aslinya di `digest.confidence_reported` dan alasannya di `digest.confidence_capped_by` |
| `evidence_ref` | hanya jalur evidence explore/plan/analyze | `{artifact_path, anchors, certified_anchors, reused}`. `reused: true` = artifact sesi lampau dipakai ulang (lihat [Evidence reuse dan boundary](#evidence-reuse-dan-boundary)) |
| `meta.task_cap`, `meta.task_cap_source` | setiap call delegated | batas panjang task efektif. `task_cap_source`: `transport` (dihitung dari sisa ruang command line argv), `floor` (scaffolding hampir memenuhi command line; angka minimum), `policy` (opt-in `AI_PROXY_MAX_TASK_CHARS_NO_ARGV`), `unbounded` (`task_cap: null`; transport stdin/file atau argv di luar Windows) |
| `meta.task_truncated` | task dipotong oleh cap | sinyal untuk meringkas instruksi, bukan untuk memecah call |
| `meta.content_mode`, `meta.content_full_chars` | `AI_PROXY_SLIM_CONTENT` aktif, content panjang, dan artifact ada di disk | `content_mode: "ref_only"`: `content` hanya preview; teks lengkap di `evidence_ref.artifact_path` |
| `meta.policy` | setiap call delegated | `auto_verify_after_execute`, `verify_mode`, `subagent_fanout_enabled` yang efektif |
| `meta.error_type`, `meta.next_action` | `ok: false` | keduanya wajib; `error_type` dari himpunan tertutup di bawah |

### `error_type`

Himpunan tertutup di `core/evidence/contract.py` `ERROR_TYPES`; `make_error` menolak nama di luar himpunan itu, dan suite `error-types` memastikan setiap literal yang dipakai kode terdaftar.

| Kelompok | `error_type` |
|---|---|
| Provider dan worker | `timeout`, `worker_died`, `worker_crashed`, `worker_stalled`, `job_expired`, `rate_limited`, `streaming_failed`, `sandbox_unavailable`, `second_agent_unavailable`, `command_not_found`, `permission_denied` |
| Output second_agent | `empty_output`, `invalid_evidence`, `session_capture_failed`, `task_truncated`, `fact_ingest_failed` |
| Prompt dan command line | `prompt_too_long`, `unsafe_command_line` |
| Config provider | `provider_config_missing`, `provider_config_invalid`, `model_unset`, `invalid_provider_selection` |
| Job, sesi, workspace | `job_already_running`, `job_submit_error`, `worker_capacity`, `runtime_lock`, `session_state_error`, `routing_error`, `workflow_init_error`, `workflow_upgrade_error`, `sweep_git_error`, `budget_exceeded` |
| Promote dan knowledge | `promote_input_missing`, `promote_input_unreadable`, `promote_existing_unreadable`, `promote_not_on_branch`, `promote_not_production_branch`, `promote_write_rejected`, `invalid_knowledge`, `secret_shaped_content`, `ignored_source`, `ignored_destination` |
| Lainnya | `unknown` |

`worker_died` dengan `meta.reason: recovery_exhausted` berarti recovery otomatis sudah dipakai; jangan ulang otomatis (lihat [Job asinkron & pemulihan](#job-asinkron--pemulihan)).

---

## Prasyarat

| Kebutuhan | Catatan |
|---|---|
| Python 3.10+ | sintaks `X \| None` dipakai di seluruh kode |
| `opencode` di `PATH` | hanya untuk command terdelegasi |
| `git` di `PATH` | opsional; dipakai `sweep` dan mode verify `syntax` |
| Dependency runtime | **nol**; seluruh runtime memakai standard library |
| Graphify | opsional |

Cek cepat:

```bash
python3 --version   # atau: python --version
opencode --version
git --version
```

```powershell
python --version
opencode --version
git --version
```

---

## Install (v3.8.3)

### Anggota tim baru — urutan lengkap dari nol

Empat langkah, dijalankan sekali per mesin (kecuali langkah 4, sekali per project):

```bash
git clone <repo> && cd agent-workflow      # 1. ambil tool-nya
python install.py --apply                  # 2. pasang config agent global
export AGENT_PATH="$PWD/main.py"           # 3. beri tahu runtime letak main.py
                                           #    (permanenkan — lihat "Set AGENT_PATH")
python main.py --command init --work-dir /path/to/project-anda   # 4. per project
```

Verifikasi sebelum memakainya:

```bash
python install.py --check                  # bundle + instalasi global
cd /path/to/project-anda && .workflow/run.sh doctor    # .workflow\run.ps1 di Windows
```

`doctor` harus melaporkan `READY` dengan nol issue. Selain itu, baca `recommended_fixes`
sebelum melanjutkan — status `NOT_READY` berarti ada pintu masuk yang benar-benar rusak,
bukan sekadar catatan gaya.

Skrip entry di `.workflow/` memanggang path absolut mesin tempat `init` dijalankan, jadi
langkah 4 milik masing-masing orang. Mengcommit `.workflow/` ke repo bersama tidak akan
menolong siapa pun; `init` sudah menambahkannya ke `.gitignore`.

### Detail installer

Clone → jalankan satu script → terkonfigurasi.

```bash
git clone <repo> && cd agent-workflow
python install.py            # DRY RUN — tampilkan semua perubahan, tulis nol
python install.py --apply    # baru menulis
python install.py --apply --only-command      # workflow aktif hanya lewat /.<command>
python install.py --apply --provider codex --model gpt-5.6-sol   # pilih second agent tanpa ditanya
python install.py --check                     # cek bundle + instalasi (termasuk project bila cwd punya .workflow/)
python install.py --rollback                  # dry-run rollback terakhir
python install.py --rollback --apply          # rollback setelah preflight hash
python install.py --uninstall                 # dry-run: apa yang akan dicabut
python install.py --uninstall --apply         # cabut, ber-receipt (bisa di-rollback)
```

Semua flag `install.py`:

| Flag | Efek | Tulis? |
|---|---|---|
| (tanpa flag) | dry run: cetak rencana | tidak |
| `--apply` | jalankan rencana; digabung dengan flag lain di bawah untuk benar-benar menulis | ya |
| `--only-command` | mode command-only (lihat "Mode intent"); disimpan, berlaku untuk install berikutnya | dengan `--apply` |
| `--auto-intent` | kembalikan mode auto-intent | dengan `--apply` |
| `--provider NAME` / `--model ID` | pilihan second agent untuk seed (`--model` butuh `--provider`) | dengan `--apply` |
| `--with-e2e` | juga pasang extra Playwright + Chromium untuk `/.verify-browser` | dengan `--apply` |
| `--set-env` | permanenkan `AGENT_PATH` (Windows: `HKCU\Environment`; POSIX: `~/.bashrc`/`~/.zshrc`) | dengan `--apply` |
| `--check` | laporan drift bundle, instalasi global, dan project bila cwd punya `.workflow/` | tidak |
| `--rollback [ID]` | batalkan satu install/uninstall dari receipt-nya (default: terbaru) | dengan `--apply` |
| `--uninstall` | cabut yang dipasang workflow (lihat "Uninstall") | dengan `--apply` |

`AGENT_HOME` (env) mengganti `~` sebagai home target — dipakai test dan e2e installer untuk
memasang ke HOME sementara.

Drift bundle (`--check`, dan penolakan `--apply`) dihitung dua arah: file `dist/` yang hash-nya
tak cocok atau tak tercatat di `dist/manifest.json`, **dan** entri manifest yang file sumbernya
di `dist/config/` sudah tidak ada. `doctor` (`checks.bundle_integrity`) memvalidasi bentuk tiap
entri manifest lebih dulu: entri yang bukan objek, tanpa `path`, atau tanpa `sha256` 64-hex membuat cek
berhenti dengan `error` + `malformed` (indeks entri), yang `doctor` jadikan issue — bukan drift
instalasi lokal.

**Dry run adalah default, disengaja.** Script ini menulis ke config agent global — dibaca setiap project di mesin itu. Kesalahan di sini tidak terkurung dalam satu repo.

Yang dilakukan `--apply`:

| Target | Strategi |
|---|---|
| `~/.claude/CLAUDE.md` | ganti isi **di antara marker** `WORKFLOW-MAIN-AGENT` — tulisan tangan di luar marker selamat. File belum ada → ditulis **hanya blok marker** |
| `~/.claude/skills/*.md`, `~/.claude/commands/*.md` | replace (template) |
| `~/.claude/hooks/*` | replace; hanya flavour OS aktif (`.ps1` di Windows, `.sh` di POSIX) |
| `~/.claude/settings.json` | tambah hook dan `statusLine` milik workflow; hook dan key user dipertahankan (lihat "Hook yang didaftarkan") |
| `~/.config/opencode/AGENTS.md`, `~/.codex/AGENTS.md`, `~/.agy/AGENTS.md` | ganti isi di antara marker `WORKFLOW-SECOND-AGENT`; file belum ada → hanya blok marker. Blok itu hanya berlaku untuk call runtime (instruksi dibuka `[WORKFLOW_AGENT]`); sesi provider yang dipakai user langsung mengabaikannya |
| `~/.claude/.workflow-install-mode.json`, `~/.claude/.workflow-installed.json` | mode intent terpilih dan ledger file yang ditulis install (untuk membersihkan file basi) |
| `~/.config/opencode/opencode.{json,jsonc}` | merge additive untuk config umum; permission `agent.plan` milik workflow ditegakkan, key user lain dipertahankan |
| `~/.config/opencode/agents/*.md` | replace — satu roster subagent global, dipakai semua project yang dikelola workflow |
| `<project_root>/opencode.json` | **bukan** tugas installer — dipasang dan di-refresh oleh `init`/`upgrade` (deny-rule file rahasia ditegakkan tiap kali) |

Semua yang akan tertimpa di-backup dulu ke `~/.claude/backups/install_<timestamp>/`.
Receipt schema v2 mencatat hash sebelum dan sesudah untuk setiap file yang dibuat atau
diubah, termasuk `settings.json` dan file mode intent. Rollback memvalidasi seluruh
destination dan backup sebelum menulis apa pun; satu perubahan setelah instalasi membuat
rollback berhenti dengan konflik, bukan menimpa edit user atau melakukan rollback parsial.
Receipt (`~/.claude/backups/install_<timestamp>/install_receipt.json`) ditulis **inkremental**:
sekali sebelum destination apa pun berubah, lalu ulang setelah tiap langkah yang tercatat,
dengan `complete: false` sampai tulis final. Tiap entry berisi `action`, `key`, `dest`,
`backup`, `pre_sha256`, dan `post_sha256`, dan dicatat setelah file-nya selesai ditulis.
Instalasi yang mati di tengah (file terkunci, disk penuh, Ctrl+C) tetap meninggalkan receipt:
`--rollback` menerimanya, mencetak `NOTE: that install was interrupted; undoing the steps it
recorded.`, dan membatalkan langkah yang tercatat saja. Langkah yang mati saat menulis tak
punya entry — backup-nya tetap di direktori backup dan disalin balik manual. Apply yang tidak
mengubah apa pun tidak meninggalkan receipt.
Receipt mencakup target instalasi global dan bundle. Init/upgrade stateful pada `.workflow/`
serta penambahan `.workflow/` ke `.gitignore` tidak masuk rollback installer karena dapat
memuat session dan state project yang tidak aman dihapus otomatis.

#### Seed `config/second_agent.seed.json`

`config/second_agent.seed.json` ada di `.gitignore`. Installer **membangunnya ulang setiap
`--apply`** dari `second_agent.example.json` — file inilah yang disalin `init` ke tiap
`.workflow/second_agent.json` baru (`adapters/install/opencode_install._copy_provider_config`,
dengan example sebagai fallback terakhir). Runtime sendiri tidak pernah membacanya: project
tanpa `.workflow/second_agent.json` ditolak `provider_config_missing`, file yang rusak ditolak
`provider_config_invalid`. Tidak ada lagi fallback diam-diam ke config level mesin.

Yang dibawa dari seed sebelumnya hanya PILIHAN (`provider`, `default_model`, model per
route); semua key lain dibangun ulang dari example, jadi tak ada key basi yang terbawa.
Urutan penentuan pilihan:

| Kondisi | Yang terjadi |
|---|---|
| `--provider NAME` (opsional `--model ID`) | dipakai apa adanya, nol pertanyaan |
| seed sebelumnya ada | pilihan lamanya dipakai lagi di atas example baru |
| `--apply` dari terminal, belum ada seed | installer **bertanya** provider lalu model |
| non-interaktif, belum ada seed | `second_agent.example.json` disalin apa adanya |

`config/second_agent.json` lama (ditulis sekali, tak pernah di-refresh — sumber
`opencode/mimo-v2.5-free` yang ikut tersalin ke project baru) di-rename jadi
`second_agent.json.retired`; pilihannya **tidak** dibawa dan disebut di warning.

Jalur non-interaktif bukan kelalaian: `tools/e2e/e2e_installer.py` dan CI memanggil
`install.py` tanpa stdin, dan prompt di sana akan **menggantung** run, bukan menggagalkannya.

Prompt menampilkan tiap provider beserta apakah CLI-nya ada di `PATH` dan apakah ia butuh
opt-in (`AI_PROXY_<PROVIDER>_OPT_IN`), lalu shortlist model provider terpilih beserta
reasoning effort yang diterima masing-masing. Blank pada provider membatalkan pemilihan
(example disalin). Blank pada model membiarkan `default_model` kosong — untuk opencode itu
ditolak saat call (`model_unset`), karena tanpa `-m` opencode memakai model terakhir yang
dipakai di mesin itu. Id model di luar shortlist tetap diterima dengan warning.

Model dipin ke `SELECTABLE_ROUTES` (`explore`, `plan`, `analyze`, `verify`, `e2e_spec`);
`sweep` dibiarkan. `routes` diedit per-key lewat `_merge_routes` supaya `timeout_seconds`
dan `agent` per-route tidak ikut terbuang.

Seed **tidak** masuk install receipt, alasan sama dengan `--set-env`: entry receipt tanpa
backup akan DIHAPUS saat `--rollback`, sedangkan file ini menampung editan user setelah
dibuat.

`--check` melaporkannya sebagai baris `second agent default:` terpisah — `NOT SET`,
`UNREADABLE`, atau `<provider>, model=<id>` — dan sengaja **di luar** hitungan drift:
file ini nol padanan di `dist/` sehingga tak punya sumber untuk drift, dan ketiadaannya
bukan kerusakan (init masih jalan lewat example).

Bila installer dijalankan dari dalam project yang sudah memiliki `.workflow/`, ia menjalankan **upgrade in-place**: workspace layout lama dimigrasi ke `.workflow/data/` (dengan backup), scripts diregenerasi, stamp versi config diperbarui, `<project_root>/opencode.json` di-refresh, dan `sessions/` dipertahankan. Workspace baru tidak di-scaffold oleh installer — pakai `python main.py --command init --work-dir DIR` (skill `/.init`). Upgrade workspace ditolak bila masih ada job aktif; install global tetap selesai dan warning harus diperiksa.

### Statusline (badge second agent)

Installer memasang `~/.claude/hooks/workflow-statusline.<ps1|sh>` dan mendaftarkannya sebagai
`statusLine` di `settings.json`.

`settings.json` **additive**: hanya key yang belum ada yang ditulis, dan template sejak 3.8.0
hanya membawa `hooks` dan `statusLine` — `model`, `enabledPlugins`,
`extraKnownMarketplaces`, `effortLevel`, dan `permissions.defaultMode` tidak lagi ditanam
(itu preferensi, bukan kebutuhan workflow; yang sudah tertanam oleh rilis lama dibiarkan). Sudah punya
`statusLine` sendiri → punyamu menang, badge tak muncul, dan tabrakannya dilaporkan di
output install. Hapus key itu lalu jalankan ulang `install.py --apply` bila ingin memakai
yang dikirim.

File script-nya sendiri ikut aturan `~/.claude/hooks/*` di tabel di atas: **replace**,
dengan backup lebih dulu. Punya `workflow-statusline.ps1` buatan sendiri di sana → ia tertimpa
(salinannya ada di `~/.claude/backups/install_<timestamp>/`). Yang dipertahankan adalah
key `statusLine` di `settings.json`, bukan file di `hooks/`.

```
agent-workflow | Second Agent 15.2M tok (14.5M cached) / 8 calls | Saved 642.7k tok
```

| bagian | artinya |
|---|---|
| `Second Agent` | seluruh token yang diproses provider untuk sesi ini, cache read termasuk. `actual_*` didahulukan, estimasi `chars//4` cuma cadangan pada baris yang tak terhitung provider |
| `(… cached)` | porsi cache read di dalam angka itu. Dicetak karena pada run agentic ia ~95% dari total — konteks sama dikirim ulang tiap langkah internal — dan angka sebesar itu tanpa penjelasan di sebelahnya terbaca sebagai bug, bukan fakta |
| `calls` | jumlah **command**, bukan baris. Continuation menulis satu baris per invocation; menghitung baris akan membuat angka melompat tiap kali ada retry |
| `Saved` | yang ditangani second agent dan **tak pernah** sampai ke context main agent: input segar (file dan output tool yang ia baca) plus reasoning (ada di dalam output, tak pernah jadi teks). Cache read dikecualikan — itu konteks yang sama dikirim ulang, bukan material baru. Teks jawabannya juga dikecualikan, karena ia **memang** sampai ke sini |
| `~` | ada baris yang jatuh ke estimasi `chars//4`. Sesi yang seluruh barisnya terhitung provider tak membawa tanda ini — tak ada yang ditebak di dalamnya |

Kedua angka selalu tampil, termasuk saat bernilai nol. Segmen yang hilang terbaca sebagai rusak, bukan sebagai kosong.

Angka diambil dari `<project_root>/.workflow/data/usage.jsonl` (atau `.workflow/usage.jsonl` pada workspace yang belum di-upgrade). Flavor PowerShell meng-cache hasilnya 30 detik; flavor `.sh` menghitung
ulang tiap render (angka sama, hanya biaya baca berbeda). Sesi Claude
dipetakan ke `MAIN_SESSION_ID` lewat `~/.claude/session_registry.json` yang ditulis
`session-bind`. Belum ada `usage.jsonl`, atau sesi belum terpetakan, badge cuma menampilkan
nama project — tak pernah menggagalkan prompt.

**Semua angka ber-cakupan sesi.** `/clear` dan `/compact` memberi `MAIN_SESSION_ID` baru
(`resume` memakai ulang yang lama), jadi ketiganya kembali nol bersamaan. Itu disengaja:
angka seumur project di sebelah angka sesi terbaca sebagai rasio yang tak pernah diukur —
tepat setelah `/clear`, bar sempat berbunyi `Second Agent 0 | Saved 129.5k`.

Soal besarnya angka: pada satu sesi nyata provider memproses 15,2M token, tetapi 14,5M di
antaranya cache read — hanya 595k yang input segar. Angka itu jujur ke apa yang diproses,
bukan ke biaya: cache read ditagih jauh lebih murah, dan runtime sengaja tak punya price
table, jadi badge tak pernah menyebut nominal uang.

`Saved` mengukur kompresi **pekerjaan**, bukan kompresi jawaban. Pada sesi contoh: 594.684
token input segar (file yang dibaca, output tool) plus 48.050 reasoning = 642.734 yang
diproses second agent dan nol di antaranya masuk ke context main agent. Yang sampai ke sini
cuma 34.018 token teks jawaban, dan itu sengaja **tidak** diklaim hemat — mengklaimnya
berarti memuji main agent karena tak membaca apa yang baru saja ia baca.

Cache read juga **tidak** masuk `Saved`, meski ia masuk `Second Agent`. Kedua angka memang
menjawab pertanyaan berbeda: yang pertama "berapa yang diproses", yang kedua "berapa yang
akan menempati jendela main agent bila ia mengerjakannya sendiri". Konteks yang sama
dikirim ulang delapan puluh kali dihitung sekali pada pertanyaan kedua. Memasukkannya
membuat `Saved` jadi 15,1M — 99,8% dari angka di sebelahnya, yakni satu angka dicetak dua
kali. Sebagai 642,7k ia 4% dari total, dan dua angka itu berhenti saling membebek.

Versi sebelumnya memakai `premium_context_avoided_tokens` (jawaban penuh dikurangi digest)
dan menghasilkan 11,7k untuk sesi yang sama — 50× lebih kecil. Field itu kini jadi cadangan
untuk baris yang tak terhitung provider, karena pada baris seperti itu estimasi `chars//4`
mengukur panjang **prompt**, bukan berapa yang dibaca. Baris cadangan menyalakan `~`.

### Hook yang didaftarkan

| Event | Matcher | Script | Fungsi |
|---|---|---|---|
| `SessionStart` | `startup\|resume\|clear\|compact` | `session-bind` | mengikat sesi Claude ke `MAIN_SESSION_ID` |
| `UserPromptSubmit` | — | `intent-gate-set` | mode auto-intent saja: pasang marker gate untuk prompt delegated |
| `UserPromptSubmit` | — | `task-events` | skill lokal dari prompt; snapshot HEAD |
| `PreToolUse` | `mcp__.*\|Read\|Grep\|Glob\|Bash\|PowerShell\|Write\|Edit\|MultiEdit\|NotebookEdit` | `intent-gate-check` | blokir gather tool selama marker gate ada; Bash/PowerShell hanya lolos untuk satu panggilan polos ke `<root>/.workflow/{run,check,inspect}` (di-parse: Bash dipecah kata ala POSIX, PowerShell lewat parser PowerShell; argumen konstan; interpreter hanya `powershell`/`pwsh` dengan `-NoProfile`/`-NonInteractive`/`-ExecutionPolicy Bypass`/`-File`, atau `bash`/`sh` tanpa flag; path di luar root, `..`, dan nama mirip ditolak); Write/Edit lolos kecuali ke script runner itu; marker berintent `verify` meloloskan jalur pilih-test (lihat "Mode intent") |
| `PostToolUse` | `Skill\|Edit\|Write\|MultiEdit\|NotebookEdit` | `task-events` | skill via tool `Skill`, file diedit |
| `Stop` | — | `graph-refresh` | regenerasi graph setelah turn yang mengimplementasi (detached) |
| `Stop` | — | `task-events` | commit sejak snapshot HEAD |

Hook dikenali dari nama script-nya. Saat install, hook milik workflow di-refresh per event,
hook user dibiarkan, dan command script workflow yang tidak lagi didaftarkan rilis ini di
suatu event dicabut dari event itu (misalnya `task-events` di `PreToolUse` dari draft 3.8.0).
Di POSIX command `powershell ... .ps1` ditulis ulang menjadi `bash ... .sh`.

### Uninstall

`python install.py --uninstall` (dry run) lalu `--uninstall --apply`:

- blok `WORKFLOW-MAIN-AGENT` / `WORKFLOW-SECOND-AGENT` dipotong dari CLAUDE.md dan tiap
  AGENTS.md; teks user di atas dan di bawahnya tetap. File yang isinya hanya blok itu dihapus;
- skill, command, hook, dan agent provider dihapus **hanya** bila isinya masih persis yang
  ditulis install (ledger atau receipt); file yang sudah diedit dibiarkan dan disebut;
- `settings.json` kehilangan command hook yang menjalankan script workflow (dikirim atau
  pensiun) dan `statusLine` workflow; key lain tetap, termasuk yang dulu ditanam rilis lama.
  Sebuah command dianggap milik workflow hanya bila script-nya ada di folder hook terpasang,
  `<home>/.claude/hooks/<nama>.ps1|.sh` (`<home>` = home install, `~`, `$HOME`,
  `%USERPROFILE%`, `$env:USERPROFILE`, atau path berbentuk home user), dan namanya dikirim atau
  pensiun. Script user bernama sama di folder lain (`~/tools/task-events.sh`) tidak disentuh
  install maupun uninstall; satu command yang menjalankan script kita plus script lain dianggap
  milik user;
- CLAUDE.md: hanya sambungan tempat blok dipotong yang dirapikan (satu baris kosong); baris
  kosong di bagian lain file tetap byte-identik;
- mode intent dan ledger dihapus.

`settings.json` dibersihkan **sebelum** script hook dihapus, dibaca longgar (BOM, cp1252). Bila
tak bisa di-parse atau bukan object JSON, semua script `hooks/` dibiarkan dan uninstall
memperingatkannya, sehingga tak ada hook yang menunjuk file hilang. File instruksi yang
markernya tak berpasangan (START tanpa END, END tanpa START, END sebelum START, atau lebih dari
satu blok) ditolak saat install (aksi `refused` + peringatan berisi path dan kedua marker) dan
dibiarkan saat uninstall.

Tidak disentuh: `config/second_agent.seed.json`, `AGENT_PATH` dari `--set-env`, dan
`.workflow/` di tiap project. Semua perubahan lewat backup + receipt yang sama dengan install,
jadi `python install.py --rollback --apply` membatalkan uninstall.

### Mode intent

Default installer adalah **auto-intent**: bahasa natural dapat dipetakan ke command dan
hook `UserPromptSubmit` mengaktifkan pre-flight gate. Seluruh blok workflow berlaku di
setiap pesan.

`--only-command` membuat workflow **aktif hanya saat dipanggil**: blok yang terpasang dibuka
stanza "Cakupan aktif" — aturan workflow (gaya caveman, Output Contract, gate, `[NEXT]`,
Graphify, Global Forbidden) berlaku hanya setelah user memanggil `/.<command>` atau skill
workflow dan selama command itu berjalan. Pesan tanpa prefix adalah chat biasa yang
mengikuti instruksi user di luar blok (CLAUDE.md user, memory, project). Hook
`intent-gate-set` tidak didaftarkan; hook pasif (`session-bind`, `task-events`,
`graph-refresh`) dan `statusLine` tetap, karena hanya mencatat. Jalankan `--auto-intent`
untuk memulihkan mode default. Tanpa kedua flag, upgrade mempertahankan mode sebelumnya.

Prompt yang diawali `<task-notification>` (notifikasi background task yang Claude Code kirim
lewat `UserPromptSubmit`) diabaikan `intent-gate-set`: tidak memasang marker dan tidak menghapus
marker yang dipasang prompt user di turn yang sama (DEC-045). Sebelumnya nama task atau potongan
output di notifikasi bisa cocok dengan NL map dan memasang gate yang tidak diminta.

Sampai 3.8.0 `intent-gate-set` membaca prompt dari field `user_prompt`, yang tidak pernah
dikirim Claude Code (field-nya `prompt`). Akibatnya gate runtime tidak pernah aktif dan hanya
lapisan prompt yang bekerja. Sejak perbaikan itu, di mode auto-intent `intent-gate-check`
benar-benar memblokir gather tool sesudah prompt yang terpetakan ke command delegated sampai
`.workflow/run` dipanggil; escape-nya tetap `WORKFLOW_LOCAL_MODE=1` atau `local_mode.flag`.

Runner hanya lolos bila ia command itu sendiri: token pertama adalah path
`.workflow/{run,check,inspect}.{ps1,sh}` (polos atau ber-quote), boleh didahului
`powershell`/`pwsh`/`bash`/`sh` beserta flag-nya. Path runner yang muncul sebagai argumen
command lain (`python -c "..." x/.workflow/run.sh`) tetap diblokir.

Field `command` di marker memilih aturan lolos. Untuk `verify`, main_agent harus memilih test
sebelum delegasi, jadi tiga hal lolos: `git diff` bersih (tanpa `&&`, `;`, pipe, redirect)
dengan `--name-only`, `--name-status`, atau `--stat[=N]`, plus opsional `--cached`, `--staged`,
`--relative`, `--no-renames`, `--no-color`, `--`, ref, dan path (opsi lain seperti `-p` atau
`--output` tetap diblokir); Read `<root>/.workflow/config.json`; Read/Write
`sessions/<id>/verify/tests.json` milik sesi itu. Dependents hanya bisa disimpulkan dari nama
file di diff. Matcher `PreToolUse` memuat `Write`/`Edit`/`MultiEdit`/`NotebookEdit`, tetapi
hook hanya menolak tulis ke script runner `.workflow/{run,check,inspect}` (ekstensi apa pun)
selama marker ada (menulis ulang runner lalu menjalankannya akan membuat allowlist runner jadi command bebas);
tulis ke file lain, termasuk `tests.json`, lolos.
`.ps1` hook diuji di Windows PowerShell 5.1 (`powershell`), shell yang dipasang settings.

Untuk semua `command` di marker, Read definisi skill juga lolos: path absolut tanpa segmen
`..`, berakhiran `.md`, file-nya ada, dan path aslinya (semua symlink/junction diikuti) berada
di dalam path asli `~/.claude/skills`. Symlink boleh menunjuk ke mana pun di dalam folder skill,
tidak keluar darinya. Alasannya: tool Skill menyuruh agent membaca `~/.claude/skills/<name>.md`
sesudah prompt yang memanggilnya mempersenjatai gate, sehingga instruksi cara dispatch terkunci
sampai sesudah dispatch (DEC-039). Grep/Glob atas folder itu, file selain `.md`, dan path
relatif tetap diblokir.

Untuk semua `command` di marker, **Read presisi** juga lolos (DEC-043): file yang ada di dalam
project root (path relatif di-resolve terhadap root, symlink diikuti), bukan di bawah
`.workflow/`, dengan bentang paling banyak 200 baris — `limit` berupa angka 1–200, atau tanpa
`limit` pada file yang memang ≤ 200 baris (file > 1 MB tak dihitung, jadi tak lolos). Paling
banyak 2 per marker; hitungannya di `runtime/delegated.reads`, dikunci ke `set_at` marker,
sehingga marker baru mulai dari nol. Ini angka yang ditegakkan untuk pengecualian "slice
kecil, satu/dua anchor" di aturan Pre-flight gate `CLAUDE.md`: atribusi file:line oleh main
agent sendiri, bukan pengumpulan bukti. Read ke-3, bentang > 200 baris, file besar tanpa
`limit`, file di luar root, Grep, Glob, MCP, dan shell tetap diblokir, dan pesan blokirnya
menyebut aturan ini. Marker tanpa `set_at` (bukan tulisan `intent-gate-set`) tak membuka lane ini.

`dist/config/claude/CLAUDE.md` sengaja memuat **kedua** stanza mode — `COMMAND-ONLY` dan
`AUTO-INTENT`, masing-masing dibungkus marker `<!-- <MODE>:START -->` / `<!-- <MODE>:END -->`.
File itu sumber sebelum dipotong, bukan hasil instalasi. Saat `--apply`, installer
(`_apply_intent_mode`) membuang stanza mode yang tidak dipilih beserta marker kedua mode,
sehingga `~/.claude/CLAUDE.md` yang terpasang memuat tepat satu stanza. Dua stanza hidup di
satu file, bukan dua varian file, supaya perbedaan keduanya terlihat saat review.

`--check` menentukan scope dari cwd: dijalankan di dalam project yang punya `.workflow/`, ia
ikut memeriksa boundary project (`<project_root>/opencode.json`). Di luar workspace, scope
itu dilaporkan `SKIPPED` — bukan didiamkan lalu dilaporkan READY. Boundary yang diperiksa
hanya milik provider yang dijalankan project (`provider` di `.workflow/second_agent.json`),
sama dengan yang dipasang `init`: project codex tak diminta `opencode.json` dan dilaporkan
`project scope: SKIPPED — the project runs codex ...`. Project yang tak menyebut provider
jatuh ke default (opencode), jadi boundary-nya tetap diperiksa.

### Extractor (sisi maintainer)

`dist/` dihasilkan dari config live maintainer:

```bash
python tools/maintain/extract_config.py --dry-run
python tools/maintain/extract_config.py
```

Postur keamanannya, berurutan menurut kepentingan:

1. **Allowlist, bukan blocklist.** Home agent berisi `.credentials.json`, `history.jsonl` ratusan KB, transkrip sesi, dan path project. Blocklist mengirimkan apa pun yang lupa disebut; allowlist hanya mengirim yang disebut.
2. **Gagal-tutup pada rahasia.** Setiap byte dipindai pola kredensial. Satu temuan **membatalkan seluruh run dan menulis nol** — peringatan akan dibaca, diabaikan, lalu ter-commit. Rahasia di git history praktis tak bisa dicabut.
3. **Read-only.** Extractor tak pernah menulis di luar `dist/`.

Path absolut diredaksi jadi `{{HOME}}` / `{{PROJECT_ROOT}}`; installer yang mengembalikannya.

### E2E

```bash
python tools/e2e/e2e.py          # lokal + installer: tanpa delegated run/kuota
python tools/e2e/e2e.py --full   # plus command delegated: menit + kuota nyata
```

`--full` opt-in. Command delegated memakai anggaran rate-limit yang sama dengan yang dibutuhkan workflow itu sendiri; suite yang membakarnya diam-diam lebih buruk daripada tak ada suite.

Installer diuji terhadap **HOME sementara**, bukan mesin yang menghasilkan `dist/` — meng-install ke mesin asal hanya membuktikan idempotensi, tak pernah menjalankan jalur *create* yang dihadapi user baru.

`SKIPPED` bukan `PASS`, dan dilaporkan terpisah.

---

## Bootstrap

`main.py` tinggal di repo ini, **bukan** di project target. Untuk init pertama, runtime perlu tahu letaknya. Sesudah init, `.workflow/config.json` menyimpan path absolutnya, jadi `AGENT_PATH` tak wajib lagi.

**Windows (persisten, jalankan sekali):**

```powershell
[Environment]::SetEnvironmentVariable("AGENT_PATH", "C:/path/to/agent-workflow/main.py", "User")
```

Tutup dan buka kembali terminal supaya variabel aktif.

**Windows (sesi berjalan saja):**

```powershell
$env:AGENT_PATH = "C:/path/to/agent-workflow/main.py"
```

**macOS / Linux (persisten):**

```bash
echo 'export AGENT_PATH="$HOME/path/to/agent-workflow/main.py"' >> ~/.bashrc   # atau ~/.zshrc
source ~/.bashrc
```

**macOS / Linux (sesi berjalan saja):**

```bash
export AGENT_PATH="$HOME/path/to/agent-workflow/main.py"
```

Verifikasi:

```bash
test -f "$AGENT_PATH" && echo OK
python3 "$AGENT_PATH" --help
```

```powershell
Test-Path $env:AGENT_PATH
python $env:AGENT_PATH --help
```

---

## Init di project target

```bash
python3 "$AGENT_PATH" --command init --work-dir /path/to/target-app --pretty
```

```powershell
python $env:AGENT_PATH --command init --work-dir "C:/path/to/target-app" --pretty
```

`init` bersifat idempoten: membuat scaffolding yang belum ada dan meregenerasi enam runner script. Workspace yang dibuat build lama (versi berbeda atau layout lama) langsung di-upgrade oleh init; bila ada job hidup, upgrade dilewati dan perintahnya disebut.

- `.workflow/config.json` — path absolut `main.py`/`check.py`, sehingga runner dapat menemukan tool tanpa mengandalkan `AGENT_PATH`
- `.workflow/second_agent.json` — salinan project-local, boleh kamu ubah
- skrip runner untuk platform yang sedang berjalan: `run` `inspect` `check` — `.ps1` di Windows, `.sh` di POSIX
- `.workflow/data/sessions/` kosong — state per-sesi dibuat lazy saat panggilan terdelegasi pertama. Semua file internal (stream, store, cache, sesi, report) ada di `.workflow/data/`; root `.workflow/` hanya berisi file yang kamu edit, script, dan `current/`
- entri `.workflow/` ditambahkan ke `.gitignore` root project

Skrip `.sh` diberi bit executable saat dibuat. Kalau repo dipindah lewat media yang membuang mode bit:

```bash
chmod +x .workflow/*.sh
```

**Batasan portabilitas.** Skrip memanggang **path absolut dari mesin tempat `init` dijalankan** — path ke `main.py`, `--work-dir`, dan interpreter Python yang terdeteksi. Tak ada satu pun yang portabel lintas mesin.

Karena itu hanya flavour OS yang sedang berjalan yang ditulis, dan `init`/`upgrade` **menghapus** skrip flavour lain yang tertinggal dari build lama. Sebuah `run.sh` yang dihasilkan di Windows berisi path bergaya `C:\...`, tidak akan jalan di Linux/macOS, dan tidak dipelihara generator mana pun di mesin itu — menyimpannya hanya membuatnya tampak dapat dipakai. Setiap anggota tim menjalankan `init` (atau `upgrade`) sendiri di environment masing-masing; itu juga berlaku saat berpindah antara WSL dan Windows asli.

`doctor` membandingkan isi skrip di disk dengan hasil generator dan melaporkannya di `run_script_drift` — `missing`, `content_differs`, atau `foreign_os_leftover`. Semuanya dihitung sebagai issue: skrip inilah satu-satunya pintu masuk, dan yang sudah melenceng akan merutekan command yang sudah tidak diterima CLI.

## Upgrade workspace

Jalur yang direkomendasikan setelah menarik versi agent-workflow baru:

```bash
cd /path/to/target-app && python /path/to/agent-workflow/install.py --apply
```

Command itu memperbarui config global dan meng-upgrade workspace di cwd. Project yang belum
punya `.workflow/` di-scaffold lewat `init`, bukan installer. Untuk workspace saja:

```bash
python3 "$AGENT_PATH" --command upgrade --work-dir /path/to/target-app --pretty
```

```powershell
python $env:AGENT_PATH --command upgrade --work-dir "C:/path/to/target-app" --pretty
```

`upgrade`:

- menolak berjalan ketika ada delegated job aktif;
- meregenerasi runner scripts dan me-repoint path tool;
- memigrasi kunci v3.4.2 (`opencode_*` → `provider_*`, `.workflow/opencode.json` → `.workflow/second_agent.json`) sekali, memindahkan nilainya;
- **migrasi layout** (sekali): isi internal `.workflow/` dipindah ke `.workflow/data/` setelah backup ke `data/backups/<stamp>/` (3 terakhir disimpan), path evidence yang diarsipkan ditulis ulang, sisa lama (`state.json`, `runtime/`, `logs/`, lock) dihapus, `secrets.json` format objek dikonversi ke list. Isi dipindah dulu ke staging `.workflow/data.migrating/` lalu di-rename ke `data/` dalam satu langkah. Staging sisa percobaan yang terputus dikembalikan dulu; bila ia berisi nama yang juga ada di root `.workflow`, upgrade berhenti dengan `MigrationConflict` dan **tak memindah apa pun** — simpan satu salinan tiap entry (bandingkan dulu), hapus yang lain, lalu jalankan ulang `upgrade`. Setelah cek itu, kegagalan punya tiga hasil berbeda:
  - **gagal sebelum rename, rollback bersih** → semua dikembalikan ke `.workflow/`, backup disimpan sebagai `.workflow/migration-backup-*`, pesan `workspace migration failed and was rolled back`;
  - **gagal sebelum rename, rollback INCOMPLETE** (`MigrationRollbackIncomplete`) → tiap langkah balik tetap dicoba dan tiap kegagalan dicatat; entry yang tak bisa kembali tetap di `.workflow/data.migrating/` (staging dipertahankan, bukan dihapus), pesan `workspace migration failed and the rollback is INCOMPLETE: ...`. Pindahkan entry itu ke `.workflow/` manual, atau jalankan ulang `upgrade` — ia memulihkan staging sisa lebih dulu. Salinan pra-migrasi mungkin ada di `.workflow/migration-backup-*`;
  - **gagal setelah rename** (`MigrationIncomplete`) → tak ada yang dikembalikan; workspace sudah di `data/` dan jalan. Pesan `workspace migration incomplete: ...` menyebut langkah yang belum selesai — perbaiki penyebabnya (biasanya file dipegang editor/antivirus) lalu jalankan ulang `upgrade`, ia melanjutkan dari langkah itu;
- `config.json` dipangkas jadi **override saja**: nilai yang sama dengan default dan key pensiun/tak dikenal dibuang dan dilaporkan; `second_agent.json` tetap di-backfill additive;
- mempertahankan nilai user dan seluruh `sessions/`;
- tidak mengirim prompt, tidak memanggil second_agent, dan tidak menjalankan verify.

Command biasa tidak menjalankan **full workspace upgrade**. Delegated call dapat mem-backfill `.workflow/config.json` saat memuat state, tetapi tidak meregenerasi scripts atau mem-backfill `.workflow/second_agent.json`. Karena backfill itu juga memperbarui marker versi, warning runner dan status `doctor: NEEDS_UPGRADE` dapat hilang sesudah delegated call pertama walaupun dua bagian tadi masih stale. Karena itu jalankan `upgrade` secara eksplisit sebelum memakai workspace versi lama.

---

## Pemakaian harian

main_agent memanggil **satu** skrip runner — tidak merakit command Python sendiri.

```powershell
& "C:/path/to/target-app/.workflow/run.ps1" explore "cari entry point auth" "<MAIN_SESSION_ID>"
```

```bash
/path/to/target-app/.workflow/run.sh explore "cari entry point auth" "<MAIN_SESSION_ID>"
```

Argumen ketiga adalah session id. **Wajib** diteruskan: tanpa itu runner memakai fallback `"default"` yang di-resolve melalui cache global tool, sehingga beberapa main_agent dapat berbagi ID efektif, saling menimpa state, dan saling memblokir job. Skrip mencetak peringatan ke stderr bila ini terjadi.

Skrip bersifat blocking dan mengembalikan JSON yang sama seperti CLI.

### Auto-intent

Prefix `/.` tetap didukung, tetapi tidak wajib. Main_agent memetakan bahasa natural ke command dan menampilkan satu baris transparansi sebelum menjalankannya:

```text
[INTENT] analyze — user meminta audit logic
```

Contoh mapping: pertanyaan lokasi/alur → `explore`, sebab/penilaian → `analyze`, fitur baru → `plan`, implementasi → `execute -y`, hasil yang sudah dibuat → `verify`, uji alur di browser → `verify-browser`, dan blast radius diff → `sweep`.

Batasnya:

- command eksplisit seperti `/.plan` selalu menang atas tebakan;
- percakapan biasa tidak dipaksa menjadi command;
- delegated intent yang ambigu dapat memicu satu pertanyaan klarifikasi;
- aksi destruktif atau irreversible tidak boleh auto-fire tanpa konfirmasi.

Auto-intent adalah kontrak prompt main_agent, bukan fitur parser Python dan tidak memiliki key `auto_intent`.

### Opsi implementasi pada `/.plan`

Setelah `[PLAN]`, main_agent menambahkan `[OPTIONS]` berisi maksimal tiga pendekatan yang tetap berada dalam scope. Setiap opsi memuat kelebihan, kekurangan, effort, risiko, dan atribusi evidence; tepat satu opsi direkomendasikan. Bila hanya satu pendekatan yang feasible, alternatif tidak boleh dikarang.

Second_agent hanya memasok evidence dan reasoning. Runtime tidak membuat atau memvalidasi blok `[OPTIONS]`.

### Kontrak lapisan prompt

Kontrak di bawah hidup di `dist/config/claude/CLAUDE.md` dan skill-nya
(`dist/config/claude/skills/*.md`), bukan di Python. Runtime tak pernah melihat pesan user
mentah maupun jawaban akhir main_agent, jadi yang menegakkan kontrak ini adalah main_agent
sendiri. Satu-satunya pengecualian sebagian adalah pre-flight gate, yang juga ditegakkan hook
agent host. Alasan per kontrak kenapa Python tak bisa menegakkannya: `docs/runtime-contracts.md`,
bagian "Contracts the runtime cannot enforce".

| Kontrak | Isi | Penegak | Sumber |
|---|---|---|---|
| Mode intent | Satu dari dua, dipilih installer. `AUTO-INTENT`: bahasa natural dipetakan ke command dan diumumkan dengan satu baris `[INTENT]` tanpa menunggu jawaban. `COMMAND-ONLY`: command hanya jalan lewat prefix `/.`; tanpa prefix = percakapan biasa, boleh menyarankan satu baris lalu berhenti | main_agent | `CLAUDE.md`, stanza `AUTO-INTENT` / `COMMAND-ONLY`; lihat [Mode intent](#mode-intent) |
| Pre-flight gate | Begitu intent resolve ke command terdelegasi (`explore`, `plan`, `analyze`, `verify`, `verify-browser`), langkah berikutnya wajib `.workflow/run`. Tool gather (Read/Grep/Glob/Bash/MCP) dilarang sebelum itu, kecuali `[LOCAL_MODE]`, proxy gagal, atau slice presisi kecil | main_agent + hook `intent-gate-set` / `intent-gate-check` (hanya mode auto-intent; fail-open) | `CLAUDE.md`, "Pre-flight gate"; `dist/config/claude/hooks/intent-map.json` |
| Output contract | Explore dan `sweep`/`doctor` = RELAY hasil runtime apa adanya. Plan/analyze = SYNTHESIS oleh main_agent: confidence tiga bagian (`problem_understanding`, `root_cause`, `solution_path`), atribusi per klaim, `open_questions` dipisah dari `uncertainty`. Field wajib tetap tampil walau kosong, dengan alasannya | main_agent | `CLAUDE.md`, "Output Contract" dan "Plan/analysis output" |
| `[OPTIONS]` | Lihat [Opsi implementasi pada `/.plan`](#opsi-implementasi-pada-plan) | main_agent | `skills/plan.md` |
| Fitur baru lewat plan | Perintah yang menjelaskan perilaku baru ("buat agar …", "tambahkan …", daftar perbaikan) tanpa plan aktif dipetakan ke `plan`, bukan `execute`. Execute tanpa plan aktif berhenti dan menyarankan `/.plan`. Permintaan spec/penjelasan dijawab langsung, tanpa `/.verify` yang tak diminta (auto-verify sesudah `/.execute` bila `auto_verify_after_execute=true` dan sesudah `/.refactor` tetap berlaku) | main_agent | `CLAUDE.md`, NL map dan tie-break; `skills/execute.md` (DEC-046) |
| `/.execute -y` | Tanpa `-y` hanya menampilkan `[EXECUTION SCOPE]` lalu berhenti. Dengan `-y` mengedit hanya file dalam scope; kebutuhan di luar scope = berhenti dan minta instruksi. Persetujuan hanya `/.execute -y` dari user setelah plan `proceed`; kalimat execute tanpa `-y` menampilkan scope dan meminta `-y`; jawaban AskUserQuestion atas `open_questions` bukan persetujuan | main_agent | `skills/execute.md`, `skills/plan.md` STEP 3 |
| Verifikasi setelah execute | `commands.auto_verify_after_execute` dibaca ulang tiap `/.execute`. `false` (default): status `implemented`, `verification: not_run`, kata "done" dan sejenisnya dilarang. `true`: `/.verify` bagian dari `/.execute` | main_agent | `skills/execute.md`; `CLAUDE.md`, "Execution rules" |
| Proxy failure | `ok:false`, `error_type` evidence tidak valid, atau content bukan evidence → berhenti, cetak `[PROXY GAGAL] …`, tanya lanjut ke `/.local` (yes/no), lalu tunggu. Fallback lokal otomatis dilarang | main_agent | `CLAUDE.md`, "Proxy failure" |
| Aksi destruktif | Commit, hapus, atau tulis di luar project tidak boleh jalan dari intent tebakan; wajib konfirmasi. Menulis ke project lain (Edit/Write atau Bash/skrip) butuh persetujuan eksplisit untuk path itu; hook tidak menahannya (lihat [limitations](limitations.md)) | main_agent | `CLAUDE.md`, "Intent detection" dan "Global Forbidden" |

Isi persisnya tetap di file sumber; tabel ini peta, bukan salinan. Saat file sumber dan tabel
ini berbeda, perbaiki salah satunya dalam perubahan yang sama.

---

## Command

| Command | Jenis | Butuh prompt | Keterangan |
|---|---|---|---|
| `init` | lokal | — | scaffold `.workflow/`; regenerate runner script |
| `upgrade` | lokal | — | migrasi layout `.workflow/data/`, refresh workspace, config override-only, preserve sessions |
| `doctor` | lokal | — | cek kesiapan, tulis `reports/doctor.json` |
| `clean` | lokal | — | prune job, fakta usang/duplikat, guard runtime-lock milik sesi tanpa lock hidup (`sessions/<id>/runtime/lock.guard`; guard yang sedang dipegang proses lain tetap), sesi lama |
| `inspect` | lokal | — | daftar job untuk sesi berjalan |
| `provider` | lokal | — | baca/ubah provider second_agent dan reasoning effort; picker tanpa argumen, langsung dengan `<provider> <model> [effort] [routes]` |
| `report` | lokal | — | ringkasan stream kualitas workspace (`quality.jsonl`), termasuk `e2e` (run), `e2e.drafts` (draft), dan `graph_refresh` (biaya Stop hook graph per outcome), serta dari `usage.jsonl`: `evidence_reuse` (command explore/analyze/plan yang dilayani artifact tersimpan dibagi command yang boleh reuse, continuation dihitung sekali; `by_outcome` memecah per `reuse_outcome`: `hit`, `no_prior`, `stale`, `unreadable`, `error`, `not_offered`, `disabled`, dan `unrecorded` untuk baris sebelum contract v3), `provider_cache` (share cached input dari input terukur provider: `all`, `first_call`, `continuation`, `fresh_thread`, `resumed_thread`; baris yang adapternya tidak melaporkan `resumed` tidak masuk dua bucket thread), `provider_threads` (per provider: `resumed`, `fresh`, `unknown`, dan `thread_changed` = call resume yang dijawab di thread lain, tanda sesi kehilangan state; baris sebelum contract v3 tidak dihitung), `by_effort` (per `<command>/<effort>`: jumlah command, median durasi, median input/output token — tiap median hanya dari command yang nilainya terukur, output tak terukur tidak dihitung nol; `default` = tanpa flag effort; baris sebelum contract v3 tidak dihitung), dan `graph_leads` (`graph_status` per command, `refreshing_by_command`, median/max `graph_ms`); serta dari `tasks.jsonl`: `tasks` (lihat [Task telemetry](#task-telemetry)); dan `provenance` (versi registry metrik, versi tiap metrik `runtime.*`, commit tool, hash isi stream — definisi di [`docs/evaluation/metrics.md`](evaluation/metrics.md)) |
| `audit` | lokal | — | baca `audit.jsonl` sesi |
| `graph-meta` | lokal | — | status snapshot graphify: segar, usang, atau tak ada |
| `explore` | terdelegasi | ya | peta codebase, entry point, pemilik |
| `plan` | terdelegasi | ya | evidence + jejak dependency terbalik |
| `analyze` | terdelegasi | ya | analisis mendalam, nol perubahan kode |
| `verify` | terdelegasi¹ | ya | verifikasi; kedalaman diatur `verify_mode` |
| `verify-browser` | terdelegasi³ | ya | verifikasi lewat browser Playwright dari request per sesi; tahap `draft` lalu `run` |
| `sweep` | lokal | — | pindai staged, unstaged, dan untracked diff tanpa OpenCode |
| `promote-validate` | lokal | ya² | validasi dokumen knowledge; nol tulisan |
| `promote-verify` | lokal | ya² | freshness tiap klaim + rekonsiliasi lawan dokumen existing |
| `promote-write` | lokal | ya² | satu-satunya jalur tulis ke direktori knowledge |
| `submit` | job | ya | jalankan asinkron, kembalikan `job_id` |
| `await` | job | ya | submit lalu tunggu selesai |
| `status` | job | — | butuh `--job-id` |
| `result` | job | — | butuh `--job-id` |
| `worker` | internal | — | dipakai proses worker, jangan dipanggil manual |

¹ `verify` melewati OpenCode sepenuhnya ketika `verify_mode` bernilai `syntax`.

² Ketiga tahap `promote-*` menerima **path ke file JSON** lewat `--prompt`, bukan dokumennya sendiri: satu dokumen knowledge melewati batas argv 8191 karakter Windows dengan mudah. Tahapnya dipisah karena persetujuan user terjadi di antaranya — CLI tak bisa bertanya apa pun, jadi verifikasi berhenti pada vonis, main_agent yang menjalankan review, dan penulisan adalah panggilan terpisah yang hanya bisa terjadi sesudahnya. `promote-write` menolak di luar `policies.production_branch`.

³ `verify-browser` adalah command **terdelegasi**: ia terdaftar di registry DELEGATED dan ikut pre-flight gate seperti `explore`/`plan`/`analyze`/`verify`. Ia membaca **request per sesi** (`.workflow/data/sessions/<session>/e2e/request.json`) di atas default project dari section `e2e` di `config.json`. Pembagian kerjanya tiga tahap: second_agent menyusun draft spec (tahap 1) dan me-review bukti browser (tahap 3); di antaranya, **player runtime** — proses anak yang dijalankan runtime (`python -m core.evidence.e2e.player`) — menggerakkan Playwright (tahap 2). main_agent tidak menjalankan browser dan second_agent juga tidak (sandbox read-only). Wawancara terjadi di main_agent hanya sesudah draft melaporkan target belum dikonfigurasi. Tahap `draft` tidak membawa vonis; tahap `run` diselesaikan sebagai `verify` sehingga vonis dan exit code-nya sama dengan verifikasi lain. Section `[E2E SPEC]` yang gagal validasi mendapat satu perbaikan di thread provider yang sama (`meta.e2e.repair`); satu-satunya atribut test adalah `data-e2e` lewat key selector `e2e` — `testid` ditolak (**breaking**); rem `repeat_failure` lepas sendiri bila project (di luar `.workflow/`, via Git) atau runtime (`TOOL_VERSION` + digest kode `core/evidence/e2e/`) berubah sejak kegagalan terakhir, streak bucket `app` hanya bertambah untuk kegagalan dengan signature sama (halaman, field, kondisi), dan tercatat sebagai knowledge `repeat_failure` untuk draft berikutnya. Nilai credential yang terketik literal di task, draft, atau `request.json` dicari lewat lookup `secrets.json` dan dikembalikan ke `${NAME}` (`request.json` ditulis ulang; nama key dilaporkan di `meta.e2e.secret_literals`, nilainya tidak pernah), dan `fill` ke field password tanpa placeholder → `spec_invalid`; kegagalan yang disebabkan scenario sendiri — selector tebakan yang meleset, selector apa pun yang cocok >1 elemen, write/navigasi yang ditolak policy run — ber-origin `scenario` dan berakhir `incomplete: scenario_error`, tidak di-retry dan tidak dihitung rem; tiap run non-pass dan tiap penolakan rem membawa `meta.e2e.diagnosis` (`cause`, `next_step`, `failed`, `fix_hint`, `reusable`), penyebab reusable disimpan sebagai knowledge `failure_hint`; tiap draft menulis baris `kind: e2e_draft` (kategori error validator, hasil repair) yang dirangkum `--command report` di `e2e.drafts`. Network: setiap run selalu mencatat metadata semua request halaman (urutan, URL tanpa query, tipe, status, timing, ukuran; tanpa body/header/cookie), 500 baris terbaru plus ringkasan atas semua request, di `report.json` → `network` dan `meta.e2e.page_requests`, termasuk `since_last_run` (perubahan dibanding run sebelumnya pada origin yang sama). Urutan selector: kandidat mengikuti role+name > label > e2e > text > css, kecuali dua jenis yang boleh memimpin — `e2e` ber-provenance `source` dengan ref `path:line`, dan selector `proven` yang tercatat di browser knowledge untuk origin, route (path `goto` terakhir, segmen identifier jadi `:id`: `/users/123` dan `/users/456` satu route), dan `within` step itu (dicek ke store; klaim `proven` tanpa dasar atau di route/scope lain → `spec_invalid`); selector `proven` yang meleset turun ke urutan biasa, dua run berturut yang meleset memensiunkannya (satu run melemahkan entry paling banyak sekali); step yang gagal sesudah selector-nya cocok bukan miss; pass hanya dicatat dari run yang lulus di percobaan pertama. URL network dibersihkan dari parameter path `;…` dan segmen berbentuk token (16+ huruf-angka tanpa pemisah, 16+ campuran huruf besar-kecil, 24+ dengan angka) jadi `:id`. Kontrak lengkap: `docs/runtime-contracts.md`, bagian `/.verify-browser`.

Tidak ada command Python `execute`. `/.execute -y` tetap tersedia sebagai command user-facing di main_agent; menulis kode sengaja tidak didelegasikan ke runtime atau second_agent.

---

## CLI langsung

Berguna untuk debugging; alur normal cukup lewat skrip runner. Untuk perilaku blocking yang sama dengan runner, gunakan `await`:

```bash
python3 main.py -c await --job-command explore -p "cari entry point auth" -s "main_app_20260723_090000" -w /path/to/target-app --pretty
```

```powershell
python main.py -c await --job-command explore -p "cari entry point auth" -s "main_app_20260723_090000" -w "C:/path/to/target-app" --pretty
```

Memanggil `-c explore`, `plan`, `analyze`, `verify`, atau `verify-browser` secara langsung hanya melakukan
`submit` dan segera mengembalikan payload job. Ambil hasilnya lewat `result`/`check`, atau
gunakan `await` seperti contoh di atas. `sweep` dan command lokal lain selesai langsung.

| Argumen | Alias | Arti |
|---|---|---|
| `--command` | `-c` | lihat tabel command |
| `--prompt` | `-p` | task |
| `--prompt-file` | | baca task dari file (alternatif `--prompt`) |
| `--session` | `-s` | id sesi main_agent |
| `--fresh-session` | | paksa sesi baru, abaikan cache ID sesi dan evidence reuse |
| `--work-dir` | `-w` | root project target (default: cwd) |
| `--model` | `-m` | override model, format `provider/model_key` |
| `--job-id` | | untuk `status`/`result`/`worker` |
| `--job-command` | | command yang dijalankan `submit`/`await` |
| `--poll-interval` | | detik antar polling saat `await` |
| `--poll-timeout` | | batas tunggu `await`; `0` = tanpa batas |
| `--pretty` | | JSON ber-indent |

`--prompt-file` menghindari masalah escaping shell pada task panjang atau multi-baris — persoalan yang bentuknya berbeda di PowerShell dan di POSIX shell.

---

## Konfigurasi

### `.workflow/config.json`

Dibuat saat `init`, dan berisi **override saja**: section `commands`, `policies`, `e2e` kosong/tidak ada berarti semua key memakai default bawaan build yang sedang jalan. Tulis hanya key yang ingin kamu ubah; key yang tidak ada tidak pernah diisi otomatis, jadi default yang berubah di rilis berikutnya ikut sampai ke project ini.

Sepuluh key berikut benar-benar mengubah perilaku runtime Python:

| Key | Default | Arti |
|---|---|---|
| `commands.verify_mode` | `"delegated"` | `delegated` = verifikasi penuh second_agent. `syntax` = check parse lokal saja. Nilai tak dikenal jatuh ke `delegated` |
| `commands.verify_test_commands` | `[]` | prefix command test yang boleh dijalankan runtime untuk `/.verify` (mis. `["python tests/run.py", "php artisan test"]`). Kosong = tak ada test yang jalan; test yang diminta dilaporkan tak dijalankan |
| `commands.verify_test_timeout_seconds` | `900` | batas waktu per command test |
| `policies.fact_relevant_limit` | `3` | maksimum fakta yang diinjeksi ke tiap prompt |
| `policies.fact_recurrence_threshold` | `5` | jumlah sesi **lain** yang harus melaporkan klaim sebelum dipromosikan |
| `policies.graph_leads_enabled` | `true` | injeksi shortlist dari `graphify-out/graph.json` |
| `policies.subagent_fanout_enabled` | `true` | minta fan-out untuk role exploration/reasoning |
| `policies.production_branch` | `"main"` | satu-satunya branch tempat `promote-write` mau menulis. Promoted knowledge menggambarkan yang hidup; hipotesis dari feature branch tak boleh masuk repo bersama membawa otoritas itu |
| `policies.knowledge_dir` | `"docs/project-knowledge"` | lokasi dokumen knowledge, relatif project root. Satu-satunya artefak workflow yang **ter-Git** — seluruh nilainya ada pada bisa dibagi |
| `policies.knowledge_relevant_limit` | `3` | maksimum dokumen knowledge yang ikut satu prompt terdelegasi. `0` mematikan sidecar knowledge |

`commands.auto_verify_after_execute` dibaca dan dikembalikan di `meta.policy`, tetapi hanya main_agent yang bisa menjalankannya karena Python tidak memiliki jalur `execute`.

Key instruksi main_agent lainnya: `commands.allow_analyze_to_plan`, `commands.allow_explore_to_plan`, `commands.auto_sweep_after_execute`, `policies.workflow_prefix`, `policies.chat_mode_for_plain_text`, `policies.fallback_requires_confirmation`, dan `policies.max_active_job_per_session`.

Tidak ada key `auto_verify_method`. Dua key yang valid dan ortogonal adalah:

- `commands.auto_verify_after_execute` — **kapan** verify dipanggil; default `false`. Saat false, `/.execute` harus melaporkan `verification: not_run`, berstatus `implemented`, lalu menawarkan `/.verify`.
- `commands.verify_mode` — **seberapa dalam** verify berjalan ketika dipanggil.

Key pensiun `commands.autoverify` dimigrasikan saat upgrade. Salah ketik key lain tidak menimbulkan error dan akan jatuh ke default.

### `.workflow/second_agent.json`

File adapter project-local ini boleh diubah per project. Namanya mengikuti PERAN, bukan vendor — sejak v3.4.3 provider second_agent dipilih lewat config, jadi file ini tidak lagi mengasumsikan OpenCode. Workspace v3.4.2 yang masih memakai `.workflow/opencode.json` dimigrasi sekali saat `upgrade` (nilainya dipindah, kunci lama dihapus). Jangan dikelirukan dengan `<project_root>/opencode.json` dan `~/.config/opencode/opencode.json`, yang memang milik OpenCode sendiri dan tetap bernama begitu.

Saat `init`, source-nya adalah `config/second_agent.seed.json` bila ada, atau `config/second_agent.example.json` pada clone bersih. File ini WAJIB ada per project: tanpanya (atau bila tak ter-parse) command delegated ditolak, tanpa fallback ke config level mesin. Loader melengkapi key yang belum tersimpan dengan default source secara in-memory; `upgrade` menuliskannya ke file secara additive. Bentuk efektifnya:

```json
{
  "provider": "opencode",
  "provider_command": "opencode",
  "provider_agent": "plan",
  "default_model": null,
  "timeout_seconds": 1800,
  "bootstrap_timeout_seconds": 180,
  "stall_threshold_seconds": 360,
  "idle_stall_seconds": 240,
  "probe_timeout_seconds": 45,
  "probe_recheck_seconds": 120,
  "job_poll_interval_seconds": 2.0,
  "routes": {
    "explore": { "model": null },
    "plan":    { "model": null },
    "analyze": { "model": null },
    "verify":  { "model": null }
  }
}
```

`model: null` berarti pakai model default OpenCode.

#### Memilih provider — dan konsekuensi keamanannya

`provider` menerima `opencode`, `codex`, atau `agy`. Ketiganya bukan pilihan setara:

| | `opencode` | `codex` | `agy` |
| --- | --- | --- | --- |
| Boundary baca file rahasia | **ditegakkan** lewat `<project_root>/opencode.json` | **tidak ada** | **tidak ada** |
| Sandbox tulis | ya | ya (`--sandbox read-only` + `approval_policy="never"`) | **tidak** — tulis di working tree hanya *dideteksi* (`core/policy/agy_guard.py`), tidak dicegah |
| Config boundary project-root | file, di-refresh tiap `init`/`upgrade` | tak ada layer-nya | tak ada layer-nya |
| `boundary` (`config/providers.py`) | `reads: secrets_denied`, `writes: denied` | `reads: unbounded`, `writes: sandboxed` | `reads: unbounded`, `writes: unbounded` |

`codex` dan `agy` sama-sama dilaporkan `doctor` sebagai `not_enforceable` dengan `trusted: true`: keduanya bisa membaca tiap file project dan menerima environment proses penuh — boundary-nya kewajiban agent, bukan penegakan runtime. Memilih `agy` juga butuh acknowledgement eksplisit (`requires_opt_in`); `/.provider` menolak menulisnya tanpa itu.

Sisi tulis dilaporkan terpisah. `doctor` menulis `checks.second_agent_write_boundary` (`{provider, writes}`) dan, hanya untuk `writes: unbounded` (`agy`), satu `WARNING: second_agent provider '<p>' can WRITE ...` tambahan di `recommended_fixes` — tetap bukan issue. Setiap hasil delegated yang sampai ke tahap provider (sukses atau gagal) membawa `meta.provider_boundary` (`{reads, writes}`), dan untuk `agy` juga `meta.provider_write_warning`.

**Codex: `--sandbox read-only` saja tidak menghentikan tulis.** Flag itu membatasi perintah shell. Tool edit file codex (`apply_patch`) diatur approval policy, bukan sandbox: diuji pada codex-cli 0.154.0 di Windows, panggilan read-only ditolak saat menulis lewat shell lalu berhasil membuat file lewat tool itu. Adapter karena itu mengirim `-c approval_policy="never"` di tiap panggilan, termasuk `exec resume`; dengan itu codex menolak patch ("writing is blocked by read-only sandbox"). Probe opt-in `WORKFLOW_CODEX_PROBE=1 python tests/run.py --only codex-write-probe` membuktikan ulang kedua jalur tulis di panggilan baru dan yang di-resume terhadap CLI sungguhan (memakai kuota codex).

**Codex di Windows — sandbox OS-nya sendiri.** Sandbox Windows codex (backend `elevated`) bisa
menolak memulai sesi: ``windows sandbox failed: elevated Windows sandbox requires effective
`:root` read access``. Pada 3.8.0 ini teramati pada thread yang **di-resume** sementara thread
baru jalan (CASE-015; regresi upstream openai/codex#46312, dan #46114 bila setiap thread
gagal). Runtime mengklasifikasikannya dari stderr saja (teks serupa di output agent, mis. file yang ia baca, tidak memicunya) sebagai `error_type: sandbox_unavailable` — bukan `unknown` —
dengan `next_action` berurutan: mulai sesi utama baru (`/clear` memberi `MAIN_SESSION_ID` dan
thread codex baru) lalu jalankan ulang; bila thread baru juga gagal, perbarui codex dan jalankan
`codex` sekali secara interaktif untuk menyetujui setup sandbox; sementara itu ganti second
agent lewat `/.provider`. Runtime **tidak** menurunkan sandbox: profil deny-read yang dikirim
butuh backend `elevated`, dan memilih yang lebih lemah (`[windows] sandbox = "unelevated"` di
`~/.codex/config.toml`) adalah keputusan user. Versi codex (`codex --version`, dibaca sekali per
proses) tercatat di `meta.provider_version`, di baris usage (`provider_version`), dan di
`doctor` (`checks.provider_version`).

Codex mengirim daftar deny yang sama sebagai flag `-c permissions.workflow.filesystem` di tiap panggilan, tetapi flag itu tidak menghentikan apa pun. Diuji terhadap codex-cli 0.147.0 mode `exec`: men-deny `**` dan `**/*` untuk `:workspace_roots` lalu meminta sebuah file di root itu tetap mengembalikan isinya, exit 0. Codex membaca dengan menjalankan shell, dan `--sandbox read-only` (bersama `approval_policy="never"`, lihat di atas) membatasi **tulis**, bukan baca.

Artinya second_agent codex bisa membaca tiap file di project yang kamu tunjuk, `.env` termasuk. `init` melaporkan ini sebagai `status: not_enforceable` dengan `permissions_enforced: 0`, dan `dist/config/codex/AGENTS.md` menyatakan ke agent-nya bahwa menghindari file rahasia adalah kewajibannya sendiri — instruksi, bukan penegakan.

Pakai `codex` bila project-nya memang tak menyimpan rahasia, atau bila kamu menerima risikonya. Untuk project yang rahasianya harus tetap tak terbaca second_agent, pakai `opencode`.

Sejak v3.7.1 posisi ini eksplisit: `codex` dan `agy` diperlakukan sebagai **trusted provider**. Selain tanpa batas baca, adapter keduanya meneruskan seluruh environment proses ke CLI provider (credential di env ikut terlihat). Itu diterima sebagai risiko, bukan diperbaiki dengan allowlist — allowlist bisa memutus auth CLI provider, dan provider yang sudah bisa membaca `.env` tak dijaga apa pun oleh env yang dipangkas. Sebagai gantinya risikonya selalu terlihat: `/.doctor` menulis cek `second_agent_read_boundary` dan satu `WARNING` di `recommended_fixes` tiap kali provider aktif `codex`/`agy` (bukan issue — readiness tetap READY), dan `/.provider` menyebutnya saat memilih.

#### Katalog model `/.provider`

`/.provider` menawarkan model dari CLI provider yang terpasang saat itu
(`core/provider/model_catalog.py`), bukan dari shortlist `config/providers.py`. Tiap entri
`meta.providers[]` membawa `models` (maks 20, terbaru dulu), `newest`, dan `models_source`
(`live` | `cache` | `static`). Picker menaruh `newest` sebagai opsi pertama "(Recommended)";
model tetap dikonfirmasi user, tidak ditulis otomatis.

| Provider | Sumber | "Terbaru" |
| --- | --- | --- |
| `opencode` | `opencode models` + cache models.dev `~/.cache/opencode/models.json` (`family`, `release_date`, `reasoning_options`) | `release_date` terbaru dalam `family` model aktif; tanpa model aktif → terbaru dari semua |
| `codex` | `~/.codex/models_cache.json` (`$CODEX_HOME`), hanya `visibility: list` | `priority` terkecil |
| `agy` | `agy models` | versi tertinggi dalam keluarga model aktif; tanpa model aktif → urutan CLI |

Varian agy (`<model>-low|medium|high`) dilipat jadi satu model dengan `efforts` = suffix
yang ada (`effort_in_id: true`). Apply menyusun id varian (`claude-opus-5-5` + `high` →
`claude-opus-5-5-high`) dan menulis `effort: null`; model bervarian tanpa effort ditolak.
Route yang menyebut model dasar agy memakai varian dengan effort yang sama.

Validasi effort saat apply memakai katalog live (fallback ke shortlist), jadi model yang
lebih baru dari shortlist diterima tanpa warning. Router dan `validate_provider_config`
tetap memakai shortlist: keduanya jalan di tiap panggilan delegated, tempat spawn CLI terlalu
mahal; model di luar shortlist di sana diperlakukan sebagai "unknown" (effort dipertahankan).

CLI hilang, timeout (30 dtk), atau format tak dikenali → `models_source: static`, shortlist
dipakai. Hasil live di-cache 10 menit di `<cache user>/agent-workflow/model-catalog.json`
supaya katalog dan apply dalam satu sesi picker melihat daftar yang sama.
`AI_PROXY_MODEL_CATALOG=static` mematikan pembacaan live.

Mode langsung: `/.provider <provider> <model> [effort] [routes]` (mis. `/.provider codex
gpt-6-luna low`) melewati picker. Payload tanpa `|` dipecah per spasi — aman karena tak ada
provider, id model, atau effort yang memuat spasi — lalu divalidasi sama persis dengan
payload `provider|model|effort|routes`.

Kunci reliability (v3.8.3):

| Kunci | Default | Arti |
| --- | --- | --- |
| `timeout_seconds` | `1800` | Batas satu panggilan agent. `0` = tanpa batas (tidak lagi default). `null` = warisi default, **bukan** tanpa batas. |
| `bootstrap_timeout_seconds` | `180` | Anggaran terpisah untuk `init_session`. Bootstrap hanya membalas "READY", jadi tak boleh mewarisi anggaran task panjang. |
| `idle_stall_seconds` | `240` | Tidak ada byte baru di stdout/stderr selama ini → `alive-stalled`. |
| `stall_threshold_seconds` | `360` | Tidak ada heartbeat selama ini padahal PID hidup → `alive-stalled`. |
| `probe_timeout_seconds` | `45` | Batas probe PING itu sendiri. Tanpa ini watchdog ikut menggantung seperti pasiennya. |
| `probe_recheck_seconds` | `120` | Cadence probe ulang selama job tetap stalled; minimum jalur `await` adalah 10 detik. |
| `job_poll_interval_seconds` | `2.0` | Interval heartbeat/poll adapter. |

Per-route juga bisa: `"plan": { "model": "...", "timeout_seconds": 3600 }`.

**Role tidak dibaca dari file ini.** Pemetaan command → role ditentukan di kode (`config/routing.py`), jadi tak bisa ditumpuk lewat config.

**Tiga key pensiun.** `job_max_runtime_seconds`, `job_poll_timeout_seconds`, dan
`agent_workflow_path` dulu ditulis ke file ini dan tidak pernah dibaca siapa pun. Ketiganya
sudah berhenti ditulis; nilainya diambil dari `AI_PROXY_JOB_MAX_RUNTIME_SECONDS`, CLI
`--poll-timeout`, dan `.workflow/config.json → runtime.agent_workflow_path` (atau `AGENT_PATH`).
Upgrade ke layout `data/` (v3.6.0) membuang key pensiun dari `config.json`; sebelum itu `doctor` menandainya
`retired — never read` dan aman dihapus manual.

### Sub-agent fan-out (default ON)

`policies.subagent_fanout_enabled` berlaku untuk `explore`, `plan`, dan `analyze`. `verify` tetap memakai satu reviewer agar seluruh diff dilihat sebagai satu perubahan.

Upgrade mempertahankan nilai existing. Workspace lama yang sudah menyimpan `false` tetap nonaktif sampai kamu mengubahnya sendiri.

Ketika aktif, prompt membawa anchor `[EVIDENCE_SIDECARS]`; cluster fan-out berada di
`.workflow/data/sessions/<id>/runtime/leads.json` agar tidak ikut membesarkan prompt:

- graph memiliki ≥2 community → satu slice per community;
- graph tidak ada atau hanya menghasilkan 0–1 community → tetap fan-out ke empat sudut investigasi: entry point, core flow, reverse dependencies, serta config/tests.

Second_agent wajib memakai custom agent `wf-slice` melalui tool `task` bila tersedia,
menjalankan slice secara paralel, lalu menggabungkan klaim dengan tag `[cN]`. Jika tool
spawn tidak tersedia, ia harus menyebutkan daftar tool yang benar-benar tersedia sebelum
membaca slice secara berurutan.

**Pemakaian dilaporkan apa adanya.** Runtime mengecek dua sinyal yang harus sepakat: baris `subagents:` yang dideklarasikan, dan tag `[cN]` pada klaim hasil merge.

| Kondisi | `meta` |
|---|---|
| deklarasi + tag cocok | `subagent_used: true`, `subagent_fanout_clusters: [...]` |
| deklarasi tanpa tag | `subagent_used: false` + `subagent_warning` |
| `subagents: none (...)` jujur | `subagent_used: false`, `covered_clusters` tetap dapat berisi tag yang dibaca |

Deklarasi tanpa klaim bertag adalah pengakuan kerja, bukan bukti kerja — dan tidak dihitung sukses.

---

## Graphify

Dengan `policies.graph_leads_enabled: true`, runtime membaca `graphify-out/graph.json` **langsung** tanpa memanggil CLI atau MCP Graphify. Ia meranking maksimal 12 candidate file berdasarkan keyword dan weighted dependency degree, membawa maksimal empat community, lalu menyuntikkannya sebagai **starting points, bukan evidence**.

- `explore`/`plan`/`analyze` menerima leads lengkap dan community untuk fan-out;
- `verify` menerima shortlist ringkas maksimal enam file tanpa community;
- graph yang lebih tua daripada source `.py` tetap dipakai, tetapi prompt membawa warning stale;
- graph tidak ada, rusak, atau sedang di-refresh (`graphify-out/.refresh.lock` dipegang worker yang masih berdetak) → delegated flow tetap berjalan tanpa leads dan tidak menunggu lock. Baris `usage.jsonl` mencatat `graph_status` (`used`, `empty`, `absent`, `refreshing`, `disabled`) dan `graph_ms`. `verify-browser` tidak memakai leads, jadi tidak melakukan lookup; ia hanya mencatat sekali per command apakah refresh sedang berjalan: `refreshing`, `available` (graph ada, tidak dibaca), atau `absent`.

Runtime Python tidak pernah menjalankan `graphify init`, `build`, `watch`, atau `update`. Satu-satunya pemicu `graphify update` adalah Stop hook `graph-refresh` di bundle Claude (`.ps1` dan `.sh`); `CLAUDE.md` dan skill `refactor` tidak lagi meminta main agent menjalankannya. Hook jalan setelah `[EXECUTION RESULT]` atau `[REFACTOR RESULT]`, hanya bila graph sudah ada, lebih tua dari source, dan binary tersedia:

- scan mtime melewati direktori skip (`vendor`, `node_modules`, `.git`, `.workflow`, `graphify-out`, `target`, ...) sebelum masuk, jadi biayanya mengikuti jumlah source, bukan jumlah dependency; nama direktori dan ekstensi dicocokkan tanpa peduli huruf besar-kecil di kedua flavour;
- graph basi → hook membuat `graphify-out/.refresh.lock` (`pid`, `token`, `started`, `beat`), menjalankan worker terpisah, lalu kembali. Turn tidak menunggu graphify. Worker menjalankan `graphify update` **tanpa batas waktu** dan tak pernah membunuhnya (DEC-042); selama graphify jalan, worker menulis ulang `beat` (dan `graphify_pid`) tiap 15 detik, lalu mencatat satu baris dan menghapus lock miliknya. `GRAPH_REFRESH_LIMIT_S` tak lagi dibaca. Lock diserahkan hook ke worker hanya selama masih membawa token hook itu, dan bila worker sudah selesai dan menghapusnya, lock tidak dibuat ulang. Lock basi hanya dihapus bila isinya masih sama saat dibaca ulang tepat sebelum dihapus; lock yang berubah dianggap `skipped_running`, sehingga dua sesi tak menjalankan dua refresh bersamaan;
- graphify menulis ulang `graph.json` di tempat, jadi selama lock dipegang runtime memperlakukan graph sebagai tidak tersedia, dan main agent diminta menganggapnya basi. Lock dipegang selama `beat`-nya berumur < 120 detik, berapa pun umur lock itu; refresh sepanjang apa pun tak pernah terbaca basi. Lock tanpa `beat` (ditulis sebelum DEC-042) memakai aturan lama: pid hidup dan berumur < 15 menit. Lock yang ada tetapi tak terbaca dianggap dipegang selama mtime-nya < 120 detik. Setiap penulisan lock ditulis ke file sementara lalu di-rename;
- graphify yang hang menahan graph selama worker-nya hidup. `--command doctor` melaporkan lock itu (`checks.graph_refresh_lock`: `state`, `age`, `beat_age`, `pid`, `graphify_pid`) dan memberi saran bila refresh sudah berjalan ≥ 1 jam atau lock-nya basi; ini saran, bukan issue, karena delegated flow tetap jalan tanpa leads. `--command clean` hanya menghapus lock yang basi (tak ada `beat` dalam 120 detik) dan tak pernah membunuh proses: hentikan proses graphify-nya sendiri, tunggu lock basi, lalu jalankan `clean`. Bila worker mati sementara graphify masih hidup, lock basi dalam 120 detik walau graphify mungkin masih menulis;
- refresh dihitung `refreshed` bila `graph.json` ditulis ulang dan bisa di-parse sebagai JSON, apa pun exit code-nya (graphify exit 1 pada graph besar karena visualisasi HTML); tulisan ulang yang tak bisa di-parse tercatat `corrupt`;
- tiap run yang lolos gate pertama pada project yang sudah punya `graph.json` menulis baris `kind: graph_refresh` ke `.workflow/data/quality.jsonl` (bila direktori itu ada) dengan `outcome` (`skipped_fresh`, `skipped_running`, `no_graphify`, `refreshed`, `corrupt`, `error`; `timeout` hanya muncul di baris dari sebelum DEC-042 dan tetap dihitung), `hook_ms` (yang dibayar turn), `scan_ms`, `files_visited`, `dirs_skipped`, dan untuk refresh `graphify_ms`, `graphify_exit`, `graph_rewritten`. `--command report` merangkumnya di `graph_refresh`.

Hook fail-open, tidak pernah menolak response, tidak pernah membuat graph baru atau menjalankan `init`/`build`/`watch`. Outer timeout Stop hook tetap 60 detik.

Cache verdict graph memakai fingerprint path, mtime nanosecond, dan ukuran seluruh source
`.py`. Perubahan isi, penambahan, atau penghapusan source menginvalidasi cache walaupun
`graph.json` tidak berubah.

---

## Task telemetry

`--command report` → `tasks` membaca `.workflow/data/tasks.jsonl` (DEC-015, direvisi DEC-020). Satu task = rangkaian event dalam satu MAIN_SESSION_ID, dari event pertamanya sampai verify yang verdict **turunan** runtime-nya `pass` setelah task itu mengedit file. Commit adalah langkah berikut yang dianjurkan, bukan penutup. Event ditulis dua pihak, tanpa instruksi tambahan di prompt:

- runtime: satu event per command delegated (`explore`, `analyze`, `plan`, `verify`, `verify-browser`). Event verify membawa `verdict` (yang **dideklarasikan** reply; `DONE` dengan blocking finding dihitung `NEEDS FIX`) dan `derived` (verdict turunan runtime: `pass`, `incomplete`, atau `fail`). Hanya `derived: pass` yang menutup task; `DONE` yang oleh runtime dianggap `incomplete`, termasuk karena `not_verified`, membiarkan task tetap `open`;
- hook `task-events` (`.ps1`/`.sh`; `UserPromptSubmit`, `PostToolUse` matcher `Skill|Edit|Write|MultiEdit|NotebookEdit`, `Stop`): skill lokal yang dipanggil dari prompt (`/.<name>`) atau lewat tool `Skill`, file di dalam project diedit (bukan `.workflow/`/`.git/`), dan commit sebagai HEAD yang **berpindah antar turn**. Event pertama sesi menyimpan snapshot HEAD di `.workflow/data/task-hook/<claude_session_id>.head`; tiap `Stop` (atau prompt berikutnya, untuk commit yang dibuat di terminal di antara turn) mencatat commit sejak snapshot — first-parent, urut terlama dulu, masing-masing dengan `paths` — lalu memajukan snapshot. Event v2 (`v: 2`). Sebelum 3.8.0 hook ini jalan di setiap `Read` dan `Bash` (dua kali untuk `Bash`); satu spawn PowerShell ~450 ms di Windows, sementara skill dan commit cukup dibaca sekali per turn. Konsekuensi: skill yang dimuat lewat `Read` tanpa prefix (auto-intent bahasa natural) tidak tercatat, dan commit tercatat di akhir turn yang membuatnya, sesudah verify di turn itu.

State: `completed` (verify `derived: pass` tanpa edit sesudahnya, di-commit atau belum), `open` (ada edit, belum `pass` — di-commit atau belum), `read_only` (tidak ada edit), `unknown` (event tanpa identitas sesi). Edit sesudah `pass` membuka task baru. Commit yang memuat file task yang baru `completed` masuk ke `sequence` task itu; commit di task `open` masuk ke `sequence`-nya tanpa mengubah state. Event sebelum DEC-020 tidak membawa `derived`, sehingga task-nya tetap `open`. Output: `tasks`, `by_state`, `skill_counts`, `unclaimed_commits` (commit berbeda yang tak satu pun salinannya diklaim task; commit baru ditandai terpakai saat diklaim, sehingga salinan yang dicatat sesi lain lebih dulu tidak menyembunyikannya dari sesi pembuatnya), dan `recent` (10 task terakhir dengan `sequence`). Stream ini terpisah dari `usage.jsonl`: skill lokal dan edit bukan call delegated dan tidak boleh masuk budget governance.

---

## Mode verify

Kontrak konfigurasi yang dimaksud memisahkan dua pengaturan:

- `auto_verify_after_execute` menentukan apakah main_agent otomatis memanggil `/.verify` setelah implementasi; default `false`;
- `verify_mode` menentukan kedalaman pemeriksaan bila verify benar-benar dipanggil.

Jadi `auto_verify_after_execute: false` tidak mematikan `/.verify`; ia hanya mencegah pemanggilan otomatis.

`verify_mode` mengatur **sedalam apa** `/.verify` bekerja:

- **`delegated`** (default) — second_agent memverifikasi dan mengembalikan kontrak berlabel. Tiap temuan wajib membawa tiga tag: `severity` (critical/high/medium/low), `origin` (introduced/regression/pre_existing/unknown), `scope_relation` (in_scope/out_of_scope). Blocking ditentukan kombinasi ketiganya, bukan severity saja — cacat pre-existing tidak menyandera verdict perubahan berjalan, dan `origin: unknown` gagal-tertutup. Section tanpa temuan wajib berisi `none` eksplisit; runtime juga menerima `none.`, `none (…)` (keterangan bebas tanpa tag), `n/a`, `not applicable`, `nothing found`/`nothing to report`, `no (blocking) findings`, `tidak ada`, `tidak ada temuan`, dan sentinel diikuti separator (`;`, `:`, `,`, `—`, `-`) dengan keterangan yang seluruhnya frasa netral (`clean`, `all clean`, `ok`, `nothing found`/`nothing blocking`/`nothing to report`, `no issues`, `bersih`, `semua bersih`, `tidak ada temuan`). Penjelasan bebas ditaruh dalam kurung. Selain itu — `none of …`, `none - caller X fails`, `none; all inspected paths are clean`, keterangan bertag, teks multi-baris — dibaca sebagai temuan; continuation lalu meminta entrinya ditulis `- none`. Pengecualian di reviewer E2E (`verify-browser`): item dengan baris lanjutan tetap kosong bila baris pertamanya `none` polos atau `none (…)`, seperti sebelumnya.
  **Test pada mode `delegated`.** Second agent memverifikasi dengan membaca dan menelusur; ia
  tidak menjalankan test runner. Test dipilih main_agent dari diff dan dependents, ditulis ke
  `.workflow/data/sessions/<MAIN_SESSION_ID>/verify/tests.json` —
  `{"commands": [...], "reason": "..."}`, maksimal 5 — lalu dijalankan **runtime** sesudah
  review kembali (`core/evidence/verify_tests.py`). Command hanya jalan bila diawali salah satu
  prefix di `commands.verify_test_commands`, tanpa shell (`&&`, pipe, redirect, `$(` ditolak),
  satu per satu, masing-masing di bawah `commands.verify_test_timeout_seconds`. Hasilnya
  ditulis ke blok `[VERIFICATION]` yang tak pernah dilihat second agent: tiap command jadi baris
  `checks_run` `runtime: ...`; yang gagal, timeout, atau tak bisa dijalankan jadi blocking
  finding (`origin: unknown`, gagal-tertutup); command yang ditolak, dan verify **tanpa**
  `tests.json`, jadi gap `not_verified` sehingga verdict-nya `incomplete`. `commands: []` dengan
  alasan menyatakan tak ada test yang relevan dan tidak menambah gap; baris
  `runtime: no test requested — <alasan>` tetap tertulis di `checks_run` tetapi **tidak dihitung**
  sebagai check, jadi verdict bergantung pada check review itu sendiri (tanpa check →
  `incomplete`, `checks_missing`). Project tanpa test suite memakai jalur yang sama: skill
  verify meminta `commands: []` dengan alasan itu, bukan melewatkan `tests.json`. Gap yang ditulis runtime (prefix `tests:`: tanpa request,
  request tak terpakai, command ditolak, crash) berjenis `runtime_gap` dan membuat exit 2;
  kelonggaran exit 0 untuk `incomplete` hanya berlaku bagi gap yang dideklarasikan agent
  (`verification_gap`). Command berisi karakter kontrol (C0 selain tab, DEL) ditolak sebelum
  dijalankan; error spawn jadi blocking finding; exception lain di langkah ini mengembalikan
  review apa adanya plus gap `tests: runtime tests crashed: <tipe>` (request tetap dipakai).
  Selama test runtime berjalan, heartbeat job ditulis sebelum tiap command dan tiap 20 s
  (`phase: runtime_tests`), sehingga suite panjang tidak terbaca `alive-stalled`. `reason` wajib satu baris
  (yang berisi newline membuat request tak terpakai); `command` dan `detail` dirapatkan jadi satu
  baris sebelum masuk blok, sehingga teks request tak bisa memalsukan section. File request
  dipakai sekali, diganti nama `tests.used.json` **sesudah** hasilnya digabung, sehingga run
  pemulihan dari job yang sama masih menemukannya. Run selesai saat proses command itu exit, bukan saat pipe-nya EOF: sesudah
  exit, pipe ditunggu paling lama 5 s lalu sisa proses yang masih memegangnya dibunuh (process
  group di POSIX, job object di Windows) dan exit code aslinya dipakai, sehingga runner yang
  meninggalkan server/watcher tidak lagi terbaca timeout. Timeout membunuh seluruh pohon proses
  command (process group + `taskkill /T` / `killpg`), lalu output dikumpulkan paling lama 5 s; timeout
  bernilai boolean diabaikan (default 900). Baris prompt "jangan jalankan test runner" hanya
  dikirim bila `verify_test_commands` terisi; selama kosong, `doctor` memberi peringatan
  (`checks.verify_test_commands: empty`) karena verify tak menjalankan test dan tak bisa `pass`.
  Saat intent verify, gate hook mengizinkan main_agent membaca `git diff --name-only`/`--stat`,
  `.workflow/config.json`, dan menulis `verify/tests.json` sebelum `.workflow/run`. Alasannya: saat second agent ikut menjalankan test, codex
  menjalankan seluruh suite (verify median sampai 366 s, CASE-010) sementara opencode tak bisa
  menjalankan satu pun dan verify-nya jadi `incomplete` (CASE-009).
- **`syntax`** — dijawab lokal, tanpa memanggil OpenCode sama sekali. Memeriksa staged, unstaged, dan untracked files, termasuk repository tanpa commit: `.py` via `compile()` in-process, `.json` via `json.loads`, `.js`/`.mjs`/`.cjs` via `node --check`, `.php` via `php -l`. File Python juga memerlukan name check `pyflakes`; bila tool tidak tersedia, hasilnya `incomplete`. Kegagalan discovery Git juga menghasilkan `incomplete`, bukan `skipped`.

Semua yang tak bisa diperiksa dilaporkan apa adanya, tidak pernah dihitung lulus:

| Keluaran | Arti |
|---|---|
| `not_checked` | tak ada checker untuk ekstensi itu, atau file > 2 MB |
| `skipped` | toolchain bahasa tak ada di `PATH` |
| `name_check: unavailable` | `pyflakes` tak terpasang; file Python masuk `skipped` dan verdict `incomplete` |

Python diperiksa in-process, bukan lewat `py_compile`, supaya tak ada `.pyc` yang tertinggal di pohon kerjamu. Direktori `__pycache__`, `node_modules`, `.git`, `vendor`, `.venv`, `venv` dilewati.

`verdict: pass` berarti semua checker yang berlaku selesai tanpa finding dan tidak ada gap.
**Bukan** berarti fiturnya bekerja atau test perilaku telah dijalankan.

---

## Provider releases

Tiap provider di `config/providers.py` menyebut rilis CLI tempat versi workflow ini diuji
(`stable_version`): opencode `1.18.34`, codex `0.154.0`, agy `1.1.13`. Runtime membaca
`<cli> --version` (`core/provider/versions.py`; baris pertama yang memuat semver, dari stdout
lalu stderr) paling banyak sekali per proses dan menyimpannya per user (`%LOCALAPPDATA%` atau
`$XDG_CACHE_HOME`/`~/.cache`, file `agent-workflow/provider-versions.json`), dikunci path,
mtime, dan ukuran executable, kedaluwarsa 24 jam; CLI tanpa versi dicache `null`, timeout tidak
dicache. Tiap usage row yang mencapai provider (call tunggal maupun tiap baris continuation,
semua provider) membawa `provider_version` serta `provider_version_status`: `stable` (sama), `untested` (rilis
lain, termasuk pre-release), `unreadable` (CLI tak menjawab). `doctor` melaporkan
`checks.provider_version` `{provider, version, status, stable}` dan memberi peringatan saat
`untested`. Sifatnya advisori: call tetap jalan; tak ada yang memblokir atau menginstal provider.

Kembali ke rilis stabil bila call gagal setelah provider di-update:

| Provider | Pin |
|---|---|
| codex | `npm i -g @openai/codex@<stable>` |
| opencode | `npm i -g opencode-ai@<stable>` (atau installer opencode dengan versi itu) |
| agy | installer agy dengan versi itu |

Rilis codex 0.155.0 sampai 0.160.0 gagal me-resume thread di Windows dengan
`sandbox_unavailable` (CASE-016); thread baru tetap jalan.

---

## Fact store

`.workflow/data/facts.jsonl` menyimpan pengetahuan yang bertahan lintas sesi. Sebuah klaim masuk lewat salah satu dari dua jalur:

1. ditandai eksplisit `[config]`/`[pattern]`/`[invariant]` oleh second_agent, atau
2. dilaporkan secara mandiri oleh ≥ `fact_recurrence_threshold` sesi **lain**.

Sesi yang sedang berjalan dikecualikan dari hitungan recurrence. Tanpa itu, sebuah fakta yang diinjeksi ke prompt lalu sekadar digemakan kembali bisa menaikkan hitungannya sendiri sampai ambang promosi — pengulangan menyamar jadi bukti.

Tiap fakta ditambatkan ke hash isi baris `file:line`. Ketika baris itu berubah, fakta dianggap usang saat dibaca dan tak pernah disajikan sebagai segar.

Dua klaim dilebur hanya bila **semua** pagar setuju: `file` sama, `category` sama, `anchor_hash` identik, polaritas negasi sama, kemiripan Jaccard ≥ 0.5, dan kedua klaim ≥ 6 kata. Satu pagar menolak → dua-duanya disimpan. Duplikat itu murah; fakta yang hilang tak bisa dikembalikan.

## Evidence reuse dan boundary

Evidence lintas sesi diindeks di `.workflow/data/evidence.jsonl` dengan lock lintas proses dan
atomic rewrite. Entry hanya boleh menunjuk artifact immutable
`sessions/<id>/logs/<prompt_id>/output.raw.md`; hash artifact dan seluruh anchor `file:line`
harus tetap cocok. Anchor yang tidak resolve atau melebihi batas membuat evidence tidak
eligible untuk reuse. Entry lama yang menunjuk `response.last.md` dibuang.
Query cache bersifat case-sensitive dan juga mengikat effective route/model, versi config,
mode fan-out, graph leads, serta facts yang dipakai. `--fresh-session` menonaktifkan lookup
reuse untuk invocation tersebut.

Sebelum delegasi, path relatif di-resolve terhadap project root; traversal, path absolut
di luar root, home-relative path, dan bare sensitive file seperti `credentials.json`
ditolak. OpenCode memakai `external_directory: deny` di level root `opencode.json` project
(berlaku untuk agent apa pun, termasuk override `AI_PROXY_OPENCODE_AGENT`) dan di
`agent.plan`; file discovery harus melalui Read/Grep/Glob built-in. Bash hanya mengizinkan
command Git read-only yang terdaftar.

Environment proses OpenCode (bootstrap, probe, run) tidak lagi mewarisi `os.environ`.
Child mendapat allowlist variabel OS/jaringan (`PATH`, temp, locale, proxy/CA,
`HOME`/`USERPROFILE`/`APPDATA`/`XDG_*`, `OPENCODE_*`) ditambah key dari `.env` di project
root aktif (`adapters/shared/child_env.py`). Auth `opencode auth login` tetap terbaca dari
`~/.local/share/opencode/auth.json`; key provider yang dirujuk config sebagai `{env:NAME}`
harus ada di `.env` project. Call meta mencatat `env_policy`, `project_dotenv`,
`project_dotenv_keys`, `parent_env_dropped` (jumlah, tanpa nilai). Codex/agy tidak berubah.

Semua content dan metadata adapter, termasuk error, timeout, bootstrap, probe, dan
`call.meta.json`, melewati redaksi recursive. Argumen proses mentah tidak disimpan;
telemetry hanya membawa jumlah, panjang, dan hash argv.

Bersihkan yang usang dan duplikat:

```bash
python3 main.py --command clean --work-dir /path/to/target-app --pretty
```

---

## Layout workspace

```text
<target-app>/.workflow/
├─ config.json              # override saja; default diisi saat dibaca
├─ second_agent.json        # salinan project-local, config provider
├─ e2e/secrets.json         # opsional, profil credential verify-browser
├─ current/                 # cermin dispatch terakhir (tanpa credential)
├─ .gitignore
├─ run.ps1  run.sh          # entry point 1-panggilan
├─ inspect.ps1  inspect.sh  # daftar job
├─ check.ps1  check.sh      # status/hasil job
└─ data/                    # semua yang bukan file edit manusia (layout 2)
   ├─ facts.jsonl           # fact store lintas sesi
   ├─ evidence.jsonl        # index artifact immutable lintas sesi
   ├─ audit.jsonl  usage.jsonl  quality.jsonl
   ├─ provider-sessions/    # ID sesi provider, terisolasi per project
   ├─ backups/<stamp>/      # salinan pra-migrasi, 3 terakhir disimpan
   ├─ reports/
   │  └─ doctor.json
   └─ sessions/<session_id>/   # dibuat lazy per sesi
      ├─ state.json
      ├─ scope.json
      ├─ command-cache.json
      ├─ runtime/
      │  ├─ prompt.txt
      │  ├─ prompt.meta.json
      │  ├─ response.last.md
      │  └─ lock
      ├─ logs/<prompt_id>/
      │  ├─ prompt.md
      │  ├─ prompt.sha256
      │  ├─ output.raw.md
      │  └─ call.meta.json
      └─ reports/sweep.last.md
```

State yang berubah-ubah (`state`/`scope`/`cache`/`runtime`/`logs`) hidup di bawah `data/sessions/<id>/`, sehingga dua main_agent pada project yang sama tak pernah saling menimpa. Root `.workflow/` hanya berisi file yang diedit manusia plus script.

Workspace layout v3.5.x (layout 1) menyimpan isi `data/` langsung di root `.workflow/`; runtime tetap membacanya sampai `upgrade` memindahkannya. Langkah pasca-pindah (rewrite path evidence, hapus sisa lama, strip config, konversi `secrets.json`, prune backup) dicatat di `data/.migration-pending.json`; bila salah satu gagal, `upgrade` berikutnya melanjutkan dari langkah itu dan `doctor` melaporkannya sebagai `migration incomplete`. Sisa `data.migrating/` yang namanya bentrok dengan root ditolak tanpa memindahkan apa pun.

Job asinkron tetap disimpan di repo tool pada `storage/jobs/`. Cache ID sesi default berada
di `storage/main-sessions/` dan dipisah dengan hash project root; pemetaan ke ID sesi
OpenCode berada project-local di `.workflow/data/provider-sessions/` sehingga ID yang sama pada
dua project tidak dapat me-resume provider session satu sama lain.

---

## Job asinkron & pemulihan

Command terdelegasi otomatis berjalan lewat worker terpisah. Pada Claude Code, runner
harus diluncurkan sebagai background tool task sehingga tool call foreground segera
mengembalikan task ID; Claude kemudian mengambil hasil task itu. Runtime tetap
agent-agnostic—caller lain boleh memakai runner blocking biasa.

Kalau background task hilang atau pemanggilan terputus, panggil runner lagi dengan
session, command, dan task yang identik. Runtime akan attach bila worker masih hidup.
Jika worker sudah mati, job yang sama dipulihkan satu kali melalui thread provider lama (opencode, codex, atau agy)
dengan prompt continuation terstruktur. Kematian kedua menghasilkan
`recovery_exhausted`, melepas lock, dan tidak memicu loop otomatis.

```powershell
& ".workflow/inspect.ps1"
& ".workflow/check.ps1" <job_id> --wait --result
```

```bash
.workflow/inspect.sh
.workflow/check.sh <job_id> --wait --result
```

Exit code status `check`: `0` selesai · `1` gagal · `2` masih jalan/antre · `3` tak
ditemukan. Dengan `--result` untuk job verify, `0` hanya berarti verdict `pass`; verdict
`fail` atau `incomplete` mengembalikan `2`.

Kalau tak ada job yang cocok, hasil terakhir masih ada di `.workflow/data/sessions/<id>/runtime/response.last.md`. Yang sedang berjalan terlihat di `.workflow/current/session.json` dan `progress.jsonl`.

Recovery bersifat best-effort, bukan process survival: jika thread provider lama tidak
pernah tercatat, runtime gagal sebagai `session_capture_failed` dan clean run diperlukan.
Hasil yang membawa `meta.session_persisted: false` berarti thread itu tidak tersimpan —
recovery maupun continuation untuknya tidak tersedia.
Request berbeda pada session yang masih terkunci tetap ditolak sebagai
`job_already_running`, kecuali worker job pemegang lock terbukti `dead` (PID hilang atau
dipakai ulang proses lain). Dalam kasus itu job lama ditandai `failed` + `reaped`, lock
sesi itu saja dilepas, dan request baru diproses sebagai job baru. Job yang sedang
recovery (belum punya worker PID) dan worker yang hidup atau stalled tidak pernah diambil.
Request identik tetap memakai jalur recovery di atas.

### Liveness worker (v3.8.3)

PID yang hidup **tidak** berarti sedang bekerja. Worker karena itu melaporkan heartbeat sekaligus usia output stream, lalu job diklasifikasi tiga keadaan:

| Keadaan | Arti | Tindakan |
| --- | --- | --- |
| `alive-progressing` | PID hidup, heartbeat segar, stream belum melewati batas idle | tunggu |
| `alive-stalled` | stream idle > `idle_stall_seconds` atau heartbeat basi > `stall_threshold_seconds` | probe fresh session |
| `dead` | PID hilang | reap → `worker_died` |

Saat `alive-stalled`, runtime mengirim PING ke **sesi OpenCode baru**, bukan sesi yang dicurigai menggantung. Probe pertama berjalan segera setelah status stalled terlihat, lalu diulang sesuai `probe_recheck_seconds` selama job masih stalled. Default source saat ini `120` detik, bukan satu menit.

Jalur normal `await` membaca `stall_threshold_seconds`, `idle_stall_seconds`, dan `probe_recheck_seconds` dari config project. Jalur attach `check.py --wait` saat ini memakai default tool untuk ketiganya dan cadence probe tetap 120 detik; tuning project belum diteruskan ke proses attach.

- probe menjawab → simpan `stalled_no_progress`, lanjut menunggu;
- provider menolak karena quota/rate limit → terminate process tree, reap sebagai `rate_limited`;
- stream probe terputus → terminate/reap sebagai `streaming_failed`;
- probe gagal karena alasan lain → terminate/reap sebagai `second_agent_unavailable`;
- PID hilang kapan pun → reap sebagai `worker_died`.

Plafon keras JobManager tetap ada sebagai jaring pengaman: default 5400 detik dan dapat diubah lewat env `AI_PROXY_JOB_MAX_RUNTIME_SECONDS`. Job yang melewatinya gagal sebagai `job_expired` meski PID tampak hidup. Key project-local bernama sama belum mengubah nilai ini.

Setiap panggilan yang mencapai `OpenCodeAdapter` menuliskan `call.meta.json` yang sudah
diredaksi (exit code, durasi, timeout, cara kill, ekor stderr aman, dan agregat argv) ke
`.workflow/data/sessions/<id>/logs/<prompt_id>/`. `verify_mode: syntax` tidak membuat file ini.

---

## Sesi

`--session` adalah otoritas tunggal untuk binding sesi. Panggilan pertama bootstrap sesi OpenCode:

```text
opencode run <prompt> --print-logs --log-level INFO
```

Runtime mengurai `session.id=ses_...` dari log dan menyimpannya. Panggilan berikutnya memakai ulang sesi itu:

```text
opencode run <prompt> -s <provider_session_id>
```

Baris log OpenCode dan banner model dibuang dari `content`; isi jawaban asisten dipertahankan utuh.

---

## Test

```bash
python3 tests/run.py
```

```powershell
python tests/run.py
```

Suite default tidak memanggil OpenCode sungguhan; alur agent memakai adapter palsu,
sedangkan jalur subprocess, heartbeat, timeout, kill-tree, installer, rollback, dan
kontrak persistence diuji secara lokal.

Gunakan `python tools/e2e/e2e.py --full` bila ingin menambahkan smoke test OpenCode nyata; mode itu opt-in karena memakai quota.

Pemeriksaan tambahan:

```bash
python3 main.py --help
python3 main.py --command doctor --work-dir . --pretty
```

---

## CI

Dua workflow GitHub Actions menjalankan gerbang yang tanpanya seseorang harus ingat
menjalankannya sendiri.

| File | Pemicu |
|---|---|
| `.github/workflows/ci.yml` | push ke `main`/`dev`, pull request, manual |
| `.github/workflows/e2e-full.yml` | manual saja |

Gerbang yang dijalankan jalur gratis, berurutan:

```
python tools/maintain/stamp_version.py --check
python tools/maintain/gen_manifest.py --check
python tests/run.py --only deps
python tests/run.py --keep-going --record .
python tools/e2e/e2e.py
```

`--only deps` berdiri sebagai step tersendiri, bukan hanya di dalam suite: itu gerbang yang
memutuskan apakah ketiadaan lockfile masih benar, dan pembaca yang menyapu daftar step harus
bisa melihatnya berlaku. `--keep-going` supaya satu kegagalan tak menyembunyikan sisanya;
`--record .` menulis hasilnya ke stream kualitas workspace, jadi `--command report` bisa
menunjukkan pass rate lintas waktu alih-alih hanya run terakhir. `e2e.py` tanpa `--full`:
nol provider CLI dipanggil, nol kuota dibakar — itu sebabnya ia boleh jalan di tiap push.

Jalur terdelegasi manual dan tak pernah otomatis: ia memanggil second_agent sungguhan,
memakan kuota nyata, dan butuh provider CLI plus kredensial yang sengaja tidak dipegang
pipeline. Ia workflow `workflow_dispatch` terpisah, dengan session id dan runner sebagai
input dispatch — default `e2e-dispatch` dan `self-hosted`.

### Toolchain dan matrix

Python dipaku ke `3.13` lewat `actions/setup-python`, jadi workflow MENYEDIAKAN interpreter
alih-alih berharap runner membawanya. Matrix `runs-on` mencakup `ubuntu-latest` dan
`windows-latest` dengan `fail-fast: false`: runtime mengirim runner PowerShell dan
menghasilkan yang POSIX, dan penanganan path paling berbeda persis di tempat yang penting
(lock, direktori sesi) — run satu-OS akan lolos sementara separuh produk tak teruji, dan satu
platform yang merah tak boleh menyembunyikan hasil platform lain.

`on: push` (`main`/`dev`) + `pull_request` + `workflow_dispatch`, dengan
`concurrency: cancel-in-progress` supaya pipeline yang tersalip di ref yang sama dibatalkan
alih-alih dibiarkan selesai melawan commit basi. Permission job dikunci ke `contents: read`.

Yang **tidak** dilakukan CI: bump, tag, publish. Langkah yang tetap milik
manusia ada di `RELEASE.md`.

---

## Batasan yang diketahui

Dipindah ke [`docs/limitations.md`](limitations.md). Heading ini dipertahankan supaya tautan
lama ke `#batasan-yang-diketahui` tetap berfungsi.

---

## Benchmark

Dipindah ke [`docs/evaluation/benchmark.md`](evaluation/benchmark.md). Hasil pemakaian nyata
(task yang masuk `main`, perbaikan, prompt, context) ada di
[`docs/evaluation/real-use-benchmark.md`](evaluation/real-use-benchmark.md); telemetry status
line di [`docs/evaluation/observed-usage.md`](evaluation/observed-usage.md).

---

## Referensi

- Catatan rilis: [`CHANGELOG.md`](../CHANGELOG.md)
- Kontrak canonical main_agent: [`dist/config/claude/CLAUDE.md`](../dist/config/claude/CLAUDE.md)
- Kontrak canonical second_agent: [`dist/config/opencode/AGENTS.md`](../dist/config/opencode/AGENTS.md)
- Runtime entry point: [`main.py`](../main.py)
- Benchmark: [`docs/evaluation/benchmark.md`](evaluation/benchmark.md) (metode sebagai `H` + `EXP` di `docs/research/`)
- Arsitektur (diagram terverifikasi): [`docs/architecture/`](architecture/README.md)
- Batasan: [`docs/limitations.md`](limitations.md)
- Evaluasi (benchmark dan telemetry): [`docs/evaluation/`](evaluation/README.md)
- Catatan riset dan keputusan desain: [`docs/research/`](research/README.md); pertanyaan riset (`RQ`): [`docs/research/questions.md`](research/questions.md)
