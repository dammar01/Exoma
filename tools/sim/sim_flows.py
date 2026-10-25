"""Whole-pipeline flow simulation for agent-workflow.

    python tools/sim/sim_flows.py      # no opencode, no quota, seconds

Complements the two existing suites rather than repeating them. `tests/run.py`
asserts units and `tools/e2e/e2e.py` asserts the CLI and installer contracts; this walks one
delegated call from `init` to `clean` against a throwaway git project with a faked
OpenCode adapter, and reports what each STAGE returned — digest, artifacts, locks,
verdicts, recovery. Every mismatch prints expected vs observed so a regression names the
stage it broke instead of a boolean.

Nothing is written outside a temp dir, and no opencode process is spawned: the real
subprocess path (bootstrap retry, rate-limit classification, stall probes) stays the job
of `tools/e2e/e2e.py --full`.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from exoma.core.provider.executor import Executor  # noqa: E402
from exoma.core.jobs.job_manager import JobManager  # noqa: E402
from exoma.core.workspace.session_manager import SessionManager  # noqa: E402
from exoma.core.runtime.upgrade import upgrade_workflow_workspace  # noqa: E402
from exoma.core.workspace.workspace_paths import workflow_paths  # noqa: E402
from exoma.adapters.providers.opencode_adapter import OpenCodeAdapter  # noqa: E402

clean_output = OpenCodeAdapter.clean_output
extract_session_id = OpenCodeAdapter.extract_session_id
import check  # noqa: E402
import main  # noqa: E402

ROWS = []


def record(stage, name, expected, observed, ok):
    ROWS.append((stage, name, expected, observed, "OK" if ok else "MISMATCH"))
    print(
        f"  {'OK      ' if ok else 'MISMATCH'} {stage:<3} {name}\n           expected={expected}\n           observed={observed}"
    )


# The two `grounded` claims below carry their anchors in DIFFERENT shapes on purpose.
# This fixture used to bracket both, which is the shape the anchor extractor wanted — so the
# simulation passed while live runs came back with refs=[] on every claim, because the prompt
# asked second_agent for the bare shape. A fixture written in the format the code prefers
# tests the fixture, not the code. Anchors are unaffected by the change either way:
# _extract_anchors scans file:line across the raw content and never looks at the brackets.
EVIDENCE = """
INFO  2026-05-09T12:10:24 +1ms service=session.prompt session.id=ses_sim001 step=0 loop
> build - gpt-5.3-codex
[EVIDENCE]
confidence: high
grounded:
- entry point defined [app/main.py:1]
- helper defined app/util.py:1
entry_points:
- app/main.py:1
uncertainties:
- none
scope_covered:
- app/
[DIGEST]
summary: simulated evidence.
key_findings:
- app/main.py is the entry point
evidence_basis: grounded
risk_level: low
recommended_next_action: proceed
confidence: high
INFO  2026-05-09T12:10:28 +0ms service=session.idle publishing
""".strip()

VERIFY_PASS = """[VERIFICATION]
verdict: DONE
blocking_findings:
- none
escalations:
- none
notes:
- none
checks_run:
- python -B tests/run.py: pass
not_verified:
- none
confidence: high - all requested checks ran
"""

E2E_SPEC = """[EVIDENCE]
confidence: high

entry_points:
- src/pages/Login.tsx:12

grounded:
- login posts to /api/login and redirects to /dashboard [src/routes/auth.ts:40]

assumptions:
- none

scope_covered:
- src/pages, src/routes

scope_not_covered:
- none

uncertainties:
- none

[E2E SPEC]
claims:
- id: login-valid-user | severity: blocking | description: valid login opens dashboard | source_refs: src/pages/Login.tsx:12

existing_tests:
- none

coverage_gap:
- login-valid-user

scenario_json:
```json
{"version": 1, "feature": "login",
 "claims": [{"id": "login-valid-user", "severity": "blocking", "description": "valid login opens dashboard"}],
 "steps": [
   {"id": "open-login", "action": "goto", "url": "/login"},
   {"id": "fill-email", "action": "fill", "selector": {"role": "textbox", "name": "Email"}, "selector_provenance": {"type": "source"}, "value": "${E2E_USER}"},
   {"id": "submit", "action": "click", "selector": {"role": "button", "name": "Masuk"}, "selector_provenance": {"type": "source"}},
   {"id": "assert-dashboard", "action": "expect_url", "contains": "/dashboard", "claim_id": "login-valid-user"}
 ]}
```

spec_uncertainties:
- none

