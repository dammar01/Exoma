# Skill Reference: verify-browser — Interactive Approvals, Tags & Knowledge (STEPS 4–6)

Read this file ONLY when:
- showing a `[BROWSER TEST PLAN]` summary or handling a paused draft (`env_missing`, `read_only_pending`);
- handling post-run `destructive_pending` approvals or editing `.workflow/e2e/permissions.json`;
- resuming after pending input (STEP 5);
- applying `data-e2e` proposals (`meta.e2e.tags` has `ready` entries, STEP 6) or promoting browser knowledge.

## STEP 4 — Prepared scenario and pending input
Show `[BROWSER TEST PLAN]` from `meta.e2e.draft` when useful or paused. Never ask "Run | Cancel" — invoking {{CMD:verify-browser}} is already authorization.
```text
[BROWSER TEST PLAN]
target: <base_url> (loopback | .test | remote allow-listed: <origin>)
display: headless | headed slow_mo <n> ms
auth: <key name> — secrets.json profile <name>: set | NOT YET (missing_env; secrets_file path)
claims: - <id> | <severity> | <description> | <source_refs>
steps: <step id. action → target (within <container>) → ready when <ready> → request <METHOD path> → claim_id>
existing_tests: <path covers id> | none
side_effects: none (writes blocked) | <step id + side_effect + test_data.marker + expected request>
cleanup: <cleans step id: step → result assertion> | no_cleanup_reason <reason> | none
read_only_requests: <draft proposals: METHOD endpoint | source_refs | reason> | none
risks: <data changes (local only), real accounts, remote URL, missing_env, spec_uncertainties, uncleaned writes>
cost: draft + review included in invocation; blocked preparation has not reached review
retry: <n> attempts on environment incomplete | off (allow_side_effects true)
knowledge: <N entries from earlier runs used by draft> | none yet
```

Only two conditions pause BEFORE playback:
- `missing_env` non-empty (`env_missing`) → ask user to populate the active profile in `secrets.json`; wait until they confirm.
- `meta.e2e.read_only_pending` non-empty → {{MULTI_CHOICE}}, one option per entry (`description` = `source_refs` + `reason`). Write ONLY checked entries into `allowed_read_only_requests` as `"POST <endpoint>"` on resume. Adding unproposed or unconfirmed entries is FORBIDDEN.

One condition asks AFTER a run:
- `meta.e2e.destructive_pending` non-empty → run met an unapproved `DELETE` or `_method` request. Ask via {{MULTI_CHOICE}}, one option per route (`method` + `endpoint`, e.g. `/api/items/:id`; state that approving approves the route). Adding unproposed entries or approving without user consent is FORBIDDEN.
- Write checked entries to **`.workflow/e2e/permissions.json`** (persists across sessions; never put in `request.json`):
```json
{
  "allowed_destructive_requests": ["DELETE http://localhost:8000/api/items/:id"],
  "blocked_requests": ["* /api/payments", "DELETE /api/users/:id"]
}
```
Write exact `entry` strings; create with both keys if absent; append only (never delete/reorder existing entries). A broken `permissions.json` (`blocked_requests` invalid/unparseable) refuses the run (`request_invalid`); a broken `allowed_destructive_requests` warns and drops approvals.
- `blocked_requests` (`*` = every method) wins over every setting (including `allow_side_effects` and `allowed_destructive_requests`), is re-checked on every 307/308 hop (`blocked_by_policy`), and also governs navigation (`goto`, `framenavigated`, `history.pushState`, meta refresh, browser redirects → `navigation_blocked`; `GET` and `*` entries block navigation, `DELETE` does not). Only the user populates `blocked_requests`; offer to add endpoints the user says must never be touched.

## STEP 5 — Resume after pending input
Only a blocked/invalid preparation needs another invocation. Keep `"phase": "auto"`, update only user-supplied/approved settings, and call the same runner. Fresh preparation is generated; never copy `scenario`, `existing_tests`, or `spec_notes` from `draft.json`.

## STEP 6 — Element tags (optional, ONE batch confirmation)
When `meta.e2e.tags` (`tag-proposals.json`) has zero `ready` entries, skip STEP 6 silently.
Runtime proposal bounds (non-negotiable): step `passed`, winning selector provenance `source` with `path:line` inside project (not gitignored), line holds ONE opening tag without markup in comments and no existing `data-e2e`. Patching from DOM (`page.content()`) is FORBIDDEN.
Flow:
1. Show `status: ready` entries as `old_line` → `new_line`, plus `skipped` entries with `reason`.
2. ONE {{QUESTION_TOOL}} for the whole batch: `Write all` | `Pick some` | `Skip`.
3. Approved → apply exact `old_line`/`new_line` edits; skip any line that changed since planning or became a symlink outside the project.
4. After writing → offer {{CMD:promote}} with one claim per tag (`e2e-tag-<tag>`, new `path:line` anchor); do not write knowledge directly from here.

## Browser knowledge (automatic, per origin)
Every first-attempt passing non-write step records proven facts in `.workflow/data/e2e-knowledge.jsonl` (`auth.login`, `auth.logout`, `navigation`, `page_ready`, `selector`; placeholders only, no credentials, not Git-tracked).
- Draft selector precedence: `e2e` with `source` `path:line` or `proven` from `e2e_knowledge.json` (same route + `within` only) may lead role+name > label > e2e > text > css. Claiming `proven` without store backing → `spec_invalid`. One miss drops to ordinary order; 2 consecutive misses retire the entry (`--command clean` prunes retired/missing anchors).
- Relay `meta.e2e.knowledge` (`added | confirmed | weakened | retired`). Never claim knowledge was used when `offered == 0`.
- Promote: anchored entries proven in ≥3 runs can be promoted via {{CMD:promote}} (`e2e-<kind>-<id>`) when the user asks for flow documentation.
