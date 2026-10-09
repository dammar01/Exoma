# Changelog

This file is the index only: one row per version, a one-line theme, and a link. From 3.7.3
on, each version's notes are their own file, `docs/releases/v<version>.md`; notes for 3.2.1
through 3.7.2 stay in the historical archive, one directory per version under `prompt/`.
Behavior itself is described in `docs/reference.md` and `docs/runtime-contracts.md` — the
notes say what changed and link there rather than repeating it.

Release procedure: `RELEASE.md`.

| Version | Notes | Theme |
|---------|-------|-------|
| 3.8.3 | [docs/releases/v3.8.3.md](docs/releases/v3.8.3.md) | `/.provider` menawarkan model dari CLI provider terpasang dengan yang terbaru sebagai rekomendasi, varian agy jadi pilihan effort, dan pilihan bisa diketik langsung (`/.provider codex gpt-6-luna low`) |
| 3.8.2 | [docs/releases/v3.8.2.md](docs/releases/v3.8.2.md) | Penutup sebelum feature freeze: codex tak lagi bisa menulis lewat tool edit di sandbox read-only; boundary baca/tulis per provider; thread provider tak hilang diam-diam; manifest dan scrub artefak gagal tertutup; semua writer temp-dan-replace ikut retry; worker mati tak lagi mengunci sesi |
| 3.8.1 | [docs/releases/v3.8.1.md](docs/releases/v3.8.1.md) | Export memeriksa nama project di semua panjang dan key per-project wajib label; gate meloloskan Read skill; selector terbukti berlaku lintas id di route dinamis; `tool_commit` dan versi provider tak lagi mengklaim yang tak terbukti |
| 3.8.0 | [docs/releases/v3.8.0.md](docs/releases/v3.8.0.md) | Latensi terukur per komponen, network metadata dan selector terbukti di `/.verify-browser`, telemetry per task lewat hook `task-events`, `clean` membuang lock guard sesi mati, RQ jadi record YAML |
| 3.7.3 | [docs/releases/v3.7.3.md](docs/releases/v3.7.3.md) | `/.verify-browser` DELEGATED di semua lapisan; **breaking:** selector `testid` → `e2e`; perbaikan draft, rem `repeat_failure`, dan polling player; secret literal ditambal per nilai; origin `scenario` + diagnosa terstruktur; research jadi YAML ber-skema; CHANGELOG jadi index; `bench/` dihapus |
| 3.7.2 | [prompt/v3.7.2/changelog.md](prompt/v3.7.2/changelog.md) | Rilis dokumentasi, tanpa perubahan runtime: `docs/research/` (metodologi, kontrak pengembangan dan riset, template tujuh jenis catatan), `CLAUDE.md` root untuk pengembangan repo ini, `docs/team-guide/` (panduan adopsi tim: kapan dipakai, task framing, contoh, troubleshooting), index `docs/README.md` dengan kontrak tiga lapis team guide/research/reference, catatan riset (`CASE-001..003`, `SYN-001..002`, `H-001..003`, `EXP-001`, daftar RQ, log periode 2026-09-26), gambar arsitektur baru `architecture.png` menggantikan `flow.png` (versi teks di `docs/architecture/`), `docs/architecture/` terverifikasi, batasan dan benchmark dipindah ke `docs/limitations.md` dan `docs/evaluation/`, telemetry keluar dari README dengan unit call/row dipisah, README ditulis ulang dengan klaim security per provider, koreksi dokumentasi usang |
| 3.7.1 | [prompt/v3.7.1/changelog.md](prompt/v3.7.1/changelog.md) | Menutup temuan audit codebase 2026-09-24 pada batas kepercayaan: key policy `/.verify-browser` jadi config-only (**breaking**), environment OpenCode dipangkas ke allowlist + `.env` project (**breaking**), paritas pola secret `grep`/`read` OpenCode, jalur baca browser menolak alamat metadata, lock store bertoken owner, rollback migrasi dan receipt installer yang melapor jujur, recurrence fact = 5 dari satu sumber |
| 3.7.0 | [prompt/v3.7.0/changelog.md](prompt/v3.7.0/changelog.md) | `/.verify-browser` dan evidence diperketat: **breaking:** body write tak terbaca ditolak (`uninspectable_body`), DELETE dan request pembawa `_method` butuh persetujuan per endpoint, confidence dibatasi rasio anchor tersertifikasi (`anchor_cap`), rem pengulangan menghitung kegagalan preflight; `blocked_requests` fail-closed, izin write lokal, route template; rem per bucket dengan `source`/`mixed` dan `ignore_repeat_brake`; anchor sadar drive Windows; task tanpa argv tak dibatasi, diganti `scope_width` |
| 3.6.0 | [prompt/v3.6.0/changelog.md](prompt/v3.6.0/changelog.md) | `/.verify-browser`: verifikasi lewat browser Playwright, fail-closed, dengan section `e2e` per project + override per run, default headed, retry khusus lingkungan, guard origin dan guard write berbasis alamat (redirect dan DNS rebinding ikut dinilai), credential hanya sebagai nama key dan profil `secrets.json` (list), POST read-only yang dikonfirmasi dengan bukti handler, knowledge browser, usulan tag `data-e2e`, dan Playwright sebagai extra opsional `--with-e2e`; **layout workspace `data/`** — `.workflow/` hanya berisi file yang diedit user, internal pindah ke `.workflow/data/`, workspace v3.5.x dimigrasi otomatis oleh `upgrade` dengan backup dan rollback, `config.json` jadi override saja, `.workflow/current/` baru; **breaking:** `allowed_mutation_paths` dihapus, tak ada fallback ke `config/second_agent.json` level mesin, opencode tanpa model ditolak `model_unset`, seed jadi `config/second_agent.seed.json`; prompt opencode lewat file lampiran (`-f`) alih-alih argv; installer menolak dist yang tak cocok manifest dan membersihkan file rilis lama; thread second_agent disimpan per provider; `~` statusline tak lagi dinyalakan oleh panggilan gagal; transcript Claude Code terbaca untuk mengukur sisi premium; `stamp_version` tak lagi membaca alamat IP sebagai versi; isi yang sempat dicatat sebagai 3.6.1 dan 3.7.0 dirilis di sini; tag pertama sejak v3.5.1 — 3.5.2 dan 3.5.3 tak pernah di-tag dan isinya dirilis bersama tag ini |
| 3.5.3 | [prompt/v3.5.3/changelog.md](prompt/v3.5.3/changelog.md) | opencode ikut terukur: adapter meminta `--format json`, membaca `step_finish.part.tokens`, dan melipat `cache.read` ke input serta `reasoning` ke output karena opencode melaporkan keduanya sebagai addend terpisah; stdin ditutup karena mode JSON menggantung tanpa itu; `clean_output` menyusun jawaban dari event `text` |
| 3.5.2 | [prompt/v3.5.2/changelog.md](prompt/v3.5.2/changelog.md) | Token nyata dari provider masuk ke usage.jsonl: reasoning dan cached ikut terekam sebagai rincian yang tak pernah dijumlahkan; satu baris per panggilan provider; codex terukur, opencode tetap estimasi dengan alasan yang dicatat; statusline global merender angka itu di setiap prompt, dikirim sebagai `workflow-statusline.{ps1,sh}` |
| 3.5.1 | [prompt/v3.5.1/changelog.md](prompt/v3.5.1/changelog.md) | Task cap diturunkan dari transport provider (argv vs stdin) alih-alih satu konstanta; kegagalan tulis stdin codex tak lagi ditelan; perbaikan parsing digest CRLF |
| 3.5.0 | [prompt/v3.5.0/changelog.md](prompt/v3.5.0/changelog.md) | Knowledge ter-Git dan `/.promote` yang menulisnya; generator skrip runner dengan deteksi drift; benchmark dijalankan sungguhan |
| 3.4.5 | [prompt/v3.4.5/changelog.md](prompt/v3.4.5/changelog.md) | Release stability (CI, test runner, release procedure) and the measurement layer: workflow contracts, telemetry, governance, verified graphify; agy provider behind an explicit opt-in |
| 3.4.4 | [prompt/v3.4.4/changelog.md](prompt/v3.4.4/changelog.md) | Second provider: codex adapter, provider registry, read-boundary findings |
| 3.4.2 | [prompt/v3.4.2/changelog.md](prompt/v3.4.2/changelog.md) | — |
| 3.4.1 | [prompt/v3.4.1/changelog.md](prompt/v3.4.1/changelog.md) | — |
| 3.4.0 | [prompt/v3.4.0/changelog.md](prompt/v3.4.0/changelog.md) | — |
| 3.3.1 | [prompt/v3.3.1/changelog.md](prompt/v3.3.1/changelog.md) | — |
| 3.3.0 | [prompt/v3.3.0/changelog.md](prompt/v3.3.0/changelog.md) | — |
| 3.2.1 | [prompt/v3.2.1/changelog.md](prompt/v3.2.1/changelog.md) | — |
| 3.2.0 | [prompt/v3.2.0/](prompt/v3.2.0/) | — |
| 3.1.2 | [prompt/v3.1.2.md](prompt/v3.1.2.md) | — |
| 3.1.1 | [prompt/v3.1.1.md](prompt/v3.1.1.md) | — |
| 3.1.0 | [prompt/v3.1.0.md](prompt/v3.1.0.md) | — |
| 3.0.1 | [prompt/v3.0.1.md](prompt/v3.0.1.md) | — |
| 3.0.0 | [prompt/v3.0.0.md](prompt/v3.0.0.md) | — |
| 2.0.0 | [prompt/v2.0.0.md](prompt/v2.0.0.md) | — |
| 0.0.0 | [prompt/v0.0.0.md](prompt/v0.0.0.md) | — |

v3.4.3 was built but never released; its changes are described inside the v3.4.4 notes.

Also released without a row of its own: [v3.4.5 addendum](docs/releases/v3.4.5-addendum.md) — what the `v3.4.5` tag contains beyond its notes.
