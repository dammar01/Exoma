# Skill: commit
description: Stage + commit message (Conventional Commits). Local.

## Trigger
/.commit

## Execution
1. Stage. `git diff --cached --name-only` already has entries → use them as they are, add nothing.
   Empty → `git -c core.quotePath=false diff --name-only` + `git -c core.quotePath=false
   ls-files --others --exclude-standard` (unicode names unescaped), then
   `git add -- <path...>`: one quoted argument per path, never `-A`/`.`. Skip
   secret paths: match the full path (`/` separators, lower case) against `skip` fnmatch-style,
   unless it matches `keep`. Still empty → "[COMMIT] No changes.", STOP.
   <!-- secret-patterns: identical to core/policy/secret_patterns.py (checked by a test) -->
   skip: `*.env` `*.env.*` `*id_rsa*` `*id_ed25519*` `*id_ecdsa*` `*.pem` `*.key` `*.p12`
   `*.pfx` `*.jks` `*.keystore` `*.ppk` `*.kdbx` `*.ssh/*` `*.aws/*` `*.gnupg/*` `*.kube/*`
   `*.docker/*` `*.npmrc` `*.netrc` `*.pgpass` `*.htpasswd` `*.tfvars` `*.git-credentials`
   `*credentials.json` `*credentials.yml` `*credentials.yaml` `*service-account*.json`
   keep: `*.env.example` `*.env.sample` `*.env.template`
   <!-- /secret-patterns -->
2. `git diff --cached` → type (feat|fix|refactor|chore|docs|test|perf|style|build|ci), scope,
   subject (≤50, imperative, no period), body only when the why is non-obvious.
3. Output:
   [COMMIT] staged: <path...> | new untracked: <path...> | skipped: <secret path> (empty: none)
   [COMMIT MESSAGE] <message>
   "Run it? (yes/no)" → yes: `git commit`. no → `git reset -q -- <path>` only for the
   paths staged in step 1 (works in a repo with no commits too), STOP.
   Do NOT commit without yes.
   The `execution-gate` hook blocks `git commit` in a turn where the user did not ask to commit.
   Permission comes ONLY from a user prompt that asks for a commit (/.commit, or a sentence with
   the word "commit" that does not negate it). A bare "yes"/"ya"/"ok" only KEEPS that permission
   into the next turn — it never creates it — and any other prompt revokes it.
   So: a commit proposed outside the /.commit flow (e.g. after a prompt that did not ask to commit)
   → do NOT ask for "yes"; ask the user to type /.commit or "commit".
