"""Stage orchestration for /.verify-browser, called from `Executor.execute()`.

Every run reads one request (`core/evidence/e2e/request.py`), which itself starts from the
project's pinned `e2e` section in config.json. The request's phase decides how far it goes:

  draft  preflight  → a draft with status `blocked` and the failing check
         stage 1    → second_agent writes `[E2E SPEC]`; a proxy failure is returned as-is
                      so main_agent sees [PROXY GAGAL]; the spec is validated and written
                      to `draft.json` for the user to confirm. No browser starts.
  run    preflight  → `incomplete` with the failing check as reason
         spec       → the confirmed scenario from the request, validated again
         stage 2    → the supervised player; never ends the run by itself, its report does.
                      An environmental `incomplete` is retried up to settings.max_retries,
                      never a `fail` and never while allow_side_effects is true
         stage 3    → hybrid review over the compact evidence (skipped only when the
                      browser could not finish — nothing a reviewer says changes `incomplete`)
         normalise  → one canonical `[VERIFICATION]`, verdict set on meta before the shared
                      finaliser reads it

A draft is not a verification: its meta.command is `verify-browser` and it carries no
verdict. A run's meta.command is `verify`, so the verdict, exit code, and acceptance
metrics are the ones every verification uses.

The executor's `_run_delegated` is the only way a stage reaches a provider. Nothing
here calls `execute()` again, so the lock this call holds is the lock the stages use.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from exoma.core.evidence.e2e import existing_tests as e2e_existing
from exoma.core.evidence.e2e import knowledge as e2e_knowledge
from exoma.core.workspace import current as e2e_current
from exoma.core.evidence.e2e import redact as e2e_redact
from exoma.core.evidence.e2e import request as e2e_request
from exoma.core.evidence.e2e import tagging as e2e_tagging
from exoma.core.evidence.e2e.classify import NOT_COUNTED_REASONS, build_report, diagnose, failure_signature, repeat_bucket
from exoma.core.evidence.e2e.classify import diagnosis_for_cause
from exoma.core.evidence.e2e.fingerprint import project_fingerprint
from exoma.core.evidence.e2e.normalize import evidence_block, to_verification
from exoma.core.evidence.e2e.preflight import display_available, preflight
from exoma.core.evidence.e2e.spec import (
    MAX_REPAIR_ERRORS,
    env_references,
    ground_claims,
    parse_spec,
    spec_continuation_prompt,
    spec_gap,
    spec_repair_prompt,
    substitute_env,
    validate_existing_tests,
    validate_scenario,
)
from exoma.core.evidence.e2e.spec import error_category, upload_file_errors, validate_read_only_requests
from exoma.core.evidence.e2e.supervisor import run_player
from exoma.core.provider.result_prep import _sanitize_result
from exoma.core.evidence.contracts import correlation_id_for
from exoma.core.evidence.runtime_io import write_quality_record
from exoma.core.runtime.state import E2E_REPEAT_LIMIT, e2e_repeat_state, note_e2e_outcome
from exoma.core.workspace.workspace_paths import atomic_write_text, now_iso, workflow_paths
from exoma.utils.redact import redact_value

# Preflight refusals the repeat brake counts: the ones the environment owns, which no edit
# to the request can clear. `preflight` also returns `spec_invalid` for a base_url or write
# policy the run itself named, and that one is the caller's to fix by asking again.
_REPEATABLE_PREFLIGHT = frozenset({"playwright_missing", "browser_missing", "base_url_unreachable"})

FAKE_ENV = "WORKFLOW_E2E_FAKE"
SMOKE_ENV = "WORKFLOW_E2E_SMOKE"
# Free-form tag for evaluation datasets (plan Fase 0/5): set it while verifying one of the
# real changes under study, and the run's quality row carries it.
LABEL_ENV = "WORKFLOW_E2E_LABEL"
INVOCATION = "verify-browser"
# Hard ceiling on browsers started for one run, whatever settings.max_retries says. A
# retry only ever buys another sample of an environment that may be flaky; past a few
# samples the answer is not "run it again", it is that the environment is the finding.
MAX_RETRIES = 5
# Incomplete reasons a second attempt could plausibly resolve: the browser or the machine
# misbehaved. Deliberately excluded are playwright_missing, browser_missing,
# base_url_unreachable, spec_invalid, env_missing and player_unavailable — nothing about
# rerunning those changes, so a retry would only spend the user's time. `fail` is never
# retried at all: that verdict means the application broke the claim, and re-rolling a
# real bug until it hides is the one outcome this whole package exists to prevent.
RETRYABLE_REASONS = frozenset(
    {"harness_error", "unknown_origin", "stuck", "timeout", "output_truncated", "launch_failed"}
)
# The directory holding the `exoma` package: the player child runs `-m exoma...` from it.
_AGENT_ROOT = Path(__file__).resolve().parents[4]
# What the player child may inherit. Credentials reach it only inside the resolved
# scenario on stdin; the parent's environment is otherwise not its business.
_INHERITED_ENV = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "LANG",
    "LC_ALL",
    "PLAYWRIGHT_BROWSERS_PATH",
    "DISPLAY",
)
_UNPROVEN = "unproven"
# What to call a repeat streak that has no reason of its own to quote.
_RUNTIME_IDENTITY: list[str] = []


def runtime_identity() -> str:
    """This runtime, as the repeat brake needs to know it: version plus a digest of the
    browser package's source.

    The project fingerprint sees the application, never the runtime driving it — so a
    streak built on a runtime bug (a poll that held the page's events until the assertion
    had failed) survived the runtime fix and refused the run that would have shown it. The
    version alone is not enough: a fix made during development does not bump it.
    """
    if not _RUNTIME_IDENTITY:
        from exoma.config.settings import TOOL_VERSION

        digest = hashlib.sha256(TOOL_VERSION.encode("utf-8"))
        for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
            try:
                digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes())
            except OSError:
                digest.update(path.name.encode("utf-8") + b"\0unreadable")
        _RUNTIME_IDENTITY.append(f"{TOOL_VERSION}+{digest.hexdigest()[:12]}")
    return _RUNTIME_IDENTITY[0]


_REPEAT_PHRASE = {
    "app": "a failing application",
    "harness": "the same harness problem",
    "timeout": "a timeout",
}


def _result(content: str, verdict: str, e2e_meta: dict, warnings: list[dict]) -> dict:
    meta = {"command": "verify", "invocation": INVOCATION, "phase": "run", "verdict": verdict, "e2e": e2e_meta}
    if warnings:
        meta["contract_warnings"] = warnings
    return _sanitize_result({"ok": True, "content": content, "meta": meta})[0]


def _child_env(extra_path: str) -> dict:
    env = {name: os.environ[name] for name in _INHERITED_ENV if name in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = extra_path
    return env


def _run_kind(fake: str | None) -> str:
    """fake proves the pipeline, smoke proves the browser, project is the only kind that
    says anything about whether browser verification is worth having."""
    if fake:
        return "fake"
    if os.environ.get(SMOKE_ENV) == "1":
        return "smoke"
    return "project"


def _record_run(
    project_root: Path,
    session_id: str,
    task: str,
    e2e_meta: dict,
    verdict: str,
    started: float,
    report: dict | None,
    scenario: object,
) -> None:
    """One `kind: e2e_run` row on the quality stream (plan §24 metrics, derived later).

    Raw counts only; every rate is computed at read time in `core/audit/telemetry.py`, so a
    better definition re-reads history instead of invalidating it. Runs only — a draft
    exercised nothing.
    """
    report = report or {}
    origin = e2e_knowledge.origin_of(str((e2e_meta.get("config") or {}).get("base_url") or "")) or None
    run_kind = _run_kind(e2e_meta.get("fake"))
    network = _network_compact((report.get("network") or {}).get("summary"))
    if network is not None:
        # Read before this run's row is written, so "last run" is the one before it.
        # Not `network`: preflight already reports the write hosts and resolver pins there.
        e2e_meta["page_requests"] = {**network, "since_last_run": _network_diff(project_root, origin, run_kind, network)}
    claims = report.get("claims") or {}
    failures = report.get("failures") or []
    artifacts = report.get("artifacts") or []
    statuses = [claim.get("status") for claim in claims.values()]
    declared = len(claims) or (len(scenario.get("claims") or []) if isinstance(scenario, dict) else 0)
    stages = e2e_meta.get("stages") or []
    kept = [a for a in artifacts if a.get("kind") in ("html", "screenshot", "trace") and not a.get("pruned")]
    write_quality_record(
        project_root,
        {
            "kind": "e2e_run",
            "recorded_at": now_iso(),
            "session_id": session_id,
            "correlation_id": correlation_id_for(project_root, session_id, task),
            "run_kind": run_kind,
            "origin": origin,
            # Request metadata totals (DEC-014), over every request of the run.
            "network": network,
            "label": os.environ.get(LABEL_ENV) or None,
            "verdict": verdict,
            "browser_verdict": e2e_meta.get("browser_verdict"),
            "reason": e2e_meta.get("reason"),
            "spec_source": e2e_meta.get("spec_source"),
            "replay_used": False,
            "existing_tests": (e2e_meta.get("existing_tests") or {}).get("status"),
            "claims": {
                "total": declared,
                "proven": statuses.count("proven"),
                "failed": statuses.count("failed"),
                "unproven": declared - statuses.count("proven") - statuses.count("failed"),
            },
            "failure_origins": {origin: sum(1 for f in failures if f.get("origin") == origin) for origin in ("app", "harness", "scenario", "unknown")},
            "diagnosis": {key: (e2e_meta.get("diagnosis") or {}).get(key) for key in ("cause", "next_step")} if e2e_meta.get("diagnosis") else None,
            "app_errors": len(report.get("app_errors") or []),
            "steps": report.get("steps") or {},
            "browser_runs": sum(1 for s in stages if s.get("stage") == 2 and s.get("returncode") is not None),
            "probes": len(report.get("probes") or []) + sum(1 for f in failures if f.get("probe")),
            "cleanup": (report.get("cleanup") or {}).get("status"),
            "requests": {
                "total": len(report.get("requests") or []),
                "blocked": sum(1 for r in report.get("requests") or [] if r.get("blocked")),
                "unplanned": sum(1 for r in report.get("requests") or [] if r.get("planned") is False),
                "uncertain": sum(1 for r in report.get("requests") or [] if r.get("attribution") == "uncertain"),
            },
            "artifacts": {
                "kept": len(kept),
                "screenshots": sum(1 for a in kept if a.get("kind") == "screenshot"),
                "traces": sum(1 for a in kept if a.get("kind") == "trace"),
                "skipped": sum(1 for a in artifacts if a.get("kind") == "skipped"),
                "pruned": sum(1 for a in artifacts if a.get("pruned")),
            },
            # No stage hands a screenshot or a trace to a model. Recorded anyway, so the
            # plan's screenshot-analysis rate is a measurement rather than an assumption.
            "screenshots_sent_to_model": 0,
            "provider_prompt_ids": [s["prompt_id"] for s in stages if s.get("prompt_id")],
            "provider_calls": sum(1 for s in stages if s.get("command")),
            "scenario_hash": (
                hashlib.sha256(json.dumps(scenario, sort_keys=True).encode("utf-8")).hexdigest()[:16]
                if isinstance(scenario, dict)
                else None
            ),
            "duration_seconds": round(time.monotonic() - started, 3),
        },
    )


def _network_compact(summary: dict | None) -> dict | None:
    """The run-level totals of the network summary, the part worth a quality row."""
    if not isinstance(summary, dict):
        return None
    return {
        key: summary.get(key)
        for key in ("requests", "failed", "response_bytes", "by_type", "by_status", "rows_dropped", "truncated", "slowest", "repeated")
    }


def _network_diff(project_root: Path, origin: str | None, run_kind: str, current: dict) -> dict | None:
    """What changed since the last run of the same kind against the same origin, or None."""
    from exoma.core.audit.telemetry import load_quality

    if not origin:
        return None
    previous = None
    for row in load_quality(project_root):
        if (
            row.get("kind") == "e2e_run"
            and row.get("origin") == origin
            and row.get("run_kind") == run_kind
            and isinstance(row.get("network"), dict)
        ):
            previous = row
    if previous is None:
        return None
    before = previous["network"]
    current_types = current.get("by_type") or {}
    before_types = before.get("by_type") or {}

    def _delta(key: str) -> int:
        return int(current.get(key) or 0) - int(before.get(key) or 0)

    def _type_delta(kind: str, field: str) -> int:
        return int((current_types.get(kind) or {}).get(field) or 0) - int((before_types.get(kind) or {}).get(field) or 0)

    return {
        "previous_run_at": previous.get("recorded_at"),
        "requests": _delta("requests"),
        "failed": _delta("failed"),
        "response_bytes": _delta("response_bytes"),
        "by_type": {
            kind: {"count": _type_delta(kind, "count"), "bytes": _type_delta(kind, "bytes")}
            for kind in sorted({*current_types, *before_types})
        },
    }


# Heaviest and least readable first. The runner's own files — spec, events, report,
# evidence, verification — are never candidates: they ARE the evidence.
_PRUNABLE_SUFFIXES = (".zip", ".png", ".html")


def _enforce_artifact_budget(directory: Path, max_mb: int) -> list[str]:
    """Drop player artifacts until the whole run fits `settings.artifact_max_mb`.

    Counted across the run directory AND its `retryN/` subdirectories: one budget per
    attempt let a run with retries keep up to (1 + max_retries) times the configured size.
    Names are relative to `directory`, so a pruned retry artifact is told apart from the
    first attempt's file of the same name.
    """
    budget = max(0, max_mb) * 1024 * 1024
    try:
        files = [p for p in directory.rglob("*") if p.is_file()]
    except OSError:
        return []
    total = sum(p.stat().st_size for p in files)
    removed: list[str] = []
    for suffix in _PRUNABLE_SUFFIXES:
        for path in sorted((p for p in files if p.suffix == suffix), key=lambda p: p.stat().st_size, reverse=True):
            if total <= budget:
                return removed
            size = path.stat().st_size
            try:
                path.unlink()
            except OSError:
                continue
            total -= size
            removed.append(path.relative_to(directory).as_posix())
    return removed


def _claim_ids(scenario: object) -> list[dict]:
    if not isinstance(scenario, dict):
        return []
    return [c for c in scenario.get("claims") or [] if isinstance(c, dict) and c.get("id")]


def _unproven(scenario: object) -> dict:
    return {c["id"]: {"severity": c.get("severity", "blocking"), "status": _UNPROVEN} for c in _claim_ids(scenario)}


def _spec_errors(scenario: dict, existing_tests: list, config: dict, project_root: Path) -> tuple[list[str], list[dict], list[dict]]:
    """Policy, coverage, and grounding — checked before any value is resolved or any
    browser exists; the player trusts what it is handed."""
    # Existing tests the project can actually run stand in for assertions on the claims
    # they cover; a listed test that will not run covers nothing.
    runnable, skipped = e2e_existing.select(existing_tests, config, project_root)
    covered = {cid for test in runnable for cid in test["covers"]}
    # `proven` is checked against the store, so a draft cannot promote its own guess.
    policy = {
        **config,
        "proven_selectors": e2e_knowledge.proven_selectors(project_root, str(config.get("base_url") or "")),
        # A source `e2e` may lead only when its cited line is really there (DEC-016).
        "project_root": str(project_root),
    }
    errors = validate_scenario(scenario, policy, covered=covered)
    if not errors:
        errors = validate_existing_tests(existing_tests, {c["id"] for c in _claim_ids(scenario)})
    if not errors:
        errors = ground_claims(scenario, project_root)
    if not errors:
        errors = upload_file_errors(scenario, project_root)
    return errors, runnable, skipped


def _draft_result(
    project_root: Path,
    session_id: str,
    e2e_meta: dict,
    *,
    status: str,
    parsed: dict | None = None,
    errors: list[str] | tuple = (),
    env_names: list[str] | tuple = (),
    missing_env: list[str] | tuple = (),
    secret_values: dict | None = None,
) -> dict:
    """The draft the user confirms. Placeholder form only; written for the run phase to copy."""
    parsed = parsed or {}
    if secret_values:
        # Last line before disk: whatever reached here typed out goes back to `${NAME}`.
        parsed, _ = e2e_redact.scrub_literals(parsed, secret_values)
    scenario = parsed.get("scenario")
    draft = {
        "version": 1,
        "status": status,
        "reason": e2e_meta.get("reason"),
        "scenario": scenario,
        "existing_tests": parsed.get("existing_tests") or [],
        "spec_notes": parsed.get("uncertainties") or [],
        "read_only_requests": parsed.get("read_only_requests") or [],
        "errors": list(errors),
        "env": list(env_names),
        "missing_env": list(missing_env),
        "created_at": now_iso(),
    }
    path = e2e_request.draft_path(project_root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Secret scan only — NOT `e2e_redact.write_json`, which drops typed values as it would
    # for player events. The run copies this scenario, so its `${NAME}` placeholders and
    # literal inputs have to survive; a secret-shaped literal is still scrubbed.
    clean, hits = redact_value(draft)
    atomic_write_text(path, json.dumps(clean, indent=2, ensure_ascii=False, default=str))
    if hits:
        e2e_meta["artifact_redactions"] = hits
    e2e_meta["draft"] = {**draft, "path": str(path)}
    diagnosis = _draft_diagnosis(status, draft["reason"], list(errors), e2e_meta.get("repair"))
    if diagnosis:
        e2e_meta["diagnosis"] = diagnosis
    try:
        _record_draft(project_root, session_id, e2e_meta, status, diagnosis)
    except Exception:  # observing the draft must never be able to fail it
        pass

    lines = ["[E2E DRAFT]", f"status: {status}", f"reason: {draft['reason'] or 'none'}", f"draft_file: {path}", "", "claims:"]
    claims = _claim_ids(scenario)
    lines += [
        f"- {c['id']} | {c.get('severity', 'blocking')} | {c.get('description', '')} | source_refs: {', '.join(map(str, c.get('source_refs') or [])) or 'none'}"
        for c in claims
    ] or ["- none"]
    lines += ["", "steps:"]
    steps = scenario.get("steps") if isinstance(scenario, dict) else None
    lines += [f"- {n}. {json.dumps(step, ensure_ascii=False)[:240]}" for n, step in enumerate(steps or [], 1)] or ["- none"]
    lines += ["", "cleanup (runs after the steps, pass or fail; reported apart from the test):"]
    cleanup_steps = scenario.get("cleanup") if isinstance(scenario, dict) else None
    lines += [
        f"- {n}. cleans {step.get('cleans')}: {json.dumps(step, ensure_ascii=False)[:240]}"
        for n, step in enumerate(cleanup_steps or [], 1)
        if isinstance(step, dict)
    ] or ["- none"]
    lines += ["", "existing_tests:"]
    lines += [f"- {t.get('path')} covers {', '.join(map(str, t.get('covers') or []))}" for t in draft["existing_tests"] if isinstance(t, dict)] or ["- none"]
    lines += ["", "env_placeholders:", *([f"- {name}" + (" (not set)" if name in missing_env else "") for name in env_names] or ["- none"])]
    secrets = e2e_meta.get("secrets") or {}
    if env_names and secrets.get("file"):
        state = (
            "created now with empty slots — fill the values"
            if secrets.get("template_created")
            else f"profile {secrets.get('profile')}" if secrets.get("profile") else "missing" if not secrets.get("exists") else "no profile selected"
        )
        lines += [f"secrets_file: {secrets['file']} ({state})"]
    lines += ["", "read_only_requests (proposals; confirm each before adding it to settings.allowed_read_only_requests):"]
    lines += [
        f"- {r.get('method')} {r.get('endpoint')} | source_refs: {', '.join(map(str, r.get('source_refs') or [])) or 'none'} | reason: {r.get('reason') or 'none'}"
        for r in draft["read_only_requests"]
        if isinstance(r, dict)
    ] or ["- none"]
    lines += ["", "errors:", *([f"- {e}" for e in draft["errors"]] or ["- none"])]
    lines += ["", "spec_uncertainties:", *([f"- {u}" for u in draft["spec_notes"]] or ["- none"])]
    meta = {"command": INVOCATION, "invocation": INVOCATION, "phase": "draft", "e2e": e2e_meta}
    return _sanitize_result({"ok": True, "content": "\n".join(lines) + "\n", "meta": meta})[0]


def _categories(errors: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for error in errors:
        name = error_category(error)
        counts[name] = counts.get(name, 0) + 1
    return counts


def _draft_diagnosis(status: str, reason: str | None, errors: list, repair: dict | None) -> dict | None:
    """Why a draft is not `ready`, in the same terms a run's diagnosis uses.

    The draft was the one outcome with no structured answer: it never reaches a browser, so
    it has no report to diagnose, and it is never counted by the repeat brake. Its errors
    are the validator's own messages; here they become categories (`spec.error_category`),
    both for what the first reply got wrong and for what was still wrong after the repair.
    """
    if status == "ready":
        return None
    repair = repair or {}
    initial = list(repair.get("errors") or errors)
    if status == "blocked":
        cause, next_step = "draft_blocked", "fix_environment"
        hint = "Preflight refused the draft before stage 1: fix what the reason names (Playwright, the browser, the base URL, or its policy), then draft again."
    elif reason == "secrets_invalid":
        cause, next_step = "draft_secrets", "fix_environment"
        hint = "secrets.json could not be read as a valid profile; fix the file or settings.secrets_profile. The scenario itself may be fine."
    else:
        cause, next_step = "draft_invalid", "fix_scenario"
        hint = (
            "The draft did not validate after the one repair. Fix the scenario JSON by the listed categories, or draft again with a narrower task; "
            "the categories show what the second agent keeps getting wrong."
        )
    return {
        "version": 1,
        "cause": cause,
        "next_step": next_step,
        "first_error": error_category(initial[0]) if initial else None,
        "categories": _categories(list(errors)),
        "initial_categories": _categories(initial),
        "repair": {"attempted": bool(repair.get("attempted")), "recovered": bool(repair.get("recovered"))} if repair else None,
        "fix_hint": hint,
    }


def _record_draft(project_root: Path, session_id: str, e2e_meta: dict, status: str, diagnosis: dict | None) -> None:
    """One `kind: e2e_draft` row on the quality stream: what stage 1 produced and why not.

    Separate from `e2e_run` on purpose — a draft exercised nothing, and counting it as a run
    would dilute every run rate. Categories only, never the messages: a message quotes a
    selector or a path, and the row is for counting.
    """
    repair = e2e_meta.get("repair") or {}
    stages = e2e_meta.get("stages") or []
    write_quality_record(
        project_root,
        {
            "kind": "e2e_draft",
            "recorded_at": now_iso(),
            "session_id": session_id,
            "run_kind": _run_kind(e2e_meta.get("fake")),
            "label": os.environ.get(LABEL_ENV) or None,
            "status": status,
            "reason": e2e_meta.get("reason"),
            "first_error": (diagnosis or {}).get("first_error"),
            "categories": (diagnosis or {}).get("categories") or {},
            "initial_categories": (diagnosis or {}).get("initial_categories") or _categories(list(repair.get("errors") or [])),
            "repair_attempted": bool(repair.get("attempted")),
            "repair_recovered": bool(repair.get("recovered")),
            "provider_calls": sum(1 for s in stages if s.get("command")),
            "secret_literals": sorted({name for names in (e2e_meta.get("secret_literals") or {}).values() for name in names}),
        },
    )


def _retryable(report: dict) -> bool:
    """Whether this report describes a run worth attempting again."""
    return report["browser_verdict"] == "incomplete" and report["reason"] in RETRYABLE_REASONS


def _destructive_requests(report: dict) -> list[dict]:
    """Deletes the guard refused for want of an approval, as `{method, endpoint, entry}`.

    `entry` is the line that would approve it, written out ready to paste, because the
    gap between "a delete was blocked" and "here is what to add" is where this kind of
    refusal usually stalls.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for obs in report.get("observations") or []:
        if obs.get("kind") != "destructive_unapproved":
            continue
        endpoint = str(obs.get("url") or "")
        if not endpoint or endpoint in seen:
            continue
        seen.add(endpoint)
        out.append(
            {"method": "DELETE", "endpoint": endpoint, "entry": f"DELETE {endpoint}"}
        )
    return out


