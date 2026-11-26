"""Round continuity and publication faults that a single clean verify cannot exercise."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from exoma.core.evidence import verify_followup as follow
from exoma.core.jobs.job_manager import JobManager
from exoma.core.runtime import execution


def _test_verify_followup():
    from tools.maintain.verify_followup_eval import replacement_controls

    with TemporaryDirectory() as temp:
        controls = replacement_controls(Path(temp))
        assert {name for name, result in controls.items() if result["supported"]} == {"moved"}, controls
        assert len(controls) == 8
    _check_paired_controls()
    _check_publication()
    _check_retry_and_recurrence()
    _check_first_failure_handoffs()


def _check_first_failure_handoffs():
    from exoma.core.evidence import verify_tests
    from exoma.core.workspace.workspace_paths import workflow_paths

    with TemporaryDirectory() as temp:
        root = Path(temp)
        session = "first-failure"
        state = {"execution_id": "one"}
        checks = [{"command": "same", "status": "failed", "log_ref": "first.json"},
                  {"command": "same", "status": "failed", "log_ref": "second.json"},
                  {"command": "another", "status": "error", "log_error": "storage unavailable"}]
        result = {"meta": {"runtime_tests": {"outcomes": checks}}}
        follow.publish(root, state, follow.binding(state, "one"), result)
        ledger = state["finding_history"]
        events = ledger["rounds"]["one"]["events"]
        assert len(events) == 3
        expected = {event["id"]: [check] for event, check in zip(events, checks, strict=True)}

        def read_handoffs():
            path = execution._runtime_file(root, session, execution.EXECUTION_FILE)
            execution._write(path, state)
            prepared = verify_tests.prepare(root, session)["meta"]["verify_followup"]
            with follow.using(follow.binding(state)):
                name = follow.handoff(root, session, state, {})
            sidecar = workflow_paths(root, session)["runtime_dir"] / name
            reviewed = json.loads(sidecar.read_text(encoding="utf-8"))
            assert prepared["open"] == reviewed["open"]
            return {f["id"]: f["first_checks"] for f in prepared["open"]}

        assert read_handoffs() == expected
        for finding in ledger["findings"].values():
            finding["first_checks"] = []
        before = deepcopy(ledger)
        legacy = read_handoffs()
        assert legacy == {e["id"]: ([c] if c["command"] == "another" else [])
                          for e, c in zip(events, checks, strict=True)}
        assert ledger == before


def _check_retry_and_recurrence():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        state = {"execution_id": "one"}
        ref = "check.py:1"
        (root / "check.py").write_text("assert quantity(-2) == 0\n", encoding="utf-8")
        obligation = {"id": "quantity", "behavior": "clamp negative units", "due": "verify",
                      "assertion": ref, "commands": ["python check.py"]}
        observation = {"blocker": 1, "description": "negative input escapes clamp", "origin": "introduced"}

        def publish(replies, blockers, status="passed", reused=False, fingerprint="unchanged",
                    command="python check.py", log_ref="receipt", coverage_status=None):
            if coverage_status is None:
                coverage_status = "covered" if status == "passed" else "open"
            coverage = {"selection": {"obligations": [obligation]}, "selection_input": fingerprint,
                        "obligations": [{"id": "quantity", "status": coverage_status}]}
            result = {"content": "[FOLLOWUP_V1]\n" + "\n".join(json.dumps(r) for r in replies) + "\n[VERIFICATION]",
                      "meta": {"review_observations": {"blockers": blockers}, "runtime_tests": {
                          "coverage": coverage, "outcomes": [{"command": command, "status": status,
                          "log_ref": log_ref, "fingerprint": fingerprint, "reused": reused}] if command else []}}}
            follow.publish(root, state, follow.binding(state), result)
            return result["meta"]["verify_followup"]

        first = publish([{"blocker": 1, "obligation": "quantity"}], [observation], "failed")
        fid = first["events"][0]["id"]
        reply = {"id": fid, "status": "resolved", "diagnosis": "intermittent fixture input",
                 "strategy": "repeat original assertion with input recorded", "obligation": "quantity",
                 "evidence": ref}
        assert publish([dict(reply, status="unchecked")], [], fingerprint=None)["open_count"] == 1
        finding = state["finding_history"]["findings"][fid]
        finding.pop("observed_input", None)
        finding["last_input"] = "untrusted-legacy-input"
        for status, reused, command in (("refused", False, "python check.py"), ("passed", True, "python check.py"),
                                        ("passed", False, None), ("passed", False, "other")):
            assert publish([dict(reply, status="unchecked")], [], status, reused, "B", command)["open_count"] == 1
            assert finding["last_input"] == "unchanged"
        obligation["commands"].append("smoke")
        publish([dict(reply, status="unchecked")], [], fingerprint="B", command="smoke")
        assert finding["last_input"] == "unchanged"
        obligation["commands"].remove("smoke")
        def check_reason(replies, reason, **options):
            record = publish(replies, [], **options)
            event = record["events"][0]
            assert (event["id"], event["reason_code"]) == (fid, reason), event
            assert finding["last_reason_code"] == reason
            assert record["open_count"] == (reason != "closure_accepted")

        check_reason([reply], "missing_retry_basis")
        reply["repeat_reason"] = "discriminate intermittent input from a deterministic failure"
        check_reason([dict(reply, diagnosis="", strategy="")], "missing_diagnosis")
        check_reason([dict(reply, strategy="")], "missing_strategy")
        check_reason([dict(reply, obligation="unrelated")], "obligation_unmatched")
        with patch.dict(finding, assertion=None):
            check_reason([reply], "original_assertion_unbound")
        check_reason([dict(reply, evidence="missing.py:1")], "assertion_unmatched")
        check_reason([reply], "fresh_pass_missing", reused=True)
        check_reason([reply], "fresh_pass_missing", log_ref=None)
        check_reason([reply], "obligation_open", coverage_status="open")
        check_reason([dict(reply, status="nonreproduced")], "reported_nonreproduced")
        check_reason([dict(reply, status="present")], "blocker_unmatched")
        check_reason([dict(reply, status="unchecked")], "reported_unchecked")
        check_reason([dict(reply, status="unexpected")], "unsupported_status")
        check_reason([], "followup_missing")
        check_reason([reply, reply], "followup_ambiguous")
        check_reason([reply], "closure_accepted")
        repeated = publish([dict(reply, status="regressed", blocker=1)], [observation], "failed")
        assert [(e["id"], e["status"]) for e in repeated["events"]] == [(fid, "regressed")]
        assert finding["last_reason_code"] == "regression_linked"
        del reply["repeat_reason"]
        check_reason([reply], "closure_accepted", fingerprint="genuinely-changed")


def _check_paired_controls():
    from exoma import cli
    from tools.maintain.verify_followup_eval import CASES, drive

    for case in CASES:
        with TemporaryDirectory() as temp, patch.object(cli, "EXECUTOR", cli.EXECUTOR):
            with patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}):
                root = Path(temp)
                first, second = drive(root, case, "replay", "fixture")
                if case == "persistent":
                    _check_green_with_open_finding(root)
                if case in ("repair", "clean"):
                    _check_later_assertion(root, legacy=case == "repair")
                if case == "clean":
                    _check_first_binding(root)
                    _check_binding_publication(root)
                    _check_case_linkage(root)
        assert first["ok"] and second["ok"] and first["diagnostics"] and second["diagnostics"]
        result = second["followup"]
        statuses = [event["status"] for event in result["events"]]
        if case == "clean":
            assert first["verdict"] == second["verdict"] == "pass" and not statuses
            continue
        fid = first["followup"]["events"][0]["id"]
        assert any(event["id"] == fid for event in result["events"])
        if case == "replacement":
            assert statuses == ["resolved", "new"] and second["verdict"] == "fail"
            assert result["events"][0]["reason_code"] == "closure_accepted"
        elif case == "persistent":
            assert statuses == ["present"] and result["open_count"] == 1
            assert result["events"][0]["reason_code"] == "blocker_linked"
        elif case == "repair":
            assert statuses == ["resolved"] and result["open_count"] == 0
        else:
            assert statuses == ["unchecked"] and "quantity" in result["open_obligations"]
            assert result["events"][0]["reason_code"] == "assertion_unmatched"
            assert second["diagnostics"][0]["status"] == "passed" and second["verdict"] == "incomplete"


def _check_late_attachment(root):
    from exoma import cli
    from tools.maintain.verify_followup_eval import COMMAND, FIXTURE, REVIEW, TAG, write_json

    session = "attachment"
    state = execution._runtime_file(root, session, execution.EXECUTION_FILE)
    eid = "attach-execution"
    selection = {"version": 1, "execution_id": eid, "tier": "integration", "reason": "late quantity check",
                 "obligations": [{"id": "release", "due": "release", "commands": [], "behavior": "release check"}]}
    execution._write(state, {"execution_id": eid, "status": "implemented", "selection": selection})
    code = (FIXTURE / "pricing.py").read_text(encoding="utf-8")
    (root / "pricing.py").write_text(code.replace("max(0, value)", "value"), encoding="utf-8")
    ref = "check_quantity.py:4"
    (root / "check_quantity.py").write_text(
        'import json\nfrom pricing import quantity\ntry:\n    assert quantity(-2) == 0\n'
        'except AssertionError:\n    status = "failed"\nelse:\n    status = "passed"\n'
        'print("EXOMA_CASE_V1 " + json.dumps({"id":"negative-two","assertion":"check_quantity.py:4","status":status}))\n'
        'raise SystemExit(int(status == "failed"))\n', encoding="utf-8")

    def review(rows, blocker=False):
        prepared = cli.run("verify-prepare", "", session, str(root))["meta"]
        write_json(Path(prepared["request"]["path"]), {"commands": [COMMAND], "selection": selection,
                                                      "selection_input": prepared["selection_input"]})
        report = REVIEW.format(verdict="NEEDS FIX" if blocker else "DONE",
                               finding=TAG + " independent total defect at pricing.py:8" if blocker else "none")
        cli.EXECUTOR.adapter.report = "\n".join(["[FOLLOWUP_V1]", *(json.dumps(row) for row in rows), report])
        result = cli.run("verify", "check pricing cases", session, str(root), model="fixture")
        assert result["ok"], result
        return result["meta"]

    first = review([])["verify_followup"]
    fid = first["events"][0]["id"]
    before = execution.current_execution(root, session)
    old = before["finding_history"]["findings"][fid]
    assert old["first_case"]["id"] == "negative-two" and old["obligation"] is None
    item = {"id": "quantity", "due": "verify", "commands": [COMMAND], "behavior": "clamp negative", "assertion": ref}
    selection["obligations"].append(item)
    proposal = {"id": fid, "execution_id": eid, "status": "attach", "case": "negative-two", "command": COMMAND,
                "obligation": "quantity", "evidence": ref, "diagnosis": "Same negative input and expected zero"}
    second = review([proposal])
    linked = execution.current_execution(root, session)
    finding = linked["finding_history"]["findings"][fid]
    event, = second["verify_followup"]["events"]
    assert event["reason_code"] == "case_linked" and second["verdict"] == "fail"
    assert finding["attachment"]["source"] == "first_failure" and finding["disposition"] == "open"
    assert finding["assertion"] is None and finding["first_case"] == old["first_case"]
    assert linked["finding_history"]["rounds"][first["round_id"]] == first

    # Replay rejected proposals independently of one another against the first-round snapshot.
    def alternative(rows, *, legacy=False, reused=False, shared=False, other_case=False):
        snapshot = deepcopy(before)
        snapshot["assertion_baselines"] = linked["assertion_baselines"]
        prior = snapshot["finding_history"]["findings"][fid]
        if legacy:
            prior.pop("first_case")
        runtime = deepcopy(second["runtime_tests"])
        if reused:
            runtime["outcomes"][0]["reused"] = True
        if shared:
            runtime["coverage"]["selection"]["obligations"].append(dict(item, id="other"))
        if other_case:
            runtime["outcomes"][0]["case_observation"]["id"] = "different-trigger"
        content = "\n".join(["[FOLLOWUP_V1]", *(json.dumps(r) for r in rows), "[VERIFICATION]"])
        result = {"content": content, "meta": {"runtime_tests": runtime, "review_observations": {"blockers": []}}}
        pin = follow.bind_current(root, snapshot)
        assert follow.publish(root, snapshot, pin, result)
        stored = deepcopy(snapshot)
        assert not follow.publish(root, snapshot, pin, result) and snapshot == stored
        return prior, result["meta"]["verify_followup"]["events"]

    for rows, flags, reason in [
        ([dict(proposal, execution_id="elsewhere")], {}, "attachment_unmatched"),
        ([proposal], {"reused": True}, "case_evidence_missing"),
        ([proposal], {"shared": True}, "attachment_ambiguous"),
        ([proposal, dict(proposal, id="second-id")], {}, "attachment_ambiguous"),
        ([dict(proposal, case="different-trigger")], {"other_case": True}, "case_mismatch"),
    ]:
        prior, events = alternative(rows, **flags)
        assert "attachment" not in prior and events[0]["reason_code"] == reason
        assert events[1]["id"] != fid
        assert (fid in events[1].get("possible_prior_ids", [])) == (reason != "case_mismatch")
    prior, events = alternative([proposal], legacy=True)
    assert prior["attachment"]["source"] == "current_observation" and events[1]["id"] != fid
    _check_competing_legacy_attachments(before, proposal, alternative)
    _check_attachment_recovery(root, session, before, second, proposal)
    execution._write(state, linked)
    (root / "pricing.py").write_text(code.replace("price * units", "price + units"), encoding="utf-8")
    resolved = dict(proposal, status="resolved", strategy="repair clamp and rerun original case")
    last = review([resolved], blocker=True)
    closure, independent = last["verify_followup"]["events"]
    assert closure["id"] == fid and closure["reason_code"] == "closure_accepted"
    assert independent["status"] == "new" and independent["id"] != fid and last["verdict"] == "fail"

    for source, alter_case, expected in (("current_observation", False, "historical_trigger_unconfirmed"),
                                         ("first_failure", True, "case_evidence_missing")):
        snapshot = deepcopy(linked)
        attached = snapshot["finding_history"]["findings"][fid]
        attached["attachment"]["source"] = source
        if source == "current_observation":
            attached.pop("first_case")
        runtime = deepcopy(last["runtime_tests"])
        if alter_case:
            runtime["outcomes"][0]["case_observation"]["id"] = "other-passing-case"
        result = {"content": "[FOLLOWUP_V1]\n" + json.dumps(resolved) + "\n[VERIFICATION]",
                  "meta": {"runtime_tests": runtime}}
        follow.publish(root, snapshot, follow.bind_current(root, snapshot), result)
        assert attached["disposition"] == "open" and attached["last_reason_code"] == expected


def _check_attachment_recovery(root, session, before, published, proposal):
    from exoma.core.jobs.job_manager import DEAD

    jobs = JobManager(root / ".workflow/jobs")
    state = execution._runtime_file(root, session, execution.EXECUTION_FILE)
    meta = deepcopy(published)
    meta.pop("verify_followup")
    payload = {"ok": True, "meta": meta, "content": "[FOLLOWUP_V1]\n" + json.dumps(proposal) + "\n[VERIFICATION]"}
    for stale in (True, False):
        execution._write(state, before)
        job = jobs.create_job("verify", "attachment recovery", session, str(root), None)
        jid = job["job_id"]
        jobs.mark_running(jid)
        with patch.object(execution, "record_verification", side_effect=OSError("worker interrupted")):
            try:
                jobs.complete_verify_round(jid, job["recovery_attempt"], payload)
            except OSError:
                pass
            else:
                raise AssertionError("checkpoint was not exercised")
        changed = root / "new_input.py"
        if stale:
            changed.write_text("changed = True\n", encoding="utf-8")
        else:
            # A checkpoint already contains the case observed by the live runner.
            Path(meta["runtime_tests"]["outcomes"][0]["log_ref"]).unlink()
        try:
            with patch.object(jobs, "liveness", return_value=DEAD):
                result = jobs.get_result(jid)
            stored = execution.current_execution(root, session)
            if stale:
                assert result["meta"]["reason"] == "verification_publication_rejected" and stored == before
            else:
                record = result["output"]["meta"]["verify_followup"]
                assert record["events"][0]["reason_code"] == "case_linked"
                assert stored["finding_history"]["revision"] == before["finding_history"]["revision"] + 1
                assert jobs.get_result(jid)["output"]["meta"]["verify_followup"] == record
                assert execution.current_execution(root, session) == stored
        finally:
            if stale:
                changed.unlink()


def _check_case_linkage(root):
    _check_late_attachment(root)
    _check_initially_bound_case(root)


def _check_initially_bound_case(root):
    """One command can exercise two different cases on the same assertion line."""
    from exoma import cli
    from tools.maintain.verify_followup_eval import COMMAND, FIXTURE, REVIEW, write_json

    session = "initial-case"
    eid = "initially-bound"
    ref = "check_quantity.py:6"
    selection = {"version": 1, "execution_id": eid, "tier": "targeted", "reason": "track the failing case",
                 "obligations": [{"id": "quantity", "behavior": "clamp negative inputs", "due": "verify",
                                  "commands": [COMMAND], "assertion": ref}]}
    state = execution._runtime_file(root, session, execution.EXECUTION_FILE)
    execution._write(state, {"execution_id": eid, "status": "implemented", "selection": selection})
    source = (FIXTURE / "pricing.py").read_text(encoding="utf-8")
    (root / "pricing.py").write_text(source.replace("max(0, value)", "value"), encoding="utf-8")
    (root / "check_quantity.py").write_text(
        'import json\nfrom pathlib import Path\nfrom pricing import quantity\n'
        'value = int(Path("case.txt").read_text())\ntry:\n    assert quantity(value) == max(0, value)\n'
        'except AssertionError:\n    status = "failed"\nelse:\n    status = "passed"\n'
        'print("EXOMA_CASE_V1 " + json.dumps({"id":str(value),"assertion":"check_quantity.py:6","status":status}))\n'
        'raise SystemExit(int(status == "failed"))\n', encoding="utf-8")

    def review(value, rows):
        (root / "case.txt").write_text(str(value), encoding="utf-8")
        prepared = cli.run("verify-prepare", "", session, str(root))["meta"]
        write_json(Path(prepared["request"]["path"]), {"commands": [COMMAND], "selection": selection,
                                                      "selection_input": prepared["selection_input"]})
        cli.EXECUTOR.adapter.report = "\n".join(["[FOLLOWUP_V1]", *(json.dumps(row) for row in rows),
                                                REVIEW.format(verdict="DONE", finding="none")])
        result = cli.run("verify", "review quantity cases", session, str(root), model="fixture")
        assert result["ok"], result
        return result["meta"]["verify_followup"]

    first = review(-2, [])
    fid = first["events"][0]["id"]
    finding = execution.current_execution(root, session)["finding_history"]["findings"][fid]
    assert finding["obligation"] == "quantity" and "attachment" not in finding and finding["first_case"]["id"] == "-2"
    proposal = {"id": fid, "status": "resolved", "diagnosis": "clamp under test",
                "strategy": "exercise original input", "obligation": "quantity", "evidence": ref}
    other = review(2, [proposal])
    assert other["open_count"] == 1 and other["events"][0]["reason_code"] == "case_evidence_missing", other["events"]
    (root / "pricing.py").write_text(source, encoding="utf-8")
    repaired = review(-2, [proposal])
    assert repaired["open_count"] == 0 and repaired["events"][0]["reason_code"] == "closure_accepted"

    # The same command on an assertion outside the declared obligation starts unbound.
    selection["execution_id"] = "other-assertion"
    selection["obligations"][0]["assertion"] = "pricing.py:3"
    execution._write(state, {"execution_id": selection["execution_id"], "status": "implemented", "selection": selection})
    (root / "pricing.py").write_text(source.replace("max(0, value)", "value"), encoding="utf-8")
    mismatch = review(-2, [])
    new_id = mismatch["events"][0]["id"]
    unbound = execution.current_execution(root, session)["finding_history"]["findings"][new_id]
    assert unbound["first_case"]["id"] == "-2" and unbound["obligation"] is None


def _check_green_with_open_finding(root):
    from exoma import cli
    from exoma.core.workspace.workspace_paths import workflow_paths
    from tools.maintain.verify_followup_eval import COMMAND, FIXTURE, REVIEW, SESSION, write_json

    (root / "pricing.py").write_bytes((FIXTURE / "pricing.py").read_bytes())
    prepared = cli.run("verify-prepare", "", SESSION, str(root))["meta"]
    prior = prepared["verify_followup"]["open"][0]
    assert prior["last_reason_code"] == "blocker_linked"
    row = json.dumps({"id": prior["id"], "status": "nonreproduced"})
    for replies, status, reason in (([], "unchecked", "followup_missing"),
                                   ([row, row], "unchecked", "followup_ambiguous"),
                                   ([row], "nonreproduced", "reported_nonreproduced")):
        cli.EXECUTOR.adapter.report = "\n".join(["[FOLLOWUP_V1]", *replies, REVIEW.format(verdict="DONE", finding="none")])
        write_json(Path(prepared["request"]["path"]), {"commands": [COMMAND], "selection": prepared["execution_selection"],
                                                      "selection_input": prepared["selection_input"]})
        result = cli.run("verify", "Check repaired quantity", SESSION, str(root), model="fixture")
        meta = result["meta"]
        assert result["ok"] and meta["verdict"] == "pass" and cli._verify_exit_code("verify", result) == 0
        assert meta["runtime_tests"]["coverage"]["obligations"][0]["status"] == "covered"
        event, = meta["verify_followup"]["events"]
        assert (event["id"], event["status"], event["reason_code"]) == (prior["id"], status, reason)
        prepared = cli.run("verify-prepare", "", SESSION, str(root))["meta"]
        upcoming = prepared["verify_followup"]["open"]
        assert upcoming[0]["last_reason_code"] == reason
        if status == "unchecked":
            assert upcoming[0]["last_round"] == prior["last_round"]
    state = execution.current_execution(root, SESSION)
    with follow.using(follow.binding(state)):
        name = follow.handoff(root, SESSION, state, {})
    handoff = workflow_paths(root, SESSION)["runtime_dir"] / name
    assert json.loads(handoff.read_text(encoding="utf-8"))["open"] == upcoming


def _check_later_assertion(root, legacy):
    from exoma import cli
    from tools.maintain.verify_followup_eval import COMMAND, SESSION, write_json

    state = execution._runtime_file(root, SESSION, execution.EXECUTION_FILE)
    if legacy:
        current = execution.current_execution(root, SESSION)
        current.pop("assertion_baselines", None)
        execution._write(state, current)
    check = root / "check_quantity.py"
    original = check.read_bytes()
    for content, verdict in ((b"from pricing import quantity\n\nprint('ok')\n", "incomplete"), (original, "pass")):
        check.write_bytes(content)
        prepared = cli.run("verify-prepare", "", SESSION, str(root))["meta"]
        write_json(Path(prepared["request"]["path"]), {
            "commands": [COMMAND], "selection": prepared["execution_selection"],
            "selection_input": prepared["selection_input"]})
        result = cli.run("verify", "check current quantity", SESSION, str(root), model="fixture")
        meta = result["meta"]
        assert meta["runtime_tests"]["outcomes"][0]["status"] == "passed"
        assert meta["verdict"] == verdict, (legacy, verdict, meta["verdict"])
        row = meta["runtime_tests"]["coverage"]["obligations"][0]
        assert row["status"] == ("open" if verdict == "incomplete" else "covered")
        baseline = row["assertion_baseline"]
        assert baseline["source"] == ("finding_history" if legacy else "current_observation")
        assert baseline["receipt"]["reference"] == "check_quantity.py:3"

    # Replay the historical obligation as a release-due selection in its own execution.
    current = execution.current_execution(root, SESSION)
    current["execution_id"] = current["selection"]["execution_id"] = "release-control"
    current["selection"]["obligations"][0]["due"] = "release"
    current.pop("finding_history", None)
    current.pop("coverage_history", None)
    execution._write(state, current)
    check.write_text("from pricing import quantity\n\nprint('unchecked')\n", encoding="utf-8")
    prepared = cli.run("verify-prepare", "", SESSION, str(root))["meta"]
    write_json(Path(prepared["request"]["path"]), {"commands": [COMMAND], "selection": current["selection"],
                                                  "selection_input": prepared["selection_input"]})
    meta = cli.run("verify", "check release coverage", SESSION, str(root), model="fixture")["meta"]
    row = meta["runtime_tests"]["coverage"]["obligations"][0]
    assert meta["verdict"] == "pass" and row["status"] == "open" and row["due"] == "release"


def _check_first_binding(root):
    from exoma import cli
    from tools.maintain.verify_followup_eval import COMMAND, SESSION, write_json

    state = execution._runtime_file(root, SESSION, execution.EXECUTION_FILE)
    selection = execution.current_execution(root, SESSION)["selection"]
    item = selection["obligations"][0]
    item["due"] = "verify"
    check = root / "check_quantity.py"
    for legacy in (False, True):
        eid = selection["execution_id"] = f"late-assertion-{legacy}"
        execution._write(state, {"execution_id": eid, "status": "active"})
        check.write_text("from pricing import quantity\n", encoding="utf-8")
        closed = execution.end_execution(root, SESSION, selection=selection)
        assert closed["ok"]
        if legacy:
            snapshot = closed["execution"]
            snapshot["assertion_baselines"] = {item["id"]: {"source": "selection", "receipt": None}}
            execution._write(state, snapshot)

        def review():
            prepared = cli.run("verify-prepare", "", SESSION, str(root))["meta"]
            write_json(Path(prepared["request"]["path"]), {"commands": [COMMAND], "selection": selection,
                                                          "selection_input": prepared["selection_input"]})
            result = cli.run("verify", "review late assertion", SESSION, str(root), model="fixture")
            assert result["ok"], result
            return result["meta"]["verdict"]

        assert review() == "incomplete"
        # A late receipt can describe even a print if no earlier assertion was observed.
        late_line = "print('first observed here')" if legacy else "assert quantity(-2) == 0"
        check.write_text("from pricing import quantity\n\n" + late_line + "\n", encoding="utf-8")
        assert review() == "pass", "first fresh observation should bind the previously missing assertion"
        baseline = execution.current_execution(root, SESSION)["assertion_baselines"][item["id"]]
        assert baseline == {"source": "current_observation", "receipt": follow.assertion_receipt(root, item["assertion"])}
        check.write_text("from pricing import quantity\n\nprint('no check')\n", encoding="utf-8")
        assert review() == "incomplete"
        assert execution.current_execution(root, SESSION)["assertion_baselines"][item["id"]] == baseline


def _check_binding_publication(root):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from exoma.core.evidence import verify_tests

    session = "baseline-race"
    state = execution._runtime_file(root, session, execution.EXECUTION_FILE)
    oid, ref, command = "quantity", "check_quantity.py:3", "python check_quantity.py"
    item = {"id": oid, "assertion": ref, "behavior": "quantity is clamped", "due": "verify", "commands": [command]}
    selection = {"version": 1, "execution_id": None, "tier": "targeted", "reason": "test binding",
                 "obligations": [item]}
    unbound = {"plan_id": "legacy", "started_at": "first", "approved_at": "first", "selection": selection,
               "assertion_baselines": {oid: {"source": "selection", "receipt": None}}}
    check = root / "check_quantity.py"

    def result(text):
        check.write_text("from pricing import quantity\n\n" + text + "\n", encoding="utf-8")
        fingerprint = verify_tests._identity(root)
        assert fingerprint
        outcome = {"command": command, "status": "passed", "log_ref": "fixture-log", "fingerprint": fingerprint}
        coverage = verify_tests._coverage(selection, [outcome], bound=True)
        coverage["selection_input"] = fingerprint
        follow.check_assertions(root, unbound, coverage, [outcome], fingerprint)
        return {"ok": True, "content": "fixture", "meta": {"verdict": "pass", "runtime_tests": {
            "outcomes": [outcome], "coverage": coverage}}}

    first = result("assert quantity(-2) == 0")
    second = result("assert quantity(-3) == 0")
    runtime = second["meta"]["runtime_tests"]
    row = runtime["coverage"]["obligations"][0]
    receipt = row["assertion_baseline"]["receipt"]
    outcome = runtime["outcomes"][0]
    for controls in ([], [dict(outcome, reused=True)], [dict(outcome, status="refused")],
                     [dict(outcome, command="unrelated")], [dict(outcome, log_ref=None)],
                     [dict(outcome, fingerprint="stale")]):
        coverage = verify_tests._coverage(selection, controls, bound=True)
        follow.check_assertions(root, unbound, coverage, controls, outcome["fingerprint"])
        assert coverage["obligations"][0]["status"] == "open"
        assert "assertion_baseline" not in coverage["obligations"][0]
        # Publication must independently reject a now-unsupported proposal.
        rejected = deepcopy(second)
        rejected["meta"]["runtime_tests"]["outcomes"] = controls
        execution._write(state, unbound)
        execution.record_verification(root, session, rejected)
        assert not rejected["ok"] and execution.current_execution(root, session) == unbound

    stale = deepcopy(second)
    (root / "other_input.py").write_text("changed = True\n", encoding="utf-8")
    execution.record_verification(root, session, stale)
    assert not stale["ok"] and execution.current_execution(root, session) == unbound
    (root / "other_input.py").unlink()

    original_receipt = first["meta"]["runtime_tests"]["coverage"]["obligations"][0]["assertion_baseline"]["receipt"]
    conflicting = deepcopy(unbound)
    conflicting["finding_history"] = {"findings": {
        "A": {"obligation": oid, "assertion": original_receipt, "disposition": "resolved"},
        "B": {"obligation": oid, "assertion": receipt, "disposition": "open"}}}
    execution._write(state, conflicting)
    rejected = deepcopy(second)
    execution.record_verification(root, session, rejected)
    assert not rejected["ok"] and execution.current_execution(root, session) == conflicting

    execution._write(state, unbound)
    start = Barrier(2)
    def publish(report):
        start.wait(timeout=5)
        execution.record_verification(root, session, report, legacy_job_id="job-" + str(report is second))

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(publish, (first, second)))
    published = execution.current_execution(root, session)
    assert not first["ok"] and second["ok"] and len(published["coverage_history"]) == 1
    assert published["assertion_baselines"][oid] == row["assertion_baseline"]
    execution.record_verification(root, session, deepcopy(second), legacy_job_id="job-True")
    assert execution.current_execution(root, session) == published

    # A concurrent historical binding of identical bytes keeps its original attribution.
    published["assertion_baselines"][oid]["source"] = "finding_history"
    execution._write(state, published)
    same_receipt = deepcopy(second)
    execution.record_verification(root, session, same_receipt, legacy_job_id="same-receipt")
    assert same_receipt["ok"]
    assert execution.current_execution(root, session)["assertion_baselines"] == published["assertion_baselines"]
    _check_checkpoint_freshness(root, session, unbound, second)


def _check_checkpoint_freshness(root, session, unbound, original_report):
    from exoma.core.jobs.job_manager import DEAD

    jobs = JobManager(root / ".workflow/jobs")
    state = execution._runtime_file(root, session, execution.EXECUTION_FILE)
    for source, edited, committed in (
        ("saved", "check_quantity.py", False),
        ("saved", "pricing.py", False),
        ("finding_history", "pricing.py", False),
        ("saved", "check_quantity.py", True),
        ("modern", "check_quantity.py", True),
    ):
        initial = deepcopy(unbound)
        report = deepcopy(original_report)
        coverage = report["meta"]["runtime_tests"]["coverage"]
        baseline = coverage["obligations"][0]["assertion_baseline"]
        if source == "finding_history":
            baseline["source"] = source
            initial["finding_history"] = {"findings": {"F1": {
                "obligation": "quantity", "assertion": baseline["receipt"], "disposition": "resolved"}}}
        else:
            initial["assertion_baselines"]["quantity"] = baseline
        if source == "modern":
            initial["execution_id"] = initial["selection"]["execution_id"] = "pinned-round"
            coverage["selection"]["execution_id"] = initial["execution_id"]
            report["meta"]["review_observations"] = {"blockers": []}
        execution._write(state, initial)
        job = jobs.create_job("verify", "checkpoint freshness", session, str(root), None)
        jid = job["job_id"]
        jobs.mark_running(jid)
        record = execution.record_verification

        def interrupt(*args, **kwargs):
            if committed:
                record(*args, **kwargs)
            raise OSError("simulated worker exit")

        with patch.object(execution, "record_verification", side_effect=interrupt):
            try:
                jobs.complete_verify_round(jid, job["recovery_attempt"], deepcopy(report))
            except OSError as exc:
                assert str(exc) == "simulated worker exit"
            else:
                raise AssertionError("checkpoint interruption was not exercised")
        assert jobs.get_job(jid)["verify_checkpoint"]["output"] == report
        before = execution.current_execution(root, session)
        path = root / edited
        original = path.read_bytes()
        try:
            path.write_bytes(b"# changed after checkpoint\n")
            with patch.object(jobs, "liveness", return_value=DEAD):
                result = jobs.get_result(jid)
            if committed:
                assert result["status"] == "completed" and result["output"]["meta"]["verdict"] == "pass"
                assert len(before["coverage_history"]) == 1
                if source == "modern":
                    assert result["output"]["meta"]["verify_followup"] == before["finding_history"]["rounds"][jid]
                else:
                    conflict = {**deepcopy(report), "content": "different checkpoint"}
                    execution.record_verification(root, session, conflict, legacy_job_id=jid)
                    assert not conflict["ok"] and conflict["meta"]["reason"] == "verification_publication_rejected"
            else:
                assert result["status"] == "failed", (source, edited, result["status"])
                assert result["meta"]["reason"] == "verification_publication_rejected"
                assert result["meta"]["verdict"] == "incomplete" and before == initial
            assert execution.current_execution(root, session) == before
        finally:
            path.write_bytes(original)


def _check_publication():
    with TemporaryDirectory() as temp:
        root = Path(temp)
        session = "test"
        current = {"execution_id": "first", "status": "active"}
        state = execution._runtime_file(root, session, execution.EXECUTION_FILE)
        execution._write(state, current)
        jobs = JobManager(root / ".workflow/jobs")
        job = jobs.create_job("verify", "", session, str(root), None)
        jobs.mark_running(job["job_id"])
        pin = job["verify_round"]
        payload = {"ok": True, "content": "[VERIFICATION]\nverdict: NEEDS FIX", "meta": {
            "verdict": "fail", "review_observations": {"blockers": [
                {"origin": "introduced", "description": "quantity contract violated"}]}}}
        assert jobs.complete_verify_round(job["job_id"], "superseded", payload)["status"] == "running"
        assert "finding_history" not in execution.current_execution(root, session)
        save = jobs._save

        def lose_terminal_record(record, **kwargs):
            if record.get("status") == "completed":
                raise OSError("simulated lost terminal write")
            return save(record, **kwargs)

        with patch.object(jobs, "_save", side_effect=lose_terminal_record):
            try:
                jobs.complete_verify_round(job["job_id"], job["recovery_attempt"], payload)
            except OSError:
                pass
            else:
                raise AssertionError("fault was not exercised")
        stored = execution.current_execution(root, session)["finding_history"]
        checkpoint = jobs.get_job(job["job_id"])["verify_checkpoint"]
        cancellation = jobs.cancel(job["job_id"], session, str(root))
        assert not cancellation["ok"] and cancellation["meta"]["error_type"] == "runtime_lock"
        assert stored["revision"] == 1 and len(stored["findings"]) == 1
        from exoma import cli
        from exoma.core.jobs.job_manager import DEAD
        with patch.object(cli, "JOB_MANAGER", jobs), patch.object(jobs, "liveness", return_value=DEAD):
            competing = cli.submit("verify", "different review", session, str(root), None)
            assert competing["meta"]["error_type"] == "job_already_running", competing
            assert jobs.get_job(job["job_id"])["verify_checkpoint"] == checkpoint
            assert jobs.get_result(job["job_id"])["output"]["meta"]["verify_followup"]["round_id"] == job["job_id"]
        assert jobs.get_result(job["job_id"])["status"] == "completed"
        assert execution.current_execution(root, session)["finding_history"] == stored
        assert follow.summary(execution.current_execution(root, session))["open_count"] == 1

        changed = deepcopy(payload)
        changed["content"] += " another result"
        for wrong in (pin, {**pin, "round_id": "different"}):
            try:
                execution.record_verification(root, session, changed, expected="first", pin=wrong)
            except ValueError:
                pass
            else:
                raise AssertionError("same-ID conflict and stale revisions must be rejected")
        assert execution.current_execution(root, session)["finding_history"] == stored
        execution._write(state, {"execution_id": "replacement"})
        execution.record_verification(root, session, changed, expected="first", pin=pin)
        assert "stale" in changed["meta"]["execution_state_error"]
        assert not changed["ok"] and changed["meta"]["verdict"] == "incomplete"
        assert cli._verify_exit_code("verify", changed) == 2
        assert "finding_history" not in execution.current_execution(root, session)
        legacy = {"plan_id": "old", "started_at": "first", "approved_at": "first"}
        execution._write(state, legacy)
        late = jobs.create_job("verify", "legacy review", session, str(root), None)
        jobs.mark_running(late["job_id"])
        replacement = {**legacy, "started_at": "second"}
        execution._write(state, replacement)
        passing = {"ok": True, "content": "review complete", "meta": {"verdict": "pass"}}
        jobs.complete_verify_round(late["job_id"], late["recovery_attempt"], passing)
        rejected = jobs.get_result(late["job_id"])
        assert rejected["status"] == "failed" and rejected["meta"]["verdict"] == "incomplete"
        assert rejected["meta"]["review_content"] == passing["content"]
        assert cli._verify_exit_code("result", rejected, "verify") == 2
        assert execution.current_execution(root, session) == replacement
        legacy_job = jobs.create_job("verify", "retry legacy", session, str(root), None)
        jid = legacy_job["job_id"]
        jobs.mark_running(jid)
        with patch.object(jobs, "_save", side_effect=lose_terminal_record):
            try:
                jobs.complete_verify_round(jid, legacy_job["recovery_attempt"], passing)
            except OSError:
                pass
            else:
                raise AssertionError("legacy terminal-write fault was not exercised")
        published = execution.current_execution(root, session)
        with patch.object(jobs, "liveness", return_value=DEAD):
            assert jobs.get_result(jid)["output"]["meta"]["verdict"] == "pass"
        assert published["last_verify_job"] == jid and execution.current_execution(root, session) == published
        assert follow.rows("[FOLLOWUP_V1]\n{}\ninvalid\n[VERIFICATION]") == []
        assert follow.assertion_receipt(root, "../escape:1") is None


def _check_competing_legacy_attachments(before, proposal, alternative):
    findings = before["finding_history"]["findings"]
    first_id = proposal["id"]
    second_id = "F-0000000000000000"
    second = dict(findings[first_id], id=second_id)
    second.pop("first_case")
    for padded in (dict(proposal, command=proposal["command"] + " "), dict(proposal, status=" attach ")):
        with patch.dict(findings, {second_id: second}):
            prior, events = alternative([proposal, dict(padded, id=second_id)], legacy=True)
        assert {e["id"]: e.get("reason_code") for e in events if e["id"] in (first_id, second_id)} == {
            first_id: "attachment_ambiguous", second_id: "attachment_ambiguous",
        }, events
        assert "attachment" not in prior
        new, = [e for e in events if e["status"] == "new"]
        assert new["id"] not in (first_id, second_id)
        prior, _ = alternative([padded], legacy=True)
        assert prior["attachment"]["source"] == "current_observation"

