# Skill: provider
description: Choose the second_agent (provider → model → effort → model per route) through interactive questions, then apply it. No argument = picker; with arguments = direct, e.g. /.provider codex gpt-6-luna low.
argument-hint: [provider model [effort] [routes]]

## Trigger
/.provider                                  → interactive picker (STEP 1-3)
/.provider <provider> <model> [effort] [routes]  → direct mode

## Direct mode (arguments given)
Example: `/.provider codex gpt-6-luna low`. SKIP STEP 1-2 — no catalog, no questions.
Pass the arguments on as they are (space-separated) as the STEP 3 payload:
    Every OS (Bash tool): bash '<work_dir>/.workflow/run.sh' provider 'codex gpt-6-luna low' '<MAIN_SESSION_ID>'
The runtime splits and validates (known provider, the model's own effort, agy variants built
from the effort). `<routes>` is optional: `same` or `explore=A,plan=B`.
`ok:false` → relay `content` + `meta.next_action`, status REFUSED; do NOT silently fall back to
the picker. The output is still the [PROVIDER] block below.
Direct mode asks nothing, so for codex/agy the acknowledgement IS the result: relay the
provider's boundary line from `meta.warnings` in full, never summarized away.

## Limits
LOCAL. No second_agent, no job, no lock. The runtime writes the config — you NEVER write
`.workflow/second_agent.json` yourself. You only render the choices and pass one string on.

## STEP 1 — Read the catalog
    Every OS (Bash tool): bash '<work_dir>/.workflow/run.sh' provider '' '<MAIN_SESSION_ID>'

The reply carries `meta.providers[]` (`name`, `installed`, `detail`, `models[] {id, efforts, effort_in_id?}`,
`newest`, `models_source`, `boundary_warning` (null when the provider keeps a read boundary)) plus
`meta.current` (the active provider/model/effort, and `routes` = each route's current model),
`meta.selectable_routes` (the routes that may be chosen) and `meta.source`.

`.workflow/run.*` MISSING → /.init first. Do not call `exoma` directly.

## STEP 2 — Ask, four steps in order
Renderer = AskUserQuestion, one question per call; each answer narrows the next options.

1. **Provider** — options from `providers[]`. `installed:false` → still show it, say in the `description`
   that its CLI is not on PATH; do not make it the first option.
   A provider with a `boundary_warning` (codex, agy) → its `description` MUST carry that warning's
   substance: no read boundary (it can read every project file, `.env` included; agy can also
   write) and it receives the full environment — treated as a trusted provider. The only
   enforced read boundary is `opencode`'s. This is a warning, not a refusal of the menu: the
   choice is the user's, confirmed in STEP 2c.
2. **Model** — options from the chosen provider's `models[]` (at most 20, already newest first).
   The FIRST option = `newest`, label ending in " (Recommended)" — the newest model the provider's
   CLI offers now (opencode/agy: the newest in the model family currently in use). Still ask;
   do not write it directly.
   The tool fits only 4 options, so BEFORE this question write a numbered list of up to the first 10
   models of `models[]` (id + efforts, `newest` marked Recommended). Question options =
   `newest` + the next 3; the `question` says that "Other" accepts a NUMBER from the list
   or another id. A numeric answer → map it to the id in that list before STEP 3.
   `models_source: static` → the CLI could not be read, the list is the bundled shortlist; say so in the `description`.
3. **Effort** — options from the `efforts` OF THE MODEL just chosen, not the provider's.
   Values differ per model, so using the provider's list would offer what upstream refuses.
   - `effort_in_id: true` (agy) → the effort is a model variant (`-low/-medium/-high`). This question
     MUST be answered; the runtime builds the variant id and writes no effort flag.
   - `efforts` EMPTY (`[]`) → that model takes no effort at all. SKIP the third question,
     go straight to the fourth with no effort field. Forcing a value is refused by the runtime.
   - A model from "Other" → its `efforts` are unknown; ask for the effort as an open question
     and say its value cannot be validated.
4. **Route** — "Use the same model for every route?" Two options: yes (every route follows the model above)
   and no (choose one by one). In the `description`, name each route's current model from
   `meta.current.routes` when one differs from `default_model` — that is exactly the state
   this question exists for.
   - **yes** → straight to STEP 3 with the routes field `same`.
   - **no** → STEP 2b.

## STEP 2b — Model per route (only when the user answered "no")
One question for EVERY route in `meta.selectable_routes` (explore, plan, analyze, verify,
e2e_spec — five). The tool limit is 4 questions per call, so split into TWO calls: the first
explore/plan/analyze/verify, the second e2e_spec (the `/.verify-browser` spec draft). Each
question's options = the chosen provider's `models[]`, the STEP 2 model marked as the default in its `description`.

Effort is NOT asked per route: the runtime uses one global effort, and the Router already
drops it for a model that takes none.

Hard tool limits: MAX 4 questions per call, 2-4 options per question, `header` MAX 12 characters.
Do not add an "other" option yourself — "Other" is automatic.

## STEP 2c — Confirm a provider without a read boundary (codex, agy)
The chosen provider has a `boundary_warning` → ONE AskUserQuestion before STEP 3, its
`question` quoting that warning: "Use <provider> as the second agent?" with two options:
**Use <provider>** (description: it can read every project file and the environment; agy can
also write — accepted as a trusted provider) and **Pick another** (description: back to
question 1). Pick another → repeat STEP 2 from the provider question; nothing is written.
This question is the whole acknowledgement: there is no variable or config key behind it.
opencode, or any provider whose `boundary_warning` is null → skip this step.

## STEP 3 — Apply
    bash '<work_dir>/.workflow/run.sh' provider '<provider>|<model>|<effort>|<routes>' '<MAIN_SESSION_ID>'

Separator `|` — not `/`, because opencode model ids contain `/`.
An empty model or effort → that field is cleared. For opencode, an empty model is REFUSED at
call time (`model_unset`): without `-m`, opencode uses the model last used on this machine.

The `<routes>` field:
- `same` (or empty) → every route uses `<model>`.
- `explore=A,plan=B,analyze=C,verify=D,e2e_spec=E` → per route. Pairs separated by `,`, assignment by `=`.
  A route not named follows `<model>`. An empty value (`explore=`) → that route is cleared.
- A route name outside explore/plan/analyze/verify/e2e_spec → refused, nothing written.

The runtime writes five fields plus `routes` at once (`provider`, `provider_command`,
`provider_agent`, `default_model`, `effort`, `routes`) and syncs the
`runtime.second_agent` hint in config.json. Routes are written key-wise: per-route `timeout_seconds`
and `agent` already in the file are NOT lost.
`ok:false` → NOTHING is written; relay `content` + `meta.next_action` as they are, do not try to
fix it yourself, do not write the file.

## Output
[PROVIDER]
selected: <provider> / <model | provider default> / effort <effort | provider default>
routes: <meta.written.routes — each route + its model; say "all follow the default" when
  meta.routes_uniform is true>
installed: <installed providers>
written: <the six fields from meta.written>
config_hint: <updated true|false + reason>
foreign_values: <meta.foreign_values, or "empty">  — NOT empty = the config names another provider
  in `provider_command`/`provider_agent`/model; show it in full, never summarize it away.
warnings: <meta.warnings, or "none">
read_boundary: <opencode → "enforced (opencode.json)" | codex/agy → "NONE — the second_agent can read every file including `.env` and receives the full environment (trusted provider)">
status: APPLIED | REFUSED

REFUSED → name the reason + next_action; do not offer to continue with another command.
APPLIED → takes effect from the next delegated call; a job already running does not change.