def _run_signature(report: dict) -> tuple:
    """What "the same failure twice" means: same verdict, same reason, same step outcomes.

    Compared between consecutive attempts only. Two matching signatures say the run is
    reproducible rather than flaky, which is the one piece of information a retry was
    there to get.
    """
    return (
        report["browser_verdict"],
        report["reason"],
        tuple((row.get("step_id"), row.get("status"), row.get("error")) for row in report["trail"]),
    )


def run(
    executor,
    *,
    project_root: Path,
    session_id: str,
    session: dict,
    task: str,
    work_dir: str | None,
    on_progress,
    session_manager,
    lock_claim: dict | None,
    verify_route: dict,
) -> dict:
    started = time.monotonic()
    fake = os.environ.get(FAKE_ENV) or None
    stages: list[dict] = []
    request, request_reason, request_errors = e2e_request.load_request(project_root, session_id)
    e2e_meta: dict = {
        "fake": fake,
        "stages": stages,
        "request": str(e2e_request.request_path(project_root, session_id)),
    }
    if request is None:
        # Nothing was attempted, so nothing is recorded; the reason is the whole answer.
        e2e_meta["reason"] = request_reason
        norm = to_verification(
            {"claims": {}, "browser_verdict": None},
            preflight={"ok": False, "reason": request_reason, "detail": "; ".join(request_errors)},
        )
        return _result(norm["content"], norm["verdict"], e2e_meta, warnings=norm["warnings"])

    phase = request["phase"]
    config = request["settings"]
    e2e_meta["phase"] = phase
    e2e_meta["config"] = {k: config[k] for k in ("base_url", "browser", "headless", "slow_mo_ms", "allow_remote", "allow_side_effects", "max_retries", "ignore_repeat_brake")}
    # Pinned defaults that were dropped for being malformed. A warning, not an error (see
    # request.config_settings), but silent would mean a knob the user set and the runtime
    # ignored, which reads from the outside exactly like the knob not working.
    if request.get("config_warnings"):
        e2e_meta["config_warnings"] = request["config_warnings"]
    run_state: dict = {"report": None, "scenario": None}

    def _brake_refusal(record: dict) -> dict:
        """The refusal itself, shared by the two places that can reach the limit.

        Two, because the limit is reached in two different situations and only one of them
        is about to start a browser. A preflight that keeps failing never gets near one,
        and braking there buys nothing except the sentence — which is the whole point:
        after the third identical refusal the run has to stop answering with the same
        reason and start naming the loop.
        """
        # An app failure has no reason — the failing app is the verdict, not an obstacle
        # that kept the run from reaching one — so the bucket is what there is to name.
        reason_text = record.get("reason") or _REPEAT_PHRASE.get(record.get("bucket") or "", "the same outcome")
        e2e_meta["reason"] = "repeat_failure"
        e2e_meta["repeat"] = dict(record)
        # What the streak kept failing on, as the same structured answer a single run gives.
        # A record from an older build has no cause; its bucket still names the owner.
        e2e_meta["diagnosis"] = diagnosis_for_cause(record.get("cause"), record.get("bucket"), reason=record.get("reason"), repeat=True)
        e2e_meta["next_action"] = (
            "the last "
            f"{record.get('streak')} runs against {repeat_origin} all ended in "
            f"'{reason_text}' — fix that outside the scenario and run again: a change to the "
            "project (code, config, fixtures) releases the brake by itself, no new session "
            "needed. A fix that changes no file (server restarted, data reset): set "
            "settings.ignore_repeat_brake true in the request for that one run "
            "(pinned in config.json it turns the brake off for every run)"
        )
        # Outlives the session and the brake: the next draft reads that this origin kept
        # failing, and why, before it proposes the same run.
        try:
            e2e_knowledge.note_repeat_failure(project_root, repeat_origin, record, session_id)
        except Exception:  # observing the run must never be able to fail it
            pass
        return _finish(
            to_verification(
                {"claims": {}, "browser_verdict": None},
                preflight={
                    "ok": False,
                    "reason": "repeat_failure",
                    "detail": (
                        f"{record.get('streak')} consecutive runs ended in '{reason_text}'. "
                        "Editing the scenario has not changed the outcome, so the cause is "
                        "outside it — the environment, the data, or the credentials. Once it "
                        "is fixed, settings.ignore_repeat_brake true in the request runs this "
                        "once anyway."
                    ),
                },
            )
        )

    def _finish(norm: dict) -> dict:
        # One quality row per run, whatever ended it: the incomplete runs are exactly what
        # an evaluation has to count. Observes the run, so it can never fail it.
        try:
            _record_run(project_root, session_id, task, e2e_meta, norm["verdict"], started, run_state["report"], run_state["scenario"])
        except Exception:
            pass
        return _result(norm["content"], norm["verdict"], e2e_meta, warnings=norm["warnings"])

    # ---- the repeat state --------------------------------------------------------
    # Loaded here, checked below. The brake used to stand in front of preflight, on the
    # reasoning that the cost worth stopping is the browser — true, and preflight does not
    # start one. What standing there did cost was the only evidence that the problem being
    # counted was gone: three runs with Playwright missing reach the limit, the user
    # installs Playwright, and the fourth run is refused before preflight can notice. The
    # streak is cleared by a passing run, and the brake stood in front of the run that
    # would have cleared it. So the check moved down, past the gates that can prove the
    # environment recovered, and still ahead of every browser this run might start.
    repeat = e2e_repeat_state(project_root, session_id)
    repeat_origin = e2e_knowledge.origin_of(str(config.get("base_url") or ""))
    fingerprint_cache: list[str | None] = []

    def current_fingerprint() -> str | None:
        # Computed at most once per run, and only when something needs it: a failure being
        # counted, or a streak at the limit being weighed.
        if not fingerprint_cache:
            try:
                fingerprint_cache.append(project_fingerprint(project_root))
            except Exception:  # a brake that cannot fingerprint behaves as it always did
                fingerprint_cache.append(None)
        return fingerprint_cache[0]

    # A streak at the limit whose last failure saw a different project than this run sees:
    # someone changed the application since. That is the fix the brake asks for, so it lets
    # this run through — a new session used to be the only way out that did not require a
    # per-request setting. The record is cleared rather than bypassed: the run that follows
    # starts counting afresh, and if the change did not help, three more failures brake again.
    # Scenario edits live under `.workflow/` and never change the fingerprint.
    if (
        repeat.get("origin") == repeat_origin
        and int(repeat.get("streak") or 0) >= E2E_REPEAT_LIMIT
        and repeat.get("fingerprint")
    ):
        now_seen = current_fingerprint()
        if now_seen and now_seen != repeat.get("fingerprint"):
            try:
                note_e2e_outcome(project_root, session_id, repeat_origin, None)
                e2e_meta["repeat_released"] = {
                    "by": "change_detected",
                    **{key: repeat.get(key) for key in ("bucket", "reason", "streak", "source")},
                }
                repeat = {}
            except Exception:  # observing the run must never be able to fail it
                pass
    # The same release for the other half of what a run depends on. A streak written by a
    # different runtime counted failures this runtime may not have: the fix was the change.
    # A record with no runtime at all predates this field, and says nothing either way.
    if (
        repeat.get("origin") == repeat_origin
        and int(repeat.get("streak") or 0) >= E2E_REPEAT_LIMIT
        and repeat.get("runtime")
        and repeat.get("runtime") != runtime_identity()
    ):
        try:
            note_e2e_outcome(project_root, session_id, repeat_origin, None)
            e2e_meta["repeat_released"] = {
                "by": "runtime_changed",
                **{key: repeat.get(key) for key in ("bucket", "reason", "streak", "source", "runtime")},
            }
            repeat = {}
        except Exception:  # observing the run must never be able to fail it
            pass

    pre = preflight(config, fake=bool(fake))
    e2e_meta["preflight"] = pre["checks"]
    network = pre.get("network") or {"write_hosts": [], "pins": {}, "hosts": []}
    if network.get("hosts"):
        e2e_meta["network"] = network
    if not pre["ok"]:
        e2e_meta["reason"] = pre["reason"]
        if phase == "draft":
            return _draft_result(project_root, session_id, e2e_meta, status="blocked", errors=[f"{pre['reason']}: {pre['detail']}"])
        # Counted, though no browser ever started. The loop the brake exists to break is
        # precisely this one: the run never reaches the browser report below, which used to
        # be the only place an outcome was written, so twelve invocations against one
        # unreachable base URL left the counter at zero and the brake never fired against
        # the shape it was built for.
        #
        # By reason, not by "preflight failed". The caller's own input — `spec_invalid` from
        # the base-URL or write policy here, and `secrets_invalid` / `env_missing` above —
        # is corrected by coming back with it fixed, which is the fix working rather than a
        # loop; braking there would lock someone out on the third attempt at a password, and
        # only a new session would let them try a fourth. What is counted is the environment,
        # which no edit to the request can change, and that is what repeating is worth
        # stopping.
        if pre["reason"] in _REPEATABLE_PREFLIGHT:
            # Read before written. `note_e2e_outcome` REPLACES a record whose bucket
            # differs, so an `app` streak sitting at the limit was being overwritten by
            # the first `playwright_missing` that came along — bucket `harness`, streak
            # back to 1 — and the brake that was holding a broken application at bay
            # vanished because a dependency went missing for one run. Whatever reached the
            # limit stays the answer until something clears it on its own terms.
            if repeat.get("origin") == repeat_origin and int(repeat.get("streak") or 0) >= E2E_REPEAT_LIMIT:
                if not config["ignore_repeat_brake"]:
                    return _brake_refusal(repeat)
                # Stepping past the brake is not the same as retiring what it was
                # holding. Counting here would replace that record with this gate's own
                # bucket at one, so a single run with the escape on would spend the
                # streak as well as bypass it — and the brake would be gone for good
                # rather than for the run that asked.
                return _finish(to_verification({"claims": {}, "browser_verdict": None}, preflight=pre))
            streak = 0
            try:
                streak = note_e2e_outcome(
                    project_root,
                    session_id,
                    repeat_origin,
                    repeat_bucket("incomplete", pre["reason"]),
                    pre["reason"],
                    source="preflight",
                    fingerprint=current_fingerprint(),
                    runtime=runtime_identity(),
                )
                if streak > 1:
                    e2e_meta["repeat_streak"] = streak
            except Exception:  # observing the run must never be able to fail it
                pass
            # Counted first, then read: this gate is where the loop actually lives, so the
            # run that reaches the limit is the one that says so. The brake below never
            # sees these — it sits past a preflight that PASSED, which is the point of it
            # sitting there — and without this the third identical `playwright_missing`
            # would answer exactly like the first.
            if (
                not config["ignore_repeat_brake"]
                and streak >= E2E_REPEAT_LIMIT
            ):
                return _brake_refusal(
                    {
                        "origin": repeat_origin,
                        "bucket": repeat_bucket("incomplete", pre["reason"]),
                        "reason": pre["reason"],
                        "streak": streak,
                    }
                )
        return _finish(to_verification({"claims": {}, "browser_verdict": None}, preflight=pre))

    # Preflight passed, so whatever it was that preflight kept failing on is no longer
    # true: Playwright is installed, the browser is there, the base URL answers. That is
    # the proof the run would otherwise have to reach a verdict to give.
    #
    # Keyed on WHO WROTE the streak. Two earlier keys were tried and both were too wide.
    # Bucket, first: `harness` and `timeout` are where a browser run's own incompletes
    # land too, so clearing by bucket retired a streak of real timeouts every time
    # preflight reached the host — which it does on every run. Then the reason, which
    # looked exact and is not: the player emits `playwright_missing` and `browser_missing`
    # itself when a launch fails, the same words preflight uses, so a browser that will
    # not start was indistinguishable from one preflight had just found. What preflight is
    # evidence about is what preflight wrote, and only the record can say that.
    if repeat.get("origin") == repeat_origin and repeat.get("source") == "preflight":
        try:
            note_e2e_outcome(project_root, session_id, repeat_origin, None)
            repeat = {}
        except Exception:  # observing the run must never be able to fail it
            pass

    # ---- the repeat brake --------------------------------------------------------
    # The in-run retry loop already refuses to try a third time when two attempts match
    # step for step; it cannot see the caller coming back with an edited scenario and the
    # same environment problem underneath. Twelve invocations against one 403 is the shape
    # it missed, and every edit in between was to steps that were never what was wrong.
    if (
        phase != "draft"
        and not config["ignore_repeat_brake"]
        and repeat.get("origin") == repeat_origin
        # `.get` throughout: this record comes off disk and may have been written by an
        # older build or half-truncated. A brake that raises on a field it expected would
        # stop the run it exists to protect, for the wrong reason and with no explanation.
        and int(repeat.get("streak") or 0) >= E2E_REPEAT_LIMIT
    ):
        return _brake_refusal(repeat)

    secrets, secret_errors, secret_info = e2e_request.load_secrets(project_root, config.get("secrets_profile") or "")
    e2e_meta["secrets"] = {key: secret_info[key] for key in ("file", "exists", "profile", "profiles")}
    # Registered names only: the process environment is a fallback for a credential, not a
    # way for a scenario to read any variable the runtime happens to have.
    lookup = {**{name: os.environ[name] for name in e2e_request.CREDENTIAL_KEYS if name in os.environ}, **secrets}

    # ---- literal secrets ------------------------------------------------------------
    # The task and the request are written by agents, and a credential that should have
    # travelled as `${NAME}` sometimes arrives typed out. Found by value, because only the
    # lookup knows what an email or a plain password looks like; replaced by its
    # placeholder, which resolves back to the same value for the player, so the run is the
    # same run without the value in any prompt, file, or result. Flagged by name.
    leaked = {}
    task, task_names = e2e_redact.scrub_literals(task, lookup)
    if task_names:
        leaked["task"] = task_names
    request_names = e2e_request.scrub_request_file(project_root, session_id, lookup)
    if request_names:
        request, _ = e2e_redact.scrub_literals(request, lookup)
        leaked["request"] = request_names
    if leaked:
        e2e_meta["secret_literals"] = leaked
        e2e_meta.setdefault("config_warnings", []).append(
            "credential value(s) typed out literally in "
            + ", ".join(f"{where} ({', '.join(names)})" for where, names in leaked.items())
            + "; replaced by their ${NAME} placeholders — write placeholders, never values"
        )

    if phase == "draft":
        return _draft(executor, project_root, session_id, session, task, work_dir, on_progress, session_manager, lock_claim, config, e2e_meta, stages, lookup, secret_errors)

    # ---- the confirmed spec ------------------------------------------------------
    scenario = request["scenario"]
    parsed = {"scenario": scenario, "existing_tests": request["existing_tests"], "uncertainties": request["spec_notes"]}
    e2e_meta["spec_source"] = "request"
    stages.append({"stage": 1, "command": None, "source": "request"})
    run_state["scenario"] = scenario
    errors, runnable_tests, skipped_tests = _spec_errors(scenario, parsed["existing_tests"], config, project_root)
    if errors:
        e2e_meta["reason"] = "spec_invalid"
        return _finish(
            to_verification(
                {"claims": _unproven(scenario), "browser_verdict": None},
                preflight={"ok": False, "reason": "spec_invalid", "detail": "; ".join(errors)},
            )
        )
    if secret_errors:
        e2e_meta["reason"] = "secrets_invalid"
        return _finish(
            to_verification(
                {"claims": _unproven(scenario), "browser_verdict": None},
                preflight={"ok": False, "reason": "secrets_invalid", "detail": "; ".join(secret_errors)},
            )
        )

    names = env_references(scenario)
    missing = [name for name in names if name not in lookup]
    if missing:
        e2e_meta["reason"] = "env_missing"
        return _finish(
            to_verification(
                {"claims": _unproven(scenario), "browser_verdict": None},
                preflight={
                    "ok": False,
                    "reason": "env_missing",
                    "detail": (
                        f"not set in .workflow/e2e/{e2e_request.SECRETS_FILE} "
                        f"(profile: {secret_info.get('profile') or 'none'}) or the environment: {', '.join(missing)}"
                    ),
                },
            )
        )
    resolved, _ = substitute_env(scenario, lookup)
    resolved_values = {name: lookup[name] for name in names}
    # A value shorter than the scrubber's minimum cannot be put back to its placeholder by
    # substring, so the player keeps no page HTML for this run (screenshots and traces are
    # already off whenever values resolve). Named, never shown.
    unscrubbable = [name for name, value in resolved_values.items() if len(value) < e2e_redact.MIN_SCRUB_CHARS]
    if unscrubbable:
        e2e_meta["secrets"]["unscrubbable"] = unscrubbable

    # ---- artifacts ----------------------------------------------------------------
    run_id = time.strftime("%Y%m%d_%H%M%S") + "_e2e"
    e2e_dir = workflow_paths(project_root, session_id)["logs_dir"] / run_id / "e2e"
    redaction_hits: list[dict] = []
    redaction_hits += e2e_redact.write_json(e2e_dir / "spec.json", scenario)  # placeholder form, never resolved
    e2e_meta["artifacts"] = str(e2e_dir)

    # ---- existing tests (plan §8): the project's own, through the user's command ------
    existing_result: dict | None = None
    existing_rows: list[dict] = list(skipped_tests)
    if runnable_tests:
        # The command gets the whole selected profile, so its output is scrubbed of all of
        # it, not only of the names the scenario happened to reference.
        existing_result = e2e_existing.run(runnable_tests, config, project_root, {**secrets, **resolved_values}, extra_env=secrets)
        redaction_hits += existing_result.get("redactions") or []
        redaction_hits += e2e_redact.write_text(e2e_dir / "existing_tests.log", existing_result["output_tail"])
        result_label = {"passed": "pass", "failed": "fail", "timeout": "timeout"}.get(existing_result["status"], "not run (launch failed)")
        existing_rows = [{**test, "result": result_label} for test in runnable_tests] + existing_rows
        stages.append(
            {
                "stage": "existing_tests",
                "command": None,
                "status": existing_result["status"],
                "returncode": existing_result["returncode"],
                "duration_seconds": existing_result["duration_seconds"],
                "files": existing_result["files"],
            }
        )
        e2e_meta["existing_tests"] = {k: existing_result[k] for k in ("status", "files", "covers")}

    # ---- stage 2: the player ------------------------------------------------------
    if not config.get("headless") and not display_available():
        # Headed is the shipped default, and a Linux box with no X11/Wayland (CI, a
        # container, SSH) cannot open a window: the launch would fail, be retried as an
        # environment problem, and end `incomplete` having proved nothing about the app.
        config = {**config, "headless": True}
        e2e_meta["config"]["headless"] = True
        e2e_meta.setdefault("config_warnings", []).append(
            "headless: false but no display is available (DISPLAY/WAYLAND_DISPLAY unset); ran headless"
        )

    def progress(event: dict) -> None:
        # The live mirror gets the event itself, scrubbed exactly like the stored events.
        mirror_event = e2e_redact.scrub_resolved([event], resolved_values)[0]
        e2e_current.e2e_event(project_root, session_id, mirror_event)
        if on_progress is None:
            return
        on_progress(
            {
                "phase": "e2e_player",
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "idle_seconds": 0.0,
                "step": event.get("step"),
            }
        )

    def _play(attempt_dir: Path) -> dict:
        if not scenario.get("steps"):
            # Every claim is covered by an existing test the project ran: no browser to start.
            return {
                "events": [{"type": "result", "status": "finished"}],
                "stderr_tail": [],
                "malformed": [],
                "launch_error": None,
                "timed_out": False,
                "stalled": False,
                "truncated": False,
                "returncode": None,
                "duration_seconds": 0.0,
            }
        payload = json.dumps(
            {
                "scenario": resolved,
                # The same scenario in placeholder form. The player reports what a step
                # expected from this one, so a resolved value never has to be scrubbed back
                # out of an event — which fails for one shorter than MIN_SCRUB_CHARS.
                "display_scenario": scenario,
                # The write decision preflight made, not the settings it made it from: the
                # player is told which hosts were approved and at which addresses, and never
                # repeats the lookup that approved them.
                "config": {
                    **config,
                    "capture_html": not unscrubbable,
                    # upload fixtures resolve against this, and only inside it
                    "project_root": str(project_root),
                    "write_hosts": network.get("write_hosts") or [],
                    "host_pins": network.get("pins") or {},
                    # From the permissions file only. It reaches the player here rather
                    # than through `settings` so that no request can name it, extend it,
                    # or empty it.
                    "blocked_requests": request.get("blocked_requests") or [],
                },
                "artifacts_dir": str(attempt_dir),
                "fake": fake,
                # The player takes no screenshot and no trace once real values are in play:
                # binary captures cannot be scrubbed afterwards.
                "has_secrets": bool(resolved_values),
            }
        )
        return run_player(
            [sys.executable, "-m", "exoma.core.evidence.e2e.player"],
            stdin_payload=payload,
            env=_child_env(str(_AGENT_ROOT)),
            cwd=str(_AGENT_ROOT),
            idle_timeout_s=float(config["idle_timeout_s"]),
            total_timeout_s=float(config["total_timeout_s"]),
            on_progress=progress,
        )

    # How many more browsers this run may start. `allow_side_effects` turns retries off
    # outright: the first attempt's write may already have reached the server, so a second
    # would either double it or run against the state the first one left behind. That is
    # the same reason the skill tells the user never to re-run a timed-out write by hand.
    budget = 0 if config["allow_side_effects"] else max(0, min(int(config["max_retries"]), MAX_RETRIES))
    attempts: list[dict] = []
    previous_signature: tuple | None = None
    attempt = 0
    while True:
        attempt += 1
        # Attempt 1 owns e2e_dir so a run that never retried looks exactly as it did
        # before; each retry gets its own directory rather than overwriting the evidence
        # of what it is retrying.
        final_dir = e2e_dir if attempt == 1 else e2e_dir / f"retry{attempt - 1}"
        supervised = _play(final_dir)
        # Everything the child sent back is scrubbed of resolved values BEFORE it is used
        # for anything: classification, artifacts, the reviewer prompt, or the result.
        events = e2e_redact.scrub_resolved(list(supervised["events"]), resolved_values)
        stderr_tail = e2e_redact.scrub_resolved(list(supervised["stderr_tail"]), resolved_values)
        malformed = e2e_redact.scrub_resolved(list(supervised["malformed"]), resolved_values)
        if supervised.get("launch_error"):
            events.append(
                {
                    "type": "harness",
                    "reason": "launch_failed",
                    "detail": e2e_redact.scrub_resolved(supervised["launch_error"], resolved_values),
                }
            )
        # Player-written files: text scrubbed exactly like the events were, then the run's
        # size budget. Both before the report names them, so what it lists is what exists.
        scrub_failures: list[dict] = []
        redaction_hits += e2e_redact.scrub_text_files(final_dir, resolved_values, scrub_failures)
        pruned = _enforce_artifact_budget(e2e_dir, int(config["artifact_max_mb"]))
        if scrub_failures:
            # Named, and kept out of the report the same way a budget-pruned file is: the
            # reviewer must never be pointed at a file this run could not scrub.
            e2e_meta["artifacts_unscrubbed"] = [
                *(e2e_meta.get("artifacts_unscrubbed") or []), *scrub_failures
            ]
            for failure in scrub_failures:
                try:
                    rel = Path(failure["path"]).relative_to(e2e_dir).as_posix()
                except ValueError:
                    continue
                pruned = sorted(set(pruned or []) | {rel})
        # A failure that left something behind — a file that could be neither scrubbed nor
        # deleted, or a directory that could not even be listed — means this attempt holds
        # files nobody scrubbed and whose names may not be known. No artifact of the
        # attempt is offered to the report, by name or otherwise.
        attempt_unsafe = any(not failure.get("removed") for failure in scrub_failures)
        if attempt_unsafe:
            e2e_meta["artifacts_unsafe"] = sorted(
                {*(e2e_meta.get("artifacts_unsafe") or []), str(final_dir)}
            )
        if pruned or attempt_unsafe:
            if pruned:
                e2e_meta["artifacts_pruned"] = sorted(set(e2e_meta.get("artifacts_pruned") or []) | set(pruned))
            prefix = "" if final_dir == e2e_dir else f"{final_dir.relative_to(e2e_dir).as_posix()}/"
            unscrubbed = {
                Path(failure["path"]).name for failure in scrub_failures
            }
            events = [
                {
                    **event,
                    "pruned": True,
                    "pruned_reason": "unscrubbed"
                    if attempt_unsafe or event.get("name") in unscrubbed
                    else "budget",
                }
                if event.get("type") == "artifact"
                and (attempt_unsafe or f"{prefix}{event.get('name')}" in (pruned or []))
                else event
                for event in events
            ]
        report = build_report(
            events,
            scenario,
            run_meta={
                "timed_out": supervised["timed_out"],
                "stalled": supervised["stalled"],
                "truncated": supervised["truncated"],
                "malformed": bool(malformed),
                "existing_tests": existing_result,
            },
        )
        signature = _run_signature(report)
        attempts.append(
            {
                "attempt": attempt,
                "artifacts": str(final_dir),
                "browser_verdict": report["browser_verdict"],
                "reason": report["reason"],
                "duration_seconds": supervised["duration_seconds"],
            }
        )
        if attempt > budget or not _retryable(report):
            break
        if signature == previous_signature:
            # Two attempts in a row ended the same way, step for step. The flake theory is
            # spent: another browser would cost a minute and prove the same thing again.
            attempts[-1]["stable"] = True
            break
        previous_signature = signature

    report["existing_tests"] = existing_rows
    # Why it failed, what failed, what to do next, what to fix — enums and fixed text, so it
    # travels into report.json, meta, the quality row, the repeat record and the knowledge
    # the next draft reads, without carrying page text or a resolved value anywhere.
    report["diagnosis"] = diagnose(report)
    if report["diagnosis"]:
        e2e_meta["diagnosis"] = report["diagnosis"]
    run_state["report"] = report
    if len(attempts) > 1:
        e2e_meta["attempts"] = attempts
    stages.append(
        {
            "stage": 2,
            "command": None,
            "returncode": supervised["returncode"],
            "duration_seconds": supervised["duration_seconds"],
            "events": len(events),
            "attempts": len(attempts),
            "timed_out": supervised["timed_out"],
            "stalled": supervised["stalled"],
            "malformed": len(malformed),
        }
    )
    redaction_hits += e2e_redact.write_jsonl(e2e_dir / "events.jsonl", events)

    redaction_hits += e2e_redact.write_json(
        e2e_dir / "report.json",
        {**report, "stderr_tail": stderr_tail, "malformed": malformed},
    )
    # ---- tag proposals: recorded, never applied -----------------------------------
    # The runner's half of the tagging pass ends here. It knows which elements were driven
    # and whether each one could be tagged; it does not edit a template, because that is a
    # write to code the user owns and the user has not been asked yet.
    try:
        tag_entries = e2e_tagging.plan(project_root, e2e_tagging.proposals(report))
    except Exception as exc:  # observing the run must never be able to fail it
        tag_entries = []
        e2e_meta["tags"] = {"error": f"{type(exc).__name__}: {exc}"}
    if tag_entries:
        redaction_hits += e2e_redact.write_json(e2e_dir / "tag-proposals.json", tag_entries)
        e2e_meta["tags"] = {
            "file": str(e2e_dir / "tag-proposals.json"),
            "ready": len(e2e_tagging.ready(tag_entries)),
            "skipped": len(tag_entries) - len(e2e_tagging.ready(tag_entries)),
        }
    # ---- knowledge: what this run proved, for the next draft -------------------------
    # From the placeholder scenario, never the resolved one. Only a run that passed on its
    # first attempt is proof: not a pass that needed a retry, nor the passing steps of a
    # run that failed. Failures always count against what the store believed.
    try:
        e2e_meta["knowledge"] = e2e_knowledge.ingest(
            project_root, report, scenario, str(config.get("base_url") or ""), session_id,
            clean_first_attempt=len(attempts) == 1 and report["browser_verdict"] == "pass",
        )
    except Exception as exc:  # observing the run must never be able to fail it
        e2e_meta["knowledge"] = {"error": f"{type(exc).__name__}: {exc}"}
    if (report.get("diagnosis") or {}).get("reusable"):
        # The one kind of failure the next draft can act on: a selector that did not fit,
        # or one the page did not render. Kept for it, keyed by the step's selector.
        try:
            e2e_knowledge.note_failure_hint(project_root, repeat_origin, report["diagnosis"], scenario, session_id)
        except Exception:  # observing the run must never be able to fail it
            pass
    e2e_meta["browser_verdict"] = report["browser_verdict"]
    e2e_meta["reason"] = report["reason"]
    e2e_meta["cleanup"] = {key: report["cleanup"][key] for key in ("status", "groups")}
    # ---- deletes waiting on an answer ------------------------------------------------
    # The one refusal in this package that is a question rather than a verdict. It travels
    # in meta so the skill can ask it and write the approval into the next request; the
    # runtime never grants it, and never asks on the user's behalf.
    # Feeds the brake above on the next invocation. A pass clears the streak; only a
    # non-pass verdict carries a reason worth counting.
    try:
        bucket = repeat_bucket(report["browser_verdict"], report["reason"])
        # A scenario's own mistake is neither counted nor allowed to clear what is: the
        # next run is a different scenario, and a typo must not erase a streak of real
        # environment failures standing behind it.
        streak = 0
        if report["reason"] not in NOT_COUNTED_REASONS:
            streak = note_e2e_outcome(
                project_root,
                session_id,
                repeat_origin,
                bucket,
                report["reason"],
                fingerprint=current_fingerprint() if bucket else None,
                signature=failure_signature(report, scenario) if bucket == "app" else None,
                runtime=runtime_identity() if bucket else None,
                cause=(report.get("diagnosis") or {}).get("cause") if bucket else None,
            )
        else:
            e2e_meta["repeat_not_counted"] = report["reason"]
        if streak > 1:
            e2e_meta["repeat_streak"] = streak
        if report["browser_verdict"] == "pass":
            # A pass is the proof a recorded repeat failure is over.
            resolved = e2e_knowledge.resolve_repeat_failures(project_root, repeat_origin)
            if resolved:
                e2e_meta["repeat_failures_resolved"] = resolved
    except Exception:  # observing the run must never be able to fail it
        pass

    pending = _destructive_requests(report)
    if pending:
        e2e_meta["destructive_pending"] = pending
        e2e_meta["next_action"] = (
            "ask the user to approve each delete below, then add the approved entries to "
            "settings.allowed_destructive_requests in the next request and run again"
        )

    # ---- stage 3: hybrid review ---------------------------------------------------
    # The reviewer reads the disk. A directory that still holds files nobody could scrub is
    # not named to it at all: withholding the file names alone would leave the path to them.
    artifacts_ref = (
        "withheld: this attempt left unscrubbed files behind (meta.e2e.artifacts_unsafe)"
        if str(final_dir) in (e2e_meta.get("artifacts_unsafe") or [])
        else str(final_dir)
    )
    block, block_hits = e2e_redact.redact_text(
        evidence_block(report, artifacts=artifacts_ref, spec_notes=parsed.get("uncertainties") or [])
    )
    redaction_hits += block_hits
    redaction_hits += e2e_redact.write_text(e2e_dir / "evidence.md", block)
    reviewer_content: str | None = None
    reviewer_error: str | None = None
    if report["browser_verdict"] != "incomplete":
        prompt, prompt_meta = executor._build_delegated_prompt(
            verify_route, "verify", task, session_id, project_root, e2e_evidence=block
        )
        review = executor._run_delegated(
            verify_route, "verify", task, session, session_id, project_root, work_dir,
            on_progress, session_manager, prompt=prompt, prompt_meta=prompt_meta, lock_claim=lock_claim,
            record_failure=False,
        )
        stages.append(
            {
                "stage": 3,
                "command": "verify",
                "prompt_id": (executor._last_call_meta or {}).get("prompt_id"),
                "ok": bool(review.get("ok")),
            }
        )
        if review.get("ok"):
            reviewer_content = review.get("content") or ""
        else:
            reviewer_error = str((review.get("meta") or {}).get("error_type") or "provider_failure")

    norm = to_verification(
        report,
        reviewer_content=reviewer_content,
        reviewer_error=reviewer_error,
        existing_tests=existing_rows,
        attempts=attempts,
    )
    redaction_hits += e2e_redact.write_text(e2e_dir / "verification.md", norm["content"])
    e2e_current.e2e_finish(project_root, session_id, e2e_dir, final_dir)
    if redaction_hits:
        e2e_meta["artifact_redactions"] = redaction_hits
    return _finish(norm)


