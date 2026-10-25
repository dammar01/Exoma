"""From a browser report to the one contract the runtime already judges.

Two outputs. `evidence_block` is the compact `[E2E EVIDENCE]` a hybrid reviewer reads —
claims, assertion results, classified failures, artifact pointer; never DOM, logs, or
screenshots. `to_verification` is the canonical `[VERIFICATION]` that becomes the
result's content, so `_finalize_verify_result`, the continuation predicate, the
invalid-evidence guard, and the exit-code mapping all run unchanged.

Combination is fail-closed (plan §14.2): a browser `fail` cannot be talked back to
`pass` by a reviewer, a reviewer that found a blocking issue fails a browser `pass`, and
anything the browser could not finish stays `incomplete`.

The `origin` tag on a finding keeps its temporal meaning (introduced / regression /
pre_existing / unknown). The player's app/harness/unknown classification is a different
axis and lives in `evidence_source` and in the `not_verified` reasons, never in `origin`.
"""

from __future__ import annotations

import re

from exoma.core.evidence.contract import (
    _NONE_ITEM,
    _expected_verification_section,
    _verification_tags,
    validate_verification_contract,
)

EVIDENCE_MARKER = "[E2E EVIDENCE]"
_VERIFICATION_SECTIONS = ("blocking_findings", "escalations", "notes", "checks_run", "not_verified")
MAX_TRAIL_LINES = 40
# Cleanup results that leave the target possibly dirty: the run cannot be a pass.
CLEANUP_UNSETTLED = frozenset({"failed", "not_run"})


def _short(value, limit: int = 160) -> str:
    text = str(value) if value is not None else ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _trail_detail(row: dict) -> str:
    parts = []
    selection = row.get("selection") or {}
    if selection:
        counts = ",".join("?" if c is None else str(c) for c in selection.get("match_counts") or [])
        chosen = selection.get("candidate")
        used = "none matched" if chosen is None else f"candidate {chosen}" + (" (fallback)" if selection.get("fallback_used") else "")
        parts.append(f"selector: {used}, matches [{counts}], {row.get('selector_provenance') or 'no provenance'}")
    ready = row.get("ready") or {}
    if ready:
        unmet = f", unmet: {'; '.join(ready.get('unmet') or [])}" if ready.get("unmet") else ""
        parts.append(f"ready: {ready.get('conditions')} condition(s) in {ready.get('waited_ms')}ms{unmet}")
    if row.get("requests"):
        parts.append(f"requests: {', '.join(row['requests'])}")
    parts.append(f"result: {row.get('status')}" + (f" ({row['error']})" if row.get("error") else ""))
    return _short(" | ".join(parts), 300)


def _request_line(item: dict) -> str:
    where = f"step {item.get('step_id')}" if item.get("attribution") == "step" else f"uncertain (after {item.get('after_step') or 'no step'})"
    if item.get("blocked"):
        outcome = _short(item.get("failure"), 120)
    elif item.get("failure"):
        outcome = f"failed: {_short(item.get('failure'), 80)}"
    else:
        outcome = f"HTTP {item.get('status')}"
    planned = {True: "planned", False: "not planned", None: "unattributed"}.get(item.get("planned"), "unattributed")
    read = " read-only" if item.get("read_only") else ""
    return (
        f"{item.get('id')} {item.get('method')}{read} {item.get('endpoint')} | {item.get('phase')} {where} | "
        f"{outcome} | {item.get('duration_ms')}ms | {planned}"
    )


def _probe_summary(probe: object) -> str:
    """A probe as one bounded line: what a reviewer needs to judge a selector, not the DOM."""
    if not isinstance(probe, dict):
        return "none"
    parts = []
    for bucket, field in (("headings", None), ("buttons", "name"), ("inputs", "label"), ("links", "name")):
        items = probe.get(bucket) or []
        if not items:
            continue
        names = [
            str(item if field is None else ((item or {}).get(field) or (item or {}).get("e2e") or "?"))
            for item in items[:6]
        ]
        parts.append(f"{bucket}: {', '.join(names)}{' …' if len(items) > 6 else ''}")
    if probe.get("truncated"):
        parts.append("truncated")
    return _short(" | ".join(parts) or "no visible elements", 300)


