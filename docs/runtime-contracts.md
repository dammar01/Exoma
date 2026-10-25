# Runtime contracts

Which parts of `.workflow/config.json` the Python runtime actually obeys, and which
contracts it structurally cannot enforce. Both lists once lived in the since-removed
`core/workflow_runtime.py` as tuples no code ever read — documentation wearing a data
structure — and were moved here during the folder restructure. Shipped defaults now live
in `core/runtime/config_defaults.py`.

## Keys the runtime reads

config.json holds OVERRIDES only. An absent key in `commands`, `policies` or `e2e` is the
shipped default, applied by the reader; nothing writes defaults into the file, so a default
changed in a later build reaches every project (`config_defaults.merge_config_defaults`,
`effective_section`). The upgrade to layout 2 strips values equal to a default and unknown
or retired keys, and reports each.

The keys below change what this process does. Everything else under `commands` and
`policies` is an instruction to main_agent only: inert here, so renaming one changes
nothing in Python. The list is kept explicit so that "configured" is never mistaken for
"enforced"; defaults and meaning are in
[reference.md, "`.workflow/config.json`"](reference.md#workflowconfigjson).

- `commands.verify_mode` (`delegated` | `syntax`; anything else warns and falls back to `delegated`)
- `commands.verify_test_commands`, `commands.verify_test_timeout_seconds` (`core/evidence/verify_tests.py`)
- `policies.fact_relevant_limit`, `policies.fact_relevant_limits`, `policies.fact_min_relevance`
- `policies.fact_recurrence_threshold`, `policies.fact_max_claim_chars`, `policies.fact_max_facts`
- `policies.enforcement`, `policies.write_allowed_outside_root` (read by the `execution-gate` and `status-guard` hooks, not by Python)
- `policies.prompt_sources` (read by the `intent-gate-set` hook, not by Python)
- `policies.graph_leads_enabled`
- `policies.subagent_fanout_enabled`
- `policies.production_branch`, `policies.knowledge_dir`, `policies.knowledge_relevant_limit` (promote and the knowledge sidecar)

`commands.auto_verify_after_execute` sits between the two: the runtime reads it and echoes
it in `meta.policy`, but nothing in Python acts on it (see the table below). The execute path
itself (plan record, `-y` approval, scope, verification status) is runtime state since 3.9.0 —
see [reference.md, "Plan and execution state"](reference.md#plan-and-execution-state).

`/.verify-browser` reads one section of config.json, `e2e`, as the project's default
settings; everything else about a run travels in a per-session request (see below).

Timeout, stall, and probe settings are **not** here. They are provider settings in the
project's `.workflow/second_agent.json` (`timeout_seconds`, `stall_threshold_seconds`,
`idle_stall_seconds`, `probe_timeout_seconds`, …; defaults in
`config.settings.default_provider_config`), where the adapter and the job manager read them.

## Contracts the runtime cannot enforce

Written down because the absence of enforcement keeps getting read as an oversight to fix
rather than as a property of where the data lives. In every case the runtime never sees
the bytes it would have to check.

| Contract | Why it is unreachable from Python |
| --- | --- |
| `[OPTIONS]` block and the confidence triple in `/.plan` | Appear in main_agent's **output**. Since 3.9.0 their mechanical half is checked when main_agent records the plan (`plan-record`: one to three options, each with a `minus`, exactly one recommended; all three confidence levels; a `proceed` with open questions or `solution_path` below `high` is refused). What the prose says is still unreachable. |
| Per-claim attribution tags | Output-side, never routed back through the runtime. |
| Intent detection without the `/.` prefix | Matches on the **user's** message. No Python path receives one. |
| The words main_agent uses before verify ("done" while `verification: not_run`) | Prose, which no hook reads. The `status-guard` Stop hook reads only the reply's `[EXECUTION RESULT]` fields (`status`, `verification`) and warns when they claim more than not_run; it cannot block a message that has already been written. Opening an execution (plan `proceed` + the user's `-y` token), its file scope, project-root writes and `git commit` approval ARE enforced since 3.9.0 — see [reference.md, "Plan and execution state"](reference.md#plan-and-execution-state). |
| `commands.auto_verify_after_execute` | Whether main_agent chains `/.verify` after an execution is still its own behavior; the runtime records the verdict once a verify runs. |
| `/.verify-browser` interview | The questions happen in main_agent's thread, and only after a draft reports the target unconfigured or unreachable. The runtime only sees the request file written afterwards. |
| Proxy-failure hard gate | Stopping, printing `[PROXY GAGAL]`, and waiting for a yes/no before any local fallback is main_agent behavior after it reads `ok:false`. The runtime returns the failure; it cannot stop main_agent from gathering on its own. |
| Output contracts (RELAY vs SYNTHESIS, mandatory fields) | Output-side, like `[OPTIONS]`. |
| Main agent leaves `graphify update` to the Stop hook, and reads the graph as stale while `graphify-out/.refresh.lock` is held | Main agent runs graphify and reads graph.json through its own tools. The runtime honours the lock only for its own reads (`core.graph.graph_index.load_graph`). |

One prompt contract is enforced **partly outside Python**: the pre-flight gate for
delegated commands. It is prompt-level in `CLAUDE.md`, and in auto-intent mode, once the user
names a delegated command explicitly (`/.analyze ...` or the slash-command payload; a prompt
that only resembles one under the NL map gets an `[INTENT HINT]` instead), the
`intent-gate-set` / `intent-gate-check` hooks in Claude Code block gather tools (including
the `PowerShell` tool, and writes to the runner scripts) until `.workflow/run` dispatches;
a Bash or PowerShell call passes only when one shared tokeniser reads it as one plain call to
`[bash] <root>/.workflow/{run,check,inspect}.sh` whose every word is bare or single-quoted —
literal in both shells, so `$`, `;`, `|`, `&`, redirects and newlines pass only inside single
quotes, and double quotes, `bash -c` and the retired `run.ps1` do not; a leading
`cd <dir inside the root> && ` is allowed — and a Read (or a one-file `cat`) passes when it
names an existing `.md` whose real path lies inside `~/.claude/skills` (the skill that says
how to dispatch; `~` and `/c/...` spellings resolved first), a background task's
`<temp>/claude.../tasks/<id>.output`, or is a precision read: a project file outside `.workflow/`, at
most 200 lines (`limit` ≤ 200, or a file that short), two per marker, counted in
`runtime/delegated.reads` keyed to the marker's `set_at`. That enforcement lives in the agent host, not in this runtime,
and it fails open. The hooks are the same on every OS: `<hook>.sh` is a bash launcher (Git Bash
on Windows, named by absolute path in `settings.json`, which also sets
`env.CLAUDE_CODE_GIT_BASH_PATH`) that runs the Python pinned in `~/.claude/hooks/python.path`
(else resolves a Python 3.10+ by running it) and execs `workflow_hooks.py <hook>`, which reads
the payload from stdin as UTF-8 and runs `<hook>.py` with it in memory — never through the
environment, which Windows caps at 32767 characters per variable; no `.ps1` ships. The full prompt-layer list, with what each contract requires, is in
[reference.md, "Kontrak lapisan prompt"](reference.md#kontrak-lapisan-prompt).

What *is* checkable is the second agent's output, because it comes back through the
runtime: see `core.evidence.contract.contract_warnings`. Those are reported, never fatal.

The `execution-gate` hook records its own refusals: one line per block or warning in the
bound session's `runtime/gate-blocks.jsonl` (rule, tool, mode, exit code, plan id; never the
path or command), fail-open. The runtime does not read it; it counts how often
the gate fires, and whether a block was legitimate is still judged from the transcript.

## /.verify-browser

`--command verify-browser` is a **delegated** command: listed in the DELEGATED registry of
the shipped `CLAUDE.md` and in `intent-map.json`, so the pre-flight gate applies to it as it
does to `verify`. Division of work: the secondary agent drafts the spec (stage 1) and reviews
the browser evidence (stage 3); the browser itself is driven by the **runtime player**, a
child process this runtime starts (stage 2). Neither main_agent nor the secondary agent
(read-only sandbox) runs the browser.

It is a background command like `verify`, dispatched from
`Executor.execute()` to `core/evidence/e2e/runner.py` under the one runtime lock the call
holds. Stages that need a provider go through `Executor._run_delegated` directly — never
back through `execute()`, whose fact ingest, evidence indexing and fan-out bookkeeping
belong to public commands.

The request is `.workflow/data/sessions/<session>/e2e/request.json` (`.workflow/sessions/…` in a workspace not yet on layout 2)
(`core/evidence/e2e/request.py`):

```json
{"version": 1, "phase": "draft" | "run", "settings": {...},
 "scenario": {...}, "existing_tests": [...], "spec_notes": [...]}
```

Settings resolve in three layers, each overriding the one before it: the shipped defaults
in `default_settings()`, the project's `e2e` section in `.workflow/config.json`
(`request.config_settings()`), then the request's own `settings`. The middle layer is what
lets a configured project run without being interviewed again. The two layers are validated
differently on purpose: a malformed value in `config.json` is a WARNING that falls back to
the shipped default (named in `meta.e2e.config_warnings`), because config.json is shared by
every command and one typo there must not stop work that never reads it; the same value in a
request is `request_invalid`, because the user confirmed it for this run.

Five keys skip the request layer entirely (`CONFIG_ONLY_SETTINGS`): `allow_remote`,
`allowed_origins`, `existing_test_command`, `existing_test_allowlist`,
`existing_test_timeout_s`. They are policy over the request — which origins a run may reach
off loopback, and which argv the runtime executes with the user's environment — and the
request is written by an agent, so a request able to set them would authorise itself. They
resolve from the defaults and `config.json` only; a request naming one is `request_invalid`
with a message pointing at `config.json`, never a silently dropped value. `base_url` stays a
request key: a run may pick its target, and that target is judged by the config's origin
policy.

`settings` keys (defaults are smoke-test starting points, not calibrated limits):
`base_url`, `browser`, `headless`, `slow_mo_ms` (pause after each browser action for a
watchable headed run; 0..5000, counts against `total_timeout_s`), `nav_timeout_ms`,
`step_timeout_ms`, `idle_timeout_s`, `total_timeout_s`, `probe_max_elements`,
`allow_remote` and `allowed_origins` (config-only), `allow_side_effects` (judged by the addresses the write
hosts resolve to; see below), `allow_local_side_effects` (default `true`: non-destructive
writes to loopback, `*.localhost` and `.test` need no further opt-in; see below),
`allowed_read_only_requests` (`"POST /path"` or `"POST http(s)://host/path"`,
exact path, no wildcard or query, at most 20; see below), `allowed_destructive_requests`
(`"DELETE /path"` or a full URL, route templates such as `/api/items/:id` allowed,
wildcards are not, at most 20; see below), `secrets_profile` (a
`secrets.json` profile name; empty = the file's `default`), `secrets_template_profiles`
(profile names a newly created `secrets.json` gets; at most 20, unique),
`fail_on_console_error`, `max_retries` (how many extra browsers one run may start; see
below), `ignore_repeat_brake` (see below), `keep_created_data` (default `false`; with
`allow_side_effects`, lets a `creates_test_data` step carry `no_cleanup_reason` instead of a
cleanup, reported as cleanup `not_planned`), `artifact_max_mb`, `existing_test_command` (config-only; an argv template run
without a shell; `{files}` and `{base_url}` expand), `existing_test_allowlist` and
`existing_test_timeout_s` (both config-only). An unknown key or a wrong type is `request_invalid`, not a silent
fallback; `allowed_mutation_paths`, removed, is `request_invalid` with a message naming
what replaced it. Hybrid review is not a setting: it always runs.

| Phase | Stage | Command / route | Reply contract | Ends when |
| --- | --- | --- | --- | --- |
| both | request | — | — | missing or malformed → `incomplete: request_missing | request_invalid` (not recorded) |
| both | preflight | — | — | `base_url` policy, write policy (`allow_side_effects` with a write host that is not loopback or `.test` and resolves public, link-local or nowhere → `spec_invalid`), Playwright, browser, or URL reachability fails → draft `blocked`, run `incomplete` with that reason |
| draft | 1 | `e2e_spec` (internal route, role exploration; refused by `main.run()` and absent from the CLI) | standard `[EVIDENCE]` … `[DIGEST]` with an `[E2E SPEC]` section inside | a proxy failure (returned as-is); otherwise always — `[E2E DRAFT]` with status `ready` or `invalid` (spec missing after one targeted continuation, still invalid after one repair, refused by policy, ungrounded) |
| run | spec | the request's `scenario` | — | refused by validation → `incomplete: spec_invalid`; malformed secrets.json, an unknown profile, or several profiles and none selected → `secrets_invalid`; an unresolved `${ENV}` → `env_missing` |
| run | existing tests | `config.json e2e.existing_test_command` over allow-listed files | exit code | never by itself — covered claims become proven (exit 0) or `unknown` (anything else) |
| run | 2 | runtime player child process (`python -m core.evidence.e2e.player` → `browser.run_scenario`) | JSONL events (`progress`, `request`, `cleanup`, `observation`, `artifact`, `harness`, `heartbeat`, `result`; shapes in `classify.py`) | never by itself — its report decides; skipped when the scenario has no steps; retried under the rules below |
| run | 3 | `verify` with an `[E2E EVIDENCE]` block in the prompt | ordinary `[VERIFICATION]` | skipped when the browser could not finish; a failed review is a declared gap, never a softer verdict |
| run | normalise | — | canonical `[VERIFICATION]` on `result.content`; `meta.verdict` set before `_finalize_verify_result` | — |

Stage 1 gets at most two follow-ups in the same provider thread, each at most once. A reply
with evidence and no `[E2E SPEC]` gets the targeted continuation (`spec.spec_continuation_prompt`).
A section that arrived but did not validate — the scenario outside its fence, invalid JSON, a
value outside an enum such as `side_effect`, a retired selector key, a policy or grounding
refusal — gets one repair (`spec.spec_repair_prompt`) quoting the validator's messages (at
most `MAX_REPAIR_ERRORS`) and asking for the complete corrected section; the repair reply
replaces the first section and is validated from scratch. Still invalid after that, the
draft is `invalid`. `meta.e2e.repair` records `{attempted, errors, recovered}`; without a
`provider_session_id` nothing is asked and it says `reason: no_provider_session`. Before this,
every section that arrived broken ended the draft on its first attempt, which is the shape
first drafts most often have.

Origin and write policy are two different questions, answered by two different things
(`core/evidence/e2e/preflight.py`). WHERE a run may point is about names: a loopback host, a
`.test` name (reserved for local development by RFC 6761, so it cannot be a public host, and
therefore needs no `allow_remote`), or an origin listed in `allowed_origins` behind
`allow_remote`. Crossing to any other origin mid-run still needs that origin listed,
whatever class base_url belongs to.

WHAT may be written to treats loopback and `.test` alike (`is_local_dev_host`): both are
local development by definition, take writes with no lookup, and get no pin — the one thing
that can still point a `.test` name elsewhere is the user's own resolver. Every other write
host is judged by address: with `allow_side_effects` true, `write_network()` resolves
base_url's host and every allow-listed origin's, and refuses the run (`spec_invalid`) if any
of them has a public address, a link-local one (169.254.0.0/16 is the cloud metadata
service), or none at all. A name that resolves to both private and public addresses is
refused on the public one. Each approved name is pinned to the address it resolved to —
including a name that resolved to loopback, such as `127.0.0.1.nip.io`, whose owner can
answer differently on the browser's own lookup; only a loopback NAME is left unpinned. The
approved names and pins travel to the player as `write_hosts` and `host_pins`; chromium is
launched with `--host-resolver-rules` (an IPv6 pin in brackets) so a name cannot resolve
elsewhere between the check and the write, and a browser whose resolver cannot be pinned is
refused a pinned write for exactly that reason. The guard never resolves anything itself —
asking DNS a second time is what rebinding exists to exploit.

Redirects never reach the route handler: Chromium follows them itself (measured against
Playwright 1.60 — a POST answered 307 re-sent its body to the new address unseen). So an
approved write is not continued but fetched by the guard with `route.fetch(max_redirects=0)`.
A 307/308, which keeps the method and body, is judged by the same write rule before each hop
is sent (at most `browser.MAX_WRITE_REDIRECTS`); a 301/302/303 is handed back to the browser
as a GET once its target passes the navigation policy; a refused hop aborts the request and
enters the ledger as blocked with the reason. `route.fetch` runs outside Chromium's resolver
rules, so a pinned host over http is fetched at its pinned address with its name in `Host`,
and a pinned host over https — which cannot be held to an address that way — is refused
unsent. A TOP-LEVEL navigation redirected off policy cannot be stopped this way without
breaking the page URL: it is detected from the `request` event after the browser followed it
and fails the step as `navigation_blocked`, saying so.

Tag proposals (`core/evidence/e2e/tagging.py`): after a run, every step that PASSED whose
winning selector had provenance `source` and a `selector_provenance.ref` of `path:line`
becomes a proposal to add `data-e2e="<step id>"` to that line. Each is validated against the
repository as it is — inside the project root, a template suffix, not git-ignored, the line
exists, holds exactly ONE opening tag and no commented-out markup (a line addresses a line,
not an element, so `<div><button>` is refused rather than resolved as the div), and does not
already carry the attribute — and the whole
plan, `ready` and `skipped` with reasons, is written to `tag-proposals.json` and summarised
in `meta.e2e.tags`. The runtime never edits a template: the user confirms the batch and the
main agent applies `old_line` → `new_line`. `apply()` re-checks both the line AND the path: the
plan was made before the user was asked, which is time enough for the path to become a symlink
out of the project. Selector VALUES never enter the event stream resolved: `selection` carries
key names only, and what a step expected (`selector`, `text`, readiness `unmet`, `contains` /
`equals` / `matches`) is reported from the PLACEHOLDER scenario the runner hands the player
as `display_scenario`, since a value can be a resolved credential shorter than
`redact.MIN_SCRUB_CHARS` and therefore unscrubbable. Elements found by
heuristic or by probing the live DOM are never proposed — there is no map from a runtime
selector back to the template that rendered it, and the rendered DOM is framework output,
not source. `knowledge_claims()` turns applied tags into anchored claims for `/.promote`.

Retry (stage 2 only, `runner.RETRYABLE_REASONS`): a run that ends `incomplete` for an
environmental reason — `harness_error`, `unknown_origin`, `stuck`, `timeout`,
`output_truncated`, `launch_failed` — starts the browser again, up to `settings.max_retries`
(default 2, hard ceiling `runner.MAX_RETRIES`). Two consecutive attempts whose verdict,
reason and per-step outcomes all match stop the loop and are marked `stable`: the run is
reproducible, not flaky, which is the one thing a retry was there to establish. Never
retried: a `fail` verdict (the application broke the claim, and re-rolling a real bug until
it hides is what this package exists to prevent), the reasons a rerun cannot change
(`playwright_missing`, `browser_missing`, `base_url_unreachable`, `spec_invalid`,
`env_missing`, `player_unavailable`, `scenario_error`), and any run with `allow_side_effects` true — its
first attempt's write may already have reached the server. Attempt 1 owns the run's `e2e/`
directory; each retry writes to `e2e/retry<n>/`, and `meta.e2e.attempts` lists them. One
`artifact_max_mb` budget covers the run directory and every `retry<n>/` together.

That loop lives inside one invocation. Across invocations there is a second brake, keyed on
origin and failure BUCKET in the session state (`state.note_e2e_outcome`): three runs in a
row against the same origin ending the same way reach the limit, and a run is refused before
a browser starts, `incomplete` with reason `repeat_failure` and the streak in
`meta.e2e.repeat`. Which run that is depends on the gate that counted the third. A browser
run is counted after its report, so the one refused is the NEXT invocation. A preflight
failure is counted at the gate and read straight back, so the invocation that writes the
third streak is itself the one refused — not a fourth. It is checked twice, in the two places a limit can be reached: at the
preflight gate, which is where a repeating environment failure actually lives, and again
once preflight has PASSED, before anything starts a browser. It used to be checked before all of them, which cost it
the only evidence that the counted problem was gone: three runs with Playwright missing
reach the limit, the user installs Playwright, and the fourth is refused before preflight
can notice. The streak is cleared by a run that passes, and the brake was standing in
front of the run that would have cleared it. The bucket is one of three (`classify.repeat_bucket`): `timeout` for a
run that ran out of time, `harness` for one the environment stopped, `app` for one the
application failed. Coarse, because one obstacle answers to several reasons in turn —
`base_url_unreachable`, then `stuck`, then `harness_error` — and an exact-reason key started
a fresh streak of one at every rename. The three rungs are three different things to do
next: wait or raise a limit; fix the environment; fix the application. An app failure has no
`reason` of its own — the failing app IS the verdict — so keyed on reason it was the one
outcome that could never accumulate, which is the outcome most worth braking on. The latest
reason still travels in the record as the detail the refusal quotes back.

Since 3.7.3 the `app` bucket is narrowed by a failure signature
(`classify.failure_signature`, versioned `v1:<digest>`). An `app` streak continues
only when the new failure has the same signature: the same page (`url_after` through
`sanitize_endpoint`, query and fragment dropped, identifier segments folded to `:id`), the
same action, the same field (the step's `selector`/`selector_candidates`/`within` as the
placeholder scenario wrote them), and the same condition (the error kind, the step's
`contains`/`equals`/`matches`/`text`/`value` except on `fill`, and the unmet readiness
conditions). It excludes the step id, so renaming a step is not a fix, and the free-text
detail, so a timestamp in a message does not make every run new. A `fail` with no failed
step is signed by the run's reason. Different app failures on one origin are different
things to fix, not one loop: keyed on the bucket alone, a login form that had not rendered, a
redirect the runtime itself held back, and that redirect again became one streak, and the
fourth run was refused. `timeout` and `harness` carry no signature and stay coarse, for the
reason above. An `app` record written before signatures existed (no `signature` field) is
read as no streak.

Every counted outcome also records `runtime`, the runtime's own identity
(`runner.runtime_identity()`: `TOOL_VERSION` plus a digest of `core/evidence/e2e/*.py`). A
streak at the limit written by a different runtime is released before braking, with
`meta.e2e.repeat_released: {by: runtime_changed, …}`. The project fingerprint below cannot see
the runtime, and a fix made during development does not bump the version. A record without
`runtime` says nothing either way and is not released by this rule.

A run is counted at two points, not one. After a browser report, keyed on that report's own
verdict and reason; and at the preflight gate, which a refused run never gets past — an
outcome written only after a browser report left `base_url_unreachable` and
`playwright_missing` counting for nothing, however many times the same missing dependency
stopped the same run, so the streak stayed at zero and the brake never fired against the
shape it was built for. By reason, not by "preflight failed": `runner._REPEATABLE_PREFLIGHT`
is `playwright_missing`, `browser_missing`, `base_url_unreachable` — what the environment
owns. Everything else is the caller's own input, including the `spec_invalid` that preflight
itself returns for a base-URL or write policy the request named, and `secrets_invalid` and
`env_missing` from the gates above it. Coming back with those corrected is the fix working
rather than a loop, so braking there would lock someone out on the third attempt at a
password and leave a new session as the only way to try a fourth. A draft is never counted —
it starts no browser, so repeating it costs nothing — and neither is `repeat_failure` itself,
which would extend the streak it has just reported. Nor is a browser run whose every
undecided failure was the scenario's own (`scenario_error`, `classify.NOT_COUNTED_REASONS`):
a guessed selector that matched nothing or too much, a write or navigation the run's own
policy refused. The next run is a different scenario, so it is not a repeat, and it neither
adds to a standing streak nor clears one — a scenario typo must not erase a streak of real
environment failures behind it (`meta.e2e.repeat_not_counted`).
For the environment buckets: bucket rather than step outcomes, deliberately — the loop this catches
edits the scenario between attempts, so a step-level key would reset every time and count to
one forever. What twelve such runs have in common is never the steps; it is the environment,
the data or the credentials underneath them. A `pass` clears the streak, and so does a new
session. So does a change to the project: every counted failure stores the project's
fingerprint (`fingerprint.project_fingerprint` — HEAD, the tracked diff and the untracked,
non-ignored files, all outside `.workflow/`), the newest one kept. Before braking, a streak
at the limit is compared with the project as it is now; a different fingerprint means the
application changed since the last failure, the record is cleared, and the run goes ahead
with `meta.e2e.repeat_released: {by: change_detected, …}`. Scenario and request edits live
under `.workflow/` and never change it — that loop is still braked. Outside Git there is no
fingerprint and this release never fires. When the brake refuses, the record is also written
to the browser knowledge store as a `repeat_failure` entry (origin, bucket, reason, streak,
fingerprint, signature — one entry per distinct app signature), offered first to every later draft for that origin, in any session; a passing
run against the origin retires it (`meta.e2e.repeat_failures_resolved`). A hint, never a
gate. So does a preflight that passes, for a streak preflight itself
wrote — one whose recorded `source` is `preflight`. Neither the bucket nor the reason can
carry that. Not the bucket, because `harness` and `timeout` are where a browser run’s own
incompletes land too, so clearing by bucket retired a streak of real timeouts every time
preflight managed to reach the host, which it does on every run. Not the reason either,
because the player emits `playwright_missing` and `browser_missing` of its own when a
launch fails — the same words preflight uses. So the gate that wrote the streak is
recorded on it. A streak both gates wrote is `mixed` and is nobody’s to retire, as is a
record from a build before the field existed; what clears those is a run that passes.
Preflight is evidence about what preflight wrote and nothing else. A streak already at the limit is never overwritten either — an
`app` streak used to be replaced by the first `playwright_missing` that came along, bucket
`harness` and the count back at one, so a missing dependency retired the brake that was
holding a broken application at bay. What clears `app` is the run passing, or
`settings.ignore_repeat_brake`, set by someone who has fixed the application and is saying
so. Off by default, and the refusal names it. Pinned in a project’s `config.json` it is
not one run but every run, which is a project deciding it does not want this brake. A run that
PASSES only on a later attempt is not a clean pass: the verdict carries a gap naming the
reasons the earlier attempts ended on (`passed only on attempt N of N`), so it is
`incomplete` — `timeout` and `not_ready` are also what an intermittently broken app looks like.

Headed is the default. On a Linux machine with neither `DISPLAY` nor `WAYLAND_DISPLAY` set
(CI, a container, SSH) a headed run is launched headless instead, with a
`meta.e2e.config_warnings` entry; Windows and macOS always have a session to open a window in.

A job running `verify-browser` whose worker dies is not recovered: recovery would replay the
whole scenario, writes included, on top of what the dead attempt already sent. The worker
fails the job with `worker_died`, `meta.reason: not_recoverable`, and releases the lock.

A draft's `meta.command` is `verify-browser`: it carries no verdict, exits like any
non-verify command, is finalised under its own name (never counted as a verification),
and writes `draft.json` beside the request for the run to copy. A run's `meta.command` is
`verify` with `meta.invocation: verify-browser`, so its verdict, exit code, and acceptance
metrics are the ones every verification uses.

Credentials: `${NAME}` placeholders resolve from one profile of `.workflow/e2e/secrets.json`,
then from the process environment. The names are a registry in code
(`core/evidence/e2e/request.py` `CREDENTIAL_KEYS`: `E2E_USER`, `E2E_PASS`); another
credential is a new registry entry, never a name a scenario or second_agent invents:

```json
{"default": "qa",
 "profiles": [{"name": "qa", "credentials": {"E2E_USER": "...", "E2E_PASS": "..."}},
              {"name": "admin", "credentials": {"E2E_USER": "...", "E2E_PASS": "..."}}]}
```

`profiles` is a list of uniquely named profiles; `credentials` holds registered names with
string values; `default` names a listed profile. The pre-list object shape
(`"profiles": {"qa": {...}}`) is `secrets_invalid` with the new shape in the message — it
is never read and never converted. The request selects a profile by name
(`settings.secrets_profile`); empty means the file's `default`, or its only profile. Several
profiles with neither is `secrets_invalid`, never a guess — trying another account is
another run with another name. An empty string is an unfilled slot, not an empty
credential. When a draft finds a placeholder unset and the file does not exist, the draft
creates it from code — the registry's names, empty, in one profile per
`secrets_template_profiles` name (the selected profile first; `default` when none), with a
JSON serializer — and says so. Creation writes a private temporary file and publishes it
with a hard link, which fails if the name exists, so an existing file, one saved meanwhile,
or one a concurrent draft created first is never rewritten. Errors name a profile, a
credential position, or a JSON line — never a value, and never a key that failed validation
(a value pasted into the key slot is what that error reports), except that a registered
name in the wrong case is pointed at its spelling. The process environment is consulted for
registered names only. The whole selected profile reaches the existing-test command's
environment and is scrubbed from its output. The resolved values reach the player only
inside the scenario on stdin. A resolved value shorter than the scrubber's 4-character
minimum cannot be mapped back to its placeholder, so that run keeps no page HTML
(`meta.e2e.secrets.unscrubbable` names the credential); structured events are still
scrubbed only for longer values.

Literal credentials in agent-written input. The task, the stage-1 draft, and the request
are written by agents, and a credential sometimes arrives typed out instead of as
`${NAME}`. Before anything else uses them, the runner looks each one up against the same
lookup the player resolves from (the selected profile plus registered environment names)
and puts the placeholder back (`core/evidence/e2e/redact.py` `scrub_literals`; the
page-echo scrubber `scrub_resolved` likewise leaves `step_id`, `id`, `claim_id`, `cleans`,
`action`, `type`, `kind` and `after_step` in player events untouched): a string
that IS a value of 4 or more characters is replaced wherever it sits; a value inside a
longer string only when it stands as its own token and is at least 8 characters for a
letters-only value, or at least 4 for any other value (digits, `@`, symbols) (not
glued to a letter, digit, `-` or `_`), so a credential that is an ordinary word never
rewrites an identifier such as the step id `fill-password`; structural fields (`id`,
`claim_id`, `cleans`, `action`, `side_effect`, `severity`, `version`, `covers`, `kind`,
`type`) are never rewritten at all, and in selector fields (`css`, `text`, `label`, `name`,
`role`, `e2e`) a value made only of letters (a plain word) is matched only as the whole
text, while any other value (an email, digits, symbols) is still found as a token inside it; a value under 4 characters only when it is
the whole of a `value` (or `password`/`secret`/`token`) field — what a `fill` types — and
never in an enum, an id, or a key. Keys are never rewritten. The task is scrubbed before the stage-1 and
stage-3 prompts are built, the draft after parsing and before validation and `draft.json`,
and `request.json` is rewritten in place on disk. The run continues — the placeholder
resolves back to the same value for the player — and is flagged: `meta.e2e.secret_literals`
maps `task` | `draft` | `request` to the credential names found, and a `config_warnings`
line says the same. Values never appear in either. Independently of the lookup, a `fill`
into a field whose selector names a password (`password`, `passwd`, `passcode`, `pwd`) must
carry a placeholder; a literal there is `spec_invalid`, which catches a password the lookup
does not know yet. Not covered: the raw provider reply archived before parsing
(`response.last.md`, `output.raw.md`), job records that store the task before the runner
sees it, and a value shorter than 4 characters anywhere but a whole `value` field.

`[E2E SPEC]` sections: `claims` (`id | severity | description | source_refs`),
`existing_tests` (`path | covers | confidence`), `coverage_gap`, `scenario_json` (a
fenced JSON scenario), `spec_uncertainties`. Validation runs in both phases, before any
`${ENV}` value is resolved or any browser exists (`core/evidence/e2e/spec.py`):

- actions from the allowlist `goto click fill select press wait_dom expect_dom
  expect_url expect_title probe`; every assertion names a `claim_id`; every claim is
  asserted at least once unless an existing test that will actually run covers it (a
  scenario may then have no steps at all);
- every claim carries `source_refs`: `path[:line[-line]]` naming a file and line that
  exist in the project, or `req:<id>`;
- `goto` targets are relative paths under `base_url`, or absolute URLs on its origin or in
  `allowed_origins` (a remote origin also needs `allow_remote`; a `.test` origin does not);
  backslashes, whitespace and non-http schemes are refused. The player applies the same rule
  at runtime to top-level navigations: a click that leaves the origin is blocked before the
  request goes out, and a redirect that does is detected after the browser followed it —
  both fail the step as `navigation_blocked` (origin `scenario`);
- every step and cleanup step has an `id` (kebab, unique across both lists); events, the
  request ledger and the report refer to it;
- a step that changes data declares `side_effect` (`creates_test_data`,
  `modifies_test_data`, `deletes_test_data`), `test_environment_required: true`,
  `test_data.marker` (the value that tells this run's data apart), optionally `request`
  (`{"method": POST|PUT|PATCH|DELETE, "path": "/items/:id"}`, `:name` matching one segment),
  and is still refused unless `allow_side_effects` is true. A descriptive `cleanup` string
  on the step is refused with the new shape named. Cleanup is the scenario's `cleanup`
  list: executable steps under the same rules, each `cleans: <step id>` naming a
  side-effect step, each group ending in at least one assertion (no `claim_id`: it proves
  the cleanup, not a claim). Created data must have a cleanup group; modified or deleted data
  needs one or a `no_cleanup_reason`. That declaration is the scenario's own word, so the
  player does not rely on it. While `allow_side_effects` is true a request whose method is
  not `GET`, `HEAD` or `OPTIONS` passes only when the request's own host is loopback
  (`localhost`, `*.localhost`, 127.0.0.0/8, `::1`), a `.test` name, or a host preflight
  approved and pinned — this catches an API on a remote host called from a local page, and
  its redirects are followed by the guard (above). While it is false a `POST`, `PUT` or
  `PATCH` — those three and nothing else, `spec.WRITE_METHODS` less `DELETE` — to a local
  development host still passes when `allow_local_side_effects` is true
  (the default): one global switch used to make `localhost:8000` and a production host the
  same decision, so proving a form saves required the permission to write anywhere
  preflight approved. A verb outside that list (`PURGE`, `PROPFIND`, anything a stack
  invents) is refused here however local the host: the permission was granted for ordinary
  form writes, and being local is not a reason to send a method nobody named.
  `allow_side_effects`, the wider opt-in, is unchanged — it was never a list of verbs.
  Every other host is aborted, on any origin (an API on another
  port is still the app's data).
  `DELETE` is outside all of that. It passes only when its endpoint is named in
  `allowed_destructive_requests`, whatever the other switches say: the runtime cannot tell
  a record the scenario created from one that was already there, and a local database still
  holds work that was not this run's to remove. So does any request CARRYING a `_method`
  override field, whatever the field says (`spec.carries_method_override`, refusal
  `override_unapproved`): a request holding that field is not the method it is spelled as,
  and which value a given framework honours, on which verbs, after how many layers of
  decoding, is not knowable from the browser side. The value is therefore not read. An
  earlier design did read it and ranked what it meant; six verification rounds each closed
  a real hole in that ranking and each left a deeper one — a demoted value, an escaped key,
  a late multipart part, a duplicated field, a doubly encoded name, a UTF-16 body — because
  the premise, not the code, was wrong. The trade is explicit: a POST whose form happens to
  carry the field, or a body that merely spells it, is refused with the endpoint named. The
  question is asked before the safe-method exit, over the query, body and headers
  (`X-HTTP-Method-Override`, `X-Method-Override`), against every decoding a backend might
  apply — percent (including doubled), JSON `\u` escapes, multipart envelopes and UTF-16.
  A multipart body is read through `post_data_buffer` when `post_data` refuses it, which
  keeps ordinary uploads working; a body neither accessor can produce is refused under
  every permission, including `allow_side_effects`, and reported as a `write_uninspected`
  observation naming the endpoint. It used to be sent under that switch and reported
  afterwards, which put the report on the wrong side of the request: an envelope carrying
  `_method=DELETE` had already landed by the time anyone read the observation, and one
  endpoint at a time is what a delete is approved by everywhere else in this guard. There is
  nothing to turn on: `allowed_destructive_requests` approves DELETEs, and a POST whose
  verb cannot be confirmed never reaches that rule, so a refusal pointing there would name
  a setting that cannot help. A scenario that must upload runs through the project's own
  `existing_test_command` instead.
  Approvals and refusals both live in `.workflow/e2e/permissions.json`, beside
  `secrets.json` and for the same reason — both answer what a run may do here, and both
  are the project's standing answer rather than one run's. Its
  `allowed_destructive_requests` are merged into whatever the request also approves, so an
  endpoint agreed to once is never asked about again. Its `blocked_requests`
  (`<METHOD|*> <endpoint>`, route templates allowed) are the opposite: read from this file
  only, never a settings key, checked before the safe-method exit and before every
  permission below it, and winning over `allow_side_effects` and an approval alike
  (`blocked_by_policy`). It is checked again at every body-keeping redirect hop, because a
  307/308 is fetched by the guard rather than issued by the browser and so never returns
  through the route handler — a write to an endpoint nobody denied, answered
  `307 → /api/payments`, used to land on the one endpoint the project had named. A
  301/302/303 is re-issued by the browser as a fresh request, which meets the list at
  `guard` like any other. A deny list a run could extend is one a run could also shorten.

  The list governs NAVIGATION as well as writes, through `Session._navigation_refusal`,
  which asks the two policies that judge an address: `navigation_error` for the origin —
  whether this run may go there at all — and the deny list for the endpoint. Only the first
  was ever asked of a navigation, so a redirect from an allowed origin onto a denied path of
  that same origin satisfied both checks at once, the origin policy having nothing to object
  to and the list's only reader never seeing a GET. It is asked at `goto`, where the step is
  refused before anything is sent; at `guard`, where a top-level navigation is aborted; and
  at `page.on("framenavigated")`, which reads the address bar after every change — a
  `history.pushState`, a meta refresh or a redirect Chromium followed internally moves the
  page without producing a request to attribute, and the address bar is what the policy is
  about. A landing caught after the fact fails the step that caused it, naming the policy
  that refused it in `blocked_detail`; the same address reported twice within one step is
  one failure, while reaching it again under a later step is that step's own. GET and `*`
  entries only: `DELETE /api/users/:id` says not to delete that record, and opening the
  address is a read.

  A step fails on what its own window caught, by comparing a counter before and after the
  action, so a navigation that action merely scheduled — a debounced `pushState`, a redirect
  still being followed — arrives after the comparison and belongs to no step.
  `report_unattributed_navigation` emits those at the end of the run as
  `{"type": "harness", "reason": "navigation_blocked"}`, which is in `INCOMPLETE_REASONS`:
  the verdict is `incomplete`, never `pass`. Harness rather than an application failure —
  the application did nothing wrong, the run went where it may not go, and a run that went
  off policy has proven nothing whatever its assertions saw. Attribution is by position in
  `blocked`, not by address: the same address can be reached twice, and a URL key let a
  later step's claim cover an earlier landing nobody owned.

  A cleanup step claims nothing either. Its failures are reported as `cleanup` events, which
  the classifier reads as housekeeping and never lets decide a verdict — right for a delete
  that could not be undone, wrong for a policy refusal, since a cleanup that reached a denied
  endpoint reached it exactly as a test step would have. So a cleanup-phase violation is left
  unclaimed and surfaces at the run level like any other.

  A malformed `permissions.json` is not a run with a shorter deny list. `load_permissions`
  returns `(permissions, warnings, errors)`, and the two keys are held to opposite
  standards, because dropping them does opposite things. Dropping an approval leaves the run
  stricter than asked, so it warns and falls back, the same posture as a config.json knob.
  Dropping a deny entry leaves the run free to touch what the project named, so it is an
  error and `load_request` refuses the run (`request_invalid`): a file present but
  unreadable, a top level that is not an object, a `blocked_requests` that is not a list, or
  any entry in it that does not parse. Each bad entry is named individually — one of them
  used to take the whole list with it, so `{"blocked_requests": ["* /api/payments", "bad"]}`
  loaded as no deny list at all, one warning among the run's others, and the run proceeded.
  An absent file is still no permissions and no problem.
  A read is included deliberately: a GET can fire a mailer or bill per call, and this list
  is where a project names the endpoints no run should touch at all. A refused delete is
  reported as `meta.e2e.destructive_pending` — `{method, endpoint, entry}`, the endpoint in
  route form — which is a question for the user, not a verdict; the runtime never grants it.
  A POST that only reads (a search, a GraphQL query) may pass as a confirmed read:
  stage 1 proposes it under `read_only_requests` (`method | endpoint | source_refs | reason`,
  the handler's `path:line` required and grounded, `req:` refused, an ungrounded proposal
  makes the draft `invalid`), the draft lists it, the user confirms, and only an entry the
  request's `allowed_read_only_requests` names is admitted. A proposal never writes itself
  into settings. Even then the body is read: a GraphQL `mutation` or `subscription` (JSON,
  batch, form `query=`, or raw), a persisted query whose operation is only a hash, or a body
  that cannot be read as text is still refused, with the reason in the step's detail. String
  literals and comments are blanked before that check; a field literally named `mutation`
  taking arguments is refused too. Admitted reads become one `read_only_request_allowed`
  observation per method and origin, with a count — a warning-class record, never an app
  error. A refused write inside a step fails that step as `mutation_blocked` (origin
  `scenario`, so the run is `incomplete: scenario_error`), whatever the step saw after it; a refused beacon (`ping`)
  or a write fired outside any step becomes a `mutation_blocked` warning observation.
  Those records keep the method and origin only, never path, query or body. The guard is
  method-only: a `GET` that mutates, a WebSocket message, or a service worker's own
  request is not seen;
- every non-GET/HEAD/OPTIONS request the guard sees, sent or refused, becomes one `request`
  event: id, `phase` (`steps` | `cleanup`), `step_id` when it was sent inside that step's
  window (readiness wait plus action), otherwise `attribution: uncertain` with `after_step`
  — never pinned to the nearest step — method, `endpoint` sanitised (scheme, host, port,
  path; query, fragment and credentials dropped; `;` path parameters dropped; numeric, UUID, long-hex, long-token, JWT,
  mixed-case-with-digits token, 16+ alphanumeric run without separator, 16+ balanced
  mixed-case and `@` segments become `:id`), resource type, `read_only`,
  `blocked`, `planned` (the step's `request` matched; `false` for an extra write; `null` when
  unattributed or a confirmed read), `status` from the
  response event, `failure`, `duration_ms` from request to `requestfinished` /
  `requestfailed`. A write still waiting when the run ends is reported with
  `no response before the run ended`, including when the player crashes. Bodies, cookies and
  tokens are never recorded. An
  HTTP 2xx proves nothing on its own — the assertions do;
- every request the page sends, reads included, is also recorded as metadata for page
  optimization (`core/evidence/e2e/network.py`), apart from the ledger above: `seq`,
  `start_ms` from the start of the run, `method`, `url` (`sanitize_endpoint`: no query,
  fragment or credentials, id-shaped segments `:id`), `resource_type`, `navigation`,
  `redirected_from`, `step_id`, `status`, `from_service_worker`, `failure`, `duration_ms` and
  `ttfb_ms` (Playwright `request.timing`), and `sizes` (`request.sizes()`: request/response
  body and header bytes). No body, header, cookie, query value or HAR is read. Always on.
  Rows are a rolling window of the newest `MAX_ROWS` (500); older rows are dropped and
  counted (`rows_dropped`, `truncated`), while the summary — `requests`, `failed`,
  `response_bytes`, `by_type`, `by_status`, the ten slowest, the ten most repeated
  endpoints — is folded over every request of the run. All four lifecycle listeners sit on
  the browser context, so a popup's requests are recorded and closed too. A URL is capped at
  500 characters, and a URL inside a failure text is sanitized the same way. Delivered at
  the end of the run (and on a player crash) as `network` events of at most 50 rows and
  48 KB, and one `network_summary`, inside the supervisor's 64 KB line limit; `report.json` carries them as `network: {rows,
  summary}`. HTTP-cache hits and initiators are not observable through Playwright;
- a step uses one `selector` or `selector_candidates` (1–5, strongest first: role, label,
  e2e, text, css), each with a `selector_provenance` (`source`, `existing_test`,
  `runtime_probe`, `heuristic` or `proven`; optionally `ref`, a `path[:line]`), optionally
  scoped by `within` (a selector for the modal, form or table). Two kinds of candidate may
  lead the list ahead of that order (`spec.leads_rank`): an `e2e` with provenance
  `source` and a `path:line` ref whose cited line, in a template file inside the project and
  outside an HTML comment, has an opening tag with `data-e2e` set to exactly that value
  (read like the tagging pass reads a line; checked at validation; without a project root
  it may not lead), and a
  `proven` selector — one the browser knowledge store
  lists for this origin, route and `within` (`knowledge.proven_selectors` returns
  `{selector, route, within}`; elsewhere the selector is an ordinary candidate). The route is
  the URL path with identifier segments as `:id`, by `sanitize_endpoint`'s rule
  (`/users/123/edit` and `/users/456/edit` are `/users/:id/edit`; route words stay apart); an entry recorded under a raw id route matches nothing until a clean run
  re-proves it. `proven` is checked against the
  store at validation; one it does not back is `spec_invalid`. Once the usual order starts,
  every later candidate obeys it, so a role-first list stays valid;
  the player tries only those candidates, needs exactly one match, and reports the
  candidate used, each candidate's match count and whether a fallback was used. `e2e`
  matches the project's `data-e2e` attribute (`[data-e2e="<value>"]`, `spec.e2e_css`) — the
  same attribute tagging proposes, and the only test attribute: `testid`, `data-testid` and
  `data-e2e` as selector keys are refused with an error naming `e2e`
  (`spec.RETIRED_SELECTOR_KEYS`), and knowledge entries recorded with `testid` are no longer
  offered to a draft;
- `ready` (1–5 conditions, each one of `hidden` / `visible` / `enabled` with a selector,
  `text`, or `url`) is awaited before the action — after navigation for a `goto` — within
  `step_timeout_ms`; unmet conditions fail the step as `not_ready` (origin `unknown`) and
  are named. Page stability on failure is `load` plus the step's own conditions, never
  `networkidle`. An action runs once: a timeout after dispatch is a failed step, never a
  re-sent write. Test steps stop early (skipped, with the reason) to leave cleanup
  `2 × step_timeout × cleanup steps` of `total_timeout_s`, at most half;
- credentials only as `${ENV_NAME}` with a registered name, spelled exactly; any other
  `${...}` is `spec_invalid`.

Cleanup runs after the test steps, whether they passed or failed. A cleanup step is
`not_needed` when the step it cleans never ran, `skipped` after an earlier failure in its
group; a planned step with no event (the player was killed or ran out of time) is
`not_run`. The report carries `cleanup: {status, groups, steps, unplanned}`, `requests`, and
`trail` (per step: selection, readiness, request ids, result), and the evidence block shows
all three. Cleanup never changes the test's `browser_verdict`: a `failed` or `not_run`
cleanup turns an otherwise passing verification into `INCOMPLETE` (exit non-zero, never
the gap-only exit 0, which covers only `verification_gap`, gaps the agent declared; a
`runtime_gap`, written by the runtime with a `tests:` prefix, exits 2) with the group in `not_verified`; a `fail` stays `fail`; a declared
`no_cleanup_reason` is a note.

Verdict combination is fail-closed: browser `fail` → `fail` whatever the reviewer says
(a reviewer `DONE` is recorded as `reviewer_verdict_overridden`); browser `pass` +
reviewer blocking → `fail`; browser `pass` + reviewer incomplete or unavailable →
`incomplete`; browser `incomplete` → `incomplete`. The `origin` tag on a finding keeps its
temporal meaning; the player's `app | harness | scenario | unknown` classification travels as
`evidence_source: e2e_runtime` and in `not_verified` reasons. `unknown` is never promoted
to `harness`.

Classification: a heuristic selector that misses, a selector of any provenance that matched
several elements (`selector_ambiguous` — the element is there, the selector is not unique),
and a write or navigation the run's own policy refused, are the scenario's (`scenario`); a
driver error is the harness's; a grounded selector (provenance `source`, `existing_test`,
`runtime_probe` or `proven`) missing from a settled page is the app's, from an unsettled page
`unknown`;
timeouts are `unknown`. A run whose undecided failures are all `scenario` ends
`incomplete: scenario_error`: not retried, not counted by the repeat brake.

Diagnosis (`classify.diagnose`): every browser run that did not pass carries one structured
answer, `{version, cause, next_step, failed, reason, fix_hint, reusable}`, in
`meta.e2e.diagnosis`, `report.json`, the evidence block the reviewer reads, and the quality
row (`cause` and `next_step`). `cause` is an enum (`classify.CAUSES`: `scenario_selector`,
`scenario_selector_ambiguous`, `scenario_write_policy`, `scenario_navigation_policy`, `selector_not_rendered`,
`app_assertion`, `app_runtime_error`, `environment_slow`, `environment_setup`,
`runtime_harness`, `unknown`), `next_step` another (`fix_scenario`, `fix_app`,
`fix_environment`, `raise_limit`, `investigate`); `failed` names the step by id, action,
claim, error kind and origin — never expected or actual values; `fix_hint` is fixed text per
cause. The repeat record stores the newest `cause`, so a brake refusal carries the same
diagnosis (`reason: repeat_failure`), derived from the reason for a record without one.
`reusable` causes (`scenario_selector`, `scenario_selector_ambiguous`, `selector_not_rendered`) are written to the browser
knowledge store as `failure_hint` entries (the step's placeholder view, cause, hint),
offered to the next draft for the origin right after `repeat_failure`, and retired by a
passing run against it. A same-origin 5xx on the main request, or an uncaught page error thrown by a
same-origin script (attributed by the script URL in its stack, not the page URL), fails
the run even when every assertion passed. Same-origin console errors fail it only with
`fail_on_console_error`. Third-party noise stays a warning. A passing existing test proves
the claims it covers unless the browser already failed them; a failing one makes them
`unknown`.

Artifacts live under `sessions/<session>/logs/<run>/e2e/`: `spec.json` (placeholder
form), `events.jsonl` (typed values omitted), `report.json`, `evidence.md`,
`verification.md`, `existing_tests.log`, and for a failed step `stepNN.html` (bounded,
input values cleared before capture) and `stepNN.png`, plus `trace.zip` for a failed run.
A heuristic selector miss keeps a bounded DOM probe instead of a screenshot. When the
scenario resolved any `${ENV}` value, no screenshot and no trace is taken: binary captures
cannot be scrubbed afterwards. Resolved values are scrubbed from events and text artifacts
(`.html .htm .txt .log .json .jsonl .md`, in any subdirectory of the run's `e2e/`)
raw, URL-encoded, HTML- and JSON-escaped (values shorter than 4 characters excepted).
Scrubbing is fail-closed: a text artifact that cannot be read or rewritten is deleted, and
every such file (or a run directory that could not be listed) is named in
`meta.e2e.artifacts_unscrubbed` (`{path, removed, error}`) and marked pruned with
`pruned_reason: unscrubbed` ("withheld: could not be scrubbed; do not open" in the evidence),
so the report and the reviewer never point at it. When anything was left behind (`removed:
false` — a file that could be neither scrubbed nor deleted, or a directory that could not be
listed), every artifact of that attempt is withheld, the attempt directory is named in
`meta.e2e.artifacts_unsafe`, and the reviewer's evidence block says `artifacts: withheld ...`
instead of the directory path. A run over `settings.artifact_max_mb` loses traces, then screenshots, then HTML. The
directory is pruned with its run and never entered into `evidence.jsonl`.

Browser knowledge (`core/evidence/e2e/knowledge.py`, `.workflow/e2e-knowledge.jsonl`) sits
at the fact store's level: written automatically, anchored, aged out on its own, never in
Git. Per base_url origin a run records `auth.login` (the login page's goto through the first
assertion after `${E2E_PASS}`), `auth.logout` (an action followed by an `expect_url` back to
that page), `navigation`, `page_ready` and `selector` (matched exactly one element), plus the
`repeat_failure` and `failure_hint` entries above —
built from the placeholder scenario, with a non-placeholder `fill` value dropped. Only proof
counts: a passed, non-writing step in a run that passed on its FIRST attempt (exactly one
attempt, `browser_verdict: pass`). A failed step weakens the matching entry, at most once per
run, and not when its selector matched before the action failed; `STALE_AFTER_FAILS` (2) in a row retire it. A selector entry
also records `matched`, the candidate that matched; it is `proven` while the entry is live
and has not missed since it last passed, and, when anchored, while its source line still
exists (checked when proven selectors are read, not only by `clean`). A `proven` candidate
that was tried and did not match — nothing matched, or a later candidate won; a step that
fails after it matched is not a miss, and neither is a step that failed before its selectors
were tried (not ready, an exception: no `selection.match_counts`); only candidates the browser
counted can miss — weakens every entry with the same origin, route, `within` and matched selector, whatever the
step's key, at most once per run, so one missing run demotes it to the usual order and two
retire it. A clean pass stores `proven_scope: {route, within}` beside `matched`; rows written
before that are hints, not proven, until re-proven on their route. The application
fingerprint is not consulted for this: it changes on every commit. A selector with a
`source_ref` is anchored like a fact; `--command clean` relocates a moved anchor and drops
retired entries and vanished anchors. A draft gets the live entries for its origin (bounded
per kind) and the `proven` selectors in the sidecar `sessions/<session>/runtime/e2e_knowledge.json`, named in the prompt
only when it holds something, as hints that still have to be grounded in code;
`meta.e2e.knowledge` reports `offered` on a draft and `added | confirmed | weakened |
retired` on a run. `promotable_claims()` turns anchored entries proven in
`PROMOTE_AFTER_PASSES` (3) runs into `/.promote` claims.

Metrics: every run (not a draft) appends one `kind: e2e_run` row to
`.workflow/data/quality.jsonl` — raw counts, the stage prompt ids, a scenario hash, `run_kind`
(`fake`, `smoke`, `project`), an optional `WORKFLOW_E2E_LABEL`, the base_url `origin`, and
the network summary as `network`. `meta.e2e.page_requests` carries the same summary plus
`since_last_run`: the change in requests, failures, response bytes and per-type counts and
bytes against the last run of the same kind on the same origin (None for the first). `exoma --command
report` derives `e2e` from those rows, split by run kind: verdict, incomplete-by-reason
and origin rates, browser runs per claim, probes, screenshots kept against screenshots
sent to a model, tokens per run (joined through `usage.jsonl`), reproducibility across
repeated scenarios, and a delegated-verify baseline from the same workspace. No
visual-browser-loop baseline is recorded, and none is estimated.

Every draft appends its own `kind: e2e_draft` row, never an `e2e_run`: `status`, `reason`,
`first_error` (the category of the first reply's first error), `categories` (every error
still standing after the repair), `initial_categories`, `repair_attempted`,
`repair_recovered`, `provider_calls`, and the names in `secret_literals`. Categories come
from `spec.error_category`, an ordered match over the validator's own messages
(`spec.DRAFT_ERROR_CATEGORY_NAMES`: `section_missing`, `fence_missing`, `json_invalid`,
`credential_literal`, `source_refs`, `provenance`, `selector_shape`, `readiness`,
`cleanup`, `side_effect`, `enum`, `policy`, `claims`, `secrets`, `structure`, `other`);
messages are never stored. A draft that is not `ready` also carries `meta.e2e.diagnosis`:
`cause` `draft_invalid` (next step `fix_scenario`), `draft_secrets` or `draft_blocked`
(`fix_environment`), with the same category counts and the repair outcome. `--command
report` adds `e2e.drafts`, per run kind: drafts, ready/invalid/blocked, ready rate, repairs
attempted and recovered, first-error and remaining-category distributions, and drafts that
carried a literal credential.

Dependency policy: Playwright is an optional extra (`exoma[e2e]`), pinned in the bundled
`requirements-e2e.txt` and installed by `exoma install --apply --with-e2e` (pip, then
`python -m playwright install chromium`). The runtime stays stdlib-only;
`core/evidence/e2e/preflight.py` and `browser.py` import it inside the call, under an
`ImportError` guard, and `tests/checks/deps.py` permits exactly that shape in exactly that
package. `/.doctor` reports `e2e_readiness` from package metadata only (installed and
pinned versions, never blocking); the browser and the URL are each run's preflight.
`WORKFLOW_E2E_FAKE=<mode>` makes the player emit a deterministic run (`pass`, `app_fail`,
`harness_fail`, `unknown`, `launch_fail`, `malformed`, `crash`, `stall`,
`heartbeat_forever`, `artifacts`) so the lifecycle is testable without a browser;
`WORKFLOW_E2E_SMOKE=1 python tests/run.py --only e2e-smoke` drives real Chromium against
`tests/fixtures/e2e_app/`.

## Second agent: provider config, model, and prompt transport

A project runs on its own `.workflow/second_agent.json` and nothing else
(`config.settings.resolve_provider_config_for`). There is no machine-wide fallback: a missing
file resolves `source: missing` and an unparseable one `source: invalid`, and the executor
refuses both before any provider call (`provider_config_missing`, `provider_config_invalid`,
each with a next_action). The per-user `second_agent.seed.json` (in the data directory) is a
template `init` copies into a new workspace and `exoma install --apply` rebuilds from
`second_agent.example.json` on every run, carrying only the previous selection (provider,
default model, per-route models). An old `second_agent.json` beside it, written once and never
refreshed, is renamed `.retired` and its selection is not carried over.

Selectable routes are `explore plan analyze verify e2e_spec`. An opencode call whose route
resolves no model is refused as `model_unset`: without `-m`, opencode answers with the model
it used last on the machine. `/.doctor` names `-free` models as a recommended fix.

Only opencode's read boundary is enforced (`<project_root>/opencode.json`, whose root
`permission` carries `external_directory: deny` next to the secret `read`/`grep` denials).
Its child process gets an allowlisted environment plus the active project's `.env`, not the
parent's (`adapters/shared/child_env.py`; counts, never values, land in the call meta as
`env_policy`/`project_dotenv`/`parent_env_dropped`). `codex` and `agy`
install as `not_enforceable`, and their adapters pass the full process environment to the
provider CLI. Both are treated as trusted providers — an accepted risk, not a defect waiting
for an env allowlist: an allowlist can break the CLI's own auth, and it protects nothing from
a provider that can already read `.env`. What the runtime guarantees instead is visibility:
`/.doctor` reports `checks.second_agent_read_boundary` (`not_enforceable` + `trusted: true`,
or `enforceable` for opencode) and one `WARNING: second_agent provider ...` entry in
`recommended_fixes` — never an issue, so readiness is unaffected.

Writes are a separate axis, declared per bundle as `boundary.writes` in `config/providers.py`
and read through `provider_boundary()`: opencode `denied` (permission map), codex `sandboxed`,
agy `unbounded`. codex's `sandboxed` takes TWO flags on every call, resumed ones included:
`--sandbox read-only` (an `exec` option, so `exec --sandbox read-only resume <id>` on a resumed
call; `-c sandbox_mode=` there made codex 0.155+ refuse every resume on Windows)
confines shell commands, and
`-c approval_policy="never"` makes codex reject an `apply_patch` outside the writable roots —
without it, codex-cli 0.154.0 on Windows wrote a file through that tool under a read-only
sandbox. `tests/checks/codex_write_probe.py` (opt-in, `WORKFLOW_CODEX_PROBE=1`) re-proves both
write paths on both branches. `/.doctor` adds `checks.second_agent_write_boundary` and, for
`unbounded` only, a second `WARNING: second_agent provider '<p>' can WRITE ...` entry. Every
delegated result of a call that reached the provider step — `ok` or not — carries
`meta.provider_boundary` (a call refused earlier, e.g. `session_capture_failed` at the
preflight, started no provider and carries none) (`{reads, writes}`; also in the archived call
meta, not in the usage row), plus `meta.provider_write_warning` when writes are unbounded.

The provider thread id is persisted or its loss is named. Session records are written through
`atomic_write_json` (unique temp name; `os.replace` retried on `PermissionError`, which Windows
raises while any reader holds the target open). Before a provider is spawned the executor
writes the session record once (`SessionManager.ensure_writable`); failure returns
`session_capture_failed` with the task not started. codex and agy report their id mid-stream;
a write that fails there is retried after the run, and if that fails too the result stays
`ok` with `meta.session_persisted: false` and `meta.session_persist_error` — no resume and no
continuation for that thread.

opencode's prompt travels as a FILE: it is written to
`sessions/<session>/runtime/opencode-prompt.md` and attached with `-f`, and argv carries one
static sentence (transport `file` in `config/providers.py`, so the task cap is the policy
cap, not a command-line remainder). On Windows `opencode` is the npm shim `opencode.cmd`,
parsed by cmd.exe, which does not understand `subprocess`'s `\"` escaping: with the prompt on
argv, a quoted JSON example became an input redirect (every verify-browser draft failed with
"The system cannot find the file specified") and an odd quote in a task was command
injection. Any remaining argument that cmd.exe would interpret (`" & | < > ^ %` or a newline
in the command path, prompt-file path, model, agent or effort) is refused before spawning
as `unsafe_command_line`.

## exoma install --apply and leftovers

`--apply` refuses a bundle (`exoma/_bundle/config/`) that does not match `exoma/_bundle/manifest.json` (dry run warns). After
installing, it removes files an earlier release installed and this one no longer ships —
only a file an install recorded writing (the ledger `~/.claude/.workflow-installed.json`, or
any install receipt), in a family installed file by file (skills, commands, hooks, provider
agents), and still byte-for-byte what was written. A file edited since is kept and named.
Each removal is backed up under `backups/install_*/stale/` and receipted as `remove`, which
`--rollback` restores. settings.json loses the hook commands that run a retired script and
has a `statusLine` that runs a workflow script refreshed; a statusLine or hook running the
user's own script is theirs. `--check` reports leftovers. `init`/`upgrade` point a workspace
at the build running the command; `$AGENT_PATH` and the agent-workflow paths a workspace
recorded are reported (`doctor` `runtime_resolver.legacy`) and never followed.

An instruction file with markers (`CLAUDE.md`, each provider `AGENTS.md`) that does not
exist yet is written as the managed block alone, as a later install would splice it; text
outside the markers in the bundle is never installed. Hook entries are merged per event by the
script they run; a shipped script's command on an event whose template entries no longer
name it is removed from that event (`_drop_relocated_hooks`), so a hook that moved does not
keep running in its old place.

`--uninstall` is the inverse, through the same backup and receipt: the managed block is cut
from each instruction file (a file left empty is removed, `action: remove`); a managed file is
removed only when its hash is one an install recorded; settings.json loses every command that
runs a shipped or retired workflow script, and a workflow `statusLine`, keeping all other
keys; the mode file and ledger are deleted unreceipted. `--rollback` undoes it like an install.

The receipt (`backups/install_<timestamp>/install_receipt.json`, `schema_version: 2`) is
written incrementally: once, empty, before any destination changes — so a receipt that cannot
be written fails the apply before anything was touched — and again after every recorded step,
with `complete: false` until the final write. Each entry (`action`, `key`, `dest`, `backup`,
`pre_sha256`, `post_sha256`) is recorded after its file is written, so every entry is a step
that finished. An apply that changed nothing leaves no receipt. `--rollback` accepts a
`complete: false` receipt, prints `NOTE: that install was interrupted; undoing the steps it
recorded.` and `Any step it did not record still has its backup under <dir>.`, and undoes the
recorded entries with the same conflict checks as a complete one. A step that died mid-write
has no entry; its backup is copied back by hand. A backup directory with no receipt, an
unreadable receipt, or another schema is refused, never rolled back blind.

## Store locks

The fact store, the knowledge store and the browser-knowledge store serialise their
read-modify-write through `utils/owned_lock.OwnedFileLock`. The lock file is created with
`O_EXCL` and holds `{pid, token, at}`. A lock is taken over only when its owner is provably
gone: the recorded pid is not running, or the file has no readable owner and is older than
the TTL (default 30 s — a writer that crashed between create and write). A live owner is
waited for up to the deadline (default: the TTL) and then reported as
`TimeoutError("lock <name> is still held by a running process")`; an old lock of a live
process is never stolen. Reclaim is serialised through an OS advisory lock on
`<lock>.reclaim`, and the owner is judged again inside it, so two writers that both saw the
same dead pid cannot delete each other's fresh lock. Release removes the file only while it
still carries the holder's token. A reused pid keeps a dead owner's lock looking alive, which
ends in a timeout, not a concurrent write. The evidence store still uses its own lock
(`core/evidence/evidence_store._EvidenceLock`); migration takes all four store locks before
moving anything.

Every OS byte-range lock below goes through one primitive in `utils/osutil.py`:
`open_lock_file` (opens the file and gives an empty one its byte), `lock_file` (exclusive
lock on byte 0; non-blocking raises `OSError` while held, blocking on Windows is msvcrt's
retry of about ten seconds) and `unlock_file`. No other module imports `msvcrt` or `fcntl`.

## Process identity and worker kill

A PID recorded for a worker, a job reservation, a job lock or the runtime lock carries a
create time (`pid_create_time`, Windows only) and an identity (`osutil.process_identity`:
the native creation time on Windows, boot id and start ticks from `/proc` on Linux, the
`ps -o lstart=` start time elsewhere). `osutil.process_generation` compares the recorded
values with the live PID and answers one of:

| Outcome | Meaning |
| --- | --- |
| `matched` | The PID still hosts the recorded process |
| `reused` | The PID is live and provably hosts a different process |
| `dead` | No process has the PID |
| `unverifiable` | The PID is live, but nothing was recorded or nothing can be read to compare |

A worker is killed only on `matched` (`JobManager._kill_worker`). `reused`, `dead` and
`unverifiable` return `{"method": "skipped", "reason": "pid_reuse_detected" | "worker_dead"
| "identity_unverifiable"}` and the job is still failed and reaped. The runtime's own PID is
never killed (`self_pid`). The policy is fail-closed because the two errors are not
symmetric: a missed kill leaves a stray worker whose job is already failed; a wrongful kill
ends a process that is not ours. Admission and recovery keep their rules: an `unverifiable`
worker still counts against capacity while younger than the claim staleness window, and a
running lock owner with an unreadable identity keeps a create-time lock but not an identity
lock.

Lock files come in two patterns: a lock file owned by its holder, removed on release, and a
one-byte file carrying an OS byte-range lock, kept between uses:

| File | Kind | Content | Lifetime |
| --- | --- | --- | --- |
| `facts.jsonl.lock`, `e2e-knowledge.jsonl.lock`, `promote.lock` | `OwnedFileLock` | `{pid, token, at}` | Removed on release; a leftover empty one is a writer that crashed between create and write, reclaimed after the TTL |
| `<data dir>/storage/jobs/locks/<session>.lock` (per user, not the project) | job session lock | job id and token | Removed on release |
| `graphify-out/.refresh.lock` | graph-refresh Stop hook, then its detached worker | `{pid, token, started, beat, graphify_pid}` | Removed by the worker holding the token. Held while `beat` is younger than 120 s, whatever the lock's age: the worker rewrites it every 15 s and graphify has no time limit. A lock without `beat` keeps the old rule (live pid, younger than 15 minutes). A stale one is removed by the next hook or by `clean`; nothing kills a process |
| `evidence.jsonl.lock` | OS lock on byte 0 | one NUL byte | Kept: the lock is the open handle, released by closing it |
| `<lock>.reclaim` | OS lock on byte 0 | one NUL byte | Kept, one per `OwnedFileLock` that was ever reclaimed |
| `<data dir>/storage/jobs/.capacity.guard`, `<session>.lock.guard`, `<claim>.guard` | OS lock on byte 0 (`JobManager._exclusive_file_guard`) | one NUL byte | Kept, one per session or claim path; they accumulate |
| `sessions/<id>/runtime/lock.guard` | OS lock on byte 0 (`_RuntimeTransitionGuard`) | one NUL byte | Kept between uses; `clean` removes it when the session has no live runtime lock and the guard can be locked without waiting. A holder keeps it. On POSIX it is unlinked while locked and a process that was waiting on it reopens the path; Windows refuses the delete while another process has it open, and that refusal keeps it |

A one-byte file shows as empty in most editors; its content is `\0`, which the Windows
byte-range lock (`msvcrt.locking`) needs to lock. Deleting a kept file while no process runs is
harmless: it is recreated on the next use. Deleting it while a process holds it is what the
design rules out: Windows refuses the delete, and on POSIX the next writer creates a new,
unlocked file, so two writers run at once. The `evidence-lock-file` test suite checks both,
and that a second process waits for the holder. The runtime guard is the one exception made
safe on purpose: `_RuntimeTransitionGuard` re-checks on POSIX that the path still
names the file it locked and reopens it if not, which is what lets `clean` remove it.

## Call telemetry

Telemetry written by `Executor` is best-effort; the provider's result, or the provider's own
exception, is what the call returns, and a telemetry failure never replaces it. Two parts
fail differently:

- **Call meta** runs in the call's `finally`. Any exception inside it — a malformed handoff
  meta, a redaction failure, a write error — is caught and recorded as
  `"<ExceptionType>: <message>"`: on the result as `meta.call_meta_error` when the provider
  returned one, and in the executor's last call meta either way, so the previous call's meta
  is never left standing in for this one. For exploration and reasoning calls it also holds
  `served_facts`, the facts `fact_store.load_relevant` served (`key`, `file`, `line`; `[]`
  when none), recorded before the prompt is built so the archived call names exactly what the
  second agent was shown.
- **Usage and audit rows** (`_record_usage`: `write_usage_record`, then `write_audit_record`)
  are written separately, on the return path. A failure there is swallowed silently — no
  `call_meta_error`, no field on the result. A missing usage or audit row is therefore not
  evidence that the call failed.

Usage rows carry contract version 4 (below). Version 3 added six fields, all `None` on older rows:

- `reuse_outcome` — why the reuse lookup did or did not serve the command: `hit`,
  `no_prior`, `stale`, `unreadable`, `error`, `not_offered`, `disabled`.
- `graph_status` — `used`, `empty`, `absent`, `refreshing`, `disabled`; for `verify-browser`, `available`, `absent` or `refreshing` from `graph_state_for_call`, taken once per command before the browser pipeline: its prompts carry no leads, so only whether a refresh ran beside it is recorded, and `graph_ms` is the cost of that check.
- `graph_ms` — how long the graph lookup took.
- `provider_resumed` — whether the provider resumed an existing thread, as the adapter
  reports it on `last_call_meta` (codex, agy). `None` where the adapter does not say:
  opencode always runs on a session it opened or resumed and cannot tell the two apart.
- `provider_thread_changed` — a resumed call answered on a different thread id, so the
  session lost its state without an error (codex, agy).
- `effort` — the reasoning effort the adapter passed; `None` means no flag was sent and the
  provider's own default applied.

`refreshing` means the call ran while `graphify-out/.refresh.lock` was held and went without
leads. The lookup never waits on that lock.

Contract version 4 adds two fields and drops one kind of row:

- `e2e_stage` — `verify-browser` only: `draft` (the `e2e_spec` call, `command:
  verify-browser`) or `run` (the review, `command: verify`). `None` on every other row.
- `browser_seconds` — on the final row of a `run`: the summed `duration_seconds` of stage 2
  (the player) and `existing_tests`. `duration_seconds` stays the provider's own time.
- `provider_version` — the provider CLI's version line (`<cli> --version`, first semver line
  of stdout then stderr, cached per user by executable path, mtime and size for 24 h); on
  every row that reached a provider, single call or continuation; `None` when it did not
  answer.
- `provider_version_status` — `stable` when that release is the bundle's `stable_version`,
  `untested` for any other release, `unreadable` when no version was read
  (`core/provider/versions.py`; reference "Provider releases").
- `tests_requested`, `tests_run`, `tests_failed`, `tests_seconds` — `/.verify` only, on the
  command's final row: whether main_agent wrote `verify/tests.json`, how many requested
  commands the runtime ran (refused ones excluded), how many of those did not pass, and their
  summed wall time. Not part of `duration_seconds`. `None` on every other row.
- A browser run that ended before its review (incomplete, preflight, refused spec or env, no
  request) reached no provider and writes **no** usage or audit row; the `e2e_run` quality row
  and the `verify` task event still record it. Before v4 such a run wrote a `verify` row with
  no `prompt_id`, `provider` or tokens, which the statusline counted as a call and the report
  as a judged verify and a zero-token baseline verification. `telemetry.report` drops rows of
  that shape (`provider_less_browser_run`); the statusline, being session-scoped, does not.
- `reuse_outcome` is `not_offered` on both browser stages, and `evidence_reuse` leaves
  `verify-browser` drafts out of the reuse-eligible commands, current and past rows alike.

## Task events

`tasks.jsonl` (`core/audit/task_telemetry.py`) is append-only and has two writers.
The executor appends one `source: runtime` event per delegated command from `_record_usage`,
after the usage and audit rows and inside the same fail-open block: `skill` (the command),
`verdict` (verify only: the reply's declared verdict, `NEEDS FIX` when the contract finds a
blocking finding), `derived` (verify only: the runtime's derived verdict, `pass`,
`incomplete` or `fail`), `next_action`, `prompt_id`. A finalised browser run is `verify`, its draft
`verify-browser`, including a run that reached no provider and so wrote no usage row. The
`task-events` hook appends `source: hook` events (`v: 2`) for what never reaches the runtime:

- `UserPromptSubmit`: `skill` for a prompt that opens with `/.<name>` naming a local workflow
  skill (delegated skills are skipped, the runtime has them);
- `PostToolUse` (matcher `Skill|Edit|Write|MultiEdit|NotebookEdit`): `skill` for the `Skill`
  tool naming a local skill, and `edit` with a project-relative `path` (`.workflow/` and
  `.git/` excluded);
- `commit` rows, from a HEAD snapshot per Claude session at
  `.workflow/data/task-hook/<session_id>.head`. The session's first event writes it. Each
  `Stop`, and each later `UserPromptSubmit`, compares HEAD with it and, when HEAD moved,
  appends one row per commit in `git log --reverse --first-parent -m --name-only --relative
  <snapshot>..HEAD` (`commit`, `paths`), then moves the snapshot. A commit is therefore
  recorded at the end of the turn that made it, after any verify in that turn; one made
  between turns is recorded on the next prompt.

Version 1 events (before 3.8.0) came from every `Read` and `Bash` call: a skill was a `Read`
of `~/.claude/skills/<name>.md`, and a commit was a `git commit` that moved HEAD between
`PreToolUse` and `PostToolUse`. The reader treats both versions alike. The hook resolves
MAIN_SESSION_ID through `~/.claude/session_registry.json`, writes only when
`.workflow/data/` exists, and exits 0 on every path.

Nothing is derived at write time. `derive_tasks` walks the events in order per session: the
first event opens a task; an `edit` clears a verdict seen before it; a verify whose
`derived` is `pass`, on a task that edited something, closes it as `completed`.
A commit closes nothing: one whose `paths` include a file the open task edited joins that
task's sequence, otherwise one that includes a file of the task the session last completed
joins that one; a commit hash is taken once it is claimed (a copy another session logged first does not hide
it from its own session); a distinct commit no copy claims is `unclaimed_commits`. A task still open at the end is `open`, or `read_only` (no edits);
events without a session are `unknown`. Older events carry no `derived`,
so their tasks stay `open`.

## Workspace layout

`.workflow/` holds what a person edits: `config.json`, `second_agent.json`,
`e2e/secrets.json`, the `run`/`check`/`inspect` scripts, and `current/`. Everything else —
`sessions/`, `provider-sessions/`, `reports/`, the usage/audit/quality/redactions streams,
the evidence, fact and browser-knowledge stores, caches, backups — lives in
`.workflow/data/`. `workspace_paths.workflow_paths` is the only place any of those paths is
spelled.

`data_dir()` decides the layout from the disk: `data/` once it exists; the `.workflow` root
while a v3.5.x-layout workspace still keeps its data there (any of `_LEGACY_MARKERS` present); `data/`
otherwise. The hooks (`intent-gate-set`, `intent-gate-check`, `workflow-statusline`) and the
generated run scripts apply the same rule when they run, so every reader agrees at any
moment. `upgrade` (and init's auto-upgrade) runs `core/runtime/migrations.py`: backup to
`data/backups/<stamp>/`, store locks held, internal items staged in `data.migrating/` and
renamed to `data/` in one step, evidence artifact paths rewritten, pre-session leftovers and
old locks removed, config stripped to overrides, a pre-list `secrets.json` converted.
A leftover `data.migrating/` from an interrupted attempt is put back first; if it holds a
name that also exists at the `.workflow` root, `MigrationConflict` is raised and nothing is
moved — keep one copy of each (compare first), remove the other, rerun upgrade. Past that
check, a failure has three distinct outcomes, and upgrade reports each differently:

- **Before the rename, clean rollback** — every moved entry goes back to `.workflow/`, the
  backup is kept as `migration-backup-<stamp>/`, and upgrade says `workspace migration failed
  and was rolled back`.
- **Before the rename, rollback incomplete** — every move-back is attempted and every failure
  collected (a move-back, a backup move, or removing the emptied staging tree). Any failure
  raises `MigrationRollbackIncomplete` (`.failures`, `.staging`); `data.migrating/` is kept,
  because it holds the only copy of what could not move back. Upgrade says `workspace
  migration failed and the rollback is INCOMPLETE: ...` and names the staging path: move its
  entries to `.workflow/` by hand, or rerun upgrade, which recovers a leftover staging
  directory first.
- **After the rename** — `MigrationIncomplete`. Nothing is moved back: the workspace is on
  `data/` and working. Upgrade says `workspace migration incomplete: ...`; rerunning resumes
  from the unfinished step recorded in the pending file.

A live job refuses the upgrade before anything moves. `doctor`
reports `workspace_layout` (layout version, data dir, unrecognised root files).

`current/` is a mirror of the latest delegated dispatch, written by `Executor.execute` and the
browser runner and never read back: `session.json` (session, command, status, phase,
`other_active` sessions holding a runtime lock), `progress.jsonl` (reset per dispatch), and
for a browser run `e2e/events.jsonl` (scrubbed like the stored events), `e2e/last.png`, and
the run's `report.json`, `verification.md`, `evidence.md`. A newer dispatch takes it over and
an older session stops writing into it.