def _draft(
    executor,
    project_root: Path,
    session_id: str,
    session: dict,
    task: str,
    work_dir: str | None,
    on_progress,
    session_manager,
    lock_claim: dict | None,
    config: dict,
    e2e_meta: dict,
    stages: list[dict],
    lookup: dict,
    secret_errors: list[str],
) -> dict:
    """Stage 1 only: second_agent proposes, the runtime validates, the user decides."""
    spec_route = executor._router_for(project_root).route("e2e_spec")
    # What earlier runs against this origin proved. A sidecar, not prompt text: the draft
    # reads it the way it reads facts, and an empty store offers nothing rather than an
    # empty section the model might fill in itself.
    try:
        offered = e2e_knowledge.write_sidecar(project_root, session_id, str(config.get("base_url") or ""))
        e2e_meta["knowledge"] = {"offered": offered}
    except Exception as exc:  # reuse is an optimisation; a draft never fails for it
        offered = 0
        e2e_meta["knowledge"] = {"offered": 0, "error": f"{type(exc).__name__}: {exc}"}
    prompt, prompt_meta = executor._build_delegated_prompt(
        spec_route, "e2e_spec", task, session_id, project_root, has_e2e_knowledge=offered > 0
    )
    first = executor._run_delegated(
        spec_route, "e2e_spec", task, session, session_id, project_root, work_dir,
        on_progress, session_manager, prompt=prompt, prompt_meta=prompt_meta, lock_claim=lock_claim,
        record_failure=False,
    )
    stages.append({"stage": 1, "command": "e2e_spec", "prompt_id": (executor._last_call_meta or {}).get("prompt_id"), "ok": bool(first.get("ok"))})
    if not first.get("ok"):
        # Returned without finalisation, so the spend is recorded here — once.
        first.setdefault("meta", {}).update({"invocation": INVOCATION, "phase": "draft", "e2e": e2e_meta})
        return executor._record_failed_call(first, project_root, INVOCATION, task, session_id)
    content = first.get("content") or ""
    has_thread = bool(session.get("provider_session_id"))
    gap = spec_gap(content)
    if gap and gap.get("recoverable") and has_thread:
        follow_up = executor._run_delegated(
            spec_route, "e2e_spec", task, session, session_id, project_root, work_dir,
            on_progress, session_manager, prompt=spec_continuation_prompt(gap), prompt_meta={}, lock_claim=lock_claim,
            record_failure=False,
        )
        stages.append({"stage": 1, "command": "e2e_spec", "continuation": True, "ok": bool(follow_up.get("ok"))})
        if follow_up.get("ok"):
            content = content.rstrip() + "\n\n" + (follow_up.get("content") or "")
            gap = spec_gap(content)

    def validated(text: str) -> tuple[dict, list[str], list[str]]:
        # Scrubbed after parsing and before validation, so draft.json, the draft shown to
        # the user and the request later copied from it all start from the placeholder
        # form — and a known password typed into a password field is replaced, not refused.
        parsed_text, names = e2e_redact.scrub_literals(parse_spec(text), lookup)
        if names:
            found_before = (e2e_meta.get("secret_literals") or {}).get("draft") or []
            e2e_meta.setdefault("secret_literals", {})["draft"] = sorted({*found_before, *names})
        found, _runnable, _skipped = _spec_errors(parsed_text["scenario"], parsed_text.get("existing_tests") or [], config, project_root)
        # A proposed read-only POST is only a proposal: the draft lists it for the user to
        # confirm, and nothing reaches the guard until the request's settings name it. An
        # ungrounded proposal still makes the draft invalid, like an ungrounded claim.
        return parsed_text, found, validate_read_only_requests(parsed_text.get("read_only_requests") or [], project_root)

    parsed, spec_errors, read_only_errors = ({}, [], []) if gap else validated(content)
    # One repair, in the same thread, for a section that arrived but did not validate: a
    # scenario outside its fence, invalid JSON, a value outside an enum, a retired selector
    # key. These are the shapes a first draft most often has, and each used to end the draft
    # as `spec_invalid` although the model that wrote it could fix it from the validator's
    # own message. Bounded to one: a second failure is the model's answer, not a typo.
    repair_errors = list(gap.get("missing") or []) if gap and gap.get("repairable") else [*spec_errors, *read_only_errors]
    if repair_errors:
        repair = {"attempted": has_thread, "errors": repair_errors[:MAX_REPAIR_ERRORS], "recovered": False}
        if has_thread:
            fixed = executor._run_delegated(
                spec_route, "e2e_spec", task, session, session_id, project_root, work_dir,
                on_progress, session_manager, prompt=spec_repair_prompt(repair_errors), prompt_meta={}, lock_claim=lock_claim,
                record_failure=False,
            )
            stages.append({"stage": 1, "command": "e2e_spec", "repair": True, "ok": bool(fixed.get("ok"))})
            candidate = (fixed.get("content") or "") if fixed.get("ok") else ""
            if candidate and spec_gap(candidate) is None:
                content, gap = candidate, None
                parsed, spec_errors, read_only_errors = validated(content)
                repair["recovered"] = not (spec_errors or read_only_errors)
        else:
            # Said, not skipped silently: without a provider thread there is no model to ask
            # that still holds the evidence, and a fresh call would re-explore from nothing.
            repair["reason"] = "no_provider_session"
        e2e_meta["repair"] = repair
    if gap:
        e2e_meta["reason"] = "spec_invalid"
        return _draft_result(project_root, session_id, e2e_meta, status="invalid", errors=list(gap.get("missing") or [gap.get("reason", "")]), secret_values=lookup)
    e2e_meta["spec_source"] = "e2e_spec"
    scenario = parsed["scenario"]
    errors = [*spec_errors, *read_only_errors, *secret_errors]
    names = env_references(scenario)
    # Not an error at draft time: the user may fill secrets.json after reading the draft.
    # Nothing ever created that file, so a user asked to "fill it" had nowhere to start;
    # the draft writes it — shape and names from the credential registry, profile names from
    # the request settings, never from the scenario second_agent proposed.
    missing = [name for name in names if name not in lookup]
    if missing and not (e2e_meta.get("secrets") or {}).get("exists"):
        e2e_meta.setdefault("secrets", {})["template_created"] = e2e_request.ensure_secrets_template(
            project_root, e2e_request.template_profiles(config)
        )
    if spec_errors or read_only_errors:
        e2e_meta["reason"] = "spec_invalid"
    elif secret_errors:
        e2e_meta["reason"] = "secrets_invalid"
    return _draft_result(
        project_root,
        session_id,
        e2e_meta,
        status="invalid" if errors else "ready",
        parsed=parsed,
        errors=errors,
        env_names=names,
        missing_env=missing,
        secret_values=lookup,
    )
