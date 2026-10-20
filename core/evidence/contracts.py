"""The five workflow contracts, as data rather than as convention.

Additive on purpose. `core/evidence/contract.py` keeps producing the plain `{ok, content, meta}`
dicts every adapter, call site, and test fixture already speaks — nothing here replaces
`make_ok`/`make_error`, and no existing shape changes. What was missing was not a
different wire format but a NAME for the shapes the runtime was already passing around:
four of the five below already existed as anonymous dicts assembled in three or four
places each, so "what is in a route decision" could only be answered by reading
`Router.route()` and hoping no caller added a key on the way past.

Each contract carries `to_dict()`/`from_dict()` so it can cross any boundary that still
expects a dict, and `from_dict()` ignores unknown keys — a producer that grows a field
must not break a consumer pinned to an older shape.

`UsageRecord` is the exception: it is genuinely new, and it is the one contract with a
downstream that does not exist yet. Every P1 metric (cost per accepted task, premium
context avoided, time to completion, first-pass correctness, rework) is an aggregation
over the `usage.jsonl` stream this type defines. Without it those metrics have no source
and can only be estimated by re-reading per-run archive folders.
"""

import hashlib
from dataclasses import asdict, dataclass, field, fields
from typing import Any

# Bumped to 2 when the actual_* token fields landed. The number is a reading aid, not a
# gate: `_coerce` drops unknown keys either way, so a v1 row stays readable and simply
# reports its actual_* fields as None — which is the truth about it. Bumped to 3 for
# reuse_outcome/graph_status/graph_ms/provider_resumed/provider_thread_changed/effort; a
# v2 row reads them as None, "not recorded". Bumped to 4 for e2e_stage/browser_seconds,
# and because a browser run that reached no provider stopped writing a row at all.
CONTRACT_VERSION = 4

# Where the usage stream lands, relative to the project's `.workflow` directory. A sibling
# of redactions.jsonl and deliberately project-local: telemetry about a codebase is
# metadata about that codebase, and it stays on the machine that produced it.
USAGE_STREAM_NAME = "usage.jsonl"
# The governance trail. A sibling stream rather than a column on the usage rows, because
# measurement and record-keeping have to be free to diverge: usage may be resampled or
# recomputed under a better definition, an audit row may not.
AUDIT_STREAM_NAME = "audit.jsonl"
# Outcomes of checks run AGAINST the repo (tests, security sweeps) as opposed to calls the
# runtime made. Different actor, different stream.
QUALITY_STREAM_NAME = "quality.jsonl"
# Task lifecycle events (DEC-015): skills, edits and commits, from the runtime and from the
# task-events hook. Not usage: local skills and edits are not delegated calls, and governance
# budgets read the usage stream as billable work.
TASK_STREAM_NAME = "tasks.jsonl"
# 2: the hook reads skills from the prompt and commits from HEAD at turn boundaries instead
# of from every Read and Bash call; a commit row now always carries `paths`.
TASK_EVENT_VERSION = 2


def _coerce(cls, payload: dict | None):
    """Build `cls` from a dict, dropping keys it does not declare.

    Tolerant by design. These types travel between a producer and a consumer that are
    versioned together but deployed apart (an archived call.meta.json outlives the code
    that wrote it), so an unexpected key means "written by a different version", not
    "corrupt". Raising there would make the aggregator fail on exactly the historical
    records it exists to read.
    """
    known = {item.name for item in fields(cls)}
    data = {key: value for key, value in (payload or {}).items() if key in known}
    return cls(**data)


