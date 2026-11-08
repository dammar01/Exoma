# Future work and backlog

Updated: 2026-11-06.

This page records deferred work agreed during the Exoma audit review. These are backlog
items, without a release commitment or an approved implementation plan. Recheck the
current code and define acceptance criteria before scheduling an item.

## Near-term focus

The current implementation scope focuses on the observed behavior gaps discussed as
**package 1**: knowledge freshness and applicability (including detached HEAD and
uncommitted changes), Agy detection of further edits to already-dirty files, visibility
when the Claude execution gate cannot enforce its checks, and preservation of worker logs
across recovery attempts. It also covers policy-aware Codex runner guidance: distinguish
host approval denial before process creation from shell, network, and job failures, and
respect existing user authorization and host permissions. Relevant regression tests belong
with those changes. Implementation and verification status are reported by the workflow;
this roadmap is not a completion record.

The deferred packages below can be considered independently of that scope. Preserving a
worker log does not require a lifecycle journal; adding regression tests does not require
the broader provider test matrix.

## Compatibility and provider contracts (package 2)

**Status: deferred backlog.** Priority and release target will be decided when scheduled.

### B2-1: Version job and session records

- **Reason:** job and session records do not currently carry explicit schema versions.
  Runtime layout migrations and configuration compatibility do not define compatibility
  for each persisted record type. No upgrade failure was reproduced in the audit.
- **Scope:** define record versions, handling of legacy records, supported migrations,
  and behavior for unsupported or malformed versions. Keep existing user data readable
  within the documented compatibility window.
- **Revisit when:** a job or session schema needs to change, or an upgrade exposes a
  concrete compatibility issue.
- **Acceptance:** fixtures cover legacy and current records; unsupported versions are
  reported explicitly; migration and interrupted-write behavior are documented and tested.

### B2-2: Extend provider contract testing

- **Reason:** internal tests and declared provider boundaries do not establish every
  behavior of an installed provider CLI. Build on the existing adapter tests and probes.
- **Scope:** CLI compatibility, session capture/resume, cancellation, write boundaries,
  error normalization, child-process shutdown, and recovery. Separate deterministic local
  tests from live checks that consume provider quota.
- **Revisit when:** a provider integration or supported CLI version changes, or release
  validation needs stronger evidence of an advertised contract.
- **Acceptance:** each supported provider has an explicit coverage matrix distinguishing
  tested, unsupported, and unverified behavior. Quota-consuming checks run in a controlled
  release or scheduled gate with the tested versions recorded.

## Architecture and evidence quality (package 3)

**Status: deferred backlog. Priority: low.** Each item needs its own justification and
bounded plan; the package is not a single implementation task.

### B3-1: Make attempt ownership explicit

- **Reason:** completion currently identifies the job without requiring an attempt
  identity. Worker liveness checks, recovery claims, terminal-state handling, and mutation
  locks already provide protection. A stale worker overwriting an active result was not
  reproduced; this proposal strengthens the ownership contract.
- **Scope:** assign attempt identities and validate ownership at result publication,
  failure, heartbeat, and ownership-sensitive release operations. Define idempotent handling
  of repeated publications and the relationship to provider sessions, including resume.
- **Revisit when:** recovery or concurrency expands, or a concrete ownership failure is
  demonstrated.
- **Acceptance:** an old attempt cannot mutate the active attempt or release its lock;
  duplicate publication has a defined outcome; existing recovery behavior remains covered.

### B3-2: Preserve lifecycle history in a journal

- **Reason:** current job state does not provide a complete history of transitions.
  A journal could improve diagnosis of recovery, cancellation, and interrupted execution.
  It does not itself enforce attempt ownership.
- **Scope:** append lifecycle events with job/attempt identities and timestamps, and
  define how current state relates to those events. Decide the authoritative record,
  crash recovery, consistency, retention, and redaction rules before implementation.
- **Revisit when:** per-attempt logs and existing artifacts are insufficient to explain
  operational failures.
- **Acceptance:** a crash between event and state writes has a defined recovery path;
  history remains attributable to its attempt; retained events respect redaction rules.

### B3-3: Track evidence provenance and derivation

- **Reason:** recurrence counts distinct sessions, not independent observations.
  Normalized-text filtering of injected claims and existing deduplication do not provide
  complete derivation tracking. Durable facts also have an ingest path without recurrence.
- **Scope:** record observation identity, source snapshot/anchors, observation type,
  originating session, and links to injected or derived evidence. Distinguish human
  confirmation of design intent, observed behavior, and implementation claims. Evaluate
  both recurring and durable-fact paths.
- **Revisit when:** repeated memory or misleading support counts cause a measured
  knowledge-quality problem, or provenance is needed for review.
- **Acceptance:** repeated or derived evidence can be traced to its origin; existing
  anchor validation and deduplication remain intact; file/model/session counts are not
  presented as proof of independence. Evaluate provenance data before introducing an
  automatic independence or confidence score.

### B3-4: Reduce core dependence on CLI initialization

- **Reason:** core job lifecycle code accesses CLI-owned services through `_main()`.
  This complicates service ownership, import-time initialization, and isolated testing;
  coupling alone does not establish a runtime defect.
- **Scope:** give services an explicit construction point and pass dependencies into core
  code, with the CLI consuming those services. Refactor one bounded path at a time while
  preserving behavior, including sanitization performed by adapter wrappers.
- **Revisit when:** this coupling obstructs a specific change or meaningful test.
- **Acceptance:** the chosen core path can run and be tested without importing the CLI
  for its service instances; external behavior remains covered by regression checks.

## Scheduling an item

Before moving an item into implementation, verify the trigger still applies, trace its
consumers, specify compatibility and failure behavior, and record a bounded plan with
acceptance checks. Backlog priority can change when new evidence warrants it.

Runtime changes belong in this repository. Research and staging remain in agent-workflow;
see [Developing Exoma](development.md). This page describes future proposals, while the
[runtime contracts](runtime-contracts.md) and [architecture](architecture/README.md)
describe current behavior.
