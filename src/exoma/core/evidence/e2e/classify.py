"""Turn player events into a browser verdict with an origin per failure.

Three origins, not two. `app` means the evidence says the application broke the claim;
`harness` means the evidence says the scenario or the runtime broke; `unknown` means
the evidence cannot tell — and `unknown` is never promoted to `harness`, because that
is exactly the move that would hide a regression behind a flaky-selector story.

Player event protocol (one JSON object per stdout line):
  {"type": "progress", "step": n, "step_id": str, "action": str, "status": "passed|failed|skipped",
   "claim_id": str|None, "expected": any, "actual": any, "url_after": str|None,
   "duration_ms": int, "error": {"kind": str, "detail": str}|None,
   "selector_provenance": str|None, "page_stable": bool|None,
   "selection": {"candidate": int|None, "match_counts": [int|None], "fallback_used": bool},
   "ready": {"conditions": int, "waited_ms": int, "unmet": [str]}}   # selection/ready when used
  {"type": "request", "id": str, "phase": "steps|cleanup", "step_id": str|None,
   "after_step": str|None, "attribution": "step|uncertain", "method": str,
   "endpoint": str (sanitised), "resource_type": str, "read_only": bool, "blocked": bool,
   "planned": bool|None, "status": int|None, "failure": str|None, "duration_ms": int|None}
  {"type": "cleanup", "step_id": str, "cleans": str, "action": str,
   "status": "passed|failed|skipped|not_needed", "expected": any, "actual": any,
   "error": {...}|None, "duration_ms": int, "detail": str|None}
  {"type": "observation", "kind": "http_5xx|page_error|console_error|mutation_blocked|read_only_request_allowed", "url": str,
   "status": int|None, "detail": str, "same_origin": bool, "main_request": bool,
   "enforced": bool}   # console_error only: settings.fail_on_console_error
  {"type": "harness", "reason": str, "detail": str}
  {"type": "artifact", "kind": "html|screenshot|trace|skipped|error", "name": str|None,
   "step": int|None, "detail": str|None, "bytes": int|None, "pruned": bool,
   "pruned_reason": "budget"|"unscrubbed"|None}
  {"type": "heartbeat"}
  {"type": "result", "status": "finished|aborted"}
"""

from __future__ import annotations

import hashlib
import json

from exoma.core.evidence.e2e.redact import sanitize_endpoint

ORIGIN_APP = "app"
ORIGIN_HARNESS = "harness"
ORIGIN_UNKNOWN = "unknown"
# The scenario itself, on evidence that says so: a selector the draft guessed that matched
# nothing or too much, a write or a navigation the run's own policy refused. Split from
# `harness` because the two have different owners — a harness problem is the runtime's or
# the machine's, and repeating the run is how it is confirmed; a scenario problem is the
# draft's, and the next run is a different scenario, not a repeat. Never inferred: a
# selector the codebase named that is missing stays `app`/`unknown`, as before.
ORIGIN_SCENARIO = "scenario"

# Reasons a run ends without a verdict. Every one of them maps to `incomplete`; the
# reason is what the user needs to fix, so it travels verbatim into `not_verified`.
INCOMPLETE_REASONS = frozenset(
    {
        "playwright_missing",
        "browser_missing",
        "base_url_unreachable",
        "spec_invalid",
        "harness_error",
        "unknown_origin",
        "stuck",
        "timeout",
        "launch_failed",
        "output_truncated",
        "player_unavailable",
        "env_missing",
        # A landing the navigation policy refused, caught outside any step's window. Named
        # rather than folded into `harness_error`, because what to do about it is specific:
        # the run reached an address the project denied, and the scenario is what has to
        # stop going there.
        "navigation_blocked",
        # Every undecided failure was the scenario's own (ORIGIN_SCENARIO). Fixed by
        # editing the scenario, so it is neither retried nor counted by the repeat brake.
        "scenario_error",
    }
)

# Reasons the repeat brake neither counts nor clears. The caller's own input, corrected by
# coming back with it fixed — braking there would lock someone out of the fix, and clearing
# there would let a scenario typo erase a streak of real environment failures.
NOT_COUNTED_REASONS = frozenset({"scenario_error"})