def correlation_id_for(project_root, session_id: str, task: str) -> str:
    """A stable id for "the same piece of work", across the commands that touch it.

    This is what makes rework and first-pass correctness computable at all. A plan, the
    execute that follows it, and the verify that judges it are three separate delegated
    calls with three separate prompt_ids; nothing in the runtime previously tied them
    together, so "was this right on the first try" had no subject to be true of.

    Derived rather than generated: a generated id would have to be threaded through the
    job record, the session state, and the CLI argv to survive between calls, and every
    hop is a place it can be dropped. Hashing the inputs that already identify the work
    means two calls about the same task in the same session correlate without anything
    being carried between them.

    Deliberately NOT global: the same task text in a different session is different work
    (a fresh attempt after a session reset should not count as rework of the old one).

    Derivation alone could not tie a chain together, though: plan, execute, and verify
    carry different task TEXTS for the same piece of work, so deriving from each produced
    three ids where the metrics needed one. So a plan additionally records its derived id
    as the session's active chain (state.json `chain`), and the execute/verify that follow
    prefer that recorded id over their own derivation. One hop, through state the runtime
    already owns, and fail-open: with no recorded chain they fall back to deriving exactly
    as before.
    """
    material = "\x00".join(
        (str(project_root), str(session_id or ""), " ".join((task or "").split()).lower())
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass
class TaskSpec:
    """What was asked, before anything decided how to answer it.

    Assembled today from argv in `main.py` and passed to `Executor.execute()` as five
    positional arguments; naming it is what gives `correlation_id` somewhere to live.
    """

    command: str
    task: str = ""
    session_id: str = ""
    project_root: str = ""
    model: str | None = None
    correlation_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict | None) -> "TaskSpec":
        return _coerce(cls, payload)

    @classmethod
    def build(
        cls,
        command: str,
        task: str,
        session_id: str,
        project_root,
        model: str | None = None,
        correlation_id: str | None = None,
    ) -> "TaskSpec":
        # An explicit id wins: a verify that follows a plan is the SAME piece of work,
        # and only the caller (reading the session's chain) can know that. Absent one,
        # derive as always — the fallback that keeps a chainless call measurable.
        return cls(
            command=(command or "").strip().lower(),
            task=task or "",
            session_id=str(session_id or ""),
            project_root=str(project_root or ""),
            model=model,
            correlation_id=correlation_id
            or correlation_id_for(project_root, session_id, task),
        )


@dataclass
class RouteDecision:
    """Who runs this call, on what model, with what budget.

    Mirrors `Router.route()` key for key. The point is not to change routing but to make
    the answer to "what does route() return" a declaration instead of a code read: the
    dict is consumed field-by-field across ~180 lines of `executor.execute()`, and a key
    added at one end had no way to announce itself at the other.
    """

    command: str = ""
    role: str = ""
    model: str | None = None
    provider_command: str | None = None
    provider_agent: str | None = None
    effort: str | None = None
    declared_tools: list = field(default_factory=list)
    timeout_seconds: int | None = None
    bootstrap_timeout_seconds: int | None = None
    poll_interval_seconds: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict | None) -> "RouteDecision":
        return _coerce(cls, payload)


@dataclass
class EvidenceBundle:
    """The artifact a delegated call produced, and how much of it is still true.

    `anchors` and `reused` are already the runtime's own freshness currency — evidence is
    reusable exactly when every anchor hash still matches — so the bundle records them
    beside the path rather than leaving the caller to rebuild the pair from two places.
    """

    artifact_path: str = ""
    anchors: int = 0
    reused: bool = False
    leads_path: str | None = None
    facts_path: str | None = None
    digest: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict | None) -> "EvidenceBundle":
        return _coerce(cls, payload)


@dataclass
class VerificationReport:
    """The derived verdict of a verification run.

    Mirrors what `validate_verification_contract()` already returns. Named because it is
    the one structure a human decision hangs on — `pass` here is what "accepted" means in
    `UsageRecord`, so the two must be reading the same field of the same thing.

    `verdict` is the DERIVED value and `declared_verdict` is what the second agent claimed.
    Keeping both is the whole point: an agent declaring DONE over non-empty
    blocking_findings is a disagreement worth preserving, not an inconsistency to resolve
    silently in favour of either side.
    """

    verdict: str = "incomplete"
    declared_verdict: str | None = None
    blocking_findings: int = 0
    checks_run: int = 0
    not_verified: int = 0
    warnings: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict | None) -> "VerificationReport":
        return _coerce(cls, payload)


