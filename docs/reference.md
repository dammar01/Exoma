# agent-workflow v3.8.4

Standalone orchestration runtime for a two-agent workflow. No third-party dependencies.

## Summary

Two roles, strictly separated:

- **main_agent** — the agent you use (Claude Code, Codex, Cursor, etc.). Orchestrator, user interface, and the **only** one allowed to write files.
- **second_agent** — the selected provider (OpenCode, Codex, or Agy), a **read-only** evidence gatherer. Not the final answer. Each provider's write boundary differs; see [Security](security.md).

This runtime sits between the two: it receives a command, assembles a structured prompt, runs the selected provider's CLI (e.g. `opencode run`), validates the output shape, stores per-session state, then returns a stable JSON contract:

```json
{ "ok": true, "content": "...", "meta": {} }
```

`digest` is added only when the second_agent output carries a parseable `[DIGEST]` block; it is not a required field.

Evidence, config, per-session state, and provider session bindings (`.workflow/data/provider-sessions/`) live project-local in `.workflow/` in the target project. The job queue and the tool's cross-project cache stay in this repo's `storage/` (see [Workspace layout](#workspace-layout)).

---

## Flow from prompt to response

```text
Prompt user
→ main_agent: auto-detect intent or use the /. override
→ .workflow/run.sh (bash on every OS)
→ main.py await → JobManager → detached worker
→ Executor → Router
→ Graphify leads + fact store → PromptBuilder
→ selected provider adapter (OpenCode/Codex/Agy) → second_agent
→ sub-agent fan-out when enabled
→ contract validation + digest + evidence storage
→ await returns {ok, content, meta, digest?}
→ main_agent relay/synthesis
→ response user
```

Auto-intent, the `[OPTIONS]` output of `/.plan`, and the decision to run `/.verify` after `/.execute` live in the **main_agent** prompt layer. The Python runtime never receives the raw user message and never sees the final response written by main_agent. The runtime only manages delegation, evidence, jobs, and policy metadata.

This diagram shows the full delegated path. `sweep` is cut short earlier by `main.run()` and finishes locally without `Executor`; `verify` with `verify_mode: syntax` stops at `quick_verify` before Router, Graphify, PromptBuilder, OpenCode, and second_agent.

---

## Response envelope

Every result has the shape `{ok, content, meta}`. Other fields are conditional:

| Field | When present | Content |
|---|---|---|
| `digest` | second_agent output carries a parseable `[DIGEST]` block | summary to be relayed. `digest.anchors = {certified, total}` is added when the evidence records anchors; a confidence lowered because of anchors/graph keeps its original value in `digest.confidence_reported` and the reason in `digest.confidence_capped_by` |
| `evidence_ref` | only the explore/plan/analyze evidence path | `{artifact_path, anchors, certified_anchors, reused}`. `reused: true` = an artifact from a past session was reused (see [Evidence reuse and boundary](#evidence-reuse-and-boundary)) |
| `meta.task_cap`, `meta.task_cap_source` | every delegated call | effective task length limit. `task_cap_source`: `transport` (computed from the remaining argv command-line space), `floor` (scaffolding nearly fills the command line; minimum value), `policy` (opt-in `AI_PROXY_MAX_TASK_CHARS_NO_ARGV`), `unbounded` (`task_cap: null`; stdin/file transport, or argv outside Windows) |
| `meta.task_truncated` | task cut by the cap | signal to shorten the instruction, not to split the call |
| `meta.content_mode`, `meta.content_full_chars` | `AI_PROXY_SLIM_CONTENT` is active, content is long, and the artifact exists on disk | `content_mode: "ref_only"`: `content` is only a preview; the full text is at `evidence_ref.artifact_path` |
| `meta.policy` | every delegated call | the effective `auto_verify_after_execute`, `verify_mode`, `subagent_fanout_enabled` |
| `meta.error_type`, `meta.next_action` | `ok: false` | both required; `error_type` from the closed set below |

### `error_type`

A closed set in `core/evidence/contract.py` `ERROR_TYPES`; `make_error` rejects names outside that set, and the `error-types` suite ensures every literal used in code is registered.

| Group | `error_type` |
|---|---|
| Provider and worker | `timeout`, `worker_died`, `worker_crashed`, `worker_stalled`, `job_expired`, `rate_limited`, `streaming_failed`, `sandbox_unavailable`, `second_agent_unavailable`, `command_not_found`, `permission_denied` |
| second_agent output | `empty_output`, `invalid_evidence`, `session_capture_failed`, `task_truncated`, `fact_ingest_failed` |
| Prompt and command line | `prompt_too_long`, `unsafe_command_line` |
| Provider config | `provider_config_missing`, `provider_config_invalid`, `model_unset`, `invalid_provider_selection` |
| Job, session, workspace | `job_already_running`, `job_submit_error`, `worker_capacity`, `runtime_lock`, `session_state_error`, `routing_error`, `workflow_init_error`, `workflow_upgrade_error`, `sweep_git_error`, `budget_exceeded` |
| Promote and knowledge | `promote_input_missing`, `promote_input_unreadable`, `promote_existing_unreadable`, `promote_not_on_branch`, `promote_not_production_branch`, `promote_write_rejected`, `invalid_knowledge`, `secret_shaped_content`, `ignored_source`, `ignored_destination` |
| Other | `unknown` |

`worker_died` with `meta.reason: recovery_exhausted` means automatic recovery has already been used; do not retry automatically (see [Async jobs & recovery](#async-jobs--recovery)).

`sandbox_unavailable` with `meta.reason: resume_sandbox_failed` means the codex Windows sandbox rejects a **resumed** thread; a new thread is known to work (see "Codex on Windows" in [`.workflow/second_agent.json`](#workflowsecond_agentjson)).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | the `X \| None` syntax is used throughout the code. `main.py`, `install.py` and `check.py` reject older Python with a single message (the version and interpreter in use) before any import. The `.sh` hooks pick an interpreter by running it, in order `python3`, `python`, `py -3`; one that does not run (WindowsApps alias) or is older than 3.10 is skipped, and with none of them the hook fails open |
| `opencode` on `PATH` | only for delegated commands |
| `git` on `PATH` | optional; used by `sweep` and the `syntax` verify mode |
| Runtime dependencies | **zero**; the whole runtime uses the standard library |
| Graphify | optional |

Quick check:

```bash
python3 --version   # or: python --version / py -3 --version
bash --version      # Windows: Git Bash (Git for Windows)
opencode --version
git --version
```

---

## Install (v3.8.4)

### New team member — full sequence from scratch

Four steps, run once per machine (except step 4, once per project):

```bash
git clone <repo> && cd agent-workflow      # 1. get the tool
python install.py --apply                  # 2. install the global agent config
export AGENT_PATH="$PWD/main.py"           # 3. tell the runtime where main.py is
                                           #    (make it permanent — see "Set AGENT_PATH")
python main.py --command init --work-dir /path/to/your-project   # 4. per project
```

Verify before using it:

```bash
python install.py --check                  # bundle + global install
cd /path/to/your-project && bash .workflow/run.sh doctor    # every OS (Git Bash on Windows)
```

`doctor` must report `READY` with zero issues. Otherwise, read `recommended_fixes`
before continuing — a `NOT_READY` status means an entry point is genuinely broken,
not merely a style note.

The entry scripts in `.workflow/` bake in absolute paths of the machine where `init` was run, so
step 4 belongs to each person. Committing `.workflow/` to a shared repo will not
help anyone; `init` already adds it to `.gitignore`.

<a id="detail-installer"></a>
### Installer details

Clone → run one script → configured.

```bash
git clone <repo> && cd agent-workflow
python install.py            # DRY RUN — show every change, write nothing
python install.py --apply    # now writes
python install.py --apply --only-command      # workflow active only through /.<command>
python install.py --apply --provider codex --model gpt-5.6-sol   # choose the second agent without a prompt
python install.py --check                     # check bundle + install (including the project when cwd has .workflow/)
python install.py --rollback                  # dry run of the latest rollback
python install.py --rollback --apply          # rollback after a hash preflight
python install.py --uninstall                 # dry run: what would be removed
python install.py --uninstall --apply         # remove, with a receipt (can be rolled back)
```

All `install.py` flags:

| Flag | Effect | Writes? |
|---|---|---|
| (no flag) | dry run: print the plan | no |
| `--apply` | execute the plan; combined with the other flags below to actually write | yes |
| `--only-command` | command-only mode (see "Intent mode"); persisted, applies to subsequent installs | with `--apply` |
| `--auto-intent` | restore auto-intent mode | with `--apply` |
| `--provider NAME` / `--model ID` | second agent choice for the seed (`--model` requires `--provider`) | with `--apply` |
| `--with-e2e` | also install the Playwright + Chromium extra for `/.verify-browser` | with `--apply` |
| `--set-env` | persist `AGENT_PATH` (Windows: `HKCU\Environment`; POSIX: `~/.bashrc`/`~/.zshrc`) | with `--apply` |
| `--update-providers` | bring each provider CLI to its `stable_version`, global npm installs only (see "Provider releases") | with `--apply` |
| `--check` | drift report for the bundle, the global install, and the project when cwd has `.workflow/` | no |
| `--rollback [ID]` | undo one install/uninstall from its receipt (default: latest) | with `--apply` |
| `--uninstall` | remove what the workflow installed (see "Uninstall") | with `--apply` |

`AGENT_HOME` (env) replaces `~` as the target home — used by tests and the installer e2e to
install into a temporary HOME.

Bundle drift (`--check`, and the `--apply` refusal) is computed in both directions: `dist/` files whose hash
does not match or that are not recorded in `dist/manifest.json`, **and** manifest entries whose source file
in `dist/config/` no longer exists. `doctor` (`checks.bundle_integrity`) first validates the shape of each
manifest entry: an entry that is not an object, lacks `path`, or lacks a 64-hex `sha256` makes the check
stop with `error` + `malformed` (the entry indices), which `doctor` turns into an issue — not local
install drift.

**Dry run is the default, deliberately.** This script writes to the global agent config — read by every project on that machine. A mistake here is not confined to one repo.

What `--apply` does:

| Target | Strategy |
|---|---|
| `~/.claude/CLAUDE.md` | replace content **between the** `WORKFLOW-MAIN-AGENT` **markers** — handwritten text outside the markers is preserved. File does not exist yet → **only the marker block** is written |
| `~/.claude/skills/*.md`, `~/.claude/commands/*.md` | replace (template) |
| `~/.claude/hooks/*` | replace; one set for all OSes: `<hook>.sh` launcher, `<hook>.py` logic, `workflow_hooks.py`, `intent-map.json`. `.ps1` files from old releases are deleted as stale files (edited ones are left alone and reported) |
| `~/.claude/settings.json` | add the workflow's hooks and `statusLine`; user hooks and keys are preserved (see "Registered hooks") |
| `~/.config/opencode/AGENTS.md`, `~/.codex/AGENTS.md`, `~/.agy/AGENTS.md` | replace content between the `WORKFLOW-SECOND-AGENT` markers; file does not exist yet → only the marker block. That block applies only to runtime calls (instructions opened with `[WORKFLOW_AGENT]`); provider sessions the user runs directly ignore it |
| `~/.claude/.workflow-install-mode.json`, `~/.claude/.workflow-installed.json` | the selected intent mode and the ledger of files written by install (for cleaning up stale files) |
| `~/.config/opencode/opencode.{json,jsonc}` | additive merge for general config; the workflow's `agent.plan` permission is enforced, other user keys are preserved |
| `~/.config/opencode/agents/*.md` | replace — one global subagent roster, used by every project managed by the workflow |
| `<project_root>/opencode.json` | **not** the installer's job — installed and refreshed by `init`/`upgrade` (the secret-file deny rule is enforced every time) |

Everything that will be overwritten is backed up first to `~/.claude/backups/install_<timestamp>/`.
Receipt schema v2 records the before and after hash of every file created or
changed, including `settings.json` and the intent mode file. Rollback validates every
destination and backup before writing anything; a single change after installation makes
rollback stop with a conflict, rather than overwriting user edits or doing a partial rollback.
The receipt (`~/.claude/backups/install_<timestamp>/install_receipt.json`) is written **incrementally**:
once before any destination changes, then again after each recorded step,
with `complete: false` until the final write. Each entry contains `action`, `key`, `dest`,
`backup`, `pre_sha256`, and `post_sha256`, and is recorded after its file has finished writing.
An install that dies midway (locked file, full disk, Ctrl+C) still leaves a receipt:
`--rollback` accepts it, prints `NOTE: that install was interrupted; undoing the steps it
recorded.`, and undoes only the recorded steps. A step that died while writing has no
entry — its backup stays in the backup directory and is copied back manually. An apply that
changes nothing leaves no receipt.
The receipt covers the global install targets and the bundle. Stateful init/upgrade of `.workflow/`
and adding `.workflow/` to `.gitignore` are not part of the installer rollback because they can
hold session and project state that is unsafe to delete automatically.

#### Seed `config/second_agent.seed.json`

`config/second_agent.seed.json` is in `.gitignore`. The installer **rebuilds it on every
`--apply`** from `second_agent.example.json` — this is the file `init` copies into each new
`.workflow/second_agent.json` (`adapters/install/opencode_install._copy_provider_config`,
with the example as the last fallback). The runtime itself never reads it: a project
without `.workflow/second_agent.json` is rejected with `provider_config_missing`, a broken file is rejected with
`provider_config_invalid`. There is no longer a silent fallback to machine-level config.

Only the CHOICES (`provider`, `default_model`, per-route models) are carried over from the previous seed;
every other key is rebuilt from the example, so no stale keys carry over.
Order in which the choice is determined:

| Condition | What happens |
|---|---|
| `--provider NAME` (optional `--model ID`) | used as is, zero questions |
| a previous seed exists | its old choice is reused on top of the new example |
| `--apply` from a terminal, no seed yet | the installer **asks** for the provider, then the model |
| non-interactive, no seed yet | `second_agent.example.json` is copied as is |

The old `config/second_agent.json` (written once, never refreshed — the source of
`opencode/mimo-v2.5-free` being copied into new projects) is renamed to
`second_agent.json.retired`; its choice is **not** carried over and is mentioned in a warning.

The non-interactive path is not an oversight: `tools/e2e/e2e_installer.py` and CI call
`install.py` without stdin, and a prompt there would **hang** the run, not fail it.

The prompt shows each provider along with whether its CLI is on `PATH` and whether it requires
opt-in (`AI_PROXY_<PROVIDER>_OPT_IN`), then a shortlist of the selected provider's models with
the reasoning efforts each accepts. Blank on the provider cancels the selection
(the example is copied). Blank on the model leaves `default_model` empty — for opencode that is
rejected at call time (`model_unset`), because without `-m` opencode uses the last model
used on that machine. Model ids outside the shortlist are still accepted with a warning.

The model is pinned to `SELECTABLE_ROUTES` (`explore`, `plan`, `analyze`, `verify`, `e2e_spec`);
`sweep` is left alone. `routes` is edited per key via `_merge_routes` so that per-route `timeout_seconds`
and `agent` are not discarded.

The seed is **not** part of the install receipt, for the same reason as `--set-env`: a receipt entry without a
backup would be DELETED on `--rollback`, while this file holds user edits after it is
created.

`--check` reports it as a separate `second agent default:` line — `NOT SET`,
`UNREADABLE`, or `<provider>, model=<id>` — and it is deliberately **outside** the drift count:
this file has no counterpart in `dist/`, so it has no source to drift from, and its absence
is not breakage (init still works via the example).

When the installer is run from inside a project that already has `.workflow/`, it performs an **in-place upgrade**: the old workspace layout is migrated to `.workflow/data/` (with a backup), scripts are regenerated, the config version stamp is updated, `<project_root>/opencode.json` is refreshed, and `sessions/` is preserved. A new workspace is not scaffolded by the installer — use `python main.py --command init --work-dir DIR` (skill `/.init`). The workspace upgrade is refused while there is still an active job; the global install still completes and the warning must be checked.

### Statusline (second agent badge)

The installer installs `~/.claude/hooks/workflow-statusline.sh` (along with its `.py`) and registers it
as `statusLine` in `settings.json`.

`settings.json` is **additive**: only keys that do not exist yet are written, and since 3.8.0 the template
carries only `hooks` and `statusLine` — `model`, `enabledPlugins`,
`extraKnownMarketplaces`, `effortLevel`, and `permissions.defaultMode` are no longer planted
(those are preferences, not workflow requirements; ones already planted by old releases are left alone). Already have
your own `statusLine` → yours wins, the badge does not appear, and the collision is reported in the
install output. Delete that key and rerun `install.py --apply` if you want to use
the shipped one.

The script file itself follows the `~/.claude/hooks/*` rule in the table above: **replace**,
with a backup first. Have your own `workflow-statusline.sh` there → it is overwritten
(its copy is in `~/.claude/backups/install_<timestamp>/`). What is preserved is the
`statusLine` key in `settings.json`, not the file in `hooks/`.

```
agent-workflow | Second Agent 15.2M tok (14.5M cached) / 8 calls | Saved 642.7k tok
```

| part | meaning |
|---|---|
| `Second Agent` | all tokens processed by the provider for this session, cache reads included. `actual_*` takes precedence; the `chars//4` estimate is only a fallback for rows the provider did not count |
| `(… cached)` | the cache-read share within that number. Printed because on agentic runs it is ~95% of the total — the same context is resent at every internal step — and a number that large without an explanation next to it reads as a bug, not a fact |
| `calls` | the number of **commands**, not rows. A continuation writes one row per invocation; counting rows would make the number jump on every retry |
| `Saved` | what the second agent handled that **never** reached the main agent's context: fresh input (files and tool output it read) plus reasoning (inside the output, never becomes text). Cache reads are excluded — that is the same context resent, not new material. Its answer text is also excluded, because it **does** reach here |
| `~` | some row fell back to the `chars//4` estimate. A session whose rows were all counted by the provider does not carry this mark — nothing in it is guessed |

Both numbers are always shown, including when they are zero. A missing segment reads as broken, not as empty.

The numbers are taken from `<project_root>/.workflow/data/usage.jsonl` (or `.workflow/usage.jsonl` on a workspace that has not been upgraded). The statusline recomputes
on every render. The Claude session
is mapped to `MAIN_SESSION_ID` via `~/.claude/session_registry.json`, written by
`session-bind`. No `usage.jsonl` yet, or the session is not mapped yet, the badge only shows
the project name — it never fails the prompt.

**All numbers are session-scoped.** `/clear` and `/compact` give a new `MAIN_SESSION_ID`
(`resume` reuses the old one), so all three return to zero together. That is deliberate:
a project-lifetime number next to a session number reads as a ratio that was never measured —
right after `/clear`, the bar once read `Second Agent 0 | Saved 129.5k`.

On the size of the numbers: in one real session the provider processed 15.2M tokens, but 14.5M of
them were cache reads — only 595k were fresh input. That number is honest about what was processed,
not about cost: cache reads are billed far cheaper, and the runtime deliberately has no price
table, so the badge never states a monetary amount.

`Saved` measures compression of **work**, not compression of the answer. In the example session: 594,684
fresh input tokens (files read, tool output) plus 48,050 reasoning = 642,734
processed by the second agent, and none of it entered the main agent's context. What reached here
was only 34,018 tokens of answer text, and that is deliberately **not** claimed as savings — claiming it
would mean crediting the main agent for not reading what it just read.

Cache reads are also **not** included in `Saved`, even though they are included in `Second Agent`. The two numbers
answer different questions: the first is "how much was processed", the second is "how much
would occupy the main agent's window if it did the work itself". The same context
resent eighty times counts once for the second question. Including it
would make `Saved` 15.1M — 99.8% of the number next to it, i.e. one number printed
twice. As 642.7k it is 4% of the total, and the two numbers stop parroting each other.

The previous version used `premium_context_avoided_tokens` (full answer minus digest)
and produced 11.7k for the same session — 50× smaller. That field is now the fallback
for rows the provider did not count, because on such rows the `chars//4` estimate
measures **prompt** length, not how much was read. Fallback rows turn on `~`.

### Registered hooks

| Event | Matcher | Script | Function |
|---|---|---|---|
| `SessionStart` | `startup\|resume\|clear\|compact` | `session-bind` | binds the Claude session to `MAIN_SESSION_ID` |
| `UserPromptSubmit` | — | `intent-gate-set` | auto-intent mode only: sets the gate marker for delegated prompts |
| `UserPromptSubmit` | — | `task-events` | local skill from the prompt; HEAD snapshot |
| `PreToolUse` | `mcp__.*\|Read\|Grep\|Glob\|Bash\|PowerShell\|Write\|Edit\|MultiEdit\|NotebookEdit` | `intent-gate-check` | blocks gather tools while the gate marker exists; Bash and PowerShell go through one tokenizer and one rule: only a single plain call `[bash] <root>/.workflow/{run,check,inspect}.sh` passes (each word plain or `'...'`; `$`, backtick, `;`, `|`, `&`, redirect, newline only inside single quotes; double quotes, `bash -c`, another interpreter in front, `run.ps1`, paths outside the root, `..`, and look-alike names are rejected); Write/Edit pass except to that runner script; a marker with intent `verify` lets the test-selection path through (see "Intent mode") |
| `PostToolUse` | `Skill\|Edit\|Write\|MultiEdit\|NotebookEdit` | `task-events` | skill via the `Skill` tool, edited files |
| `Stop` | — | `graph-refresh` | regenerates the graph after a turn that implemented something (detached) |
| `Stop` | — | `task-events` | commits since the HEAD snapshot |

Hooks are recognized by their script name. On install, the workflow's hooks are refreshed per event,
user hooks are left alone, and workflow script commands that this release no longer registers on
an event are removed from that event (for example `task-events` on `PreToolUse` from the 3.8.0 draft).
The command in the template is the same for all OSes, `{{HOOK_BASH}} "{{HOME}}/.claude/hooks/<hook>.sh"`;
the installer fills `{{HOOK_BASH}}` with `bash` on POSIX and the absolute Git Bash path (quoted) on
Windows, because a plain `bash` there can fall through to WSL bash in `System32`. Without Git Bash on
Windows, or without `bash` on PATH on POSIX, `--apply` is refused (exit 6) before writing anything.
Maintainer and e2e tools that run hooks or the runner also refuse (rather than guessing a plain
`bash`) when bash is not found. Old commands of the form
`powershell ... .ps1` are recognized by their hook name and replaced.

### Uninstall

`python install.py --uninstall` (dry run), then `--uninstall --apply`:

- the `WORKFLOW-MAIN-AGENT` / `WORKFLOW-SECOND-AGENT` block is cut from CLAUDE.md and each
  AGENTS.md; user text above and below it stays. A file whose content is only that block is deleted;
- skills, commands, hooks, and provider agents are deleted **only** if their content is still exactly what
  install wrote (ledger or receipt); files that have been edited are left alone and reported;
- `settings.json` loses the hook commands that run workflow scripts (shipped or
  retired) and the workflow `statusLine`; other keys stay, including those planted by old releases.
  A command is considered the workflow's only if its script is in the installed hook folder,
  `<home>/.claude/hooks/<nama>.sh` (or `.ps1` from old releases; `<home>` = the install home, `~`, `$HOME`,
  `%USERPROFILE%`, `$env:USERPROFILE`, or a user-home-shaped path), and its name is shipped or
  retired. A user script with the same name in another folder (`~/tools/task-events.sh`) is touched by neither
  install nor uninstall; a single command that runs our script plus another script is considered
  the user's;
- CLAUDE.md: only the seam where the block was cut is tidied (one blank line); blank
  lines elsewhere in the file stay byte-identical;
- the intent mode and ledger are deleted.

`settings.json` is cleaned **before** hook scripts are deleted, and is read leniently (BOM, cp1252). If
it cannot be parsed or is not a JSON object, all `hooks/` scripts are left alone and uninstall
warns about it, so no hook ever points at a missing file. Instruction files whose
markers are unpaired (START without END, END without START, END before START, or more than
one block) are refused at install (action `refused` + a warning with the path and both markers) and
left alone at uninstall.

Not touched: `config/second_agent.seed.json`, `AGENT_PATH` from `--set-env`, and
`.workflow/` in each project. All changes go through the same backup + receipt as install,
so `python install.py --rollback --apply` undoes an uninstall.

### Intent mode

The installer default is **auto-intent**: natural language can be mapped to commands and
the `UserPromptSubmit` hook activates the pre-flight gate. The entire workflow block applies to
every message.

`--only-command` makes the workflow **active only when invoked**: the installed block opens with
the "Cakupan aktif" ("active scope") stanza — the workflow rules (caveman style, Output Contract, gate, `[NEXT]`,
Graphify, Global Forbidden) apply only after the user invokes `/.<command>` or a workflow
skill, and for as long as that command runs. A message without a prefix is ordinary chat that
follows the user's instructions outside the block (the user's CLAUDE.md, memory, project). The
`intent-gate-set` hook is not registered; the passive hooks (`session-bind`, `task-events`,
`graph-refresh`) and `statusLine` remain, because they only record. Run `--auto-intent`
to restore the default mode. Without either flag, an upgrade keeps the previous mode.

A turn the user did not write is ignored by `intent-gate-set`: it neither sets a marker nor
removes a marker set by a user prompt in the same turn (DEC-045, DEC-059). Claude Code sends such
turns via `UserPromptSubmit` too. A turn counts as not the user's when its `source` is `system`,
`loop_wakeup`, `schedule_wakeup` or `poll_event`; when it carries `agent_id` (it runs inside a
subagent); or when, after any leading `<system-reminder>` blocks are removed, it is empty or
opens with `<task-notification>`, `[SYSTEM NOTIFICATION`, `<agent-message` or
`Another Claude session sent a message` (a native subagent's hand-back). `source` `user` and
`sdk` stay classified, and a user prompt that only mentions one of these shapes mid-sentence
still arms the gate. Previously a task name, an agent report or a snippet of output could match
the NL map and set a gate nobody asked for, which also blocked the main agent's own subagents
during `/.execute`. A real marker still binds subagents: their gather calls go through
`intent-gate-check` like the main agent's. The marker records `source`, `has_agent_id` and
`prompt_id` beside `prompt_excerpt`, so a false arming names its origin.

Up to 3.8.0, `intent-gate-set` read the prompt from the `user_prompt` field, which Claude Code never
sends (the field is `prompt`). As a result the runtime gate was never active and only the
prompt layer worked. Since that fix, in auto-intent mode `intent-gate-check`
actually blocks gather tools after a prompt that maps to a delegated command until
`.workflow/run` is called; the escape is still `WORKFLOW_LOCAL_MODE=1` or `local_mode.flag`.

The runner passes only when it is the command itself: the first word is this project's
`.workflow/{run,check,inspect}.sh` path, or the second word after a plain `bash` (no
flags). The Bash and PowerShell tools are read by the same tokenizer: each word must be plain (letters, digits,
`_ . / : = + % - ^`, and `~` mid-word) or wrapped in single quotes, which are literal in both
shells. That is why a task may contain `$`, `;`, `|`, `&`, `<`, `>`, and parentheses as long as they are inside
`'...'`; double quotes are rejected because both shells expand `$` inside them, and an apostrophe
inside the task cannot be written. A runner path that appears as an argument of another command
(`python -c "..." x/.workflow/run.sh`) and `run.ps1` (retired) are still blocked.

The `command` field in the marker selects the pass rule. For `verify`, main_agent must select tests
before delegating, so three things pass: a plain `git diff` (same word rules as the runner)
with `--name-only`, `--name-status`, or `--stat[=N]`, plus optional `--cached`, `--staged`,
`--relative`, `--no-renames`, `--no-color`, `--`, refs, and paths (other options such as `-p` or
`--output` are still blocked); Read of `<root>/.workflow/config.json`; Read/Write of
that session's `sessions/<id>/verify/tests.json`. Dependents can only be inferred from the file
names in the diff. The `PreToolUse` matcher includes `Write`/`Edit`/`MultiEdit`/`NotebookEdit`, but
the hook only rejects writes to the `.workflow/{run,check,inspect}` runner scripts (any extension)
while the marker exists (rewriting the runner and then running it would turn the runner allowlist into a free command);
writes to other files, including `tests.json`, pass.
The hook is tested through the `.sh` launcher that is actually installed, on Windows through Git Bash and on Linux.

For every `command` in the marker, Read of a skill definition also passes: an absolute path with no
`..` segment, ending in `.md`, the file exists, and its real path (all symlinks/junctions followed) is
inside the real path of `~/.claude/skills`. A symlink may point anywhere inside the skill folder,
not outside it. The reason: the Skill tool tells the agent to read `~/.claude/skills/<name>.md`
after the prompt that invoked it has armed the gate, so the dispatch instructions would be locked
until after dispatch (DEC-039). Grep/Glob over that folder, non-`.md` files, and relative
paths are still blocked.

For every `command` in the marker, a **precision Read** also passes (DEC-043): a file inside the
project root (relative paths resolved against the root, symlinks followed), not under
`.workflow/`, spanning at most 200 lines — `limit` a number 1–200, or no
`limit` on a file that is itself ≤ 200 lines (files > 1 MB are not counted, so they do not pass). At
most 2 per marker; the count is in `runtime/delegated.reads`, keyed to the marker's `set_at`,
so a new marker starts from zero. This is the enforced number for the "small
slice, one/two anchors" exception in the `CLAUDE.md` Pre-flight gate rule: file:line attribution by the main
agent itself, not evidence gathering. A 3rd Read, a span > 200 lines, a large file without
`limit`, a file outside the root, Grep, Glob, MCP, and shell are still blocked, and the block message
cites this rule. A marker without `set_at` (not written by `intent-gate-set`) does not open this lane.

`dist/config/claude/CLAUDE.md` deliberately contains **both** mode stanzas — `COMMAND-ONLY` and
`AUTO-INTENT`, each wrapped in `<!-- <MODE>:START -->` / `<!-- <MODE>:END -->` markers.
That file is the source before trimming, not the installed result. On `--apply`, the installer
(`_apply_intent_mode`) drops the unselected mode stanza along with both modes' markers,
so the installed `~/.claude/CLAUDE.md` contains exactly one stanza. The two stanzas live in
one file, not two file variants, so the difference between them is visible in review.

`--check` determines scope from the cwd: run inside a project that has `.workflow/`, it
also checks the project boundary (`<project_root>/opencode.json`). Outside a workspace, that scope
is reported as `SKIPPED` — not silently left out and then reported READY. The boundary checked is
only that of the provider the project runs (`provider` in `.workflow/second_agent.json`),
the same one `init` installs: a codex project is not asked for `opencode.json` and is reported as
`project scope: SKIPPED — the project runs codex ...`. A project that names no provider
falls back to the default (opencode), so its boundary is still checked.

### Extractor (maintainer side)

`dist/` is generated from the maintainer's live config:

```bash
python tools/maintain/extract_config.py --dry-run
python tools/maintain/extract_config.py
```

Its security posture, in order of importance:

1. **Allowlist, not blocklist.** The agent home contains `.credentials.json`, a `history.jsonl` of hundreds of KB, session transcripts, and project paths. A blocklist ships anything it forgot to name; an allowlist ships only what is named.
2. **Fail closed on secrets.** Every byte is scanned for credential patterns. A single hit **aborts the whole run and writes nothing** — a warning would be read, ignored, then committed. A secret in git history is practically impossible to revoke.
3. **Read-only.** The extractor never writes outside `dist/`.

Absolute paths are redacted to `{{HOME}}` / `{{PROJECT_ROOT}}`; the installer restores them.

### E2E

```bash
python tools/e2e/e2e.py          # local + installer: no delegated run/quota
python tools/e2e/e2e.py --full   # plus delegated commands: minutes + real quota
```

`--full` is opt-in. Delegated commands use the same rate-limit budget the workflow itself needs; a suite that silently burns it is worse than no suite.

The installer is tested against a **temporary HOME**, not the machine that produced `dist/` — installing onto the origin machine only proves idempotence, it never exercises the *create* path a new user faces.

`SKIPPED` is not `PASS`, and is reported separately.

---

## Bootstrap

`main.py` lives in this repo, **not** in the target project. For the first init, the runtime needs to know where it is. After init, `.workflow/config.json` stores its absolute path, so `AGENT_PATH` is no longer required.

**Windows (persistent, run once):**

```powershell
[Environment]::SetEnvironmentVariable("AGENT_PATH", "C:/path/to/agent-workflow/main.py", "User")
```

Close and reopen the terminal so the variable takes effect.

**Windows (current session only):**

```powershell
$env:AGENT_PATH = "C:/path/to/agent-workflow/main.py"
```

**macOS / Linux (persistent):**

```bash
echo 'export AGENT_PATH="$HOME/path/to/agent-workflow/main.py"' >> ~/.bashrc   # or ~/.zshrc
source ~/.bashrc
```

**macOS / Linux (current session only):**

```bash
export AGENT_PATH="$HOME/path/to/agent-workflow/main.py"
```

Verification:

```bash
test -f "$AGENT_PATH" && echo OK
python3 "$AGENT_PATH" --help
```

```powershell
Test-Path $env:AGENT_PATH
python $env:AGENT_PATH --help
```

---

## Init in the target project

```bash
python3 "$AGENT_PATH" --command init --work-dir /path/to/target-app --pretty
```

```powershell
python $env:AGENT_PATH --command init --work-dir "C:/path/to/target-app" --pretty
```

`init` is idempotent: it creates the scaffolding that does not exist yet and regenerates the three runner scripts. A workspace created by an old build (different version or old layout) is upgraded directly by init; if a live job exists, the upgrade is skipped and the command to run is named.

- `.workflow/config.json` — the absolute paths of `main.py`/`check.py`, so the runner can find the tool without relying on `AGENT_PATH`
- `.workflow/second_agent.json` — a project-local copy, which you may edit
- runner scripts `run.sh` `inspect.sh` `check.sh` — bash on all OSes (Git Bash on Windows); there is no `.ps1` any more
- an empty `.workflow/data/sessions/` — per-session state is created lazily on the first delegated call. All internal files (streams, store, cache, sessions, reports) are in `.workflow/data/`; the `.workflow/` root holds only the files you edit, the scripts, and `current/`
- a `.workflow/` entry is added to the project root `.gitignore`

The `.sh` scripts are given the executable bit when created. If the repo is moved via media that drops mode bits:

```bash
chmod +x .workflow/*.sh
```

**Portability limitation.** The scripts bake in **absolute paths from the machine where `init` was run** — the path to `main.py`, `--work-dir`, and the detected Python interpreter. None of them is portable across machines.

Paths are written with forward slashes (`C:/...` on Windows), which both Git Bash and Windows Python read. `init`/`upgrade` **delete** `run.ps1`/`inspect.ps1`/`check.ps1` left over from old builds: no generator maintains them any more and the gate does not let calls to them through, so keeping them only makes them look usable. A `run.sh` from another machine still contains that machine's absolute paths. Every team member runs `init` (or `upgrade`) themselves in their own environment; that also applies when moving between WSL and native Windows.

`doctor` compares the script contents on disk with the generator output and reports it in `run_script_drift` — `missing`, `content_differs`, or `retired_leftover` (`.ps1` from an old build). All of them count as issues: these scripts are the only entry point, and one that has drifted will route commands the CLI no longer accepts.

## Workspace upgrade

The recommended path after pulling a new agent-workflow version:

```bash
cd /path/to/target-app && python /path/to/agent-workflow/install.py --apply
```

That command updates the global config and upgrades the workspace in the cwd. A project that does not
have `.workflow/` yet is scaffolded via `init`, not the installer. For the workspace only:

```bash
python3 "$AGENT_PATH" --command upgrade --work-dir /path/to/target-app --pretty
```

```powershell
python $env:AGENT_PATH --command upgrade --work-dir "C:/path/to/target-app" --pretty
```

`upgrade`:

- refuses to run while a delegated job is active;
- regenerates the runner scripts and repoints the tool paths;
- migrates the v3.4.2 keys (`opencode_*` → `provider_*`, `.workflow/opencode.json` → `.workflow/second_agent.json`) once, moving their values;
- **layout migration** (once): the internal contents of `.workflow/` are moved to `.workflow/data/` after a backup to `data/backups/<stamp>/` (the last 3 are kept), archived evidence paths are rewritten, old leftovers (`state.json`, `runtime/`, `logs/`, locks) are deleted, an object-format `secrets.json` is converted to a list. Contents are first moved to the staging `.workflow/data.migrating/` and then renamed to `data/` in a single step. Staging left over from an interrupted attempt is restored first; if it contains a name that also exists in the `.workflow` root, the upgrade stops with `MigrationConflict` and **moves nothing** — keep one copy of each entry (compare them first), delete the other, then rerun `upgrade`. After that check, a failure has three distinct outcomes:
  - **failure before the rename, clean rollback** → everything is returned to `.workflow/`, the backup is kept as `.workflow/migration-backup-*`, message `workspace migration failed and was rolled back`;
  - **failure before the rename, INCOMPLETE rollback** (`MigrationRollbackIncomplete`) → every reverse step is still attempted and every failure is recorded; entries that cannot be returned stay in `.workflow/data.migrating/` (staging is kept, not deleted), message `workspace migration failed and the rollback is INCOMPLETE: ...`. Move those entries to `.workflow/` manually, or rerun `upgrade` — it restores leftover staging first. A pre-migration copy may exist in `.workflow/migration-backup-*`;
  - **failure after the rename** (`MigrationIncomplete`) → nothing is returned; the workspace is already in `data/` and works. The message `workspace migration incomplete: ...` names the unfinished step — fix the cause (usually a file held by an editor/antivirus) and rerun `upgrade`; it resumes from that step;
- `config.json` is trimmed to **overrides only**: values equal to the default and retired/unknown keys are dropped and reported; `second_agent.json` is still backfilled additively;
- preserves user values and all of `sessions/`;
- sends no prompt, does not call second_agent, and does not run verify.

Ordinary commands do not run a **full workspace upgrade**. A delegated call can backfill `.workflow/config.json` when loading state, but it does not regenerate scripts or backfill `.workflow/second_agent.json`. Because that backfill also updates the version marker, the runner warning and the `doctor: NEEDS_UPGRADE` status can disappear after the first delegated call even though those two parts are still stale. So run `upgrade` explicitly before using a workspace from an old version.

---

## Daily usage

main_agent calls **one** runner script — it does not assemble a Python command itself — via the
Bash tool, on all OSes:

```bash
bash '/path/to/target-app/.workflow/run.sh' explore 'find the auth entry point' '<MAIN_SESSION_ID>'
```

Each argument is wrapped in single quotes: that is the form the pre-flight gate lets through, and its content is literal
(`$`, `;`, `|`, parentheses are allowed; apostrophes are not). On Windows the path is `C:/...`.

The third argument is the session id. It **must** be passed: without it the runner uses the `"default"` fallback, which is resolved through the tool's global cache, so several main_agents can share an effective ID, overwrite each other's state, and block each other's jobs. The script prints a warning to stderr when this happens.

The script is blocking and returns the same JSON as the CLI.

### Auto-intent

The `/.` prefix is still supported, but not required. Main_agent maps natural language to a command and shows one transparency line before running it:

```text
[INTENT] analyze — user asks for a logic audit
```

Mapping examples: location/flow questions → `explore`, cause/assessment → `analyze`, new feature → `plan`, implementation → `execute -y`, results already produced → `verify`, testing a flow in the browser → `verify-browser`, and diff blast radius → `sweep`.

Its limits:

- an explicit command such as `/.plan` always wins over a guess;
- ordinary conversation is not forced into a command;
- an ambiguous delegated intent may trigger one clarifying question;
- destructive or irreversible actions must not auto-fire without confirmation.

Auto-intent is a main_agent prompt contract, not a Python parser feature, and has no `auto_intent` key.

### Implementation options in `/.plan`

After `[PLAN]`, main_agent appends `[OPTIONS]` containing at most three approaches that stay within scope. Each option includes pros, cons, effort, risk, and evidence attribution; exactly one option is recommended. When only one approach is feasible, alternatives must not be invented.

Second_agent only supplies evidence and reasoning. The runtime does not create or validate the `[OPTIONS]` block.

<a id="kontrak-lapisan-prompt"></a>
### Prompt-layer contracts

The contracts below live in `dist/config/claude/CLAUDE.md` and its skills
(`dist/config/claude/skills/*.md`), not in Python. The runtime never sees the raw user message
or main_agent's final answer, so the one enforcing these contracts is main_agent
itself. The only partial exception is the pre-flight gate, which is also enforced by the host
agent's hooks. The per-contract reason why Python cannot enforce them: `docs/runtime-contracts.md`,
section "Contracts the runtime cannot enforce".

| Contract | Content | Enforcer | Source |
|---|---|---|---|
| Intent mode | One of two, chosen by the installer. `AUTO-INTENT`: natural language is mapped to a command and announced with one `[INTENT]` line without waiting for an answer. `COMMAND-ONLY`: commands run only via the `/.` prefix; no prefix = ordinary conversation, may suggest one line and then stop | main_agent | `CLAUDE.md`, stanza `AUTO-INTENT` / `COMMAND-ONLY`; see [Intent mode](#intent-mode) |
| Pre-flight gate | Once intent resolves to a delegated command (`explore`, `plan`, `analyze`, `verify`, `verify-browser`), the next step must be `.workflow/run`. Gather tools (Read/Grep/Glob/Bash/MCP) are forbidden before that, except under `[LOCAL_MODE]`, proxy failure, or a small precision slice | main_agent + hooks `intent-gate-set` / `intent-gate-check` (auto-intent mode only; fail-open) | `CLAUDE.md`, "Pre-flight gate"; `dist/config/claude/hooks/intent-map.json` |
| Output contract | Explore and `sweep`/`doctor` = RELAY the runtime result as is. Plan/analyze = SYNTHESIS by main_agent: three-part confidence (`problem_understanding`, `root_cause`, `solution_path`), per-claim attribution, `open_questions` kept separate from `uncertainty`. Required fields are still shown even when empty, with the reason | main_agent | `CLAUDE.md`, "Output Contract" and "Plan/analysis output" |
| `[OPTIONS]` | See [Implementation options in `/.plan`](#implementation-options-in-plan) | main_agent | `skills/plan.md` |
| New feature via plan | A command that describes new behavior ("buat agar …", "tambahkan …", a list of fixes) without an active plan is mapped to `plan`, not `execute`. Execute without an active plan stops and suggests `/.plan`. Requests for a spec/explanation are answered directly, without an unrequested `/.verify` (auto-verify after `/.execute` when `auto_verify_after_execute=true` and after `/.refactor` still applies) | main_agent | `CLAUDE.md`, NL map and tie-break; `skills/execute.md` (DEC-046) |
| `/.execute -y` | Without `-y` it only shows `[EXECUTION SCOPE]` and then stops. With `-y` it edits only files in scope; a need outside scope = stop and ask for instructions. Approval is only `/.execute -y` from the user after a `proceed` plan; an execute sentence without `-y` shows the scope and asks for `-y`; an AskUserQuestion answer to `open_questions` is not approval | main_agent | `skills/execute.md`, `skills/plan.md` STEP 3 |
| Verification after execute | `commands.auto_verify_after_execute` is reread on every `/.execute`. `false` (default): status `implemented`, `verification: not_run`, the word "done" and similar are forbidden. `true`: `/.verify` is part of `/.execute` | main_agent | `skills/execute.md`; `CLAUDE.md`, "Execution rules" |
| Proxy failure | `ok:false`, an invalid-evidence `error_type`, or content that is not evidence → stop, print `[PROXY GAGAL] …`, ask whether to continue with `/.local` (yes/no), then wait. Automatic local fallback is forbidden | main_agent | `CLAUDE.md`, "Proxy failure" |
| Destructive actions | Commit, delete, or writing outside the project must not run from a guessed intent; confirmation is required. Writing to another project (Edit/Write or Bash/script) needs explicit approval for that path; the hook does not stop it (see [limitations](limitations.md)) | main_agent | `CLAUDE.md`, "Intent detection" and "Global Forbidden" |
| `/.commit` staging | An existing staged set is used as is. With nothing staged, main_agent stages changed tracked files and untracked files Git does not ignore, one quoted path per argument (never `-A` or `.`), and skips a path whose lowercased, `/`-separated form matches the canonical secret-file list of `core/policy/secret_patterns.py` (`SECRET_READ_PATTERNS`, minus `SECRET_READ_ALLOWLIST`), the same list the providers' read boundary uses; a test keeps the skill's copy identical. It shows staged, newly tracked, and skipped paths with the message, and commits only on `yes`; `no` unstages only what it staged (`git reset -q --`, which also works before the first commit). The skip list is a prompt rule over file names, not a content scanner | main_agent | `skills/commit.md` (DEC-060, DEC-061) |

The exact content stays in the source files; this table is a map, not a copy. When a source file and this
table differ, fix one of them in the same change.

---

## Command

| Command | Kind | Needs prompt | Notes |
|---|---|---|---|
| `init` | local | — | scaffold `.workflow/`; regenerate runner scripts |
| `upgrade` | local | — | migrate the `.workflow/data/` layout, refresh the workspace, override-only config, preserve sessions |
| `doctor` | local | — | readiness check, writes `reports/doctor.json` |
| `clean` | local | — | prune jobs, stale/duplicate facts, runtime-lock guards of sessions with no live lock (`sessions/<id>/runtime/lock.guard`; a guard currently held by another process stays), old sessions |
| `inspect` | local | — | list jobs for the current session |
| `provider` | local | — | read/change the second_agent provider and reasoning effort; picker with no arguments, direct with `<provider> <model> [effort] [routes]` |
| `report` | local | — | summary of the workspace quality stream (`quality.jsonl`), including `e2e` (runs), `e2e.drafts` (drafts), and `graph_refresh` (Stop hook graph cost per outcome), and from `usage.jsonl`: `evidence_reuse` (explore/analyze/plan commands served by a stored artifact divided by commands eligible for reuse, continuations counted once; `by_outcome` breaks it down per `reuse_outcome`: `hit`, `no_prior`, `stale`, `unreadable`, `error`, `not_offered`, `disabled`, and `unrecorded` for rows before contract v3), `provider_cache` (cached-input share of the provider's measured input: `all`, `first_call`, `continuation`, `fresh_thread`, `resumed_thread`; rows whose adapter does not report `resumed` are not included in the two thread buckets), `provider_threads` (per provider: `resumed`, `fresh`, `unknown`, and `thread_changed` = a resume call answered in a different thread, a sign the session lost state; rows before contract v3 are not counted), `by_effort` (per `<command>/<effort>`: command count, median duration, median input/output tokens — each median only from commands whose value was measured, unmeasured output is not counted as zero; `default` = no effort flag; rows before contract v3 are not counted), and `graph_leads` (`graph_status` per command, `refreshing_by_command`, median/max `graph_ms`); and from `tasks.jsonl`: `tasks` (see [Task telemetry](#task-telemetry)); and `provenance` (metric registry version, version of each `runtime.*` metric, tool commit, stream content hash — definitions in [`docs/evaluation/metrics.md`](evaluation/metrics.md)) |
| `audit` | local | — | read the session's `audit.jsonl` |
| `graph-meta` | local | — | graphify snapshot status: fresh, stale, or absent |
| `explore` | delegated | yes | codebase map, entry points, owners |
| `plan` | delegated | yes | evidence + reverse dependency trace |
| `analyze` | delegated | yes | in-depth analysis, zero code changes |
| `verify` | delegated¹ | yes | verification; depth set by `verify_mode` |
| `verify-browser` | delegated³ | yes | verification through a Playwright browser from a per-session request; `draft` stage then `run` |
| `sweep` | local | — | scan staged, unstaged, and untracked diff without OpenCode |
| `promote-validate` | local | yes² | validate a knowledge document; zero writes |
| `promote-verify` | local | yes² | freshness of each claim + reconciliation against existing documents |
| `promote-write` | local | yes² | the only write path into the knowledge directory |
| `submit` | job | yes | run asynchronously, return `job_id` |
| `await` | job | yes | submit and then wait for completion |
| `status` | job | — | requires `--job-id` |
| `result` | job | — | requires `--job-id` |
| `worker` | internal | — | used by the worker process, do not call manually |

¹ `verify` bypasses OpenCode entirely when `verify_mode` is `syntax`.

² All three `promote-*` stages take a **path to a JSON file** via `--prompt`, not the document itself: a single knowledge document easily exceeds Windows' 8191-character argv limit. The stages are split because user approval happens between them — the CLI cannot ask anything, so verification stops at a verdict, main_agent runs the review, and writing is a separate call that can only happen afterwards. `promote-write` refuses outside `policies.production_branch`.

³ `verify-browser` is a **delegated** command: it is registered in the DELEGATED registry and goes through the pre-flight gate like `explore`/`plan`/`analyze`/`verify`. It reads the **per-session request** (`.workflow/data/sessions/<session>/e2e/request.json`) on top of the project defaults from the `e2e` section in `config.json`. The work is split into three stages: second_agent drafts the spec (stage 1) and reviews the browser evidence (stage 3); in between, the **runtime player** — a child process run by the runtime (`python -m core.evidence.e2e.player`) — drives Playwright (stage 2). Neither main_agent nor second_agent runs the browser (read-only sandbox). The interview happens in main_agent only after the draft reports that the target is not configured yet. The `draft` stage carries no verdict; the `run` stage is finalized as `verify`, so its verdict and exit code match other verifications. An `[E2E SPEC]` section that fails validation gets one repair in the same provider thread (`meta.e2e.repair`); the only test attribute is `data-e2e`, via the `e2e` selector key — `testid` is rejected (**breaking**); the `repeat_failure` brake releases by itself when the project (outside `.workflow/`, via Git) or the runtime (`TOOL_VERSION` + the code digest of `core/evidence/e2e/`) has changed since the last failure, the `app` bucket streak only grows for failures with the same signature (page, field, condition), and it is recorded as `repeat_failure` knowledge for the next draft. Credential values typed literally in the task, draft, or `request.json` are found via a `secrets.json` lookup and turned back into `${NAME}` (`request.json` is rewritten; key names are reported in `meta.e2e.secret_literals`, values never are), and a `fill` into a password field without a placeholder → `spec_invalid`; failures caused by the scenario itself — a guessed selector that misses, any selector that matches >1 element, a write/navigation rejected by the run policy — have origin `scenario` and end as `incomplete: scenario_error`, are not retried, and do not count toward the brake; every non-pass run and every brake refusal carries `meta.e2e.diagnosis` (`cause`, `next_step`, `failed`, `fix_hint`, `reusable`), and reusable causes are stored as `failure_hint` knowledge; every draft writes a `kind: e2e_draft` row (validator error category, repair result) that `--command report` summarizes in `e2e.drafts`. Network: every run always records metadata of all page requests (order, URL without query, type, status, timing, size; no body/headers/cookies), the latest 500 rows plus a summary over all requests, in `report.json` → `network` and `meta.e2e.page_requests`, including `since_last_run` (changes compared with the previous run on the same origin). Selector order: candidates follow role+name > label > e2e > text > css, except two kinds that may lead — an `e2e` with `source` provenance and a `path:line` ref, and a `proven` selector recorded in browser knowledge for the origin, the route (the last `goto` path, identifier segments become `:id`: `/users/123` and `/users/456` are one route), and that step's `within` (checked against the store; a `proven` claim without basis or on another route/scope → `spec_invalid`); a `proven` selector that misses drops to the normal order, two consecutive runs that miss retire it (one run weakens an entry at most once); a step that fails after its selector matched is not a miss; a pass is recorded only from a run that passed on the first attempt. Network URLs are cleaned of `;…` path parameters and token-shaped segments (16+ alphanumerics without separators, 16+ mixed upper/lower case, 24+ with digits) to `:id`. Full contract: `docs/runtime-contracts.md`, section `/.verify-browser`.

There is no Python `execute` command. `/.execute -y` remains available as a user-facing command in main_agent; writing code is deliberately not delegated to the runtime or second_agent.

---

<a id="cli-langsung"></a>
## Direct CLI

Useful for debugging; the normal flow only needs the runner script. For the same blocking behavior as the runner, use `await`:

```bash
python3 main.py -c await --job-command explore -p "find the auth entry point" -s "main_app_20260723_090000" -w /path/to/target-app --pretty
```

```powershell
python main.py -c await --job-command explore -p "find the auth entry point" -s "main_app_20260723_090000" -w "C:/path/to/target-app" --pretty
```

Calling `-c explore`, `plan`, `analyze`, `verify`, or `verify-browser` directly only performs
`submit` and immediately returns the job payload. Fetch the result via `result`/`check`, or
use `await` as in the example above. `sweep` and other local commands finish directly.

| Argument | Alias | Meaning |
|---|---|---|
| `--command` | `-c` | see the command table |
| `--prompt` | `-p` | task |
| `--prompt-file` | | read the task from a file (alternative to `--prompt`) |
| `--session` | `-s` | main_agent session id |
| `--fresh-session` | | force a new session, ignore the session ID cache and evidence reuse |
| `--work-dir` | `-w` | target project root (default: cwd) |
| `--model` | `-m` | model override, format `provider/model_key` |
| `--job-id` | | for `status`/`result`/`worker` |
| `--job-command` | | the command run by `submit`/`await` |
| `--poll-interval` | | seconds between polls during `await` |
| `--poll-timeout` | | `await` wait limit; `0` = unlimited |
| `--pretty` | | indented JSON |

`--prompt-file` avoids shell escaping problems with long or multi-line tasks — a problem that takes a different shape in PowerShell and in POSIX shells.

---

## Configuration

### `.workflow/config.json`

Created at `init`, and contains **overrides only**: an empty/absent `commands`, `policies`, `e2e` section means every key uses the default built into the running build. Write only the keys you want to change; absent keys are never filled in automatically, so defaults that change in a later release reach this project too.

The following ten keys actually change the Python runtime's behavior:

| Key | Default | Meaning |
|---|---|---|
| `commands.verify_mode` | `"delegated"` | `delegated` = full second_agent verification. `syntax` = local parse check only. Unknown values fall back to `delegated` |
| `commands.verify_test_commands` | `[]` | test command prefixes the runtime may run for `/.verify` (e.g. `["python tests/run.py", "php artisan test"]`). Empty = no tests run; requested tests are reported as not run |
| `commands.verify_test_timeout_seconds` | `900` | time limit per test command |
| `policies.fact_relevant_limit` | `3` | maximum facts injected into each prompt |
| `policies.fact_recurrence_threshold` | `5` | number of **other** sessions that must report a claim before it is promoted |
| `policies.graph_leads_enabled` | `true` | inject a shortlist from `graphify-out/graph.json` |
| `policies.subagent_fanout_enabled` | `true` | request fan-out for the exploration/reasoning roles |
| `policies.production_branch` | `"main"` | the only branch where `promote-write` will write. Promoted knowledge describes what is live; a hypothesis from a feature branch must not enter the shared repo carrying that authority |
| `policies.knowledge_dir` | `"docs/project-knowledge"` | location of knowledge documents, relative to the project root. The only workflow artifact that is **tracked in Git** — its whole value lies in being shareable |
| `policies.knowledge_relevant_limit` | `3` | maximum knowledge documents included in one delegated prompt. `0` turns off the knowledge sidecar |

`commands.auto_verify_after_execute` is read and returned in `meta.policy`, but only main_agent can act on it because Python has no `execute` path.

Other main_agent instruction keys: `commands.allow_analyze_to_plan`, `commands.allow_explore_to_plan`, `commands.auto_sweep_after_execute`, `policies.workflow_prefix`, `policies.chat_mode_for_plain_text`, `policies.fallback_requires_confirmation`, and `policies.max_active_job_per_session`.

There is no `auto_verify_method` key. The two valid and orthogonal keys are:

- `commands.auto_verify_after_execute` — **when** verify is called; default `false`. When false, `/.execute` must report `verification: not_run`, with status `implemented`, and then offer `/.verify`.
- `commands.verify_mode` — **how deep** verify goes when called.

The retired key `commands.autoverify` is migrated on upgrade. A typo in any other key causes no error and falls back to the default.

### `.workflow/second_agent.json`

This project-local adapter file may be changed per project. Its name follows the ROLE, not the vendor — since v3.4.3 the second_agent provider is chosen through config, so this file no longer assumes OpenCode. A v3.4.2 workspace that still uses `.workflow/opencode.json` is migrated once on `upgrade` (values moved, old keys deleted). Do not confuse it with `<project_root>/opencode.json` and `~/.config/opencode/opencode.json`, which genuinely belong to OpenCode itself and keep those names.

At `init`, its source is `config/second_agent.seed.json` if present, or `config/second_agent.example.json` on a clean clone. This file MUST exist per project: without it (or if it does not parse) delegated commands are rejected, with no fallback to machine-level config. The loader fills in keys not yet stored with the source defaults in memory; `upgrade` writes them to the file additively. Its effective shape:

```json
{
  "provider": "opencode",
  "provider_command": "opencode",
  "provider_agent": "plan",
  "default_model": null,
  "timeout_seconds": 1800,
  "bootstrap_timeout_seconds": 180,
  "stall_threshold_seconds": 360,
  "idle_stall_seconds": 240,
  "probe_timeout_seconds": 45,
  "probe_recheck_seconds": 120,
  "job_poll_interval_seconds": 2.0,
  "routes": {
    "explore": { "model": null },
    "plan":    { "model": null },
    "analyze": { "model": null },
    "verify":  { "model": null }
  }
}
```

`model: null` means use OpenCode's default model.

<a id="memilih-provider--dan-konsekuensi-keamanannya"></a>
#### Choosing a provider — and its security consequences

`provider` accepts `opencode`, `codex`, or `agy`. The three are not equivalent choices:

| | `opencode` | `codex` | `agy` |
| --- | --- | --- | --- |
| Secret-file read boundary | **enforced** via `<project_root>/opencode.json` | **none** | **none** |
| Write sandbox | yes | yes (`--sandbox read-only` + `approval_policy="never"`) | **no** — writes in the working tree are only *detected* (`core/policy/agy_guard.py`), not prevented |
| Project-root boundary config | file, refreshed on every `init`/`upgrade` | no such layer | no such layer |
| `boundary` (`config/providers.py`) | `reads: secrets_denied`, `writes: denied` | `reads: unbounded`, `writes: sandboxed` | `reads: unbounded`, `writes: unbounded` |

`codex` and `agy` are both reported by `doctor` as `not_enforceable` with `trusted: true`: both can read every project file and receive the full process environment — their boundary is the agent's obligation, not runtime enforcement. Choosing `agy` also requires explicit acknowledgement (`requires_opt_in`); `/.provider` refuses to write it without that.

The write side is reported separately. `doctor` writes `checks.second_agent_write_boundary` (`{provider, writes}`) and, only for `writes: unbounded` (`agy`), one additional `WARNING: second_agent provider '<p>' can WRITE ...` in `recommended_fixes` — still not an issue. Every delegated result that reaches the provider stage (success or failure) carries `meta.provider_boundary` (`{reads, writes}`), and for `agy` also `meta.provider_write_warning`.

**Codex: `--sandbox read-only` alone does not stop writes.** That flag restricts shell commands. Codex's file-edit tool (`apply_patch`) is governed by the approval policy, not the sandbox: tested on codex-cli 0.154.0 on Windows, a read-only call was refused when writing through the shell and then succeeded in creating a file through that tool. The adapter therefore sends `-c approval_policy="never"` on every call, including `exec resume`; with that, codex rejects the patch ("writing is blocked by read-only sandbox"). The opt-in probe `WORKFLOW_CODEX_PROBE=1 python tests/run.py --only codex-write-probe` re-proves both write paths on new and resumed calls against the real CLI (uses codex quota).

**Codex on Windows — its own OS sandbox.** Codex's Windows sandbox (`elevated` backend) can
refuse to start a session: ``windows sandbox failed: elevated Windows sandbox requires effective
`:root` read access``. In 3.8.0 this was observed on **resumed** threads while new threads
worked (CASE-015; upstream regression openai/codex#46312, and #46114 when every thread
fails). The runtime classifies it from stderr only (similar text in the agent's output, e.g. a file it read, does not trigger it) as `error_type: sandbox_unavailable` — not `unknown` —
with an ordered `next_action`: start a new main session (`/clear` gives a new `MAIN_SESSION_ID` and a new
codex thread) and rerun; if a new thread also fails, update codex and run
`codex` once interactively to approve the sandbox setup; in the meantime switch the second
agent via `/.provider`. When what failed is a resume, the same error carries
`meta.reason: resume_sandbox_failed` and a resume-specific `next_action`: the failing codex release,
`/clear` (the old thread's context is not carried over), then pin codex to `stable_version` if every
resume keeps failing. Resume sends the sandbox as `exec --sandbox read-only resume <id> …`
(the `exec` flag before the subcommand), not `-c sandbox_mode=…`: that `-c` form, together with the
deny-read profile, is what made resume get rejected on the codex releases listed in
[Provider releases](#provider-releases) (CASE-021); the flag form resumes on the releases tested
there, and the write probe proves read-only still holds. The runtime does **not** downgrade the sandbox: the deny-read profile it sends
needs the `elevated` backend, and choosing a weaker one (`[windows] sandbox = "unelevated"` in
`~/.codex/config.toml`) is the user's decision. The codex version (`codex --version`, read once per
process) is recorded in `meta.provider_version`, in the usage row (`provider_version`), and in
`doctor` (`checks.provider_version`).

Codex sends the same deny list as a `-c permissions.workflow.filesystem` flag on every call, but that flag stops nothing. Tested against codex-cli 0.147.0 in `exec` mode: denying `**` and `**/*` for `:workspace_roots` and then asking for a file in that root still returned its contents, exit 0. Codex reads by running a shell, and `--sandbox read-only` (together with `approval_policy="never"`, see above) restricts **writes**, not reads.

This means a codex second_agent can read every file in the project you point it at, `.env` included. `init` reports this as `status: not_enforceable` with `permissions_enforced: 0`, and `dist/config/codex/AGENTS.md` tells its agent that avoiding secret files is its own obligation — an instruction, not enforcement.

Use `codex` when the project holds no secrets, or when you accept the risk. For a project whose secrets must stay unreadable to second_agent, use `opencode`.

Since v3.7.1 this position is explicit: `codex` and `agy` are treated as **trusted providers**. Besides having no read boundary, both adapters pass the full process environment to the provider CLI (credentials in env become visible). That is accepted as a risk, not fixed with an allowlist — an allowlist could break the provider CLI's auth, and a provider that can already read `.env` is protected by nothing through a trimmed env. Instead, the risk is always visible: `/.doctor` writes a `second_agent_read_boundary` check and one `WARNING` in `recommended_fixes` whenever the active provider is `codex`/`agy` (not an issue — readiness stays READY), and `/.provider` mentions it when selecting.

#### `/.provider` model catalog

`/.provider` offers models from the provider CLI installed at that moment
(`core/provider/model_catalog.py`), not from the `config/providers.py` shortlist. Each
`meta.providers[]` entry carries `models` (max 20, newest first), `newest`, and `models_source`
(`live` | `cache` | `static`). The picker puts `newest` as the first "(Recommended)" option;
the model is still confirmed by the user, not written automatically.

| Provider | Source | "Newest" |
| --- | --- | --- |
| `opencode` | `opencode models` + the models.dev cache `~/.cache/opencode/models.json` (`family`, `release_date`, `reasoning_options`) | latest `release_date` within the active model's `family`; no active model → newest of all |
| `codex` | `~/.codex/models_cache.json` (`$CODEX_HOME`), only `visibility: list` | smallest `priority` |
| `agy` | `agy models` | highest version within the active model family; no active model → CLI order |

Agy variants (`<model>-low|medium|high`) are folded into one model with `efforts` = the suffixes
present (`effort_in_id: true`). Apply builds the variant id (`claude-opus-5-5` + `high` →
`claude-opus-5-5-high`) and writes `effort: null`; a variant-bearing model without an effort is rejected.
A route that names an agy base model uses the variant with the same effort.

Effort validation at apply uses the live catalog (falling back to the shortlist), so a model
newer than the shortlist is accepted without a warning. The Router and `validate_provider_config`
still use the shortlist: both run on every delegated call, where spawning the CLI is too
expensive; a model outside the shortlist is treated there as "unknown" (effort kept).

CLI missing, timeout (30 s), or unrecognized format → `models_source: static`, the shortlist
is used. Live results are cached for 10 minutes in `<cache user>/agent-workflow/model-catalog.json`
so that the catalog and apply within one picker session see the same list.
`AI_PROXY_MODEL_CATALOG=static` turns off live reading.

Direct mode: `/.provider <provider> <model> [effort] [routes]` (e.g. `/.provider codex
gpt-6-luna low`) skips the picker. A payload without `|` is split on spaces — safe because no
provider, model id, or effort contains a space — and is then validated exactly like the
`provider|model|effort|routes` payload.

Reliability keys (v3.8.4):

| Key | Default | Meaning |
| --- | --- | --- |
| `timeout_seconds` | `1800` | Limit for one agent call. `0` = unlimited (no longer the default). `null` = inherit the default, **not** unlimited. |
| `bootstrap_timeout_seconds` | `180` | A separate budget for `init_session`. Bootstrap only replies "READY", so it must not inherit the long-task budget. |
| `idle_stall_seconds` | `240` | No new bytes on stdout/stderr for this long → `alive-stalled`. |
| `stall_threshold_seconds` | `360` | No heartbeat for this long while the PID is alive → `alive-stalled`. |
| `probe_timeout_seconds` | `45` | Limit of the PING probe itself. Without it the watchdog hangs just like its patient. |
| `probe_recheck_seconds` | `120` | Re-probe cadence while the job stays stalled; the minimum for the `await` path is 10 seconds. |
| `job_poll_interval_seconds` | `2.0` | Adapter heartbeat/poll interval. |

Per route is also possible: `"plan": { "model": "...", "timeout_seconds": 3600 }`.

**Roles are not read from this file.** The command → role mapping is defined in code (`config/routing.py`), so it cannot be overridden via config.

**Three retired keys.** `job_max_runtime_seconds`, `job_poll_timeout_seconds`, and
`agent_workflow_path` used to be written to this file and were never read by anyone. All three
are no longer written; their values come from `AI_PROXY_JOB_MAX_RUNTIME_SECONDS`, the CLI
`--poll-timeout`, and `.workflow/config.json → runtime.agent_workflow_path` (or `AGENT_PATH`).
The upgrade to the `data/` layout (v3.6.0) drops retired keys from `config.json`; before that, `doctor` flags them
`retired — never read` and they are safe to delete manually.

### Sub-agent fan-out (default ON)

`policies.subagent_fanout_enabled` applies to `explore`, `plan`, and `analyze`. `verify` keeps using a single reviewer so the whole diff is seen as one change.

Upgrade preserves the existing value. An old workspace that already stores `false` stays disabled until you change it yourself.

When active, the prompt carries the `[EVIDENCE_SIDECARS]` anchor; the fan-out clusters live in
`.workflow/data/sessions/<id>/runtime/leads.json` so they do not inflate the prompt:

- the graph has ≥2 communities → one slice per community;
- no graph, or it yields only 0–1 communities → still fan out to four investigation angles: entry points, core flow, reverse dependencies, and config/tests.

Second_agent must use the custom agent `wf-slice` via the `task` tool when available,
run the slices in parallel, then merge the claims with `[cN]` tags. If the spawn
tool is not available, it must state the list of tools that are actually available before
reading the slices sequentially.

**Usage is reported as it is.** The runtime checks two signals that must agree: the declared `subagents:` line, and the `[cN]` tags on the merged claims.

| Condition | `meta` |
|---|---|
| declaration + tags match | `subagent_used: true`, `subagent_fanout_clusters: [...]` |
| declaration without tags | `subagent_used: false` + `subagent_warning` |
| honest `subagents: none (...)` | `subagent_used: false`, `covered_clusters` may still contain the tags that were read |

A declaration without tagged claims is a claim of work, not proof of work — and is not counted as success.

---

## Graphify

With `policies.graph_leads_enabled: true`, the runtime reads `graphify-out/graph.json` **directly** without calling the Graphify CLI or MCP. It ranks at most 12 candidate files by keyword and weighted dependency degree, carries at most four communities, then injects them as **starting points, not evidence**.

- `explore`/`plan`/`analyze` receive the full leads and communities for fan-out;
- `verify` receives a compact shortlist of at most six files without communities;
- a graph older than the `.py` sources is still used, but the prompt carries a stale warning;
- graph absent, broken, or being refreshed (`graphify-out/.refresh.lock` held by a worker that is still beating) → the delegated flow still runs without leads and does not wait for the lock. The `usage.jsonl` row records `graph_status` (`used`, `empty`, `absent`, `refreshing`, `disabled`) and `graph_ms`. `verify-browser` does not use leads, so it does no lookup; it only records once per command whether a refresh is running: `refreshing`, `available` (graph exists, not read), or `absent`.

The Python runtime never runs `graphify init`, `build`, `watch`, or `update`. The only trigger of `graphify update` is the `graph-refresh` Stop hook in the Claude bundle (`.sh` launcher + `graph-refresh.py`); `CLAUDE.md` and the `refactor` skill no longer ask the main agent to run it. The hook runs after `[EXECUTION RESULT]` or `[REFACTOR RESULT]`, only when the graph already exists, is older than the sources, and the binary is available:

- the mtime scan skips the skip directories (`vendor`, `node_modules`, `.git`, `.workflow`, `graphify-out`, `target`, ...) before entering them, so its cost follows the number of sources, not the number of dependencies; directory names and extensions are matched case-insensitively;
- stale graph → the hook creates `graphify-out/.refresh.lock` (`pid`, `token`, `started`, `beat`), starts a separate worker, then returns. The turn does not wait for graphify. The worker runs `graphify update` **with no time limit** and never kills it (DEC-042); while graphify runs, the worker rewrites `beat` (and `graphify_pid`) every 15 seconds, then records one row and deletes its own lock. `GRAPH_REFRESH_LIMIT_S` is no longer read. The hook hands the lock to the worker only while it still carries that hook's token, and if the worker has already finished and deleted it, the lock is not recreated. A stale lock is deleted only if its content is still the same when reread right before deletion; a lock that changed is treated as `skipped_running`, so two sessions do not run two refreshes at once;
- graphify rewrites `graph.json` in place, so while the lock is held the runtime treats the graph as unavailable, and the main agent is asked to treat it as stale. The lock counts as held while its `beat` is < 120 seconds old, whatever the lock's age; a refresh of any length never reads as stale. A lock without `beat` (written before DEC-042) uses the old rule: pid alive and < 15 minutes old. A lock that exists but cannot be read counts as held while its mtime is < 120 seconds old. Every lock write goes to a temporary file and is then renamed;
- a hung graphify holds the graph for as long as its worker lives. `--command doctor` reports that lock (`checks.graph_refresh_lock`: `state`, `age`, `beat_age`, `pid`, `graphify_pid`) and gives advice when the refresh has been running ≥ 1 hour or the lock is stale; this is advice, not an issue, because the delegated flow still runs without leads. `--command clean` only deletes a stale lock (no `beat` within 120 seconds) and never kills a process: stop the graphify process yourself, wait for the lock to go stale, then run `clean`. If the worker dies while graphify is still alive, the lock goes stale within 120 seconds even though graphify may still be writing;
- a refresh counts as `refreshed` when `graph.json` was rewritten and parses as JSON, whatever the exit code (graphify exits 1 on large graphs because of the HTML visualization); a rewrite that does not parse is recorded as `corrupt`;
- every run that passes the first gate on a project that already has `graph.json` writes a `kind: graph_refresh` row to `.workflow/data/quality.jsonl` (when that directory exists) with `outcome` (`skipped_fresh`, `skipped_running`, `no_graphify`, `refreshed`, `corrupt`, `error`; `timeout` only appears in rows from before DEC-042 and is still counted), `hook_ms` (what the turn paid), `scan_ms`, `files_visited`, `dirs_skipped`, and for a refresh `graphify_ms`, `graphify_exit`, `graph_rewritten`. `--command report` summarizes it in `graph_refresh`.

The hook fails open, never rejects a response, never creates a new graph or runs `init`/`build`/`watch`. The Stop hook's outer timeout stays 60 seconds.

The graph verdict cache uses a fingerprint of the path, nanosecond mtime, and size of every `.py`
source. A content change, addition, or deletion of a source invalidates the cache even if
`graph.json` did not change.

---

## Task telemetry

`--command report` → `tasks` reads `.workflow/data/tasks.jsonl` (DEC-015, revised by DEC-020). One task = a series of events within one MAIN_SESSION_ID, from its first event until a verify whose runtime-**derived** verdict is `pass` after that task edited files. A commit is the recommended next step, not the closer. Events are written by two parties, with no additional instructions in the prompt:

- runtime: one event per delegated command (`explore`, `analyze`, `plan`, `verify`, `verify-browser`). A verify event carries `verdict` (the one **declared** by the reply; `DONE` with a blocking finding counts as `NEEDS FIX`) and `derived` (the runtime-derived verdict: `pass`, `incomplete`, or `fail`). Only `derived: pass` closes a task; a `DONE` that the runtime considers `incomplete`, including because of `not_verified`, leaves the task `open`;
- the `task-events` hook (`.sh` launcher + `task-events.py`; `UserPromptSubmit`, `PostToolUse` matcher `Skill|Edit|Write|MultiEdit|NotebookEdit`, `Stop`): local skills invoked from the prompt (`/.<name>`) or via the `Skill` tool, files inside the project that were edited (not `.workflow/`/`.git/`), and commits as HEAD **moving between turns**. The session's first event stores a HEAD snapshot in `.workflow/data/task-hook/<claude_session_id>.head`; each `Stop` (or the next prompt, for commits made in a terminal between turns) records the commits since the snapshot — first-parent, oldest first, each with `paths` — then advances the snapshot. Events are v2 (`v: 2`). Before 3.8.0 this hook ran on every `Read` and `Bash` (twice for `Bash`); one PowerShell spawn is ~450 ms on Windows, while skills and commits only need to be read once per turn. Consequence: a skill loaded via `Read` without a prefix (natural-language auto-intent) is not recorded, and a commit is recorded at the end of the turn that made it, after the verify in that turn.

States: `completed` (verify `derived: pass` with no edit afterwards, committed or not), `open` (has edits, not yet `pass` — committed or not), `read_only` (no edits), `unknown` (events without a session identity). An edit after `pass` opens a new task. A commit containing files of a task that just became `completed` goes into that task's `sequence`; a commit in an `open` task goes into its `sequence` without changing its state. Events from before DEC-020 do not carry `derived`, so their task stays `open`. Output: `tasks`, `by_state`, `skill_counts`, `unclaimed_commits` (distinct commits none of whose copies is claimed by a task; a commit is marked as used only when claimed, so a copy recorded earlier by another session does not hide it from the session that made it), and `recent` (the last 10 tasks with `sequence`). This stream is separate from `usage.jsonl`: local skills and edits are not delegated calls and must not enter budget governance.

`--command report` → `rework` is computed from `usage.jsonl`, not from this stream: per `correlation_id`, the assessed verify rounds are ordered by `recorded_at`, and a group counts as rework only when a round that is not `pass` (`fail` or `incomplete`) is followed by another round; `extra_verification_rounds` = the rounds after the first failure. A plan verified per phase with all rounds `pass` is not rework (DEC-051, `runtime.rework@1` in [metrics](evaluation/metrics.md)).

---

<a id="mode-verify"></a>
## Verify mode

The configuration contract in question separates two settings:

- `auto_verify_after_execute` determines whether main_agent automatically calls `/.verify` after implementation; default `false`;
- `verify_mode` determines the depth of the check when verify is actually called.

So `auto_verify_after_execute: false` does not turn off `/.verify`; it only prevents the automatic call.

`verify_mode` controls **how deep** `/.verify` goes:

- **`delegated`** (default) — second_agent verifies and returns a labeled contract. Every finding must carry three tags: `severity` (critical/high/medium/low), `origin` (introduced/regression/pre_existing/unknown), `scope_relation` (in_scope/out_of_scope). Blocking is determined by the combination of all three, not severity alone — a pre-existing defect does not hold the verdict of the current change hostage, and `origin: unknown` fails closed. A section without findings must contain an explicit `none`; the runtime also accepts `none.`, `none (…)` (free remark without a tag), `n/a`, `not applicable`, `nothing found`/`nothing to report`, `no (blocking) findings`, `tidak ada`, `tidak ada temuan`, and a sentinel followed by a separator (`;`, `:`, `,`, `—`, `-`) with a remark that consists entirely of neutral phrases (`clean`, `all clean`, `ok`, `nothing found`/`nothing blocking`/`nothing to report`, `no issues`, `bersih`, `semua bersih`, `tidak ada temuan`). Free explanation goes in parentheses. Anything else — `none of …`, `none - caller X fails`, `none; all inspected paths are clean`, a tagged remark, multi-line text — is read as a finding; a continuation then asks for the entry to be written as `- none`. Exception in the E2E reviewer (`verify-browser`): an item with continuation lines is still empty when its first line is a plain `none` or `none (…)`, as before.
  **Tests in `delegated` mode.** The second agent verifies by reading and tracing; it
  does not run a test runner. Tests are selected by main_agent from the diff and dependents, written to
  `.workflow/data/sessions/<MAIN_SESSION_ID>/verify/tests.json` —
  `{"commands": [...], "reason": "..."}`, at most 5 — and then run by the **runtime** after the
  review returns (`core/evidence/verify_tests.py`). A command only runs when it starts with one of the
  prefixes in `commands.verify_test_commands`, without a shell (`&&`, pipe, redirect, `$(` are rejected),
  one at a time, each under `commands.verify_test_timeout_seconds`. The results are
  written to a `[VERIFICATION]` block the second agent never sees: each command becomes a
  `checks_run` `runtime: ...` line; one that fails, times out, or cannot be run becomes a blocking
  finding (`origin: unknown`, fail closed); a rejected command, and a verify **without**
  `tests.json`, become a `not_verified` gap, so the verdict is `incomplete`. `commands: []` with
  a reason states that no test is relevant and adds no gap; the line
  `runtime: no test requested — <alasan>` is still written in `checks_run` but is **not counted**
  as a check, so the verdict depends on the review's own checks (no checks →
  `incomplete`, `checks_missing`). A project without a test suite uses the same path: the verify
  skill asks for `commands: []` with that reason, rather than skipping `tests.json`. Gaps written by the runtime (prefix `tests:`: no request,
  unused request, rejected command, crash) are of type `runtime_gap` and cause exit 2;
  the exit 0 allowance for `incomplete` applies only to gaps declared by the agent
  (`verification_gap`). A command containing control characters (C0 other than tab, DEL) is rejected before
  it runs; a spawn error becomes a blocking finding; any other exception in this step returns the
  review as is plus a `tests: runtime tests crashed: <tipe>` gap (the request is still consumed).
  While runtime tests run, the job heartbeat is written before each command and every 20 s
  (`phase: runtime_tests`), so a long suite does not read as `alive-stalled`. `reason` must be a single line
  (one containing a newline makes the request unusable); `command` and `detail` are collapsed onto one
  line before entering the block, so request text cannot forge a section. The request belongs
  to one job: creating a verify job moves `tests.json` to `tests.claimed.json` and drops any claim
  an earlier job left (its provider call failed or its run was refused before the tests ran), so
  a new verify without a new `tests.json` gets the `tests:` gap, never an earlier choice. A
  recovery run of the same job is not a new job and still finds its claim. Only a job's own
  run reads a claim; a run without a job (an in-process `main.run`, as the test harnesses use)
  reads `tests.json` alone. The claim is used once, renamed to `tests.used.json` **after** its
  results are merged. A run finishes when that command's process exits, not when its pipe hits EOF: after
  exit, the pipe is waited on for at most 5 s and then any remaining processes still holding it are killed (process
  group on POSIX, job object on Windows) and the original exit code is used, so a runner that
  leaves a server/watcher behind no longer reads as a timeout. A timeout kills the command's whole process
  tree (process group + `taskkill /T` / `killpg`), then output is collected for at most 5 s; a boolean
  timeout value is ignored (default 900). The prompt line "jangan jalankan test runner" ("do not run the test runner") is only
  sent when `verify_test_commands` is set; while it is empty, `doctor` gives a warning
  (`checks.verify_test_commands: empty`) because verify runs no tests and cannot `pass`.
  Under a verify intent, the gate hook lets main_agent read `git diff --name-only`/`--stat`,
  `.workflow/config.json`, and write `verify/tests.json` before `.workflow/run`. The reason: when the second agent also ran tests, codex
  ran the whole suite (verify median up to 366 s, CASE-010) while opencode could not
  run a single one and its verify became `incomplete` (CASE-009).
- **`syntax`** — answered locally, without calling OpenCode at all. Checks staged, unstaged, and untracked files, including a repository with no commits: `.py` via in-process `compile()`, `.json` via `json.loads`, `.js`/`.mjs`/`.cjs` via `node --check`, `.php` via `php -l`. Python files also require the `pyflakes` name check; if the tool is not available, the result is `incomplete`. A Git discovery failure also yields `incomplete`, not `skipped`.

Everything that cannot be checked is reported as it is, never counted as passing:

| Output | Meaning |
|---|---|
| `not_checked` | no checker for that extension, or file > 2 MB |
| `skipped` | the language toolchain is not on `PATH` |
| `name_check: unavailable` | `pyflakes` is not installed; Python files go to `skipped` and the verdict is `incomplete` |

Python is checked in-process, not via `py_compile`, so no `.pyc` is left behind in your working tree. The directories `__pycache__`, `node_modules`, `.git`, `vendor`, `.venv`, `venv` are skipped.

`verdict: pass` means every applicable checker finished without findings and there is no gap.
It does **not** mean the feature works or that behavior tests were run.

---

## Provider releases

Each provider in `config/providers.py` names the CLI release this workflow version was tested on
(`stable_version`): opencode `1.19.3`, codex `0.163.0`, agy `1.1.13`. The runtime reads
`<cli> --version` (`core/provider/versions.py`; the first line containing a semver, from stdout
then stderr) at most once per process and stores it per user (`%LOCALAPPDATA%` or
`$XDG_CACHE_HOME`/`~/.cache`, file `agent-workflow/provider-versions.json`), keyed by the executable's path,
mtime, and size, expiring after 24 hours; a CLI without a version is cached as `null`, a timeout is not
cached. Every usage row that reaches the provider (single calls as well as each continuation row,
all providers) carries `provider_version` and `provider_version_status`: `stable` (same), `untested` (another
release, including pre-releases), `unreadable` (the CLI did not answer). `doctor` reports
`checks.provider_version` `{provider, version, status, stable}` and warns on
`untested`. It is advisory: the call still runs; nothing blocks the provider, and the installer
only changes providers when asked to with `--update-providers`.

`install.py --update-providers` (opt-in, DEC-057) brings each CLI to `stable_version`, never
to a newer release. The dry run prints the plan per provider; `--apply` executes it
after all files are installed. A provider is changed only when its bundle names an update method
(`update` in `config/providers.py`: codex `@openai/codex`, opencode `opencode-ai`, both npm),
its CLI is on PATH, is not already on the stable release, is a global npm install (a shim in npm's global bin
or resolving to the global `node_modules`, and its package directory exists), and the npm registry has that stable
release. Otherwise the provider is reported and left alone: agy (`unsupported`, its installer is not
documented), a CLI that is not installed (not freshly installed), a CLI from Homebrew/another installer
(belongs to whoever installed it), npm missing, or the registry unreachable. No `sudo`. Each update
is verified by reading `--version` again after that provider's version cache and model catalog cache
are discarded. Updates are not atomic and not part of the receipt (`--rollback` does not revert them):
a provider that fails gets a warning with the npm error and a pin-back command to the previous release,
the other providers continue, and `~/.claude/backups/install_<stamp>/provider_update.json` records
the release before and after for each provider.

Going back to the stable release if calls fail after a provider is updated:

| Provider | Pin |
|---|---|
| codex | `npm i -g @openai/codex@<stable>` |
| opencode | `npm i -g opencode-ai@<stable>` (or the opencode installer with that version) |
| agy | the agy installer with that version |

Codex releases 0.155.0 through 0.160.0 failed to resume threads on Windows with
`sandbox_unavailable` (CASE-016) while the resume sandbox was sent via `-c sandbox_mode`;
the adapter now sends `exec --sandbox`, and resume works on the releases tested within that range
(codex-cli 0.155.1 and 0.160.0, CASE-021).
`stable_version` moves only to a release that passed the live probe: codex 0.163.0 passed
`codex-write-probe` (writes refused on a new and a resumed thread) and opencode 1.19.3 refused
writes on a new and a resumed session, both on Windows (DEC-058).

---

## Fact store

`.workflow/data/facts.jsonl` stores knowledge that persists across sessions. A claim enters through one of two paths:

1. explicitly tagged `[config]`/`[pattern]`/`[invariant]` by second_agent, or
2. reported independently by ≥ `fact_recurrence_threshold` **other** sessions.

The current session is excluded from the recurrence count. Without that, a fact injected into the prompt and then merely echoed back could raise its own count up to the promotion threshold — repetition masquerading as evidence.

Each fact is anchored to the content hash of its `file:line` line. When that line changes, the fact is considered stale when read and is never served as fresh.

Two claims are merged only when **all** guards agree: same `file`, same `category`, identical `anchor_hash`, same negation polarity, Jaccard similarity ≥ 0.5, and both claims ≥ 6 words. One guard refuses → both are kept. Duplicates are cheap; a lost fact cannot be recovered.

## Evidence reuse and boundary

Cross-session evidence is indexed in `.workflow/data/evidence.jsonl` with a cross-process lock and
atomic rewrite. An entry may only point to an immutable artifact
`sessions/<id>/logs/<prompt_id>/output.raw.md`; the artifact hash and every `file:line` anchor
must still match. An anchor that does not resolve or exceeds the limit makes the evidence
ineligible for reuse. Old entries pointing to `response.last.md` are discarded.
The cache query is case-sensitive and also binds the effective route/model, config version,
fan-out mode, graph leads, and the facts used. `--fresh-session` disables the reuse
lookup for that invocation.

Before delegation, relative paths are resolved against the project root; traversal, absolute paths
outside the root, home-relative paths, and bare sensitive files such as `credentials.json`
are rejected. OpenCode uses `external_directory: deny` at the root level of the project's `opencode.json`
(applying to any agent, including the `AI_PROXY_OPENCODE_AGENT` override) and in
`agent.plan`; file discovery must go through the built-in Read/Grep/Glob. Bash only allows
the registered read-only Git commands.

The OpenCode process environment (bootstrap, probe, run) no longer inherits `os.environ`.
The child gets an allowlist of OS/network variables (`PATH`, temp, locale, proxy/CA,
`HOME`/`USERPROFILE`/`APPDATA`/`XDG_*`, `OPENCODE_*`) plus the keys from `.env` in the active project
root (`adapters/shared/child_env.py`). `opencode auth login` auth is still read from
`~/.local/share/opencode/auth.json`; provider keys referenced in config as `{env:NAME}`
must be in the project `.env`. Call meta records `env_policy`, `project_dotenv`,
`project_dotenv_keys`, `parent_env_dropped` (counts, no values). Codex/agy are unchanged.

All adapter content and metadata, including errors, timeouts, bootstrap, probe, and
`call.meta.json`, go through recursive redaction. Raw process arguments are not stored;
telemetry only carries the count, length, and hash of argv.

Clean up stale entries and duplicates:

```bash
python3 main.py --command clean --work-dir /path/to/target-app --pretty
```

---

<a id="layout-workspace"></a>
## Workspace layout

```text
<target-app>/.workflow/
├─ config.json              # overrides only; defaults filled in on read
├─ second_agent.json        # project-local copy, provider config
├─ e2e/secrets.json         # optional, verify-browser credential profiles
├─ current/                 # mirror of the last dispatch (no credentials)
├─ .gitignore
├─ run.sh                  # 1-call entry point (bash on every OS)
├─ inspect.sh              # job list
├─ check.sh                # job status/result
└─ data/                    # everything that is not a human-edited file (layout 2)
   ├─ facts.jsonl           # cross-session fact store
   ├─ evidence.jsonl        # cross-session immutable artifact index
   ├─ audit.jsonl  usage.jsonl  quality.jsonl
   ├─ provider-sessions/    # provider session IDs, isolated per project
   ├─ backups/<stamp>/      # pre-migration copies, last 3 kept
   ├─ reports/
   │  └─ doctor.json
   └─ sessions/<session_id>/   # created lazily per session
      ├─ state.json
      ├─ scope.json
      ├─ command-cache.json
      ├─ runtime/
      │  ├─ prompt.txt
      │  ├─ prompt.meta.json
      │  ├─ response.last.md
      │  └─ lock
      ├─ logs/<prompt_id>/
      │  ├─ prompt.md
      │  ├─ prompt.sha256
      │  ├─ output.raw.md
      │  └─ call.meta.json
      └─ reports/sweep.last.md
```

Mutable state (`state`/`scope`/`cache`/`runtime`/`logs`) lives under `data/sessions/<id>/`, so two main_agents on the same project never overwrite each other. The `.workflow/` root holds only human-edited files plus scripts.

The v3.5.x workspace layout (layout 1) stored the contents of `data/` directly in the `.workflow/` root; the runtime still reads it until `upgrade` moves it. Post-move steps (rewrite evidence paths, delete old leftovers, strip config, convert `secrets.json`, prune backups) are recorded in `data/.migration-pending.json`; if any of them fails, the next `upgrade` resumes from that step and `doctor` reports it as `migration incomplete`. A leftover `data.migrating/` whose names collide with the root is refused without moving anything.

Async jobs are still stored in the tool repo under `storage/jobs/`. The default session ID cache is
in `storage/main-sessions/` and is separated by a hash of the project root; the mapping to OpenCode
session IDs is project-local in `.workflow/data/provider-sessions/`, so the same ID on
two projects cannot resume each other's provider sessions.

---

## Async jobs & recovery

Delegated commands automatically run through a separate worker. On Claude Code, the runner
must be launched as a background tool task so the foreground tool call immediately
returns a task ID; Claude then fetches that task's result. The runtime stays
agent-agnostic—other callers may use the ordinary blocking runner.

If the background task is lost or the call is interrupted, call the runner again with an
identical session, command, and task. The runtime attaches if the worker is still alive.
If the worker is already dead, the same job is recovered once through the old provider thread (opencode, codex, or agy)
with a structured continuation prompt. A second death yields
`recovery_exhausted`, releases the lock, and does not trigger an automatic loop.

```bash
bash .workflow/inspect.sh
bash .workflow/check.sh <job_id> --wait --result
```

`check` status exit codes: `0` finished · `1` failed · `2` still running/queued · `3` not
found. With `--result` for a verify job, `0` only means verdict `pass`; a verdict of
`fail` or `incomplete` returns `2`.

If no matching job exists, the last result is still in `.workflow/data/sessions/<id>/runtime/response.last.md`. What is currently running is visible in `.workflow/current/session.json` and `progress.jsonl`.

Recovery is best-effort, not process survival: if the old provider thread was never
recorded, the runtime fails with `session_capture_failed` and a clean run is needed.
A result carrying `meta.session_persisted: false` means that thread was not stored —
neither recovery nor continuation is available for it.
A different request on a session that is still locked is still rejected as
`job_already_running`, unless the worker of the job holding the lock is proven `dead` (PID gone or
reused by another process). In that case the old job is marked `failed` + `reaped`, only that
session's lock is released, and the new request is processed as a new job. A job that is
recovering (has no worker PID yet) and a worker that is alive or stalled are never taken over.
An identical request still uses the recovery path above.

### Liveness worker (v3.8.4)

A live PID does **not** mean it is working. The worker therefore reports a heartbeat together with the age of the output stream, and the job is classified into three states:

| State | Meaning | Action |
| --- | --- | --- |
| `alive-progressing` | PID alive, heartbeat fresh, stream has not crossed the idle limit | wait |
| `alive-stalled` | stream idle > `idle_stall_seconds` or heartbeat stale > `stall_threshold_seconds` | probe a fresh session |
| `dead` | PID gone | reap → `worker_died` |

When `alive-stalled`, the runtime sends a PING to a **new OpenCode session**, not the session suspected of hanging. The first probe runs as soon as the stalled status is seen, then repeats per `probe_recheck_seconds` while the job is still stalled. The current source default is `120` seconds, not one minute.

The normal `await` path reads `stall_threshold_seconds`, `idle_stall_seconds`, and `probe_recheck_seconds` from the project config. The `check.py --wait` attach path currently uses the tool defaults for all three and a fixed probe cadence of 120 seconds; project tuning is not yet passed to the attach process.

- probe answers → store `stalled_no_progress`, keep waiting;
- provider refuses because of quota/rate limit → terminate the process tree, reap as `rate_limited`;
- probe stream cut off → terminate/reap as `streaming_failed`;
- probe fails for another reason → terminate/reap as `second_agent_unavailable`;
- PID gone at any time → reap as `worker_died`.

The JobManager hard ceiling remains as a safety net: default 5400 seconds, changeable via the env `AI_PROXY_JOB_MAX_RUNTIME_SECONDS`. A job that exceeds it fails as `job_expired` even if the PID appears alive. A project-local key with the same name does not yet change this value.

Every call that reaches `OpenCodeAdapter` writes a redacted `call.meta.json`
(exit code, duration, timeout, kill method, safe stderr tail, and argv aggregates) to
`.workflow/data/sessions/<id>/logs/<prompt_id>/`. `verify_mode: syntax` does not create this file.

---

## Sessions

`--session` is the single authority for session binding. The first call bootstraps an OpenCode session:

```text
opencode run <prompt> --print-logs --log-level INFO
```

The runtime parses `session.id=ses_...` from the log and stores it. Subsequent calls reuse that session:

```text
opencode run <prompt> -s <provider_session_id>
```

OpenCode log lines and the model banner are stripped from `content`; the assistant's answer is kept intact.

---

## Test

```bash
python3 tests/run.py
```

```powershell
python tests/run.py
```

The default suite does not call the real OpenCode; agent flows use a fake adapter,
while the subprocess path, heartbeat, timeout, kill-tree, installer, rollback, and
persistence contracts are tested locally.

Use `python tools/e2e/e2e.py --full` if you want to add a real OpenCode smoke test; that mode is opt-in because it uses quota.

Additional checks:

```bash
python3 main.py --help
python3 main.py --command doctor --work-dir . --pretty
```

---

## CI

Two GitHub Actions workflows run the gates that someone would otherwise have to remember
to run themselves.

| File | Trigger |
|---|---|
| `.github/workflows/ci.yml` | push to `main`/`dev`, pull request, manual |
| `.github/workflows/e2e-full.yml` | manual only |

Gates run by the free path, in order:

```
python tools/maintain/stamp_version.py --check
python tools/maintain/gen_manifest.py --check
python tests/run.py --only deps
python tests/run.py --keep-going --record .
python tools/e2e/e2e.py
```

`--only deps` stands as its own step, not only inside the suite: it is the gate that
decides whether the absence of a lockfile is still correct, and a reader scanning the step list must
be able to see it applied. `--keep-going` so that one failure does not hide the rest;
`--record .` writes the results to the workspace quality stream, so `--command report` can
show the pass rate over time instead of only the last run. `e2e.py` without `--full`:
zero provider CLIs called, zero quota burned — which is why it may run on every push.

The delegated path is manual and never automatic: it calls the real second_agent,
consumes real quota, and needs a provider CLI plus credentials the pipeline deliberately does
not hold. It is a separate `workflow_dispatch` workflow, with the session id and runner as
dispatch inputs — defaults `e2e-dispatch` and `self-hosted`.

### Toolchain and matrix

Python is pinned to `3.13` via `actions/setup-python`, so the workflow PROVIDES the interpreter
instead of hoping the runner has it. The `runs-on` matrix covers `ubuntu-latest` and
`windows-latest` with `fail-fast: false`: hooks and the runner are bash on both OSes (Git Bash on
Windows, checked in the job's first step), but path handling differs most exactly where
it matters (locks, session directories, backslash paths accepted by the launcher) — a single-OS run would
pass while half the product went untested, and one red platform must not hide
the other platform's results.

`on: push` (`main`/`dev`) + `pull_request` + `workflow_dispatch`, with
`concurrency: cancel-in-progress` so that a pipeline overtaken on the same ref is cancelled
instead of being left to finish against a stale commit. Job permissions are locked to `contents: read`.

What CI does **not** do: bump, tag, publish. The steps that remain human-owned
are in `RELEASE.md`.

---

<a id="batasan-yang-diketahui"></a>
## Known limitations

Moved to [`docs/limitations.md`](limitations.md). This heading is kept so that old
links to `#batasan-yang-diketahui` keep working.

---

## Benchmark

Moved to [`docs/evaluation/benchmark.md`](evaluation/benchmark.md). Real-use results
(tasks that landed in `main`, fixes, prompts, context) are in
[`docs/evaluation/real-use-benchmark.md`](evaluation/real-use-benchmark.md); status line
telemetry in [`docs/evaluation/observed-usage.md`](evaluation/observed-usage.md).

---

## References

- Release notes: [`CHANGELOG.md`](../CHANGELOG.md)
- Canonical main_agent contract: [`dist/config/claude/CLAUDE.md`](../dist/config/claude/CLAUDE.md)
- Canonical second_agent contract: [`dist/config/opencode/AGENTS.md`](../dist/config/opencode/AGENTS.md)
- Runtime entry point: [`main.py`](../main.py)
- Benchmark: [`docs/evaluation/benchmark.md`](evaluation/benchmark.md) (method as `H` + `EXP` in `docs/research/`)
- Architecture (verified diagrams): [`docs/architecture/`](architecture/README.md)
- Limitations: [`docs/limitations.md`](limitations.md)
- Evaluation (benchmark and telemetry): [`docs/evaluation/`](evaluation/README.md)
- Research notes and design decisions: [`docs/research/`](research/README.md); research questions (`RQ`): [`docs/research/questions.md`](research/questions.md)