def evidence_block(report: dict, *, artifacts: str | None = None, spec_notes: list[str] | None = None) -> str:
    claims = report.get("claims") or {}
    steps = report.get("steps") or {}
    lines = [
        EVIDENCE_MARKER,
        f"browser_verdict: {report.get('browser_verdict')}"
        + (f" ({report.get('reason')})" if report.get("reason") else ""),
    ]
    diagnosis = report.get("diagnosis")
    if diagnosis:
        failed = diagnosis.get("failed") or {}
        where = f" | step: {failed.get('step_id')} {failed.get('action')}" if failed else ""
        lines.append(f"diagnosis: {diagnosis.get('cause')} | next_step: {diagnosis.get('next_step')}{where} — {diagnosis.get('fix_hint')}")
    lines += [
        "claims:",
    ]
    for cid, claim in claims.items():
        origin = f" | origin: {claim['origin']}" if claim.get("origin") else ""
        lines.append(f"- {cid} | {claim.get('severity')} | status: {claim.get('status')}{origin}")
    if not claims:
        lines.append("- none")
    lines.append(
        f"steps: {steps.get('total', 0)} total, {steps.get('passed', 0)} passed, "
        f"{steps.get('failed', 0)} failed, {steps.get('skipped', 0)} skipped"
    )
    lines.append("assertion_failed:")
    failures = [f for f in report.get("failures") or [] if f.get("origin") == "app"]
    for failure in failures:
        lines.append(
            f"- claim: {failure.get('claim_id')} | step {failure.get('step')} {failure.get('action')}"
            f" | expected: {_short(failure.get('expected'))} | actual: {_short(failure.get('actual'))}"
        )
        if failure.get("probe"):
            lines.append(f"  probe: {_probe_summary(failure['probe'])}")
    if not failures:
        lines.append("- none")
    lines.append("app_errors:")
    for obs in report.get("app_errors") or []:
        lines.append(f"- {obs.get('kind')} {obs.get('status') or ''} {_short(obs.get('url'))} — {_short(obs.get('detail'))}")
    if not report.get("app_errors"):
        lines.append("- none")
    lines.append("harness_issues:")
    harness = [f for f in report.get("failures") or [] if f.get("origin") != "app"]
    for failure in harness:
        lines.append(
            f"- step {failure.get('step')} {failure.get('action')} | origin: {failure.get('origin')}"
            f" | claim: {failure.get('claim_id')} — {_short(failure.get('detail'))}"
        )
        if failure.get("probe"):
            lines.append(f"  probe: {_probe_summary(failure['probe'])}")
    for item in report.get("harness") or []:
        lines.append(f"- {item.get('reason')} — {_short(item.get('detail'))}")
    if not harness and not report.get("harness"):
        lines.append("- none")
    lines.append("observations:")
    noise = [o for o in report.get("observations") or [] if o not in (report.get("app_errors") or [])]
    for obs in noise:
        lines.append(f"- {obs.get('kind')} {obs.get('status') or ''} {_short(obs.get('url'))} (warning)")
    if not noise:
        lines.append("- none")
    if spec_notes:
        lines.append("spec_uncertainties:")
        lines.extend(f"- {_short(note)}" for note in spec_notes)
    tests = report.get("existing_tests") or []
    if tests:
        lines.append("existing_tests:")
        for test in tests:
            lines.append(
                f"- {test.get('path')} | covers: {', '.join(test.get('covers') or []) or '?'}"
                f" | {test.get('result') or 'listed, not run'}"
            )
    probes = report.get("probes") or []
    if probes:
        lines.append("probes:")
        lines.extend(f"- step {p.get('step')}: {_probe_summary(p.get('probe'))}" for p in probes)
    trail = report.get("trail") or []
    if trail:
        # step -> selector -> readiness -> action -> requests -> result, one bounded line each
        lines.append("trail:")
        for row in trail[:MAX_TRAIL_LINES]:
            lines.append(f"- {row.get('step_id') or row.get('step')} {row.get('action')} | {_trail_detail(row)}")
        if len(trail) > MAX_TRAIL_LINES:
            lines.append(f"- … {len(trail) - MAX_TRAIL_LINES} more in report.json")
    requests = report.get("requests") or []
    if requests:
        lines.append("requests:")
        for item in requests[:MAX_TRAIL_LINES]:
            lines.append(f"- {_request_line(item)}")
        if len(requests) > MAX_TRAIL_LINES:
            lines.append(f"- … {len(requests) - MAX_TRAIL_LINES} more in report.json")
    cleanup = report.get("cleanup") or {}
    if (cleanup.get("status") and cleanup.get("status") != "not_planned") or cleanup.get("unplanned"):
        lines.append(f"cleanup: {cleanup.get('status')}")
        for group in cleanup.get("groups") or []:
            detail = f" — {_short(group.get('detail'))}" if group.get("detail") else ""
            lines.append(f"- cleans {group.get('cleans')}: {group.get('status')}{detail}")
        for item in cleanup.get("unplanned") or []:
            lines.append(f"- {item.get('step_id')} ({item.get('side_effect')}): no cleanup planned — {_short(item.get('reason'))}")
    files = report.get("artifacts") or []
    if files:
        lines.append("artifact_files:")
        for item in files:
            step = f", step {item['step']}" if item.get("step") else ""
            if item.get("pruned") and item.get("pruned_reason") == "unscrubbed":
                note = " — withheld: could not be scrubbed; do not open"
            elif item.get("pruned"):
                note = " — pruned: over settings.artifact_max_mb"
            elif item.get("detail"):
                note = f" — {_short(item.get('detail'))}"
            else:
                note = ""
            lines.append(f"- {item.get('name') or item.get('kind')} ({item.get('kind')}{step}){note}")
    lines.append(f"artifacts: {artifacts or 'none'}")
    return "\n".join(lines)


