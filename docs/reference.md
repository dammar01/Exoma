# Exoma v1.1.0

Standalone orchestration runtime for a two-agent workflow, installed as the `exoma` Python
package and CLI. No third-party dependencies. Forked from agent-workflow 3.9.0.

Module paths in this reference (`core/...`, `adapters/...`, `config/...`) are relative to
the package, `src/exoma/` in the repository.

## Summary

Two roles, strictly separated:

- **main_agent** — the agent you use (Claude Code, Codex, Cursor, etc.). Orchestrator, user interface, and the **only** one allowed to write files.
- **second_agent** — the selected provider (OpenCode, Codex, Agy, or Claude Code), a **read-only** evidence gatherer by role. Not the final answer. Only OpenCode enforces that boundary; each provider's write boundary differs, see [Security](security.md).

Terms: `main_agent` and `second_agent` are the identifiers the runtime, hooks, skills and
JSON fields use. Prose elsewhere may say *primary agent* and *second agent* (or *secondary
agent*) for the same two roles. The *proxy* is the runtime path that calls the second agent;
*evidence* is its output, an *anchor* is a cited `path:line` in it, and a *marker* is the file a
hook writes to arm a gate (`delegated.marker`).

This runtime sits between the two: it receives a command, assembles a structured prompt, runs the selected provider's CLI (e.g. `opencode run`), validates the output shape, stores per-session state, then returns a stable JSON contract:

```json
{ "ok": true, "content": "...", "meta": {} }
```

`digest` is added only when the second_agent output carries a parseable `[DIGEST]` block; it is not a required field.

