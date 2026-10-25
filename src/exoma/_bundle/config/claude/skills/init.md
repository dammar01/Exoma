# Skill: init
description: Buat/regenerate .workflow/ workspace. Local. Bootstrap lewat CLI `exoma`.

## Trigger
/.init

## STEP 1 — Resolve bootstrap source (WAJIB — urutan ini, JANGAN dilewati)
PENTING: runtime TIDAK ada di project. Ia package Python `exoma` (`pip install exoma` / `uv tool install exoma`).
Resolve berurutan:
1. `exoma --version` jalan → INI SUMBER. Lanjut STEP 2 dengan `exoma`.
2. Tak ada di PATH tapi `<python> -m exoma --version` jalan → pakai `<python> -m exoma`.
3. Masih tak ada → minta user install dulu: `uv tool install exoma` atau `pip install exoma`.
`$AGENT_PATH` (checkout agent-workflow) = alias lama: dibaca hanya kalau user menyebutnya, BUKAN sumber utama.
JANGAN simpulkan "package missing" sebelum cek `exoma --version` dan `<python> -m exoma --version`.

## STEP 2 — Run init
work_dir = absolute path project aktif.
Semua OS (tool Bash): exoma init --work-dir "<work_dir>" --pretty
(fallback: `<python> -m exoma init --work-dir "<work_dir>" --pretty`)
<python> = yang pertama JALAN dan ≥ 3.10 dari `python3`, `python`, `py -3` (Windows sering `python`, mac/linux sering cuma `python3`). Lebih tua → exoma menolak dengan satu pesan; pakai interpreter lain, jangan tebak "package missing".
init otomatis: generate scripts (run/inspect/check .sh, bash di semua OS, memanggil `<python> -m exoma`) + config (interpreter + package root, abs-path) + copy second_agent.json + sessions/ scaffold + .gitignore (.workflow/). state/scope/cache/logs/runtime = per-session, dibuat lazy saat delegated call pertama (BUKAN di root).
init SCAFFOLD, bukan pembaruan: config.json dan second_agent.json yang SUDAH ADA tidak ditimpa (tak ada active-job guard di init — itu milik /.upgrade). Workspace dari build lama tetap versi lama sesudah init. `meta.upgrade_needed: true` + `meta.versions` di output = jalankan /.upgrade.

## Output
[INIT]
bootstrap: exoma <versi> (<exoma | python -m exoma>)
generated: run/inspect/check.sh + config.json (v0.1.0, python + package_root abs) + second_agent.json (copy) + sessions/ (state/scope/cache/logs/runtime per-session, lazy)
gitignore: .workflow/ ok
upgrade_needed: <meta.upgrade_needed> (true → sebut versi dari meta.versions + tawarkan /.upgrade)
status: READY
".workflow siap. Coba /.explore atau /.doctor."