@dataclass
class UsageRecord:
    """One delegated call, as a measurable row.

    The unit the P1 metrics aggregate over. Fields are recorded, never derived at write
    time, so a later change to how a metric is defined re-reads history instead of
    invalidating it.

    Token counts inherit `token_source` from the call meta they come from — today always
    `estimated` (chars//4). The field ships anyway rather than being assumed: a provider
    that starts reporting actuals must be able to say so in the same stream, and a cost
    figure that cannot tell an estimate from a measurement is not a cost figure.

    `accepted` is `True` only for a verify whose DERIVED verdict is `pass`. This is a
    deliberately conservative definition and it has a known consequence: work that never
    goes through /.verify is never counted as accepted, so cost-per-accepted-task reads
    HIGH rather than neutral. Recorded as a nullable field so the aggregator can tell
    "judged and rejected" (False) from "never judged" (None) — collapsing those two is
    what would make the number dishonest rather than merely conservative.
    """

    contract_version: int = CONTRACT_VERSION
    recorded_at: str = ""
    correlation_id: str = ""
    session_id: str = ""
    prompt_id: str | None = None
    command: str = ""
    role: str | None = None
    model: str | None = None
    provider: str | None = None
    ok: bool = False
    error_type: str | None = None
    verdict: str | None = None
    accepted: bool | None = None
    duration_seconds: float | None = None
    prompt_chars: int | None = None
    response_chars: int | None = None
    digest_chars: int | None = None
    estimated_input_tokens: int | None = None
    estimated_output_tokens: int | None = None
    # "estimated" (chars//4), "provider" (both directions measured), or "mixed" (one
    # direction measured and the other estimated — the shape a provider that reports only
    # its output count produces). A cost figure that cannot tell those apart is not one.
    token_source: str | None = None
    # What the provider itself reported, when it reported anything. Kept BESIDE the
    # estimates rather than replacing them, because the two count different things: a
    # chars//4 estimate measures the answer that arrived, while a provider's output count
    # includes the reasoning that never did. A row that cannot show both cannot explain
    # the gap between them, and that gap is the whole reason these fields exist.
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    # Breakdowns, NOT addends. Every provider that reports reasoning counts it inside
    # output_tokens, and cached input inside input_tokens; adding these to a total bills
    # the same token twice. Recorded because the split is the interesting part, and
    # spelled out here because a later reader summing every int on this row is the
    # obvious mistake to make.
    actual_reasoning_tokens: int | None = None
    actual_cached_input_tokens: int | None = None
    # Which provider invocation of this command produced the row. A continuation runs the
    # adapter a second time, and this is what separates the retry's cost from the first
    # attempt's. None for a row that never reached a provider at all, such as a reuse hit.
    provider_call_index: int | None = None
    # Output the second agent produced that main_agent never had to read, because the
    # digest stood in for it. The digest-first contract's whole justification, finally
    # expressed as a number instead of an argument.
    premium_context_avoided_tokens: int | None = None
    reused_evidence: bool = False
    provider_call_avoided: bool = False
    # Why the reuse lookup did or did not serve this command: `hit`, `no_prior` (the exact
    # query never ran before), `stale` (it ran, an anchor has moved since), `unreadable`,
    # `error`, `not_offered` (a role reuse does not apply to), `disabled`. CASE-009 saw a
    # zero reuse rate and could not say which of these it was made of.
    reuse_outcome: str | None = None
    # Graph leads for this call: `used`, `empty` (a graph, nothing matched), `absent`,
    # `refreshing` (the refresh worker held the lock, DEC-012), `disabled`; `available` for
    # verify-browser, which takes no leads and only records the refresh state. `graph_ms` is
    # what the lookup took, the part of a call's latency the graph is responsible for.
    graph_status: str | None = None
    graph_ms: int | None = None
    # Whether the provider resumed an existing thread for this invocation, as the adapter
    # reported it (codex, agy). None where the adapter does not say: unknown, not fresh.
    # H-008 separates a cached share earned by a static prefix from one earned by a
    # growing resumed thread, and needs this to tell the two apart.
    provider_resumed: bool | None = None
    # A resumed call whose provider answered on a DIFFERENT thread id: the session lost its
    # state without an error. None where the adapter does not report it.
    provider_thread_changed: bool | None = None
    # Reasoning effort the adapter passed (`low`, `medium`, `high`, ...). None is the
    # provider's own default, not "unknown effort": no flag was sent.
    effort: str | None = None
    # verify-browser only: `draft` (the e2e_spec call, recorded as `verify-browser`) or
    # `run` (the review, recorded as `verify`). Lets a reader keep browser verification
    # apart from /.verify without joining on prompt ids.
    e2e_stage: str | None = None
    # Wall time the browser itself took in a `run` — the player and any existing tests the
    # project ran — which `duration_seconds` (the provider's time) never includes. On the
    # command's final row only, so a sum over rows counts it once.
    browser_seconds: float | None = None
    # The provider CLI's own version string (`<cli> --version`), or None. Ties a failure to
    # the release that produced it (CASE-015).
    provider_version: str | None = None
    # `stable` when that release is the bundle's `stable_version`, `untested` when it is
    # another, `unreadable` when the CLI did not answer (core/provider/versions.py).
    provider_version_status: str | None = None
    # /.verify only, on the command's final row: whether main_agent wrote a test request,
    # how many requested commands the runtime ran and how many of those did not pass, and
    # their summed wall time — none of it inside `duration_seconds`.
    tests_requested: bool | None = None
    tests_run: int | None = None
    tests_failed: int | None = None
    tests_seconds: float | None = None
    # Fact lifecycle for the command, on its final row only (exploration/reasoning). How
    # many stored facts were candidates, how many were dropped as stale or below the
    # relevance floor, how many were injected, and how many this run's output added or
    # had refused. None on rows written before these fields, and on every other role:
    # "no facts asked for" is not "zero facts found".
    facts_candidates: int | None = None
    facts_injected: int | None = None
    facts_rejected_stale: int | None = None
    facts_rejected_irrelevant: int | None = None
    facts_ingested: int | None = None
    facts_ingest_rejected: int | None = None
    # How many credential-shaped values the redaction boundary scrubbed from this call.
    # A count, never the values: the whole reason they were scrubbed is that they should
    # exist nowhere on disk, and telemetry is not an exception to that.
    redactions: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict | None) -> "UsageRecord":
        return _coerce(cls, payload)


