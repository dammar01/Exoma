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
    _check_paired_controls()
    _check_publication()
    _check_retry_and_recurrence()


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
                    command="python check.py"):
            coverage = {"selection": {"obligations": [obligation]}, "selection_input": fingerprint,
                        "obligations": [{"id": "quantity", "status": "covered" if status == "passed" else "open"}]}
            result = {"content": "[FOLLOWUP_V1]\n" + "\n".join(json.dumps(r) for r in replies) + "\n[VERIFICATION]",
                      "meta": {"review_observations": {"blockers": blockers}, "runtime_tests": {
                          "coverage": coverage, "outcomes": [{"command": command, "status": status,
                          "log_ref": "receipt", "fingerprint": fingerprint, "reused": reused}] if command else []}}}
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
        assert publish([reply], [])["open_count"] == 1  # Unjustified identical input.
        reply["repeat_reason"] = "discriminate intermittent input from a deterministic failure"
        assert publish([reply], [], reused=True)["open_count"] == 1
        assert publish([dict(reply, status="nonreproduced")], [])["open_count"] == 1
        assert publish([reply, reply], [])["open_count"] == 1  # Ambiguous decision.
        assert publish([reply], [])["events"][0]["status"] == "resolved"
        repeated = publish([dict(reply, status="regressed", blocker=1)], [observation], "failed")
        assert [(e["id"], e["status"]) for e in repeated["events"]] == [(fid, "regressed")]
        del reply["repeat_reason"]
        assert publish([reply], [], fingerprint="genuinely-changed")["open_count"] == 0


def _check_paired_controls():
    from exoma import cli
    from tools.maintain.verify_followup_eval import CASES, drive

    for case in CASES:
        with TemporaryDirectory() as temp, patch.object(cli, "EXECUTOR", cli.EXECUTOR):
            with patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}):
                root = Path(temp)
                first, second = drive(root, case, "replay", "fixture")
                if case in ("repair", "clean"):
                    _check_later_assertion(root, legacy=case == "repair")
                if case == "clean":
                    _check_first_binding(root)
                    _check_binding_publication(root)
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
        elif case == "persistent":
            assert statuses == ["present"] and result["open_count"] == 1
        elif case == "repair":
            assert statuses == ["resolved"] and result["open_count"] == 0
        else:
            assert statuses == ["unchecked"] and "quantity" in result["open_obligations"]
            assert second["diagnostics"][0]["status"] == "passed" and second["verdict"] == "incomplete"


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
        check.write_text("from pricing import quantity\n\nassert quantity(-2) == 0\n", encoding="utf-8")
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

