# Skill: commit
description: Stage + commit message (Conventional Commits). Local.

## Trigger
/.commit

## Execution
1. Stage. `git diff --cached --name-only` sudah berisi → pakai itu apa adanya, jangan tambah.
   Kosong → `git -c core.quotePath=false diff --name-only` + `git -c core.quotePath=false
   ls-files --others --exclude-standard` (nama unicode tak di-escape), lalu
   `git add -- <path...>`: tiap path satu argumen ber-quote, tak pernah `-A`/`.`. Lewati
   path secret, cocokkan nama file case-insensitive: `.env*` (kecuali `.example`/`.sample`/
   `.template`), `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_ed25519*`, `*credential*`,
   `*secret*`. Tetap kosong → "[COMMIT] Tak ada perubahan.", STOP.
2. `git diff --cached` → type (feat|fix|refactor|chore|docs|test|perf|style|build|ci), scope,
   subject (≤50, imperative, no period), body hanya bila why non-obvious.
3. Output:
   [COMMIT] staged: <path...> | untracked baru: <path...> | skipped: <path secret> (kosong: none)
   [COMMIT MESSAGE] <pesan>
   "Jalankan? (yes/no)" → yes: `git commit`. no → `git reset -q -- <path>` hanya untuk
   path yang di-stage langkah 1 (jalan juga di repo tanpa commit), STOP.
   JANGAN commit tanpa yes.
