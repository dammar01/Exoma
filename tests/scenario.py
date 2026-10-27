"""Unit and integration checks for Exoma.

    python tests/run.py               # everything
    python tests/run.py --list        # what can be run on its own
    python tests/scenario.py          # this suite only, same as before

The individual checks live in tests/checks/; this module owns the shared fixture setup
and the ordering, which is stateful — later checks read workspaces earlier ones
built. That statefulness is why this suite has no per-section entry point: the standalone
checks it calls at the end DO have one, through tests/run.py.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# Run from anywhere: `python tests/scenario.py` puts tests/ on sys.path, not the repo
# root, so every `import main` below would miss without this.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))
# Global state (job records, the main-session cache, the provider seed) goes to a
# throwaway EXOMA_HOME, never the user's own data directory. Set before anything imports
# exoma.config.settings, which resolves it once.
if not os.environ.get("EXOMA_HOME"):
    os.environ["EXOMA_HOME"] = tempfile.mkdtemp(prefix="exoma-test-home-")
# Child interpreters the checks spawn (`python -c ...` lock holders, probes) import exoma
# from this checkout's src/, as they used to import core/ from the repo root they ran in.
os.environ["PYTHONPATH"] = os.pathsep.join(
    part for part in (str(REPO_ROOT / "src"), os.environ.get("PYTHONPATH", "")) if part
)

from exoma import check
from exoma import cli as main
from exoma.adapters.providers.opencode_adapter import OpenCodeAdapter
from exoma.core.provider.executor import Executor
from exoma.core.jobs.job_manager import JobManager
from exoma.core.prompt.prompt_builder import build_prompt
from exoma.core.runtime.state import ensure_workflow_workspace

from tests.checks.support import (
    FakeJobProcess,
    FakeOpenCodeAdapter,
    RecordingOpenCodeAdapter,
    assert_true,
    clean_output,
    extract_session_id,
)

from tests.checks.facts import (
    _test_anchor_relocation,
    _test_evidence_anchor_relocation,
    _test_evidence_lock_file,
    _test_evidence_reuse,
    _test_fact_selection,
    _test_facts_concurrency,
)
from tests.checks.runtime_guards import _test_runtime_guard_cleanup
from tests.checks.process_identity import (
    _test_byte_lock_excludes_handles_and_processes,
    _test_process_generation_outcomes,
    _test_worker_kill_is_fail_closed,
)
from tests.checks.task_telemetry import _test_task_telemetry
from tests.checks.redaction import (
    _test_key_pattern_needs_a_left_boundary,
    _test_redaction_boundary,
)
from tests.checks.verify_gaps import (
    _test_empty_section_is_not_a_finding,
    _test_quick_verify_gaps,
    _test_verification_routing,
)
from tests.checks.jobs import _test_submit_admission
from tests.checks.workspace import (
    _test_init_upgrade_and_session_guard,
    _test_script_drift_path_spelling,
    _test_project_session_isolation,
    _test_workspace_release_guards,
)
from tests.checks.continuation import (
    _test_contract_continuation,
    _test_continuation_prompt_is_bounded,
)
from tests.checks.cli import _test_cli_script_reaches_its_exit_code
from tests.checks.contracts import _test_workflow_contracts
from tests.checks.promote import _test_promote_boundaries
from tests.checks.execution import _test_execution_chain
from tests.checks.e2e_browser import _test_e2e_browser_session
from tests.checks.e2e_doctor import _test_e2e_doctor_readiness
from tests.checks.e2e_existing_tests import _test_e2e_existing_tests
from tests.checks.e2e_metrics import _test_e2e_metrics
from tests.checks.e2e_harness import _test_e2e_harness_names_its_model
from tests.checks.e2e_normalize import _test_e2e_classification_and_verdicts
from tests.checks.e2e_network import _test_e2e_network
from tests.checks.e2e_redact import _test_e2e_redaction_variants
from tests.checks.e2e_smoke import _test_e2e_real_browser_smoke
from tests.checks.e2e_routing import _test_e2e_routing
from tests.checks.e2e_spec import _test_e2e_spec_contract
from tests.checks.e2e_supervisor import _test_e2e_supervisor
from tests.checks.e2e_tagging import _test_e2e_tagging
from tests.checks.e2e_hardening import _test_e2e_hardening
from tests.checks.e2e_knowledge import _test_e2e_knowledge
from tests.checks.installer import (
    _test_installer_check_reports_second_agent_default,
    _test_provider_update,
    _test_installer_drift_check,
    _test_installer_e2e_deps_are_opt_in,
    _test_installer_rollback_receipt,
    _test_installer_seed_is_non_interactive_without_a_tty,
    _test_installer_seed_prompt_answers,
    _test_installer_seed_reports_unenforced_provider,
    _test_installer_seeds_second_agent_config,
    _test_installer_settings_are_additive,
    _test_installer_settings_merge,
    _test_installer_text_merging,
)
from tests.checks.deps import _test_runtime_is_stdlib_only
from tests.checks.hidden_spawn import _test_every_spawn_hides_its_console
from tests.checks.governance import _test_governance_controls
from tests.checks.graph_refresh import _test_graph_refresh_hook
from tests.checks.graph_verification import _test_graph_verification
from tests.checks.hardening import _test_hardening
from tests.checks.telemetry import _test_telemetry_metrics
from tests.checks.transcript import _test_transcript_parsing
from tests.checks.usage_tokens import _test_usage_token_accounting
from tests.checks.messages import _test_no_code_in_messages
from tests.checks.provider import (
    _test_agy_provider,
    _test_doctor_read_boundary_warning,
    _test_secret_files,
    _test_provider_seam,
    _test_provider_selection,
)
from tests.checks.registry import _test_every_check_is_registered
from tests.checks.bundle_sync import (
    _test_bundle_registry_bijection,
    _test_every_shipped_hook_is_a_launcher_and_its_module,
)
from tests.checks.stamp_version import _test_stamp_version_reads_versions_not_addresses
from tests.checks.manifest import (
    _test_malformed_manifest_entry_fails_closed,
    _test_manifest_matches_dist,
)
from tests.checks.codex_write_probe import _test_codex_sandbox_blocks_writes
from tests.checks.atomic_writers import (
    _test_atomic_helpers_keep_their_contract,
    _test_every_writer_outlasts_a_held_file,
)
from tests.checks.session_persistence import (
    _test_atomic_write_outlasts_a_held_file,
    _test_provider_thread_id_is_never_lost_silently,
    _test_result_names_the_provider_boundary,
)
from tests.checks.workflow_layout import _test_workflow_layout
from tests.checks.workspace_migration import _test_workspace_migration
from tests.checks.installer_stale import _test_installer_leaves_no_stale_files
from tests.checks.isolation import _test_suite_ignores_the_machine_seed
from tests.checks.installer_uninstall import _test_installer_uninstall
from tests.checks.intent_gate import (
    _test_intent_gate_reads_the_prompt_field,
    _test_intent_gate_runner_parsed,
    _test_intent_gate_runner_tamper,
    _test_intent_gate_non_ascii_root,
)
from tests.checks.hook_contract import (
    _test_gate_k5_fixtures,
    _test_hook_launchers_resolve_python,
    _test_hooks_run_through_their_launchers,
)
from tests.checks.agy_host import (
    _test_agy_host_contract,
    _test_agy_host_install_all_or_nothing,
    _test_agy_host_install_merges,
    _test_agy_host_uninstall,
)
from tests.checks.os_resolvers import (
    _test_entry_points_refuse_an_old_python,
    _test_find_bash_and_os_locations,
    _test_find_python_runs_each_candidate,
)
from tests.checks.verify_tests import _test_verify_runtime_tests
from tests.checks.metrics import _test_metrics_contract
from tests.checks.replay import _test_fact_replay
from tests.checks.opencode_launch import _test_opencode_prompt_never_reaches_cmd
from tests.checks.provider_sessions import _test_provider_threads_are_kept_per_provider
from tests.checks.statusline import _test_statusline_failed_calls_are_not_estimates
from tests.checks.adapters import (
    _test_adapter_error_normalization,
    _test_error_types_are_registered,
    _test_stdin_failure_reaches_call_meta,
    _test_adapter_redaction_is_shared,
)
from tests.checks.audit import (
    _test_audit_is_not_telemetry,
    _test_audit_report,
    _test_audit_survives_a_torn_row,
)
from tests.checks.prompt import (
    _test_permitted_tools_line,
    _test_prompt_contract_blocks,
    _test_task_cap_follows_the_provider_transport,
    _test_task_cap_is_visible_in_and_out_of_band,
    _test_unknown_role_is_refused,
    _test_verify_branch_carries_routing_contract,
)
from tests.checks.support import no_console

def run_tests() -> None:
    _test_every_check_is_registered()
    _test_bundle_registry_bijection()
    _test_every_shipped_hook_is_a_launcher_and_its_module()
    _test_stamp_version_reads_versions_not_addresses()
    _test_manifest_matches_dist()
    _test_malformed_manifest_entry_fails_closed()
    _test_workflow_layout()
    _test_workspace_migration()
    _test_installer_leaves_no_stale_files()
    _test_suite_ignores_the_machine_seed()
    _test_installer_uninstall()
    _test_intent_gate_reads_the_prompt_field()
    _test_intent_gate_runner_parsed()
    _test_intent_gate_runner_tamper()
    _test_intent_gate_non_ascii_root()
    _test_hooks_run_through_their_launchers()
    _test_hook_launchers_resolve_python()
    _test_agy_host_contract()
    _test_agy_host_install_merges()
    _test_agy_host_install_all_or_nothing()
    _test_agy_host_uninstall()
    _test_gate_k5_fixtures()
    _test_entry_points_refuse_an_old_python()
    _test_find_python_runs_each_candidate()
    _test_find_bash_and_os_locations()
    _test_verify_runtime_tests()
    _test_metrics_contract()
    _test_fact_replay()
    _test_opencode_prompt_never_reaches_cmd()
    _test_statusline_failed_calls_are_not_estimates()
    _test_provider_seam()
    _test_provider_threads_are_kept_per_provider()
    _test_atomic_write_outlasts_a_held_file()
    _test_atomic_helpers_keep_their_contract()
    _test_every_writer_outlasts_a_held_file()
    _test_provider_thread_id_is_never_lost_silently()
    _test_result_names_the_provider_boundary()
    _test_codex_sandbox_blocks_writes()
    _test_provider_selection()
    _test_agy_provider()
    _test_doctor_read_boundary_warning()
    _test_secret_files()
    _test_no_code_in_messages()
    _test_adapter_error_normalization()
    _test_error_types_are_registered()
    _test_key_pattern_needs_a_left_boundary()
    _test_adapter_redaction_is_shared()
    _test_stdin_failure_reaches_call_meta()
    _test_prompt_contract_blocks()
    _test_verify_branch_carries_routing_contract()
    _test_permitted_tools_line()
    _test_task_cap_is_visible_in_and_out_of_band()
    _test_task_cap_follows_the_provider_transport()
    _test_unknown_role_is_refused()
    _test_audit_report()
    _test_audit_survives_a_torn_row()
    _test_audit_is_not_telemetry()
    fake_opencode = FakeOpenCodeAdapter()
    temp_root = Path(tempfile.mkdtemp(prefix="agent-workflow-test-"))
    original_popen = main.subprocess.Popen
    main.SESSION_MANAGER = main.SessionManager(temp_root / "sessions")
    main.JOB_MANAGER = JobManager(temp_root / "jobs")
    main.EXECUTOR = Executor(adapter=fake_opencode, session_manager=main.SESSION_MANAGER)
    check.JOB_MANAGER = main.JOB_MANAGER
    work_dir = str(temp_root)
    git_init = main.subprocess.run(
        ["git", "init", "-q", str(temp_root)], capture_output=True, text=True
    , **no_console())
    assert_true(git_init.returncode == 0, f"test fixture git init failed: {git_init.stderr}")
    ensure_workflow_workspace(temp_root, os.getenv("AGENT_PATH"))

    try:
        # 1. Prompt constraints
        prompt = build_prompt(
            role="exploration",
            task="cek graph",
            session_id="prompt-test",
            command="explore",
            project_root=str(temp_root),
        )
        assert_true("[WORKFLOW_AGENT]" in prompt, "prompt must tag workflow-agent context")
        # The prompt stays within the Windows argv limit by anchoring the full protocol
        # in AGENTS.md.
        assert_true(
            "full evidence protocol in AGENTS.md" in prompt,
            "evidence prompt must anchor the full protocol (graphify-first) to AGENTS.md",
        )
        claude_contract = (
            REPO_ROOT
            / "src"
            / "exoma"
            / "_bundle"
            / "config"
            / "claude"
            / "CLAUDE.md"
        ).read_text(encoding="utf-8")
        assert_true(
            "run_in_background: true" in claude_contract,
            "Claude delegated runner must use the background-task tool mode",
        )
        assert_true(
            "meta.reason=recovery_exhausted" in claude_contract,
            "Claude contract must stop bounded recovery after the second death",
        )

        # 2. Bootstrap flow
        recording_opencode = RecordingOpenCodeAdapter()
        bootstrapped = recording_opencode.run(
            "real prompt",
            {"session_id": "bootstrap-test", "provider_session_id": None},
            "provider/model",
            work_dir,
        )
        assert_true(bootstrapped["ok"], "bootstrap flow must succeed")
        assert_true(recording_opencode.init_calls[0]["work_dir"] == work_dir, "init must receive work_dir")
        assert_true("--print-logs" in recording_opencode.init_calls[0]["args"], "init must use --print-logs")
        assert_true("stderr" not in bootstrapped["meta"]["bootstrap"], "init logs must not be exposed in bootstrap meta")
        assert_true(recording_opencode.run_calls[0]["work_dir"] == work_dir, "agent run must receive work_dir")
        run_args = recording_opencode.run_calls[0]["args"]
        assert_true("--print-logs" not in run_args, "agent run must not use --print-logs")
        assert_true(
            run_args[run_args.index("--format") + 1] == "json",
            "agent run must ask for the JSON event stream: it is the only mode that "
            "reports token counts, so losing this flag silently returns every usage row "
            "to a chars//4 estimate",
        )
        assert_true("--log-level" not in run_args, "agent run must not use --log-level after init")
        assert_true(run_args[-2:] == ["-s", "ses_boot123"], "agent run must resume captured OpenCode session")

        # 3. Explore command
        explore = main.run("explore", "cari entry point module auth", "test-session-v2", work_dir=work_dir)
        assert_true(explore["ok"], "explore must succeed")
        assert_true(explore["meta"]["provider_session_id"] == "ses_test123", "first run must capture OpenCode session")
        assert_true(fake_opencode.calls[-1]["work_dir"] == work_dir, "work_dir must reach adapter")
        assert_true("INFO  2026" not in explore["content"], "OpenCode logs must be stripped")
        assert_true("> build" not in explore["content"], "OpenCode model banner must be stripped")
        assert_true(
            "args" not in explore.get("meta", {}),
            "Executor must remove raw argv from injected adapter results",
        )

        # 4. Analyze with model override and session reuse
        analyze = main.run(
            "analyze",
            "cek logic auth",
            "test-session-v2",
            work_dir=work_dir,
            model="9router-sdi/gpt-5.3-codex",
        )
        assert_true(analyze["ok"], "analyze must succeed")
        assert_true(fake_opencode.calls[-1]["model"] == "9router-sdi/gpt-5.3-codex", "model override must reach adapter")
        assert_true(
            fake_opencode.calls[-1]["session"].get("provider_session_id") == "ses_test123",
            "second run must reuse OpenCode session",
        )

        # 5. Failure simulation
        fake_opencode.fail_next = True
        failure = main.run("verify", "simulate opencode failure", "test-session-v2", work_dir=work_dir)
        assert_true(not failure["ok"], "OpenCode failure must return error")

        # 6. Job manager lock + result states
        job = main.JOB_MANAGER.create_job("execute", "do async thing", "async-session", work_dir, None)
        assert_true(job["status"] == "pending", "job must start pending")
        duplicate_error = None
        try:
            main.JOB_MANAGER.create_job("execute", "second async thing", "async-session", work_dir, None)
        except ValueError as exc:
            duplicate_error = str(exc)
        assert_true(duplicate_error is not None, "second active job on same session must fail")
        running = main.JOB_MANAGER.mark_running(job["job_id"])
        assert_true(running["status"] == "running", "job must move to running")
        completed = main.JOB_MANAGER.complete_job(job["job_id"], {"ok": True, "content": "done", "meta": {}})
        assert_true(completed["status"] == "completed", "job must complete")
        result = main.get_result(job["job_id"])
        assert_true(result["ok"] and result["status"] == "completed", "result must expose completed output")
        missing = main.get_status("missing-job")
        assert_true(not missing["ok"] and missing["status"] == "not_found", "missing status must be structured")

        old_owner = main.JOB_MANAGER.create_job(
            "analyze", "old", "owner-session", work_dir, None
        )
        main.JOB_MANAGER.complete_job(old_owner["job_id"], {"ok": True})
        new_owner = main.JOB_MANAGER.create_job(
            "analyze", "new", "owner-session", work_dir, None
        )
        main.JOB_MANAGER._release_session_lock(old_owner)
        active_owner = main.JOB_MANAGER.active_job_for_session("owner-session")
        assert_true(
            active_owner and active_owner["job_id"] == new_owner["job_id"],
            "a late job must not release a newer owner's session lock",
        )
        main.JOB_MANAGER.fail_job(new_owner["job_id"], "cleanup")

        # 7. Identical requests attach while alive and recover once after worker death.
        def fake_popen(*args, **kwargs):
            # Only the worker spawn is faked. `main.subprocess.Popen` is the real
            # stdlib attribute, so `subprocess.run` resolves through it too — a
            # blanket fake breaks unrelated POSIX calls (`_process_identity`
            # shelling out to `ps` for a pid with no /proc entry), which is a
            # Linux-only failure Windows never reaches.
            argv = args[0] if args else kwargs.get("args") or []
            argv = [str(a) for a in argv] if isinstance(argv, (list, tuple)) else []
            if "--command" in argv and "worker" in argv:
                return FakeJobProcess(os.getpid())
            return original_popen(*args, **kwargs)

        main.subprocess.Popen = fake_popen
        submitted = main.submit("analyze", "long task", "submit-session", work_dir, None)
        assert_true(submitted["ok"], "submit must succeed")
        assert_true(submitted["status"] == "pending", "submit must return pending")
        attached = main.submit("analyze", "long task", "submit-session", work_dir, None)
        assert_true(
            attached["job_id"] == submitted["job_id"]
            and attached["meta"]["reused"]
            and not attached["meta"]["recovery"],
            "same request with a live worker must attach",
        )
        blocked = main.submit("analyze", "different task", "submit-session", work_dir, None)
        assert_true(
            not blocked["ok"] and blocked["meta"]["error_type"] == "job_already_running",
            "different request on the same locked session must be rejected",
        )
        main.JOB_MANAGER.set_worker_pid(submitted["job_id"], 999999999)
        recovered = main.submit("analyze", "long task", "submit-session", work_dir, None)
        assert_true(
            recovered["ok"]
            and recovered["job_id"] == submitted["job_id"]
            and recovered["meta"]["recovery"],
            "dead worker must restart the same job once",
        )
        main.JOB_MANAGER.set_worker_pid(submitted["job_id"], 999999999)
        exhausted = main.submit("analyze", "long task", "submit-session", work_dir, None)
        assert_true(
            not exhausted["ok"]
            and exhausted["meta"].get("reason") == "recovery_exhausted",
            "a second worker death must fail terminal instead of looping",
        )
        assert_true(
            main.JOB_MANAGER.active_job_for_session("submit-session") is None,
            "recovery exhaustion must release the session lock",
        )

        # 8. A different request frees a session whose worker is gone, instead of being
        # refused until someone runs `clean`. The old job is failed, not recovered.
        orphaned = main.submit("analyze", "long task", "dead-session", work_dir, None)
        assert_true(orphaned["ok"], "submit must succeed")
        main.JOB_MANAGER.set_worker_pid(orphaned["job_id"], 999999999)
        moved_on = main.submit("analyze", "another task", "dead-session", work_dir, None)
        assert_true(
            moved_on["ok"] and moved_on["job_id"] != orphaned["job_id"],
            f"a different request on a session whose worker died must proceed: {moved_on}",
        )
        old = main.JOB_MANAGER.get_job(orphaned["job_id"])
        assert_true(
            old["status"] == "failed" and old.get("reaped"),
            f"the dead worker's job is failed and marked reaped: {old.get('status')}",
        )
        owner = main.JOB_MANAGER.active_job_for_session("dead-session")
        assert_true(
            owner and owner["job_id"] == moved_on["job_id"],
            "the session lock now belongs to the new request",
        )
        # The lock changed hands: a release pinned to the old job must not touch the new
        # owner, even when the new owner's worker is dead too.
        main.JOB_MANAGER.set_worker_pid(moved_on["job_id"], 999999999)
        assert_true(
            not main.JOB_MANAGER.release_dead_session_lock("dead-session", orphaned["job_id"]),
            "a release pinned to a job that no longer holds the lock is refused",
        )
        still = main.JOB_MANAGER.get_job(moved_on["job_id"])
        assert_true(
            still["status"] not in {"failed", "completed"}
            and main.JOB_MANAGER.active_job_for_session("dead-session")["job_id"] == moved_on["job_id"],
            "the current owner is neither failed nor unlocked by a stale release",
        )
        main.JOB_MANAGER.fail_job(moved_on["job_id"], "cleanup")

        queued = main.JOB_MANAGER.create_job(
            "explore", "inspect queued flow", "queued-session", work_dir, None
        )
        assert_true(main.should_run_in_background("explore"), "explore must be marked as background command")
        main.subprocess.Popen = original_popen

        # 8. Worker path updates job state on success
        worker_job = main.JOB_MANAGER.create_job("explore", "inspect async flow", "worker-session", work_dir, None)
        worker_output = main.run_worker(worker_job["job_id"])
        assert_true(worker_output["ok"], "worker must execute queued command")
        worker_status = main.get_status(worker_job["job_id"])
        assert_true(worker_status["status"] == "completed", "worker must persist completed state")

        recovery_session = main.SESSION_MANAGER.load_or_create("recover-worker-session")
        main.SESSION_MANAGER.update_provider_session_id(
            recovery_session, "ses_recover123"
        )
        recovery_job = main.JOB_MANAGER.create_job(
            "explore",
            "finish interrupted mapping",
            "recover-worker-session",
            work_dir,
            None,
        )
        main.JOB_MANAGER.set_worker_pid(recovery_job["job_id"], 999999999)
        main.JOB_MANAGER.mark_running(recovery_job["job_id"])
        recovery_claim = main.JOB_MANAGER.claim_recovery(
            recovery_job["job_id"], stale_after_seconds=0
        )
        main.JOB_MANAGER.release_recovery_claim(recovery_job["job_id"])
        assert_true(
            recovery_claim["action"] == "recover",
            "dead/unstarted job must enter bounded recovery",
        )
        recovery_output = main.run_worker(recovery_job["job_id"])
        assert_true(recovery_output["ok"], "recovery worker must execute")
        assert_true(
            "Continue the interrupted task" in fake_opencode.calls[-1]["prompt"]
            and "finish interrupted mapping" in fake_opencode.calls[-1]["prompt"],
            "recovery prompt must carry job context and original task",
        )
        assert_true(
            fake_opencode.calls[-1]["session"].get("provider_session_id")
            == "ses_recover123",
            "recovery must reuse the captured OpenCode session",
        )

        pre_spawn_job = main.JOB_MANAGER.create_job(
            "explore",
            "start dispatch that lost its submit owner",
            "pre-spawn-orphan-session",
            work_dir,
            None,
        )
        pre_spawn_record = main.JOB_MANAGER.get_job(pre_spawn_job["job_id"])
        pre_spawn_record["reservation_owner_pid"] = 999999999
        pre_spawn_record["reservation_owner_create_time"] = None
        main.JOB_MANAGER._save(pre_spawn_record)
        pre_spawn_claim = main.JOB_MANAGER.claim_recovery(
            pre_spawn_job["job_id"], stale_after_seconds=0
        )
        main.JOB_MANAGER.release_recovery_claim(pre_spawn_job["job_id"])
        claimed_job = pre_spawn_claim.get("job") or {}
        assert_true(
            pre_spawn_claim.get("action") == "recover"
            and claimed_job.get("recovery_attempt") == 0
            and claimed_job.get("recovery_reason") == "pre_spawn_orphan",
            f"pre-spawn orphan must resume initial dispatch without a recovery attempt: {pre_spawn_claim}",
        )
        pre_spawn_output = main.run_worker(pre_spawn_job["job_id"])
        assert_true(
            pre_spawn_output.get("ok")
            and "Continue the interrupted task" not in fake_opencode.calls[-1]["prompt"],
            f"pre-spawn orphan must run the original task without captured provider session: {pre_spawn_output}",
        )

        # 9. check.py status/result payloads
        # A freshly queued job has no worker PID yet and remains attachable.
        pending_status = check._status_payload(queued["job_id"])
        assert_true(
            pending_status["status"] == "pending",
            f"queued job must remain pending before dispatch: {pending_status}",
        )

        # ...and a job whose worker IS alive still reports as running. Use this very
        # process as the stand-in worker: it is guaranteed alive for the assertion.
        live_job = main.JOB_MANAGER.create_job("explore", "live worker", "live-session", work_dir, None)
        main.JOB_MANAGER.set_worker_pid(live_job["job_id"], os.getpid())
        main.JOB_MANAGER.mark_running(live_job["job_id"])
        main.JOB_MANAGER.touch_heartbeat(live_job["job_id"], {"phase": "agent", "idle_seconds": 0})
        live_status = check._status_payload(live_job["job_id"])
        assert_true(
            live_status["status"] == "running" and live_status["done"] is False,
            f"a job with a live worker and a fresh beat must not be reaped: {live_status}",
        )

        complete_job = main.JOB_MANAGER.create_job("execute", "done task", "result-session", work_dir, None)
        main.JOB_MANAGER.complete_job(complete_job["job_id"], {"ok": True, "content": "clean output", "meta": {}})
        result_ok, result_payload = check._result_payload(complete_job["job_id"])
        assert_true(result_ok, "completed job must return output-only payload")
        assert_true(result_payload == "clean output", "completed result must expose cleaned content only")

        failed_job = main.JOB_MANAGER.create_job("execute", "bad task", "failed-session", work_dir, None)
        main.JOB_MANAGER.fail_job(failed_job["job_id"], "boom")
        failed_ok, failed_payload = check._result_payload(failed_job["job_id"])
        assert_true(not failed_ok, "failed job must not return plain output")
        assert_true(failed_payload["status"] == "failed", "failed result must keep failed status")

        missing_status = check._status_payload("missing-job")
        assert_true(missing_status["status"] == "not_found", "missing job must report not_found in check.py")

        # 10. check.py internal wait loop and timeout contract
        wait_job = main.JOB_MANAGER.create_job("execute", "wait task", "wait-session", work_dir, None)

        def complete_later() -> None:
            time.sleep(0.1)
            main.JOB_MANAGER.complete_job(wait_job["job_id"], {"ok": True, "content": "waited output", "meta": {}})

        waiter = threading.Thread(target=complete_later)
        waiter.start()
        wait_ok, wait_payload = check._wait_for_result(wait_job["job_id"], 0.05, 2)
        waiter.join()
        assert_true(wait_ok, "wait loop must eventually return completed result")
        assert_true(wait_payload == "waited output", "wait loop must return cleaned output only on success")

        timeout_job = main.JOB_MANAGER.create_job("execute", "timeout task", "timeout-session", work_dir, None)
        timeout_ok, timeout_payload = check._result_payload(timeout_job["job_id"])
        assert_true(not timeout_ok, "non-wait result lookup must stay incomplete")

        timeout_status = check._wait_for_status(timeout_job["job_id"], 0.05, 1)
        assert_true(timeout_status["status"] == "pending", "timed out status must preserve current job state")
        assert_true(timeout_status.get("timed_out") is True, "timed out wait must mark timed_out")

        # A worker killed mid-flight leaves the record "running" and recoverable. Both
        # attach loops must still finish on it instead of polling until timeout.
        dead_attach = main.JOB_MANAGER.create_job(
            "explore", "dead attach", "dead-attach-session", work_dir, None
        )
        main.JOB_MANAGER.set_worker_pid(dead_attach["job_id"], 999999999)
        main.JOB_MANAGER.mark_running(dead_attach["job_id"])
        dead_result_ok, dead_result_payload = check._result_payload(dead_attach["job_id"])
        assert_true(
            not dead_result_ok
            and dead_result_payload.get("error_type") == "worker_died"
            and dead_result_payload.get("done") is True
            and dead_result_payload.get("next_action"),
            f"result payload must mark a dead worker terminal and relay next_action: {dead_result_payload}",
        )
        dead_wait_ok, dead_wait_payload = check._wait_for_result(dead_attach["job_id"], 0.05, 5)
        assert_true(
            not dead_wait_ok and dead_wait_payload.get("timed_out") is not True,
            f"result wait must exit on a dead worker instead of timing out: {dead_wait_payload}",
        )
        dead_wait_status = check._wait_for_status(dead_attach["job_id"], 0.05, 5)
        assert_true(
            dead_wait_status.get("done") is True
            and dead_wait_status.get("timed_out") is not True,
            f"status wait must exit on a dead worker instead of timing out: {dead_wait_status}",
        )
        main.JOB_MANAGER.fail_job(dead_attach["job_id"], "test cleanup")

        # 11. Structured errors, idempotency, reaper, digest, guard, router
        from exoma.core.evidence.contract import extract_digest, make_error, validate_verification_contract
        from exoma.core.prompt.router import Router
        from exoma.utils import osutil, path_guard

        # next_action mandatory
        enforced = False
        try:
            make_error("permission_denied", "x", next_action="")
        except ValueError:
            enforced = True
        assert_true(enforced, "make_error must require next_action")

        # role code-authoritative; local command rejected from delegation
        assert_true(Router().route("explore")["role"] == "exploration", "router derives role from code")
        execute_rejected = False
        try:
            Router().route("execute")
        except ValueError:
            execute_rejected = True
        assert_true(execute_rejected, "execute must not be delegable")

        # idempotency: identical request reuses the same job
        idem_a = main.JOB_MANAGER.create_job("explore", "same task", "idem-session", work_dir, None)
        idem_b = main.JOB_MANAGER.create_job("explore", "same task", "idem-session", work_dir, None)
        assert_true(idem_a["job_id"] == idem_b["job_id"], "identical request must reuse job")
        main.JOB_MANAGER.complete_job(idem_a["job_id"], {"ok": True, "content": "x", "meta": {}})

        # A first dead worker remains locked and advertises one bounded recovery.
        dead = main.JOB_MANAGER.create_job("explore", "dead worker", "reaper-session", work_dir, None)
        main.JOB_MANAGER.set_worker_pid(dead["job_id"], 999999999)
        main.JOB_MANAGER.mark_running(dead["job_id"])
        reaped = main.get_result(dead["job_id"])
        assert_true(
            reaped["status"] == "running"
            and reaped["meta"].get("recoverable") is True,
            "first dead worker must stay recoverable instead of releasing its lock",
        )
        main.JOB_MANAGER.fail_job(dead["job_id"], "test cleanup")

        # digest extraction + fallback
        digest = extract_digest("findings:\n- a\n[DIGEST]\nsummary: s\nkey_findings:\n- k\nrisk_level: high\nrecommended_next_action: go\nconfidence: low")
        assert_true(digest and digest["summary"] == "s" and digest["risk_level"] == "high", "digest must parse")
        assert_true(extract_digest("no digest here") is None, "missing digest must fall back to None")

        valid_verify = """[VERIFICATION]
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
confidence: high — all requested checks ran
"""
        assert_true(
            validate_verification_contract(valid_verify)["verdict"] == "pass",
            "well-formed complete verification must pass",
        )
        incomplete_verify = valid_verify.replace(
            "- python -B tests/run.py: pass", "- none"
        )
        assert_true(
            validate_verification_contract(incomplete_verify)["verdict"] == "incomplete",
            "verification without an executed check must be incomplete",
        )
        blocking_verify = valid_verify.replace(
            "- none\nescalations:",
            "- severity: high | origin: introduced | scope_relation: in_scope\n"
            "  problem: broken [a.py:1]\nescalations:",
            1,
        )
        assert_true(
            validate_verification_contract(blocking_verify)["verdict"] == "fail",
            "a blocking finding must fail even when the declared verdict says DONE",
        )
        misrouted_verify = valid_verify.replace(
            "notes:\n- none",
            "notes:\n- severity: high | origin: introduced | "
            "scope_relation: in_scope — broken [a.py:1]",
        )
        misrouted_assessment = validate_verification_contract(misrouted_verify)
        assert_true(
            misrouted_assessment["verdict"] == "fail"
            and any(
                item.get("kind") == "finding_misrouted"
                for item in misrouted_assessment["warnings"]
            ),
            f"a blocking-class finding cannot pass from notes: {misrouted_assessment}",
        )
        assert_true(
            main._verify_exit_code(
                "await", {"ok": True, "meta": {"verdict": "fail"}}, "verify"
            )
            == 2,
            "await must propagate a failed verify exit code",
        )
        assert_true(
            main._verify_exit_code(
                "await", {"ok": True, "meta": {"verdict": "incomplete"}}, "verify"
            )
            == 2,
            "await must propagate an incomplete verify exit code",
        )
        assert_true(
            main._verify_exit_code(
                "await", {"ok": True, "meta": {"verdict": "pass"}}, "verify"
            )
            == 0,
            "await must preserve a clean verify exit code",
        )
        assert_true(
            main._verify_exit_code(
                "result",
                {"ok": True, "status": "completed", "output": {"ok": True, "meta": {"verdict": "fail"}}},
                "verify",
            )
            == 2
            and main._verify_exit_code(
                "result",
                {"ok": True, "status": "completed", "output": {"ok": True, "meta": {"verdict": "pass"}}},
                "verify",
            )
            == 0,
            "result must derive verify exit status from the stored output",
        )
        # An agent that names what it could not reach earns `verification_gap`, which keeps
        # the verdict off `pass` — that part is deliberate. What must NOT follow is the CLI
        # calling an honest, complete report a failed run.
        gap_verify = valid_verify.replace(
            "not_verified:\n- none",
            "not_verified:\n- hooks/*.sh never executed (no shell in the read boundary)",
        )
        gap_assessment = validate_verification_contract(gap_verify)
        assert_true(
            gap_assessment["verdict"] == "incomplete"
            and {item.get("kind") for item in gap_assessment["warnings"]}
            == {"verification_gap"},
            f"a declared gap must stay incomplete, and be the only warning: {gap_assessment}",
        )
        assert_true(
            main._verify_exit_code(
                "await",
                {
                    "ok": True,
                    "meta": {
                        "verdict": gap_assessment["verdict"],
                        "verify_contract": gap_assessment,
                    },
                },
                "verify",
            )
            == 0,
            "an incomplete whose only warning is a declared gap must not exit nonzero",
        )
        broken_assessment = validate_verification_contract(
            valid_verify.replace("confidence: high — all requested checks ran", "")
        )
        assert_true(
            main._verify_exit_code(
                "await",
                {
                    "ok": True,
                    "meta": {
                        "verdict": broken_assessment["verdict"],
                        "verify_contract": broken_assessment,
                    },
                },
                "verify",
            )
            == 2,
            f"an incomplete from a damaged contract must still exit nonzero: {broken_assessment}",
        )

        # Secret access is denied on the TOOL CALL, not by scanning the task text. The
        # text scan blocked an audit for naming the files it audited while leaving the
        # files themselves reachable, so the rules now ship as opencode permissions.
        assert_true(
            not hasattr(path_guard, "validate_scope"),
            "the task-text scope guard must stay removed; enforcement is opencode's",
        )
        project_policy = json.loads(
            (
                REPO_ROOT
                / "src"
                / "exoma"
                / "_bundle"
                / "config"
                / "opencode"
                / "opencode.project.json"
            ).read_text(encoding="utf-8")
        )
        policy_read = project_policy["permission"]["read"]
        policy_grep = project_policy["permission"]["grep"]
        for pattern in ("*.env", "*.env.*", "*id_rsa*", "*.pem", "*.key", "*.ssh/*"):
            assert_true(
                policy_read.get(pattern) == "deny",
                f"project policy must deny reading {pattern}",
            )
        assert_true(
            policy_grep.get("*.env") == "deny",
            "grep must be denied too: it returns file CONTENTS, so read-only denial alone leaks",
        )
        # Root-level, not only on agent.plan: the plan agent's deny covers the default
        # launch, but AI_PROXY_OPENCODE_AGENT can name another agent, and path_guard counts
        # this key as part of the boundary it no longer checks itself.
        assert_true(
            project_policy["permission"].get("external_directory") == "deny",
            "project policy must deny out-of-root access for every agent, not just plan",
        )
        plan_permission = json.loads(
            (REPO_ROOT / "src" / "exoma" / "_bundle" / "config" / "opencode" / "opencode.template.json").read_text(
                encoding="utf-8"
            )
        )["agent"]["plan"]["permission"]
        assert_true(
            plan_permission.get("external_directory") == "deny",
            "the plan agent's own out-of-root deny must stay beside the root one",
        )
        assert_true(
            policy_read.get("*.env.example") == "allow",
            "example env files carry no secret and must stay readable",
        )
        assert_true(
            list(policy_read).index("*.env") < list(policy_read).index("*.env.example"),
            "the allow exception must come after the deny it narrows; order decides the winner",
        )
        assert_true(
            not any(p in policy_read for p in ("*secret*", "*credential*")),
            "word-shaped patterns are what made the old guard block ordinary source files",
        )

        # cross-OS primitive
        assert_true(osutil.process_alive(999999999) is False, "process_alive must report dead pid")

        # invalid_evidence: menu/refusal (ok:true but no evidence) must be rejected as proxy failure
        class MenuAdapter:
            command = "opencode"
            timeout_seconds = 0
            no_timeout = True

            def run(self, prompt, session, model=None, work_dir=None):
                return {"ok": True, "content": "Specify command: explore, plan, analyze, verify, sweep, doctor.", "meta": {"provider_session_id": "ses_menu"}}

        menu_exec = Executor(adapter=MenuAdapter(), session_manager=main.SESSION_MANAGER)
        menu_session = main.SESSION_MANAGER.load_or_create("menu-session")
        menu_res = menu_exec.execute("analyze", "do analysis", menu_session, work_dir)
        assert_true(not menu_res["ok"], "menu/refusal response must be rejected, not treated as success")
        assert_true(menu_res["meta"]["error_type"] == "invalid_evidence", "non-evidence must flag invalid_evidence")

        # rich error (error_type + next_action) must survive the job path, not collapse to a string
        rich_job = main.JOB_MANAGER.create_job("analyze", "rich err", "rich-session", work_dir, None)
        main.JOB_MANAGER.fail_job(
            rich_job["job_id"],
            "non-evidence",
            output={"ok": False, "content": "non-evidence", "meta": {"error_type": "invalid_evidence", "next_action": "STOP, ask user"}},
        )
        rich_res = main.get_result(rich_job["job_id"])
        assert_true(rich_res["meta"].get("error_type") == "invalid_evidence", "job path must preserve error_type")
        assert_true(rich_res["meta"].get("next_action") == "STOP, ask user", "job path must preserve next_action")

        # 12. Liveness tri-state, heartbeat, runtime ceiling, probe
        from exoma.core.evidence import fact_store
        from exoma.core.graph import graph_index
        from exoma.core.jobs import job_manager as jm_mod

        # Stall detection and the runtime ceiling are separate managers on purpose: the
        # ceiling outranks stall in get_result (a hard backstop must win over a probe
        # hint), so sharing one tiny threshold would mask the stall path entirely.
        watchdog = JobManager(
            temp_root / "jobs", stall_threshold_seconds=1, max_runtime_seconds=3600
        )
        live_job = watchdog.create_job("explore", "watchdog", "watchdog-session", work_dir, None)
        job_id = live_job["job_id"]

        assert_true(
            watchdog.liveness(watchdog.get_job(job_id)) is None,
            "a job with no worker pid yet must not be classified (nothing to reap)",
        )
        watchdog.set_worker_pid(job_id, 999999999)
        assert_true(
            watchdog.liveness(watchdog.get_job(job_id)) == jm_mod.DEAD,
            "a gone pid must classify as dead",
        )

        # Live pid + fresh heartbeat = progressing; the SAME pid with a stale heartbeat
        # must classify as stalled. That difference is the whole point of the heartbeat:
        # pid liveness alone reports both cases identically.
        watchdog.mark_running(job_id)
        watchdog.set_worker_pid(job_id, os.getpid())
        watchdog.touch_heartbeat(job_id, {"phase": "agent", "elapsed_seconds": 3})
        beat = watchdog.read_heartbeat(job_id)
        assert_true(
            beat and beat.get("at") and beat["progress"]["phase"] == "agent",
            "heartbeat must record its timestamp and progress payload",
        )
        beating = watchdog.get_job(job_id)
        assert_true(
            watchdog.liveness(beating) == jm_mod.ALIVE_PROGRESSING,
            "live pid with a fresh heartbeat must be progressing",
        )
        time.sleep(1.2)  # exceed the 1s stall threshold configured above
        assert_true(
            watchdog.liveness(watchdog.get_job(job_id)) == jm_mod.ALIVE_STALLED,
            "live pid with a stale heartbeat must be stalled, not dead",
        )

        stalled = watchdog.get_result(job_id)
        assert_true(
            stalled["meta"].get("error_type") == "worker_stalled",
            "a stalled worker must be reported as stalled",
        )
        assert_true(
            watchdog.get_job(job_id)["status"] == "running",
            "a stalled worker must NOT be reaped on suspicion — its work may still land",
        )

        probed = watchdog.record_probe(job_id, {"alive": False, "reason": "probe_timeout"})
        assert_true(
            probed["liveness"] == "stalled_on_limit",
            "a probe that cannot reach opencode means rate/usage limit, not a hang",
        )
        assert_true(
            watchdog.read_probe(job_id)["liveness"] == "stalled_on_limit",
            "the probe verdict must survive a round-trip through its side file",
        )
        assert_true(
            watchdog.record_probe(job_id, {"alive": True, "reason": "probe_ok"})["liveness"]
            == "stalled_no_progress",
            "a probe that answers means opencode is healthy and this session is hung",
        )

        # A late beat must NOT resurrect a job another process already ended. Heartbeats
        # arrive every couple of seconds and get_result runs in a different process, so
        # folding the beat into the job record made this a routine collision, not a rare one.
        raced = watchdog.create_job("explore", "race", "race-session", work_dir, None)
        watchdog.set_worker_pid(raced["job_id"], os.getpid())
        watchdog.mark_running(raced["job_id"])
        watchdog.fail_job(raced["job_id"], "worker process died before completing (reaped)")
        watchdog.touch_heartbeat(raced["job_id"], {"phase": "agent"})  # beat arrives late
        settled = watchdog.get_job(raced["job_id"])
        assert_true(
            settled["status"] == "failed" and settled["error"],
            "a late heartbeat must never revert a terminal job or erase its error",
        )

        # Runtime ceiling: the OOM backstop, where the pid can look alive but the job is lost.
        ceiling = JobManager(
            temp_root / "jobs", stall_threshold_seconds=3600, max_runtime_seconds=1
        )
        expired = ceiling.create_job("plan", "expired", "expired-session", work_dir, None)
        # os.getpid() is a guaranteed-alive pid so the ceiling takes the job_expired branch
        # (a dead pid would classify as worker_died instead). _kill_worker's self-pid guard
        # skips the actual kill, so reaping this job cannot take the test process down.
        ceiling.set_worker_pid(expired["job_id"], os.getpid())
        ceiling.mark_running(expired["job_id"])
        ceiling.touch_heartbeat(expired["job_id"], {"phase": "agent"})
        time.sleep(1.2)
        expired_res = ceiling.get_result(expired["job_id"])
        assert_true(
            expired_res["meta"].get("error_type") == "job_expired",
            "a job past the runtime ceiling must fail as expired, distinct from worker_died",
        )

        # Tolerant fact parsing preserves blank lines, nested bullets, and continuations.
        messy = (
            "grounded:\n\n- claim A [main.py:1]\n  * nested detail\n"
            "- claim B [core/x.py:2]\n  wrapped tail\n\nassumptions:\n- guess\n"
        )
        grounded = fact_store._parse_block(messy, "grounded")
        assert_true(len(grounded) == 3, f"tolerant parser must keep blank/nested bullets, got {grounded}")
        assert_true(
            "wrapped tail" in grounded[-1],
            "a wrapped continuation line must join its bullet, not be dropped",
        )
        assert_true(
            fact_store._parse_block(messy, "assumptions") == ["guess"],
            "the next section header must end the block",
        )
        assert_true(
            fact_store._parse_block("grounded:\n- a [x.py:1]\n- b [y.py:2]\nassumptions:\n- c\n", "grounded")
            == ["a [x.py:1]", "b [y.py:2]"],
            "the pre-3.4.0 flat format must keep parsing identically",
        )
        # A top-level `key: value` line ends the current block.
        assert_true(
            fact_store._parse_block(
                "dependents:\n- calls X [a.py:1]\nexternal: none (no external libs)\n", "dependents"
            )
            == ["calls X [a.py:1]"],
            "a `key: value` line at column 0 must end the section, not extend the last bullet",
        )
        assert_true(
            fact_store._parse_block("grounded:\n- claim [a.py:1]\nconfidence: high\n", "grounded")
            == ["claim [a.py:1]"],
            "a trailing confidence line must not be absorbed into a claim",
        )

        # Graph leads: absent graph degrades to None, never an exception.
        assert_true(
            graph_index.leads(temp_root, "anything") is None,
            "a project without graphify-out must yield no leads instead of failing",
        )
        assert_true(
            graph_index.load_graph(temp_root) is None,
            "a missing graph.json must return None, not raise",
        )

        with tempfile.TemporaryDirectory(prefix="graph-cache-") as graph_tmp:
            graph_root = Path(graph_tmp)
            source = graph_root / "a.py"
            removed_source = graph_root / "b.py"
            source.write_text("value = 1\n", encoding="utf-8")
            removed_source.write_text("other = 2\n", encoding="utf-8")
            graph_file = graph_root / "graphify-out" / "graph.json"
            graph_file.parent.mkdir(parents=True)
            graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
            base_ns = time.time_ns()
            os.utime(source, ns=(base_ns, base_ns))
            os.utime(removed_source, ns=(base_ns, base_ns))
            os.utime(graph_file, ns=(base_ns + 10_000_000, base_ns + 10_000_000))
            graph_index._STALE_CACHE.clear()
            assert_true(graph_index.is_stale(graph_root) is False, "new graph must be fresh")
            removed_source.unlink()
            graph_index._STALE_CACHE.clear()
            assert_true(
                graph_index.is_stale(graph_root) is True,
                "deleting a source must make an unchanged graph stale",
            )
            removed_source.write_text("other = 2\n", encoding="utf-8")
            os.utime(removed_source, ns=(base_ns, base_ns))
            os.utime(graph_file, ns=(base_ns + 30_000_000, base_ns + 30_000_000))
            graph_index._STALE_CACHE.clear()
            assert_true(graph_index.is_stale(graph_root) is False, "refreshed graph must be fresh")
            os.utime(source, ns=(base_ns + 40_000_000, base_ns + 40_000_000))
            graph_index._STALE_CACHE.clear()
            assert_true(
                graph_index.is_stale(graph_root) is True,
                "disk cache must invalidate when a source changes",
            )
        repo_root = REPO_ROOT
        repo_leads = graph_index.leads(repo_root, "session manager")
        # Guarded on the graph EXISTING, not on leads being truthy: keying the guard on
        # the result itself would let a leads() that silently returns None skip its own
        # assertions and still report success.
        assert_true(
            (graph_index.graph_path(repo_root).exists()) == (repo_leads is not None),
            "leads must be produced whenever graph.json exists, and only then",
        )
        if repo_leads:
            assert_true(
                all("\\" not in row["file"] for row in repo_leads["files"]),
                "lead paths must be repo-relative POSIX so they mean the same on any machine",
            )
            # Leads travel through a sidecar so the prompt only needs a bounded anchor.
            leads_prompt = build_prompt(
                role="reasoning",
                task="session manager",
                session_id="leads",
                command="plan",
                project_root=str(repo_root),
                runtime_dir=str(repo_root / ".workflow" / "sessions" / "leads" / "runtime"),
                has_leads=True,
            )
            assert_true("[EVIDENCE_SIDECARS" in leads_prompt, "prompt must anchor to the leads sidecar")
            assert_true("leads.json" in leads_prompt, "leads must reach the agent via the sidecar file")
            assert_true(
                "WEAK hints" in leads_prompt,
                "leads must be framed as starting points, never as findings",
            )

        # Delegated runs have a finite default timeout.
        default_adapter = OpenCodeAdapter()
        assert_true(
            not default_adapter.no_timeout and default_adapter.timeout_seconds > 0,
            "the default adapter must carry a real timeout",
        )
        assert_true(
            default_adapter.bootstrap_timeout_seconds > 0
            and default_adapter.bootstrap_timeout_seconds < default_adapter.timeout_seconds,
            "bootstrap must have its own, shorter budget than a full task",
        )
        # Side files must not outlive their job — nothing rewrites them once it ends.
        pruned_id = raced["job_id"]
        assert_true(
            watchdog._beat_path(pruned_id).exists(),
            "precondition: the raced job still has its beat file",
        )
        watchdog.log_path(pruned_id).write_text("worker log", encoding="utf-8")
        watchdog.prune_jobs(ttl_days=0, keep_last=0)
        assert_true(
            not watchdog._beat_path(pruned_id).exists()
            and not watchdog._probe_path(pruned_id).exists()
            and not watchdog.log_path(pruned_id).exists(),
            "pruning a job must take heartbeat, probe, and worker log side files",
        )

        # 13. Sub-agent fan-out: instruction gating and honest usage detection
        from exoma.core.evidence.contract import detect_subagent_usage
        from exoma.core.runtime.config_defaults import subagent_fanout_enabled

        def _p(has_leads, fanout):
            return build_prompt(
                role="exploration", task="t", session_id="s", command="explore",
                project_root=str(temp_root),
                runtime_dir=str(temp_root / ".workflow" / "sessions" / "s" / "runtime"),
                has_leads=has_leads, subagent_fanout=fanout,
            )

        # The prompt carries only the sidecar anchor; AGENTS.md owns the fan-out rules.
        fan = _p(True, True)
        assert_true("FAN-OUT call" in fan, "a fan-out call must be flagged in the prompt")
        assert_true("leads.json" in fan, "fan-out must point the agent at the leads sidecar")
        assert_true(
            "subagents:" in fan,
            "the output format must ask which clusters were dispatched",
        )
        assert_true(
            "SUBAGENT_PLAN" not in fan and "no dependency graph is available" not in fan,
            "the fan-out plan body must live in AGENTS.md, not inline in the prompt",
        )
        assert_true(
            "FAN-OUT call" not in _p(True, False),
            "fan-out must stay off when the policy is off",
        )

        # Detection needs BOTH signals to agree — a declaration alone is a claim of work,
        # not evidence of it.
        real = "subagents: c1, c2\ngrounded:\n- [c1] X [a.py:1]\n- [c2] Y [b.py:2]\n"
        got = detect_subagent_usage(real)
        assert_true(
            got["used"] and got["fanout_clusters"] == ["c1", "c2"],
            f"real fan-out must register: {got}",
        )
        assert_true(not got["mismatch"], "matching declaration and tags must not warn")

        disjoint = "subagents: c1\ngrounded:\n- [c2] X [a.py:1]\n"
        got = detect_subagent_usage(disjoint)
        assert_true(
            not got["used"] and got["mismatch"],
            f"disjoint declaration and coverage must be rejected: {got}",
        )
        assert_true(
            got["fanout_clusters"] == [] and got["covered_clusters"] == ["c2"],
            f"disjoint coverage must remain visible without proving fan-out: {got}",
        )

        one_empty = "subagents: c1, c2\ngrounded:\n- [c1] X [a.py:1]\n"
        got = detect_subagent_usage(one_empty)
        assert_true(
            got["used"] and got["fanout_clusters"] == ["c1", "c2"],
            f"a dispatched empty slice must not invalidate confirmed fan-out: {got}",
        )

        lying = "subagents: c1, c2\ngrounded:\n- X [a.py:1]\n"
        got = detect_subagent_usage(lying)
        assert_true(
            not got["used"] and got["mismatch"] and got["fanout_clusters"] == [],
            "a declared fan-out with no tagged claims must be flagged, not counted as success",
        )

        # Tagged cluster coverage alone is not proof that fan-out occurred.
        honest = (
            "subagents: none (no spawn tool; tools: read, grep)\n"
            "grounded:\n- [c1] X [a.py:1]\n- [c3] Y [b.py:2]\n"
        )
        got = detect_subagent_usage(honest)
        assert_true(
            not got["used"] and not got["mismatch"],
            "an honest 'no spawn tool' answer must be neither used nor a mismatch",
        )
        assert_true(
            got["fanout_clusters"] == [] and got["covered_clusters"] == ["c1", "c3"],
            f"tagged-but-not-fanned-out must report coverage, never fan-out: {got}",
        )
        assert_true(
            not detect_subagent_usage("grounded:\n- X [a.py:1]\n")["used"],
            "output with no subagents line must not register as fan-out",
        )

        # Fan-out defaults to on, including when config is unreadable.
        assert_true(
            subagent_fanout_enabled(temp_root) is True,
            "fan-out must default to on",
        )
        assert_true(
            subagent_fanout_enabled(temp_root / "does-not-exist") is True,
            "an unreadable config must fall back to the default, not to off",
        )

        # Fresh init uses the example config; null delegates model selection to OpenCode.
        import json as _json

        example = (
            REPO_ROOT / "src" / "exoma" / "config" / "second_agent.example.json"
        )
        shipped = _json.loads(example.read_text(encoding="utf-8"))
        models = [shipped.get("default_model")] + [
            r.get("model") for r in (shipped.get("routes") or {}).values()
        ]
        assert_true(
            all(m is None for m in models),
            f"shipped example must not carry placeholder models: {models}",
        )
        assert_true(
            shipped.get("timeout_seconds", 0) > 0,
            "shipped example must carry a real timeout, not an unbounded wait",
        )

        calls_before_sweep = len(fake_opencode.calls)
        local_sweep = main.run("sweep", "", "local-sweep", work_dir)
        assert_true(local_sweep["ok"], f"local sweep must return a report: {local_sweep}")
        assert_true(
            len(fake_opencode.calls) == calls_before_sweep,
            "sweep must not consume an OpenCode call",
        )

        # JobManager still accepts an empty task for compatibility with stored sweep jobs.
        taskless = main.JOB_MANAGER.create_job("sweep", None, "taskless-session", work_dir, None)
        assert_true(
            taskless["task"] == "" and taskless["request_hash"],
            "a job with no task must be created normally, not crash",
        )
        main.JOB_MANAGER.fail_job(taskless["job_id"], "cleanup")

        _test_facts_concurrency()
        _test_anchor_relocation()
        _test_fact_selection()
        _test_evidence_anchor_relocation()
        _test_evidence_reuse()
        _test_evidence_lock_file()
        _test_runtime_guard_cleanup()
        _test_process_generation_outcomes()
        _test_worker_kill_is_fail_closed()
        _test_byte_lock_excludes_handles_and_processes()
        _test_task_telemetry()
        _test_redaction_boundary()
        _test_quick_verify_gaps()
        _test_verification_routing()
        _test_empty_section_is_not_a_finding()
        _test_cli_script_reaches_its_exit_code()
        _test_e2e_spec_contract()
        _test_e2e_classification_and_verdicts()
        _test_e2e_network()
        _test_e2e_supervisor()
        _test_e2e_routing()
        _test_e2e_browser_session()
        _test_e2e_redaction_variants()
        _test_e2e_doctor_readiness()
        _test_e2e_existing_tests()
        _test_e2e_tagging()
        _test_e2e_hardening()
        _test_e2e_knowledge()
        _test_e2e_metrics()
        _test_e2e_harness_names_its_model()
        _test_e2e_real_browser_smoke()
        _test_submit_admission()
        _test_workspace_release_guards()
        _test_project_session_isolation()
        _test_init_upgrade_and_session_guard()
        _test_script_drift_path_spelling()
        _test_contract_continuation()
        _test_continuation_prompt_is_bounded()
        _test_workflow_contracts()
        _test_promote_boundaries()
        _test_execution_chain()
        _test_installer_text_merging()
        _test_installer_settings_merge()
        _test_installer_settings_are_additive()
        _test_installer_rollback_receipt()
        _test_installer_drift_check()
        _test_installer_seeds_second_agent_config()
        _test_installer_seed_is_non_interactive_without_a_tty()
        _test_installer_seed_prompt_answers()
        _test_installer_seed_reports_unenforced_provider()
        _test_installer_check_reports_second_agent_default()
        _test_provider_update()
        _test_installer_e2e_deps_are_opt_in()
        _test_runtime_is_stdlib_only()
        _test_every_spawn_hides_its_console()
        _test_telemetry_metrics()
        _test_usage_token_accounting()
        _test_transcript_parsing()
        _test_governance_controls()
        _test_graph_verification()
        _test_graph_refresh_hook()
        _test_hardening()

        print("tests/scenario: success")
    finally:
        main.subprocess.Popen = original_popen


if __name__ == "__main__":
    run_tests()