# The repeat brake counts runs that keep ending the same way, and "the same way" has to be
# coarse enough to survive one problem wearing three names. An unreachable base URL surfaces
# as `base_url_unreachable` on one run, `stuck` on the next and `harness_error` on the third
# — keyed on the exact reason, each one started a fresh streak of one and the brake never
# fired against a loop that was plainly the same loop. Keyed on the rung, they accumulate.
#
# Three rungs, because they are the three different things a person does next: wait or raise
# a limit; fix the environment the run needs; fix the application under test.
_REPEAT_TIMEOUT = frozenset({"timeout", "stuck"})

def repeat_bucket(verdict: str | None, reason: str | None) -> str | None:
    """Which repeat rung a finished run belongs to, or None when nothing should be counted.

    A pass is the one outcome that clears the record rather than adding to it.

    An app failure arrives with `reason=None` — the reason field describes why a run could
    not reach a verdict, and a failing app IS the verdict. Counting it needs a value of its
    own, so it gets one here: without it, the failure most worth braking on was the only
    one that reset the streak on every run.
    """
    if verdict == "pass" or verdict is None:
        return None
    if verdict == "fail":
        return ORIGIN_APP
    if not reason or reason in NOT_COUNTED_REASONS:
        return None
    return "timeout" if reason in _REPEAT_TIMEOUT else ORIGIN_HARNESS


FAILURE_SIGNATURE_VERSION = 1
_SELECTOR_FIELDS = ("selector", "selector_candidates", "within")
_CONDITION_FIELDS = ("contains", "equals", "matches", "text", "value")


def failure_signature(report: dict, scenario: dict | None) -> str | None:
    """Which app failure this run was, precisely enough that a different one is not the same.

    Only for the `app` bucket. The coarse bucket above stays right for `timeout` and
    `harness`: those are the environment, which answers to several names in turn and is
    exactly what a scenario edit cannot change. An app failure is the opposite case. Keyed on
    the bucket alone, a form that had not rendered yet, a redirect the runtime itself held
    back and the same redirect again were one streak, and the fourth run — the first one
    whose scenario and runtime were both right — was refused.

    The page (the route after the step, identifiers folded to `:id`, query dropped), the
    action, the field (the step's selectors as the scenario wrote them) and the condition
    (the error kind, what the step expected, which readiness conditions never held). Read
    from the placeholder scenario and the report, so no resolved credential can reach it,
    and no page text: a message with a timestamp in it would make every run new.

    Not the step id: renaming a step is not a fix. Not the free-text detail, for the reason
    above. A fail with no failed step (an enforced console error, say) is signed by the run's
    reason, so a repeat of it still counts.
    """
    if report.get("browser_verdict") != "fail":
        return None
    steps = {str(step.get("id")): step for step in (scenario or {}).get("steps") or [] if isinstance(step, dict)}
    failures = report.get("failures") or []
    if not failures:
        basis = {"run": report.get("reason") or "fail"}
    else:
        row = failures[0]
        step = steps.get(str(row.get("step_id"))) or {}
        url = row.get("url_after")
        basis = {
            "page": sanitize_endpoint(url) if url else None,
            "action": row.get("action"),
            "field": {key: step[key] for key in _SELECTOR_FIELDS if key in step},
            "kind": row.get("error_kind"),
            "expected": {key: step[key] for key in _CONDITION_FIELDS if key in step and step.get("action") != "fill"},
            "unmet": sorted(json.dumps(item, sort_keys=True) for item in (row.get("ready_unmet") or [])),
        }
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    return f"v{FAILURE_SIGNATURE_VERSION}:{digest}"


# `proven`: a selector a passed run matched on this route under this `within` (validation
# refuses `proven` anywhere else) — as grounded as one the codebase named, so its absence
# from a settled page is the application's.
_GROUNDED = frozenset({"source", "existing_test", "runtime_probe", "proven"})
_SELECTOR_ERRORS = frozenset({"selector_missing", "selector_ambiguous", "not_visible"})


