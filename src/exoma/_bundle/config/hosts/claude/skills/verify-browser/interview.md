# Skill Reference: verify-browser — Setup Interview (STEP 1)

Read this file ONLY when the STEP 3 draft reports `blocked` (`base_url_unreachable` or wrong target) or `invalid` due to missing flow/project configuration.

## STEP 1 — Interview ({{QUESTION_TOOL}}, main thread)
Fill from context first (diff, the user's message); ask ONLY what is still uncertain. {{ASK_USER}}

1. **Flow/feature under test + claims**: the flow to exercise and the outcome that counts as correct. Offer options from context when available.
2. **`base_url`**: `http://localhost:8000` | `http://127.0.0.1:8000` | a virtual host (e.g. `http://app.test`) → the user fills it in through "Other".
   Three classes, never mixed:
   - Loopback (`localhost`/`127.0.0.1`/`::1`/`*.localhost`) → passes as is.
   - `.test` (RFC 6761 reserved suffix for local development) → treated like localhost: passes WITHOUT `allow_remote`, writes need no DNS lookup or pinning.
   - Real domain (`staging.example.com`) → REQUIRES `allow_remote: true` + `allowed_origins: ["<scheme://host[:port]> exactly"]` in the `e2e` section of `config.json` — NOT in the request. Show the snippet; the user writes/approves the `config.json` edit.
   Navigation to ANOTHER origin (another `.test` included) still needs an `allowed_origins` entry, whatever the `base_url`'s class.
3. **Login needed?** `no` | `yes, the account already exists`.
   - Credential key names are FIXED by the runtime: `E2E_USER`, `E2E_PASS` (registry `CREDENTIAL_KEYS`). Scenarios may only use `${E2E_USER}` / `${E2E_PASS}`, exact case; another name or wrong case → `spec_invalid`. Do NOT invent other key names.
   - Yes → ask for the account PROFILE NAME (e.g. `qa`, `admin`), NOT values. Set `secrets_template_profiles` (used only when the file does not exist yet) and pick one via `secrets_profile`.
   - User fills values in `.workflow/e2e/secrets.json` (gitignored):
     `{"default": "qa", "profiles": [{"name": "qa", "credentials": {"E2E_USER": "...", "E2E_PASS": "..."}}, {"name": "admin", "credentials": {...}}]}`.
   - Multiple accounts = multiple profiles; one run uses one profile (`secrets_profile`, empty = `default`).
   - Missing file → STEP 3 draft creates the template with empty slots; an existing file is never rewritten, even under concurrent drafts.
   - Legacy dict format (`"profiles": {"qa": {...}}`) is REFUSED (`secrets_invalid`), not converted → ask the user to rewrite it in the list format above.
   - User pastes a password in chat → do NOT use, repeat, or write it; ask them to put it in `secrets.json`.
4. **Display**: **headed by default** (`headless: false`) — ask only when the user seems to want otherwise (`headed + slow_mo_ms 700` watchable | `headed` without pauses | `headless` for CI/no display). Config knob, not a per-run question.
5. **Data side effects (create/change/delete test data)?** `no` (default) | `allowed` (LOCAL app only: loopback, `.test`, or private host; every change must have runnable cleanup).
   - `allow_local_side_effects` (default `true`) allows POST/PUT/PATCH only (not custom verbs like `PURGE`/`PROPFIND`) to loopback, `*.localhost`, and `.test` without opt-in so form logins work. Set `allow_local_side_effects: false` to restore the blanket block.
   - `DELETE` and any request carrying a `_method` field (body, query, or `X-HTTP-Method-Override` / `X-Method-Override` headers, across percent/JSON-escape/multipart/UTF-16 decodings) are NEVER covered by `allow_local_side_effects` or `allow_side_effects`. They trigger `override_unapproved` / `mutation_blocked` and require endpoint approval in `allowed_destructive_requests` (`.workflow/e2e/permissions.json`).
   - A POST whose body merely spells `_method` also asks for approval by design (coarse provable rule). Multipart bodies fall back to `post_data_buffer`; an unreadable write body is unconditionally refused (`uninspectable_body`, observation `write_uninspected`) — run upload scenarios via `existing_test_command` instead.
   - Non-local hosts with `allow_side_effects: false`: player aborts everything except GET/HEAD/OPTIONS. Read-only POSTs (search/filter/GraphQL query) are proposed in `read_only_requests` with handler `file:line` proof, confirmed by user, then placed in `allowed_read_only_requests`. GraphQL mutations/subscriptions/persisted queries stay blocked.
   - `allow_side_effects: true`: loopback and `.test` accept writes directly. Other hosts are judged by resolved IP: preflight refuses (`spec_invalid`) public IPs, link-local (`169.254.0.0/16`), or unresolvable hosts. Private/loopback IPs are pinned via `--host-resolver-rules` (Chromium only; non-Chromium or HTTPS writes to pinned hosts are refused). `allowed_mutation_paths` is REMOVED (`request_invalid`).
   - 307/308 write redirects are followed by the guard and checked on every hop before resending the body. GET mutations, WebSockets, and service workers remain invisible to the guard.
6. **Existing project tests (optional)**: argv command without a shell (`{files}`, `{base_url}`) + allowlist glob — config-only in `config.json` `e2e`, never in `request.json`. None → skip.

Hybrid review ALWAYS runs (2 second_agent calls: draft + review); not a question. After collecting answers, offer to save project defaults in `config.json`, then return to STEP 2–3 in the main skill.