def billable_input(row: "UsageRecord") -> int:
    """The input count to charge this row by: measured when measured, estimated otherwise.

    Cached input is NOT added. It is already part of the input count that sits beside it,
    and adding the two would bill the cheap half of a call twice.

    Falls back to zero only for a row that has neither figure, which is a row that recorded
    no call. Everywhere a distinction between "zero" and "unknown" matters, the nullable
    fields are still there to read directly.
    """
    if row.actual_input_tokens is not None:
        return row.actual_input_tokens
    return row.estimated_input_tokens or 0


def billable_output(row: "UsageRecord") -> int:
    """The output count to charge this row by.

    Reasoning is NOT added, for the same reason cached input is not: providers report it
    as a breakdown OF this number. Summing them is the mistake that makes a token-spend
    figure grow every time a model thinks harder about the same answer.
    """
    if row.actual_output_tokens is not None:
        return row.actual_output_tokens
    return row.estimated_output_tokens or 0


def _digest_chars(digest: Any) -> int | None:
    """How much text the digest actually is, as main_agent would read it."""
    if not isinstance(digest, dict):
        return None
    parts: list[str] = [str(digest.get("summary") or "")]
    findings = digest.get("key_findings")
    if isinstance(findings, list):
        parts.extend(str(item) for item in findings)
    parts.append(str(digest.get("recommended_next_action") or ""))
    return sum(len(part) for part in parts)