def classify_step(event: dict) -> str:
    """Origin of one failed step."""
    error = event.get("error") or {}
    kind = str(error.get("kind") or "")
    action = str(event.get("action") or "")
    provenance = event.get("selector_provenance")
    stable = event.get("page_stable")

    # mutation_blocked: the player's own write guard refused a request (allow_side_effects
    # false). The application did nothing wrong; the scenario asked for a write it may not
    # do. navigation_blocked: the scenario went somewhere the project's policy denies.
    if kind in {"navigation_blocked", "mutation_blocked"}:
        return ORIGIN_SCENARIO
    if kind in {"launch_failed", "browser_missing", "harness_error"}:
        return ORIGIN_HARNESS
    if kind == "selector_ambiguous":
        # Two or more elements matched. The element the claim is about IS on the page — the
        # selector is simply not unique, which is the scenario's to fix whatever its
        # provenance. Calling it `app` failed a login run on a password field whose
        # show/hide toggle carried the same label, and fed the app streak.
        return ORIGIN_SCENARIO
    if kind in _SELECTOR_ERRORS:
        if provenance == "heuristic":
            # A guessed selector that did not fit: the draft's, not the app's.
            return ORIGIN_SCENARIO
        if provenance in _GROUNDED:
            # A selector the codebase itself named is gone. On a page that finished
            # loading that is the application changing; on one that never settled it
            # could be either, and guessing costs a hidden regression.
            return ORIGIN_APP if stable is True else ORIGIN_UNKNOWN
        return ORIGIN_UNKNOWN
    if kind in {"timeout", "not_ready"}:
        # not_ready: the app's own indicator never said "ready" — a slow app and a broken
        # one look the same from here.
        return ORIGIN_UNKNOWN
    if action in {"expect_url", "expect_title"}:
        return ORIGIN_APP
    if action == "expect_dom":
        return ORIGIN_APP if provenance != "heuristic" else ORIGIN_UNKNOWN
    if kind in {"http_5xx", "page_error"}:
        return ORIGIN_APP
    return ORIGIN_UNKNOWN


