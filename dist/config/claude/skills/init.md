# Skill: init
description: Buat/regenerate .workflow/ workspace. Local. Bootstrap dari $AGENT_PATH (repo agent-workflow).

## Trigger
/.init

## STEP 1 — Resolve bootstrap source (WAJIB — urutan ini, JANGAN dilewati)
PENTING: main.py TIDAK ada di project. Ada di repo agent-workflow. Pointer utama = $AGENT_PATH.
Resolve berurutan:
1. Cek $AGENT_PATH — Windows: `$env:AGENT_PATH` | POSIX: `echo $AGENT_PATH`.
   Berisi path + file exists → INI SUMBER. Lanjut STEP 2.
2. Kosong tapi .workflow/config.json ada → baca runtime.main_py_path.
3. Masih tak ada → tanya user path repo agent-workflow, ATAU minta set:
   Windows: [Environment]::SetEnvironmentVariable("AGENT_PATH","<repo>\main.py","User")
   POSIX:   export AGENT_PATH="<repo>/main.py"
JANGAN simpulkan "package missing / chicken-egg" sebelum cek $AGENT_PATH.
JANGAN hunt main.py di project/global/pip/npm — bukan package, ini git repo via $AGENT_PATH.

## STEP 2 — Run init
work_dir = absolute path project aktif.
Windows: python "$env:AGENT_PATH" --command init --work-dir "<work_dir>" --pretty
POSIX:   python3 "$AGENT_PATH" --command init --work-dir "<work_dir>" --pretty
init otomatis: generate scripts (run/inspect/check) + config abs-path + copy second_agent.json + sessions/ scaffold + .gitignore (.workflow/). state/scope/cache/logs/runtime = per-session, dibuat lazy saat delegated call pertama (BUKAN di root).
init SCAFFOLD, bukan pembaruan: config.json dan second_agent.json yang SUDAH ADA tidak ditimpa (tak ada active-job guard di init — itu milik /.upgrade). Workspace dari build lama tetap versi lama sesudah init. `meta.upgrade_needed: true` + `meta.versions` di output = jalankan /.upgrade.

## Output
[INIT]
bootstrap: $AGENT_PATH = <path>
generated: run/inspect/check.{ps1,sh} + config.json (v3.8.3, main_py_path abs) + second_agent.json (copy) + sessions/ (state/scope/cache/logs/runtime per-session, lazy)
gitignore: .workflow/ ok
upgrade_needed: <meta.upgrade_needed> (true → sebut versi dari meta.versions + tawarkan /.upgrade)
status: READY
".workflow siap. Coba /.explore atau /.doctor."