_HEAD = re.compile(r"^([a-z_]+)\s*:", re.IGNORECASE)
_KNOWN_HEADS = {*_VERIFICATION_SECTIONS, "verdict", "confidence"}


def _section_blocks(body: str, name: str) -> list[str]:
    """Bullet items under `name:` WITH their indented continuation lines.

    The shared contract parser keeps one line per finding, which is all a verdict needs.
    Carrying a reviewer's finding forward needs the whole thing — problem, trigger,
    impact, fix — so this walks the same section boundaries and keeps every line.
    """
    lines = body.splitlines()
    items: list[str] = []
    current: list[str] | None = None
    collecting = False
    for line in lines:
        stripped = line.strip()
        head = _HEAD.match(stripped)
        if head and head.group(1).lower() in _KNOWN_HEADS and not stripped.startswith("-"):
            if collecting:
                break
            collecting = head.group(1).lower() == name
            inline = stripped.split(":", 1)[1].strip()
            if collecting and inline and not inline.startswith("#"):
                current = [inline.lstrip("-").strip()]
                items.append("")  # placeholder replaced below
            continue
        if not collecting:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            break
        if stripped.startswith("-"):
            if current is not None:
                items[-1] = "\n  ".join(current)
            current = [stripped.lstrip("-").strip()]
            items.append("")
        elif stripped and current is not None:
            current.append(stripped)
    if current is not None and items:
        items[-1] = "\n  ".join(current)
    return [item for item in items if item and not _is_none_item(item)]


def _is_none_item(item: str) -> bool:
    """A sentinel, judged by its first line. An item that continues on further lines gets
    only the plain forms (`none`, `none (why)`): a separator note such as `none - clean`
    in front of a continuation line could hide the finding written there."""
    first, _, more = item.partition("\n")
    if more.strip():
        return _NONE_ITEM.fullmatch_plain(first)
    return _NONE_ITEM.fullmatch(first)