def build_report(events: list[dict], scenario: dict | None, *, run_meta: dict | None = None) -> dict:
    """Fold the event stream into one report the normaliser can read.

    Returns:
      browser_verdict: pass | fail | incomplete
      reason: an INCOMPLETE_REASONS value when incomplete, else None
      claims: {id: {"severity", "status": proven|failed|unproven, "origin", "step", "detail"}}
      steps: {"total", "passed", "failed", "skipped"}
      failures: [{step, action, claim_id, origin, expected, actual, detail}]
      observations: [{kind, url, status, detail, same_origin, main_request}]
      harness: [{reason, detail}]
      requests: [the request ledger records, in order]
      network: {"rows": [request metadata, newest MAX_ROWS], "summary": {...}} | None
      trail: [{step_id, action, status, selection, ready, requests, error}] — step to
             selector to readiness to action to request to result, one row per step
      cleanup: {"status": not_planned|not_needed|passed|failed|not_run, "groups": [...],
                "steps": [...], "unplanned": [{step_id, reason}]}
    """
    run_meta = run_meta or {}
    claims: dict[str, dict] = {}
    for claim in (scenario or {}).get("claims") or []:
        if isinstance(claim, dict) and claim.get("id"):
            claims[str(claim["id"])] = {
                "severity": claim.get("severity", "blocking"),
                "status": "unproven",
                "origin": None,
                "step": None,
                "detail": None,
            }

    steps = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
    failures: list[dict] = []
    observations: list[dict] = []
    harness: list[dict] = []
    artifacts: list[dict] = []
    probes: list[dict] = []
    requests: list[dict] = []
    network_rows: list[dict] = []
    network_summary: dict | None = None
    trail: list[dict] = []
    cleanup_events: dict[str, dict] = {}
    finished = False

    for event in events:
        kind = event.get("type")
        if kind == "request":
            requests.append({key: event.get(key) for key in _REQUEST_FIELDS})
            continue
        if kind == "network":
            network_rows.extend(row for row in event.get("rows") or [] if isinstance(row, dict))
            continue
        if kind == "network_summary":
            network_summary = {key: value for key, value in event.items() if key != "type"}
            continue
        if kind == "cleanup":
            cleanup_events[str(event.get("step_id"))] = {key: event.get(key) for key in _CLEANUP_FIELDS}
            continue
        if kind == "progress":
            trail.append(
                {
                    "step": event.get("step"),
                    "step_id": event.get("step_id"),
                    "action": event.get("action"),
                    "status": event.get("status"),
                    "selector_provenance": event.get("selector_provenance"),
                    "selection": event.get("selection"),
                    "ready": event.get("ready"),
                    "error": (event.get("error") or {}).get("kind"),
                }
            )
            steps["total"] += 1
            status = event.get("status")
            cid = event.get("claim_id")
            if status == "passed":
                steps["passed"] += 1
                if event.get("action") == "probe":
                    probes.append({"step": event.get("step"), "probe": (event.get("actual") or {}).get("probe")})
                if cid in claims and claims[cid]["status"] == "unproven":
                    claims[cid]["status"] = "proven"
                    claims[cid]["step"] = event.get("step")
            elif status == "failed":
                steps["failed"] += 1
                origin = classify_step(event)
                detail = (event.get("error") or {}).get("detail") or ""
                failures.append(
                    {
                        "step": event.get("step"),
                        "step_id": event.get("step_id"),
                        "action": event.get("action"),
                        "claim_id": cid,
                        "origin": origin,
                        "expected": event.get("expected"),
                        "actual": event.get("actual"),
                        "detail": detail,
                        "probe": event.get("probe"),
                        # What the repeat brake needs to tell one app failure from another
                        # (failure_signature). Kind and conditions, never page text.
                        "error_kind": (event.get("error") or {}).get("kind"),
                        "url_after": event.get("url_after"),
                        "ready_unmet": (event.get("ready") or {}).get("unmet"),
                    }
                )
                if cid in claims:
                    claims[cid].update({"status": "failed", "origin": origin, "step": event.get("step"), "detail": detail})
            else:
                steps["skipped"] += 1
        elif kind == "observation":
            observations.append(
                {
                    "kind": event.get("kind"),
                    "url": event.get("url"),
                    "status": event.get("status"),
                    "detail": event.get("detail"),
                    "same_origin": bool(event.get("same_origin")),
                    "main_request": bool(event.get("main_request")),
                    "enforced": bool(event.get("enforced")),
                }
            )
        elif kind == "artifact":
            artifacts.append({key: event.get(key) for key in ("kind", "name", "step", "detail", "bytes", "pruned", "pruned_reason")})
        elif kind == "harness":
            harness.append({"reason": event.get("reason") or "harness_error", "detail": event.get("detail") or ""})
        elif kind == "result":
            finished = event.get("status") == "finished"

    # Existing project tests (run_meta["existing_tests"], from existing_tests.run): a pass
    # proves the claims the spec says it covers, unless the browser already failed them; a
    # failure or timeout makes them unknown; a runner that never started is the harness's.
    existing = run_meta.get("existing_tests") or {}
    tested_files = ", ".join(existing.get("files") or [])
    for cid in existing.get("covers") or []:
        claim = claims.get(cid)
        if claim is None or claim["status"] == "failed":
            continue
        status = existing.get("status")
        if status == "passed":
            claim.update({"status": "proven", "detail": f"existing test passed: {tested_files}"})
            continue
        kind = {"failed": "existing_test_failed", "timeout": "timeout"}.get(str(status), "harness_error")
        origin = classify_step({"action": "existing_test", "error": {"kind": kind}})
        detail = f"existing test {status}: {tested_files}"
        failures.append(
            {
                "step": None,
                "action": "existing_test",
                "claim_id": cid,
                "origin": origin,
                "expected": {"exit": 0},
                "actual": {"status": status, "returncode": existing.get("returncode")},
                "detail": detail,
                "probe": None,
            }
        )
        claim.update({"status": "failed", "origin": origin, "step": None, "detail": detail})

    # A same-origin 5xx on the main request or an uncaught same-origin page error is an
    # application failure even when every assertion passed — the assertion may simply
    # not have looked. A same-origin console error joins them only when the project
    # opted in (fail_on_console_error). Third-party and background noise stays a warning.
    app_errors = [
        obs
        for obs in observations
        if obs["same_origin"]
        and (
            (obs["kind"] == "http_5xx" and obs["main_request"])
            or obs["kind"] == "page_error"
            or (obs["kind"] == "console_error" and obs["enforced"])
        )
    ]

    reason = None
    if run_meta.get("timed_out"):
        reason = "timeout"
    elif run_meta.get("stalled"):
        reason = "stuck"
    elif run_meta.get("truncated"):
        reason = "output_truncated"
    elif run_meta.get("malformed"):
        reason = "harness_error"
    elif harness:
        reason = harness[0]["reason"] if harness[0]["reason"] in INCOMPLETE_REASONS else "harness_error"
    elif not finished:
        reason = "harness_error"

    app_failures = [f for f in failures if f["origin"] == ORIGIN_APP]
    undecided = [f for f in failures if f["origin"] != ORIGIN_APP]
    blocking_app = [
        f for f in app_failures if claims.get(f["claim_id"] or "", {}).get("severity", "blocking") == "blocking"
    ]

    if blocking_app or app_errors:
        verdict = "fail"
        reason = None
    elif reason is not None:
        verdict = "incomplete"
    elif undecided:
        verdict = "incomplete"
        if any(f["origin"] == ORIGIN_UNKNOWN for f in undecided):
            reason = "unknown_origin"
        elif all(f["origin"] == ORIGIN_SCENARIO for f in undecided):
            reason = "scenario_error"
        else:
            reason = "harness_error"
    elif any(c["status"] != "proven" for c in claims.values() if c["severity"] == "blocking"):
        verdict = "incomplete"
        reason = "harness_error"
    else:
        verdict = "pass"

    for row in trail:
        row["requests"] = [r["id"] for r in requests if r.get("attribution") == "step" and r.get("step_id") == row["step_id"] and r.get("phase") == "steps"]

    return {
        "browser_verdict": verdict,
        "reason": reason,
        "claims": claims,
        "steps": steps,
        "failures": failures,
        "app_errors": app_errors,
        "observations": observations,
        "harness": harness,
        "artifacts": artifacts,
        "probes": probes,
        "finished": finished,
        "requests": requests,
        # Request metadata for page optimization (DEC-014); None when the player sent none.
        "network": {"rows": network_rows, "summary": network_summary} if network_summary is not None else None,
        "trail": trail,
        "cleanup": cleanup_report(scenario, cleanup_events, requests, {str(row["step_id"]): row["status"] for row in trail}),
    }


