# Development & Research Contract

This contract defines how significant changes to `agent-workflow` must be documented.

The goal is to preserve the reasoning and evidence behind project evolution without
requiring private conversation logs or confidential project data.

## 1. Scope

A research record is required for changes that affect:

- runtime architecture,
- agent orchestration,
- routing,
- provider behavior,
- evidence handling,
- facts or knowledge systems,
- graph behavior,
- verification,
- safety or policy,
- prompt contracts,
- task/context management,
- recovery behavior,
- performance characteristics,
- or other behavior that changes the project's core design.

Trivial changes such as formatting, typo fixes, documentation wording, or isolated
maintenance do not require a full research record.

## 2. Minimum record

Every significant change must have at least one of `DEC-XXX`, `H-XXX`, `EXP-XXX`, or
`CASE-XXX`, written in the format of §16.

The record must explain the problem, the decision or hypothesis, the reasoning, the
evidence, the implementation reference, the validation status, and the limitations.

## 3. Literature

When literature materially informs a change, record the source in
[`literature/`](literature/).

Do not claim that a paper caused an implementation unless that relationship is known.
Multiple papers may be represented through a [synthesis](synthesis/) record.

## 4. AI-assisted discussion

AI discussion may contribute to exploration, synthesis, hypothesis generation, and
implementation reasoning.

Private transcripts must not be committed to the repository when they contain confidential
information. AI-generated claims must not be treated as validated evidence by default.

## 5. Confidential information

Never commit private conversations, confidential source code, credentials, internal URLs,
customer information, business-sensitive metrics, proprietary architecture details, or
identifiable company information. Use sanitized abstractions instead.

## 6. Provenance

Every significant design claim should declare its provenance where known:
`direct`, `adapted`, `synthesized`, `observed`, or `unknown`.

`unknown` is an acceptable historical state.

## 7. Validation

Implementation does not equal validation. Each record distinguishes `implemented`,
`observed`, and `validated`. A feature may be implemented while the hypothesis behind it
remains unvalidated.

## 8. Negative results

Failures, regressions, unexpected behavior, rejected hypotheses, and failed approaches
should be documented when they provide useful engineering or research information. Do not
document only successful outcomes.

## 9. Knowledge promotion

Research observations must not automatically become operational knowledge.

Operational knowledge lives in `docs/project-knowledge/`, is written only through the
`promote-validate` → `promote-verify` → `promote-write` stages, and is injected into
exploration and reasoning prompts by the runtime. Promote a claim there only when it is sufficiently
supported and useful for future runtime behavior, and keep a reference to the originating
research record whenever possible.

## 10. Historical reconstruction

Historical records may be reconstructed from source code, git history, release history
(`CHANGELOG.md` and `docs/releases/`, `prompt/` up to v3.7.2), existing documentation, and
remembered observations.

Reconstructed history must be marked as reconstructed when certainty is limited. Missing
provenance must not be fabricated.

## 11. Default development flow

For significant changes the default sequence is: problem, research or literature (when
relevant), synthesis (when multiple sources are involved), hypothesis or decision,
implementation, validation, observation or experiment result, and finally an update to the
record. Not every change requires every stage.

## 12. New mechanisms

The runtime already has many subsystems, and infrastructure can grow faster than
capability. A harness that becomes too complex becomes the engineering problem itself.

Before adding a subsystem, mechanism, or record type, the change record answers:

1. What problem does this solve?
2. What measurable benefit is expected?
3. What complexity does it add?
4. Can an existing mechanism solve the same problem?

"Build first, justify later" is not accepted for significant changes.

The research charter ([CHARTER.md](CHARTER.md) §8 and §12) adds the research side of the same
test: which research question and measurable variable the mechanism serves, which experiment
it enables, and why an existing mechanism cannot. A mechanism that answers none of them is out
of research scope, even when it improves the product.

## 13. Completion rule

A significant feature is not considered fully documented until a future maintainer can
answer:

1. Why was this introduced?
2. What informed the decision?
3. What was actually implemented?
4. What evidence supports it?
5. What are its limitations?
6. Under what conditions should it be reconsidered?

## 14. Disposition and validation authority

Every record carries a `disposition` block in its metadata, next to its type-specific
`status` or `outcome`. The two answer different questions: `status` says where the record is
in its own life cycle (a hypothesis is `proposed`, an experiment `planned`); `disposition`
says what exists and what has been shown, using the distinction in §7.

```yaml
disposition:
  implementation_status:  # not_implemented | partially_implemented | implemented | not_applicable
  validation_status:      # not_validated | observed | partially_validated | validated | rejected
  validated_by:
  validated_on:
```