def _reviewer_sections(content: str | None) -> dict[str, list[str]]:
    """The reviewer's items, with findings re-filed where the routing table puts them.

    The shared validator decides blocking from each finding's tags, not from the heading
    it was written under. Copying sections verbatim would let a `high | introduced`
    finding filed under `escalations` read as non-blocking here and blocking there — the
    content saying INCOMPLETE while the runtime computes `fail`. Re-filing by the same
    table keeps the two readings identical. A finding whose tags do not parse stays where
    the reviewer put it; the validator treats it the same way.
    """
    if not content or "[VERIFICATION]" not in content:
        return {name: [] for name in _VERIFICATION_SECTIONS}
    body = content.split("[VERIFICATION]", 1)[1]
    raw = {name: _section_blocks(body, name) for name in _VERIFICATION_SECTIONS}
    routed = {name: [] for name in _VERIFICATION_SECTIONS}
    routed["checks_run"] = raw["checks_run"]
    routed["not_verified"] = raw["not_verified"]
    for name in ("blocking_findings", "escalations", "notes"):
        for item in raw[name]:
            tags, invalid = _verification_tags(item.split("\n", 1)[0])
            target = name if invalid else _expected_verification_section(tags)
            routed[target].append(item)
    return routed


def _reviewer_confidence(content: str | None) -> str | None:
    if not content:
        return None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("confidence:"):
            return stripped.split(":", 1)[1].strip()
    return None


def _mentions(items: list[str], claim_id: str | None) -> bool:
    return bool(claim_id) and any(claim_id in item for item in items)


