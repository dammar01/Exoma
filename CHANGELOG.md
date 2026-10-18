# Changelog

This file is the index only: one row per version, a one-line theme, and a link. From 3.7.3
on, each version's notes are their own file, `docs/releases/v<version>.md`; notes for 3.2.1
through 3.7.2 stay in the historical archive, one directory per version under `prompt/`.
Behavior itself is described in `docs/reference.md` and `docs/runtime-contracts.md` — the
notes say what changed and link there rather than repeating it.

Release procedure: `RELEASE.md`.

| Version | Notes | Theme |
|---------|-------|-------|
| 3.8.4 | [docs/releases/v3.8.4.md](docs/releases/v3.8.4.md) | Provider stable pins move to opencode 1.19.3 and codex 0.163.0 after a live boundary probe; the intent gate ignores subagent hand-backs and other turns the user did not write; user documentation translated to English |
| 3.8.3 | [docs/releases/v3.8.3.md](docs/releases/v3.8.3.md) | `/.provider` offers models from the installed provider CLI with the newest as the recommendation, agy variants become effort choices, and the choice can be typed directly (`/.provider codex gpt-6-luna low`) |
| 3.8.2 | [docs/releases/v3.8.2.md](docs/releases/v3.8.2.md) | Closing release before the feature freeze: codex can no longer write through its edit tool in the read-only sandbox; per-provider read/write boundary; provider threads are not silently lost; manifest and artifact scrub fail closed; all temp-and-replace writers share the retry; a dead worker no longer locks the session |
| 3.8.1 | [docs/releases/v3.8.1.md](docs/releases/v3.8.1.md) | Export checks project names at every length and per-project keys must be labels; the gate lets skill Reads through; proven selectors apply across ids on dynamic routes; `tool_commit` and the provider version no longer claim what is not proven |
| 3.8.0 | [docs/releases/v3.8.0.md](docs/releases/v3.8.0.md) | Per-component measured latency, network metadata and proven selectors in `/.verify-browser`, per-task telemetry through the `task-events` hook, `clean` removes guard locks of dead sessions, RQs become YAML records |
| 3.7.3 | [docs/releases/v3.7.3.md](docs/releases/v3.7.3.md) | `/.verify-browser` DELEGATED in every layer; **breaking:** selector `testid` → `e2e`; fixes to drafts, the `repeat_failure` brake, and player polling; literal secrets patched per value; `scenario` origin + structured diagnostics; research becomes schema-backed YAML; CHANGELOG becomes an index; `bench/` removed |
| 3.7.2 | [prompt/v3.7.2/changelog.md](prompt/v3.7.2/changelog.md) | Documentation release, no runtime changes: `docs/research/` (methodology, development and research contract, templates for seven record types), root `CLAUDE.md` for developing this repo, `docs/team-guide/` (team adoption guide: when to use it, task framing, examples, troubleshooting), `docs/README.md` index with the three-layer team guide/research/reference contract, research records (`CASE-001..003`, `SYN-001..002`, `H-001..003`, `EXP-001`, RQ list, period log 2026-09-26), new architecture image `architecture.png` replacing `flow.png` (text version in `docs/architecture/`), verified `docs/architecture/`, limitations and benchmark moved to `docs/limitations.md` and `docs/evaluation/`, telemetry moved out of the README with call/row units separated, README rewritten with per-provider security claims, corrections to outdated documentation |
| 3.7.1 | [prompt/v3.7.1/changelog.md](prompt/v3.7.1/changelog.md) | Closes the 2026-09-24 codebase audit findings on trust boundaries: `/.verify-browser` policy keys become config-only (**breaking**), the OpenCode environment is trimmed to an allowlist + the project `.env` (**breaking**), parity of OpenCode `grep`/`read` secret patterns, the browser read path rejects metadata addresses, owner-token store locks, migration rollback and installer receipts that report honestly, fact recurrence = 5 from a single source |
| 3.7.0 | [prompt/v3.7.0/changelog.md](prompt/v3.7.0/changelog.md) | `/.verify-browser` and evidence tightened: **breaking:** unreadable write bodies are rejected (`uninspectable_body`), DELETE and requests carrying `_method` need per-endpoint approval, confidence capped by the certified-anchor ratio (`anchor_cap`), the repeat brake counts preflight failures; `blocked_requests` fail-closed, local write permission, route templates; per-bucket brake with `source`/`mixed` and `ignore_repeat_brake`; Windows-drive-aware anchors; tasks without argv are uncapped, replaced by `scope_width` |
| 3.6.0 | [prompt/v3.6.0/changelog.md](prompt/v3.6.0/changelog.md) | `/.verify-browser`: verification through a Playwright browser, fail-closed, with a per-project `e2e` section + per-run overrides, headed by default, environment-specific retry, address-based origin guard and write guard (redirects and DNS rebinding included in the assessment), credentials only as key names and `secrets.json` profiles (list), read-only POSTs confirmed with handler evidence, browser knowledge, `data-e2e` tag proposals, and Playwright as the optional extra `--with-e2e`; **`data/` workspace layout** — `.workflow/` holds only user-edited files, internals move to `.workflow/data/`, v3.5.x workspaces are migrated automatically by `upgrade` with backup and rollback, `config.json` becomes overrides only, new `.workflow/current/`; **breaking:** `allowed_mutation_paths` removed, no fallback to the machine-level `config/second_agent.json`, opencode without a model is rejected with `model_unset`, the seed becomes `config/second_agent.seed.json`; opencode prompt via an attached file (`-f`) instead of argv; the installer rejects a dist that does not match the manifest and cleans up old release files; second_agent threads stored per provider; the statusline `~` is no longer turned on by failed calls; Claude Code transcripts are read to measure the premium side; `stamp_version` no longer reads IP addresses as versions; contents briefly recorded as 3.6.1 and 3.7.0 are released here; the first tag since v3.5.1 — 3.5.2 and 3.5.3 were never tagged and their contents are released with this tag |
| 3.5.3 | [prompt/v3.5.3/changelog.md](prompt/v3.5.3/changelog.md) | opencode becomes measured too: the adapter requests `--format json`, reads `step_finish.part.tokens`, and folds `cache.read` into input and `reasoning` into output because opencode reports both as separate addends; stdin is closed because JSON mode hangs without it; `clean_output` assembles the answer from `text` events |
| 3.5.2 | [prompt/v3.5.2/changelog.md](prompt/v3.5.2/changelog.md) | Real tokens from the provider go into usage.jsonl: reasoning and cached are recorded too as a breakdown that is never summed; one row per provider call; codex measured, opencode still estimated with the reason recorded; the global statusline renders those numbers on every prompt, shipped as `workflow-statusline.{ps1,sh}` |
| 3.5.1 | [prompt/v3.5.1/changelog.md](prompt/v3.5.1/changelog.md) | Task cap derived from the provider transport (argv vs stdin) instead of a single constant; codex stdin write failures are no longer swallowed; CRLF digest parsing fix |
| 3.5.0 | [prompt/v3.5.0/changelog.md](prompt/v3.5.0/changelog.md) | Git-tracked knowledge and the `/.promote` that writes it; runner script generator with drift detection; benchmark actually run |
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
