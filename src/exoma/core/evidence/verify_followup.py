"""Advisory, execution-scoped follow-up for standard verification rounds."""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import uuid

from exoma.core.evidence.contract import (
    _expected_verification_section, _meaningful_items, _section_items, _verification_tags,
)
from exoma.core.workspace.workspace_paths import atomic_write_text, now_iso, workflow_paths
from exoma.core.policy.secret_patterns import is_secret_path
from exoma.utils import git
from exoma.utils.redact import redact_value

_ACTIVE = ContextVar("verify_round", default=None)
MAX_PRIOR = 32
MAX_BLOCK = 65536


class StaleRound(ValueError):
    """The execution changed before the reviewer received its pinned inputs."""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def binding(current, round_id=None, *, input_id=None):
    if not current or not current.get("execution_id"):
        return None
    history = current.get("finding_history", {})
    return {"execution_id": current["execution_id"], "previous": history.get("revision", 0),
            "round_id": round_id or uuid.uuid4().hex, "input": input_id}


def bind_current(root, current, round_id=None):
    from exoma.core.evidence import verify_tests

    if current and current.get("execution_id"):
        return binding(current, round_id, input_id=verify_tests._identity(root))
    return None


def admission(root, current, round_id):
    """Persist the execution identity even for records predating finding history."""
    from exoma.core.runtime.execution import execution_identity

    identity = execution_identity(current)
    if isinstance(identity, tuple):
        return {"legacy_identity": list(identity)}
    return bind_current(root, current, round_id)


def matches_revision(current, pin):
    return binding(current, pin["round_id"], input_id=pin.get("input")) == pin


@contextlib.contextmanager
def using(pin):
    token = _ACTIVE.set(pin)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active():
    return _ACTIVE.get()


def summary(current):
    ledger = (current or {}).get("finding_history", {})
    def for_review(finding):
        # Older runtime findings can lack first_checks. Only project a unique
        # first-round failure; committed observations remain immutable.
        if not finding.get("command") or finding.get("first_checks"):
            return finding
        first_round = ledger.get("rounds", {}).get(finding.get("first_round"), {})
        matches = [c for c in first_round.get("checks", [])
                   if c.get("command") == finding["command"] and c.get("status") in ("failed", "timeout", "error")]
        if len(matches) == 1:
            return {**finding, "first_checks": matches}
        return finding

    pending = [f for f in ledger.get("findings", {}).values() if f["disposition"] == "open"]
    pending.sort(key=lambda f: (f.get("reviewed_revision", 0), f["id"]))
    closed = [f for f in ledger.get("findings", {}).values() if f["disposition"] == "resolved"]
    closed.sort(key=lambda f: f.get("reviewed_revision", 0), reverse=True)
    return {"version": 1, "revision": ledger.get("revision", 0), "open_count": len(pending),
            "open": [for_review(f) for f in pending[:MAX_PRIOR]],
            "recently_resolved": [for_review(f) for f in closed[:MAX_PRIOR]],
            "remaining": max(0, len(pending) - MAX_PRIOR)}


