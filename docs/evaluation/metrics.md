# Metrics

The calculation contract for every benchmark figure this project reports. The registry in
`core/audit/metrics.py` is the source; this page renders it, and a test keeps the two in step.

- **Cite a figure as `<id>@<version>`**, for example `outcome.solved@1`. A change to how a metric
  is computed bumps its version; the old figure keeps meaning what it meant.
- **Same word, different ID.** "Task" is three different things here (`runtime.accepted_task`,
  `runtime.workflow_task`, `outcome.coding_task`), and so is "saved" (`runtime.context_avoided`,
  `outcome.context_saved`, and the status line's own figure). They are not interchangeable.
- **Each producer stamps its output** with a `provenance` block: registry version, the metric
  versions it computed, the tool's commit (`+dirty` when uncommitted; `null` when the install
  is not the top of its own work tree or `git status` fails, since a failed status cannot
  prove a clean tree), its parameters, and a hash over its inputs (contents only, no paths).
- **Recount:** on the machine holding the inputs only: inputs are private transcripts, usage streams and git history; the method is public, the data is not.
- **Measuring your own use.** Any user can run the offline tools over their own projects; the
  figures are theirs. `--export <file>` writes a copy that may be shared (aggregates and the
  provenance stamp). It is checked against an allowlist: values are numbers, booleans, null
  or identifier-shaped strings (`[A-Za-z0-9_.:+-]`, at most 64), keys likewise plus single
  spaces, and the fixed `recount`/`producer` texts only where the stamp writes them; anything
  else is refused naming its JSON path, never its value. A contributing project's name found
  as whole words in a value is refused too, at any length (`ui` as well as `shop-api`; `ui`
  does not match `build`). Under `per_project` and `by_project` every key must be a project
  label the tools write (`PA`, `Project AB`), so a folder name there is refused by shape
  (DEC-038). How to run them:
  [Measure your own use](../team-guide/measure-your-use.md).
- The statistics differ between producers on purpose: unifying them would change figures
  already recorded (CASE-011, CASE-012). Each metric names its convention below.

Registry version: **1**.

## Running the tools

Both read only; both print one JSON object. Run them from the clone.

```bash
# Real use of one version over every project under a folder (real_use.*)
python tools/maintain/measure_real_use.py --projects-root <folder> --version 3.8.0

# Coding outcomes: what reached main, fixes within 24 h, context saved (outcome.*)
python tools/maintain/measure_task_outcomes.py --projects-root <folder> --labels <labels.json> --version 3.8.0

# The same, also written to a file that may be shared (refused if it would carry a path or a project name)
python tools/maintain/measure_real_use.py --projects-root <folder> --version 3.8.0 --export real-use.json
```

`--transcripts` points at another transcript folder (default `~/.claude/projects`);
`--exclude <dir>` leaves a project out (default: the clone itself, `agent-workflow`). The
labels file maps a transcript file name to its category: `{"<session>.jsonl": "feature"}`.
`--export` cannot be combined with `--show-names` or, for real use, `--sample-rebuttals`.

## Statistic conventions

| Name | Method |
| --- | --- |
| `median_interpolated` | statistics-style median (mean of the two middle values), rounded to 2 decimals |
| `nearest_rank` | sorted values, index round(p * (n - 1)) with Python's half-to-even round; rounded to 1 decimal |
| `median_stdlib` | statistics.median, rounded to 1 decimal |
| `p90_floor` | sorted values, index floor(0.9 * (n - 1)) |
| `count` | a count; no statistic |
| `rate` | numerator / denominator, rounded to 3 decimals; None when the denominator is 0 |
| `sum` | a sum over the unit |

## Metrics

### Runtime report (`--command report`)

| ID | Unit | Statistic | Definition | Denominator | Missing data |
| --- | --- | --- | --- | --- | --- |
| `runtime.command@1` | command | `count` | A delegated command: usage rows grouped by prompt_id (a continuation is one); a row without prompt_id is its own command. Report key `calls`/`commands`. | - | Rows of a provider-less browser run (no prompt_id, provider or tokens; CASE-014) are dropped before counting. |
| `runtime.provider_call@1` | provider invocation | `count` | One usage row. Report key `provider_calls`. | - | As runtime.command. |
| `runtime.cost_tokens@1` | tokens | `sum` | Input plus output per row: the provider's actual count when present, else characters // 4. Cached input and reasoning are inside these counts, never added. Report key `cost`. | - | `token_source` says which rows were estimated. |
| `runtime.accepted_task@1` | correlation_id | `count` | A correlation id (plan chain or derived task id) with at least one verify row whose derived verdict is pass. Report key `accepted_tasks`. | correlation ids with at least one judged verify | Never-verified ids are outside the denominator, not counted as failures. |
| `runtime.first_pass_correctness@1` | correlation_id | `rate` | Share of judged correlation ids whose first judged verify passed. | correlation ids with at least one judged verify | As runtime.accepted_task. |
| `runtime.rework@1` | correlation_id | `rate` | A judged correlation id whose judged verify rounds, ordered by recorded_at, hold a round that did not pass (fail or incomplete) followed by another round. extra_verification_rounds counts the rounds after that first failure. All-pass rounds (a plan verified phase by phase) are not rework (DEC-051). Report key `rework`. | correlation ids with at least one judged verify | As runtime.accepted_task. |
| `runtime.time_to_completion@1` | seconds per command | `median_interpolated` | Summed provider duration_seconds of one command's rows. Browser time (browser_seconds) and runtime test time (tests_seconds) are not included. | commands with at least one measured duration | Unmeasured commands are counted apart, never averaged in. |
| `runtime.evidence_reuse_rate@1` | command | `rate` | Exploration and reasoning commands served from a stored artifact. verify-browser drafts are not eligible. | exploration and reasoning commands | Rows without a role are named, not placed. |
| `runtime.fact_lifecycle@1` | command | `sum` | Facts offered (candidates), served (injected), dropped as stale or below the relevance floor, and added or refused at ingest, summed per exploration/reasoning command. Report key `facts`. | commands whose final row carries fact counts | Commands recorded before the fact fields are counted as unrecorded_commands, never as zeros. |
| `runtime.context_avoided@1` | tokens | `sum` | (answer characters - digest characters) // 4 on the row carrying the digest. Report key `premium_context_avoided`. | - | Rows without a digest contribute nothing. |
| `runtime.workflow_task@1` | event run in one MAIN_SESSION_ID | `count` | Events from a session's first event to a verify whose derived verdict is pass after an edit (DEC-020). States completed, open, read_only, unknown. | - | Events without a session are `unknown`; events before DEC-020 carry no derived verdict and stay open. |

