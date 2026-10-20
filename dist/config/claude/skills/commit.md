# Skill: commit
description: Stage + commit message (Conventional Commits). Local.

## Trigger
/.commit

## Execution
1. Stage. `git diff --cached --name-only` sudah berisi → pakai itu apa adanya, jangan tambah.
   Kosong → `git -c core.quotePath=false diff --name-only` + `git -c core.quotePath=false
   ls-files --others --exclude-standard` (nama unicode tak di-escape), lalu
   `git add -- <path...>`: tiap path satu argumen ber-quote, tak pernah `-A`/`.`. Lewati
   path secret: cocokkan path lengkap (pemisah `/`, huruf kecil) ke `skip` ala fnmatch,
   kecuali cocok `keep`. Tetap kosong → "[COMMIT] Tak ada perubahan.", STOP.
   <!-- secret-patterns: sama persis dengan core/policy/secret_patterns.py (dicek test) -->
   skip: `*.env` `*.env.*` `*id_rsa*` `*id_ed25519*` `*id_ecdsa*` `*.pem` `*.key` `*.p12`
   `*.pfx` `*.jks` `*.keystore` `*.ppk` `*.kdbx` `*.ssh/*` `*.aws/*` `*.gnupg/*` `*.kube/*`
   `*.docker/*` `*.npmrc` `*.netrc` `*.pgpass` `*.htpasswd` `*.tfvars` `*.git-credentials`
   `*credentials.json` `*credentials.yml` `*credentials.yaml` `*service-account*.json`
   keep: `*.env.example` `*.env.sample` `*.env.template`
   <!-- /secret-patterns -->
2. `git diff --cached` → type (feat|fix|refactor|chore|docs|test|perf|style|build|ci), scope,
   subject (≤50, imperative, no period), body hanya bila why non-obvious.
3. Output:
   [COMMIT] staged: <path...> | untracked baru: <path...> | skipped: <path secret> (kosong: none)
   [COMMIT MESSAGE] <pesan>
   "Jalankan? (yes/no)" → yes: `git commit`. no → `git reset -q -- <path>` hanya untuk
   path yang di-stage langkah 1 (jalan juga di repo tanpa commit), STOP.
   JANGAN commit tanpa yes.
   Hook `execution-gate` memblok `git commit` di giliran yang user tak minta commit
   (/.commit atau kalimat yang meminta commit; "yes" atas pesan yang kamu usulkan ikut sah).