def to_verification(
    report: dict,
    *,
    reviewer_content: str | None = None,
    reviewer_error: str | None = None,
    preflight: dict | None = None,
    existing_tests: list[dict] | None = None,
    attempts: list[dict] | None = None,
) -> dict:
    """Compose the canonical contract. Returns {content, verdict, declared, warnings}.

    `verdict` is the effective runtime verdict (pass|fail|incomplete) the caller must
    put on `meta.verdict` BEFORE the shared finaliser runs; the content is written so
    that `validate_verification_contract` reaches the same answer on its own.
    """
    claims = report.get("claims") or {}
    failures = report.get("failures") or []
    browser = report.get("browser_verdict")
    sections = _reviewer_sections(reviewer_content)
    assessment = validate_verification_contract(reviewer_content) if reviewer_content else None

    blocking: list[str] = []
    escalations: list[str] = list(sections["escalations"])
    notes: list[str] = list(sections["notes"])
    checks: list[str] = []
    gaps: list[str] = []

    if preflight and not preflight.get("ok"):
        reason = preflight.get("reason") or "harness_error"
        gaps.append(f"e2e preflight: {reason} — {_short(preflight.get('detail'))}; no claim was exercised")
        for cid in claims:
            gaps.append(f"e2e:{cid}: not run ({reason})")

    for failure in failures:
        cid = failure.get("claim_id")
        severity = claims.get(cid or "", {}).get("severity", "blocking")
        where = f"[runtime:e2e:step {failure.get('step')}]"
        if failure.get("origin") == "app":
            if _mentions(sections["blocking_findings"], cid):
                continue  # the reviewer already tagged this one with a temporal origin
            problem = (
                f"claim `{cid}` failed at runtime (evidence_source: e2e_runtime): "
                f"{failure.get('action')} expected {_short(failure.get('expected'))}, "
                f"actual {_short(failure.get('actual'))} {where}"
            )
            if severity == "blocking":
                blocking.append(f"severity: high | origin: unknown | scope_relation: in_scope — {problem}")
            else:
                notes.append(f"severity: medium | origin: unknown | scope_relation: in_scope — {problem}")
        else:
            gaps.append(
                f"e2e:{cid or failure.get('action')}: {failure.get('origin')} origin — "
                f"{_short(failure.get('detail'))} {where}"
            )

    for obs in report.get("app_errors") or []:
        problem = (
            f"{obs.get('kind')} {obs.get('status') or ''} on {_short(obs.get('url'))} "
            f"(evidence_source: e2e_runtime) — {_short(obs.get('detail'))} [runtime:e2e:{obs.get('kind')}]"
        )
        if not any(_short(obs.get("url")) in item for item in sections["blocking_findings"]):
            blocking.append(f"severity: high | origin: unknown | scope_relation: in_scope — {problem}")

    for obs in report.get("observations") or []:
        if obs in (report.get("app_errors") or []):
            continue
        notes.append(
            f"severity: low | origin: unknown | scope_relation: out_of_scope — "
            f"{obs.get('kind')} {obs.get('status') or ''} {_short(obs.get('url'))} observed, not attributed to the change"
        )

    for item in sections["blocking_findings"]:
        if item not in blocking:
            blocking.append(item)

    if browser is not None and not (preflight and not preflight.get("ok")):
        steps = report.get("steps") or {}
        checks.append(
            f"e2e:player: {steps.get('total', 0)} steps, {steps.get('passed', 0)} passed, "
            f"{steps.get('failed', 0)} failed — browser verdict {browser}"
        )
        for cid, claim in claims.items():
            if claim.get("status") == "proven":
                checks.append(f"e2e:{cid}: pass (step {claim.get('step')})")
            elif claim.get("status") == "unproven":
                gaps.append(f"e2e:{cid}: never reached — {report.get('reason') or 'run ended early'}")
    for test in existing_tests or []:
        checks.append(
            f"e2e:existing_test: {test.get('path')} covers {', '.join(test.get('covers') or []) or '?'}"
            f" — {test.get('result') or 'listed, not run'}"
        )
    if report.get("reason") and browser == "incomplete" and not any(report["reason"] in g for g in gaps):
        gaps.append(f"e2e run: {report['reason']}")

    # The cleanup is judged apart from the test: a proven claim stays proven, but a target
    # that may still hold this run's data is not a clean pass.
    cleanup = report.get("cleanup") or {}
    cleanup_unsettled = False
    if browser is not None and not (preflight and not preflight.get("ok")):
        for group in cleanup.get("groups") or []:
            if group.get("status") in CLEANUP_UNSETTLED:
                cleanup_unsettled = True
                gaps.append(
                    f"e2e cleanup: '{group.get('cleans')}' {group.get('status')} — "
                    f"{_short(group.get('detail') or 'no detail')}; test data may remain"
                )
            elif group.get("status") == "passed":
                checks.append(f"e2e cleanup: '{group.get('cleans')}' cleaned and asserted")
        for item in cleanup.get("unplanned") or []:
            notes.append(
                f"severity: low | origin: unknown | scope_relation: in_scope — step {item.get('step_id')} "
                f"({item.get('side_effect')}) has no cleanup by plan: {_short(item.get('reason'))}"
            )

    checks.extend(sections["checks_run"])
    gaps.extend(sections["not_verified"])

    # A run that needed a retry to finish is not a clean pass. The retry exists for a flaky
    # environment, but `timeout` and `not_ready` are also what an intermittently broken app
    # looks like, and a pass on the second try is exactly a bug hiding behind a re-roll.
    earlier = [a for a in (attempts or [])[:-1] if a.get("browser_verdict") != "pass"]
    if earlier and browser == "pass":
        reasons = ", ".join(sorted({str(a.get("reason") or a.get("browser_verdict")) for a in earlier}))
        gaps.append(
            f"e2e run: passed only on attempt {len(attempts)} of {len(attempts)} — the earlier "
            f"attempt(s) ended {reasons}; a flaky environment or an intermittent application "
            "failure, not a clean pass"
        )

    if reviewer_content is None:
        if reviewer_error:
            gaps.append(f"hybrid review: not obtained ({reviewer_error}); coverage of the change was not judged")
        elif browser == "pass":
            # Hybrid review always runs after a browser pass, so this is a caller that skipped
            # it. A browser pass alone never stands in for a judged change.
            gaps.append("hybrid review: not run; coverage of the change was not judged")

    # Fail-closed combination.
    if browser == "fail":
        declared, verdict = "NEEDS FIX", "fail"
    elif browser != "pass":
        declared, verdict = "INCOMPLETE", "incomplete"
    elif assessment is not None and assessment["verdict"] == "fail" and blocking:
        declared, verdict = "NEEDS FIX", "fail"
    elif assessment is not None and (
        assessment["declared_verdict"] != "DONE"
        or any(w.get("kind") != "verification_gap" for w in assessment.get("warnings") or [])
    ):
        declared, verdict = "INCOMPLETE", "incomplete"
    elif blocking:
        declared, verdict = "NEEDS FIX", "fail"
    else:
        declared, verdict = "DONE", "incomplete" if gaps else "pass"
    if declared == "NEEDS FIX" and not blocking:
        # Cannot happen for a browser fail (it always adds a finding), kept as a guard so
        # the contract never carries a NEEDS FIX the validator would reject.
        declared, verdict = "INCOMPLETE", "incomplete"
    if cleanup_unsettled and verdict != "fail":
        # Declared INCOMPLETE, never DONE-with-a-gap: the gap-only exit 0 is for a judgement
        # nobody made, not for data left behind in the target.
        declared, verdict = "INCOMPLETE", "incomplete"

    def section(name: str, items: list[str]) -> list[str]:
        return [f"{name}:", *(f"- {item}" for item in items), *(["- none"] if not items else []), ""]

    def compose(declared: str, verdict: str) -> str:
        if browser == "fail":
            confidence = "high — the application violated a grounded claim in a real browser run"
        elif verdict != "pass" and declared != "DONE":
            confidence = "low — verification did not complete; see not_verified"
        else:
            reviewer = _reviewer_confidence(reviewer_content) or ""
            confidence = (
                reviewer
                if reviewer.split(" ", 1)[0].lower() in {"low", "medium", "high"}
                else "medium — browser run passed; coverage judged only by the runtime"
            )
        return "\n".join(
            [
                "[VERIFICATION]",
                f"verdict: {declared}",
                "",
                *section("blocking_findings", blocking),
                *section("escalations", escalations),
                *section("notes", notes),
                *section("checks_run", checks),
                *section("not_verified", gaps),
                f"confidence: {confidence}",
            ]
        )

    warnings = []
    content = compose(declared, verdict)
    # The contract is only worth anything if the runtime reads it the way it was meant.
    # Whatever this function concluded, the shared validator gets the last word when it
    # is stricter — a reviewer line that fails to parse, a tag the table routes
    # differently — and the declared verdict follows so the two never disagree.
    rank = {"pass": 0, "incomplete": 1, "fail": 2}
    # Twice at most: re-declaring can itself change what the validator reads (a DONE
    # that becomes INCOMPLETE drops the gap-only exit), so the second pass confirms the
    # rewritten contract instead of assuming it.
    for _ in range(2):
        checked = validate_verification_contract(content)
        if rank[checked["verdict"]] <= rank[verdict]:
            break
        warnings.append(
            {
                "kind": "normalized_verdict_tightened",
                "detail": f"{verdict} -> {checked['verdict']} to match the shared validator",
            }
        )
        verdict = checked["verdict"]
        if verdict == "fail" and blocking:
            declared = "NEEDS FIX"
        elif verdict != "pass" and declared == "DONE" and not all(
            w.get("kind") == "verification_gap" for w in checked.get("warnings") or []
        ):
            declared = "INCOMPLETE"
        content = compose(declared, verdict)
    if assessment is not None and reviewer_content and assessment["verdict"] == "pass" and browser == "fail":
        warnings.append({"kind": "reviewer_verdict_overridden", "detail": "browser fail outranks reviewer pass"})
    return {"content": content, "verdict": verdict, "declared": declared, "warnings": warnings}