# ---- diagnosis ---------------------------------------------------------------------
# One structured answer per run that did not pass: why it failed, what failed, what to do
# next, what to fix, and whether the next draft should be told. Enums, so a run's outcome
# can be counted and compared; the hint is fixed text per cause, never page text, so it
# can carry no resolved value and does not change from one run to the next.
DIAGNOSIS_VERSION = 1
NEXT_STEPS = ("fix_scenario", "fix_app", "fix_environment", "raise_limit", "investigate")
_CAUSES = {
    # cause: (next_step, reusable, fix_hint)
    "scenario_selector": (
        "fix_scenario", True,
        "The draft's selector matched no element or several. Ground it in source (role/label/data-e2e) or add a probe step, then draft again.",
    ),
    "scenario_write_policy": (
        "fix_scenario", False,
        "The step sends a write this run may not do. Drop the step, or approve it: settings.allow_side_effects, allowed_read_only_requests, allowed_destructive_requests.",
    ),
    "scenario_navigation_policy": (
        "fix_scenario", False,
        "The scenario navigated somewhere the project's policy denies (allowed_origins or blocked_requests). Change the route, not the policy, unless the policy is wrong.",
    ),
    "scenario_selector_ambiguous": (
        "fix_scenario", True,
        "The selector matched several elements. Make it unique — scope it with `within`, use role plus name, or a data-e2e tag — then run again.",
    ),
    "selector_not_rendered": (
        "investigate", True,
        "A selector cited from source is not on the rendered page. Either the application changed or the source_ref is wrong; probe the route before changing either.",
    ),
    "app_assertion": (
        "fix_app", False,
        "An assertion grounded in the claim failed on a page that loaded. Treat it as a regression until the claim itself is shown to be wrong.",
    ),
    "app_runtime_error": (
        "fix_app", False,
        "The application raised an error (same-origin 5xx on the main request, an uncaught page error, or an enforced console error).",
    ),
    "environment_slow": (
        "raise_limit", False,
        "The page never reached the declared readiness in time. Check the app is warm and the ready conditions are the app's own, then raise step_timeout_ms or total_timeout_s.",
    ),
    "environment_setup": (
        "fix_environment", False,
        "The browser or the application could not be reached. Install or start what the reason names; editing the scenario will not help.",
    ),
    "runtime_harness": (
        "investigate", False,
        "The runtime could not finish the run (malformed or truncated player output, a harness error). Read report.json and the player stderr before running again.",
    ),
    "unknown": (
        "investigate", False,
        "The evidence cannot tell the app from the scenario. Probe the failing step's page and read the trail before editing anything.",
    ),
}
CAUSES = tuple(_CAUSES)
_SETUP_REASONS = frozenset({"playwright_missing", "browser_missing", "base_url_unreachable", "launch_failed", "player_unavailable"})