[DIGEST]
summary: one blocking claim, no existing coverage.
key_findings:
- login redirect must be proven at runtime
risk_level: medium
confidence: high
"""

VERIFY_FAIL = """[VERIFICATION]
verdict: DONE
blocking_findings:
- severity: high | origin: introduced | scope_relation: in_scope
  problem: broken [app/main.py:1]
escalations:
- none
notes:
- none
checks_run:
- python -B tests/run.py: fail
not_verified:
- none
confidence: high - all requested checks ran
"""


class SimAdapter:
    """Stands in for OpenCodeAdapter. `script` maps command -> canned body."""

    command = "opencode"
    timeout_seconds = 0
    no_timeout = True

    def __init__(self):
        self.calls = []
        self.body_for = {}
        self.default_body = EVIDENCE
        self.fail_next = False

    def run(self, prompt, session, model=None, work_dir=None):
        self.calls.append(
            {
                "prompt": prompt,
                "session": dict(session),
                "model": model,
                "work_dir": work_dir,
            }
        )
        if self.fail_next:
            self.fail_next = False
            return {
                "ok": False,
                "content": "simulated opencode failure",
                "meta": {
                    "simulated": True,
                    "error_type": "streaming_failed",
                    "next_action": "retry",
                },
            }
        command = None
        for token in ("explore", "plan", "analyze", "e2e_spec", "verify"):
            if f"[COMMAND] {token}" in prompt or f"command: {token}" in prompt.lower():
                command = token
                break
        body = self.body_for.get(command, self.default_body)
        return {
            "ok": True,
            "content": clean_output(body),
            "meta": {
                "simulated": True,
                "provider_session_id": extract_session_id(body)
                or "ses_sim001",
                "args": ["opencode", "run", prompt],
            },
        }


def read_config(root):
    return json.loads(workflow_paths(root)["config"].read_text(encoding="utf-8"))


def write_config(root, config):
    workflow_paths(root)["config"].write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )


def simulate():
    temp_root = Path(tempfile.mkdtemp(prefix="sim-flows-"))
    project = temp_root / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    (project / "app").mkdir()
    (project / "app" / "main.py").write_text(
        "def main():\n    return 'ready'\n", encoding="utf-8"
    )
    (project / "app" / "util.py").write_text("VALUE = 1\n", encoding="utf-8")
    work_dir = str(project)

    adapter = SimAdapter()
    main.SESSION_MANAGER = SessionManager(temp_root / "provider-sessions")
    main.JOB_MANAGER = JobManager(temp_root / "jobs")
    main.EXECUTOR = Executor(adapter=adapter, session_manager=main.SESSION_MANAGER)
    check.JOB_MANAGER = main.JOB_MANAGER
    original_popen = main.subprocess.Popen

    try:
        # --- S1 init ---------------------------------------------------------
        init = main.run("init", "", "sim-session", work_dir)
        paths = workflow_paths(project)
        created = {
            "config.json": paths["config"].exists(),
            "sessions/": (paths["data_dir"] / "sessions").is_dir(),
            "run scripts": bool(list(paths["workflow_dir"].glob("run.*"))),
            "second_agent.json": (
                paths["workflow_dir"] / "second_agent.json"
            ).exists(),
            ".gitignore": (paths["workflow_dir"] / ".gitignore").exists(),
        }
        record(
            "S1",
            "init bootstraps workspace",
            "ok=True, config+sessions+run scripts+second_agent.json present",
            f"ok={init['ok']}, {created}",
            init["ok"] and all(created.values()),
        )

        # --- S2 doctor -------------------------------------------------------
        doctor = main.run("doctor", "", "sim-session", work_dir)
        status = (doctor.get("meta") or {}).get("status")
        record(
            "S2",
            "doctor reports readiness",
            "status in READY/NEEDS_UPGRADE/NOT_READY",
            f"ok={doctor['ok']}, status={status}",
            status in {"READY", "NEEDS_UPGRADE", "NOT_READY"},
        )

        # --- S3 explore happy path ------------------------------------------
        before_calls = len(adapter.calls)
        explore = main.run("explore", "map the entry point", "sim-session", work_dir)
        runtime_dir = workflow_paths(project, "sim-session")["runtime_dir"]
        artifacts = {
            "prompt.txt": (runtime_dir / "prompt.txt").exists(),
            "leads.json": (runtime_dir / "leads.json").exists(),
            "facts.json": (runtime_dir / "facts.json").exists(),
            "response.last.md": (runtime_dir / "response.last.md").exists(),
        }
        logs = list(
            (workflow_paths(project, "sim-session")["session_dir"] / "logs").glob(
                "*/output.raw.md"
            )
        )
        record(
            "S3",
            "explore returns digest + writes artifacts",
            "ok=True, digest.summary set, 1 opencode call, sidecars+raw log written",
            f"ok={explore['ok']}, digest={(explore.get('digest') or {}).get('summary')!r}, "
            f"calls={len(adapter.calls) - before_calls}, {artifacts}, raw_logs={len(logs)}",
            explore["ok"]
            and (explore.get("digest") or {}).get("summary")
            and len(adapter.calls) - before_calls == 1
            and all(artifacts.values())
            and logs,
        )
        record(
            "S3b",
            "log noise stripped from content",
            "no 'INFO  2026' and no '> build' in content",
            f"INFO={'INFO  2026' in explore['content']}, banner={'> build' in explore['content']}",
            "INFO  2026" not in explore["content"]
            and "> build" not in explore["content"],
        )
        record(
            "S3c",
            "provider session captured",
            "meta.provider_session_id == ses_sim001",
            str(explore["meta"].get("provider_session_id")),
            explore["meta"].get("provider_session_id") == "ses_sim001",
        )

        # --- S4 evidence reuse on identical query ----------------------------
        before_calls = len(adapter.calls)
        again = main.run("explore", "map the entry point", "sim-session", work_dir)
        ref = again.get("evidence_ref") or {}
        record(
            "S4",
            "identical explore reuses stored artifact",
            "evidence_ref.reused=True, 0 new opencode calls",
            f"reused={ref.get('reused')}, new_calls={len(adapter.calls) - before_calls}",
            ref.get("reused") is True and len(adapter.calls) == before_calls,
        )

        # The anchor is app/main.py:1, and freshness hashes that LINE — editing line 2
        # leaves the artifact legitimately fresh. Edit the anchored line itself.
        (project / "app" / "main.py").write_text(
            "def main(flag=False):\n    return 'ready'\n", encoding="utf-8"
        )
        time.sleep(0.05)
        before_calls = len(adapter.calls)
        stale = main.run("explore", "map the entry point", "sim-session", work_dir)
        stale_ref = stale.get("evidence_ref") or {}
        record(
            "S4b",
            "edited anchor line invalidates reuse",
            "reused not True, 1 new opencode call",
            f"reused={stale_ref.get('reused')}, new_calls={len(adapter.calls) - before_calls}",
            stale_ref.get("reused") is not True
            and len(adapter.calls) - before_calls == 1,
        )

        # --- S4c ref_only content stays recoverable ---------------------------
        # Slimming only pays if the pointer it leaves behind actually resolves. That read
        # is a NEW dependency — nothing before this exercised the path where main_agent
        # must open the artifact to see what the payload no longer carries — so it is
        # checked against a real workspace rather than a fabricated path.
        # The fixture is a few hundred chars, well under the shipped 1000-char floor, so
        # the thresholds are lowered for this one scenario instead of padding the fixture
        # into something that no longer looks like real evidence.
        keep = (main.DEFAULT_SLIM_CONTENT_MIN_CHARS, main.DEFAULT_CONTENT_PREVIEW_CHARS)
        main.DEFAULT_SLIM_CONTENT_MIN_CHARS, main.DEFAULT_CONTENT_PREVIEW_CHARS = 50, 60
        try:
            fresh = main.run("explore", "map the slim path", "sim-session", work_dir)
            slim = main._slim_result(fresh, slim_content=True)
        finally:
            main.DEFAULT_SLIM_CONTENT_MIN_CHARS, main.DEFAULT_CONTENT_PREVIEW_CHARS = keep
        artifact = Path((fresh.get("evidence_ref") or {}).get("artifact_path") or "")
        recovered = artifact.read_text(encoding="utf-8") if artifact.is_file() else ""
        record(
            "S4c",
            "ref_only names an artifact that holds the withheld evidence",
            "content shrinks, meta.content_mode=ref_only, named artifact readable and complete",
            f"mode={slim.get('meta', {}).get('content_mode')}, "
            f"chars={len(slim.get('content') or '')} of {len(fresh.get('content') or '')}, "
            f"artifact_readable={bool(recovered)}",
            slim.get("meta", {}).get("content_mode") == "ref_only"
            and len(slim.get("content") or "") < len(fresh.get("content") or "")
            and str(artifact) in (slim.get("content") or "")
            and "[EVIDENCE]" in recovered
            and len(recovered) >= len(fresh.get("content") or ""),
        )

        # --- S5 non-evidence output ------------------------------------------
        menu = SimAdapter()
        menu.default_body = "Specify a command: explore, plan, analyze, verify."
        menu_exec = Executor(adapter=menu, session_manager=main.SESSION_MANAGER)
        menu_session = main.SESSION_MANAGER.load_or_create("menu-session")
        menu_res = menu_exec.execute(
            "analyze", "assess the design", menu_session, work_dir
        )
        record(
            "S5",
            "menu/refusal rejected as proxy failure",
            "ok=False, error_type=invalid_evidence",
            f"ok={menu_res['ok']}, error_type={menu_res['meta'].get('error_type')}",
            not menu_res["ok"]
            and menu_res["meta"].get("error_type") == "invalid_evidence",
        )

        # --- S6 adapter-level failure ----------------------------------------
        adapter.fail_next = True
        failed = main.run("analyze", "trigger adapter failure", "sim-session", work_dir)
        record(
            "S6",
            "adapter failure surfaces as error",
            "ok=False",
            f"ok={failed['ok']}, error_type={failed.get('meta', {}).get('error_type')}",
            not failed["ok"],
        )

        # --- S7 task truncation ----------------------------------------------
        huge = "analyse this dump " + ("x" * 12000)
        truncated = main.run("analyze", huge, "sim-session", work_dir)
        meta = truncated.get("meta", {})
        record(
            "S7",
            "oversize task refused before delegation",
            "ok=False, error_type=task_truncated",
            f"ok={truncated['ok']}, error_type={meta.get('error_type')}, "
            f"kept={meta.get('task_kept_chars')}/{meta.get('task_original_chars')}",
            not truncated["ok"] and meta.get("error_type") == "task_truncated",
        )

        # --- S8 plan end to end ------------------------------------------------
        plan = main.run("plan", "add a health endpoint", "plan-session", work_dir)
        body = (plan.get("content") or "").lower()
        fields = {f: f in body for f in ("grounded", "uncertainties")}
        record(
            "S8",
            "plan returns contract fields",
            "ok=True, grounded+uncertainties present",
            f"ok={plan['ok']}, {fields}, warnings={plan.get('meta', {}).get('contract_warnings')}",
            plan["ok"] and all(fields.values()),
        )

        # --- S9 verify syntax mode ---------------------------------------------
        config = read_config(project)
        config.setdefault("commands", {})["verify_mode"] = "syntax"
        write_config(project, config)
        before_calls = len(adapter.calls)
        quick = main.run("verify", "check the change", "verify-session", work_dir)
        qmeta = quick.get("meta", {})
        record(
            "S9",
            "verify syntax mode stays local",
            "verify_mode=syntax, 0 opencode calls, [QUICK VERIFY] body",
            f"ok={quick['ok']}, mode={qmeta.get('verify_mode')}, "
            f"new_calls={len(adapter.calls) - before_calls}, "
            f"head={(quick.get('content') or '')[:14]!r}",
            qmeta.get("verify_mode") == "syntax" and len(adapter.calls) == before_calls,
        )

        # --- S10 verify delegated: pass and fail --------------------------------
        config["commands"]["verify_mode"] = "delegated"
        write_config(project, config)
        adapter.body_for["verify"] = VERIFY_PASS
        adapter.default_body = VERIFY_PASS
        vpass = main.run(
            "verify", "confirm the change landed", "verify-pass-session", work_dir
        )
        record(
            "S10",
            "delegated verify pass",
            "meta.verdict=pass, exit code 0",
            f"verdict={vpass.get('meta', {}).get('verdict')}, "
            f"exit={main._verify_exit_code('verify', vpass, 'verify')}",
            vpass.get("meta", {}).get("verdict") == "pass"
            and main._verify_exit_code("verify", vpass, "verify") == 0,
        )

        adapter.body_for["verify"] = VERIFY_FAIL
        adapter.default_body = VERIFY_FAIL
        vfail = main.run(
            "verify", "confirm the change landed", "verify-fail-session", work_dir
        )
        record(
            "S10b",
            "delegated verify fail overrides self-declared DONE",
            "meta.verdict=fail, exit code 2",
            f"verdict={vfail.get('meta', {}).get('verdict')}, "
            f"exit={main._verify_exit_code('verify', vfail, 'verify')}",
            vfail.get("meta", {}).get("verdict") == "fail"
            and main._verify_exit_code("verify", vfail, "verify") == 2,
        )
        adapter.default_body = EVIDENCE
        adapter.body_for.clear()

        # --- S11 runtime lock contention ---------------------------------------
        entered, release = threading.Event(), threading.Event()

        class BlockingAdapter(SimAdapter):
            def run(self, *a, **kw):
                entered.set()
                release.wait(timeout=10)
                return super().run(*a, **kw)

        main.EXECUTOR = Executor(
            adapter=BlockingAdapter(), session_manager=main.SESSION_MANAGER
        )
        holder_out = []
        holder = threading.Thread(
            target=lambda: holder_out.append(
                main.run("analyze", "hold the lock", "lock-session", work_dir)
            )
        )
        holder.start()
        entered.wait(timeout=10)
        contender = main.run(
            "analyze", "contend for the lock", "lock-session", work_dir
        )
        release.set()
        holder.join(timeout=15)
        record(
            "S11",
            "second call on a locked session is rejected",
            "ok=False, error_type=runtime_lock; holder still ok=True",
            f"contender_ok={contender['ok']}, error_type={contender['meta'].get('error_type')}, "
            f"holder_ok={holder_out[0]['ok'] if holder_out else 'no result'}",
            not contender["ok"]
            and contender["meta"].get("error_type") == "runtime_lock"
            and holder_out
            and holder_out[0]["ok"],
        )
        main.EXECUTOR = Executor(adapter=adapter, session_manager=main.SESSION_MANAGER)

        lock_file = workflow_paths(project, "lock-session")["lock"]
        record(
            "S11b",
            "lock released after the holder finishes",
            "lock file absent",
            f"exists={lock_file.exists()}",
            not lock_file.exists(),
        )

        # --- S12 submit / attach / recovery / exhaustion -------------------------
        class FakeProc:
            def __init__(self, pid):
                self.pid = pid

        def fake_popen(*a, **kw):
            # Fake ONLY the worker spawn. `main.subprocess.Popen` is the real stdlib
            # attribute, so `subprocess.run` goes through it too — a blanket fake breaks
            # unrelated POSIX calls (`ps` in `_process_identity`) that Windows skips.
            argv = a[0] if a else kw.get("args") or []
            argv = [str(x) for x in argv] if isinstance(argv, (list, tuple)) else []
            if "--command" in argv and "worker" in argv:
                return FakeProc(os.getpid())
            return original_popen(*a, **kw)

        main.subprocess.Popen = fake_popen
        submitted = main.submit(
            "analyze", "long running task", "job-session", work_dir, None
        )
        attached = main.submit(
            "analyze", "long running task", "job-session", work_dir, None
        )
        blocked = main.submit(
            "analyze", "a different task", "job-session", work_dir, None
        )
        record(
            "S12",
            "identical resubmit attaches, different task is blocked",
            "same job_id + reused=True; different task error_type=job_already_running",
            f"submitted={submitted['status']}, attached_same={attached['job_id'] == submitted['job_id']}, "
            f"reused={attached['meta'].get('reused')}, blocked={blocked['meta'].get('error_type')}",
            submitted["ok"]
            and attached["job_id"] == submitted["job_id"]
            and attached["meta"].get("reused")
            and blocked["meta"].get("error_type") == "job_already_running",
        )

        main.JOB_MANAGER.set_worker_pid(submitted["job_id"], 999999999)
        recovered = main.submit(
            "analyze", "long running task", "job-session", work_dir, None
        )
        main.JOB_MANAGER.set_worker_pid(submitted["job_id"], 999999999)
        exhausted = main.submit(
            "analyze", "long running task", "job-session", work_dir, None
        )
        record(
            "S12b",
            "worker death recovers once, then stops",
            "1st death: recovery=True same job; 2nd death: ok=False reason=recovery_exhausted",
            f"recovery={recovered['meta'].get('recovery')}, same_job={recovered['job_id'] == submitted['job_id']}, "
            f"exhausted_ok={exhausted['ok']}, reason={exhausted['meta'].get('reason')}",
            recovered["ok"]
            and recovered["meta"].get("recovery")
            and recovered["job_id"] == submitted["job_id"]
            and not exhausted["ok"]
            and exhausted["meta"].get("reason") == "recovery_exhausted",
        )
        record(
            "S12c",
            "exhaustion releases the session lock",
            "active_job_for_session is None",
            str(main.JOB_MANAGER.active_job_for_session("job-session")),
            main.JOB_MANAGER.active_job_for_session("job-session") is None,
        )
        main.subprocess.Popen = original_popen

        # --- S13 worker path ------------------------------------------------------
        worker_job = main.JOB_MANAGER.create_job(
            "explore", "run through the worker", "worker-session", work_dir, None
        )
        worker_out = main.run_worker(worker_job["job_id"])
        worker_status = main.get_status(worker_job["job_id"])
        result_ok, result_payload = check._result_payload(worker_job["job_id"])
        record(
            "S13",
            "worker executes queued job and persists state",
            "worker ok=True, status=completed, check.py returns cleaned content",
            f"ok={worker_out['ok']}, status={worker_status['status']}, "
            f"check_ok={result_ok}, payload_is_str={isinstance(result_payload, str)}",
            worker_out["ok"] and worker_status["status"] == "completed" and result_ok,
        )

        # --- S14 recovery worker reuses the captured provider session --------------
        rec_session = main.SESSION_MANAGER.load_or_create("recovery-session")
        main.SESSION_MANAGER.update_provider_session_id(rec_session, "ses_recover999")
        rec_job = main.JOB_MANAGER.create_job(
            "explore",
            "finish the interrupted mapping",
            "recovery-session",
            work_dir,
            None,
        )
        main.JOB_MANAGER.set_worker_pid(rec_job["job_id"], 999999999)
        main.JOB_MANAGER.mark_running(rec_job["job_id"])
        claim = main.JOB_MANAGER.claim_recovery(
            rec_job["job_id"], stale_after_seconds=0
        )
        main.JOB_MANAGER.release_recovery_claim(rec_job["job_id"])
        rec_out = main.run_worker(rec_job["job_id"])
        last = adapter.calls[-1]
        record(
            "S14",
            "recovery resumes the same provider session with continuation",
            "action=recover, prompt carries continuation + original task, session=ses_recover999",
            f"action={claim['action']}, ok={rec_out['ok']}, "
            f"continuation={'Continue the interrupted task' in last['prompt']}, "
            f"task_in_prompt={'finish the interrupted mapping' in last['prompt']}, "
            f"provider={last['session'].get('provider_session_id')}",
            claim["action"] == "recover"
            and rec_out["ok"]
            and "Continue the interrupted task" in last["prompt"]
            and last["session"].get("provider_session_id") == "ses_recover999",
        )

        # --- S15 session isolation ----------------------------------------------
        a = main.run("explore", "isolated query A", "iso-session-a", work_dir)
        b = main.run("explore", "isolated query B", "iso-session-b", work_dir)
        dir_a = workflow_paths(project, "iso-session-a")["session_dir"]
        dir_b = workflow_paths(project, "iso-session-b")["session_dir"]
        record(
            "S15",
            "two sessions keep separate state trees",
            "distinct session dirs, both with their own state.json",
            f"a={dir_a.name}, b={dir_b.name}, "
            f"state_a={(dir_a / 'state.json').exists()}, state_b={(dir_b / 'state.json').exists()}",
            a["ok"]
            and b["ok"]
            and dir_a != dir_b
            and (dir_a / "state.json").exists()
            and (dir_b / "state.json").exists(),
        )

        weird = main.run(
            "explore", "path traversal session id", "../../evil id", work_dir
        )
        sessions_root = workflow_paths(project)["data_dir"] / "sessions"
        escaped = [p for p in sessions_root.iterdir() if p.is_dir()]
        record(
            "S15b",
            "unsafe session id cannot escape the sessions dir",
            "sanitized dir stays under .workflow/sessions",
            f"ok={weird['ok']}, dirs={[p.name for p in escaped][:6]}",
            weird["ok"]
            and all(
                sessions_root.resolve() in p.resolve().parents
                or p.parent == sessions_root
                for p in escaped
            ),
        )

        # --- S16 sweep is local ---------------------------------------------------
        before_calls = len(adapter.calls)
        sweep = main.run("sweep", "", "sim-session", work_dir)
        record(
            "S16",
            "sweep produces a report without spending a call",
            "ok=True, meta.report set, 0 opencode calls",
            f"ok={sweep['ok']}, report={bool(sweep.get('meta', {}).get('report'))}, "
            f"new_calls={len(adapter.calls) - before_calls}",
            sweep["ok"]
            and sweep.get("meta", {}).get("report")
            and len(adapter.calls) == before_calls,
        )

        # --- S17 upgrade gate -----------------------------------------------------
        # Content, not version: a generator change shipped without a version bump used to
        # never reach an existing workspace. Bumping runtime_version alone must now change
        # nothing, while a script that actually differs on disk must be rewritten.
        lazy = upgrade_workflow_workspace(project, str(REPO_ROOT / "main.py"))
        config = read_config(project)
        config.setdefault("runtime", {})["runtime_version"] = "0.0.0"
        write_config(project, config)
        bumped = upgrade_workflow_workspace(project, str(REPO_ROOT / "main.py"))
        script = next(
            (
                p
                for p in (project / ".workflow").glob("run.*")
                if p.suffix in {".ps1", ".sh"}
            ),
            None,
        )
        if script is not None:
            script.write_text("# drifted\n", encoding="utf-8")
        drifted = upgrade_workflow_workspace(project, str(REPO_ROOT / "main.py"))
        record(
            "S17",
            "upgrade regenerates scripts when content differs, not when the version moves",
            "unchanged: 0 scripts; version bumped alone: 0; drifted script: >0",
            f"unchanged={len(lazy['regenerated_scripts'])}, "
            f"version_bumped={len(bumped['regenerated_scripts'])}, "
            f"drifted={len(drifted['regenerated_scripts'])}",
            lazy["regenerated_scripts"] == []
            and bumped["regenerated_scripts"] == []
            and len(drifted["regenerated_scripts"]) > 0,
        )

        # --- S18 clean / prune ----------------------------------------------------
        cleaned = main.run("clean", "", "sim-session", work_dir)
        cmeta = cleaned.get("meta", {})
        record(
            "S18",
            "clean prunes jobs, facts and sessions",
            "ok=True with job/fact/session counters",
            f"ok={cleaned['ok']}, kept={cmeta.get('kept')}, removed={cmeta.get('removed')}, "
            f"facts={cmeta.get('facts')}, sessions={cmeta.get('sessions')}",
            cleaned["ok"] and "facts" in cmeta and "sessions" in cmeta,
        )

        # --- S19 inspect ----------------------------------------------------------
        inspected = main.run("inspect", "", "sim-session", work_dir)
        record(
            "S19",
            "inspect reports workspace state",
            "ok=True",
            f"ok={inspected['ok']}, keys={sorted(list((inspected.get('meta') or {}).keys()))[:6]}",
            inspected["ok"],
        )

        # --- S20 routing guard ----------------------------------------------------
        from exoma.core.prompt.router import Router

        rejected = None
        try:
            Router().route("execute")
        except ValueError as exc:
            rejected = str(exc)
        routed = main.EXECUTOR.execute(
            "execute",
            "do the thing",
            main.SESSION_MANAGER.load_or_create("route-session"),
            work_dir,
        )
        record(
            "S20",
            "execute is not delegable",
            "Router raises; Executor returns error_type=routing_error",
            f"router_raised={rejected is not None}, ok={routed['ok']}, "
            f"error_type={routed.get('meta', {}).get('error_type')}",
            rejected is not None
            and not routed["ok"]
            and routed["meta"].get("error_type") == "routing_error",
        )

        # --- S21 clean releases stale session locks --------------------------------
        # A lock whose owner can never finish must go; one with a live owner must not.
        # Getting the second half wrong would let clean steal a lock mid-call.
        manager = JobManager()
        orphan = manager.lock_dir / "sim-orphan-lock.lock"
        orphan.write_text(
            json.dumps({"job_id": "job_sim_missing", "token": "sim"}), encoding="utf-8"
        )
        live_lock = manager.lock_dir / "sim-live-lock.lock"
        live_job_id = "job_sim_live"
        live_lock.write_text(
            json.dumps({"job_id": live_job_id, "token": "sim"}), encoding="utf-8"
        )
        live_record = manager._path(live_job_id)
        live_record.write_text(
            json.dumps(
                {
                    "job_id": live_job_id,
                    "status": "running",
                    "worker_pid": os.getpid(),
                    "worker_create_time": None,
                    "worker_identity": None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        try:
            outcome = manager.release_stale_session_locks()
            freed = {entry["session_id"] for entry in outcome["released"]}
            record(
                "S21",
                "clean releases orphaned session locks but spares live ones",
                "orphan released; live lock kept",
                f"released={sorted(freed)}, orphan_gone={not orphan.exists()}, "
                f"live_kept={live_lock.exists()}",
                "sim-orphan-lock" in freed
                and not orphan.exists()
                and live_lock.exists(),
            )
        finally:
            for path in (orphan, live_lock, live_record):
                try:
                    path.unlink()
                except OSError:
                    pass

        # --- S22 verify-browser: draft, confirmed run, fake player, canonical verdict ---
        # The draft answers on the internal `e2e_spec` route with a spec, the run's review
        # on `verify` with a clean review; the player is the shipped fake. Same entry
        # point as every other flow (`main.run`), so the lock, the finaliser, and the exit
        # code are the real ones. Settings travel in the session's request file only.
        from exoma.core.evidence.e2e.request import request_path

        # The file E2E_SPEC cites: grounding refuses a spec whose references do not exist.
        login_source = project / "src" / "pages" / "Login.tsx"
        login_source.parent.mkdir(parents=True, exist_ok=True)
        login_source.write_text("\n".join(f"export const line{n} = {n};" for n in range(1, 21)) + "\n", encoding="utf-8")
        adapter.body_for["e2e_spec"] = E2E_SPEC
        adapter.body_for["verify"] = VERIFY_PASS
        adapter.default_body = VERIFY_PASS
        saved_env = {k: os.environ.get(k) for k in ("WORKFLOW_E2E_FAKE", "E2E_USER")}

        def write_request(session, body):
            path = request_path(project, session)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"version": 1, "settings": {}, **body}), encoding="utf-8")

        def browser_flow(session):
            write_request(session, {"phase": "draft"})
            draft = main.run("verify-browser", "prove the login change", session, work_dir)
            info = draft.get("meta", {}).get("e2e", {}).get("draft") or {}
            if info.get("status") != "ready":
                return draft, draft
            write_request(session, {"phase": "run", "scenario": info["scenario"], "existing_tests": info["existing_tests"]})
            return draft, main.run("verify-browser", "prove the login change", session, work_dir)

        def exit_code(result):
            command, _ = main._browser_exit_commands("verify-browser", result, "explore")
            return main._verify_exit_code(command, result)

        try:
            os.environ["WORKFLOW_E2E_FAKE"] = "pass"
            os.environ["E2E_USER"] = "sim-user"
            before_calls = len(adapter.calls)
            draft, e2e_pass = browser_flow("e2e-pass-session")
            stage_cmds = [c.get("command") for c in e2e_pass.get("meta", {}).get("e2e", {}).get("stages", [])]
            record(
                "S22",
                "verify-browser pass: draft spec, confirmed run, fake player, hybrid review",
                "draft ready (exit 0), run stages request/player/verify, 2 provider calls, verdict pass, exit 0",
                f"draft={draft.get('meta', {}).get('e2e', {}).get('draft', {}).get('status')}/exit={exit_code(draft)}, "
                f"stages={stage_cmds}, new_calls={len(adapter.calls) - before_calls}, "
                f"verdict={e2e_pass.get('meta', {}).get('verdict')}, exit={exit_code(e2e_pass)}",
                draft.get("meta", {}).get("phase") == "draft"
                and exit_code(draft) == 0
                and e2e_pass.get("meta", {}).get("invocation") == "verify-browser"
                and stage_cmds == [None, None, "verify"]
                and len(adapter.calls) - before_calls == 2
                and e2e_pass.get("meta", {}).get("verdict") == "pass"
                and exit_code(e2e_pass) == 0,
            )

            os.environ["WORKFLOW_E2E_FAKE"] = "app_fail"
            _, e2e_fail = browser_flow("e2e-fail-session")
            record(
                "S22b",
                "verify-browser app failure outranks a clean review",
                "verdict fail, NEEDS FIX, exit 2, reviewer override recorded",
                f"verdict={e2e_fail.get('meta', {}).get('verdict')}, exit={exit_code(e2e_fail)}, "
                f"overridden={any(w.get('kind') == 'reviewer_verdict_overridden' for w in e2e_fail.get('meta', {}).get('contract_warnings', []))}",
                e2e_fail.get("meta", {}).get("verdict") == "fail"
                and "verdict: NEEDS FIX" in (e2e_fail.get("content") or "")
                and exit_code(e2e_fail) == 2,
            )

            os.environ["WORKFLOW_E2E_FAKE"] = "harness_fail"
            before_calls = len(adapter.calls)
            _, e2e_inc = browser_flow("e2e-inc-session")
            record(
                "S22c",
                "verify-browser harness failure is incomplete and skips the reviewer",
                "verdict incomplete, 1 provider call (draft spec only), reason named",
                f"verdict={e2e_inc.get('meta', {}).get('verdict')}, new_calls={len(adapter.calls) - before_calls}, "
                f"reason={e2e_inc.get('meta', {}).get('e2e', {}).get('reason')}",
                e2e_inc.get("meta", {}).get("verdict") == "incomplete"
                and len(adapter.calls) - before_calls == 1,
            )
        finally:
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            adapter.default_body = EVIDENCE
            adapter.body_for.clear()

    finally:
        main.subprocess.Popen = original_popen
        shutil.rmtree(temp_root, ignore_errors=True)


def main_entry():
    print("[SIMULATION] agent-workflow E2E flows (faked OpenCode, temp project)\n")
    simulate()
    mismatched = [r for r in ROWS if r[4] != "OK"]
    print("\n[SIMULATION REPORT]")
    print(
        f"  {len(ROWS) - len(mismatched)} as-expected | {len(mismatched)} mismatched ({len(ROWS)} flows)"
    )
    for row in mismatched:
        print(f"  MISMATCH {row[0]} {row[1]}")
    return 1 if mismatched else 0


if __name__ == "__main__":
    sys.exit(main_entry())