def usage_from_result(
    result: dict,
    *,
    spec: TaskSpec,
    call_meta: dict | None,
    recorded_at: str,
    reuse_outcome: str | None = None,
    graph_status: str | None = None,
    graph_ms: int | None = None,
) -> UsageRecord:
    """Assemble a UsageRecord from what a finished call already knows.

    Reads only fields that exist today. Anything absent stays `None` rather than being
    filled with a default: a zero duration and an unrecorded duration are different facts,
    and averaging the first into a report as though it were the second is how telemetry
    starts lying.
    """
    meta = result.get("meta") if isinstance(result, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    call = call_meta if isinstance(call_meta, dict) else {}

    e2e_stage = None
    if spec.command == "verify-browser":
        e2e_stage = "draft"
    elif meta.get("invocation") == "verify-browser":
        e2e_stage = "run"
    browser_seconds = _browser_seconds(meta.get("e2e")) if e2e_stage == "run" else None

    response_chars = call.get("response_chars")
    if response_chars is None:
        content = result.get("content") if isinstance(result, dict) else None
        response_chars = len(content) if isinstance(content, str) else None

    digest_chars = _digest_chars(result.get("digest") if isinstance(result, dict) else None)
    # What the digest stood in for is a property of the ANSWER the command returned, not
    # of whichever provider call happened to produce the last piece of it. A continuation
    # merges two replies and records a row per call; the row carrying the digest holds
    # only its own half, and comparing the digest against that half would report a saving
    # smaller than the one main_agent actually got — sometimes zero. `final_response_chars`
    # is how the caller says "measure against the whole answer"; absent, the row's own
    # count is the whole answer and the two are the same thing.
    final_chars = call.get("final_response_chars")
    if not isinstance(final_chars, int):
        final_chars = response_chars
    avoided = None
    if final_chars is not None and digest_chars is not None:
        avoided = max(0, final_chars - digest_chars) // 4

    verdict = meta.get("verdict")
    accepted = None
    if spec.command == "verify" and verdict is not None:
        accepted = verdict == "pass"

    return UsageRecord(
        recorded_at=recorded_at,
        correlation_id=spec.correlation_id,
        session_id=spec.session_id,
        prompt_id=call.get("prompt_id"),
        command=spec.command,
        role=call.get("role"),
        model=call.get("model") or spec.model,
        provider=call.get("provider") or call.get("provider_command"),
        ok=bool(result.get("ok")) if isinstance(result, dict) else False,
        error_type=meta.get("error_type"),
        verdict=verdict,
        accepted=accepted,
        duration_seconds=call.get("duration_seconds") or meta.get("duration_seconds"),
        prompt_chars=call.get("prompt_chars"),
        response_chars=response_chars,
        digest_chars=digest_chars,
        estimated_input_tokens=call.get("estimated_input_tokens"),
        estimated_output_tokens=call.get("estimated_output_tokens"),
        token_source=call.get("token_source"),
        # Read straight through from the call meta, with no arithmetic on the way. Whoever
        # measured these is the only party entitled to combine them: reasoning already sits
        # inside the output count, so a helpful-looking sum here would double-bill it.
        actual_input_tokens=call.get("actual_input_tokens"),
        actual_output_tokens=call.get("actual_output_tokens"),
        actual_reasoning_tokens=call.get("actual_reasoning_tokens"),
        actual_cached_input_tokens=call.get("actual_cached_input_tokens"),
        provider_call_index=call.get("provider_call_index"),
        premium_context_avoided_tokens=avoided,
        reused_evidence=bool(meta.get("reused_evidence")),
        redactions=sum(
            int(item.get("count") or 0)
            for item in (meta.get("redactions") or [])
            if isinstance(item, dict)
        ),
        # A reuse hit answered without spending a provider call at all. Distinct from
        # premium context avoided, which measures main_agent's context rather than the
        # second agent's spend.
        provider_call_avoided=bool(meta.get("reused_evidence")),
        reuse_outcome=reuse_outcome,
        graph_status=graph_status,
        graph_ms=graph_ms,
        provider_resumed=(
            call.get("resumed") if isinstance(call.get("resumed"), bool) else None
        ),
        provider_thread_changed=(
            call.get("thread_changed") if isinstance(call.get("thread_changed"), bool) else None
        ),
        effort=call.get("effort") if isinstance(call.get("effort"), str) else None,
        e2e_stage=e2e_stage,
        browser_seconds=browser_seconds,
        provider_version=(
            call.get("provider_version") if isinstance(call.get("provider_version"), str) else None
        ),
        provider_version_status=(
            call.get("provider_version_status")
            if isinstance(call.get("provider_version_status"), str) else None
        ),
        **_runtime_tests_fields(meta.get("runtime_tests")),
        **_fact_fields(meta.get("facts")),
    )


def _fact_fields(facts) -> dict:
    if not isinstance(facts, dict):
        return {}

    def count(key: str) -> int | None:
        value = facts.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    rejected = [count("ingest_rejected_anchor"), count("ingest_rejected_length")]
    return {
        "facts_candidates": count("candidates"),
        "facts_injected": count("injected"),
        "facts_rejected_stale": count("rejected_stale"),
        "facts_rejected_irrelevant": count("rejected_irrelevant"),
        "facts_ingested": count("ingested"),
        "facts_ingest_rejected": (
            sum(v for v in rejected if v is not None) if any(v is not None for v in rejected) else None
        ),
    }


def _runtime_tests_fields(runtime_tests) -> dict:
    if not isinstance(runtime_tests, dict):
        return {}
    ran = [o for o in runtime_tests.get("outcomes") or [] if isinstance(o, dict) and o.get("status") != "refused"]
    return {
        "tests_requested": bool(runtime_tests.get("requested")),
        "tests_run": len(ran),
        "tests_failed": sum(1 for o in ran if o.get("status") != "passed"),
        "tests_seconds": runtime_tests.get("seconds"),
    }


def _browser_seconds(e2e_meta) -> float | None:
    """The player's and the project's own tests' time in one browser run, or None."""
    stages = e2e_meta.get("stages") if isinstance(e2e_meta, dict) else None
    measured = [
        float(stage["duration_seconds"])
        for stage in stages or []
        if isinstance(stage, dict)
        and stage.get("stage") in (2, "existing_tests")
        and isinstance(stage.get("duration_seconds"), (int, float))
    ]
    return round(sum(measured), 3) if measured else None