def _cause_of(report: dict) -> tuple[str, dict | None]:
    failures = report.get("failures") or []
    reason = report.get("reason")
    first = failures[0] if failures else None
    if report.get("browser_verdict") == "fail":
        app = next((f for f in failures if f.get("origin") == ORIGIN_APP), None)
        if app is None:
            return "app_runtime_error", None
        if app.get("error_kind") in {"selector_missing", "not_visible"}:
            return "selector_not_rendered", app
        return "app_assertion", app
    if reason in _SETUP_REASONS:
        return "environment_setup", first
    if reason in {"timeout", "stuck"}:
        return "environment_slow", first
    if first is not None:
        kind = first.get("error_kind")
        if first.get("origin") == ORIGIN_SCENARIO:
            if kind == "selector_ambiguous":
                return "scenario_selector_ambiguous", first
            if kind == "mutation_blocked":
                return "scenario_write_policy", first
            if kind == "navigation_blocked":
                return "scenario_navigation_policy", first
            return "scenario_selector", first
        if kind in {"timeout", "not_ready"}:
            return "environment_slow", first
    if reason == "navigation_blocked":
        return "scenario_navigation_policy", first
    if reason in {"harness_error", "output_truncated"}:
        return "runtime_harness", first
    return "unknown", first


def diagnose(report: dict) -> dict | None:
    """`{version, cause, next_step, failed, reason, fix_hint, reusable}` for a run that did not pass.

    `failed` names the step by id, action, claim and error kind — never expected or actual
    values, which may hold page text. `reusable` marks the causes the next draft can act on
    (a selector that did not fit, one the page did not render); those are written to the
    browser knowledge store for it.
    """
    if report.get("browser_verdict") in (None, "pass"):
        return None
    cause, failure = _cause_of(report)
    next_step, reusable, hint = _CAUSES[cause]
    failed = None
    if failure is not None:
        failed = {key: failure.get(key) for key in ("step_id", "action", "claim_id", "error_kind", "origin")}
    return {
        "version": DIAGNOSIS_VERSION,
        "cause": cause,
        "next_step": next_step,
        "failed": failed,
        "reason": report.get("reason"),
        "fix_hint": hint,
        "reusable": reusable,
    }


_BUCKET_CAUSE = {ORIGIN_APP: "app_assertion", ORIGIN_HARNESS: "runtime_harness", "timeout": "environment_slow"}


