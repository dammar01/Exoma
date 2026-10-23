import time
from pathlib import Path

from adapters.contract.base import SecondAgentAdapter
from adapters.contract.registry import hint_conflict, resolve_adapter, selected_provider
from config.providers import provider_boundary, transport_budget
from config.settings import DEFAULT_TASK_TRUNCATION_HARD_RATIO
from core.evidence.contract import (
    FANOUT_DECLINED,
    anchor_cap,
    FANOUT_DENIED,
    FANOUT_INCAPABLE,
    FANOUT_MISMATCH,
    FANOUT_UNREPORTED,
    STRUCTURAL_KINDS,
    cap_confidence,
    contract_warnings,
    detect_subagent_usage,
    extract_digest,
    make_error,
    readable_claims,
    validate_verification_contract,
)
from core.evidence.contracts import TaskSpec, correlation_id_for, usage_from_result
from adapters.shared.usage import token_source_for
from core.policy.governance import budget_limit, budget_state
from core.audit import task_telemetry, telemetry
from core.evidence import fact_store
from core.evidence import evidence_store
from core.graph import graph_index
from core.prompt.prompt_builder import build_prompt
from core.prompt.router import Router
from utils.redact import redact_value
from core.evidence import quick_verify
from core.audit.diagnostics import run_sweep
from core.evidence.runtime_io import (
    write_audit_record,
    write_call_meta,
    write_evidence_sidecars,
    write_prompt_handoff,
    write_redaction_audit,
    write_first_reply,
    write_response_snapshot,
    write_usage_record,
)
from core.workspace.session_manager import bind_provider, record_provider_session
from core.workspace.runtime_lock import (
    acquire_runtime_lock,
    release_runtime_lock,
    runtime_lock_owned,
)
from core.runtime.agent_output import parse_questions
from core.knowledge import retrieve as knowledge_retrieve
from core.runtime.config_defaults import auto_verify_after_execute, graph_leads_enabled, knowledge_relevant_limit, production_branch, subagent_fanout_enabled, verify_mode
from core.runtime.state import active_chain_correlation, bind_session, fanout_capability, set_fanout_capability, stamp_task_chain, update_command_cache, update_plan_scope, update_state_from_agent_output
from core.provider.continuation import (
    _EVIDENCE_MARKERS,
    _contract_gap,
    _continuation_prompt,
    _merge_continuation,
)
from core.provider.result_prep import (
    _attach_redactions,
    _evidence_context,
    _sanitize_result,
    _scope_incomplete,
    _without_raw_args,
    scope_warnings,
)
from core.workspace.workspace_paths import (
    CONFIG_VERSION,
    detect_project_root,
    now_iso,
    workflow_paths,
)


# One line per way fan-out can fail to happen. Each says what to DO about it, because
# "no fan-out" alone left the reader unable to tell a repairable config wall from a
# permanent limitation.
_FANOUT_WARNINGS = {
    FANOUT_MISMATCH: (
        "second_agent declared sub-agents but tagged no claims with [cN], so the "
        "dispatch cannot be corroborated — see meta.declared_clusters for what it "
        "claimed. The sub-agents may well have run; the missing tags are what make it "
        "unprovable. Treat the fan-out as unconfirmed, and read the merged claims as "
        "the primary agent's own work"
    ),
    FANOUT_DENIED: (
        "sub-agent spawn was refused by a permission rule, not missing. Check the "
        "opencode agent's `permission.task` — the runtime calls `--agent plan`, which "
        "may not spawn write-capable subagents like `general`; `explore` is allowed"
    ),
    FANOUT_INCAPABLE: (
        "second_agent reports no spawn tool; fan-out is off for this project and will be "
        "retried automatically after AI_PROXY_FANOUT_RECHECK_HOURS (default 24h). To "
        "retry now, delete .workflow/capabilities.json"
    ),
    FANOUT_DECLINED: (
        "second_agent chose not to fan out; the answer is a sequential read"
    ),
    FANOUT_UNREPORTED: (
        "second_agent omitted the required `subagents:` line, so whether it fanned out "
        "is unknown; treat the result as a sequential read"
    ),
}


def _anchor_counts(entry: dict) -> tuple[int, int, bool]:
    """(certified, cited, scanned_all) for one evidence row.

    Rows indexed before `anchor_scan` existed only know their stored prefix; for them the
    prefix is the whole answer unless it is exactly as long as the old cap, which is the one
    length that cannot tell "every citation" from "stopped here".
    """
    scan = entry.get("anchor_scan")
    if isinstance(scan, dict) and isinstance(scan.get("cited"), int):
        return int(scan.get("certified") or 0), scan["cited"], bool(scan.get("scanned_all"))
    stored = entry.get("anchors") or []
    certified = sum(1 for a in stored if a.get("certified") is not False)
    complete = entry.get("anchors_complete") is True
    return (
        certified,
        len(stored),
        complete or len(stored) < evidence_store.MAX_ANCHORS_PER_ARTIFACT,
    )



def graph_leads_for_call(project_root, task: str) -> tuple[dict | None, str, int]:
    """Graph leads for one delegated call, with what happened and what it cost.

    Returns (leads, status, ms). Status is `used`, `empty` (a graph, nothing matched),
    `absent`, or `refreshing`. Recorded for RQ-13: DEC-012 makes a call during a refresh
    go without leads, and without this it is indistinguishable from having no graph. A
    lock taken between the check and the read lands as `empty`; rare, and still a call
    without leads.
    """
    started = time.monotonic()
    leads = None
    if graph_index.refresh_in_progress(project_root):
        status = "refreshing"
    else:
        leads = graph_index.leads(project_root, task)
        if leads:
            status = "used"
        elif graph_index.graph_path(project_root).is_file():
            status = "empty"
        else:
            status = "absent"
    return leads, status, round((time.monotonic() - started) * 1000)


def graph_state_for_call(project_root) -> tuple[str, int]:
    """Whether a graph refresh was running when a call that takes no leads started.

    For `verify-browser`, whose prompts carry no graph leads: the latency question (RQ-13,
    EXP-004) is about a refresh competing with the call, not about leads, so the lookup is
    not run. Status is `refreshing`, `available` (a graph, not consulted) or `absent`.
    """
    started = time.monotonic()
    if graph_index.refresh_in_progress(project_root):
        status = "refreshing"
    elif graph_index.graph_path(project_root).is_file():
        status = "available"
    else:
        status = "absent"
    return status, round((time.monotonic() - started) * 1000)


def _scope_width(project_root, session_id: str | None) -> dict | None:
    """How far the working tree has spread against the scope the plan declared.

    Fail-open and silent, like every other read of the tree in this pipeline: no git, no
    repo, or an unreadable scope file means the signal is absent, never wrong. A clean
    tree returns zeros rather than None, because "nothing has changed yet" is itself an
    answer main_agent can use.
    """
    try:
        from core.evidence.quick_verify import scope_width
        from core.runtime.state import load_workspace_state

        scope = load_workspace_state(Path(project_root), session_id)["scope"]
        return scope_width(Path(project_root), scope.get("editable_scope"))
    except Exception:
        return None