Evidence, config, per-session state, and provider session bindings (`.workflow/data/provider-sessions/`) live project-local in `.workflow/` in the target project. The job queue, the main-session cache and the provider seed live in the per-user data directory (see [Workspace layout](#workspace-layout)).

---

## Flow from prompt to response

```text
Prompt user
→ main_agent: auto-detect intent or use the /. override
→ .workflow/run.sh (bash on every OS)
→ python -m exoma --command await → JobManager → detached worker
→ Executor → Router
→ Graphify leads + fact store → PromptBuilder
→ selected provider adapter (OpenCode/Codex/Agy/Claude) → second_agent
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
| `digest` | second_agent output carries a parseable `[DIGEST]` block | summary to be relayed. `digest.anchors = {certified, total, scanned_all}` is added when the evidence records anchors — up to 200 distinct citations are certified, and `scanned_all: false` means the scan stopped before the content ended (confidence capped at `medium` even when `certified == total`); a confidence lowered because of anchors/graph keeps its original value in `digest.confidence_reported` and the reason in `digest.confidence_capped_by` |
| `evidence_ref` | only the explore/plan/analyze evidence path | `{artifact_path, anchors, certified_anchors, scanned_all, reused}`; `anchors` counts certified-or-not citations, of which the first 40 are stored for reuse. `reused: true` = an artifact from a past session was reused (see [Evidence reuse and boundary](#evidence-reuse-and-boundary)) |
| `meta.task_cap`, `meta.task_cap_source` | every delegated call | effective task length limit. `task_cap_source`: `transport` (computed from the remaining argv command-line space), `floor` (scaffolding nearly fills the command line; minimum value), `policy` (opt-in `EXOMA_MAX_TASK_CHARS_NO_ARGV`), `unbounded` (`task_cap: null`; stdin/file transport, or argv outside Windows) |
| `meta.task_truncated` | task cut by the cap | signal to shorten the instruction, not to split the call |
| `meta.content_mode`, `meta.content_full_chars` | `EXOMA_SLIM_CONTENT` is active, content is long, and the artifact exists on disk | `content_mode: "ref_only"`: `content` is only a preview; the full text is at `evidence_ref.artifact_path` |
| `meta.claims[].ref_status` | exploration/reasoning results with grounded claims | one status per ref: `certified`, `unverified` (a `file:line` this project cannot open; also raises a `claim_ref_unverified` contract warning). A citation is read from the project root; a relative one that does not resolve there is matched by path suffix against the project's files (dependency, build, cache and `.workflow/` directories excluded; no index past 20000 files) and certifies only when exactly one file ends with it on a path-component boundary, ignoring case on Windows (the stored path keeps the on-disk spelling). Absolute paths and `..` get no fallback, `not_a_file` |
| `meta.uncertified_anchors` | a citation failed to certify | up to 10 `file:line` refs that did not hash to a line in this project |
| `meta.scope_warnings` | `scope_not_covered` lists non-capping gaps | `[{category, detail}]` for `live_state`, `external`, `out_of_scope` bullets. Only `in_scope_unreachable` and untagged bullets cap confidence at `medium` |
| `meta.facts` | exploration/reasoning calls | fact lifecycle counts: `candidates`, `injected`, `rejected_stale`, `rejected_irrelevant`, `ingested`, `ingest_rejected_anchor`, `ingest_rejected_length` |
| `meta.policy` | every delegated call | the effective `auto_verify_after_execute`, `verify_mode`, `subagent_fanout_enabled` |
| `meta.error_type`, `meta.next_action` | `ok: false` | both required; `error_type` from the closed set below |

### `error_type`

A closed set in `core/evidence/contract.py` `ERROR_TYPES`; `make_error` rejects names outside that set, and the `error-types` suite ensures every literal used in code is registered.

| Group | `error_type` |
|---|---|
| Provider and worker | `timeout`, `worker_died`, `worker_crashed`, `worker_stalled`, `job_expired`, `rate_limited`, `streaming_failed`, `sandbox_unavailable`, `network_denied`, `second_agent_unavailable`, `command_not_found`, `permission_denied` |
| second_agent output | `empty_output`, `invalid_evidence`, `session_capture_failed`, `task_truncated`, `fact_ingest_failed` |
| Prompt and command line | `prompt_too_long`, `unsafe_command_line` |
| Provider config | `provider_config_missing`, `provider_config_invalid`, `model_unset`, `invalid_provider_selection` |
| Job, session, workspace | `job_already_running`, `job_submit_error`, `worker_capacity`, `runtime_lock`, `session_state_error`, `routing_error`, `recursion_refused`, `workflow_init_error`, `workflow_upgrade_error`, `sweep_git_error`, `budget_exceeded` |
| Promote and knowledge | `promote_input_missing`, `promote_input_unreadable`, `promote_existing_unreadable`, `promote_not_on_branch`, `promote_not_production_branch`, `promote_write_rejected`, `invalid_knowledge`, `secret_shaped_content`, `ignored_source`, `ignored_destination` |
| Project tests (`test`) | `test_input_invalid`, `test_not_configured`, `tests_failed` |
| Other | `unknown` |

`recursion_refused`: the process carries `WORKFLOW_AGENT_ROLE=second_agent` (every provider adapter sets it on its child) and asked for `explore`, `plan`, `analyze`, `verify`, `verify-browser`, `submit`, `await`, `worker`, `plan-record`, `execute-begin`, `execute-end` or `test`. Refused in `cli.main` after argument parsing, at `run()` entry, and at `submit()` entry, the one path to a job and a worker spawn (so `exoma check --wait` reclaiming a pending job spawns nothing either), before any session, job or file side effect. The stalled-job probe (`check_stalled_job`, reached from `check --wait`) is a provider call too: for a marked caller the probe of a live worker is skipped and the job keeps waiting, not reaped on the probe's word (a worker whose process is gone is still reaped, which calls no provider); `meta.command` (and `meta.job_command` for `submit`/`await`) names the request, and the exit status follows the usual mapping (1, or 2 for `verify`). Read-only commands (`inspect`, `status`, `result`, `report`, `audit`, ...) stay open, as do local commands outside that list such as `doctor` (which writes its own report). The marker stops inherited recursion when the host and the second agent are the same CLI; it is not a security boundary, since a child that can run commands can unset it ([Security](security.md)).

`worker_died` with `meta.reason: recovery_exhausted` means automatic recovery has already been used; do not retry automatically (see [Async jobs & recovery](#async-jobs--recovery)).

`sandbox_unavailable` with `meta.reason: resume_sandbox_failed` means the codex Windows sandbox rejects a **resumed** thread; a new thread is known to work (see "Codex on Windows" in [`.workflow/second_agent.json`](#workflowsecond_agentjson)).

`network_denied`: the OS refused the provider a network socket — Windows WSAEACCES, `os error 10013` (codex), or `connect EACCES <address>` (opencode, Node). Classified from stderr only, before `permission_denied`, whose signs (`forbidden`, `eacces`) the same text also carries. One cause is a main host's network restriction inherited by the runner and provider. Codex advice is conditional on the current host permissions: request escalation only when necessary and permitted, otherwise report the blocked boundary. A host auto-review denial before process creation is a separate case with no runner JSON. See [Hosts](#hosts).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | stdlib `tomllib`: the installer parses Codex's `config.toml` before it writes it. `exoma`, `exoma install` and `exoma check` reject older Python with a single message (the version and interpreter in use) before any import. The `.sh` hooks run the interpreter `exoma install` pinned in `~/.claude/hooks/python.path` (the one it ran under and installed the extras into, also named by generated `.workflow/*.sh`); without that file they pick one by running it, in order `python3`, `python`, `py -3`; one that does not run (WindowsApps alias) or is older than 3.11 is skipped, and with none of them the hook fails open. The pin is taken without asking its version: after moving to a newer Python, run `exoma install` from it and `/.upgrade` each workspace. `/.doctor` reports a pin older than 3.11; `exoma install --check` does not check the pin |
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

## Install (v1.1.0)

### New team member — full sequence from scratch

Three steps, run once per machine (except step 3, once per project):

```bash
uv tool install exoma                       # 1. get the tool (or: pip install exoma)
exoma install                               # 2. install the global agent config (asks the host and second agent)
exoma init --work-dir /path/to/your-project # 3. per project
```

`exoma` is also `python -m exoma`, with the same arguments. Every command accepts both the
subcommand form (`exoma init`) and the flag form agent-workflow used (`exoma --command init`).

Verify before using it:

```bash
exoma install --check                       # bundle + global install
cd /path/to/your-project && bash .workflow/run.sh doctor    # every OS (Git Bash on Windows)
```

`doctor` must report `READY` with zero issues. Otherwise, read `recommended_fixes`
before continuing — a `NOT_READY` status means an entry point is genuinely broken,
not merely a style note.

The entry scripts in `.workflow/` bake in absolute paths of the machine where `init` was run, so
step 3 belongs to each person. Committing `.workflow/` to a shared repo will not
help anyone; `init` already adds it to `.gitignore`.

Do not install Exoma and agent-workflow into the same `~/.claude`: both ship hooks and skills
under the same names, and the last install wins.

<a id="detail-installer"></a>
### Installer details

Install the package → run one command → configured.

```bash
exoma install                             # a terminal asks every choice, the current one as default
exoma install --no-input                  # ask nothing: keep the stored choices
exoma install --dry-run                   # show every change, write nothing
exoma install claude                      # Claude Code main agent
exoma install opencode --provider claude  # OpenCode main agent, Claude Code second agent
exoma install codex --provider codex      # Codex main agent and Codex second agent
exoma install all                         # Claude Code, Agy, Codex, and OpenCode
exoma install --only-command              # workflow active only through /.<command>
exoma install claude --provider codex --model gpt-5.6-sol   # choose the second agent without a prompt
exoma install --with-e2e                  # also the browser extra for /.verify-browser (never without this flag)
exoma install --check                     # check bundle + install (including the project when cwd has .workflow/)
exoma install --rollback                  # dry run of the latest rollback
exoma install --rollback --apply          # rollback after a hash preflight
exoma install --uninstall                 # dry run: what would be removed
exoma install --uninstall --apply         # remove, with a receipt (can be rolled back)
exoma update                              # update package, global config, and current workspace
exoma update --timeout 1800               # allow up to 1800 seconds for each child step
```

`exoma update` detects the manager of the environment Exoma runs in: a uv tool installation
(`uv tool upgrade exoma`), then a pipx environment, recognised by the `pipx_metadata.json` pipx
writes into it (`pipx upgrade exoma`; refused with a message when pipx is not on PATH), otherwise
pip in the running Python environment. A uv or pipx on PATH that did not build this environment
changes nothing. pip's output goes to the terminal as it runs. When the pip step fails on an
externally managed Python (PEP 668: an `EXTERNALLY-MANAGED` file in the stdlib directory, outside
a virtual environment — Debian, Ubuntu, Homebrew), the update names `pipx install exoma` and `uv tool install exoma`
as the routes that work. It invokes the newly installed Exoma to apply global config, upgrades the
workspace when cwd or a parent contains `.workflow/`, and checks the resulting install. The
install it runs passes `--no-input`: every stored choice stands.
If that directory has a missing, unreadable, or malformed `config.json`, update stops before
changing the package. A later install or check failure reports an incomplete update and names
any new global install receipt for an explicit rollback; workspace changes are outside it.

Update streams manager stdout/stderr directly and reports stage start, elapsed completion,
and a waiting heartbeat on stderr every 15 seconds. A heartbeat indicates waiting, not
child progress. `--timeout SECONDS` accepts a positive finite number and defaults to 900
seconds independently for package, refresh and check; output never resets that deadline.
Manager detection via `uv tool dir` has a separate 10-second limit. A detection timeout
refuses update instead of selecting pip; optional installation preserves that timeout's
reason and refuses to install without a confirmed manager.
The limits apply to child execution, not total preflight/filesystem work. Timeout attempts
process-tree cleanup and skips subsequent steps; cleanup can outlast the deadline and
remains best-effort under OS access restrictions. Ctrl+C cleans up and exits 130. An
interrupted package step reports uncertain package state; a refresh/check interruption
retains the incomplete-update receipt and workspace recovery advice. Update children
receive no interactive input. See [manual recovery](installation.md#update) for older updaters.
Use `--manager uv`, `--manager pipx` or `--manager pip` to select the package manager
explicitly; `uv` and `pipx` are refused when this environment is not theirs.

All `exoma install` flags:

| Argument / flag | Effect | Writes? |
|---|---|---|
| `[claude\|agy\|codex\|opencode\|all]` | which main-agent host to install for (`all` installs all four); persisted | yes |
| (no argument) | a terminal asks for the hosts (any combination, the stored choice as default); otherwise the stored choice, else `claude` | yes |
| `--no-input` | ask nothing, from a terminal too: stored choices stand, flags supply the rest; `exoma update` passes it (see "Choices asked from a terminal") | yes |
| `--dry-run` | print the plan, write nothing (combines with every install flag below) | no |
| `--only-command` | command-only mode (see "Intent mode"); persisted, applies to subsequent installs | yes |
| `--auto-intent` | restore auto-intent mode | yes |
| `--no-agy-permissions` / `--agy-permissions` | the Agy allow rules are added whenever the `agy` host is chosen; `--no-agy-permissions` removes them and is persisted, `--agy-permissions` undoes that (see "Hosts") | yes |
| `--provider NAME` / `--model ID` | second agent choice for every chosen host's seed (`--model` requires `--provider`) | yes |
| `--with-e2e` | also install the Playwright + Chromium extra for `/.verify-browser`; never installed without it. The package goes in through the manager of the environment Exoma runs in (see "Browser extra"); a failed step exits 1 | yes |
| `--update-providers` | bring each provider CLI to its `stable_version`, global npm installs only (see "Provider releases") | yes |
| `--check` | drift report for the bundle, the global install, and the project when cwd has `.workflow/` | no |
| `--rollback [ID]` | undo one install/uninstall from its receipt (default: latest) | with `--apply` |
| `--uninstall` | remove what the workflow installed (see "Uninstall") | with `--apply` |

**Browser extra.** `--with-e2e` installs the pinned `requirements-e2e.txt` into the
environment Exoma runs in, through the manager that built it, detected as `exoma update` does:

| Environment | Package step | Kept across upgrades |
|---|---|---|
| uv tool | `uv pip install --python <exoma python> -r requirements-e2e.txt` | not by `uv tool upgrade`; the report names `uv tool install --reinstall --with <pin> exoma` |
| pipx | `pipx inject exoma <pin>` | yes, pipx records injected packages |
| pip | `<exoma python> -m pip install -r requirements-e2e.txt` | yes |

uv and pipx environments need not carry pip, so `python -m pip` is never assumed there. The
browser step (`<exoma python> -m playwright install chromium`) follows the package. Either step
failing, a pipx environment without `pipx` on PATH, or an environment with none of the three, is
reported as `e2e: failed — <reason>; run: <command>` in the install report and makes the install
exit 1 after it; the other install steps are still applied. A `--dry-run` reports a missing
package command as a warning (`an install would fail — …`), plans no browser download, and
exits 0. A `requirements-e2e.txt` that pins nothing installs nothing, with a warning, and is not
a failure; one missing from the package is.

Since 1.0 an install writes by default. `--apply` is still accepted on an install, with a notice
that it is no longer needed; it keeps its meaning for `--rollback` and `--uninstall`, which preview
without it. `--host <name>` is accepted as a deprecated spelling of the positional host. The 0.x
choice `both` is gone: each install replaces the stored host choice rather than adding to it, so
name `all` to keep several hosts.

<a id="detail-host-choice"></a>
**Host choice.** The main agent runs in `claude` (Claude Code, `~/.claude`), `agy`
(Antigravity CLI, `~/.gemini`), `codex` (Codex CLI, `~/.codex`), or `opencode` (OpenCode,
`~/.config/opencode`); `all` installs all four.
An earlier Claude+Agy selection remains valid when read from stored install state. The host is a different choice from the second agent
(`--provider`): the provider bundles install whichever host is picked. The hosts come from, in
order: the positional host (or the deprecated `--host`); the choice the last install recorded; for an install from before the choice
existed, what it wrote (Claude, plus Agy when `~/.gemini/config/hooks.json` holds the `exoma`
hook, plus Codex when `~/.codex/hooks.json` registers Exoma's lifecycle hooks, plus OpenCode when
`~/.config/opencode/plugins/exoma.js` exists — the last two only arise when the mode file was
lost); else `claude`, with a warning. From a terminal (see "Choices asked from a terminal") the
hosts are asked on every install with the stored choice as the default, and the answer replaces it. The choice is stored
with the intent mode in `~/.claude/.workflow-install-mode.json` (`hosts`, `agy_permissions`,
`agy_permissions_opt_out`,
`agy_permission_rules`), receipted like it. A host dropped from the choice is taken out: its
instruction block, its hook registration (`settings.json` hooks, the Agy `exoma` hook, or
Exoma handlers in Codex `hooks.json`; plus Agy permission rules; the OpenCode plugin is a file
and goes with the prune), then its files through the stale prune — only files an install
wrote and nobody edited. `~/.claude/` still holds the installer's own state (backups, ledger,
mode file, `hooks/python.path`) when only `agy` is chosen; no Agy file reads it.

<a id="detail-bundle-layout"></a>
**Bundle layout.** `exoma/_bundle/config/` holds one folder per role: `hosts/<name>/` for a
main-agent host (`hosts/claude/`, `hosts/agy/`, `hosts/codex/`, `hosts/opencode/`) and `providers/<name>/` for a second-agent
provider (`providers/{opencode,codex,agy,claude}/`), so a host and a provider may share a name. A
bundle key — in the manifest, the install ledger and every receipt — is a path relative to that
folder. Keys an install before v0.3.0 recorded (`claude/...`, `agy-host/...`, `<provider>/...`)
are read as their current form (`config/bundle_keys.canonical_key`), so stale cleanup,
`--uninstall` and doctor still recognise what an older install left; keys are only written in
the current form. doctor (`checks.bundle_integrity`) skips the files of a host the install did not
choose (`skipped`, reason `host_not_chosen`) and the project-root boundary of every provider the
project does not run (`skipped`, reason `provider_not_selected`), as `--check` does. A file the
install renders rather than copies (a skill naming its host, the OpenCode plugin shim) is compared
against the installer's rendering, not the manifest hash.

doctor also writes `checks.hosts`: `{chosen, hosts: {<host>: {missing: [...]}}}` for every host the
install chose (a stored Claude+Agy selection lists both; with no recorded choice it is Claude, plus Agy when
`~/.gemini/config/hooks.json` holds the `exoma` hook, plus Codex when `~/.codex/hooks.json` registers
Exoma's lifecycle hooks — the same inference as the installer's). Claude: the `WORKFLOW-MAIN-AGENT` block, the
skills, the hooks. Agy: the `EXOMA-HOST` block, the `exoma` hook in `hooks.json`, `CONTRACT.md`, the
hook scripts, the skills, and — with `permissions: on` — every permission rule this machine needs,
whether Exoma added it or the user already had it. A host with something missing
is an issue, fixed by `exoma install`. Codex checks its main-agent block, contract,
hook file, and skills; hook trust remains a separate `/hooks` review. OpenCode checks its
`EXOMA-OPENCODE-HOST` block, the plugin, the contract, the hook files, and the skills; a running
OpenCode loads the plugin only when it starts. These are file checks; what only a session in the
host can see is the Agy `/.doctor` STEP 3 ("Hosts", "Probe").

### Native command rendering and Codex migration

Workflow command IDs are bare names such as `plan` and `verify-browser`. Shared templates
use `{{CMD:plan}}` and `{{CMD_PREFIX}}`; `config/hosts.py` renders them for the main host.
Codex guidance uses `$exoma-plan`, `$exoma-execute -y`, and `$exoma-verify`. Legacy dotted
aliases remain compatibility inputs. Claude Code retains `/.plan`; other hosts retain
their existing invocation surfaces. The second-agent provider does not select this format.

Rendering applies to skill descriptions and bodies before native frontmatter is built,
as well as the contract, help, next steps, and hook guidance. Host sections select question
and process-wait instructions; command-only mode applies to help and skills too. The registry
validators understand command tokens, and generated Claude wrappers receive rendered metadata.
Source templates are never refreshed from files in an installed home.

For first-party CLI guidance, `--main-host codex` is an optional presentation context on
the CLI and generated runner. It is separate from init's `--host`, provider selection,
and permissions. Only authored guidance fields are formatted; evidence, digests, command IDs,
verdicts and exit behavior are preserved. Without the option the existing presentation remains.

When updating an older workspace, bootstrap with `exoma upgrade --work-dir <root>`
before using runner examples containing `--main-host`. This bootstrap does not need the new
runner option. Refresh global host files with the installer; workspace upgrade and global
host refresh are separate operations. Review changed hooks through `/hooks` in Codex.

The bound Codex hook checks explicit session arguments on stateful runner calls before
dispatch, including when no delegated marker is armed. Missing, default, duplicate,
ambiguous, or different session arguments are refused in blocking mode. Task contents are data.
The check depends on hook delivery and the configured enforcement mode; standalone CLI calls
without a host binding retain their existing session behavior. Doctor prioritizes its explicit
session argument and consults cached state only when none was supplied.

`AGENT_HOME` (env) replaces `~` as the target home — used by tests and the installer e2e to
install into a temporary HOME. The runtime reads the interpreter pin (`.claude/hooks/python.path`)
from the same home, so `.workflow/*.sh` generated under `AGENT_HOME` name the interpreter that
install pinned there, not the one in the real home. `EXOMA_HOME` moves runtime data only.

Bundle drift (`--check`, and the install refusal) is computed in both directions: bundle files
(`exoma/_bundle/config/`) whose hash does not match or that are not recorded in
`exoma/_bundle/manifest.json`, **and** manifest entries whose source file no longer exists. `doctor` (`checks.bundle_integrity`) first validates the shape of each
manifest entry: an entry that is not an object, lacks `path`, or lacks a 64-hex `sha256` makes the check
stop with `error` + `malformed` (the entry indices), which `doctor` turns into an issue — not local
install drift.

**An install writes; `--dry-run` previews.** It writes to the global agent config, read by
every project on that machine, so every overwritten file is backed up and receipted, and
`--rollback` undoes a whole install (below).

What an install does:

| Target | Strategy |
|---|---|
| `~/.claude/CLAUDE.md` | replace content **between the** `WORKFLOW-MAIN-AGENT` **markers** — handwritten text outside the markers is preserved. File does not exist yet → **only the marker block** is written |
| `~/.claude/skills/*.md`, `~/.claude/commands/*.md` | replace (template) |
| `~/.claude/hooks/*` | replace; one set for all OSes: `<hook>.sh` launcher, `<hook>.py` logic, `workflow_hooks.py`, `intent-map.json`. `.ps1` files from old releases are deleted as stale files (edited ones are left alone and reported) |
| `~/.claude/settings.json` | add the workflow's hooks and `statusLine`; user hooks and keys are preserved (see "Registered hooks") |
| `~/.claude/exoma/second-agent.md` | replace (whole file): the claude second agent's contract, passed per call with `--append-system-prompt-file`. Never a block in `~/.claude/CLAUDE.md`, which the user's own sessions read |
| `~/.config/opencode/AGENTS.md`, `~/.codex/AGENTS.md` | replace content between the `WORKFLOW-SECOND-AGENT` markers; file does not exist yet → only the marker block. That block applies only to runtime calls (instructions opened with `[WORKFLOW_AGENT]`); provider sessions the user runs directly ignore it |
| `~/.gemini/config/agents/exoma-second-agent/agent.md` | replace (whole file: Agy reads the custom agent's frontmatter only at the top). The agy second agent's contract, selected per call with `--agent` (see "Choosing a provider"), so an Agy host session never reads it. A `WORKFLOW-SECOND-AGENT` block an earlier release left in `~/.agy/AGENTS.md`, which Agy does not read, is cut (backed up, receipted; the user's text stays) |
| `~/.claude/.workflow-install-mode.json`, `~/.claude/.workflow-installed.json` | the selected intent mode and the ledger of files written by install (for cleaning up stale files) |
| `~/.config/opencode/opencode.{json,jsonc}` | additive merge for general config; the workflow's `agent.plan` permission is enforced, other user keys are preserved |
| `~/.config/opencode/agents/*.md` | replace — one global subagent roster (`exm-*`), used by every project managed by the workflow. The `wf-*` files of 0.x go through the stale prune; the `wf-*` entries 0.x merged into `permission.task` of the user's `opencode.json` stay (they allow agents that no longer exist) |
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

#### Seeds `second_agent.seed.<host>.json`

`<data dir>/config/second_agent.seed.<host>.json` — one per chosen host — lives in the per-user
data directory, outside any repository. The installer **rebuilds each on every install** from
`second_agent.example.json`. `init` copies the seed of the host that ran it into the new
`.workflow/second_agent.json`: each host's `/.init` passes `exoma init --host <host>`
(rendered into the skill at install), and `adapters/install/opencode_install._copy_provider_config`
takes, first that exists, `second_agent.seed.<host>.json`, the shared `second_agent.seed.json`
(written by 0.x installs, still honoured), then the example. An install from a terminal asks
once and writes that one choice into every chosen host's seed; seeds that already differ keep
their own choice only on a non-interactive install. One project changes its second agent with
`/.provider`, which writes its `.workflow/second_agent.json`. The runtime itself never reads it: a project
without `.workflow/second_agent.json` is rejected with `provider_config_missing`, a broken file is rejected with
`provider_config_invalid`. There is no longer a silent fallback to machine-level config.

Only the CHOICES (`provider`, `default_model`, per-route models) are carried over from the previous seed;
every other key is rebuilt from the example, so no stale keys carry over.
Order in which the choice is determined:

| Condition | What happens |
|---|---|
| `--provider NAME` (optional `--model ID`) | used as is, zero questions |
| an install from a terminal | the installer **asks once**, for every chosen host, for the provider, the model, then the effort, the first chosen host's previous choice (its seed, else the shared seed) as each step's default; the answer is written into each host's seed, and a note says so when an answer was given and those seeds held a different provider or model |
| non-interactive, a previous seed for this host exists | its old choice (provider, model, effort, per-route models) is reused on top of the new example |
| non-interactive, no host seed, a shared 0.x seed exists | the shared seed's choice is carried into the host seed |
| non-interactive, no seed yet | `second_agent.example.json` is copied as is |

An old `second_agent.json` beside the seed (written once, never refreshed — the source of
`opencode/mimo-v2.5-free` being copied into new projects) is renamed to
`second_agent.json.retired`; its choice is **not** carried over and is mentioned in a warning.

The non-interactive path is not an oversight: `tools/e2e/e2e_installer.py` and CI call
`exoma install` without stdin, and a prompt there would **hang** the run, not fail it.

The prompt shows each provider along with whether its CLI is on `PATH` and whether it keeps a
read boundary (codex, agy and claude are marked as trusted providers), then the selected
provider's models from the live catalog (`core/provider/model_catalog.py`: what the installed
CLI lists now, newest first, at most 20; the bundled shortlist when the CLI cannot be asked —
always for claude, whose CLI has no model listing) with the reasoning efforts each accepts, then
those efforts. Blank keeps the previous answer at each step: on the provider with no previous seed
it cancels the selection (the example is copied); a different provider inherits neither the old
model nor its effort, and per-route models survive only an answer that keeps provider and model.
A model the catalog does not list has no effort question; kept unchanged, it keeps its stored
effort (as `--no-input` does), and another model starts without one.
A picked effort is written to the seed's `effort`; for agy it names a model variant and is written
into the model id instead. Blank on the model with no previous one leaves `default_model` empty — for opencode that is
rejected at call time (`model_unset`), because without `-m` opencode uses the last model
used on that machine. Model ids outside the catalog are still accepted with a warning.

<a id="detail-install-choices"></a>
**Choices asked from a terminal.** An install that writes, with stdin a terminal and without
`--no-input`, asks before anything is written: the hosts; the intent mode
(unless `--only-command`/`--auto-intent` is given); the browser extra (unless `--with-e2e`; default
no); the Agy allow rules when `agy` is chosen (unless `--agy-permissions`/`--no-agy-permissions`;
default the stored choice); `--update-providers` (unless given; default no); then the second agent,
once for every chosen host (unless `--provider`). Enter keeps the default; end of input (Ctrl+D, Ctrl+Z) answers
blank. Ctrl+C at any question stops the install with `[INSTALL] ABORTED — interrupted at a
question. Nothing was written.` and exit status 130, before a receipt or any file is written. A dry
run, a run without a terminal, and `exoma update` ask nothing.

The model is pinned to `SELECTABLE_ROUTES` (`explore`, `plan`, `analyze`, `verify`, `e2e_spec`);
`sweep` is left alone. `routes` is edited per key via `_merge_routes` so that per-route `timeout_seconds`
and `agent` are not discarded.

The seed is **not** part of the install receipt, because a receipt entry without a
backup would be DELETED on `--rollback`, while this file holds user edits after it is
created.

`--check` reports it as a separate `second agent default:` line — `NOT SET`,
`UNREADABLE`, or `<provider>, model=<id>` — and it is deliberately **outside** the drift count:
this file has no counterpart in the bundle, so it has no source to drift from, and its absence
is not breakage (init still works via the example). One `second agent (<host>):` line per chosen
host follows it, naming that host's `second_agent.seed.<host>.json` selection, or saying the host
has none and init uses the shared seed.

When the installer is run from inside a project that already has `.workflow/`, it performs an **in-place upgrade**: the old workspace layout is migrated to `.workflow/data/` (with a backup), scripts are regenerated, the config version stamp is updated, `<project_root>/opencode.json` is refreshed, and `sessions/` is preserved. A new workspace is not scaffolded by the installer — use `exoma init --work-dir DIR` (skill `/.init`). The workspace upgrade is refused while there is still an active job; the global install still completes, but `exoma install` exits nonzero and reports the unfinished workspace in its warnings.

### Statusline (second agent badge)

The installer installs `~/.claude/hooks/workflow-statusline.sh` (along with its `.py`) and registers it
as `statusLine` in `settings.json`.

`settings.json` is **additive**: only keys that do not exist yet are written, and since 3.8.0 the template
carries only `hooks` and `statusLine` — `model`, `enabledPlugins`,
`extraKnownMarketplaces`, `effortLevel`, and `permissions.defaultMode` are no longer planted
(those are preferences, not workflow requirements; ones already planted by old releases are left alone). Already have
your own `statusLine` → yours wins, the badge does not appear, and the collision is reported in the
install output. Delete that key and rerun `exoma install` if you want to use
the shipped one.

The script file itself follows the `~/.claude/hooks/*` rule in the table above: **replace**,
with a backup first. Have your own `workflow-statusline.sh` there → it is overwritten
(its copy is in `~/.claude/backups/install_<timestamp>/`). What is preserved is the
`statusLine` key in `settings.json`, not the file in `hooks/`.

```
my-app | Second Agent 15.2M tok (14.5M cached) / 8 calls | Saved 642.7k tok
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

**All numbers are session-scoped.** `/clear`, `/compact` and a fork (`/branch`, `/fork`,
`--fork-session`; SessionStart `source: fork`) give a new `MAIN_SESSION_ID` (`resume` reuses
the old one), so all three return to zero together. A fork shares nothing at runtime with the
session it came from — no plan, marker, token or execution — so `/.execute -y` in a fork needs
its plan recorded there. That is deliberate:
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
| `SessionStart` | `startup\|resume\|clear\|compact\|fork` | `session-bind` | binds the Claude session to `MAIN_SESSION_ID` |
| `UserPromptSubmit` | — | `intent-gate-set` | auto-intent mode only: sets the gate marker for delegated prompts |
| `UserPromptSubmit` | — | `task-events` | local skill from the prompt; HEAD snapshot |
| `PreToolUse` | `mcp__.*\|Read\|Grep\|Glob\|Bash\|PowerShell\|Write\|Edit\|MultiEdit\|NotebookEdit` | `intent-gate-check` | blocks gather tools while the gate marker exists; Bash and PowerShell go through one tokenizer and one rule: only a single plain call `[bash] <root>/.workflow/{run,check,inspect}.sh` passes (each word plain or `'...'`; `$`, backtick, `;`, `|`, `&`, redirect, newline only inside single quotes; double quotes, `bash -c`, another interpreter in front, `run.ps1`, paths outside the root, `..`, and look-alike names are rejected); Write/Edit pass except to that runner script; a marker with intent `verify` lets the test-selection path through (see "Intent mode") |
| `PostToolUse` | `Skill\|Edit\|Write\|MultiEdit\|NotebookEdit` | `task-events` | skill via the `Skill` tool, edited files |
| `Stop` | — | `graph-refresh` | regenerates the graph after a turn that implemented something (detached) |
| `Stop` | — | `task-events` | commits since the HEAD snapshot; the anchor check, reading `transcript_path` backward to the newest plan/analyze runner call |

Hooks are recognized by their script name. On install, the workflow's hooks are refreshed per event,
user hooks are left alone, and workflow script commands that this release no longer registers on
an event are removed from that event (for example `task-events` on `PreToolUse` from the 3.8.0 draft).
The command in the template is the same for all OSes, `{{HOOK_BASH}} "{{HOME}}/.claude/hooks/<hook>.sh"`;
the installer fills `{{HOOK_BASH}}` with `bash` on POSIX and the absolute Git Bash path (quoted) on
Windows, because a plain `bash` there can fall through to WSL bash in `System32`. On Windows the
installer also adds `env.CLAUDE_CODE_GIT_BASH_PATH` (Claude Code's own setting for where Git Bash
is) when `settings.json` does not set it; a value the user set is kept, and one naming a missing
file is reported. Without it, a Claude Code that cannot find Git Bash by itself (a headless
`claude -p` started from PowerShell) runs the hook commands through PowerShell, where the quoted
`"<bash>" "<script>"` pair is a parse error and every PreToolUse hook blocks its tool.
The installer writes `~/.claude/hooks/python.path` (its own interpreter) beside the launchers.
A launcher runs that pinned interpreter, else the first of `python3`, `python`, `py -3` on PATH
that runs as Python 3.11 or newer. `/.doctor`'s `python_callable` check asks the same question
in the same order, so a machine with `python3` and no `python` alias is ready; it reports
`python not callable (<what was tried>)` only when the launchers would find nothing either, or
when the pin is older than 3.11 (the launchers would still run it), naming the repin steps.
A hook receives its payload on stdin, read as UTF-8 bytes whatever the console code page is,
and `workflow_hooks.py` hands it to the hook module in memory: there is no size limit (the
environment variable it used before failed past 32767 characters on Windows, so every large
Edit/Write crashed `task-events` and the PreToolUse gates failed open). A hook that raises, or
exits with anything but 0 or 2 (a message exit included), exits 0 and appends the traceback to
`~/.claude/hook-errors.log` (beside the registry, never beside the hook scripts); exit 2 still blocks. `session-bind` replaces
`session_registry.json` under a sidecar lock file (`session_registry.json.lock`, created
exclusively and holding its owner's token; a holder removes only its own lock), so sessions
starting together keep their bindings. Two limits: after 2 s of waiting a bind goes ahead
unlocked rather than delay the session start, and a lock older than 10 s (its holder died) is
removed only while it still holds the token judged stale, which leaves the instant between
that check and the removal unguarded.
Without Git Bash on
Windows, or without `bash` on PATH on POSIX, an install is refused (exit 6) before writing anything.
Maintainer and e2e tools that run hooks or the runner also refuse (rather than guessing a plain
`bash`) when bash is not found. Old commands of the form
`powershell ... .ps1` are recognized by their hook name and replaced.

### Uninstall

`exoma install --uninstall` (dry run), then `--uninstall --apply`:

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
- `~/.gemini/config/hooks.json` loses the `exoma` hook name; every other name stays. A file
  that held only `exoma` is deleted. The `EXOMA-HOST` block is cut from `~/.gemini/GEMINI.md`
  like the other instruction blocks;
- the intent mode and ledger are deleted.

`settings.json` is cleaned **before** hook scripts are deleted, and is read leniently (BOM, cp1252). If
it cannot be parsed or is not a JSON object, all `hooks/` scripts are left alone and uninstall
warns about it, so no hook ever points at a missing file. The same holds for Agy's `hooks.json`:
it is cleaned before the scripts, and when it cannot be parsed or is not a JSON object it is left
as is with a warning and the `hooks/` scripts (which hold `agy-host.py`) stay. Instruction files whose
markers are unpaired (START without END, END without START, END before START, or more than
one block) are refused at install (action `refused` + a warning with the path and both markers) and
left alone at uninstall.

Not touched: the seeds `second_agent.seed.<host>.json` (and a 0.x `second_agent.seed.json`) and
`.workflow/` in each project. All changes go through the same backup + receipt as install,
so `exoma install --rollback --apply` undoes an uninstall.

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
removes a marker set by a user prompt in the same turn. Claude Code sends such
turns via `UserPromptSubmit` too. A turn counts as not the user's when its `source` is `system`,
`loop_wakeup`, `schedule_wakeup` or `poll_event`; when it carries `agent_id` (it runs inside a
subagent); or when, after any leading `<system-reminder>` blocks are removed, it is empty or
opens with `<task-notification>`, `[SYSTEM NOTIFICATION`, `<agent-message` or
`Another Claude session sent a message` (a native subagent's hand-back). `source` `user` and
`sdk` stay classified, and a user prompt that only mentions one of these shapes mid-sentence
still counts as the user's. Claude Code does not document `source`, so a project may extend both
lists with `policies.prompt_sources` (`{"user": [...], "host": [...]}`) in `.workflow/config.json`.
A label in neither list is treated as the user's, with one limit: `execute.token` and
`commit.marker` are then written only when the command opens the prompt (`/.execute -y ...`,
`commit ...`) or arrives as the slash-command payload; each such label is counted in
`~/.claude/prompt-sources.json`. Text inside `<pasted_content>` is never classified and approves
nothing. Previously a task name, an agent report or a snippet of output could match
the NL map and set a gate nobody asked for, which also blocked the main agent's own subagents
during `/.execute`. A real marker still binds subagents: their gather calls go through
`intent-gate-check` like the main agent's. The marker records `source`, `has_agent_id` and
`prompt_id` beside `prompt_excerpt`, so a false arming names its origin.

Up to 3.8.0, `intent-gate-set` read the prompt from the `user_prompt` field, which Claude Code never
sends (the field is `prompt`). As a result the runtime gate was never active and only the
prompt layer worked. Since that fix, in auto-intent mode `intent-gate-check`
actually blocks gather tools after a prompt that names a delegated command until
`.workflow/run` is called; the escape is still `WORKFLOW_LOCAL_MODE=1` or `local_mode.flag`.
Either lifts the gather block only: a Write of the runner scripts or of an escape file is
refused under both, on every host.

Only a command the user named arms the gate: the prefix typed at the start of the prompt
(`/.analyze ...`, `prefix_regex` in `intent-map.json`) or the slash-command payload
(`<command-name>/.analyze</command-name>`). A prompt that only resembles a delegated command
under the NL map arms nothing and clears a pending marker; it gets an `[INTENT HINT]` as
`additionalContext` naming the command and the runner call, and the prompt-level gate decides.
Before agent-workflow 3.9.0 any NL match armed the gate, so questions and descriptions ("saat saya analisa
...", a question that mentions playwright) blocked the main agent's own reply.

The runner passes only when it is the command itself: the first word is this project's
`.workflow/{run,check,inspect}.sh` path, or the second word after a plain `bash` (no
flags), and that path, every symlink followed, still lies in the project's `.workflow`: a
`run.sh` linked to a script elsewhere is not the runner. The Bash and PowerShell tools are read by the same tokenizer: each word must be plain (letters, digits,
`_ . / : = + % - ^`, and `~` mid-word) or wrapped in single quotes, which are literal in both
shells. That is why a task may contain `$`, `;`, `|`, `&`, `<`, `>`, parentheses and line breaks as long as they are inside
`'...'`; a line break outside quotes ends the command and is refused, double quotes are rejected because both shells expand `$` inside them, and an apostrophe
inside the task cannot be written. A command may open with `cd <dir> && ` when `<dir>` is the
project root or inside it (the Bash tool resets the working directory between calls); the rest
is judged on its own. A runner path that appears as an argument of another command
(`python -c "..." x/.workflow/run.sh`) and `run.ps1` (retired) are still blocked.

The `command` field in the marker selects the pass rule. For `verify`, main_agent must select tests
before delegating, so three things pass: a plain `git diff` or `git -C <dir inside the root> diff` (same word rules as the runner)
with `--name-only`, `--name-status`, or `--stat[=N]`, plus optional `--cached`, `--staged`,
`--relative`, `--no-renames`, `--no-color`, `--`, refs, and paths (other options such as `-p` or
`--output` are still blocked); Read of `<root>/.workflow/config.json`; Read/Write of
that session's `sessions/<id>/verify/tests.json`. Dependents can only be inferred from the file
names in the diff. The `PreToolUse` matcher includes `Write`/`Edit`/`MultiEdit`/`NotebookEdit`:
writes are not gathering, so they pass the pre-flight gate (the execution gate still judges
them). The one exception is a write to the `.workflow/{run,check,inspect}` runner scripts (any
extension), refused **whether or not a marker is pending**: the runner is the one command the
gate lets through, so a runner rewritten between delegations would pass the next armed gate as a
free command. `/.upgrade` regenerates the runner; nothing else writes it.
The hook is tested through the `.sh` launcher that is actually installed, on Windows through Git Bash and on Linux.

For every `command` in the marker, Read of a skill definition also passes: an absolute path with no
`..` segment, ending in `.md`, the file exists, and its real path (all symlinks/junctions followed) is
inside the real path of `~/.claude/skills`. A symlink may point anywhere inside the skill folder,
not outside it. The reason: the Skill tool tells the agent to read `~/.claude/skills/<name>.md`
after the prompt that invoked it has armed the gate, so the dispatch instructions would be locked
until after dispatch. `~/...` and Git Bash's `/c/...` spellings are read as the shell
reads them before the check. Grep/Glob over that folder, non-`.md` files, and relative
paths are still blocked. A Read of a background task's output (`<temp>/claude.../tasks/<id>.output`,
an existing file under the temp directory) passes too: that is where a delegation started in the
background returns its result. The layout is Claude Code's and undocumented; if it moves, those
reads are gated again. `cat <file>` (`type`, `Get-Content`, `gc`) of one such file, bare or
single-quoted with nothing else on the line, passes like the Read.

For every `command` in the marker, a **precision Read** also passes: a file inside the
project root (relative paths resolved against the root, symlinks followed), not under
`.workflow/`, spanning at most 200 lines — `limit` a number 1–200, or no
`limit` on a file that is itself ≤ 200 lines (files > 1 MB are not counted, so they do not pass). At
most 2 per marker; the count is in `runtime/delegated.reads`, keyed to the marker's `set_at`,
so a new marker starts from zero. This is the enforced number for the "small
slice, one/two anchors" exception in the `CLAUDE.md` Pre-flight gate rule: file:line attribution by the main
agent itself, not evidence gathering. A 3rd Read, a span > 200 lines, a large file without
`limit`, a file outside the root, Grep, Glob, MCP, and shell are still blocked, and the block message
cites this rule. A marker without `set_at` (not written by `intent-gate-set`) does not open this lane.

`src/exoma/_bundle/config/hosts/claude/CLAUDE.md` deliberately contains **both** mode stanzas — `COMMAND-ONLY` and
`AUTO-INTENT`, each wrapped in `<!-- <MODE>:START -->` / `<!-- <MODE>:END -->` markers.
That file is the source before trimming, not the installed result. On install, the installer
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

The bundle (`src/exoma/_bundle/`) is bootstrapped from the maintainer's live config:

```bash
python tools/maintain/extract_config.py --dry-run
python tools/maintain/extract_config.py
```

Its security posture, in order of importance:

1. **Allowlist, not blocklist.** The agent home contains `.credentials.json`, a `history.jsonl` of hundreds of KB, session transcripts, and project paths. A blocklist ships anything it forgot to name; an allowlist ships only what is named.
2. **Fail closed on secrets.** Every byte is scanned for credential patterns. A single hit **aborts the whole run and writes nothing** — a warning would be read, ignored, then committed. A secret in git history is practically impossible to revoke.
3. **Read-only.** The extractor never writes outside the bundle.

Absolute paths are redacted to `{{HOME}}` / `{{PROJECT_ROOT}}`; the installer restores them.

### E2E

```bash
python tools/e2e/e2e.py          # local + installer: no delegated run/quota
python tools/e2e/e2e.py --full --model <id> [--provider opencode|codex|agy]
                                 # plus delegated commands: minutes + real quota
```

`--full` is opt-in, and needs `--model`: the shipped seed names no model, so without one the
run stops at argument parsing (exit 2) before any check or call. `--provider` defaults to
`opencode`. The temporary project gets that choice through `--command provider`, the path
`/.provider` takes, before the first delegated call. Delegated commands use the same rate-limit budget the workflow itself needs; a suite that silently burns it is worse than no suite.

The installer is tested against a **temporary HOME**, not the machine that produced the bundle — installing onto the origin machine only proves idempotence, it never exercises the *create* path a new user faces.

`SKIPPED` is not `PASS`, and is reported separately.

---

## Hosts

The host is the CLI the developer talks to: it runs the main agent, and its hooks enforce the
runtime's rules at the tool boundary. Claude Code is the reference host; Agy (Antigravity CLI),
Codex CLI and OpenCode are additional hosts. `exoma install <host>` picks which are installed ("Host choice"). The decisions the hosts
enforce live once, in `workflow_policy.py` (one source, installed beside each host's hooks): the
pre-flight decision (`preflight_refusal`), write root and scope, commit approval, the user's
approvals, intent classification, the pre-flight marker, the `[EXECUTION RESULT]` claim. Each
host keeps only its payload parsing — which of its tools is a `write`, `shell`, `read` or
`other` call, and what it may always read (its skills, its task output) — and its answer
format. A Claude hook that cannot import that
module (a partial install) exits 0 without acting, like every other state it cannot read.

| Capability | Claude Code | Agy (as calibrated, CASE-029) | Codex CLI | OpenCode |
|---|---|---|---|---|
| Instructions | `~/.claude/CLAUDE.md` block | `~/.gemini/GEMINI.md` block `EXOMA-HOST`, pointing to its rendered `CONTRACT.md` | `~/.codex/AGENTS.md` block `EXOMA-CODEX-HOST`, pointing to `~/.codex/exoma/CONTRACT.md`; separate from the Codex provider block | `~/.config/opencode/AGENTS.md` block `EXOMA-OPENCODE-HOST`; the rendered `~/.config/opencode/exoma/CONTRACT.md` is carried in the system prompt by the plugin (no `read` call); separate from the opencode provider block |
| Commands | skills + slash commands | `/.<name>` skills | `$exoma-<name>` skills under `~/.codex/skills/`, listed by `/skills` (Codex has no custom command files) | `/exoma-<name>` commands under `~/.config/opencode/commands/`, each running the `exoma-<name>` skill under `~/.config/opencode/skills/`; `/.<name>` and `$exoma-<name>` in a prompt map to workflow commands |
| Prompt routing | `UserPromptSubmit` | `PreInvocation`, `invocationNum` 0 | `UserPromptSubmit`; explicit skill mentions map to workflow commands | plugin `chat.message` |
| Context injection | `additionalContext` | `PreInvocation` ephemeral message | `SessionStart` and `UserPromptSubmit` additional context | plugin `experimental.chat.system.transform`, on every model call |
| Tool gate | `PreToolUse`, exit 2 blocks | `PreToolUse`, `decision: deny` | `PreToolUse`, `permissionDecision: deny`; requires hook trust | plugin `tool.execute.before` throws |
| Session identity | `SessionStart` + `session_registry.json` | `conversationId` + Agy registry | `SessionStart` + per-thread files under `~/.codex/exoma/sessions/` | first prompt of a session + per-session files under `~/.config/opencode/exoma/sessions/` |
| End of turn | `Stop`: status guard warns | `Stop`: status guard continues once | `Stop`: status guard warns (`systemMessage`); skipped while `stop_hook_active` | not registered |
| Graph refresh, task telemetry, statusline | Registered | Not registered | Not registered | Not registered |

### Host profiles and rule labels

One contract source, `hosts/claude/CLAUDE.md`, is rendered for every host; what differs per host
is data in `src/exoma/config/hosts.py` (`PROFILES`), not a rewrite of Claude's text:

| Profile field | Claude Code | Agy | Codex CLI | OpenCode |
|---|---|---|---|---|
| Runner prefix (`{{RUNNER}}`) | `bash` (the Bash tool is Git Bash on Windows) | this machine's bash by full path; on Windows `& '<Git>/bin/bash.exe'` (PowerShell) | as Agy | as Agy |
| Shell tool (`{{SHELL_TOOL}}`) | the Bash tool | `run_command`, sandbox bypassed | `exec_command` | the `bash` tool |
| Waiting for the runner (`{{DELEGATED_RUN}}`) | background-task tool, collect the same task ID | Agy backgrounds a long call itself; read that task's output | `exec_command` with a long timeout; normal permissions when allowed, conditional escalation only when needed and permitted; wait on the same session | foreground, 600000 ms timeout (no background tool) |
| Asking the user (`{{ASK_USER}}`) | AskUserQuestion | `ask_question` | the user-input tool, else the reply | the reply |
| Skill path (`{{SKILL_PATH}}`) | `~/.claude/skills/<name>.md` | `~/.gemini/antigravity-cli/skills/.<name>/SKILL.md` | `~/.codex/skills/exoma-<name>/SKILL.md` | `~/.config/opencode/skills/exoma-<name>/SKILL.md` |

Rendering (`render_contract`): a `<!-- HOST:<a>,<b>:START -->` … `<!-- HOST:<a>,<b>:END -->`
section keeps its text only for the hosts it names, and the markers never ship; the placeholders
above take the profile's values. Codex, OpenCode and Agy get the managed block alone, as before.
The skills are still Claude's text: their `bash '<…>/.workflow/run.sh'` gets the host's runner
prefix and their skill path the host's, and the host's instruction block maps their tool names.

**One runner call per host.** The installer writes `host-profile.json` beside each host's hook
scripts (with `workflow_policy.py` and `intent-map.json`, the list `HOOK_DATA_FILES`):
`{host, runner_prefix, background, read_range_keys}`. The Agy, Codex and OpenCode hooks read it
for the `[INTENT HINT]`, for the runner call they name in a refusal, and for the bash a runner
call may start with, so the contract, the hint and the gate spell one call. Without the file (an
install from before it) a hook falls back to finding Git Bash itself, as before. Claude Code's
hooks keep the bare `bash`. A PowerShell call operator in front of the quoted bash (`& '<bash>'
'<root>/.workflow/run.sh' …`) is dropped before the gate judges the words, on Agy, Codex and
OpenCode alike.

**Read ranges.** A precision read passes on its line span. `read_range_keys` names the host's own
argument keys: a limit key (`limit`) gives a span; a start and an end line (Agy's `StartLine` /
`EndLine` are the assumed names) give `end - start + 1`; when both are given the larger span
counts. An end without a start, an end before the start, a value that is not a whole number in
ASCII digits (at most 9), or no key at all leaves the span unknown, and the whole file is
measured, as before.

**Rule labels.** Every rule in the contract carries `<!-- rule:<id> -->`; `src/exoma/config/rules.py`
holds one entry per id with its level on each host, and the render turns the marker into
`<!-- rule:<id> | <host>: <level> -->`:

- `enforced` — a hook or the runtime refuses the call on that host. A hook-backed rule holds
  only while its hook can resolve the session (and, for the pre-flight gate, only while a
  marker is armed); the hooks fail open otherwise, so `enforced` names what refuses an
  armed, bound call, not a guarantee for every call;
- `detected` — recorded and reported after the fact (`execute-end`, the end of a turn), not blocked;
- `advised` — nothing checks it there.

A level above `advised` names what backs it: the shared-policy function the host's hook files
call (`hook_symbol`), or a runtime function (`runtime`). `tests/checks/host_profiles.py` fails
when a marker has no registry entry or an entry no marker, when a level is not backed by a call
in that host's hook files, or when a non-Claude contract carries Claude-only text
(`run_in_background`, `AskUserQuestion`, `~/.claude/skills`, an unrendered placeholder or HOST
marker). Shell and script writes are `detected` on every host: no hook sees them, and
`execute-end` lists every changed file outside the plan's `files` in `meta.outside_scope`.

**OpenCode host.** Install with `exoma install opencode`, then restart OpenCode: it loads plugins
at startup. `~/.config/opencode/plugins/exoma.js` is a shim (OpenCode plugin API v1, 1.19) that
hands each event to `~/.config/opencode/exoma/hooks/opencode-host.py` as JSON on stdin and acts on
the JSON it prints; the decisions are `workflow_policy`'s, as for every host. It does nothing in a
process started with `WORKFLOW_AGENT_ROLE=second_agent`, and fails open (a hook that cannot run
answers nothing). OpenCode exposes no session-start hook, so the first prompt of a session in a
project with `.workflow/` binds it; the binding is re-injected on every model call, followed by
the rendered `~/.config/opencode/exoma/CONTRACT.md`, which the hook reads itself. The agent never
reads the contract with a tool: that path lies outside the project, so a `read` would ask for
`external_directory` access (and an initialised project's `opencode.json` denies it); only when the
file cannot be read does the binding tell the agent to read it. The second agent never gets the
contract (the plugin and the hook skip that role). The install also writes one
`~/.config/opencode/commands/exoma-<name>.md` per Claude `/.<name>` command: typed `/exoma-<name>`,
it expands to `$exoma-<name> <arguments>` and an instruction to load the `exoma-<name>` skill with
the skill tool, so the hook reads the command at the prompt's start (arming a gate, recording
`-y`) as if it were typed. The status
guard is not registered: OpenCode offers no end-of-turn hook a plugin can answer. Its `AGENTS.md`
block coexists with the opencode second-agent block, as for Codex.

**Codex host.** Install with `exoma install codex`, then review and trust the
Exoma handlers with `/hooks` in Codex before relying on the gates. `~/.codex/hooks.json` is
merged: Exoma adds one handler for each of `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
and `Stop` and preserves other handlers. Both commands single-quote their paths, so neither shell
expands `$` or backticks in them: `command` (POSIX shell) is `'<python>' '<codex-host.py>' <Event>`
with `'` written `'\''` inside; `commandWindows` opens with PowerShell's call operator,
`& '<python>' '<codex-host.py>' <Event>`, with `'` doubled inside. On Windows Codex runs a hook as
`[Console]::In.ReadLine() | <command>` under `powershell -NoProfile -Command`, where a leading
quoted path without `&` is a parse error. Pre-release builds of this version wrote both commands
double-quoted and without `&`, so every hook exited 1 on Windows; `--check` and doctor count that
form as drift, and an install replaces it (Codex then asks for the changed handlers to be trusted
again in `/hooks`). An install whose host-choice file was lost still recognizes that form as
Exoma's registration and repairs it instead of withdrawing the Codex host. Uninstall keeps
`codex-host.py` while any surviving handler names it, under either escaping. Its `AGENTS.md` block coexists with the always
installed Codex second-agent block. The latter applies only to runtime prompts beginning
`[WORKFLOW_AGENT]`; the hooks distinguish the delegated process using
`WORKFLOW_AGENT_ROLE=second_agent`, set by `CodexAdapter` on fresh and resumed calls. Thus
Codex host + Codex provider is two separate CLI processes and two separate thread IDs.

The Codex host shares `workflow_policy.py` with Claude/Agy. Its adapter reads
`exec_command`, file-read tools, and `apply_patch` calls and delegates the same pre-flight,
write-scope and commit decisions. A single-file `cat` or `Get-Content` call can use a precision
read when the file has at most 200 lines; `sed -n '<start>,<end>p'` and
`Get-Content -TotalCount <n>` can read a bounded slice. Two precision reads pass per marker.
The `UserPromptSubmit` event does not expose a prompt-source field; Exoma accepts an
execute or commit approval only when the explicit command begins the submitted prompt.
This is weaker provenance than Claude's source field. Codex hooks also require trust,
may be disabled by the user, and do not cover every specialized tool path; the gate is a
workflow guardrail, not an OS security boundary. Codex's write sandbox for the second
agent remains independent of the host hooks, and its read boundary remains unenforceable.
See [OpenAI's hook documentation](https://learn.chatgpt.com/docs/hooks) for current event
and tool coverage.

**Codex host: runner permissions follow the active session.** Use normal execution when
the runner is allowed. With `approval_policy=never`, or when escalation is forbidden,
omit `sandbox_permissions`, `justification`, and `prefix_rule`. A restricted network may
still prevent delegation; `never` alone does not establish whether networking is allowed.
Only when broader permissions are needed and the host permits requesting them, request
the delegated runner with `sandbox_permissions: "require_escalated"`, a justification,
and a narrow prefix for `<bash> <root>/.workflow/run.sh`. Use available metadata to name
the configured provider and the kind of repository context sent to its service, reusing
existing authorization for this provider, project and context, and asking only for missing
authorization. Do not guess an endpoint.

Distinguish host approval denial **before** process creation (no runner JSON), shell
failure without JSON, `network_denied` after launch, and a job error returned as JSON.
Report an auto-review denial and its reason; do not retry through another shell, prefix,
or weaker policy. Continue only through a materially safer authorized path or explicit new
authorization, still subject to host policy. Exoma cannot override review, and a command
prefix does not guarantee approval. See [OpenAI's Auto-review documentation](https://learn.chatgpt.com/docs/sandboxing/auto-review).

Keep waiting on the same running session. When the runner itself is escalated, its children
inherit that environment; provider-specific restrictions remain separate. The Codex second
agent still uses `--sandbox read-only` and `approval_policy="never"`. Exoma does not change
the host's approval policy or write a network grant into its configuration.

**Codex host on a `workspace-write` sandbox.** Every delegated call writes the global job
store (`<data dir>/storage/jobs`, see [the workspace layout](#workspace-layout)) and the
main-session cache beside it from the host's own process. `<data dir>/storage` sits outside the
project, so a host sandbox that lets only the workspace be written refuses it: each call ends in
`job_submit_error` before a worker starts (CASE-034, a record in agent-workflow). The
provider-version and model-catalog caches live in the per-user cache directory
(`%LOCALAPPDATA%\exoma` on Windows, `$XDG_CACHE_HOME/exoma` or `~/.cache/exoma` elsewhere),
not under `<data dir>`, and are skipped when their write fails, so they need no grant; the
data directory's `.env` stays out of the sandbox's reach. An install whose hosts include `codex`
writes the grant into `~/.codex/config.toml` as a block between `# EXOMA-CODEX-GRANT:START` and
`# EXOMA-CODEX-GRANT:END` comments, appended to the file or replaced in place:

```toml
# EXOMA-CODEX-GRANT:START — managed by Exoma: lets Codex's workspace-write sandbox write the job store
[sandbox_workspace_write]
writable_roots = ['C:\Users\<you>\AppData\Local\exoma\storage']
# EXOMA-CODEX-GRANT:END
```

The file is never parsed and rewritten (the runtime is stdlib-only and the stdlib has no TOML
writer; `tomllib` only reads). When the user already keeps a `[sandbox_workspace_write]` table (or a dotted or inline
key for it), a second table of that name would make the file invalid TOML, so the install leaves
the file alone and prints the snippet: add the storage directory to that table's `writable_roots`
yourself. A table of the user's added later, beside Exoma's block, is the same conflict: the next
install takes the block out (the user's table governs) and prints the snippet. Grant markers that
do not pair up (a hand edit) leave the file untouched and the install refused for it. The
result is parsed before it is written, and an install that would leave invalid TOML is
refused. `--check` reports the block missing or stale, and reports a conflict or unpaired markers
as drift (the user's table alone is not drift); `--uninstall` and dropping the `codex` host take the block out and keep the rest of the
file. Keys added below the block by hand belong to that table in TOML: add them above it.
Use the storage directory this machine resolves to, not the example path. A TOML literal
string (single quotes) keeps Windows backslashes as written. The other way out is to start
the host with `EXOMA_HOME` set to a directory its sandbox can write; it is read once at
import, so it must already be in the environment of the process that runs `.workflow/run.sh`,
and it moves only the per-user runtime data (jobs, main-session cache, seeds), not the
provider caches above or the project's `.workflow/`. A store moved this way is a separate
store: the machine-wide worker cap counts each store on its own.

When the store is refused, the `job_submit_error` envelope carries
`meta.reason: job_store_not_writable`, `meta.job_store`, `meta.writable_root` and
`meta.fix_snippet` (the snippet above with this machine's path), and `next_action` names
both fixes for any host, with Codex's setting as the example. Only a permission refusal reads
this way: a `PermissionError`, or `EACCES`, `EPERM` or `EROFS`, raised on a path inside the
store or with no path at all. Any other error in the store (a full disk, an I/O error)
carries `meta.reason: job_store_write_failed`, `meta.job_store` and `meta.errno`, with no
grant advice, since a grant would not fix it. Doctor probes the same store
(`checks.job_store_writable`); run inside the host session, it fails exactly when submit
would: the store is listed in `issues`, so the status is `NOT_READY` and doctor exits 1, and
`recommended_fixes` carries the snippet. Doctor does not read the grant itself: whether the
block Exoma wrote is missing or drifted is reported by `exoma install --check`, which the
fix names.

**Agy's hook contract, as measured** (CASE-029, a draft in agent-workflow). Hooks load from
`~/.gemini/config/hooks.json` and the workspace's `.agents/hooks.json`. Every payload carries
`conversationId`, `workspacePaths`, `transcriptPath`, `modelName` and `lastUserInput` (the
user's prompt, undocumented). A resume with `--conversation <id>` keeps the id and does not fire
`SessionStart`. A hook that crashes or exits non-zero fails the tool call; exit 2 means
nothing. On Windows the command runs through `cmd.exe`, which receives quotes literally, so a
command cannot quote a path.

**The wrapper**, `~/.gemini/antigravity-cli/exoma/hooks/agy-host.py <Event>` (with
`workflow_policy.py` and `intent-map.json` beside it), registered under the hook name `exoma`
for `PreInvocation`, `PreToolUse` (matcher `*`) and `Stop`:

- `PreInvocation`, turn start: binds the conversation to a `MAIN_SESSION_ID` (the first
  workspace path holding `.workflow/` is the project root), writes `execute.token` and
  `commit.marker` as `intent-gate-set` does, arms or clears `delegated.marker`, and injects
  `[SESSION BINDING]` plus any `[INTENT HINT]`.
- `PreToolUse`: while `delegated.marker` exists, the pre-flight decision is the Claude gate's
  (`preflight_refusal`, "Pre-flight gate" above): `run_command` is `shell`, the write tools are
  `write`, `read_file`/`view_file`/`view_file_outline`/`view_code_item` are `read` (a precision
  read passes; Agy gives no `limit`, so the file itself must be ≤ 200 lines), and the listing,
  search and `mcp_*` tools are `other`. What Agy may always read is an existing `.md` under
  `~/.gemini/antigravity-cli/{skills,exoma}`. `& ` in front of the command (PowerShell's call
  operator) is dropped before the words are read. `<bash>` is the one the installer names in
  the instructions: `& '<Git Bash>'` on Windows, where Agy runs commands through PowerShell and a
  bare `bash` can be WSL's (it cannot read `E:/` paths, and is refused), the quoted `bash` path
  or a bare `bash` elsewhere. A write of the runner scripts is refused marker or not. The
  execution gate applies to the write tools (`write_to_file`, `replace_file_content`,
  `multi_replace_file_content`, `sed_file`, `notebook_edit`, and any tool named with
  `write`/`replace`/`edit`/`create`) and `git commit` through `run_command`, with
  `~/.gemini` and `~/.claude` writable from any project. `policies.enforcement` applies as it
  does for Claude Code; `warn` answers `allow` with the message as the reason.
- `Stop`: an `[EXECUTION RESULT]` in the last model response (`source: MODEL`,
  `PLANNER_RESPONSE` in `transcriptPath`) that claims more than `verification: not_run` gets
  `decision: continue` once per turn.
- Fail-open, against Agy's default: any error is appended to `~/.gemini/exoma-hook-errors.log`
  and answered with `allow` (`{}` for the other events).

**Role.** Agy loads `~/.gemini` on every run, so a second-agent call sees the host block and
the host hooks too. The hooks decide the role from the environment alone: a process with
`WORKFLOW_AGENT_ROLE=second_agent` (set by the agy adapter, inherited by Agy's hooks) is the
runtime's call, and the wrapper allows it untouched; `intent-gate-set` skips it the same way.
The prompt's `[WORKFLOW_AGENT]` header (the runtime's, every provider) is not trusted by any
hook, because a user can type or paste it: a user prompt that opens with it is gated like any
other. The header stays a prompt-level cue: the `GEMINI.md` block tells the model to ignore
itself on such a call. None of this bounds the second agent: agy's boundary stays `unbounded`
("Choosing a provider").

**Install** (`exoma install agy` or `all`). The installer splices the `EXOMA-HOST` block into
`~/.gemini/GEMINI.md` (another workflow's block is left beside it) and merges the `exoma` name
into `~/.gemini/config/hooks.json` (other names are the user's). Under `~/.gemini/antigravity-cli/`
it writes `exoma/CONTRACT.md`, `exoma/hooks/{agy-host.py,workflow_policy.py,intent-map.json}`,
and one `skills/.<name>/SKILL.md` per Claude skill (Agy frontmatter `name`/`description` on top,
the runner call rewritten to the host's bash, then the skill unchanged; `.doctor` also gets
`hosts/agy/doctor.agy.md`, see "Probe"). The
contract, hook scripts and skills are rendered from the `hosts/claude/` sources at install time, so
the bundle ships them once. The hook command is
`<interpreter> <home>/.gemini/antigravity-cli/exoma/hooks/agy-host.py <Event>`, unquoted. The
interpreter is the first of these whose path holds no space and no `cmd.exe` metacharacter
(`& % ^ | < > ( ) "`): the install interpreter, its resolved link target, the base interpreter
of its environment, then `python3`/`python`/`py -3` from PATH (run and at least 3.11). The
wrapper is stdlib-only, so a base interpreter runs it as well as the environment's own; this is
what lets pipx on macOS, under `~/Library/Application Support/pipx`, install the hook. When
none qualifies, or the home itself holds such a character, the hooks are refused with a warning
(the install report then adds `to drop the Agy host from this choice: <command>` for the hosts
chosen, see the doctor fix below), as is a
`hooks.json` that cannot be read, is not valid JSON, or whose root is not an object
(the file is left as it is). The two files install together or not at all: the refusal is
decided before `GEMINI.md` is written, and a refused install writes neither — it also cuts an
`EXOMA-HOST` block an earlier install left, backed up and receipted, since instructions with no
hook enforcing them are a half install (unpaired markers are left alone and reported). `--check`
stamps every Agy source in the bundle on every machine, and for an install that chose `agy`
compares the installed block and files, reports a missing or drifted `exoma` entry in
`hooks.json`, and prints `agy host: NOT INSTALLABLE` with the reason when the hook would be refused,
followed by `agy host: to drop it from this choice — <command>`.
`/.doctor` gives the same answer: a chosen `agy` host whose hook is refused is one issue,
`host agy: not installable on this machine — <reason>`, with the fix "install from an
interpreter on a plain path, or drop the host". The command, the same in all three places (one
helper, from the stored choice): `exoma install <host>` when one other host is
kept; the interactive host prompt naming the hosts to choose when several are (a
non-interactive run keeps the stored choice, agy included); and, when agy
was the only host, switching host (`exoma install <claude|codex|opencode>`) or
`exoma install --uninstall --apply` — never a host added on the user's behalf. The `GEMINI.md`
and `hooks.json` the refusal leaves out are not also reported missing or as bundle drift; an
`EXOMA-HOST` block an earlier install left in `GEMINI.md`, or markers there that do not pair up,
is, as `host agy: drift — leftover …` (one test shared with `--check`), with the fix that the
next `exoma install` takes it out; a `GEMINI.md` that cannot be read counts as no leftover in
both, never a traceback. The refusal is judged for the home doctor reports on.

**Permission rules.** Agy asks before every command unless a rule allows it, and its sandbox
refuses writes outside the workspace. Whenever the `agy` host is chosen (`agy` or `all`), the
installer adds two rules to `permissions.allow` in `~/.gemini/antigravity-cli/settings.json`, from
`hosts/agy/permissions.template.json`: `command(regex:...)` for one runner call through this
machine's bash, script inside a `.workflow/` directory, every argument a single-quoted string or
a plain word; and `write_file(<data dir>)` for the runtime's data directory (`EXOMA_HOME`, by
default `%LOCALAPPDATA%\exoma`). They apply to every Agy session. `--no-agy-permissions` removes
them and is stored (`agy_permissions_opt_out`), so later upgrades keep them off;
`--agy-permissions` clears it. A stored `agy_permissions: false` without that opt-out is read as
no choice, and the rules go in. The rules approve the runner call; they do **not** lift the
sandbox. Agy on Windows (CASE-033) starts Git Bash inside the sandbox and it dies before the script
runs (exit 1, no output), and an `unsandboxed(...)` rule is rejected as invalid and
grants nothing. The runner therefore runs with the sandbox bypassed per call (`BypassSandbox`),
which the `GEMINI.md` block tells the model to set on runner calls only. Python started inside
the sandbox does not help: the runtime then cannot read the provider's config in the home
directory nor write `.workflow/data`. Only rules the installer added are recorded
(`agy_permission_rules`) and only those are ever removed — by the opt-out, by dropping the `agy`
host, or by `--uninstall`; a rule the user already had is never claimed. `--check` reports a
missing rule while they are on.

**Probe.** `--check` and doctor's `checks.hosts` compare files; they cannot tell whether Agy loads
them. The Agy `/.doctor` skill (the Claude skill plus `hosts/agy/doctor.agy.md`) adds a STEP 3
only an Agy session can perform, reported as `agy_session`: whether skills are registered, whether
the hooks inject `[SESSION BINDING]`, whether the `run.sh doctor` call ran without a prompt, and
the runner's failure (WSL bash, the sandbox refusing Git Bash — `NtCreateDirectoryObject`, or
exit 1 with no output — or the data directory) when it did not. The Claude `/.doctor` never carries it. After an install that chose `agy`, the installer says
to run `/.doctor` in Agy.

**Limits.** Calibrated on one release, on Windows, in print mode. `lastUserInput` is
undocumented: without it routing and the role header are lost, and the role then rests on
`WORKFLOW_AGENT_ROLE`. `ask_question` and background tasks were not exercised.

---

## Bootstrap

The runtime is the installed `exoma` package, **not** something in the target project. Any
shell that can run `exoma --version` (or `python -m exoma --version`) can bootstrap a project;
no environment variable is needed. `init` records the interpreter and the directory holding the
package in `.workflow/config.json`, so the runner scripts reach the same build afterwards.

`AGENT_PATH`, which agent-workflow used to locate a checkout's `main.py`, is not read. A source
checkout of Exoma still runs as `exoma ...` from its root (a shim over `exoma.cli`).

Verification:

```bash
exoma --version
python -m exoma --help
```

---

## Init in the target project

```bash
exoma init --work-dir /path/to/target-app --pretty
```

`init` is idempotent: it creates the scaffolding that does not exist yet and regenerates the three runner scripts. A workspace created by an old build (different version or old layout) is upgraded directly by init; if a live job exists, the upgrade is skipped and the command to run is named.

- `.workflow/config.json` — `runtime.python` (the interpreter) and `runtime.package_root` (the directory holding the `exoma` package), so the runner reaches this build without any environment variable
- `.workflow/second_agent.json` — a project-local copy, which you may edit
- runner scripts `run.sh` `inspect.sh` `check.sh` — bash on all OSes (Git Bash on Windows) that put `package_root` first on `PYTHONPATH` and exec `<python> -m exoma`; there is no `.ps1` any more
- an empty `.workflow/data/sessions/` — per-session state is created lazily on the first delegated call. All internal files (streams, store, cache, sessions, reports) are in `.workflow/data/`; the `.workflow/` root holds only the files you edit, the scripts, and `current/`
- a `.workflow/` entry is added to the project root `.gitignore`

The `.sh` scripts are given the executable bit when created. If the repo is moved via media that drops mode bits:

```bash
chmod +x .workflow/*.sh
```

**Portability limitation.** The scripts bake in **absolute paths from the machine where `init` was run** — the package root, `--work-dir`, and the detected Python interpreter. None of them is portable across machines.

Paths are written with forward slashes (`C:/...` on Windows), which both Git Bash and Windows Python read. `init`/`upgrade` **delete** `run.ps1`/`inspect.ps1`/`check.ps1` left over from old builds: no generator maintains them any more and the gate does not let calls to them through, so keeping them only makes them look usable. A `run.sh` from another machine still contains that machine's absolute paths. Every team member runs `init` (or `upgrade`) themselves in their own environment; that also applies when moving between WSL and native Windows.

The project root written into `run.sh` and `inspect.sh` is resolved first (symlinks, `..`), so one project reached through two spellings — macOS `/var` and `/private/var` — generates one text and an upgrade sees no drift.

`doctor` compares the script contents on disk with the generator output and reports it in `run_script_drift` — `missing`, `content_differs`, or `retired_leftover` (`.ps1` from an old build). All of them count as issues: these scripts are the only entry point, and one that has drifted will route commands the CLI no longer accepts.

## Workspace upgrade

The recommended path after updating Exoma (`uv tool upgrade exoma` or `pip install -U exoma`):

```bash
cd /path/to/target-app && exoma install
```

That command updates the global config and upgrades the workspace in the cwd. A project that does not
have `.workflow/` yet is scaffolded via `init`, not the installer. For the workspace only:

```bash
exoma upgrade --work-dir /path/to/target-app --pretty
```

`upgrade`:

- refuses to run while a delegated job is active;
- regenerates the runner scripts and repoints `runtime.python`/`runtime.package_root` at the running build;
- takes over a workspace agent-workflow wrote: `runtime.agent_workflow_path`, `main_py_path`, `check_py_path` and `tool_dir` are removed, and the scripts call `-m exoma` instead of a checkout's `main.py`;
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
bash '/path/to/target-app/.workflow/run.sh' explore --session '<MAIN_SESSION_ID>' --task 'find the auth entry point'
```

That is Claude Code's spelling. Agy, Codex and OpenCode run commands through PowerShell on
Windows, so there the call starts with this machine's Git Bash by full path behind the call
operator (`& 'C:/Program Files/Git/bin/bash.exe' '/path/to/target-app/.workflow/run.sh' …`);
each host's rendered contract and `host-profile.json` carry the exact prefix
([Host profiles and rule labels](#host-profiles-and-rule-labels)). `& '<…>/run.sh'` without the
bash hands the script to Windows, which opens it in a new window and returns nothing.

`run.sh <command> [--session <id>] [--task <text> | --task-file <path>]`. Each value is wrapped
in single quotes: that is the form the pre-flight gate lets through, and its content is literal
(`$`, `;`, `|`, parentheses are allowed; apostrophes are not). On Windows the path is `C:/...`.

- **Named, not positional.** Windows PowerShell 5.1 passes an empty `''` argument to a native
  program as nothing at all, so in the positional form `run.sh execute-begin '' '<id>'` the
  session id arrived as the task and the call ran on `default` (CASE-033). With options a
  missing value cannot move another one. No task: leave `--task` out (never `--task ''`).
- **`--task-file <path>`** is passed to the CLI as `--prompt-file`: the task is read from the
  file as UTF-8 and never goes through a shell's quoting, so apostrophes and double quotes
  survive (PowerShell 5.1 mangles quotes it passes to a program). `--task` and `--task-file`
  together are refused. A conventional place is
  `.workflow/data/sessions/<id>/runtime/task.txt`, overwritten by the next call.
- **Refused shapes** (exit 2, with the usage line): an unknown option, an option without its
  value, and an option whose value is another option name — what `--task '' --session x`
  leaves once the empty value is dropped.
- **Positional form.** `run.sh <command> [<task>] [<session>]`, the form up to 0.3.x, still
  works when the second word does not start with `--`. A task that itself opens with `--`
  needs the named form.

**The session.** `--session` falls back to `$MAIN_SESSION_ID`, then to `"default"`, which the
CLI resolves through the tool's global cache — so several main_agents can share an effective
ID, overwrite each other's state, and block each other's jobs. The runner therefore **refuses**
`default` (exit 2, naming `--session`) for every command that reads or writes one session's state:
`explore`, `plan`, `analyze`, `verify`, `verify-browser`, `sweep`, `plan-record`,
`execute-begin`, `execute-end`, `test`, `verify-prepare` (`core/runtime/scripts.SESSION_REQUIRED`). The CLI refuses it
too for the local ones of that list (`exoma sweep` with no `--session` exits with the same
advice); a delegated command typed by hand at a terminal keeps the cache. `doctor`, `init`,
`upgrade`, `clean`, `inspect` and `provider` work on the project and need no session.
`EXOMA_ALLOW_DEFAULT_SESSION=1` lifts the refusal deliberately.

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

The contracts below live in `src/exoma/_bundle/config/hosts/claude/CLAUDE.md` and its skills
(`src/exoma/_bundle/config/hosts/claude/skills/*.md`), not in Python. The runtime never sees the raw user message
or main_agent's final answer, so the one enforcing these contracts is main_agent
itself. The partial exceptions are the pre-flight gate and, since agent-workflow 3.9.0, the execute chain
([Plan and execution state](#plan-and-execution-state)), both also enforced by the host agent's hooks. The per-contract reason why Python cannot enforce them: `docs/runtime-contracts.md`,
section "Contracts the runtime cannot enforce".

| Contract | Content | Enforcer | Source |
|---|---|---|---|
| Intent mode | One of two, chosen by the installer. `AUTO-INTENT`: natural language is mapped to a command and announced with one `[INTENT]` line without waiting for an answer. `COMMAND-ONLY`: commands run only via the `/.` prefix; no prefix = ordinary conversation, may suggest one line and then stop | main_agent | `CLAUDE.md`, stanza `AUTO-INTENT` / `COMMAND-ONLY`; see [Intent mode](#intent-mode) |
| Pre-flight gate | Once intent resolves to a delegated command (`explore`, `plan`, `analyze`, `verify`, `verify-browser`), the next step must be `.workflow/run`. Gather tools (Read/Grep/Glob/Bash/MCP) are forbidden before that, except under `[LOCAL_MODE]`, proxy failure, or a small precision slice | main_agent + hooks `intent-gate-set` / `intent-gate-check` (auto-intent mode only; fail-open; writes of the runner scripts and of the escape files refused always; an escape in use while armed, or a delegated command the user named that could not arm, logged to `gate-skips.jsonl` and reported by `doctor`) | `CLAUDE.md`, "Pre-flight gate"; `src/exoma/_bundle/config/hosts/claude/hooks/intent-map.json` |
| Output contract | Explore and `sweep`/`doctor` = RELAY the runtime result as is. Plan/analyze = SYNTHESIS by main_agent: three-part confidence (`problem_understanding`, `root_cause`, `solution_path`), per-claim attribution, `open_questions` kept separate from `uncertainty`. Required fields are still shown even when empty, with the reason | main_agent | `CLAUDE.md`, "Output Contract" and "Plan/analysis output" |
| `[OPTIONS]` | See [Implementation options in `/.plan`](#implementation-options-in-plan) | main_agent | `skills/plan.md` |
| New feature via plan | A command that describes new behavior ("buat agar …", "tambahkan …", a list of fixes) without an active plan is mapped to `plan`, not `execute`. Execute without an active plan stops and suggests `/.plan`. Requests for a spec/explanation are answered directly, without an unrequested `/.verify` (auto-verify after `/.execute` when `auto_verify_after_execute=true` still applies) | main_agent | `CLAUDE.md`, NL map and tie-break; `skills/execute.md` |
| `/.execute -y` | Without `-y` it only shows `[EXECUTION SCOPE]` and then stops. With `-y` it edits only files in scope; a need outside scope = stop and ask for instructions. Approval is only `/.execute -y` from the user after a `proceed` plan; an execute sentence without `-y` shows the scope and asks for `-y`; an AskUserQuestion answer to `open_questions` is not approval | main_agent; since agent-workflow 3.9.0 also runtime + hooks: `execute-begin` needs the recorded plan and the `-y` token, `execution-gate` blocks Edit/Write outside the scope | `skills/execute.md`, `skills/plan.md` STEP 3; [Plan and execution state](#plan-and-execution-state) |
| Verification after execute | `commands.auto_verify_after_execute` is reread on every `/.execute`. `false` (default): status `implemented`, `verification: not_run`, the word "done" and similar are forbidden. `true`: `/.verify` is part of `/.execute` | main_agent; the `status-guard` Stop hook warns when the `[EXECUTION RESULT]` fields claim more than `verification: not_run` allows | `skills/execute.md`; `CLAUDE.md`, "Execution rules" |
| Proxy failure | `ok:false`, an invalid-evidence `error_type`, or content that is not evidence → stop, print `[PROXY FAILED] …`, ask whether to continue with `/.local` (yes/no), then wait. Automatic local fallback is forbidden | main_agent | `CLAUDE.md`, "Proxy failure" |
| Destructive actions | Commit, delete, or writing outside the project must not run from a guessed intent; confirmation is required. Writing to another project (Edit/Write or Bash/script) needs explicit approval for that path. Since agent-workflow 3.9.0 the `execution-gate` hook blocks Edit/Write outside the root unless the path is in `policies.write_allowed_outside_root`; Bash/script writes are not checked (see [limitations](limitations.md)) | main_agent | `CLAUDE.md`, "Intent detection" and "Global Forbidden" |
| `/.commit` staging | An existing staged set is used as is. With nothing staged, main_agent stages changed tracked files and untracked files Git does not ignore, one quoted path per argument (never `-A` or `.`), and skips a path whose lowercased, `/`-separated form matches the canonical secret-file list of `core/policy/secret_patterns.py` (`SECRET_READ_PATTERNS`, minus `SECRET_READ_ALLOWLIST`), the same list the providers' read boundary uses; a test keeps the skill's copy identical. It shows staged, newly tracked, and skipped paths with the message, and commits only on `yes`; `no` unstages only what it staged (`git reset -q --`, which also works before the first commit). The skip list is a prompt rule over file names, not a content scanner | main_agent | `skills/commit.md` |

The exact content stays in the source files; this table is a map, not a copy. When a source file and this
table differ, fix one of them in the same change.

---

## Command

| Command | Kind | Needs prompt | Notes |
|---|---|---|---|
| `init` | local | — | scaffold `.workflow/`; regenerate runner scripts |
| `upgrade` | local | — | migrate the `.workflow/data/` layout, refresh the workspace, override-only config, preserve sessions |
| `doctor` | local | — | readiness check, writes `reports/doctor.json`; recent pre-flight gate skips (the last 7 days of every session's `runtime/gate-skips.jsonl`, plus this project's rows in `~/.claude/gate-skips.jsonl`) are listed in `checks.gate_skips` with a recommended fix, never as an issue; a provider config it may not read (a sandboxed host denies the home directory) is an issue, `provider config unreadable: <path>`, listed in `checks.mcp_second_agent.unreadable`, never a crash |
| `clean` | local | — | prune jobs, stale/duplicate facts, runtime-lock guards of sessions with no live lock (`sessions/<id>/runtime/lock.guard`; a guard currently held by another process stays), old sessions |
| `inspect` | local | — | list jobs for the current session |
| `provider` | local | — | read/change the second_agent provider and reasoning effort; picker with no arguments, direct with `<provider> <model> [effort] [routes]` |
| `report` | local | — | summary of the workspace quality stream (`quality.jsonl`), including `e2e` (runs), `e2e.drafts` (drafts), and `graph_refresh` (Stop hook graph cost per outcome), and from `usage.jsonl`: `evidence_reuse` (explore/analyze/plan commands served by a stored artifact divided by commands eligible for reuse, continuations counted once; `by_outcome` breaks it down per `reuse_outcome`: `hit`, `no_prior`, `stale`, `unreadable`, `error`, `not_offered`, `disabled`, and `unrecorded` for rows before contract v3), `facts` (fact lifecycle summed per exploration/reasoning command: `candidates`, `injected`, `rejected_stale`, `rejected_irrelevant`, `ingested`, `ingest_rejected`, `injection_rate`, `stale_rate`; commands recorded before agent-workflow 3.9.0 count as `unrecorded_commands`), `provider_cache` (cached-input share of the provider's measured input: `all`, `first_call`, `continuation`, `fresh_thread`, `resumed_thread`; rows whose adapter does not report `resumed` are not included in the two thread buckets), `provider_threads` (per provider: `resumed`, `fresh`, `unknown`, and `thread_changed` = a resume call answered in a different thread, a sign the session lost state; rows before contract v3 are not counted), `by_effort` (per `<command>/<effort>`: command count, median duration, median input/output tokens — each median only from commands whose value was measured, unmeasured output is not counted as zero; `default` = no effort flag; rows before contract v3 are not counted), and `graph_leads` (`graph_status` per command, `refreshing_by_command`, median/max `graph_ms`); and from `tasks.jsonl`: `tasks` (see [Task telemetry](#task-telemetry)); and `provenance` (metric registry version, version of each `runtime.*` metric, tool commit, stream content hash — definitions in [`docs/evaluation/metrics.md`](evaluation/metrics.md)) |
| `audit` | local | — | read the session's `audit.jsonl` |
| `graph-meta` | local | — | graphify snapshot status: fresh, stale, or absent |
| `explore` | delegated | yes | codebase map, entry points, owners |
| `plan` | delegated | yes | evidence + reverse dependency trace |
| `analyze` | delegated | yes | in-depth analysis, zero code changes |
| `verify` | delegated¹ | yes | verification; depth set by `verify_mode` |
| `verify-prepare` | local | no | effective verification policy and pending request diagnostics for an explicit session; no provider, tests, verdict, or marker removal |
| `verify-browser` | delegated³ | yes | verification through a Playwright browser from a per-session request; `auto` prepares, runs and reviews in one invocation |
| `sweep` | local | — | scan staged, unstaged, and untracked diff without OpenCode |
| `promote-validate` | local | yes² | validate a knowledge document; zero writes |
| `promote-verify` | local | yes² | freshness of each claim + reconciliation against existing documents |
| `promote-write` | local | yes² | the only write path into the knowledge directory |
| `plan-record` | local | yes (path to the plan JSON) | records the session's plan; refuses an unsound `proceed` — see [Plan and execution state](#plan-and-execution-state) |
| `execute-begin` | local | no | opens an execution: needs a recorded `proceed` plan and the user's `/.execute -y` token |
| `execute-end` | local | no | closes the execution as `implemented`; verification stays `not_run` until a verify runs |
| `test` | local | yes (one command line, or a JSON list of up to 5) | runs project tests under the verify allowlist and records each outcome with the tree's content fingerprint, so `/.verify` can reuse a pass — see "Tests already run" under `verify_mode` |
| `submit` | job | yes | run asynchronously, return `job_id` |
| `await` | job | yes | submit and then wait for completion |
| `status` | job | — | requires `--job-id` |
| `result` | job | — | requires `--job-id` |
| `worker` | internal | — | used by the worker process, do not call manually |

¹ `verify` bypasses OpenCode entirely when `verify_mode` is `syntax`.

² All three `promote-*` stages take a **path to a JSON file** via `--prompt`, not the document itself: a single knowledge document easily exceeds Windows' 8191-character argv limit. The stages are split because user approval happens between them — the CLI cannot ask anything, so verification stops at a verdict, main_agent runs the review, and writing is a separate call that can only happen afterwards. `promote-write` refuses outside `policies.production_branch`.

³ `verify-browser` is a **delegated** command: it is registered in the DELEGATED registry and goes through the pre-flight gate like `explore`/`plan`/`analyze`/`verify`. It reads the **per-session request** (`.workflow/data/sessions/<session>/e2e/request.json`) on top of the project defaults from the `e2e` section in `config.json`. The work is split into three stages: second_agent drafts the spec (stage 1) and reviews the browser evidence (stage 3); in between, the **runtime player** — a child process run by the runtime (`python -m core.evidence.e2e.player`) — drives Playwright (stage 2). Neither main_agent nor second_agent runs the browser (read-only sandbox). The skill writes `phase: auto`: the runtime prepares and validates a fresh spec, then runs the player and review under the same lock without an agent copying draft.json. Auto refuses supplied scenario/existing_tests/spec_notes fields. Invalid/blocked preparation, missing credentials, or read-only proposals not covered by existing approvals return a draft before starting the player; after missing input or approval is supplied, a new auto invocation prepares afresh. Explicit `draft` and `run` remain available. The interview happens in main_agent only after preparation reports that the target is not configured yet. The `draft` stage carries no verdict; the `run` stage is finalized as `verify`, so its verdict and exit code match other verifications. An `[E2E SPEC]` section that fails validation gets one repair in the same provider thread (`meta.e2e.repair`); the only test attribute is `data-e2e`, via the `e2e` selector key — `testid` is rejected (**breaking**); the `repeat_failure` brake releases by itself when the project (outside `.workflow/`, via Git) or the runtime (`TOOL_VERSION` + the code digest of `core/evidence/e2e/`) has changed since the last failure, the `app` bucket streak only grows for failures with the same signature (page, field, condition), and it is recorded as `repeat_failure` knowledge for the next draft. Credential values typed literally in the task, draft, or `request.json` are found via a `secrets.json` lookup and turned back into `${NAME}` (`request.json` is rewritten; key names are reported in `meta.e2e.secret_literals`, values never are), and a `fill` into a password field without a placeholder → `spec_invalid`; failures caused by the scenario itself — a guessed selector that misses, any selector that matches >1 element, a write/navigation rejected by the run policy — have origin `scenario` and end as `incomplete: scenario_error`, are not retried, and do not count toward the brake; every non-pass run and every brake refusal carries `meta.e2e.diagnosis` (`cause`, `next_step`, `failed`, `fix_hint`, `reusable`), and reusable causes are stored as `failure_hint` knowledge; every draft writes a `kind: e2e_draft` row (validator error category, repair result) that `--command report` summarizes in `e2e.drafts`. Network: every run always records metadata of all page requests (order, URL without query, type, status, timing, size; no body/headers/cookies), the latest 500 rows plus a summary over all requests, in `report.json` → `network` and `meta.e2e.page_requests`, including `since_last_run` (changes compared with the previous run on the same origin). Selector order: candidates follow role+name > label > e2e > text > css, except two kinds that may lead — an `e2e` with `source` provenance and a `path:line` ref, and a `proven` selector recorded in browser knowledge for the origin, the route (the last `goto` path, identifier segments become `:id`: `/users/123` and `/users/456` are one route), and that step's `within` (checked against the store; a `proven` claim without basis or on another route/scope → `spec_invalid`); a `proven` selector that misses drops to the normal order, two consecutive runs that miss retire it (one run weakens an entry at most once); a step that fails after its selector matched is not a miss; a pass is recorded only from a run that passed on the first attempt. Network URLs are cleaned of `;…` path parameters and token-shaped segments (16+ alphanumerics without separators, 16+ mixed upper/lower case, 24+ with digits) to `:id`. Full contract: `docs/runtime-contracts.md`, section `/.verify-browser`.

There is no Python `execute` command that writes code: `/.execute -y` remains a main_agent command, and writing code is deliberately not delegated to the runtime or second_agent. What the runtime holds since agent-workflow 3.9.0 is the execution's **state** — `execute-begin` / `execute-end` and the hooks that read it.

---

<a id="cli-langsung"></a>
## Direct CLI

Useful for debugging; the normal flow only needs the runner script. For the same blocking behavior as the runner, use `await`:

```bash
exoma -c await --job-command explore -p "find the auth entry point" -s "main_app_20260723_090000" -w /path/to/target-app --pretty
```

```powershell
exoma -c await --job-command explore -p "find the auth entry point" -s "main_app_20260723_090000" -w "C:/path/to/target-app" --pretty
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

### Environment variables

Runtime knobs are read from `EXOMA_<NAME>` (`EXOMA_TIMEOUT_SECONDS`, `EXOMA_PROVIDER`,
`EXOMA_SLIM_CONTENT`, …; the full list is `LEGACY_ENV_NAMES` in
`src/exoma/config/settings.py`). Each one is still read under its pre-rename spelling
`AI_PROXY_<NAME>` as a fallback; when both are set, `EXOMA_<NAME>` wins. `doctor` lists every
such `AI_PROXY_*` variable it finds set (`checks.legacy_env`) with a recommended fix to rename
it; an `AI_PROXY_*` name outside that list was never read and is not reported.
Variables named after a provider CLI (`OPENCODE_COMMAND`, `AGY_COMMAND`) and `EXOMA_HOME` have
no legacy spelling.

### `.workflow/config.json`

Created at `init`, and contains **overrides only**: an empty/absent `commands`, `policies`, `e2e` section means every key uses the default built into the running build. Write only the keys you want to change; absent keys are never filled in automatically, so defaults that change in a later release reach this project too.

The following keys actually change the Python runtime's (or a shipped hook's) behavior:

| Key | Default | Meaning |
|---|---|---|
| `commands.verify_mode` | `"delegated"` | `delegated` = full second_agent verification. `syntax` = local parse check only. Unknown values fall back to `delegated` |
| `commands.verify_test_commands` | `[]` | test command prefixes the runtime may run for `/.verify` (e.g. `["python tests/run.py", "php artisan test"]`). Empty = no tests run; requested tests are reported as not run |
| `commands.verify_test_timeout_seconds` | `900` | time limit per test command |
| `commands.verify_reuse_env` | `["WORKFLOW_*", "EXOMA_*", "PYTEST_*", "CI", "NODE_ENV", "APP_ENV"]` | environment variable name patterns hashed into the identity a `run.sh test` pass is reused under |
| `commands.verify_reuse_files` | `[".env", ".env.*"]` | globs under the project root (inputs Git ignores) hashed into that identity |
| `policies.fact_relevant_limit` | `3` | maximum facts injected into each prompt, for a role `fact_relevant_limits` does not name. Set explicitly, it also overrides the per-role defaults |
| `policies.fact_relevant_limits` | `{"exploration": 3, "reasoning": 4}` | per-role fact limit |
| `policies.fact_min_relevance` | `2.0` | weighted overlap a fact needs to be served: shared identifier 2, shared word 1, path-only token 0.5; EN/ID function words and generic path segments score 0 |
| `policies.fact_recurrence_threshold` | `5` | number of **other** sessions that must report a claim before it is promoted |
| `policies.fact_max_claim_chars` | `400` | longer claims are refused at ingest, never truncated |
| `policies.fact_max_facts` | `500` | fact store ceiling; past it the least-used, least-recently-used facts are evicted first |
| `policies.enforcement` | `"hybrid"` | the `execution-gate` / `status-guard` hooks: `hybrid` blocks the deterministic gates and warns on an `[EXECUTION RESULT]` that claims more than not_run, `warn` only reports, `off` disables both |
| `policies.write_allowed_outside_root` | `[]` | paths outside the project root a Write/Edit may land in, beyond the temp dir and `~/.claude` |
| `policies.graph_leads_enabled` | `true` | inject a shortlist from `graphify-out/graph.json` |
| `policies.subagent_fanout_enabled` | `true` | request fan-out for the exploration/reasoning roles |
| `policies.production_branch` | `"main"` | the only branch where `promote-write` will write. Promoted knowledge describes what is live; a hypothesis from a feature branch must not enter the shared repo carrying that authority |
| `policies.knowledge_dir` | `"docs/project-knowledge"` | location of knowledge documents, relative to the project root; an absolute path, `..`, or a symlink resolving outside the root is refused (`knowledge_path_escape`). The only workflow artifact that is **tracked in Git** — its whole value lies in being shareable |
| `policies.knowledge_relevant_limit` | `3` | maximum knowledge documents included in one delegated prompt. `0` turns off the knowledge sidecar |

`commands.auto_verify_after_execute` is read and returned in `meta.policy`; main_agent acts on it. The execute path itself is runtime state since agent-workflow 3.9.0 — see [Plan and execution state](#plan-and-execution-state).

Other main_agent instruction keys: `commands.allow_analyze_to_plan`, `commands.allow_explore_to_plan`, `commands.auto_sweep_after_execute`, `policies.workflow_prefix`, `policies.chat_mode_for_plain_text`, `policies.fallback_requires_confirmation`, and `policies.max_active_job_per_session`.

There is no `auto_verify_method` key. The two valid and orthogonal keys are:

- `commands.auto_verify_after_execute` — **when** verify is called; default `false`. When false, `/.execute` must report `verification: not_run`, with status `implemented`, and then offer `/.verify`.
- `commands.verify_mode` — **how deep** verify goes when called.

The retired key `commands.autoverify` is migrated on upgrade. A typo in any other key causes no error and falls back to the default.

### `.workflow/second_agent.json`

This project-local adapter file may be changed per project. Its name follows the ROLE, not the vendor — since v3.4.3 the second_agent provider is chosen through config, so this file no longer assumes OpenCode. A v3.4.2 workspace that still uses `.workflow/opencode.json` is migrated once on `upgrade` (values moved, old keys deleted). Do not confuse it with `<project_root>/opencode.json` and `~/.config/opencode/opencode.json`, which genuinely belong to OpenCode itself and keep those names.

At `init`, its source is, first that exists, the seed of the host that ran it (`second_agent.seed.<host>.json`, from `exoma init --host <host>`), the shared 0.x `second_agent.seed.json`, or the shipped `exoma/config/second_agent.example.json`. This file MUST exist per project: without it (or if it does not parse) delegated commands are rejected, with no fallback to machine-level config. The loader fills in keys not yet stored with the source defaults in memory; `upgrade` writes them to the file additively. Its effective shape:

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

#### Choosing a provider — and its security consequences

`provider` accepts `opencode`, `codex`, `agy`, or `claude`. The four are not equivalent choices:

| | `opencode` | `codex` | `agy` | `claude` |
| --- | --- | --- | --- | --- |
| Secret-file read boundary | **enforced** via `<project_root>/opencode.json` | **none** | **none** | **none** |
| Write sandbox | yes | yes (`--sandbox read-only` + `approval_policy="never"`) | **no** — writes in the working tree, and changes to files matching the secret patterns (ignored ones included), are only *detected* (`core/policy/agy_guard.py`), not prevented | yes — the session has only `Read`, `Grep` and `Glob` (`--tools`); no write, edit or shell tool exists |
| Selection | no question | confirmed in the `/.provider` picker | confirmed in the `/.provider` picker | confirmed in the `/.provider` picker |
| Project-root boundary config | file, refreshed on every `init`/`upgrade` | no such layer | no such layer | no such layer |
| `boundary` (`config/providers.py`) | `reads: secrets_denied`, `writes: denied` | `reads: unbounded`, `writes: sandboxed` | `reads: unbounded`, `writes: unbounded` | `reads: unbounded`, `writes: denied` |
| Second-agent instructions | `~/.config/opencode/AGENTS.md` block | `~/.codex/AGENTS.md` block | custom agent `~/.gemini/config/agents/exoma-second-agent/agent.md`, passed as `--agent` only while that file is installed (`provider_agent`, default `exoma-second-agent`, env `EXOMA_AGY_AGENT`) | `~/.claude/exoma/second-agent.md`, passed with `--append-system-prompt-file` while that file is installed |
| Prompt transport | attached file (`-f`) | stdin | argv | stdin |

**Claude as second agent.** `ClaudeAdapter` (`adapters/providers/claude_adapter.py`) runs
`claude -p --output-format stream-json --verbose --tools Read,Grep,Glob --setting-sources ''
--strict-mcp-config` with the prompt on stdin, plus `--resume <id>`, `--model` and `--effort`
when set. `--setting-sources ''` loads no user, project or local settings, so the user's hooks
(Exoma's Claude host hooks among them), permission rules, MCP servers and `~/.claude/CLAUDE.md`
never reach the call; `WORKFLOW_AGENT_ROLE=second_agent` is set as for codex. It works under the
subscription login: `--bare` would isolate further but accepts only `ANTHROPIC_API_KEY`. The
session id is read from the stream's `init` event; the answer is the closing `result` event, and a
result with `is_error: true` fails the call even at exit 0. Probed on Claude Code 2.1.316: the
init event listed exactly the three tools, a request to create a file was refused for lack of a
write tool, and nothing of the user's CLAUDE.md or SessionStart hooks was in context. The CLI has
no model listing, so the picker offers the bundled list (aliases `opus`, `sonnet`, `haiku`, then
pinned ids) and accepts any other id typed in.

`codex`, `agy` and `claude` are reported by `doctor` as `not_enforceable` with `trusted: true`: each can read every project file and receive the full process environment — their boundary is the agent's obligation, not runtime enforcement. Choosing `codex` or `agy` needs no variable or config key. The `/.provider` catalog gives each provider a `boundary_warning` (null for opencode) — `providers.provider_boundary_warning`, worded from the provider's `boundary`: codex "can read every file", agy "can read AND write every file" — and the picker quotes it in one confirmation question before applying. Every apply of such a provider, including direct mode (`/.provider <provider> ...`) and `exoma install --provider`, which ask nothing, returns the same text first in `meta.warnings` (the installer prints it as a plan warning). Nothing is refused: the acknowledgement is prompt-level, not runtime-enforced.

The write side is reported separately. `doctor` writes `checks.second_agent_write_boundary` (`{provider, writes}`) and, only for `writes: unbounded` (`agy`), one additional `WARNING: second_agent provider '<p>' can WRITE ...` in `recommended_fixes` — still not an issue. Every delegated result that reaches the provider stage (success or failure) carries `meta.provider_boundary` (`{reads, writes}`), and for `agy` also `meta.provider_write_warning`.

**Codex: `--sandbox read-only` alone does not stop writes.** That flag restricts shell commands. Codex's file-edit tool (`apply_patch`) is governed by the approval policy, not the sandbox: tested on codex-cli 0.154.0 on Windows, a read-only call was refused when writing through the shell and then succeeded in creating a file through that tool. The adapter therefore sends `-c approval_policy="never"` on every call, including `exec resume`; with that, codex rejects the patch ("writing is blocked by read-only sandbox"). The opt-in probe `WORKFLOW_CODEX_PROBE=1 python tests/run.py --only codex-write-probe` re-proves both write paths on new and resumed calls against the real CLI (uses codex quota).

**Codex on Windows — its own OS sandbox.** Codex's Windows sandbox (`elevated` backend) can
refuse to start a session: ``windows sandbox failed: elevated Windows sandbox requires effective
`:root` read access``. In 3.8.0 this was observed on **resumed** threads while new threads
worked (upstream regression openai/codex#46312, and #46114 when every thread
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
[Provider releases](#provider-releases); the flag form resumes on the releases tested
there, and the write probe proves read-only still holds. The runtime does **not** downgrade the sandbox: the deny-read profile it sends
needs the `elevated` backend, and choosing a weaker one (`[windows] sandbox = "unelevated"` in
`~/.codex/config.toml`) is the user's decision. The codex version (`codex --version`, read once per
process) is recorded in `meta.provider_version`, in the usage row (`provider_version`), and in
`doctor` (`checks.provider_version`).

Codex sends the same deny list as a `-c permissions.workflow.filesystem` flag on every call, but that flag stops nothing. Tested against codex-cli 0.147.0 in `exec` mode: denying `**` and `**/*` for `:workspace_roots` and then asking for a file in that root still returned its contents, exit 0. Codex reads by running a shell, and `--sandbox read-only` (together with `approval_policy="never"`, see above) restricts **writes**, not reads.

This means a codex second_agent can read every file in the project you point it at, `.env` included. `init` reports this as `status: not_enforceable` with `permissions_enforced: 0`, and `src/exoma/_bundle/config/providers/codex/AGENTS.md` tells its agent that avoiding secret files is its own obligation — an instruction, not enforcement.

Use `codex` when the project holds no secrets, or when you accept the risk. For a project whose secrets must stay unreadable to second_agent, use `opencode`.

Since v3.7.1 this position is explicit: `codex` and `agy` are treated as **trusted providers**. Besides having no read boundary, both adapters pass the full process environment to the provider CLI (credentials in env become visible). That is accepted as a risk, not fixed with an allowlist — an allowlist could break the provider CLI's auth, and a provider that can already read `.env` is protected by nothing through a trimmed env. Instead, the risk is always visible: `/.doctor` writes a `second_agent_read_boundary` check and one `WARNING` in `recommended_fixes` whenever the active provider is `codex`/`agy` (not an issue — readiness stays READY), and `/.provider` mentions it when selecting. The check carries `secret_files: {count, sample, truncated}` from `core/policy/secret_patterns.find_secret_files`: project files whose path matches the canonical secret patterns, ignored files included, skipping dependency and build directories (`node_modules`, `vendor`, `.venv`, `dist`, `build`, …) and `.git`/`.workflow`, at most 50,000 entries per scan (`truncated: true` past that). The warning names up to five of them. Files are matched by name only; contents are never opened. The threat model behind these choices: [security.md](security.md).

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
is used. Live results are cached for 10 minutes in `<cache user>/exoma/model-catalog.json`
so that the catalog and apply within one picker session see the same list.
`EXOMA_MODEL_CATALOG=static` turns off live reading.

Direct mode: `/.provider <provider> <model> [effort] [routes]` (e.g. `/.provider codex
gpt-6-luna low`) skips the picker. A payload without `|` is split on spaces — safe because no
provider, model id, or effort contains a space — and is then validated exactly like the
`provider|model|effort|routes` payload.

Reliability keys (v1.1.0):

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

**Roles are not read from this file.** The command → role mapping is defined in code (`exoma/config/routing.py`), so it cannot be overridden via config.

**Three retired keys.** `job_max_runtime_seconds`, `job_poll_timeout_seconds`, and
`agent_workflow_path` used to be written to this file and were never read by anyone. All three
are no longer written; their values come from `EXOMA_JOB_MAX_RUNTIME_SECONDS`, the CLI
`--poll-timeout`, and `.workflow/config.json → runtime.python`/`runtime.package_root`.
The upgrade to the `data/` layout (v3.6.0) drops retired keys from `config.json`; before that, `doctor` flags them
`retired — never read` and they are safe to delete manually.

### Sub-agent fan-out (default ON)

`policies.subagent_fanout_enabled` applies to `explore`, `plan`, and `analyze`. `verify` keeps using a single reviewer so the whole diff is seen as one change.

Upgrade preserves the existing value. An old workspace that already stores `false` stays disabled until you change it yourself.

When active, the prompt carries the `[EVIDENCE_SIDECARS]` anchor; the fan-out clusters live in
`.workflow/data/sessions/<id>/runtime/leads.json` so they do not inflate the prompt:

- the graph has ≥2 communities → one slice per community;
- no graph, or it yields only 0–1 communities → still fan out to four investigation angles: entry points, core flow, reverse dependencies, and config/tests.

Second_agent must use the custom agent `exm-slice` via the `task` tool when available,
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

## Session export

`exoma session export <MAIN_SESSION_ID> [--work-dir DIR] [--out PATH] [--no-transcript]`
writes one main session as a zip, by default to `<data dir>/exports/<id>_<UTC stamp>.zip`
(`core/audit/session_export.py`). Made for collecting real sessions in a study
([experiment protocol](team-guide/experiment-protocol.md)); nothing is uploaded.

| Bundle path | Content |
| --- | --- |
| `session/...` | the session directory (prompts, responses, per-call logs and their archived evidence, runtime sidecars); `lock` files and files over 8 MB are listed under `skipped` instead |
| `streams/{usage,audit,quality}.jsonl` | the rows of each shared stream whose `session_id` (or `main_session_id`) is this session |
| `transcript/<file>` | the host's transcript of the main session: Claude Code (from `~/.claude/session_registry.json`) or Codex (the rollout file of the bound thread); otherwise `transcript.status: unavailable` with the reason. OpenCode and Agy transcripts are not collected |
| `review.md` | the participant's review |
| `manifest.json` | `schema`, `exoma_version`, `session_id`, `host`, `transcript`, `redactions`, `files` (path, bytes, sha256), `skipped`, `review` (`status: template\|filled\|unreadable`, `source`) |

Every text file is passed through `utils/redact.redact` before it is written: credential-shaped
runs become `[REDACTED:<kind>]`, and only the count reaches the manifest. A file that is not UTF-8
text, a symbolic link, or a file resolving outside the session directory is never copied: it
is listed under `skipped` with the reason, because redaction cannot check it. The review lives beside
the bundles as `<id>.review.md`: the first export writes it from a template and reports
`review: template`; once edited, the next export of the same session carries it and reports
`filled`. A review file that is not UTF-8 text is not copied: the export completes with
`review: unreadable` and lists it under `skipped`. An unknown session, or an id that is not a plain path component
(separators, `..`), exits 1 without writing anything.

## Graphify

With `policies.graph_leads_enabled: true`, the runtime reads `graphify-out/graph.json` **directly** without calling the Graphify CLI or MCP. It ranks at most 12 candidate files by keyword and weighted dependency degree, carries at most four communities, then injects them as **starting points, not evidence**.

- `explore`/`plan`/`analyze` receive the full leads and communities for fan-out;
- `verify` receives a compact shortlist of at most six files without communities;
- a graph older than the sources (`.py`, `.js`, `.mjs`, `.cjs`, `.ts`, `.tsx`, `.jsx`, `.php`, `.go`, `.rs`, `.java`, `.rb`) is still used, but the prompt carries a stale warning;
- graph absent, broken, or being refreshed (`graphify-out/.refresh.lock` held by a worker that is still beating) → the delegated flow still runs without leads and does not wait for the lock. The `usage.jsonl` row records `graph_status` (`used`, `empty`, `absent`, `refreshing`, `disabled`) and `graph_ms`. `verify-browser` does not use leads, so it does no lookup; it only records once per command whether a refresh is running: `refreshing`, `available` (graph exists, not read), or `absent`.

The Python runtime never runs `graphify init`, `build`, `watch`, or `update`. The only trigger of `graphify update` is the `graph-refresh` Stop hook in the Claude bundle (`.sh` launcher + `graph-refresh.py`); `CLAUDE.md` no longer asks the main agent to run it. The hook runs after `[EXECUTION RESULT]`, only when the graph already exists, is older than the sources, and the binary is available:

- the mtime scan skips the skip directories before entering them, so its cost follows the number of sources, not the number of dependencies; directory names and extensions are matched case-insensitively. The runtime fingerprint uses the same extensions and skip directories: `__pycache__`, `.git`, `graphify-out`, `.workflow`, `node_modules`, `venv`, `.venv`, `site-packages`, `.tox`, `vendor`, `storage`, `dist`, `build`, `.next`, `coverage`, `target`;
- stale graph → the hook creates `graphify-out/.refresh.lock` (`pid`, `token`, `started`, `beat`), starts a separate worker, then returns. The turn does not wait for graphify. The worker runs `graphify update` **with no time limit** and never kills it; while graphify runs, the worker rewrites `beat` (and `graphify_pid`) every 15 seconds, then records one row and deletes its own lock. `GRAPH_REFRESH_LIMIT_S` is no longer read. The hook hands the lock to the worker only while it still carries that hook's token, and if the worker has already finished and deleted it, the lock is not recreated. A stale lock is deleted only if its content is still the same when reread right before deletion; a lock that changed is treated as `skipped_running`, so two sessions do not run two refreshes at once;
- graphify rewrites `graph.json` in place, so while the lock is held the runtime treats the graph as unavailable, and the main agent is asked to treat it as stale. The lock counts as held while its `beat` is < 120 seconds old, whatever the lock's age; a refresh of any length never reads as stale. A lock without `beat` (written before agent-workflow 3.9.0) uses the old rule: pid alive and < 15 minutes old. A lock that exists but cannot be read counts as held while its mtime is < 120 seconds old. Every lock write goes to a temporary file and is then renamed;
- a hung graphify holds the graph for as long as its worker lives. `--command doctor` reports that lock (`checks.graph_refresh_lock`: `state`, `age`, `beat_age`, `pid`, `graphify_pid`) and gives advice when the refresh has been running ≥ 1 hour or the lock is stale; this is advice, not an issue, because the delegated flow still runs without leads. `--command clean` only deletes a stale lock (no `beat` within 120 seconds) and never kills a process: stop the graphify process yourself, wait for the lock to go stale, then run `clean`. If the worker dies while graphify is still alive, the lock goes stale within 120 seconds even though graphify may still be writing;
- a refresh counts as `refreshed` when `graph.json` was rewritten and parses as JSON, whatever the exit code (graphify exits 1 on large graphs because of the HTML visualization); a rewrite that does not parse is recorded as `corrupt`;
- every run that passes the first gate on a project that already has `graph.json` writes a `kind: graph_refresh` row to `.workflow/data/quality.jsonl` (when that directory exists) with `outcome` (`skipped_fresh`, `skipped_running`, `no_graphify`, `refreshed`, `corrupt`, `error`; `timeout` only appears in older rows and is still counted), `hook_ms` (what the turn paid), `scan_ms`, `files_visited`, `dirs_skipped`, and for a refresh `graphify_ms`, `graphify_exit`, `graph_rewritten`. `--command report` summarizes it in `graph_refresh`.

The hook fails open, never rejects a response, never creates a new graph or runs `init`/`build`/`watch`. The Stop hook's outer timeout stays 60 seconds.

The graph verdict cache uses a fingerprint of the path, nanosecond mtime, and size of every
source file (same extensions and skip directories as above). A content change, addition, or deletion of a source invalidates the cache even if
`graph.json` did not change.

---

## Task telemetry

`--command report` → `tasks` reads `.workflow/data/tasks.jsonl`. One task = a series of events within one MAIN_SESSION_ID, from its first event until a verify whose runtime-**derived** verdict is `pass` after that task edited files. A commit is the recommended next step, not the closer. Events are written by two parties, with no additional instructions in the prompt:

- runtime: one event per delegated command (`explore`, `analyze`, `plan`, `verify`, `verify-browser`). A verify event carries `verdict` (the one **declared** by the reply; `DONE` with a blocking finding counts as `NEEDS FIX`) and `derived` (the runtime-derived verdict: `pass`, `incomplete`, or `fail`). Only `derived: pass` closes a task; a `DONE` that the runtime considers `incomplete`, including because of `not_verified`, leaves the task `open`;
- the `task-events` hook (`.sh` launcher + `task-events.py`; `UserPromptSubmit`, `PostToolUse` matcher `Skill|Edit|Write|MultiEdit|NotebookEdit`, `Stop`): local skills invoked from the prompt (`/.<name>`) or via the `Skill` tool, files inside the project that were edited (not `.workflow/`/`.git/`), and commits as HEAD **moving between turns**. The session's first event stores a HEAD snapshot in `.workflow/data/task-hook/<claude_session_id>.head`; each `Stop` (or the next prompt, for commits made in a terminal between turns) records the commits since the snapshot — first-parent, oldest first, each with `paths` — then advances the snapshot. Events are v2 (`v: 2`). Before 3.8.0 this hook ran on every `Read` and `Bash` (twice for `Bash`); one PowerShell spawn is ~450 ms on Windows, while skills and commits only need to be read once per turn. Consequence: a skill loaded via `Read` without a prefix (natural-language auto-intent) is not recorded, and a commit is recorded at the end of the turn that made it, after the verify in that turn. The anchor check keeps it that way: at a `Stop` whose reply carries its own `[PLAN]` or `[ANALYSIS RESULT]` block after a plan/analyze run, the hook reads `transcript_path` backward from its end, only as far as the newest `.workflow/run.sh plan|analyze` call (at most 32 MiB), and counts an assistant `Read` tool call made after it whose range overlaps an anchor that run cites; with none, it prints an `[ANCHOR CHECK]` systemMessage, once per run (`runtime/anchor-reads.json`). No other `Stop` reads the transcript, and lines without a `tool_use` are never parsed. A read through the shell is not seen, and an unreadable transcript, or one with no runner call within that limit, warns nothing. `policies.enforcement: off` turns it off.

States: `completed` (verify `derived: pass` with no edit afterwards, committed or not), `open` (has edits, not yet `pass` — committed or not), `read_only` (no edits), `unknown` (events without a session identity). An edit after `pass` opens a new task. A commit containing files of a task that just became `completed` goes into that task's `sequence`; a commit in an `open` task goes into its `sequence` without changing its state. Older events do not carry `derived`, so their task stays `open`. Output: `tasks`, `by_state`, `skill_counts`, `unclaimed_commits` (distinct commits none of whose copies is claimed by a task; a commit is marked as used only when claimed, so a copy recorded earlier by another session does not hide it from the session that made it), and `recent` (the last 10 tasks with `sequence`). This stream is separate from `usage.jsonl`: local skills and edits are not delegated calls and must not enter budget governance.

`--command report` → `rework` is computed from `usage.jsonl`, not from this stream: per `correlation_id`, the assessed verify rounds are ordered by `recorded_at`, and a group counts as rework only when a round that is not `pass` (`fail` or `incomplete`) is followed by another round; `extra_verification_rounds` = the rounds after the first failure. A plan verified per phase with all rounds `pass` is not rework (`runtime.rework@1` in [metrics](evaluation/metrics.md)).

---

<a id="mode-verify"></a>
## Verify mode

### Runtime preparation

Before selecting tests, call `run.sh verify-prepare --session '<MAIN_SESSION_ID>'`.
This local protocol command returns `{ok, content, meta}` without a provider call, job,
test run, request claim/consumption, verdict, or delegated-marker removal. An explicit
session is required. It is a runner command, not a user-facing skill.

`meta.policy` contains `verify_mode`, `auto_verify_after_execute`, `test_command_prefixes`
(argv arrays), `test_timeout_seconds`, and `max_commands` (5). `meta.request` describes
only that session's pending `verify/tests.json`: `path`, `status` (`missing`, `invalid`,
or `valid`), `commands`, `reason`, and `validation_errors`. Claimed requests from earlier
jobs are not inspected. Missing/invalid requests are successful inspections, not verification
passes. `meta.config_warnings` reports invalid configuration without returning raw config.
`meta.next_step.action` is `select_tests`, `fix_request`, or `verify`; syntax mode needs
no test request. The agent selects relevant tests from the returned prefixes, writes the
request, and invokes verify explicitly. Preparation never selects or executes tests.

Preparation and execution share policy and command validation. Missing/unreadable config
uses delegated mode, an empty allowlist, 900 seconds, and auto-verify false. Invalid mode,
timeout, or boolean values use those defaults with diagnostics; auto-verify requires an
actual boolean (the string `"false"` is not true). A malformed allowlist permits no commands.
Preparation is a snapshot: actual verification rechecks policy and claims the request as
usual. Agents need not read config or pending requests directly; old read exemptions remain
available for compatibility.

The configuration contract in question separates two settings:

- `auto_verify_after_execute` determines whether main_agent automatically calls `/.verify` after implementation; default `false`;
- `verify_mode` determines the depth of the check when verify is actually called.

So `auto_verify_after_execute: false` does not turn off `/.verify`; it only prevents the automatic call.

`verify_mode` controls **how deep** `/.verify` goes:

- **`delegated`** (default) — second_agent verifies and returns a labeled contract. Every finding must carry three tags: `severity` (critical/high/medium/low), `origin` (introduced/regression/pre_existing/unknown), `scope_relation` (in_scope/out_of_scope). Blocking is determined by all three tags; a pre-existing defect does not block the current change, while `origin: unknown` fails closed. For an empty section write exactly `- none`; describe the checks in `checks_run`. Compatibility forms include `none.`, `n/a`, `not applicable`, `nothing found`/`nothing to report`, `no (blocking) findings`, `tidak ada`, `tidak ada temuan`, and separator notes consisting entirely of supported neutral phrases such as `clean`, `all clean`, `ok`, `no issues`, `bersih`, and `semua bersih`.

  Parentheses now use a bounded grammar too: supported neutral phrases, `tidak ada blocking findings`, or `checked` followed by check targets joined by `and`, optionally ending in `, clean`. Targets are hooks, telemetry, retry/timeout paths (optionally prefixed by `the`), or `file:line` references (optionally followed by `and its callers`). For example, `none (checked hooks and telemetry)` and `none (checked core/evidence/contract.py:590 and its callers, clean)` remain empty. The legacy `none (checked the retry path)` followed by `and the timeout path` remains empty only because the **whole** combined explanation matches that grammar. Unknown prose is meaningful, including `none (caller X fails)` or a neutral prefix followed by a problem. Both the validator and E2E reviewer retain complete continuation text and apply the same classifier; plain `none` followed by a finding is never empty. Tags on continuation lines participate in routing.

  The shared commit gate likewise preserves the distinction between command data and execution. A complete `python[3[.version]][.exe] -c` invocation containing only one `print` expression with literal string arguments is recognized as data. The matcher rejects double-quoted shell expansion/escaping, wrappers, extra statements, keyword arguments, redirects, chaining and pipelines; anything unrecognized retains the existing approval check. This bounded recognition is not a general shell parser. P01–P12 and G01–G04 run through the native `research-parser-regressions` and `research-gate-regressions` suites, with command strings passed as data only.
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
  tree (process group + `taskkill /T` / `killpg`; on Windows a Toolhelp snapshot and process handles
  provide a fallback when `taskkill` is denied, killing a descendant only while its creation time
  still matches the snapshot), then output is collected for at most 5 s; a boolean
  timeout value is ignored (default 900). Second-agent verification always reads and traces;
  it never runs test runners, including when the allowlist is empty or E2E evidence is supplied.
  The runtime owns test execution. An empty allowlist produces a doctor warning
  (`checks.verify_test_commands: empty`); it does not alone prevent a review-based `pass`.
  An explicit request with `commands: []` and a one-line reason records "no test requested",
  which is not an executed check. A pass then requires actual review checks; a missing or
  invalid request remains a verification gap.
  Under a verify intent, main_agent calls local `verify-prepare` for policy/request diagnostics,
  reads `git diff --name-only`/`--stat`, and writes `verify/tests.json` before delegated dispatch.
  Legacy native config/request read exemptions remain compatible but are no longer needed.
  The reason: when the second agent also ran tests, codex
  ran the whole suite (verify median up to 366 s) while opencode could not
  run a single one and its verify became `incomplete`.
  **Tests already run.** `run.sh test --session <id> --task '<command>'` (`--task` may be a JSON
  list, at most 5) runs commands under the same policy (allowlist, no shell, timeout) and appends
  each non-refused outcome to `sessions/<id>/verify/test-runs.jsonl` with `fingerprint`, the reuse
  identity (`verify_tests.reuse_identity`): a sha256 over the Git tree id of the working tree's content
  outside `.workflow/` (`core/evidence/tree_fingerprint.py`: `git add -A` into a copy of the index that
  keeps its mtime, so Git's racy-entry check still catches a same-size edit, then `git write-tree`;
  `assume-unchanged` and `skip-worktree` are cleared on the copy first, one `update-index` option per
  call, and the copy's mtime put back, because Git never reads a flagged entry's file and an edit to
  it would otherwise keep the old blob; the flagged paths are read as bytes, so a name holding a
  carriage return round-trips; a flag that cannot be cleared, or a listing that fails, gives no tree
  id (`null`, nothing reusable); the real index is never touched; any failing Git step (`git add
  -A`, the removal of `.workflow/` from the copy, `git write-tree`) also gives `null`;
  HEAD is not part of it, so a commit that changes no file keeps it; content as Git stores it, so an
  edit Git's conversions undo, such as LF to CRLF under `text`/`eol` attributes, `core.autocrlf` or a
  clean filter, keeps it too: list a byte-sensitive input in `verify_reuse_files`), the bytes of
  `.workflow/config.json`, every environment variable whose name matches
  `commands.verify_reuse_env` (default `WORKFLOW_*`, `EXOMA_*`, `PYTEST_*`, `CI`, `NODE_ENV`,
  `APP_ENV`), and every file matching `commands.verify_reuse_files` (globs under the project root,
  default `.env`, `.env.*`; for inputs Git ignores). Only hashes are stored. The identity is taken
  before the first command and after every command, and each row carries the identity its own
  command ran on, `null` when that command changed it. A list that changes an input, tests it, then
  restores it therefore records the middle pass under the middle state, never under the state the
  list started and ended in; the call's `fingerprint` is the starting identity, `null` when any
  command changed it. A list in which N commands run takes N + 1 identities (a refused command
  takes none). When a verify then requests the
  **exact same command string** and that command's **latest** recorded run `passed` under the
  identity the project has now, it is not run: its outcome is `passed` with `reused: true`, `ran_at`
  and `ran_seconds`, its `checks_run` line ends `— reused, not rerun`, and `meta.runtime_tests.reused`
  counts them. Reuse holds only until a command of that verify actually runs (it may write the
  tree): every command after it runs too. It still has to be allowlisted now. The key is the command text, so a filtered run
  (`--only`, `-k`, a test path) never stands in for a full one or the reverse. A failed latest run, any
  change to the identity's inputs, no Git (`null`), or a record with a line that cannot be read runs it
  as before. Each `run.sh test` first writes its own marker, `verify/test-runs.<pid>-<token>.pending`
  (whole: a temporary name, then a rename), naming its process (pid and start identity) and its
  commands. When the marker cannot be written, nothing runs and the call fails with
  `session_state_error`. Every command a marker names is withheld from reuse, whether its run is
  still going or crashed; a marker that cannot be read withholds everything, and so does a record
  that cannot be written. After appending its outcomes a run removes its own marker and the markers
  of runs that were already dead before it started and whose commands it has just recorded, never a
  live run's nor one that appeared while it ran, so concurrent runs cannot clear each other or
  expose an older pass. A marker that cannot be read and is older than any run can last
  (5 × `verify_test_timeout_seconds` + 300 s, by its mtime) is retired by the next `run.sh test`
  before it runs: a `{"barrier": ...}` row is appended first, which drops every earlier row from
  reuse, then the marker is renamed `*.pending.corrupt`. A newer one keeps withholding everything.
  An identity that cannot be computed (for example a file name Git or Python cannot encode) makes
  the run unreusable (`fingerprint: null`), never a crash. "Dead" means proven dead (`osutil.process_state`): a pid
  the OS cannot answer for (Windows access denied, any other query failure) counts as alive, unlike
  `osutil.process_alive`, which the job reaper uses and which counts unknown as dead. A run whose record cannot be written leaves its marker
  and removes the record file; every such failure is reported as `record_error` (in `meta` and the
  `[TESTS]` note) with `fingerprint: null`. A pass made by verify itself is not recorded; a command
  verify ran that did not pass (`failed`, `timeout`, `error`) is appended as a row with
  `"source": "verify"` and `fingerprint: null`, so an earlier `run.sh test` pass for it is no longer
  the latest row and is not reused. When that append fails, verify writes a pending marker naming the
  failed commands, with `"kind": "verify_failure"`, so they stay withheld until a `run.sh test` that
  saw the marker before it started records them all again; that run retires it even while the
  verify's process is still alive (a foreground verify), because the marker records a failure that
  is over, not a run that is going; when the marker cannot be written either, the record
  file is removed (nothing in the session is reused); `meta.runtime_tests.record_error` says which
  happened, and that an earlier pass may still be reused when neither worked. A verify that dies
  before its tests finish records nothing. `reusable` takes no lock: after
  computing the identity it reads the markers and the record's size and mtime again, and returns
  nothing when either changed, so a `run.sh test` that started or recorded during the read is not
  missed; one that starts after the read returned is not seen by that verify. Not covered: installed dependencies and virtualenvs, submodule contents, `PATH` and
  `PYTHONPATH` (they differ between `run.sh test` and the verify worker), and ignored inputs nobody
  listed; a project whose tests read such inputs lists them in `verify_reuse_files` or keeps those
  tests out of `run.sh test`. Output: `[TESTS]` with one line per command and a note that counts
  the passes a later verify may reuse, taken from `meta.reusable` alone, never from the call's
  `fingerprint` (a list can keep its identity while a command's last outcome failed or another
  run's marker withholds it) (`recorded: K of M passes reusable` when some command in the list
  changed the inputs; that command's own pass is the one not reusable); `meta.reusable` lists those
  commands by the rules verify applies (each command's last outcome in the list passed under the
  state it ran on, and no marker left after the run withholds it; an unreadable marker empties it),
  not whether the project is still in that state when a verify asks; any
  command that did not pass → `tests_failed`; bad `--task` → `test_input_invalid`; empty allowlist →
  `test_not_configured`.
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
(`stable_version`): opencode `1.19.3`, codex `0.168.0`, agy `1.1.13`. The runtime reads
`<cli> --version` (`core/provider/versions.py`; the first line containing a semver, from stdout
then stderr) at most once per process and stores it per user (`%LOCALAPPDATA%` or
`$XDG_CACHE_HOME`/`~/.cache`, file `exoma/provider-versions.json`), keyed by the executable's path,
mtime, and size, expiring after 24 hours; a CLI without a version is cached as `null`, a timeout is not
cached. Every usage row that reaches the provider (single calls as well as each continuation row,
all providers) carries `provider_version` and `provider_version_status`: `stable` (same), `untested` (another
release, including pre-releases), `unreadable` (the CLI did not answer). `doctor` reports
`checks.provider_version` `{provider, version, status, stable}` and warns on
`untested`. It is advisory: the call still runs; nothing blocks the provider, and the installer
only changes providers when asked to with `--update-providers`.

`exoma install --update-providers` (opt-in) brings each CLI to `stable_version`, never
to a newer release. `--dry-run` prints the plan per provider; an install executes it
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
`sandbox_unavailable` while the resume sandbox was sent via `-c sandbox_mode`;
the adapter now sends `exec --sandbox`, and resume works on the releases tested within that range
(codex-cli 0.155.1 and 0.160.0).
`stable_version` moves only to a release that passed the live probe. On 2026-11-13,
Codex `0.168.0` with `gpt-6.1-sol` passed `codex-write-probe` on Windows: shell and
file-edit writes were refused on a new and a resumed thread, the thread identity was
preserved, and JSONL usage remained available. The shipped `auto_review.extra_policy`
was accepted as a string and refused as a number by the real CLI in a temporary home.
The host reviewer-context allowlist retains the previously tested `0.163.0` alongside
`0.168.0`; this is separate from the updater's single preferred baseline.

The opt-in probe uses `WORKFLOW_CODEX_PROBE=1`; `CODEX_COMMAND` can select an isolated
CLI, `WORKFLOW_CODEX_PROBE_MODEL` names its model, and `WORKFLOW_CODEX_PROBE_VERSION`
requires its exact version. An enabled probe fails if the CLI is missing, the provider
turn fails, or either write attempt lacks evidence. Shell attempts come from exec's
structured events. A rejected patch omitted from those events is checked against the
matching tool call/output in this probe's own thread journal, including Code Mode
`tools.apply_patch` calls. Resume reads only newly appended records, so it cannot borrow
the fresh turn's proof. Journal inspection reads neither user auth/config files nor
other conversations; the live calls still use normal CLI sign-in.

Earlier live probes established the Windows write boundary for Codex `0.163.0` and
OpenCode `1.19.3` on fresh and resumed sessions.

---

## Fact store

`.workflow/data/facts.jsonl` stores knowledge that persists across sessions. A claim enters through one of two paths:

1. explicitly tagged `[config]`/`[pattern]`/`[invariant]` by second_agent, or
2. reported independently by ≥ `fact_recurrence_threshold` **other** sessions.

Every `file:line` the claim cites must resolve (a short path through the unique-suffix match
above, stored under the path it resolved to); one that does not refuses the whole claim. The
first anchor is stored in `file`/`line`/`anchor_hash`, any further ones in `anchors[]`, and a fact
is served only while all of them can still be placed. Claims longer than `fact_max_claim_chars`
are refused.

Retrieval scores each fact against the task: a shared identifier (snake_case, camelCase, dotted or
backticked) counts 2, a shared word 1, a token found only in the fact's path 0.5; English and
Indonesian function words and generic path segments (`core`, `tests`, `py`, ...) count nothing.
Facts below `fact_min_relevance` are dropped; ties break on identifier hits, then recency. Promoted
knowledge is ranked with the same function. Each served fact's `hits` and `last_used` go to
`.workflow/data/facts-usage.json`, and eviction past `fact_max_facts` keeps the most-used, most
recently used facts. The call's `call.meta.json` lists what it was served under `served_facts`
(`key` = the usage-sidecar key, `file`, `line` as served; `[]` when nothing passed the floor;
absent for roles that get no facts), so a later replay knows the exact set
(`tools/maintain/replay_facts.py`, [metrics](evaluation/metrics.md)).

The current session is excluded from the recurrence count. Without that, a fact injected into the prompt and then merely echoed back could raise its own count up to the promotion threshold — repetition masquerading as evidence.

Each fact is anchored to the content hash of its `file:line` line. When that line changes, the fact is considered stale when read and is never served as fresh.

Two claims are merged only when **all** guards agree: same `file`, same `category`, identical `anchor_hash`, same negation polarity, Jaccard similarity ≥ 0.5, and both claims ≥ 6 words. One guard refuses → both are kept. Duplicates are cheap; a lost fact cannot be recovered.

## Plan and execution state

Since agent-workflow 3.9.0 the plan → `/.execute -y` → `/.verify` chain is runtime state in the session's
`runtime/` directory (`core/runtime/execution.py`):

| Command | Reads | Writes | Refuses with |
|---|---|---|---|
| `plan-record <path>` | the plan JSON at `<path>` | `plan.json` (with `base_tree`) | `plan_invalid`: `decision: proceed` with open questions, `solution_path` below `high`, an empty `files` scope, an option without `minus`, or not exactly one recommended option; `plan_input_missing`, `plan_input_unreadable` |
| `execute-begin` | `plan.json`, `execute.token` | `execution.json` (`status: active`, `verification: not_run`, `scope` = the plan's `files`, `plan_revision`, `begin_tree`, `changed_since_plan`); consumes the token | `execute_no_plan`, `execute_plan_not_proceed`, `execute_not_approved` (no `execute.token` younger than 15 minutes bound to the current plan revision, scope and session; a legacy/unbound or copied token; a token already consumed; the plan re-recorded — even unchanged — before or during opening); `runtime_lock` when the session's execution transaction cannot be acquired in time or its state cannot be read or written |
| `execute-end` | `execution.json` | `status: implemented`, `end_tree`, `changed_files`, `outside_scope` | `execution_not_active` |
| `verify` (delegated or syntax) | — | `verification`: `pass`, `fail`, `incomplete`, or `syntax_only`; `changed_since_execute` | — |

**Tree snapshots.** `base_tree`, `begin_tree` and `end_tree` are Git tree ids of the working
tree outside `.workflow/` (`core/evidence/tree_fingerprint.py`, the same id test reuse uses), taken
at `plan-record`, `execute-begin` and `execute-end`. Each change list compares two snapshots
(`git diff-tree`), so it holds only what changed between them, never the uncommitted edits the
tree already carried: one `{status, path}` per file (`A`, `M`, `D`, `T`; the path relative to the
project root). `changed_since_plan`: plan to `execute-begin`. `changed_files`: `execute-begin` to
`execute-end`. `outside_scope`: the `changed_files` paths the plan's `files` do not cover, by the
`execution-gate` rule (the file, a directory above it, or a glob) — written through any tool,
Bash included, which the hook never sees. `changed_since_execute`: `execute-end` to a finished
verify, also put in that verify's `meta` when non-empty. `execute-end`'s `content` names the count
and the out-of-scope paths. A snapshot that cannot be taken or compared (no Git, or `git gc`
pruned an unreferenced tree) makes the list `null`: unknown, never "nothing changed". Like test
reuse, a snapshot sees content as Git stores it, so an edit Git's conversions undo (LF to CRLF in
a normalized file) is not a change.

**Approval binding.** An `execute.token` carries a `plan_binding`: the recorded plan's revision,
its `files` scope and the runtime session. `execute-begin` opens an execution only when that
binding equals the current plan's, so a token from before a `plan-record` (an identical document
included), from another session, or without a binding cannot open it. The token is claimed by an
atomic rename, so one `-y` opens one execution even when two `execute-begin` calls race. Plan and
execution writes share one per-session transaction lock with the hooks; contention past its bound
returns `runtime_lock` instead of a partial write.

`execute.token` and `commit.marker` are written by the `intent-gate-set` hook only for a prompt
the user wrote: `/.execute -y` (typed or as the expanded slash command) and a prompt asking to
commit (a bare affirmative keeps an existing commit marker; any other prompt, a negated mention
included, clears it). Both live in the session's own runtime directory, and a fork is its own
session, so a prompt in one window never clears another window's approval. For a prompt whose
`source` neither list names, see the intent gate above: the execute command must open the
prompt. Commit also accepts a direct request at the start such as `gas commit`, `tolong
commit perubahan ini`, or `please commit these changes`. Codex and OpenCode apply that
same rule after normalizing `$exoma-commit`. Pasted material, skill bodies, code fences,
blockquotes and quoted examples do not supply commit intent or negate a request outside
them. A bare affirmative never creates or renews permission; an expired marker remains
expired. Negation outside a literal still applies when naming a quoted command, such as
``don't `git commit` yet``. These commit rules do not relax the anchored `execute -y` approval.

Hooks, governed by `policies.enforcement`:

- `execution-gate` (PreToolUse, `Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell`): blocks a
  write outside the project root (the temp dir, `~/.claude` and `policies.write_allowed_outside_root`
  excepted), a write inside the root outside an active execution's scope (`.workflow/` always
  passes; with no active execution every in-root write passes), and `git commit` without a
   `commit.marker` younger than 30 minutes (`git.exe` and a quoted path to git count, as do
   quoted option values such as `git -C "a b" commit`). One validated call to this project's
   `.workflow/run.sh` for `explore`, `plan`, `analyze` or `verify` treats its task argument
   as data: mentioning `git commit` there needs no commit marker. This exception requires
   an absolute path to the runner, a recognized interpreter/direct runner, named `--session` and exactly one `--task` or
   `--task-file`, with bare/single-quoted arguments and no additional shell commands.
   `run.sh test` is excluded because its task is a command to execute. Appended commands,
   nested shells, substitutions, relative/tilde runner paths, unknown options, positional runner arguments and other
   unsupported quoting retain the conservative regex check; text in those forms can still
   produce a false block. Containment is decided on the path after resolving symlinks and
   junctions; scope accepts either spelling. Shell commands are not parsed for file writes.
  Every refusal, blocked (`hybrid`) or warned (`warn`), appends one JSON line to
  `runtime/gate-blocks.jsonl` of the bound session: `at`, `hook`, `rule` (`commit_unapproved`,
  `outside_root`, `outside_scope`), `tool`, `mode`, `exit` (2 or 0) and the active `plan_id`.
  The path and the command are not recorded. Writing the line is fail-open: a failure never
  changes the decision.
  If its helper, session binding, workspace, or payload is unavailable, or an exception
  prevents checking, the Claude hook stays fail-open but emits an unavailable warning and
  records a reason in `gate-skips.jsonl`. It falls back to `~/.claude/gate-skips.jsonl`
  without a session; new shared rows use `root_digest`. Intentional `off` is recorded
  without an unavailable warning. Doctor's `checks.execution_gate` separates configured
  mode from recent fail-open observations; `unconfirmed` never means enforcement was proved.
- `status-guard` (Stop): when `execution.json` says `verification: not_run`, a reply whose own
  `[EXECUTION RESULT]` block claims more — `status` outside `implemented|partial|blocked`, or a
  `verification` other than `not_run` — gets a `systemMessage` warning. It reads those fields
  only, never prose: the word list it used before fired on "begitu selesai, saya lanjut" and on
  every quoted digest. The block must be the reply's own: one quoted with `>`, inside
  a code fence, or named mid-sentence is a relayed result and is not read. A reply with no
  `[EXECUTION RESULT]` is not judged. It never blocks.

## Evidence reuse and boundary

Promoted knowledge is checked before injection: freshness per claim is separate from
document applicability. Invalid documents and unsafe, secret-pattern, ignored, or
out-of-project code sources are not used for automatic source reads.
Missing/unlocatable code sources are omitted, and summaries are
rebuilt from retained claims. Commit/blob shortcuts require a clean existing source;
otherwise the current working-file anchor is checked. Git path output is read as bytes and
decoded with the file system encoding, so a non-ASCII name is matched by its real spelling
under any process locale; undecodable output is an unresolved comparison, not a clean tree.
Index entries marked `assume-unchanged` or `skip-worktree` count as changed (the index is never
modified), so their claims are decided by the anchor. A source reached through a symlink, or
through a symlinked directory, stays inside the project by its resolved target: secret-pattern
and ignore checks apply to both the cited path and the target, the commit/blob shortcuts are
skipped (Git records the link, not the target's content), and the anchor is read through the
link. A concurrent swap of the link is not guarded. User-sourced intent remains separate
from code-backed observations. Fresh anchors establish source continuity, not semantic truth.
Applicability compares the production ref with the index and working tree, including
untracked paths and the in-project targets of anchored symlinks; detached HEAD is evaluated by
its actual content, not assumed production.
An unresolved comparison reports `needs_branch_verification`. The served metadata is part
of the evidence reuse fingerprint.

The reuse identity includes a fingerprint of the promoted knowledge the call was shown (evidence context schema 3), so promoting or correcting knowledge invalidates an identical earlier answer; rows written under schema 2 miss once.

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
(applying to any agent, including the `EXOMA_OPENCODE_AGENT` override) and in
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
exoma --command clean --work-dir /path/to/target-app --pretty
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
      │  ├─ gate-blocks.jsonl   # execution-gate refusals, appended by the hook
      │  ├─ gate-skips.jsonl    # pre-flight escapes used while armed, once per marker
      │  └─ lock
      ├─ logs/<prompt_id>/
      │  ├─ prompt.md
      │  ├─ prompt.sha256
      │  ├─ output.raw.md
      │  └─ call.meta.json
      └─ reports/sweep.last.md
```

`<data dir>` is the per-user data directory: `$EXOMA_HOME` when set, else
`%LOCALAPPDATA%\exoma` (Windows), `~/Library/Application Support/exoma` (macOS), or
`$XDG_DATA_HOME/exoma` / `~/.local/share/exoma` (Linux). It holds `storage/` (jobs, main-session
cache) and the `config/second_agent.seed*.json` seeds; never anything per project. Jobs an agent-workflow
checkout recorded in its own `storage/` stay there: they belong to that runtime. Every delegated call writes this directory from the host process, so a host sandbox limited to the workspace must be granted its `storage/` ("Codex host on a `workspace-write` sandbox" above).

Mutable state (`state`/`scope`/`cache`/`runtime`/`logs`) lives under `data/sessions/<id>/`, so two main_agents on the same project never overwrite each other. The `.workflow/` root holds only human-edited files plus scripts.

The v3.5.x workspace layout (layout 1) stored the contents of `data/` directly in the `.workflow/` root; the runtime still reads it until `upgrade` moves it. Post-move steps (rewrite evidence paths, delete old leftovers, strip config, convert `secrets.json`, prune backups) are recorded in `data/.migration-pending.json`; if any of them fails, the next `upgrade` resumes from that step and `doctor` reports it as `migration incomplete`. A leftover `data.migrating/` whose names collide with the root is refused without moving anything.

Async jobs are stored per user under `<data dir>/storage/jobs/`. The default session ID cache is
in `<data dir>/storage/main-sessions/` and is separated by a hash of the project root; the mapping to OpenCode
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

The worker log path stays `logs/<job_id>.log`; each spawn appends a timestamped separator
and its output, preserving earlier recovery segments. Pruning removes it with the job.
This is diagnostic output, not an append-only lifecycle journal or an ownership fence.

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

### Liveness worker (v1.1.0)

A live PID does **not** mean it is working. The worker therefore reports a heartbeat together with the age of the output stream, and the job is classified into three states:

| State | Meaning | Action |
| --- | --- | --- |
| `alive-progressing` | PID alive, heartbeat fresh, stream has not crossed the idle limit | wait |
| `alive-stalled` | stream idle > `idle_stall_seconds` or heartbeat stale > `stall_threshold_seconds` | probe a fresh session |
| `dead` | PID gone | reap → `worker_died` |

When `alive-stalled`, the runtime sends a PING to a **new OpenCode session**, not the session suspected of hanging. The first probe runs as soon as the stalled status is seen, then repeats per `probe_recheck_seconds` while the job is still stalled. The current source default is `120` seconds, not one minute.

The normal `await` path reads `stall_threshold_seconds`, `idle_stall_seconds`, and `probe_recheck_seconds` from the project config. The `exoma check --wait` attach path currently uses the tool defaults for all three and a fixed probe cadence of 120 seconds; project tuning is not yet passed to the attach process.

- probe answers → store `stalled_no_progress`, keep waiting;
- provider refuses because of quota/rate limit → terminate the process tree, reap as `rate_limited`;
- probe stream cut off → terminate/reap as `streaming_failed`;
- probe fails for another reason → terminate/reap as `second_agent_unavailable`;
- PID gone at any time → reap as `worker_died`.

Termination always goes through the process-generation check: a reused, unverifiable or
current-process PID is never killed. When the job record cannot be locked or read during a
reap, the record read just before the reap decision is used for that check only if it is the
same job and still active; nothing is written, the session lock is left alone, and `meta`
carries `job_record_error` and `job_record_written: false`. Without such a record the kill is
skipped with `kill.reason: no_trusted_worker_record`. The call that hit the failure returns this
result and stops waiting; nothing retries on its own. The job record still reads as active, so a
later `check` or await on the same job reaps again against the state it can then read.
A worker that raises records the same typed `worker_crashed` result in its job record that it
returns, so a later attach reads `error_type` and `next_action`, not a bare message.

The JobManager hard ceiling remains as a safety net: default 5400 seconds, changeable via the env `EXOMA_JOB_MAX_RUNTIME_SECONDS`. A job that exceeds it fails as `job_expired` even if the PID appears alive. A project-local key with the same name does not yet change this value.

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

Use `python tools/e2e/e2e.py --full --model <id>` if you want to add a real provider smoke test; that mode is opt-in because it uses quota.

Additional checks:

```bash
exoma --help
exoma --command doctor --work-dir . --pretty
```

---

## CI

Three GitHub Actions workflows run the gates that someone would otherwise have to remember
to run themselves.

| File | Trigger |
|---|---|
| `.github/workflows/ci.yml` | push to `main`/`dev`, pull request, manual, `workflow_call` (from `publish.yml`) |
| `.github/workflows/publish.yml` | push of a `v*` tag |
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

The full checks matrix uses Python `3.11` and `3.13` via `actions/setup-python` on
`ubuntu-latest`, `windows-latest`, and `macos-latest`, with `fail-fast: false`. Hooks and
generated runners use Bash on every OS (Git Bash on Windows, checked in the job's first
step). Each platform runs the default source suite and local E2E checks; process identity,
locks and path handling are exercised on the native OS. A separate POSIX-permissions job
keeps the atomic-mode and Codex auto-review checks on Linux/macOS with Python `3.13`.

`on: push` (`main`/`dev`) + `pull_request` + `workflow_dispatch`, with
`concurrency: cancel-in-progress` so that a pipeline overtaken on the same ref is cancelled
instead of being left to finish against a stale commit. Job permissions are locked to `contents: read`.

The `dist` matrix uses Python `3.11` (the package floor) and `3.13` on the same three OSes. It builds the sdist and wheel,
installs the wheel into a clean venv with home variables and cwd pointed at temporary
directories, and runs `exoma --version`, `exoma install claude --no-input`, `exoma init`,
and the generated `inspect.sh`. A second clean venv installs the sdist and checks import,
bundle presence and version. Only Ubuntu with Python `3.13` uploads `dist/` as the publish artifact.
These smoke checks do not run the full source suite against installed artifacts or invoke
real provider CLIs. `ci.yml` also accepts `workflow_call`.

`.github/workflows/publish.yml` runs on a pushed `v*` tag: it calls `ci.yml`, refuses a tag
whose name does not match the built wheel's version, and uploads that same artifact to PyPI
through Trusted Publishing (`id-token: write`, environment `pypi`, no stored token).

What CI does **not** do: bump or tag. The steps that remain human-owned are in `RELEASE.md`.

---

<a id="batasan-yang-diketahui"></a>
## Known limitations

Moved to [`docs/limitations.md`](limitations.md). This heading is kept so that old
links to `#batasan-yang-diketahui` keep working.

---

## Codex Auto-review context

`exoma install codex` automatically installs an owned additive block in the Codex host's
`auto_review.extra_policy`, together with the contract, skills, instructions and hooks.
Selecting Codex in the terminal menu, `all`, the legacy `--host codex`, or a stored host
choice uses the same rule. There is no separate context prompt or required opt-in flag,
including on a fresh noninteractive install or an update with `--no-input`.

`--no-codex-auto-review-context` removes the owned block and saves an explicit opt-out,
including across updates and host deselection/reselection. The existing positive flag
`--codex-auto-review-context` clears that opt-out. Selecting only Codex as the project's
second-agent provider does not enable Codex host context.

Migration: old install state recorded only the effective `codex_auto_review_context`
boolean, so a default `false` cannot be distinguished from a deliberate refusal. Missing,
`true` and `false` legacy values all adopt the automatic default when Codex is a chosen
host. To keep the context disabled, use `--no-codex-auto-review-context` once after updating;
new state records this separately as `codex_auto_review_context_opt_out`. Installer,
`--check` and doctor resolve this intent with the same rule; missing context in a legacy
Codex install is reported as missing until the next install repairs it.

The block describes authorized Exoma source inspection and evidence gathering. It does not
change the reviewer, sandbox, user replacement policy, trust, authentication or managed
requirements. Global installation state does not establish the effective policy of an
already running chat: profile/project/managed settings may override it. Reload Codex after
installation and inspect its active settings when determining which policy is in use.

Automatic merge requires a tested Codex CLI
baseline listed in [`installer/codex_policy.py`](../src/exoma/installer/codex_policy.py).
Other CLI versions are reported as `support-unverified`, not assumed
unsupported upstream or silently accepted. An effective `CODEX_HOME` different from the
host directory managed by the installer is likewise reported without writing another file.
These guards apply to automatic installs as well: a refused config transaction exits
nonzero and reports an incomplete integration. Review and trust the installed hooks with
`/hooks`; installing their files alone does not activate the host gates.

Grant and reviewer context share one atomic config write, original backup and final-hash
receipt. Reinstall replaces only an unedited owned block. User policy text and settings
remain; hand-edited ownership, invalid TOML and unsupported layouts are preserved and
reported. `--check` distinguishes installed/disabled, missing/outdated, conflict and
support-unverified. Uninstall removes only the owned block; rollback refuses a config
edited after installation. Unmarked policy text added manually stays user-owned.
Doctor classifies the block with the same read-only classifier, not by the config file's
existence: installed/disabled are clean; missing is listed under `missing`; outdated, a block
left while the context is off (`removed`), conflict, support-unverified, an unreadable template
or a config that cannot be opened (`unreadable`) are drift with that reason. The config is
decoded as the installer reads it (UTF-8, then cp1252, then Latin-1, which maps every byte), so
a decoding problem is never `unreadable`. Where the classifier reaches TOML parsing, bytes that
are not valid TOML read as `conflict`; with the context off and no owned markers it does not
parse and reports `disabled`, and an unverified CLI or `CODEX_HOME` reports `support-unverified`
first. The template is part of the package hash
check of both `--check` and a writing install.

## References

- Release notes: [`CHANGELOG.md`](../CHANGELOG.md)
- Canonical main_agent contract: [`src/exoma/_bundle/config/hosts/claude/CLAUDE.md`](../src/exoma/_bundle/config/hosts/claude/CLAUDE.md)
- Canonical second_agent contract: [`src/exoma/_bundle/config/providers/opencode/AGENTS.md`](../src/exoma/_bundle/config/providers/opencode/AGENTS.md)
- Runtime entry point: [`src/exoma/cli.py`](../src/exoma/cli.py)
- Architecture (verified diagrams): [`docs/architecture/`](architecture/README.md)
- Limitations: [`docs/limitations.md`](limitations.md)
- Metrics registry: [`docs/evaluation/metrics.md`](evaluation/metrics.md)