def diagnosis_for_cause(cause: str | None, bucket: str | None = None, *, reason: str | None = None, repeat: bool = False) -> dict:
    """The structured diagnosis for a cause known without a report — a repeat record's.

    `repeat` marks the brake's version: after E2E_REPEAT_LIMIT identical outcomes the next
    step is never "edit the scenario and try again" for a cause the scenario does not own.
    """
    if cause not in _CAUSES:
        if reason in _SETUP_REASONS:
            cause = "environment_setup"
        elif reason in {"timeout", "stuck"}:
            cause = "environment_slow"
        else:
            cause = _BUCKET_CAUSE.get(str(bucket or ""), "unknown")
    next_step, reusable, hint = _CAUSES[cause]
    return {
        "version": DIAGNOSIS_VERSION,
        "cause": cause,
        "next_step": next_step,
        "failed": None,
        "reason": "repeat_failure" if repeat else None,
        "fix_hint": hint,
        "reusable": reusable,
    }


_REQUEST_FIELDS = (
    "id", "phase", "step_id", "after_step", "attribution", "method", "endpoint", "resource_type",
    "read_only", "blocked", "planned", "status", "failure", "duration_ms",
)
_CLEANUP_FIELDS = ("step_id", "cleans", "action", "status", "expected", "actual", "error", "duration_ms", "detail", "selection", "ready")


def cleanup_report(scenario: dict | None, events: dict[str, dict], requests: list[dict], step_statuses: dict[str, str] | None = None) -> dict:
    """The cleanup's own result, separate from the test's.

    A planned cleanup step with no event means the player never got there (killed, timed
    out, crashed): `not_run`, which the normaliser treats like a failure — data may remain.
    The exception is a target the report shows as `skipped`: it never ran, so it wrote
    nothing. A target with no progress event at all may have been killed mid-write, and
    stays `not_run`.
    Per target: all steps `not_needed` → not_needed; any failed → failed; any missing →
    not_run; otherwise passed. Overall: failed beats not_run beats passed beats not_needed.
    """
    scenario = scenario or {}
    planned = [s for s in scenario.get("cleanup") or [] if isinstance(s, dict)]
    unplanned = [
        {"step_id": s.get("id"), "side_effect": s.get("side_effect"), "reason": s.get("no_cleanup_reason")}
        for s in scenario.get("steps") or []
        if isinstance(s, dict) and s.get("no_cleanup_reason")
    ]
    if not planned:
        return {"status": "not_planned", "groups": [], "steps": [], "unplanned": unplanned}
    rows: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for index, step in enumerate(planned, start=1):
        step_id = str(step.get("id") or f"cleanup-{index}")
        row = events.get(step_id)
        if row is None:
            never_ran = (step_statuses or {}).get(str(step.get("cleans"))) == "skipped"
            row = {"step_id": step_id, "cleans": step.get("cleans"), "action": step.get("action"), "status": "not_needed" if never_ran else "not_run"}
        row = {**row, "requests": [r["id"] for r in requests if r.get("phase") == "cleanup" and r.get("step_id") == step_id]}
        rows.append(row)
        groups.setdefault(str(step.get("cleans") or ""), []).append(row)
    summary = []
    for target, group in groups.items():
        statuses = [row.get("status") for row in group]
        if all(status == "not_needed" for status in statuses):
            status = "not_needed"
        elif "failed" in statuses:
            status = "failed"
        elif "not_run" in statuses:
            status = "not_run"
        else:
            status = "passed"
        failing = next((row for row in group if row.get("status") in ("failed", "not_run")), None)
        detail = None
        if failing is not None:
            detail = ((failing.get("error") or {}).get("detail") if failing.get("status") == "failed" else "the player never reached this cleanup step")
        summary.append({"cleans": target, "status": status, "detail": detail})
    order = ("failed", "not_run", "passed", "not_needed")
    overall = next(status for status in order if any(g["status"] == status for g in summary))
    return {"status": overall, "groups": summary, "steps": rows, "unplanned": unplanned}