- `observed` means there is real-use evidence at some tier in
  [methodology.md](methodology.md) "Evidence tiers", but no designed evaluation. In plain
  words: used for real without a reported problem; a re-countable source may exist, but the
  record has not been evaluated against the conditions below. It is not a weaker word for
  `validated`; it says what kind of evidence exists. A DEC used that way can become
  `validated` without a measured figure, because a clean run (next items) counts as the
  measurement, but only when all four conditions of the `validated` item below hold and the
  maintainer sets `validated_by` and `validated_on`.
- `validated`, `partially_validated`, and `rejected` require an evaluation the record names
  (usually an `EXP-XXX`) and a result measured against the record's stated success criteria.
- Exception for decisions (`DEC-XXX`) only: a DEC is evaluated on the maintainer's direct
  use of the decided behavior instead of an `EXP`. A run counts as clean when all three
  conditions hold; a clean run is required for `partially_validated` and `rejected`, and is one
  of the two measurements a `validated` DEC may name (next item):
  1. The runtime verdict is `pass`, or the equivalent success outcome for that behavior.
  2. The run needed no manual workaround: no `ignore_repeat_brake`, no hand-edited request
     or scenario, and no runtime patch mid-run.
  3. The maintainer reported no issue with that run.

  The record's Validation section names the run: its date, its job id, and a sanitised
  description of what was exercised. Any condition missing makes the run not clean. This is
  `creator_observation` evidence (see methodology.md "Evidence
  tiers"). It shows the decision works in the maintainer's use, not that it generalises.
  Hypotheses (`H-XXX`) still require an `EXP-XXX`.
- Since schema version 3 (DEC-019) a `validated` DEC needs four things in its `validation`
  block, all of them; a clean run alone is no longer enough, and a measurement can stand in
  for it:
  1. `uses` of 2 or more: the decided behavior was used repeatedly, not once.
  2. `consistent: true`: it behaved the same way each time, with no use pointing the other
     way.
  3. `maintainer_decision`: the maintainer's explicit decision, a sanitized summary.
  4. A measurement: a clean run as above, or a `measurements` entry with a named,
     re-countable source and its figure.

  Repeated use and the maintainer's word are the creator's evidence; the measurement is what
  that evidence cannot supply by itself. `partially_validated` and `rejected` keep the
  clean-run rule above.
- Only the maintainer sets `validated_by` and `validated_on`. Setting any value other than
  `not_validated` or `observed` without them is incomplete.
- Every hypothesis states its success criteria: the primary metric, what counts as
  `supported` and as `rejected`, and whether the data is recorded today. A threshold not
  chosen yet is written `[PLACEHOLDER]`, never invented.

The research corpus counts as final when every record has a complete disposition, every
hypothesis has success criteria, every `answered` research question lists at least one
validated record and the maintainer's decision, and every version-2-or-later record names
the questions it serves (§16). Final does not mean answered: a record may be final and still
`not_validated`.

A research question is `answered` or `unanswered`, never half-answered: a partial answer is
an unfinished one, and calling it answered invites the bias the status exists to prevent.
What already bears on an unanswered question is written in its `what_exists` and `records`,
not in its status. An `answered` question lists at least one `validated` record and carries
a `decision` block: the date, the method (`critical_interview`), and a sanitized summary of
the maintainer's decision, never a transcript (§4, §5).

## 15. Tracked records and drafts

A record is tracked in `docs/research/` only when it describes something that was carried
out and has a recorded result: an observation with its source stated, a decision
implemented in code, an experiment that ran. Figures in a tracked record come from a named
source that can be re-counted (for example a named telemetry file and line range); a figure
without one is removed, not rounded.

A figure that a registered metric produces names it as `<id>@<version>`
([`docs/evaluation/metrics.md`](../evaluation/metrics.md), source `core/audit/metrics.py`,
DEC-026) in the record's evidence source, together with the producer's `provenance` stamp:
the tool commit, the parameters it ran with, and the input hash. A changed method is a new
metric version, never a silent redefinition; two figures with different IDs are not compared
as the same quantity. The inputs of real-use figures are private, so "re-countable" means on
the maintainer's machine: a record presenting such a figure states that, and does not imply
that a reader can reproduce it. Records frozen before DEC-026 keep their figures as written.

Proposed hypotheses, planned experiments, syntheses, and decisions without implementation
are drafts. Drafts live in `docs/research-drafts/` with the same subfolders. That directory
is git-ignored: it is the maintainer's local working area, not part of a release. Tracked
records refer to a draft by its ID in plain text, never by a link or an ID field. A draft
returns to `docs/research/` when it has been carried out and its result is recorded in it.

A research question (`RQ`) is tracked as soon as it is asked: a question is never carried
out, so it has nothing to wait for. Its `records` name tracked records only, and the drafts
serving it are named in its `notes`, so a clean clone without drafts validates the same way.

Tracked records are frozen as of 3.7.3: their content changes only to add a new result or
to correct a factual error against its source, never to add untested reasoning. The one
exception is the format conversion of §16 (DEC-011), which changed their encoding, not
their content, and filled the commits they named as `unknown` from Git history. Schema
version 2 (DEC-017) did not touch them: a tracked record at version 1 stays valid as it is.
Schema version 3 (DEC-019) froze the version-2 records the same way. The four decisions then
`validated` received the version-3 predicates as a new result from the maintainer's
re-evaluation, which is the kind of change this section allows.

## 16. Record format

Every record is a YAML file, `<ID>-<kebab-slug>.yaml`, in its type's directory, following
[`schema.yaml`](schema.yaml) and started from that directory's `_TEMPLATE.yaml`. The schema
fixes the fields per type and which of them are enums; unknown keys are refused, so a field
cannot be invented in one record. `tools/maintain/check_research.py` enforces it in the test
suite and in CI, together with the rules the schema cannot express:

- IDs are unique across tracked records and drafts, and match the file name; the prefix
  matches the directory. IDs are `PREFIX-NNN`; a research question is `RQ-NN` with an
  optional lowercase letter for a sub-question (`RQ-07a`).
- Records are `schema_version` 3. A tracked `DEC`, `H`, `EXP`, `CASE`, `SYN`, `ARC` or `LIT`
  may stay at version 1 or 2 only if it was frozen before the next version (§15); the
  checker holds those closed lists (`FROZEN_V1`, `FROZEN_V2` in
  `tools/maintain/check_research.py`), so a new record written at an old version is refused.
  Drafts and `RQ` records are always the current version.
- Every version-2-or-later `DEC`, `H`, `EXP`, `CASE` and `SYN` names the research
  question(s) it serves under `questions`: a record that serves no question has no reason to
  change the project. The links hold both ways: an `RQ`'s `records` are tracked records, each
  of which (from version 2) names the `RQ` back, and a tracked record's `RQ` lists it. An
  `answered` `RQ` lists at least one `validated` record and carries a `decision` (§14); an
  `unanswered` one may list none yet. Version-1 records carry no `questions` and are linked
  from the `RQ` side only.
- An implemented or partially implemented `DEC` names the commit(s) that carry it
  (`implementation.commits`). `uncommitted` is allowed while the change is in the working
  tree and is refused by `--strict`, which a release runs; replace it with the hash once the
  commit exists.
- `H` and `EXP` records are external to the code: they carry no commit anywhere. An `EXP`
  pins `version_under_test` instead and names its `hypothesis`, and that `H` lists the `EXP`
  back under `experiments`; every `EXP` an `H` lists names that `H`. Benchmark method —
  arms, metrics, results — is written as an `EXP`.
- `validation_status` beyond `observed` needs `validated_by` and `validated_on` (§14) and an
  evaluation: an `EXP` reference, a completed `EXP` with results, or for a `DEC` a
  `validation.runs` entry with `clean: true`. A `validated` `DEC` needs the four predicates
  of §14 instead.
- A tracked record meets its type's `tracked_when` in the schema (§15); anything else is a
  draft.
- Every referenced ID resolves to a tracked record, or to a draft when
  `docs/research-drafts/` exists.

Drafts in `docs/research-drafts/` follow the same format; `--drafts` validates them without
the tracked gate and refuses a Markdown draft. Drafts written before 3.7.3's conversion are
Markdown until the maintainer converts them, which `--drafts` reports one by one. A draft in
a nested directory such as `_archive/` is not read. CI has no drafts directory, so `--drafts`
is a local check the maintainer runs; CI checks the tracked corpus.

The README inventory and [questions.md](questions.md) are generated from the records
(`--write-inventory`), and the check fails when either is stale; `RQ` records appear only in
questions.md. Prose goes in block scalars (`|`); a list item that starts with a backtick
is quoted. The schema is part of this contract: changing a field or an enum bumps
`schema_version` and is recorded as a `DEC`. PyYAML is a development dependency
(`requirements-dev.txt`); the runtime never reads these records.