def _apply_runtime_tests(project_root, session_id: str, result: dict, on_progress=None) -> dict:
    """`verify_tests.apply`, which must not throw away the review it runs after.

    It runs once the paid provider call has returned. An exception escaping it lost that
    review, and since the request is consumed only on success, every retry crashed the same
    way. Anything unexpected now returns the review with a runtime gap instead — never a
    pass: the `tests:` prefix makes it a `runtime_gap`, which exits nonzero.
    """
    from core.evidence import verify_tests
    from core.evidence.contract import RUNTIME_GAP_PREFIX

    original = dict(result)
    original_meta = dict(result.get("meta") or {})
    try:
        return verify_tests.apply(project_root, session_id, result, on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001 — reported in the result, fail-closed
        kind = type(exc).__name__
        gap = f"{RUNTIME_GAP_PREFIX} runtime tests crashed: {kind}; no test result was recorded"
        try:
            content = verify_tests.merge_gap(original.get("content") or "", gap)
        except Exception:
            content = (original.get("content") or "") + f"\n\nnot_verified:\n- {gap}\n"
        # Consumed like a request that ran: a later verify must not rerun it silently.
        try:
            verify_tests._consume(project_root, session_id)
        except Exception:
            pass
        original["content"] = content
        original["meta"] = {
            **original_meta,
            "runtime_tests": {"requested": None, "error": kind, "outcomes": [], "seconds": 0},
        }
        return original


def _redacted_text(value) -> str | None:
    """`value` through redaction when it is a string, else None. Never raises."""
    if not isinstance(value, str):
        return None
    try:
        cleaned = redact_value(value)[0]
    except Exception:
        return None
    return cleaned if isinstance(cleaned, str) else None


def _apply_provider_usage(meta: dict, usage) -> None:
    """Stamp provider-reported counts onto a call meta, or leave it estimated.

    Copies four numbers and computes nothing from them. Reasoning already sits inside the
    output count and cached input inside the input count, so the only arithmetic that
    could happen here is the arithmetic that would be wrong.

    A provider that reported nothing leaves every field untouched, which keeps the row's
    chars//4 estimate and its `token_source: estimated`. That is a measurement this
    runtime does not have, and saying so is more useful than a zero that reads as one.
    """
    if not isinstance(usage, dict):
        return
    meta["actual_input_tokens"] = usage.get("input_tokens")
    meta["actual_output_tokens"] = usage.get("output_tokens")
    meta["actual_reasoning_tokens"] = usage.get("reasoning_tokens")
    meta["actual_cached_input_tokens"] = usage.get("cached_input_tokens")
    meta["token_source"] = token_source_for(usage)


class Executor:
    def __init__(
        self,
        router: Router | None = None,
        adapter: SecondAgentAdapter | None = None,
        session_manager=None,
        provider: str | None = None,
    ) -> None:
        self.router = router or Router()
        self._router_override = router is not None
        # An injected adapter wins outright (tests, callers that already built one), and so
        # does an explicitly named provider.
        self._adapter_override = adapter is not None or provider is not None
        # Built here so `self.adapter` exists before any call, but NOT final: this happens
        # at import time with no project in hand, so it can only ever see the built-in
        # default. `_adapter_for()` re-resolves once execute() knows the project root —
        # without it, `.workflow/config.json` and second_agent.json selected a provider
        # that the thing actually running the call never read.
        self.adapter = adapter or resolve_adapter(provider)
        self.session_manager = session_manager
        # The call meta of the run in flight, so the usage record can be assembled on the
        # return path without threading it back through every early return. Reset at the
        # top of execute(): a reuse hit builds no call meta, and inheriting the previous
        # call's numbers would attribute a real provider spend to a free answer.
        self._last_call_meta: dict | None = None
        # One entry per provider invocation of the run in flight, in order. A continuation
        # runs the adapter twice, and a single call meta cannot describe two calls: the
        # attribute the adapter writes is rebound wholesale on the second run, so whatever
        # the first one measured is gone unless it was copied out before then.
        self._call_metas: list[dict] = []

    # Per-call usage instrumentation, reset in `_execute`. Class-level defaults, not
    # `__init__` assignments: `_record_usage` swallows every error, so an executor built
    # without `__init__` would otherwise lose its usage row to an AttributeError silently.
    _reuse_outcome: str | None = None
    _graph_status: str | None = None
    # Facts served to the call in flight, as `fact_store.served_fact_refs`; None for roles
    # that get no facts. Archived in call.meta.json so EXP-013 knows exactly what was served.
    _served_facts: list[dict] | None = None
    _graph_ms: int | None = None

    def _adapter_for(self, project_root):
        """The adapter THIS project selects, resolved late enough to know the project.

        Selection order matches `adapters/contract/registry.py`: an injected adapter or a pinned
        provider, then second_agent.json's `provider`, then the built-in default. A
        `provider` that was merely defaulted into the config does not count as a choice —
        `provider_explicit` is what separates "the file said codex" from "the file said
        nothing".

        `.workflow/config.json`'s `runtime.second_agent` is NOT consulted. It named the
        adapter while `provider_command` kept coming from second_agent.json, so a project
        that selected codex there built the codex adapter and handed it opencode's binary —
        a combination `_command_guard` refuses on every call. The hint is reported by
        `_config_provenance` instead.
        """
        if self._adapter_override:
            return self.adapter
        name = selected_provider(project_root)
        if name == getattr(self.adapter, "adapter", None):
            return self.adapter
        self.adapter = resolve_adapter(name, project_root=project_root)
        return self.adapter

    def _router_for(self, project_root) -> Router:
        """Use an injected router as-is; otherwise route via project-local opencode config."""
        if self._router_override:
            return self.router
        from config.settings import load_provider_config_for

        return Router(load_provider_config_for(project_root))

    def _provider_config_refusal(self, project_root, command: str) -> dict | None:
        """A structured error when the project has no usable second_agent.json, else None.

        An injected router carries its own config, so there is nothing on disk to refuse.
        """
        if self._router_override:
            return None
        try:
            from config.settings import resolve_provider_config_for

            resolved = resolve_provider_config_for(project_root)
        except Exception:
            return None
        source = resolved.get("source")
        if source == "missing":
            return make_error(
                "provider_config_missing",
                f"no second agent config for this project: {resolved.get('path')} does not exist",
                next_action=(
                    "Run /.init to create it from the tool seed, then /.provider to choose "
                    "the provider and model. The runtime no longer falls back to a "
                    "machine-wide config."
                ),
                meta={"command": command, "config_path": resolved.get("path")},
            )
        if source == "invalid":
            return make_error(
                "provider_config_invalid",
                f"{resolved.get('path')} cannot be read: {resolved.get('error')}",
                next_action=(
                    "Fix the file so it is one JSON object, or delete it and run /.init. "
                    "Nothing is substituted for an unreadable config."
                ),
                meta={
                    "command": command,
                    "config_path": resolved.get("path"),
                    "config_error": resolved.get("error"),
                },
            )
        return None

    def _config_provenance(self, project_root) -> dict:
        """Which provider config the route's values actually came from.

        Recorded on every call because the failure it exposes is invisible otherwise: a
        project that ships `.workflow/second_agent.json` and still shows
        `config_source: tool_default` is running on settings it never chose, and the
        model in this same metadata block is the proof.
        """
        if self._router_override:
            return {"config_source": "injected_router"}
        try:
            from config.settings import resolve_provider_config_for

            resolved = resolve_provider_config_for(project_root)
        except Exception as exc:
            return {"config_source": "unresolved", "config_error": str(exc)}
        meta = {
            "config_source": resolved.get("source"),
            "config_path": resolved.get("path"),
        }
        if resolved.get("error"):
            meta["config_error"] = resolved["error"]
        # A config.json still naming a provider is a config the runtime is knowingly not
        # honouring. Saying so is the whole difference between a deprecated key and a key
        # that quietly does nothing.
        conflict = hint_conflict(
            project_root,
            resolved.get("config"),
            selected=getattr(self.adapter, "adapter", None),
        )
        if conflict:
            meta.update(conflict)
        return meta

    @staticmethod
    def _audit_redactions(
        project_root: Path, session_id: str, command: str, redactions: list[dict]
    ) -> None:
        if not redactions:
            return
        try:
            write_redaction_audit(project_root, session_id, command, redactions)
        except OSError:
            # The redaction itself already happened; this only records that it did, so a
            # full disk or a locked file must not fail the call. Narrowed from a bare
            # Exception on purpose: anything that is NOT an I/O failure here is a bug in
            # the audit writer, and swallowing it hid that the trail had stopped being
            # written at all.
            pass

    def _finalize_runtime_result(
        self,
        result: dict,
        project_root: Path,
        command: str,
        task: str,
        session_id: str,
        bound: dict,
        fanout: bool,
        *,
        audit_existing_redactions: bool = False,
    ) -> dict:
        result, new_redactions = _sanitize_result(result)
        existing_redactions = (result.get("meta") or {}).get("redactions") or []
        self._audit_redactions(
            project_root,
            session_id,
            command,
            existing_redactions if audit_existing_redactions else new_redactions,
        )

        content = result.get("content") or ""
        cache_names = {
            "explore": "last_explore_result",
            "analyze": "last_analyze_result",
            "plan": "last_plan_result",
        }
        cache_name = cache_names.get(command)
        if cache_name:
            update_command_cache(project_root, cache_name, content, session_id)
            update_state_from_agent_output(
                project_root, command, task, content, session_id
            )
        if command == "plan":
            update_plan_scope(project_root, content, session_id)

        meta = result.setdefault("meta", {})
        meta["project_root"] = str(project_root)
        meta["session_reset"] = bool(bound.get("session_reset"))
        meta["policy"] = {
            "auto_verify_after_execute": auto_verify_after_execute(project_root),
            "verify_mode": verify_mode(project_root),
            "subagent_fanout_enabled": fanout,
        }
        result, metadata_redactions = _sanitize_result(result)
        self._audit_redactions(
            project_root, session_id, command, metadata_redactions
        )
        self._record_usage(result, project_root, command, task, session_id)
        return result

    def _budget_state(self, project_root: Path, session_id: str) -> dict:
        """This session's spend against its ceiling, read from recorded history.

        Fail-open: an unreadable usage stream must not become a refusal. The ceiling
        exists to stop runaway spend, and a governance control whose broken state is
        "nothing runs" gets removed rather than fixed.

        Bounded by what has been RECORDED. Calls in flight are not in the stream yet, so
        concurrent work can cross the line by up to the amount running at the time. Stated
        rather than papered over: a hard per-call reservation would need a lock across
        every worker, which is a real cost for a bound nobody has needed yet.
        """
        try:
            from config.settings import load_provider_config_for

            limit = budget_limit(load_provider_config_for(project_root))
            if limit is None:
                return {"limit": None, "exceeded": False}
            return budget_state(telemetry.load_usage(project_root), session_id, limit)
        except Exception:
            return {"limit": None, "exceeded": False}

    def _adapter_run(
        self,
        prompt: str,
        session: dict,
        model: str | None,
        work_dir: str | None,
    ) -> dict:
        """Invoke the adapter with its metadata cleared first, every single time.

        A wrapper rather than a line repeated at each call site: `last_call_meta` is
        rebound per subprocess and never reset by the adapter, and one Executor serves
        many calls (`main.EXECUTOR` is a singleton), so metadata left standing from an
        earlier invocation reads as this one's. A continuation refused before it spawned
        anything would then be measured with the FIRST call's numbers and bill them
        twice. Clearing at the seam makes forgetting a call site impossible rather than
        merely unlikely.
        """
        try:
            self.adapter.last_call_meta = None
        except Exception:
            pass
        return self.adapter.run(prompt, session, model, work_dir)

    @staticmethod
    def _reached_a_provider(adapter_meta: dict) -> bool:
        """Whether this invocation actually spawned a provider subprocess.

        `returncode` is the structural evidence: both adapters write it from the one
        place they call Popen, and the refusals that happen first — an oversized command
        line, a `provider_command` that is not the configured provider's binary — return
        before reaching it. Presence, not truthiness: a timeout records `returncode: None`
        and is very much a call that spent tokens.
        """
        return "returncode" in adapter_meta or bool(adapter_meta.get("timed_out"))

    def _stamp_provider_version(self, adapter_meta: dict) -> None:
        """Name the provider CLI release this invocation ran on, and whether it was tested.

        Every provider, not only codex: a release that changes behavior breaks the workflow
        from the provider's side (CASE-015), and the usage row is the trail a report reads.
        The version is read once per binary and cached on disk across runs
        (core/provider/versions.py). Both usage paths carry the stamp: the snapshot of each
        invocation, and the single-invocation call meta built from the adapter's attribute.
        """
        from core.provider import versions

        name = getattr(self.adapter, "adapter", None)
        command = getattr(self.adapter, "command", None)
        if not isinstance(name, str) or not isinstance(command, str):
            return
        if not isinstance(adapter_meta.get("provider_version"), str):
            adapter_meta["provider_version"] = versions.read_version(command)
        adapter_meta["provider_version_status"] = versions.status(name, adapter_meta.get("provider_version"))

    def _snapshot_invocation(self, prompt: str, result: dict) -> None:
        """Copy out what THIS provider invocation measured, before the next one lands.

        `adapter.last_call_meta` is a single attribute reassigned wholesale on every run,
        so a continuation's second call destroys the first call's numbers. The adapters
        carry the same warning about their own metadata for the same reason; this is that
        hazard one level up, where two runs of one command meet.

        An attempt that never reached a provider is not an invocation and gets no
        snapshot. `_per_invocation_metas` turns each snapshot into its own usage row, so
        recording one here for a call that spawned nothing would add a row for spend that
        did not happen — and, before the clearing in `_adapter_run`, would have carried
        the previous call's numbers into it.

        Instrumentation: it never raises into the call it is measuring.
        """
        try:
            adapter_meta = dict(getattr(self.adapter, "last_call_meta", None) or {})
            if not self._reached_a_provider(adapter_meta):
                return
            self._stamp_provider_version(adapter_meta)
            content = result.get("content") if isinstance(result, dict) else None
            self._call_metas.append(
                {
                    "adapter_meta": adapter_meta,
                    "prompt_chars": len(prompt),
                    "response_chars": len(content) if isinstance(content, str) else None,
                }
            )
        except Exception:
            pass

    def _per_invocation_metas(self, call_meta: dict) -> list[dict]:
        """The archived call meta, split into one row per provider invocation.

        Returns an empty list when the command made at most one provider call: a single
        invocation is fully described by the call meta that already exists, and emitting a
        near-duplicate of it would double every figure in the report.

        Built from the ALREADY-REDACTED call meta, overriding only counts and durations.
        Merging each invocation's raw adapter meta in here would route metadata into the
        usage stream around the redaction boundary the aggregate row goes through.
        """
        snapshots = list(self._call_metas)
        if len(snapshots) < 2:
            return []
        rows: list[dict] = []
        for index, snap in enumerate(snapshots):
            adapter_meta = snap.get("adapter_meta") or {}
            prompt_chars = snap.get("prompt_chars")
            resp_chars = snap.get("response_chars")
            row = {
                **call_meta,
                "prompt_chars": prompt_chars,
                "response_chars": resp_chars,
                "estimated_input_tokens": (
                    prompt_chars // 4 if isinstance(prompt_chars, int) else None
                ),
                "estimated_output_tokens": (
                    resp_chars // 4 if isinstance(resp_chars, int) else None
                ),
                "token_source": "estimated",
                "duration_seconds": adapter_meta.get("duration_seconds"),
                "provider_call_index": index,
                # Reset for this row: the aggregate may carry provider counts from the
                # last invocation, and inheriting them would credit one call's tokens to
                # both. Re-stamped below from this invocation's own usage, if it has any.
                "actual_input_tokens": None,
                "actual_output_tokens": None,
                "actual_reasoning_tokens": None,
                "actual_cached_input_tokens": None,
                # This invocation's own thread state: a continuation's first call can be
                # fresh while its retry resumes, and the aggregate carries only the last.
                # A bool, so taking it from the raw snapshot routes nothing around redaction.
                "resumed": (
                    adapter_meta.get("resumed")
                    if isinstance(adapter_meta.get("resumed"), bool)
                    else None
                ),
                "thread_changed": (
                    adapter_meta.get("thread_changed")
                    if isinstance(adapter_meta.get("thread_changed"), bool)
                    else None
                ),
                # The release this invocation ran on, from its own stamped snapshot. A
                # string read from a CLI's output, so it goes through redaction like the
                # aggregate did; the status is one of three fixed words.
                "provider_version": _redacted_text(adapter_meta.get("provider_version")),
                "provider_version_status": (
                    adapter_meta.get("provider_version_status")
                    if adapter_meta.get("provider_version_status") in ("stable", "untested", "unreadable")
                    else None
                ),
            }
            _apply_provider_usage(row, adapter_meta.get("provider_usage"))
            rows.append(row)
        # The saved-context figure is measured against the merged answer, and lands on the
        # final row alongside the digest that earned it. Without this the last row would be
        # compared against its own fragment of the reply.
        rows[-1]["final_response_chars"] = call_meta.get("response_chars")
        return rows

    def _record_usage(
        self,
        result: dict,
        project_root: Path,
        command: str,
        task: str,
        session_id: str,
    ) -> None:
        """Append this call to the usage stream, from the one place every call passes.

        Deliberately here rather than beside write_call_meta: the reuse path returns
        without ever building a call meta, and a reuse hit is the single most interesting
        row in the stream — it is a delegated call that cost nothing. Recording only the
        calls that reached a provider would systematically overstate cost per task by
        omitting exactly the cheap ones.

        Reads the sanitised result, so nothing redacted out of the response can reappear
        in telemetry.
        """
        try:
            measured = result
            if command == "verify" and result.get("ok"):
                meta = result.get("meta") or {}
                if meta.get("verdict") is None and meta.get("mode") != "quick":
                    # The verdict is derived one layer up, in job_lifecycle, so at this
                    # point every verify would record accepted=None — the metric would be
                    # structurally empty rather than conservative. Derived here from the
                    # same validator on the same content, read-only. Deliberately NOT via
                    # `_finalize_verify_result`: that one owns `meta.verdict`, and job_lifecycle
                    # is where it is supposed to be set. Measuring here must not pre-empt that
                    # decision, so the shallow copy keeps this reading out of the result the
                    # caller receives.
                    assessment = validate_verification_contract(result.get("content") or "")
                    measured = dict(result)
                    measured["meta"] = {**meta, "verdict": assessment["verdict"]}
            # The chain hop that makes plan/execute/verify one measurable task: a plan
            # records its derived id as the session's active chain, and the commands
            # that act on that plan adopt it instead of deriving their own. Everything
            # else (explore, analyze) stays per-task derived. Inside this try on
            # purpose — chain state is instrumentation, never a reason a call fails.
            correlation = None
            if command == "plan":
                correlation = correlation_id_for(project_root, session_id, task)
                stamp_task_chain(project_root, session_id, correlation)
            elif command in ("execute", "verify"):
                correlation = active_chain_correlation(project_root, session_id)
            spec = TaskSpec.build(
                command,
                task,
                session_id,
                project_root,
                model=None,
                correlation_id=correlation,
            )
            recorded_at = now_iso()
            # One row per provider invocation, still written from this single place. The
            # alternative — recording inside the provider flow — would lose the reuse hit,
            # which reaches here having made no provider call at all and is the one row in
            # the stream proving a delegated call can cost nothing.
            metas = self._per_invocation_metas(self._last_call_meta or {}) or [
                self._last_call_meta
            ]
            result_meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            # A browser run that ended before its review (incomplete, preflight, spec or
            # env refused, no request) reached no provider. A row for it carried no prompt
            # id and no tokens, so every reader counted it as a call of its own — the
            # statusline as one more second-agent call, the report as a judged verify with
            # zero tokens in the baseline. The browser run itself is the `e2e_run` quality
            # row; the task event below still records the verify.
            provider_less_run = (
                self._last_call_meta is None
                and not result_meta.get("reused_evidence")
                and result_meta.get("invocation") == "verify-browser"
            )
            if provider_less_run:
                metas = []
            last = len(metas) - 1
            payload = None
            for index, call_meta in enumerate(metas):
                # Everything derived from the FINAL answer belongs to the final row only.
                # The digest, the saved-context figure it implies, and the verdict are
                # properties of the result the command returned, not of each attempt that
                # went into it; repeating them per row would count one saving twice.
                measured_row = measured
                if index != last:
                    row_meta = dict(measured.get("meta") or {})
                    row_meta.pop("verdict", None)
                    row_meta.pop("redactions", None)
                    # The browser's own time and the runtime's tests belong to the command, once.
                    row_meta.pop("e2e", None)
                    row_meta.pop("runtime_tests", None)
                    row_meta.pop("facts", None)
                    measured_row = {**measured, "digest": None, "meta": row_meta}
                record = usage_from_result(
                    measured_row,
                    spec=spec,
                    call_meta=call_meta,
                    recorded_at=recorded_at,
                    reuse_outcome=self._reuse_outcome,
                    graph_status=self._graph_status,
                    graph_ms=self._graph_ms,
                )
                payload = record.to_dict()
                write_usage_record(project_root, payload)
            # The audit row is a strict subset, written separately rather than derived
            # from the usage stream at read time: telemetry may legitimately be pruned or
            # resampled, and an audit trail that disappears with it is not a trail.
            if payload is not None:
                write_audit_record(
                    project_root,
                    {
                        "at": payload["recorded_at"],
                        "session_id": payload["session_id"],
                        "correlation_id": payload["correlation_id"],
                        "prompt_id": payload["prompt_id"],
                        "command": payload["command"],
                        "provider": payload["provider"],
                        "model": payload["model"],
                        "ok": payload["ok"],
                        "error_type": payload["error_type"],
                        "verdict": payload["verdict"],
                        "redactions": payload["redactions"],
                        "project_root": str(project_root),
                    },
                )
            # One task event per command (DEC-015), carrying the verdict the reply declared
            # and the one the runtime derived from it. Only a derived `pass` closes a task
            # (DEC-020): a DONE the runtime marks `incomplete`, for not_verified gaps too,
            # leaves it open. A declared DONE with blocking findings counts as NEEDS FIX.
            task_verdict = derived_verdict = None
            if command == "verify" and result.get("ok"):
                assessment = validate_verification_contract(result.get("content") or "")
                derived_verdict = assessment["verdict"]
                task_verdict = "NEEDS FIX" if derived_verdict == "fail" else assessment["declared_verdict"]
            task_telemetry.record_skill(
                project_root,
                session_id,
                command,
                verdict=task_verdict,
                derived=derived_verdict,
                next_action=(result.get("digest") or {}).get("recommended_next_action")
                if isinstance(result.get("digest"), dict) else None,
                prompt_id=payload["prompt_id"] if payload else None,
            )
        except Exception:
            # Instrumentation, not the call. Broad on purpose: this runs on the return
            # path of every delegated command, and there is no failure here worth
            # converting a delivered answer into an error.
            pass

    def _record_failed_call(
        self,
        result: dict,
        project_root: Path,
        command: str,
        task: str,
        session_id: str,
    ) -> dict:
        """Usage for a call that burned tokens and then failed anyway.

        Deliberately NOT `_finalize_runtime_result`. That one also writes the command
        cache, workflow state and plan scope, and a menu or a refusal stored as
        `last_analyze_result` is a worse failure than the missing row this fixes: the
        next command would build on it as though it were evidence. Only the spending is
        recorded here.

        The rows this adds are the expensive ones. A call that times out has burned a
        full run of reasoning before being cut off, and it was previously the one shape
        of call that recorded nothing — so the budget ceiling stopped watching precisely
        where spend was highest. Returns its argument so a failing site stays a `return`.

        Not every `ok:false` reached a provider, though. Both adapters refuse some calls
        before spawning anything — an oversized command line, a `provider_command` that
        is not the configured provider's binary — and those cost nothing at all. Writing
        a row for them would invent spend, which is the same error as the missing row
        this method exists to fix, pointing the other way.

        A `returncode` key is the structural evidence that a subprocess ran: both adapters
        write it from the one place they call Popen, and the pre-spawn refusals return
        before reaching it. Presence, not truthiness — a timeout records `returncode:
        None` and is very much a call that spent tokens.
        """
        if not self._reached_a_provider(self._last_call_meta or {}):
            return result
        self._record_usage(result, project_root, command, task, session_id)
        return result

    def _maybe_reuse(
        self,
        project_root: Path,
        command: str,
        task: str,
        session_id: str,
        context: dict,
    ) -> dict | None:
        """Serve a fresh, identical prior evidence artifact instead of re-delegating.

        Returns a complete result dict on a hit, or None to fall through to delegation.
        Fail-open: the reuse path must never be why a call breaks — on ANY error we return
        None and normal delegation proceeds. The freshness guarantee lives in
        evidence_store.find_fresh (exact query + all anchors unchanged).
        """
        try:
            self._reuse_outcome = "error"
            hit, self._reuse_outcome = evidence_store.find_fresh_with_reason(
                project_root, command, task, context
            )
            if not hit:
                return None
            ap = hit.get("artifact_path")
            content, reuse_redactions = evidence_store.read_artifact_with_redactions(
                project_root, hit
            )
            if not content or not content.strip():
                self._reuse_outcome = "unreadable"
                return None
            certified_count, cited_count, scanned_all = _anchor_counts(hit)
            result = {
                "ok": True,
                "content": content,
                "evidence_ref": {
                    "artifact_path": ap,
                    "anchors": cited_count,
                    # Always equal to `anchors` on this path — reuse requires every anchor
                    # to have been certified — but reported anyway so a consumer can read
                    # one field shape whether the evidence is fresh or recalled.
                    "certified_anchors": certified_count,
                    "scanned_all": scanned_all,
                    "reused": True,
                },
                "meta": {
                    "reused_evidence": True,
                    "reused_from_session": hit.get("session"),
                    "captured_at": hit.get("captured_at"),
                    "command": command,
                },
            }
            _attach_redactions(result["meta"], reuse_redactions)
            if hit.get("digest") is not None:
                # Copied before the ratio is written into it: the stored digest belongs to
                # the artifact and is read again by the next reuse, and annotating it in
                # place would leave this run's numbers in the record.
                result["digest"] = dict(hit["digest"]) if isinstance(hit["digest"], dict) else hit["digest"]
                if isinstance(result["digest"], dict) and cited_count:
                    # The same shape as a fresh result's, so one reader handles both. On
                    # this path the two numbers are always equal — reuse requires it.
                    result["digest"]["anchors"] = {
                        "certified": certified_count,
                        "total": cited_count,
                        "scanned_all": scanned_all,
                    }
            return result
        except Exception:
            self._reuse_outcome = "error"
            return None

    def _run_delegated(
        self,
        route: dict,
        command: str,
        task: str,
        session: dict,
        session_id: str,
        project_root: Path,
        work_dir: str | None,
        on_progress,
        session_manager,
        *,
        prompt: str,
        prompt_meta: dict,
        lock_claim: dict | None,
        record_failure: bool = True,
    ) -> dict:
        """One delegated call, from prompt handoff to a judged reply.

        Everything between "the prompt is built" and "the reply is usable" lives
        here: the handoff file, adapter binding, the provider run, the single bounded
        continuation, call-meta archiving, session persistence, redaction audit, and
        the guards that turn a menu or a refusal into `invalid_evidence`. What it does
        NOT do is the role-shaped bookkeeping around a public command — fact ingest,
        evidence indexing, fan-out capability, command cache — which stays in
        `execute()` so an internal caller (a verify stage that needs a second
        delegated reply) can run a call without teaching the fact store about it.

        Returns the result dict. Any `ok: false` here is already recorded for usage
        where a provider was reached, so a caller may return it as-is. The prompt id
        of the archived call is on `self._last_call_meta["prompt_id"]`.

        `record_failure=False` is for a caller that keeps going after a failed call and
        finalises one result for the whole command (the e2e stages). Usage rows are
        written per snapshot in `_call_metas`, so recording here AND at that caller's
        finalisation would bill every invocation so far twice. Such a caller owns the
        recording: it either finalises, or calls `_record_failed_call` itself before
        returning a failure.
        """
        record_failed = (
            self._record_failed_call if record_failure else (lambda result, *_: result)
        )
        handoff = write_prompt_handoff(
            project_root,
            command,
            session_id,
            prompt,
            lock_claim=lock_claim,
        )
        if not handoff.get("ok"):
            return handoff

        # Storage that cannot take the provider thread id must fail the call HERE, before
        # anything is spent. codex and agy report their id mid-stream, after the work has
        # started; a write that failed there used to vanish inside the adapter, leaving a
        # paid-for call with no thread to resume or continue.
        ensure_writable = getattr(session_manager, "ensure_writable", None)
        if ensure_writable is not None:
            try:
                ensure_writable(session)
            except Exception as exc:  # noqa: BLE001 — becomes a named error, not a crash
                return make_error(
                    "session_capture_failed",
                    "session record is not writable; the provider thread id could not be kept",
                    next_action=(
                        "Check write access to .workflow/data/sessions and that no other "
                        "process holds the session file, then retry; the delegated task "
                        "was not started."
                    ),
                    meta={"error": f"{type(exc).__name__}: {exc}"},
                )

        # Late binding: until here the adapter could only be the import-time default.
        self.adapter = self._adapter_for(project_root)
        # `or` rather than a dict default: the route always carries both keys, and a null
        # value means "this provider has none" — not "fall back to another provider's".
        self.adapter.command = route.get("provider_command") or getattr(
            self.adapter, "command", None
        )
        self.adapter.agent = route.get("provider_agent") or getattr(
            self.adapter, "agent", None
        )
        # Plain assignment, no `or` chain: None here means "send no effort flag", and
        # inheriting the adapter's import-time value would make a config that cleared
        # `effort` keep sending the one it just removed.
        self.adapter.effort = route.get("effort")
        self.adapter.timeout_seconds = route.get(
            "timeout_seconds", getattr(self.adapter, "timeout_seconds", 0)
        )
        self.adapter.no_timeout = (
            self.adapter.timeout_seconds is None or self.adapter.timeout_seconds <= 0
        )
        if route.get("bootstrap_timeout_seconds") is not None:
            self.adapter.bootstrap_timeout_seconds = route["bootstrap_timeout_seconds"]
        if route.get("poll_interval_seconds") is not None:
            self.adapter.poll_interval = route["poll_interval_seconds"]
        # Only the adapter's poll loop can emit liveness while opencode blocks.
        self.adapter.on_progress = on_progress

        # Resume only a thread the selected provider issued. Bound here, after late adapter
        # binding and before the session is copied for the adapter, because this is the
        # first point that knows which provider the call goes to.
        if session_manager is not None:
            # The preflight above proved the record writable a moment ago; a write that
            # fails here anyway is the same named failure, with the task still not started,
            # never a raw exception out of the executor.
            try:
                session_manager.bind_provider(session, route.get("provider"))
            except OSError as exc:
                self.adapter.on_progress = None
                return make_error(
                    "session_capture_failed",
                    "session record could not be bound to the selected provider",
                    next_action=(
                        "Check write access to .workflow/data/sessions and that no other "
                        "process holds the session file, then retry; the delegated task "
                        "was not started."
                    ),
                    meta={"error": f"{type(exc).__name__}: {exc}"},
                )
        else:
            bind_provider(session, route.get("provider"))
        adapter_session = dict(session)
        adapter_session["session_id"] = session_id

        # The id the provider handed over but storage has not confirmed. Tracked apart from
        # `session`, because `update_provider_session_id` sets the id on the dict BEFORE it
        # writes: a failed write left the dict looking persisted, and the post-run save
        # below skipped exactly the id that needed it.
        unsaved_session: dict = {}

        def persist_new_session(provider_session_id: str) -> None:
            adapter_session["provider_session_id"] = provider_session_id
            if session_manager is None:
                record_provider_session(session, provider_session_id)
                return
            try:
                session_manager.update_provider_session_id(
                    session, provider_session_id
                )
                unsaved_session.clear()
            except Exception as exc:  # noqa: BLE001 — retried after the run
                unsaved_session["id"] = provider_session_id
                unsaved_session["error"] = f"{type(exc).__name__}: {exc}"

        session_callback_bound = False
        try:
            if hasattr(self.adapter, "on_session_created"):
                session_callback_bound = True
                self.adapter.on_session_created = persist_new_session
        except Exception:
            session_callback_bound = False
        try:
            self.adapter.last_call_meta = {}
        except Exception:
            pass
        continuation_meta: dict = {}
        # Held across the continuation so the reply that failed the contract can be
        # archived even when the merge discards it.
        first_reply = ""
        try:
            result = self._adapter_run(
                prompt,
                adapter_session,
                route.get("model"),
                work_dir,
            )
            result, _ = _sanitize_result(result)
            self._snapshot_invocation(prompt, result)

            # One bounded continuation. A reply that never reached its contract is not the
            # same failure as a refusal or a dead call: the session is alive and holding the
            # work, so asking for the missing block costs one call and saves the whole read.
            # Strictly once — a second agent that cannot produce the contract twice will not
            # produce it on the tenth try, and a loop here would spend quota discovering that.
            gap = _contract_gap(command, route.get("role"), result)
            if gap:
                first_reply = result.get("content") or ""
                continuation_meta = {
                    "continuation_reason": gap["reason"],
                    "continuation_missing": gap["missing"],
                    "continuation_attempts": 0,
                    "continuation_recovered": False,
                }
                # Without a captured provider session the follow-up would open a NEW thread,
                # which has none of the work and would simply answer from nothing.
                if adapter_session.get("provider_session_id"):
                    continuation_meta["continuation_attempts"] = 1
                    continuation_text = _continuation_prompt(command, gap)
                    retry = self._adapter_run(
                        continuation_text,
                        adapter_session,
                        route.get("model"),
                        work_dir,
                    )
                    retry, _ = _sanitize_result(retry)
                    # Snapshotted whether or not the retry is kept. A follow-up that failed
                    # the contract still spent its tokens, and a stream that only records
                    # the attempts that worked reports the cheap half of the bill.
                    self._snapshot_invocation(continuation_text, retry)
                    if retry.get("ok"):
                        merged = {
                            **retry,
                            "content": _merge_continuation(
                                result.get("content"), retry.get("content")
                            ),
                        }
                        # Prefer the merge: it carries both halves. Fall back to the
                        # follow-up alone only if the join itself reads as damaged, so a
                        # malformed first reply can never block a recovery that worked.
                        for candidate, joined in ((merged, True), (retry, False)):
                            if _contract_gap(
                                command, route.get("role"), candidate
                            ):
                                continue
                            continuation_meta["continuation_recovered"] = True
                            continuation_meta["continuation_first_reply_chars"] = len(
                                result.get("content") or ""
                            )
                            continuation_meta["continuation_merged"] = joined
                            result = candidate
                            break
                else:
                    continuation_meta["continuation_skipped"] = (
                        "no provider_session_id captured — a follow-up would start an "
                        "empty thread instead of continuing this one"
                    )
        finally:
            self.adapter.on_progress = None
            if session_callback_bound:
                try:
                    self.adapter.on_session_created = None
                except Exception:
                    pass
            # The whole telemetry block is guarded. It runs in `finally`, so anything it
            # raised — a non-dict handoff meta, a redaction failure, a write error outside
            # write_call_meta's own I/O guard — REPLACED the provider's result or its real
            # exception. Telemetry may be lost; the answer and the original error may not.
            call_meta_error = None
            try:
                # Archive exit, duration, kill, and stderr metadata for failure diagnosis,
                # plus minimal cost telemetry. Char counts are exact; token counts are
                # rough len/4 estimates and are labelled token_source=estimated so they are
                # never confused with provider-reported actuals (those would land in
                # actual_input_tokens/actual_output_tokens with token_source=provider).
                _result = locals().get("result")
                _content = _result.get("content") if isinstance(_result, dict) else None
                _prompt_chars = len(prompt)
                _resp_chars = len(_content) if isinstance(_content, str) else None
                _adapter_meta = getattr(self.adapter, "last_call_meta", None) or {}
                if not self._reached_a_provider(_adapter_meta) and self._call_metas:
                    # The LAST invocation reached no provider — a continuation refused before
                    # Popen is the case — but an earlier one did, and its numbers are the only
                    # measurement this command has. Reading the cleared attribute here would
                    # write `token_source: estimated` over a call that was actually counted,
                    # which is a silent downgrade: the row still looks like a row.
                    _adapter_meta = self._call_metas[-1].get("adapter_meta") or {}
                # The snapshot carries the version stamp; the live attribute does not, and a
                # single-invocation command is described by this row alone. Stamped on a copy,
                # never on the adapter's own dict, and never at the cost of the row.
                _adapter_meta = dict(_adapter_meta)
                if self._reached_a_provider(_adapter_meta):
                    try:
                        self._stamp_provider_version(_adapter_meta)
                    except Exception:
                        pass
                adapter_meta, meta_redactions = redact_value(
                    _without_raw_args(_adapter_meta)
                )
                if not isinstance(adapter_meta, dict):
                    adapter_meta = {}
                call_meta = {
                    "command": command,
                    "role": route.get("role"),
                    "model": route.get("model"),
                    "timeout_seconds": self.adapter.timeout_seconds,
                    # The effort this adapter passed; None is the provider's own default,
                    # whatever that is configured to outside this runtime.
                    "effort": getattr(self.adapter, "effort", None),
                    "prompt_chars": _prompt_chars,
                    "response_chars": _resp_chars,
                    "estimated_input_tokens": _prompt_chars // 4,
                    "estimated_output_tokens": (
                        _resp_chars // 4 if _resp_chars is not None else None
                    ),
                    "token_source": "estimated",
                    **self._config_provenance(project_root),
                    **prompt_meta,
                    **adapter_meta,
                    **continuation_meta,
                }
                call_meta, persisted_meta_redactions = redact_value(call_meta)
                _attach_redactions(
                    call_meta, [*meta_redactions, *persisted_meta_redactions]
                )
                call_meta["prompt_id"] = handoff.get("meta", {}).get("prompt_id")
                # Applied after the literal above, not inside it: `**adapter_meta` merges the
                # adapter's own keys last, so a token_source written earlier would be decided
                # by merge order rather than by what was actually measured.
                _apply_provider_usage(call_meta, adapter_meta.get("provider_usage"))
                # Same reason, and the field usage rows were missing entirely: the route knows
                # which provider was picked, but nothing carried the name down, so every row
                # in the stream said `provider: null`. That is readable while one provider is
                # measured and the rest are not — a report can then show both `provider` and
                # `estimated` in `token_source` without being able to say which provider is
                # which half.
                call_meta["provider"] = route.get("provider")
                call_meta["provider_command"] = route.get("provider_command")
                call_meta["provider_boundary"] = provider_boundary(route.get("provider"))
                if self._served_facts is not None:
                    call_meta["served_facts"] = self._served_facts
                self._last_call_meta = call_meta
                # `_call_metas` deliberately keeps the RAW snapshots here. Expansion happens
                # once, in `_record_usage`; expanding into the same attribute would feed the
                # expansion its own output on the second pass and blank every count in it.
                write_call_meta(
                    project_root,
                    handoff.get("meta", {}).get("prompt_id"),
                    session_id,
                    call_meta,
                )
            except Exception as telemetry_exc:  # noqa: BLE001 — recorded, never raised
                call_meta_error = f"{type(telemetry_exc).__name__}: {telemetry_exc}"
                # Never leave the previous call's meta standing in for this one.
                self._last_call_meta = {
                    "command": command,
                    "role": route.get("role"),
                    "provider": route.get("provider"),
                    "call_meta_error": call_meta_error,
                }

        if call_meta_error:
            result.setdefault("meta", {})["call_meta_error"] = call_meta_error
        if continuation_meta:
            # Rides out on the result too, not just the archived call meta: whether an
            # answer arrived first-try or needed a nudge changes how much to trust it.
            result.setdefault("meta", {}).update(continuation_meta)

        write_response_snapshot(
            project_root,
            result.get("content") or "",
            prompt_id=handoff.get("meta", {}).get("prompt_id"),
            session_id=session_id,
        )
        # Written whenever a continuation was needed, not only when it discarded the first
        # reply: a merged result no longer shows where the seam was, and the failing half is
        # what a later diagnosis has to read.
        write_first_reply(
            project_root,
            first_reply,
            prompt_id=handoff.get("meta", {}).get("prompt_id"),
            session_id=session_id,
        )

        provider_session_id = result.get("meta", {}).get(
            "provider_session_id"
        ) or adapter_session.get("provider_session_id") or session.get(
            "provider_session_id"
        )
        if unsaved_session or (
            result.get("ok")
            and provider_session_id
            and not session.get("provider_session_id")
        ):
            provider_session_id = unsaved_session.get("id") or provider_session_id
            if session_manager is not None:
                try:
                    session_manager.update_provider_session_id(
                        session, provider_session_id
                    )
                    unsaved_session.clear()
                except Exception as exc:  # noqa: BLE001 — surfaced on the result below
                    unsaved_session["id"] = provider_session_id
                    unsaved_session["error"] = f"{type(exc).__name__}: {exc}"
            else:
                record_provider_session(session, provider_session_id)
        if unsaved_session:
            # Both writes failed. The answer stands — throwing away paid evidence would not
            # bring the thread back — but the loss is named in its own field: no resume, no
            # continuation, and the next call starts a fresh thread.
            meta = result.setdefault("meta", {})
            meta["session_persisted"] = False
            meta["session_persist_error"] = unsaved_session.get("error")
        # On the result, not only in the archive: the reader deciding how far to trust this
        # evidence — or whether to look at `git status` — is the main agent, not doctor.
        boundary = provider_boundary(route.get("provider")) if route.get("provider") else None
        if boundary is not None:
            result.setdefault("meta", {})["provider_boundary"] = boundary
        if boundary is not None and boundary["writes"] == "unbounded":
            result["meta"]["provider_write_warning"] = (
                f"{route.get('provider')} can write to the project; nothing stopped it, "
                "the working-tree guard only detects a write after the fact"
            )

        redactions = (result.get("meta") or {}).get("redactions")
        self._audit_redactions(
            project_root, session_id, command, redactions or []
        )

        if not result.get("ok"):
            return record_failed(
                result, project_root, command, task, session_id
            )

        # Verify sits in the `verification` role alongside init/doctor/submit, which have no
        # second_agent reply to judge — hence the command check rather than a role check.
        # Without this, a menu or a refusal returned for /.verify skipped the guard entirely
        # and arrived as ok:true with an `incomplete` verdict, which reads as work done badly
        # rather than as a proxy that never answered.
        verify_meta = result.get("meta") or {}
        if (
            command == "verify"
            and verify_meta.get("mode") != "quick"
            and "quick_verify" not in verify_meta
            and "[verification]" not in (result.get("content") or "").lower()
        ):
            attempted = continuation_meta.get("continuation_attempts")
            detail = (
                "second_agent returned no [VERIFICATION] block (menu/refusal/question), "
                "not a verification"
            )
            if attempted:
                detail += "; a continuation in the same session was requested and still returned none"
            elif continuation_meta.get("continuation_skipped"):
                detail += (
                    f"; no continuation was possible ({continuation_meta['continuation_skipped']})"
                )
            # Recorded despite the adapter having succeeded: the provider ran, the tokens
            # are spent, and it is this guard rather than the provider that judged the
            # reply unusable. A row omitted here bills nothing for the most wasteful
            # outcome there is — a full call whose answer is thrown away.
            return record_failed(
                make_error(
                    "invalid_evidence",
                    detail,
                    next_action="STOP. Warn user [PROXY GAGAL]. Do NOT auto-fallback. Ask user: retry or /.local? (yes/no).",
                    meta=verify_meta,
                    raw_preview=(result.get("content") or "")[:240],
                ),
                project_root,
                command,
                task,
                session_id,
            )

        if route["role"] in ("exploration", "reasoning"):
            body = (result.get("content") or "").lower()
            if not any(m in body for m in _EVIDENCE_MARKERS):
                # Only reachable once the continuation above has already been tried and
                # failed (or was impossible for want of a session to continue). The detail
                # matters to the user: "it would not answer" and "it was never asked twice"
                # call for different next steps.
                attempted = continuation_meta.get("continuation_attempts")
                detail = (
                    "second_agent returned non-evidence output (menu/refusal/question), "
                    "not analysis"
                )
                if attempted:
                    detail += "; a continuation in the same session was requested and still returned none"
                elif continuation_meta.get("continuation_skipped"):
                    detail += (
                        f"; no continuation was possible ({continuation_meta['continuation_skipped']})"
                    )
                return record_failed(
                    make_error(
                        "invalid_evidence",
                        detail,
                        next_action="STOP. Warn user [PROXY GAGAL]. Do NOT auto-fallback. Ask user: retry or /.local? (yes/no).",
                        meta=result.get("meta", {}),
                        raw_preview=(result.get("content") or "")[:240],
                    ),
                    project_root,
                    command,
                    task,
                    session_id,
                )
        return result

    def _build_delegated_prompt(
        self,
        route: dict,
        command: str,
        task: str,
        session_id: str,
        project_root: Path,
        *,
        has_facts: bool = False,
        has_knowledge: bool = False,
        has_leads: bool = False,
        fanout: bool = False,
        e2e_evidence: str | None = None,
        has_e2e_knowledge: bool = False,
    ) -> tuple[str, dict]:
        """The prompt for one delegated call, sized to the provider's transport.

        Returns (prompt, prompt_meta). Split out of `execute()` so a verify stage can
        build a second prompt on the same route without re-deriving the transport cap.
        """
        runtime_dir = str(workflow_paths(project_root, session_id)["runtime_dir"])

        prompt_meta: dict = {}
        # The route names the configured provider, but an INJECTED adapter overrides that
        # choice without touching the route — so sizing off the route alone would measure
        # one provider's transport and send through another's. The adapter's own name wins
        # when it was handed in; in every other case the two agree by construction, since
        # `_adapter_for()` resolves from the same config the router read.
        _sizing_provider = route.get("provider")
        if self._adapter_override:
            _sizing_provider = getattr(self.adapter, "adapter", None) or _sizing_provider
        _transport = transport_budget(_sizing_provider)
        if _transport and _transport.get("kind") == "argv":
            # Measured, not assumed. A flat reserve is a guess about values the route is
            # holding right here: an absolute provider_command path, a model id, an agent
            # name, an effort flag, a session id. Config can make any of them long, and a
            # guess that is too small does not degrade — it produces a prompt the adapter
            # refuses. `len(v)+3` mirrors the adapters' own `_too_long_for_cmd` accounting;
            # the trailing slack covers the subcommand and flag names neither side varies.
            _transport["reserved"] = 160 + sum(
                len(str(value)) + 3
                for value in (
                    route.get("provider_command"),
                    route.get("provider_agent"),
                    route.get("model"),
                    route.get("effort"),
                    session_id,
                )
                if value
            )
        prompt = build_prompt(
            role=route["role"],
            task=task,
            session_id=session_id,
            command=command,
            project_root=str(project_root),
            runtime_dir=runtime_dir,
            has_facts=has_facts,
            has_knowledge=has_knowledge,
            has_leads=has_leads,
            subagent_fanout=fanout,
            e2e_evidence=e2e_evidence,
            has_e2e_knowledge=has_e2e_knowledge,
            declared_tools=route.get("declared_tools"),
            meta_sink=prompt_meta,
            # Sized from the transport, not from one constant shared by every provider.
            # The adapter is still bound late (below), but the provider NAME is settled
            # here by the route — which is all the sizing needs, and lets the prompt be
            # built once, correctly, rather than built and then re-measured.
            transport=_transport,
        )
        return prompt, prompt_meta

    def execute(
        self,
        command: str,
        task: str,
        session: dict,
        work_dir: str | None = None,
        model: str | None = None,
        on_progress=None,
        allow_reuse: bool = True,
        session_manager=None,
        workflow_session_id: str | None = None,
        *,
        _runtime_lock: dict | None = None,
        _resolved_route: dict | None = None,
    ) -> dict:
        """One delegated dispatch, mirrored into `.workflow/current/` while it runs.

        The mirror is written once per dispatch, by the outer call: the lock-holding
        re-entry below (`_runtime_lock` set) is the same dispatch, not a new one.
        """
        if _runtime_lock is not None:
            return self._execute(
                command, task, session, work_dir, model, on_progress, allow_reuse,
                session_manager, workflow_session_id,
                _runtime_lock=_runtime_lock, _resolved_route=_resolved_route,
            )
        from core.workspace import current

        project_root = detect_project_root(work_dir)
        session_id = str(workflow_session_id or session["session_id"])
        normalized = command.strip().lower()
        current.start(project_root, session_id, normalized, task)

        def mirrored(beat):
            if isinstance(beat, dict):
                current.progress(project_root, session_id, beat)
            if on_progress is not None:
                on_progress(beat)

        result: dict = {}
        try:
            result = self._execute(
                command, task, session, work_dir, model, mirrored, allow_reuse,
                session_manager, workflow_session_id,
                _resolved_route=_resolved_route,
            )
            return result
        finally:
            current.finish(project_root, session_id, result)

    def _execute(
        self,
        command: str,
        task: str,
        session: dict,
        work_dir: str | None = None,
        model: str | None = None,
        on_progress=None,
        allow_reuse: bool = True,
        session_manager=None,
        workflow_session_id: str | None = None,
        *,
        _runtime_lock: dict | None = None,
        _resolved_route: dict | None = None,
    ) -> dict:
        normalized_command = command.strip().lower()
        self._last_call_meta = None
        self._call_metas = []
        self._served_facts = None
        # Per-call instrumentation for the usage row (v3.8.0 phase 1): why reuse missed,
        # and whether graph leads were there to use. None means the call never got as far
        # as asking — the runtime-only paths return before either question comes up.
        self._reuse_outcome = None
        self._graph_status = None
        self._graph_ms = None
        provider_session_id = session["session_id"]
        session_id = str(workflow_session_id or provider_session_id)
        effective_session_manager = (
            session_manager if session_manager is not None else self.session_manager
        )
        project_root = detect_project_root(work_dir)

        # No secret-path preflight here any more. It scanned the TASK TEXT, so it blocked
        # talking about a file rather than reading one — an audit that named `.env` in its
        # instructions was rejected while nothing stopped a run that never mentioned it.
        # The boundary now sits where it can be enforced: opencode `permission.read`/`grep`
        # deny rules, shipped in the project's opencode.json, which act on the actual tool
        # call. See dist/config/opencode/opencode.project.json.
        mode = verify_mode(project_root) if normalized_command == "verify" else None
        if mode == "syntax":
            result = quick_verify.run(project_root, session_id)
            result["meta"]["verify_mode"] = mode
            result["meta"]["command"] = normalized_command
            return _sanitize_result(result)[0]

        if _resolved_route is None:
            refusal = self._provider_config_refusal(project_root, normalized_command)
            if refusal is not None:
                return refusal
            try:
                route = self._router_for(project_root).route(
                    normalized_command, model_override=model
                )
            except ValueError as exc:
                # Two different refusals arrive as the same exception, and they need
                # opposite advice: an unsupported command is a typo, a disallowed provider
                # is a policy decision someone made on purpose. One next_action covering
                # both sent every allowlist denial off to check its spelling.
                denied = "allowlist" in str(exc)
                return make_error(
                    "routing_error",
                    str(exc),
                    next_action=(
                        "Select an allowed provider (/.provider), or widen "
                        "provider_allowlist in .workflow/second_agent.json."
                        if denied
                        else "Use a supported command (explore/plan/analyze/verify/sweep)."
                    ),
                    meta={"command": normalized_command},
                )
        else:
            route = dict(_resolved_route)

        budget = self._budget_state(project_root, session_id)
        if budget.get("exceeded"):
            return make_error(
                "budget_exceeded",
                (
                    f"session {session_id} has spent {budget['spent']} of its "
                    f"{budget['limit']} token budget (estimated, chars//4)"
                ),
                next_action=(
                    "Raise session_token_budget in .workflow/second_agent.json, or start a "
                    "fresh session. Retrying this one will be refused again."
                ),
                meta={"command": normalized_command, "budget": budget},
            )

        lock_path = workflow_paths(project_root, session_id)["lock"]
        if _runtime_lock is None:
            lock_claim = acquire_runtime_lock(
                lock_path, normalized_command, session_id
            )
            if not lock_claim.get("ok"):
                payload = lock_claim.get("payload") or {}
                holder = payload.get("session_id") or "unknown"
                result = make_error(
                    "runtime_lock",
                    f"runtime lock active for session {holder}",
                    next_action=(
                        "Wait for the in-flight delegated call on this session to finish, "
                        "then retry; if its owner is gone, inspect the runtime lock."
                    ),
                    meta={"lock": payload, "lock_path": str(lock_path)},
                )
                return _sanitize_result(result)[0]
            try:
                return self.execute(
                    command,
                    task,
                    session,
                    work_dir,
                    model,
                    on_progress,
                    allow_reuse,
                    effective_session_manager,
                    session_id,
                    _runtime_lock=lock_claim,
                    _resolved_route=route,
                )
            finally:
                release_runtime_lock(
                    lock_path, session_id, lock_claim.get("token")
                )

        lock_token = str(_runtime_lock.get("token") or "")
        if not lock_token or not runtime_lock_owned(
            lock_path, session_id, lock_token
        ):
            result = make_error(
                "runtime_lock",
                "runtime lock ownership was lost before delegation",
                next_action="Retry the command after the current session owner finishes.",
                meta={"lock_path": str(lock_path)},
            )
            return _sanitize_result(result)[0]

        if normalized_command == "verify-browser":
            # The request-driven browser pipeline, under the one lock this call already
            # holds. Stage 1 and 3 go through `_run_delegated` directly — never back through
            # `execute()`, whose role bookkeeping (facts, evidence index, fan-out) belongs
            # to public commands. Stage 3 takes `verify`'s route so a per-command model
            # choice for verification applies to the review too.
            from core.evidence.e2e import runner as e2e_runner

            try:
                verify_route = self._router_for(project_root).route("verify", model_override=model)
            except ValueError:
                verify_route = route
            bound = bind_session(project_root, session_id)
            # Once per command: the run is finalised, and its usage row written, once.
            self._graph_status, self._graph_ms = graph_state_for_call(project_root)
            # The e2e_spec draft takes the exploration route but is never offered reuse;
            # left unset, every draft counted as an `unrecorded` reuse-eligible command.
            self._reuse_outcome = "not_offered"
            result = e2e_runner.run(
                self,
                project_root=project_root,
                session_id=session_id,
                session=session,
                task=task,
                work_dir=work_dir,
                on_progress=on_progress,
                session_manager=effective_session_manager,
                lock_claim=_runtime_lock,
                verify_route=verify_route,
            )
            if not result.get("ok"):
                return result
            # A run is finalised as `verify` (verdict, acceptance, usage baseline); a draft
            # as `verify-browser`, so it never counts as a verification.
            finalized_as = str((result.get("meta") or {}).get("command") or normalized_command)
            return self._finalize_runtime_result(
                result, project_root, finalized_as, task, session_id, bound, False
            )

        known_facts = None
        known_knowledge = None
        # Fact lifecycle counts for this command, read and ingest side. One dict so the
        # usage row can say how many facts were offered, served, refused and learned.
        fact_stats: dict = {}
        graph_leads = None
        fanout = False
        if graph_leads_enabled(project_root):
            graph_leads, self._graph_status, self._graph_ms = graph_leads_for_call(
                project_root, task
            )
        else:
            self._graph_status = "disabled"
        if route["role"] in ("exploration", "reasoning"):
            facts = fact_store.load_relevant(
                project_root, task, role=route["role"], stats=fact_stats
            )
            self._served_facts = fact_store.served_fact_refs(facts)
            known_facts = [
                f"{f['claim']} [{f['file']}:{f['line']}]"
                for f in facts
                if f.get("file")
            ] or None
            fanout = (
                subagent_fanout_enabled(project_root)
                and fanout_capability(project_root) is not False
            )
            # Promoted knowledge rides the same roles as facts. Wrapped broadly because
            # it reaches out to git: a repository that cannot answer must degrade this
            # call to "no knowledge this time", never fail a delegated call that would
            # otherwise have succeeded without it.
            limit = knowledge_relevant_limit(project_root)
            if limit:
                try:
                    known_knowledge = knowledge_retrieve.load_relevant(
                        project_root,
                        task,
                        production_ref=production_branch(project_root),
                        limit=limit,
                    ) or None
                except Exception:
                    known_knowledge = None
        evidence_context = _evidence_context(
            route, fanout, graph_leads, known_facts, known_knowledge
        )

        # Sidecars keep dynamic evidence off the Windows-limited command line; the
        # command prompt carries only the task and paths needed to read them.
        write_evidence_sidecars(
            project_root, session_id, graph_leads, known_facts, known_knowledge
        )

        prompt, prompt_meta = self._build_delegated_prompt(
            route,
            normalized_command,
            task,
            session_id,
            project_root,
            has_facts=bool(known_facts),
            has_knowledge=bool(known_knowledge),
            has_leads=bool(graph_leads and graph_leads.get("files")),
            fanout=fanout,
        )

        # A cut instruction is answered in full confidence, so the only place it can be
        # caught is before the call. Past the hard ratio the second agent would be
        # answering a materially different question — cheaper to refuse than to deliver
        # a confident answer to it.
        if prompt_meta.get("task_truncated"):
            lost = prompt_meta["task_original_chars"] - prompt_meta["task_kept_chars"]
            if lost / prompt_meta["task_original_chars"] > DEFAULT_TASK_TRUNCATION_HARD_RATIO:
                return make_error(
                    "task_truncated",
                    f"task lost {lost} of {prompt_meta['task_original_chars']} chars "
                    f"(cap {prompt_meta['task_kept_chars']}); too much of the instruction is gone "
                    "to trust an answer to it",
                    next_action=(
                        "Shorten the task to instructions only — move evidence, dumps, and "
                        "file contents out of it; the second agent gathers those itself."
                    ),
                    meta=dict(prompt_meta),
                )

        bound = bind_session(project_root, session_id)
        if not allow_reuse:
            self._reuse_outcome = "disabled"
        elif route["role"] not in ("exploration", "reasoning"):
            self._reuse_outcome = "not_offered"
        if allow_reuse and route["role"] in ("exploration", "reasoning"):
            reused = self._maybe_reuse(
                project_root,
                normalized_command,
                task,
                session_id,
                evidence_context,
            )
            if reused is not None:
                return self._finalize_runtime_result(
                    reused,
                    project_root,
                    normalized_command,
                    task,
                    session_id,
                    bound,
                    fanout,
                    audit_existing_redactions=True,
                )

        result = self._run_delegated(
            route,
            normalized_command,
            task,
            session,
            session_id,
            project_root,
            work_dir,
            on_progress,
            effective_session_manager,
            prompt=prompt,
            prompt_meta=prompt_meta,
            lock_claim=_runtime_lock,
        )
        if not result.get("ok"):
            return result
        if normalized_command == "verify":
            # The tests main_agent chose for this change, run here and written into the
            # review's [VERIFICATION] block, which the second agent never saw them in.
            result = _apply_runtime_tests(project_root, session_id, result, on_progress)
        prompt_id = (self._last_call_meta or {}).get("prompt_id")

        digest = extract_digest(result.get("content") or "")
        if digest is not None:
            result["digest"] = digest

        # Above the role branch, and above the contract-warning branch inside it. The cap is
        # what lets main_agent judge how much room a task had, and the cases that need it
        # most are the ones both branches skip: a clean call, where nothing else implies the
        # number, and verify/sweep, whose role never enters the evidence branch at all. It
        # was reaching call.meta.json and no reader.
        result.setdefault("meta", {}).update(
            {k: v for k, v in prompt_meta.items() if k.startswith("task_")}
        )

        # What the working tree actually costs a reviewer right now. Instruction length
        # used to be the only thing bounded here, and it was the wrong parameter: a task
        # does not widen because its wording is long, it widens because it ends up
        # touching more files than anyone agreed to. This is that measurement, carried on
        # every delegated result so main_agent sees drift without asking for it.
        width = _scope_width(project_root, session_id)
        if width:
            result["meta"]["scope_width"] = width

        if route["role"] in ("exploration", "reasoning"):
            # Reported, never enforced: a contract miss is worth surfacing, but it says
            # nothing about whether the evidence underneath is correct.
            issues = contract_warnings(normalized_command, result.get("content") or "")
            if prompt_meta.get("task_truncated"):
                issues.append(
                    {
                        "kind": "task_truncated",
                        "detail": (
                            f"{prompt_meta['task_original_chars'] - prompt_meta['task_kept_chars']}"
                            f" chars cut from the instruction (cap {prompt_meta['task_kept_chars']})"
                        ),
                    }
                )
            if width and (width.get("wide") or width.get("outside_scope")):
                outside = width.get("outside_scope") or []
                detail = f"{width['files']} file(s), {width['lines']} line(s) changed"
                if outside:
                    detail += (
                        f"; {len(outside)} outside the plan's editable scope"
                        f" ({', '.join(outside[:3])}"
                        f"{', ...' if len(outside) > 3 else ''})"
                    )
                issues.append({"kind": "scope_wide", "detail": detail})
            if issues:
                result.setdefault("meta", {})["contract_warnings"] = issues

            # A damaged payload needs its own field, not a line inside a warnings list.
            # The confidence cap below cannot carry this one: capping edits the digest, and
            # the worst case here IS a missing digest — the run that returned content cut
            # mid-word had nothing left to cap, so the only surviving signal was ok:true.
            damaged = [issue["kind"] for issue in issues if issue["kind"] in STRUCTURAL_KINDS]
            if damaged:
                result.setdefault("meta", {})["content_incomplete"] = damaged

            # Conditions the second agent cannot grade itself, applied to the number
            # main_agent reads. Each signal is already computed above; this is the wiring.
            caps: list[tuple[str, str]] = []
            if prompt_meta.get("task_truncated"):
                caps.append(("medium", "task was truncated before the call"))
            if (graph_leads or {}).get("stale"):
                caps.append(("medium", "dependency graph is stale"))
            for issue in issues:
                if issue["kind"] == "grounded_without_evidence":
                    caps.append(("low", "grounded claims carry no file:line"))
                elif issue["kind"] == "missing_fields":
                    caps.append(("medium", f"contract: {issue['detail']}"))
                elif issue["kind"] in STRUCTURAL_KINDS:
                    # Truncated output is not partially right — the reasoning that would
                    # have qualified the conclusion is the part that went missing.
                    caps.append(("low", f"output structurally incomplete: {issue['kind']}"))
                elif issue["kind"] == "trailing_non_evidence":
                    caps.append(
                        ("medium", "output closes by addressing the user, not on evidence")
                    )
                elif issue["kind"] == "scope_wide":
                    # Reasoning done over a tree that has already drifted past its plan is
                    # reasoning about a codebase nobody has described yet.
                    caps.append(("medium", "working tree is wider than the declared scope"))
            if _scope_incomplete(result.get("content") or ""):
                caps.append(("medium", "scope_not_covered lists in-scope ground the run could not reach"))
            gaps = scope_warnings(result.get("content") or "")
            if gaps:
                result.setdefault("meta", {})["scope_warnings"] = gaps
            if caps and digest is not None:
                result["digest"] = cap_confidence(digest, caps)

            # Keep prose and anchors derived from the same claims so presentation cannot
            # become a second source of truth.
            claims = readable_claims(result.get("content") or "")
            if claims:
                try:
                    claims = evidence_store.certify_claim_refs(project_root, claims)
                except Exception:
                    pass  # status is an annotation; its absence must not cost the claims
                result.setdefault("meta", {})["claims"] = claims
                unverified = [
                    claim["text"]
                    for claim in claims
                    if "unverified" in (claim.get("ref_status") or [])
                ]
                if unverified:
                    # Warning, not a cap: the anchor ratio already prices these refs in.
                    # This names the claims, so the reader knows which ones to distrust.
                    result["meta"].setdefault("contract_warnings", []).append(
                        {
                            "kind": "claim_ref_unverified",
                            "detail": (
                                f"{len(unverified)} grounded claim(s) cite a file:line "
                                "this project cannot open"
                            ),
                            "samples": unverified[:3],
                        }
                    )
            questions = parse_questions(result.get("content") or "")
            if questions["open_questions"] or questions["resolvable_uncertainties"]:
                result.setdefault("meta", {})["questions"] = questions

        if fanout:
            # Report what actually happened, not what was asked for. A run that was told
            # to fan out and did not is still a usable result — but calling it a fan-out
            # would make the next decision rest on work nobody did.
            content = result.get("content") or ""
            usage = detect_subagent_usage(content)
            meta = result.setdefault("meta", {})
            meta["subagent_used"] = usage["used"]
            meta["fanout_mode"] = usage["mode"]
            meta["subagent_fanout_clusters"] = usage["fanout_clusters"]
            meta["covered_clusters"] = usage["covered_clusters"]
            if usage["declared_clusters"] and not usage["used"]:
                meta["declared_clusters"] = usage["declared_clusters"]
            warning = _FANOUT_WARNINGS.get(usage["mode"])
            if usage.get("false_incapable_report"):
                # Say it plainly. The agent broke a rule its own prompt states, and the
                # reader needs to know the "declined" below is a downgrade, not a report.
                meta["false_incapable_report"] = True
                warning = (
                    "second_agent claimed it has no spawn tool while listing one in its "
                    "own tool inventory; counted as a decline, and fan-out was NOT "
                    "disabled. Spawning a read-only subagent writes nothing — being in a "
                    "read-only agent is not a reason to refuse `task`"
                )
            if warning:
                meta["subagent_warning"] = warning
            # Only a genuine tool absence latches capability off. A permission refusal is
            # repairable configuration and must not disable fan-out permanently.
            try:
                if usage["mode"] == FANOUT_INCAPABLE:
                    set_fanout_capability(project_root, False)
                elif usage["used"]:
                    set_fanout_capability(project_root, True)
            except Exception:
                pass

        if route["role"] in ("exploration", "reasoning"):
            # Fact ingestion is best-effort, but its failure remains visible.
            try:
                ingest_stats: dict = {}
                added = fact_store.ingest(
                    project_root, result.get("content") or "", session_id, stats=ingest_stats
                )
                result.setdefault("meta", {})["facts_ingested"] = added
                fact_stats.update(
                    ingested=added,
                    ingest_rejected_anchor=ingest_stats.get("rejected_anchor", 0),
                    ingest_rejected_length=ingest_stats.get("rejected_length", 0),
                )
            except Exception as exc:
                result.setdefault("meta", {})["fact_ingest_error"] = {
                    "error_type": "fact_ingest_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "next_action": "Report this: the fact store rejected the run's output; facts are not being learned.",
                }
            if fact_stats:
                result.setdefault("meta", {})["facts"] = dict(fact_stats)

            # Index the immutable per-run copy; response.last.md is mutable and cannot
            # identify which delegated response an evidence row certifies.
            try:
                if not prompt_id:
                    raise ValueError("prompt_id missing; immutable artifact unavailable")
                artifact_path = (
                    workflow_paths(project_root, session_id)["logs_dir"]
                    / str(prompt_id)
                    / "output.raw.md"
                )
                entry = evidence_store.record(
                    project_root,
                    normalized_command,
                    task,
                    session_id,
                    result.get("digest"),
                    artifact_path,
                    result.get("content") or "",
                    evidence_context,
                )
                certified_count, cited_count, scanned_all = _anchor_counts(entry)
                result["evidence_ref"] = {
                    "artifact_path": str(artifact_path),
                    "anchors": cited_count,
                    "certified_anchors": certified_count,
                    "scanned_all": scanned_all,
                    "reused": False,
                }
                # The same numbers, in the block the reader is told to read first.
                # `scanned_all` travels with them: without it `40/40` read as "every
                # citation checked" when it meant "the first forty checked".
                if result.get("digest") and cited_count:
                    result["digest"]["anchors"] = {
                        "certified": certified_count,
                        "total": cited_count,
                        "scanned_all": scanned_all,
                    }
                # The ladder lives in `contract.anchor_cap`, beside the cap it feeds.
                if result.get("digest") and cited_count:
                    cap = anchor_cap(certified_count, cited_count, scanned_all)
                    if cap:
                        result["digest"] = cap_confidence(result["digest"], [cap])
                uncertified = (entry.get("anchor_scan") or {}).get("uncertified") or []
                if uncertified:
                    result.setdefault("meta", {})["uncertified_anchors"] = uncertified
            except Exception as exc:
                result.setdefault("meta", {})["evidence_store_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )

        return self._finalize_runtime_result(
            result,
            project_root,
            normalized_command,
            task,
            session_id,
            bound,
            fanout,
        )