### Real use (`tools/maintain/measure_real_use.py`)

| ID | Unit | Statistic | Definition | Denominator | Missing data |
| --- | --- | --- | --- | --- | --- |
| `real_use.session@1` | Claude Code transcript | `count` | One transcript file whose last bundle banner names the requested version, with at least one human prompt. | - | Transcripts without a banner are excluded. |
| `real_use.prompts_per_session@1` | prompt | `nearest_rank` | Human turns, excluding interrupts, compaction summaries, shell echoes and argument-less built-in slash commands. | sessions | - |
| `real_use.rebuttal@1` | follow-up prompt | `count` | A follow-up whose first 800 characters match the CORRECTION pattern (Indonesian and English). Recall measured 0.08 in CASE-011: a lower bound. | follow-up prompts | - |
| `real_use.active_minutes@1` | minutes per session | `nearest_rank` | Sum of gaps between consecutive transcript timestamps no longer than --idle-minutes (default 15). | sessions | - |
| `real_use.delegated_call@2` | prompt answered by a provider | `count` | Distinct prompt_id (else correlation_id) among a session's usage rows that name a provider. A usage row belongs to one session: the earliest-starting transcript that binds its MAIN_SESSION_ID (@1 joined it to every such transcript; identical on all data recorded up to 2026-10-04, where no id was bound twice). Its `delegated_runtime` block is per row, not per command. | - | - |

### Task outcomes (`tools/maintain/measure_task_outcomes.py`)

| ID | Unit | Statistic | Definition | Denominator | Missing data |
| --- | --- | --- | --- | --- | --- |
| `outcome.coding_task@1` | labelled transcript | `count` | A real_use.session labelled with a coding category (feature, bug, performance, migration, refactor) in the labels file. | - | Unlabelled sessions are counted apart (`unlabelled_sessions`). |
| `outcome.solved@1` | coding task | `rate` | The first first-parent state of `main`, from session start to end + --search-days (30), holding at least --solved-share (0.8) of the lines the session left (Edit/Write/MultiEdit lines of 8+ characters it did not itself remove). 0.3 to 0.8 is partial; below is not in main. | coding tasks | A repository without `main` grades every task not_in_main. |
| `outcome.solved_fixed@1` | solved task | `count` | A solved task whose lines changed on main within --fix-hours (24) of landing, in a commit by an author of the commits that brought them in (author window: -5 min before start, +1 min after landing). | - | - |
| `outcome.feature_segment@1` | correlation id within one session | `count` | A coding task's tasks.jsonl events split by the correlation id of the verify that judged them: a verify's prompt_id joined to usage.jsonl (`direct`), through a verify-browser run's provider_prompt_ids in quality.jsonl (`browser`), or, without a prompt_id, the nearest verify usage row of the same session within 120 s (`inferred`); else `unlinked`. Edits join the next verify of their session; edits after the last one join the last segment, or `unverified`. Report keys `segments_per_task_median`, `segment_links`. | - | Sessions before 3.8.0 wrote no task events and have no segments. |
| `outcome.solved_fixed_outside_feature@1` | solved task | `count` | An outcome.solved_fixed@1 task whose fix no continuation explains. A task continues an earlier one of the same project when it starts at most --link-hours (72) after that one ended and edits a file that one left lines in; with several such earlier tasks it is `ambiguous` and continues none. A fix is explained when a continuation of the task started within --fix-hours of landing and edited every file whose lines were lost. No author check: transcripts carry none. | - | - |
| `outcome.continuation_cost@1` | coding task | `median_stdlib` | Delegated calls (distinct prompt_id with a provider) and their input tokens (actual, else estimated) recorded before the session's first edit in tasks.jsonl. Reported for continuations and for tasks that continue nothing. Report keys `ramp_up_*`. | tasks with a recorded edit | Tasks without task events (before 3.8.0) are left out. |
| `outcome.context_final@1` | tokens | `median_stdlib` | input + cache_read + cache_creation of the task's last main-agent message. p90 uses p90_floor. | tasks with usage | Tasks without usage are left out of the statistic. |
| `outcome.context_saved@2` | tokens per task | `median_stdlib` | Second agent's fresh input (input - cached) minus what returned to the main agent (response characters // 4), floored at 0; reasoning is reported apart, not counted. Usage rows belong to the earliest-starting transcript that binds their MAIN_SESSION_ID. p90 uses p90_floor. (@1 computed the same net, joined rows to every binding transcript, and its definition text wrongly added reasoning.) | coding tasks | Rows without provider counts fall back to estimates; such tasks are counted in context_saved_partly_estimated_tasks. |