def handoff(root, session_id, current, request):
    pin = active()
    if not pin:
        return None
    prior = summary(current)
    if not matches_revision(current, pin):
        raise StaleRound("verification snapshot is stale; prepare a fresh verification")
    selection = (request or {}).get("selection") or (current or {}).get("selection") or {}
    obligations = selection.get("obligations") if isinstance(selection, dict) else None
    data = {**prior, **pin, "planned_obligations": obligations[:MAX_PRIOR] if isinstance(obligations, list) else [],
            "planned_count": len(obligations) if isinstance(obligations, list) else 0}
    path = workflow_paths(Path(root), session_id)["runtime_dir"] / ("verify-" + pin["round_id"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(redact_value(data)[0], ensure_ascii=False))
    return path.name


def rows(content):
    before, marker, _ = content.partition("[VERIFICATION]")
    if not marker or len(before) > MAX_BLOCK + 1024 or before.count("[FOLLOWUP_V1]") != 1:
        return []
    block = before.split("[FOLLOWUP_V1]", 1)[1].strip()
    if len(block) > MAX_BLOCK:
        return []
    try:
        items = [json.loads(line) for line in block.splitlines() if line.strip()]
    except (ValueError, RecursionError):
        return []
    return items if all(isinstance(item, dict) for item in items) else []


def discoveries(content):
    review = content.partition("[VERIFICATION]")[2].partition("[DIGEST]")[0]
    found = []
    for section in ("blocking_findings", "escalations", "notes"):
        for index, item in enumerate(_meaningful_items(_section_items(review, section)), 1):
            tags, invalid = _verification_tags(item)
            destination = section if invalid else _expected_verification_section(tags)
            if destination == "blocking_findings":
                found.append({"description": item[:1800], "origin": tags.get("origin", "unknown"),
                              "blocker": index if section == "blocking_findings" else None})
    return found


def assertion_receipt(root, ref):
    """Bind a project-local assertion reference to its source. Store only hashes."""
    if not isinstance(ref, str):
        return None
    relative, sep, line = ref.rpartition(":")
    if not sep or not line.isascii() or not line.isdecimal() or len(line) > 8 or not relative:
        return None
    target = Path(relative)
    if target.is_absolute() or PureWindowsPath(relative).drive or ".." in target.parts or is_secret_path(relative):
        return None
    root = Path(root).resolve()
    try:
        path = (root / target).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 2_000_000:
            return None
        resolved = path.relative_to(root).as_posix()
        if is_secret_path(resolved) or any(git.is_ignored(root, name) for name in {relative, resolved}):
            return None
        data = path.read_bytes()
    except (OSError, ValueError, RuntimeError):
        return None
    lines = data.splitlines()
    number = int(line)
    if not 1 <= number <= len(lines) or not lines[number-1].strip():
        return None
    return {"reference": ref, "file_sha256": hashlib.sha256(data).hexdigest(),
            "line_sha256": hashlib.sha256(lines[number-1].strip()).hexdigest()}


def _string(row, key, limit=600):
    value = row.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def selection_assertions(root, selection):
    baselines = {}
    for item in selection["obligations"]:
        if item.get("assertion"):
            receipt = assertion_receipt(root, item["assertion"])
            if receipt is not None:
                baselines[item["id"]] = {"source": "selection", "receipt": receipt}
    return baselines


def _bound_baseline(execution, oid):
    baseline = execution.get("assertion_baselines", {}).get(oid)
    # Earlier execute-end records used this placeholder for an unreadable reference.
    return None if baseline == {"source": "selection", "receipt": None} else baseline


def _first_baseline(execution, item, current, outcomes, fingerprint):
    findings = execution.get("finding_history", {}).get("findings", {}).values()
    previous = [f["assertion"] for f in findings if f.get("obligation") == item["id"] and f.get("assertion")]
    if previous:
        original = previous[0]
        if (original.get("reference") != item["assertion"] or any(receipt != original for receipt in previous)):
            return None, "conflicting historical assertion receipts"
        return {"source": "finding_history", "receipt": original}, None
    if current and any(c.get("command") in item["commands"] and _observed(c, fingerprint) for c in outcomes):
        return {"source": "current_observation", "receipt": current}, None
    return None, "original assertion missing or unobserved"


def check_assertions(root, execution, coverage, outcomes=(), fingerprint=None):
    """Keep the original obligation evidence even after its finding closes."""
    if coverage.get("inputs_changed"):
        fingerprint = None
    # Freeze test-owned case evidence with the fresh command outcome, before the
    # worker checkpoints it. Recovery need not reopen a mutable diagnostic file.
    for outcome in outcomes:
        outcome.pop("case_observation", None)
        outcome.pop("case_ambiguous", None)
        if "EXOMA_CASE_V1 " in outcome.get("tail", ""):
            case = _case_observation(root, outcome, fingerprint)
            if case:
                outcome["case_observation"] = case
            else:
                outcome["case_ambiguous"] = True
    for item, row in zip(coverage["selection"]["obligations"], coverage["obligations"]):
        if not item.get("assertion"):
            continue
        oid = item["id"]
        current = assertion_receipt(root, item["assertion"])
        baseline = _bound_baseline(execution, oid)
        if baseline is None:
            baseline, reason = _first_baseline(execution, item, current, outcomes, fingerprint)
            if baseline is None:
                row.update(status="open", reason=reason)
                continue
        row["assertion_baseline"] = baseline
        if not current or baseline.get("receipt") != current:
            row.update(status="open", reason="original assertion missing or changed")


def bind_assertions(root, execution, coverage, outcomes):
    """Compare and bind under the caller's transaction, preserving original provenance."""
    from exoma.core.evidence import verify_tests

    items = {item["id"]: item for item in coverage["selection"]["obligations"]}
    additions = {}
    fingerprint = coverage.get("selection_input")
    needs_fresh_input = False
    for row in coverage["obligations"]:
        item = items.get(row["id"])
        if not item or not item.get("assertion"):
            continue
        proposal = row.get("assertion_baseline") or {}
        receipt = proposal.get("receipt")
        if receipt is None:
            if row["status"] == "covered":
                return False
            continue
        baseline = _bound_baseline(execution, row["id"])
        covered = row["status"] == "covered"
        current = assertion_receipt(root, item["assertion"]) if covered or baseline is None else None
        if covered and current != receipt:
            return False
        if baseline is None:
            baseline, _ = _first_baseline(execution, item, current, outcomes,
                                          None if coverage.get("inputs_changed") else fingerprint)
            if baseline is None:
                return False
            additions[row["id"]] = baseline
            if baseline["source"] == "current_observation":
                needs_fresh_input = True
        if baseline.get("receipt") != receipt:
            return False
        needs_fresh_input = needs_fresh_input or covered
    if needs_fresh_input and (coverage.get("inputs_changed") or not fingerprint
                              or verify_tests._identity(root) != fingerprint):
        return False
    if additions:
        execution.setdefault("assertion_baselines", {}).update(additions)
    return True


def _observed(check, fingerprint):
    return (bool(fingerprint) and check.get("fingerprint") == fingerprint and check.get("log_ref")
            and not check.get("reused") and check.get("status") in {"passed", "failed", "timeout"})


def _observed_commands(checks, commands, fingerprint):
    observed = {check["command"] for check in checks if _observed(check, fingerprint)}
    return bool(commands) and observed.issuperset(commands)


def _case_observation(root, outcome, fingerprint):
    """Read an opt-in, single-case report from the runtime's retained stdout."""
    if not _observed(outcome, fingerprint) or outcome["status"] not in ("passed", "failed"):
        return None
    try:
        sessions = workflow_paths(root)["sessions_dir"].resolve()
        path = Path(outcome["log_ref"]).resolve(strict=True)
        relative = path.relative_to(sessions)
        if (len(relative.parts) != 4 or relative.parts[1:3] != ("verify", "test-output")
                or not path.match("test-*.json") or path.stat().st_size > 4_000_000):
            return None
        data = path.read_bytes()
        record = json.loads(data)
        if (record["version"] != 1 or record["status"] != outcome["status"]
                or record["exit_code"] != outcome["exit_code"] or record["output"] != outcome["log_output"]
                or outcome.get("pipe_incomplete") or record.get("pipe_incomplete")
                or any(s["truncated"] for s in record["output"].values())):
            return None
        line, = (line for line in record["stdout"].splitlines() if line.startswith("EXOMA_CASE_V1 "))
        if line not in outcome["tail"].splitlines():
            return None
        case = json.loads(line[len("EXOMA_CASE_V1 "):])
        if (case["status"] != outcome["status"] or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", case["id"])):
            return None
        receipt = assertion_receipt(root, case["assertion"])
        if receipt:
            return {"id": case["id"], "assertion": receipt, "command": outcome["command"],
                    "log_ref": str(path), "log_sha256": hashlib.sha256(data).hexdigest()}
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def _same_case(left, right):
    return bool(left and right and all(left[k] == right[k] for k in ("command", "id", "assertion")))


def _prior_retry_input(finding, rounds, commands):
    if "observed_input" in finding:
        return finding["observed_input"]
    # Older ledgers advanced last_input even for refused commands. Recover from receipts.
    previous = None
    for round_ in rounds.values():
        fingerprint = round_.get("selection_input")
        if (any(event.get("id") == finding["id"] for event in round_.get("events", []))
                and _observed_commands(round_.get("checks", []), commands, fingerprint)):
            previous = fingerprint
    return previous


def _retry_gap(diagnosis, strategy, changed_input, repeat):
    if not diagnosis:
        return "missing_diagnosis"
    if not strategy:
        return "missing_strategy"
    if not (changed_input or repeat):
        return "missing_retry_basis"
    return None


def _closure_reason(finding, row, assertion, commands, fresh, covered, retry_gap, cases):
    """Return the first unmet closure condition or the accepted decision."""
    if retry_gap:
        return retry_gap
    oid = finding.get("obligation")
    if not commands or _string(row, "obligation") != oid:
        return "obligation_unmatched"
    link = finding.get("attachment", {})
    if link.get("source") == "current_observation":
        return "historical_trigger_unconfirmed"
    original = finding.get("assertion") or link.get("case", {}).get("assertion")
    if not original:
        return "original_assertion_unbound"
    if assertion != original:
        return "assertion_unmatched"
    if not fresh.issuperset(commands):
        return "fresh_pass_missing"
    if oid not in covered:
        return "obligation_open"
    expected = finding.get("first_case") or link.get("case")
    if not expected and finding.get("case_ambiguous"):
        return "case_evidence_missing"
    if expected and (expected["command"] not in fresh or expected["assertion"] != assertion
                     or not _same_case(expected, cases.get(expected["command"]))):
        return "case_evidence_missing"
    return "closure_accepted"


def publish(root, execution, pin, result):
    """Apply exactly one review under the caller's execution transaction."""
    ledger = execution.setdefault("finding_history", {"version": 1, "revision": 0, "findings": {}, "rounds": {}})
    runtime = result["meta"].get("runtime_tests") or {}
    content = result.get("content", "")
    proposal = result["meta"].get("review_observations") or {}
    observed = proposal.get("blockers", [])
    replies = rows(content)
    report_hash = digest([pin, content, runtime, proposal])
    past = ledger["rounds"].get(pin["round_id"])
    if past:
        if past["digest"] != report_hash:
            raise ValueError("conflicting result for committed verification round")
        result["meta"]["verify_followup"] = past
        return False
    if not matches_revision(execution, pin):
        raise ValueError("stale verification history revision")
    coverage = runtime.get("coverage") or {}
    obligations = {o["id"]: o for o in coverage.get("selection", {}).get("obligations", [])}
    covered = {o["id"] for o in coverage.get("obligations", []) if o["status"] == "covered"}
    outcomes = runtime.get("outcomes", [])
    fingerprint = coverage.get("selection_input")
    if pin.get("input"):
        from exoma.core.evidence import verify_tests

        if ((fingerprint and pin["input"] != fingerprint)
                or verify_tests._identity(root) != pin["input"]):
            raise ValueError("verification input changed after dispatch; prepare a new round")
    events = []
    by_index = {item["blocker"]: item for item in observed if type(item.get("blocker")) is int}
    consumed = set()
    findings = ledger["findings"]
    prior = summary(execution)
    offered = {f["id"] for f in prior["open"]}
    previously_closed = {f["id"] for f in prior["recently_resolved"]}
    checks = [{k: o[k] for k in ("command", "status", "log_ref", "log_error", "fingerprint", "reused") if k in o}
              for o in outcomes]
    handled_failures = set()
    cases = {}
    ambiguous_cases = set()
    if fingerprint and pin.get("input") == fingerprint and not coverage.get("inputs_changed"):
        for outcome in outcomes:
            command = outcome["command"]
            # Repeated invocations in one round cannot identify a unique observation.
            is_observed = _observed(outcome, fingerprint)
            case = outcome.get("case_observation") if is_observed else None
            if (outcome.get("case_ambiguous") and not outcome.get("reused")
                    and (outcome.get("fingerprint") == fingerprint or outcome.get("pipe_incomplete"))):
                ambiguous_cases.add(command)
            if command in cases:
                if cases[command] is not None or case is not None:
                    ambiguous_cases.add(command)
                cases[command] = None
            else:
                cases[command] = case

    def attachment(finding, row):
        oid, command = _string(row, "obligation"), _string(row, "command")
        item, case = obligations.get(oid), cases.get(command)
        if (row.get("execution_id") != pin["execution_id"] or not item
                or command not in item["commands"] or not _string(row, "diagnosis")
                or finding.get("obligation") not in (None, oid) or finding.get("command") not in (None, command)):
            return "attachment_unmatched"
        baseline = _bound_baseline(execution, oid) or {}
        if (not case or baseline.get("receipt") != case["assertion"]
                or case["id"] != _string(row, "case") or item.get("assertion") != _string(row, "evidence")
                or case["assertion"]["reference"] != item.get("assertion")
                or finding.get("assertion") not in (None, case["assertion"])
                or not any(c["command"] == command and c["status"] == "failed" for c in checks)):
            return "case_evidence_missing"
        claimants = [r for r in replies if _string(r, "status") == "attach" and _string(r, "command") == command]
        if (len(claimants) != 1 or sum(command in o["commands"] for o in obligations.values()) != 1
                or any(other is not finding and other["disposition"] == "open"
                       and _same_case(other.get("first_case"), case) for other in findings.values())):
            return "attachment_ambiguous"
        first = finding.get("first_case")
        if not first and finding.get("assertion") and finding.get("obligation"):
            return "attachment_unmatched"
        if first and not _same_case(first, case):
            return "case_mismatch"
        link = finding.get("attachment")
        if link and not _same_case(link["case"], case):
            return "case_mismatch"
        if not link:
            link = {"source": "first_failure" if first else "current_observation",
                    "round_id": pin["round_id"], "input": fingerprint, "obligation": oid,
                    "case": case, "diagnosis": _string(row, "diagnosis")}
            finding.update(attachment=link, obligation=oid, behavior=item["behavior"])
        if first:
            handled_failures.add(command)
        finding["observed_input"] = finding["last_input"] = fingerprint
        return "case_linked" if first else "case_observed_prospectively"

    def record_followup(finding, status, reason_code, **details):
        # summary() exposes this even when a missing reply leaves last_round unchanged.
        finding["last_reason_code"] = reason_code
        events.append({"id": finding["id"], "status": status, "reason_code": reason_code, **details})

    def record_observation(finding, commands):
        required = [finding["command"]] if finding.get("command") in commands else commands
        previous = _prior_retry_input(finding, ledger["rounds"], required)
        observed = _observed_commands(checks, required, fingerprint)
        finding["observed_input"] = finding["last_input"] = fingerprint if observed else previous
        return previous

    def associate(index, obligation):
        if type(index) is not int or index not in by_index or index in consumed:
            return False
        consumed.add(index)
        if obligation:
            # A reviewer blocker on a suite does not identify its runtime case.
            handled_failures.update(cmd for cmd in obligation["commands"] if not cases.get(cmd))
        return True

    for fid in sorted(previously_closed):
        reports = [r for r in replies if r.get("id") == fid]
        if len(reports) != 1 or reports[0].get("status") != "regressed":
            continue
        row = reports[0]
        f = findings[fid]
        if _string(row, "diagnosis") and associate(row.get("blocker"), obligations.get(f.get("obligation"))):
            record_observation(f, obligations.get(f.get("obligation"), {}).get("commands", []))
            f.update(disposition="open", last_round=pin["round_id"])
            record_followup(f, "regressed", "regression_linked", diagnosis=_string(row, "diagnosis"),
                            observation=by_index[row["blocker"]]["description"])

    for fid in sorted(offered):
        f = findings[fid]
        f["reviewed_revision"] = ledger["revision"] + 1
        oid = f.get("obligation")
        obligation = obligations.get(oid)
        commands = obligation["commands"] if obligation else []
        prior_input = record_observation(f, commands)
        matches = [r for r in replies if r.get("id") == fid]
        if len(matches) != 1:
            record_followup(f, "unchecked", "followup_ambiguous" if matches else "followup_missing",
                            reason="absent or ambiguous follow-up")
            continue
        row = matches[0]
        status = _string(row, "status")
        if status == "attach":
            reason = attachment(f, row)
            f["last_round"] = pin["round_id"]
            observation = {}
            if reason in ("case_linked", "case_observed_prospectively"):
                observation["observation"] = cases[_string(row, "command")]
            record_followup(f, "present" if reason == "case_linked" else "unchecked", reason,
                            obligation=_string(row, "obligation"), case=_string(row, "case"),
                            diagnosis=_string(row, "diagnosis"), **observation)
            continue
        diagnosis = _string(row, "diagnosis")
        strategy = _string(row, "strategy")
        repeat = _string(row, "repeat_reason")
        assertion = assertion_receipt(root, _string(row, "evidence"))
        fresh = {c["command"] for c in checks if c.get("status") == "passed" and _observed(c, fingerprint)}
        # Wording alone cannot make an unchanged experiment materially different.
        changed_input = bool(prior_input and fingerprint and prior_input != fingerprint)
        f["latest_checks"] = [check for check in checks if check["command"] in commands]
        retry_gap = _retry_gap(diagnosis, strategy, changed_input, repeat)
        if status == "resolved":
            reason = _closure_reason(f, row, assertion, commands, fresh, covered, retry_gap, cases)
            status = "resolved" if reason == "closure_accepted" else "unchecked"
        elif status == "present":
            reason = "missing_diagnosis"
            if diagnosis:
                reason = "blocker_linked" if associate(row.get("blocker"), obligation) else "blocker_unmatched"
        elif status == "nonreproduced":
            reason = "reported_nonreproduced"
        else:
            reason = "reported_unchecked" if status == "unchecked" else "unsupported_status"
            status = "unchecked"
        record_followup(f, status, reason, diagnosis=diagnosis, strategy=strategy, repeat_reason=repeat,
                        evidence=assertion, equivalence=_string(row, "equivalence"), retry_supported=retry_gap is None)
        f.update(disposition="resolved" if status == "resolved" else "open",
                 last_strategy=strategy, last_round=pin["round_id"])

    def create(description, origin, oid, trigger=None):
        obligation = obligations.get(oid)
        first_checks = [c for c in checks if c["command"] in (obligation or {}).get("commands", [])]
        fid = "F-" + uuid.uuid4().hex[:16]
        f = {"id": fid, "description": description, "origin": origin, "disposition": "open",
             "first_round": pin["round_id"], "last_round": pin["round_id"], "observed_input": None,
             "obligation": oid, "assertion": assertion_receipt(root, (obligation or {}).get("assertion")),
             "behavior": (obligation or {}).get("behavior"), "command": trigger["command"] if trigger else None,
             "first_checks": first_checks}
        if trigger is not None:
            if not first_checks:
                f["first_checks"] = [trigger]
            elif len(first_checks) > 1:
                f["first_trigger"] = trigger
        record_observation(f, (obligation or {}).get("commands", []))
        findings[fid] = f
        events.append({"id": fid, "status": "new", "origin": origin})
        return f

    for item in observed:
        index = item.get("blocker")
        if index in consumed:
            continue
        links = [r for r in replies if not r.get("id") and type(r.get("blocker")) is int and r["blocker"] == index]
        oid = _string(links[0], "obligation") if len(links) == 1 else None
        create(item["description"], item["origin"], oid if oid in obligations else None)
        if oid in obligations:
            handled_failures.update(cmd for cmd in obligations[oid]["commands"] if not cases.get(cmd))
    for check in checks:
        command = check["command"]
        if check["status"] not in ("failed", "timeout", "error") or command in handled_failures:
            continue
        ids = [oid for oid, item in obligations.items() if command in item["commands"]]
        oid = ids[0] if len(ids) == 1 else None
        case = cases.get(command) if check["status"] == "failed" else None
        if case and oid and (_bound_baseline(execution, oid) or {}).get("receipt") != case["assertion"]:
            oid = None
        candidates = [fid for fid in sorted(offered) if findings[fid].get("command") == command
                      and not (findings[fid].get("first_case") and cases.get(command)
                               and not _same_case(findings[fid]["first_case"], cases[command]))]
        finding = create("Runtime check " + command + " " + check["status"], "unknown", oid, check)
        if case:
            finding["first_case"] = case
        elif command in ambiguous_cases:
            finding["case_ambiguous"] = True
        if candidates:
            events[-1]["possible_prior_ids"] = candidates
    record = {**pin, "digest": report_hash, "at": now_iso(), "selection_input": fingerprint,
              "open_obligations": [o["id"] for o in coverage.get("obligations", []) if o["status"] == "open"],
              "checks": checks, "events": events, "advisory": True,
              "open_count": sum(f["disposition"] == "open" for f in findings.values())}
    ledger["rounds"][pin["round_id"]] = record
    ledger["revision"] += 1
    result["meta"]["verify_followup"] = record
    return True

