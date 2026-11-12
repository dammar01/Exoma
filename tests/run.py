"""Test entry point.

    python tests/run.py                    # everything, same order as before
    python tests/run.py --list             # what can be run on its own
    python tests/run.py --only continuation --only jobs
    python tests/run.py --keep-going       # report every failure, not just the first

Why a registry rather than a bare script: the scenario suite is one stateful sequence —
later assertions read workspaces earlier ones built — so a failure partway through used to
hide everything behind it, and there was no way to re-run just the part being debugged.
The standalone checks each build their own temp workspace, so they genuinely can run alone,
and those are what this exposes. `scenario` stays whole because splitting it would mean
inventing independence it does not have.
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

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

from tests.checks.adapters import (  # noqa: E402
    _test_adapter_error_normalization,
    _test_stdin_failure_reaches_call_meta,
    _test_adapter_redaction_is_shared,
    _test_error_types_are_registered,
)
from tests.checks.audit import (  # noqa: E402
    _test_audit_is_not_telemetry,
    _test_audit_report,
    _test_audit_survives_a_torn_row,
)
from tests.checks.prompt import (  # noqa: E402
    _test_permitted_tools_line,
    _test_prompt_contract_blocks,
    _test_task_cap_follows_the_provider_transport,
    _test_task_cap_is_visible_in_and_out_of_band,
    _test_unknown_role_is_refused,
    _test_verify_branch_carries_routing_contract,
)
from tests.checks.host_profiles import (  # noqa: E402
    _test_agy_precision_read_slice,
    _test_codex_grant,
    _test_contract_renders_per_host,
    _test_host_sections_whitespace,
    _test_read_limit_from_host_keys,
    _test_rule_registry_parity,
    _test_runner_call_single_source,
)
from tests.checks.bundle_sync import (  # noqa: E402
    _test_bundle_registry_bijection,
    _test_every_shipped_hook_is_a_launcher_and_its_module,
)
from tests.checks.continuation import (  # noqa: E402
    _test_contract_continuation,
    _test_continuation_prompt_is_bounded,
)
from tests.checks.cli import _test_cli_script_reaches_its_exit_code  # noqa: E402
from tests.checks.self_update import _test_self_update  # noqa: E402
from tests.checks.contracts import _test_workflow_contracts  # noqa: E402
from tests.checks.promote import _test_promote_boundaries  # noqa: E402
from tests.checks.knowledge_retrieve import _test_knowledge_retrieval  # noqa: E402
from tests.checks.execution import _test_execution_chain  # noqa: E402
from tests.checks.codex_auto_review import check_codex_auto_review  # noqa: E402
from tests.checks.e2e_browser import _test_e2e_browser_session  # noqa: E402
from tests.checks.e2e_doctor import _test_e2e_doctor_readiness  # noqa: E402
from tests.checks.e2e_existing_tests import _test_e2e_existing_tests  # noqa: E402
from tests.checks.e2e_metrics import _test_e2e_metrics  # noqa: E402
from tests.checks.e2e_harness import _test_e2e_harness_names_its_model  # noqa: E402
from tests.checks.e2e_normalize import _test_e2e_classification_and_verdicts  # noqa: E402
from tests.checks.e2e_network import _test_e2e_network  # noqa: E402
from tests.checks.e2e_redact import _test_e2e_redaction_variants  # noqa: E402
from tests.checks.e2e_smoke import _test_e2e_real_browser_smoke  # noqa: E402
from tests.checks.e2e_routing import _test_e2e_routing  # noqa: E402
from tests.checks.e2e_spec import _test_e2e_spec_contract  # noqa: E402
from tests.checks.e2e_supervisor import _test_e2e_supervisor  # noqa: E402
from tests.checks.e2e_tagging import _test_e2e_tagging  # noqa: E402
from tests.checks.e2e_hardening import _test_e2e_hardening  # noqa: E402
from tests.checks.e2e_knowledge import _test_e2e_knowledge  # noqa: E402
from tests.checks.deps import _test_runtime_is_stdlib_only  # noqa: E402
from tests.checks.hidden_spawn import _test_every_spawn_hides_its_console  # noqa: E402
from tests.checks.governance import _test_governance_controls  # noqa: E402
from tests.checks.graph_refresh import _test_graph_refresh_hook  # noqa: E402
from tests.checks.graph_verification import _test_graph_verification  # noqa: E402
from tests.checks.hardening import _test_hardening  # noqa: E402
from tests.checks.telemetry import _test_telemetry_metrics  # noqa: E402
from tests.checks.runtime_guards import _test_runtime_guard_cleanup  # noqa: E402
from tests.checks.process_identity import (  # noqa: E402
    _test_byte_lock_excludes_handles_and_processes,
    _test_process_generation_outcomes,
    _test_worker_kill_is_fail_closed,
)
from tests.checks.task_telemetry import _test_task_telemetry  # noqa: E402
from tests.checks.transcript import _test_transcript_parsing  # noqa: E402
from tests.checks.facts import (  # noqa: E402
    _test_anchor_relocation,
    _test_evidence_anchor_relocation,
    _test_evidence_lock_file,
    _test_evidence_reuse,
    _test_fact_selection,
    _test_facts_concurrency,
)
from tests.checks.installer import (  # noqa: E402
    _test_installer_check_reports_second_agent_default,
    _test_installer_drift_check,
    _test_installer_e2e_deps_are_opt_in,
    _test_install_method_detection,
    _test_installer_rollback_receipt,
    _test_installer_seed_is_non_interactive_without_a_tty,
    _test_installer_seed_prompt_answers,
    _test_installer_asks_every_choice,
    _test_installer_seed_reports_unenforced_provider,
    _test_installer_seeds_second_agent_config,
    _test_installer_settings_are_additive,
    _test_installer_settings_merge,
    _test_installer_text_merging,
    _test_provider_update,
)
from tests.checks.jobs import _test_job_store_not_writable, _test_submit_admission, _test_worker_log_preservation  # noqa: E402
from tests.checks.messages import _test_no_code_in_messages  # noqa: E402
from tests.checks.provider import (  # noqa: E402
    _test_agy_guard_gitlinks,
    _test_agy_provider,
    _test_doctor_read_boundary_warning,
    _test_secret_files,
    _test_provider_seam,
    _test_provider_selection,
)
from tests.checks.provider_sessions import _test_provider_threads_are_kept_per_provider  # noqa: E402
from tests.checks.isolation import _test_suite_ignores_the_machine_seed  # noqa: E402
from tests.checks.installer_uninstall import _test_installer_uninstall  # noqa: E402
from tests.checks.intent_gate import (  # noqa: E402
    _test_intent_gate_reads_the_prompt_field,
    _test_intent_gate_runner_parsed,
    _test_intent_gate_runner_tamper,
    _test_intent_gate_non_ascii_root,
)
from tests.checks.hook_contract import (  # noqa: E402
    _test_gate_k5_fixtures,
    _test_hook_launchers_resolve_python,
    _test_hooks_run_through_their_launchers,
)
from tests.checks.agy_host import (  # noqa: E402
    _test_agy_host_contract,
    _test_agy_host_falls_back_to_a_plain_interpreter,
    _test_doctor_reports_a_refused_agy_host_once,
    _test_agy_host_install_all_or_nothing,
    _test_agy_host_install_merges,
    _test_agy_host_selection,
    _test_bundle_layout_and_agy_agent,
    _test_agy_permissions_and_doctor,
    _test_agy_host_uninstall,
)
from tests.checks.codex_host import _test_codex_host_contract, _test_codex_host_install  # noqa: E402
from tests.checks.v1_surfaces import (  # noqa: E402
    _test_claude_second_agent,
    _test_e2e_stays_opt_in,
    _test_opencode_host_contract,
    _test_opencode_host_install,
    _test_per_host_seed,
    _test_session_export,
)
from tests.checks.same_provider import (  # noqa: E402
    _test_recursion_guard,
    _test_same_provider_child_isolation,
    _test_same_provider_export_picks_the_host,
)
from tests.checks.os_resolvers import (  # noqa: E402
    _test_doctor_python_check_follows_the_launchers,
    _test_entry_points_refuse_an_old_python,
    _test_find_bash_and_os_locations,
    _test_find_python_runs_each_candidate,
)
from tests.checks.verify_tests import _test_verify_runtime_tests, _test_verify_reuses_unchanged_runs, _test_verify_reuse_needs_git, _test_run_sh_test_command  # noqa: E402
from tests.checks.metrics import _test_metrics_contract  # noqa: E402
from tests.checks.replay import _test_fact_replay  # noqa: E402
from tests.checks.usage_tokens import _test_usage_token_accounting  # noqa: E402
from tests.checks.redaction import _test_key_pattern_needs_a_left_boundary, _test_redaction_boundary  # noqa: E402
from tests.checks.registry import _test_every_check_is_registered  # noqa: E402
from tests.checks.stamp_version import _test_stamp_version_reads_versions_not_addresses  # noqa: E402
from tests.checks.manifest import (  # noqa: E402
    _run_package_artifacts,
    _test_doctor_agrees_with_install_check,
    _test_malformed_manifest_entry_fails_closed,
    _test_manifest_matches_dist,
    _test_package_artifacts_dispatch,
)
from tests.checks.codex_write_probe import _test_codex_sandbox_blocks_writes  # noqa: E402
from tests.checks.atomic_writers import (  # noqa: E402
    _test_atomic_helpers_keep_their_contract,
    _test_every_writer_outlasts_a_held_file,
)
from tests.checks.session_persistence import (  # noqa: E402
    _test_atomic_write_outlasts_a_held_file,
    _test_provider_thread_id_is_never_lost_silently,
    _test_result_names_the_provider_boundary,
)
from tests.checks.workflow_layout import _test_workflow_layout  # noqa: E402
from tests.checks.workspace_migration import _test_workspace_migration  # noqa: E402
from tests.checks.installer_stale import _test_installer_leaves_no_stale_files  # noqa: E402
from tests.checks.opencode_launch import _test_opencode_prompt_never_reaches_cmd  # noqa: E402
from tests.checks.statusline import _test_statusline_failed_calls_are_not_estimates  # noqa: E402
from tests.checks.verify_gaps import (  # noqa: E402
    _test_empty_section_is_not_a_finding,
    _test_quick_verify_gaps,
    _test_verification_routing,
)
from tests.checks.research_regressions import (  # noqa: E402
    _test_research_parser_regressions,
    _test_research_gate_regressions,
)
from tests.checks.workspace import (  # noqa: E402
    _test_init_upgrade_and_session_guard,
    _test_script_drift_path_spelling,
    _test_project_session_isolation,
    _test_workspace_release_guards,
)
from tests.scenario import run_tests  # noqa: E402

# name -> (callable, one-line description). `scenario` runs the standalone checks itself
# at the end of its own sequence, so a default run does not repeat them here.
SUITES: dict[str, tuple] = {
    "scenario": (run_tests, "stateful sequence + every standalone check (the full run)"),
    "registry": (_test_every_check_is_registered, "every check here is reachable from both entry points"),
    "research-parser-regressions": (_test_research_parser_regressions, "P01-P12: preserve findings while accepting supported empty-section forms"),
    "research-gate-regressions": (_test_research_gate_regressions, "G01-G04: session authorization, literal command data, and directory scope"),
    "test-isolation": (_test_suite_ignores_the_machine_seed, "test workspaces are seeded from the shipped example, never the machine's provider pick"),
    "bundle-sync": (_test_bundle_registry_bijection, "skills/ and intent-map.json agree with CLAUDE.md"),
    "hook-flavours": (_test_every_shipped_hook_is_a_launcher_and_its_module, "every shipped hook is a bash launcher beside its .py, all listed by workflow_hooks.py; no .ps1 ships"),
    "stamp-version": (_test_stamp_version_reads_versions_not_addresses, "version stamping ignores IP addresses and --check passes"),
    "manifest": (_test_manifest_matches_dist, "src/exoma/_bundle/manifest.json matches the dist/ tree it describes"),
    "package-artifacts": (_run_package_artifacts, "explicit opt-in: build current wheel/sdist and smoke each in a clean environment"),
    "package-artifacts-dispatch": (_test_package_artifacts_dispatch, "both CLI spellings run artifact smoke once; ordinary suites never build"),
    "manifest-malformed": (_test_malformed_manifest_entry_fails_closed, "a malformed manifest entry fails bundle integrity by name"),
    "doctor-rendered": (_test_doctor_agrees_with_install_check, "doctor treats install-rendered files and an unselected provider's project boundary as clean, like install --check"),
    "workflow-layout": (_test_workflow_layout, ".workflow/ keeps editable files, data/ the rest; unmigrated workspaces and hooks follow the same rule"),
    "workspace-migration": (_test_workspace_migration, "v3.5.x -> data layout moves everything once with a backup, refuses under a live job, rolls back on failure; overrides-only config; current/ mirror"),
    "opencode-launch": (_test_opencode_prompt_never_reaches_cmd, "opencode's prompt travels as an attached file; cmd.exe-parsed arguments are refused"),
    "statusline": (_test_statusline_failed_calls_are_not_estimates, "a failed call is counted beside the calls, never as an estimate"),
    "provider-seam": (_test_provider_seam, "adapter registry and provider resolution"),
    "provider-threads": (_test_provider_threads_are_kept_per_provider, "a provider thread is resumed only by the provider that issued it"),
    "atomic-write-held": (_test_atomic_write_outlasts_a_held_file, "a reader briefly holding a file does not lose an atomic write"),
    "atomic-helpers": (_test_atomic_helpers_keep_their_contract, "atomic text/bytes helpers: newline, fsync, mode, no temp left on failure"),
    "atomic-writers": (_test_every_writer_outlasts_a_held_file, "every temp-and-replace writer outlasts a held file and keeps its raise-or-swallow policy"),
    "session-persistence": (_test_provider_thread_id_is_never_lost_silently, "a provider thread id is kept, or its loss is named"),
    "provider-boundary": (_test_result_names_the_provider_boundary, "every delegated result names its provider's read/write boundary"),
    "codex-write-probe": (_test_codex_sandbox_blocks_writes, "opt-in (WORKFLOW_CODEX_PROBE=1): a real codex call cannot write under --sandbox read-only"),
    "provider-selection": (_test_provider_selection, "interactive provider/model/effort write"),
    "agy": (_test_agy_provider, "agy parsing, argv, and its read-boundary guard"),
    "agy-gitlinks": (_test_agy_guard_gitlinks, "dirty gitlinks preserve ordinary mutation detection without recursive content reads"),
    "doctor-read-boundary": (_test_doctor_read_boundary_warning, "doctor warns that a codex/agy second agent reads every file and env var"),
    "secret-files": (_test_secret_files, "secret files found by name, an ignored .env watched by the agy guard, codex selectable with its boundary warning, EXOMA_/AI_PROXY_ env alias"),
    "cli": (_test_cli_script_reaches_its_exit_code, "main.py run as a script prints JSON and exits through its exit-code mapping"),
    "self-update": (_test_self_update, "update refreshes the package and current workspace through a fresh CLI"),
    "messages": (_test_no_code_in_messages, "AST scan: no code leaks into user-facing text"),
    "facts-concurrency": (_test_facts_concurrency, "fact store under concurrent writers"),
    "fact-selection": (_test_fact_selection, "every cited anchor must hold, relevance ignores grammar, eviction keeps what is used"),
    "fact-replay": (_test_fact_replay, "EXP-013 replay: one-token and weighted arms over archived calls, facts learned later kept out, store untouched"),
    "anchor-relocation": (_test_anchor_relocation, "facts survive a line moving"),
    "evidence-anchor-relocation": (_test_evidence_anchor_relocation, "artifact reuse rejects moved citation lines while immutable evidence stays intact"),
    "evidence-reuse": (_test_evidence_reuse, "identical query served from a fresh artifact"),
    "evidence-lock-file": (_test_evidence_lock_file, "evidence.jsonl.lock is a persistent one-byte OS lock that excludes other processes"),
    "runtime-guards": (_test_runtime_guard_cleanup, "clean removes the runtime-lock guards of dead sessions, keeps live and held ones (DEC-013)"),
    "process-identity": (_test_process_generation_outcomes, "a recorded PID is matched, reused, dead or unverifiable; unverifiable is never matched"),
    "worker-kill": (_test_worker_kill_is_fail_closed, "a worker is killed only while its PID provably still hosts it"),
    "byte-lock": (_test_byte_lock_excludes_handles_and_processes, "the one osutil byte-0 lock excludes other handles and processes and dies with its holder"),
    "redaction": (_test_redaction_boundary, "secret boundary on outbound payloads"),
    "redaction-boundary": (_test_key_pattern_needs_a_left_boundary, "kebab-case words containing sk- are not keys; real keys still are"),
    "verify-gaps": (_test_quick_verify_gaps, "quick verify reports gaps as incomplete"),
    "verify-routing": (_test_verification_routing, "the routing table decides what blocks, both ways"),
    "verify-empty-section": (_test_empty_section_is_not_a_finding, "an empty section holds nothing, not the next heading"),
    "e2e-spec": (_test_e2e_spec_contract, "[E2E SPEC] parses, validates, and knows when only the section is missing"),
    "e2e-normalize": (_test_e2e_classification_and_verdicts, "app/harness/unknown origins and the fail-closed verdict matrix"),
    "e2e-network": (_test_e2e_network, "request metadata: rolling window, whole-run totals, no query values, cross-run difference"),
    "e2e-supervisor": (_test_e2e_supervisor, "the player child process: protocol, idle/total timeouts, tree kill"),
    "e2e-routing": (_test_e2e_routing, "verify-browser: draft then confirmed run under one lock, request and secrets.json profiles only, /.verify back to delegated|syntax"),
    "e2e-browser": (_test_e2e_browser_session, "the real player's step semantics, origin guard, and observers against a stand-in page"),
    "e2e-redact": (_test_e2e_redaction_variants, "resolved ${ENV} values scrubbed raw, URL-encoded, and escaped, in events and text artifacts"),
    "e2e-smoke": (_test_e2e_real_browser_smoke, "real Chromium against the fixture app through the full runner (opt-in: WORKFLOW_E2E_SMOKE=1)"),
    "e2e-existing-tests": (_test_e2e_existing_tests, "the project's own tests: user command and allowlist only, no shell, bounded, scrubbed, not trusted blindly"),
    "e2e-tagging": (_test_e2e_tagging, "data-e2e is written only to a cited template line inside the project that Git carries"),
    "e2e-hardening": (_test_e2e_hardening, "retry passes are not clean, expected values stay placeholders, redirects and recovery cannot replay writes"),
    "e2e-knowledge": (_test_e2e_knowledge, "what a browser run proved is kept per origin, retired on repeated failure, and offered to the next draft"),
    "e2e-harness": (_test_e2e_harness_names_its_model, "tools/e2e/e2e.py --full refuses without --model before any check, and selects the named provider and model through the provider command"),
    "e2e-metrics": (_test_e2e_metrics, "e2e runs as quality rows: rates per run kind, token join, reproducibility, delegated baseline"),
    "e2e-doctor": (_test_e2e_doctor_readiness, "doctor reports verify-browser readiness from package metadata and probes nothing"),
    "jobs": (_test_submit_admission, "job admission, capacity, and lock"),
    "worker-logs": (_test_worker_log_preservation, "same-job recovery preserves previous worker log output"),
    "job-store": (_test_job_store_not_writable, "an unwritable job store names the sandbox root to grant, in submit and in doctor"),
    "workspace-release": (_test_workspace_release_guards, "lock release guards"),
    "session-isolation": (_test_project_session_isolation, "one project's session cannot read another's"),
    "init-upgrade": (_test_init_upgrade_and_session_guard, "init/upgrade, the named runner options, the session guard for session-state commands, and an unreadable provider config in doctor"),
    "drift-spelling": (_test_script_drift_path_spelling, "one project reached through two path spellings (`..`, a symlink) generates one runner text, so upgrade sees no drift"),
    "continuation": (_test_contract_continuation, "bounded continuation keeps the first reply's evidence"),
    "continuation-size": (_test_continuation_prompt_is_bounded, "the recovery prompt fits the command line it travels on"),
    "contracts": (_test_workflow_contracts, "workflow contracts round-trip and the usage stream derives honestly"),
    "execution-chain": (_test_execution_chain, "plan-record refuses a proceed with open questions; execute-begin needs the user's own -y; verify writes its verdict"),
    "promote-boundaries": (_test_promote_boundaries, "promoted knowledge stays inside the project and is part of evidence reuse identity"),
    "knowledge-retrieve": (_test_knowledge_retrieval, "retrieval checks source freshness, detached HEAD, and staged/unstaged changes"),
    "deps": (_test_runtime_is_stdlib_only, "shipped code imports nothing outside the stdlib"),
    "hidden-spawn": (_test_every_spawn_hides_its_console, "every runtime subprocess hides its console on Windows"),
    "telemetry": (_test_telemetry_metrics, "P1 metrics count tasks, not calls, and report their denominators"),
    "task-telemetry": (_test_task_telemetry, "DEC-015: tasks by state from skill, edit and commit events; both task-events hook flavours agree"),
    "usage-tokens": (_test_usage_token_accounting, "provider token counts reach the row, and breakdowns never become addends"),
    "transcript": (_test_transcript_parsing, "a Claude transcript yields human turns and per-turn context, not tool-call counts"),
    "governance": (_test_governance_controls, "provider allowlist, budget ceiling, tool policy, local-first streams"),
    "graph-verification": (_test_graph_verification, "per-node graph provenance, drift vs move, subgraph slicing"),
    "graph-refresh": (_test_graph_refresh_hook, "the Stop hook prunes, returns before graphify, and readers wait out the refresh lock"),
    "adapters": (_test_adapter_error_normalization, "every adapter normalises errors and counts redactions alike"),
    "adapters-shared": (_test_adapter_redaction_is_shared, "no adapter carries a private copy of the redaction helpers"),
    "adapters-stdin": (_test_stdin_failure_reaches_call_meta, "a failed stdin handover names its cause in the call meta"),
    "error-types": (_test_error_types_are_registered, "every literal make_error type is registered, so no failure path raises"),
    "prompt-blocks": (_test_prompt_contract_blocks, "every role/command branch asks for a shape the runtime parses"),
    "prompt-verify": (_test_verify_branch_carries_routing_contract, "verify prompt carries the severity routing triple"),
    "prompt-tools": (_test_permitted_tools_line, "declared tool policy reaches the prompt, absence stays absent"),
    "prompt-cap": (_test_task_cap_is_visible_in_and_out_of_band, "task truncation is reported in band and out"),
    "prompt-transport-cap": (_test_task_cap_follows_the_provider_transport, "the task cap is derived from the provider transport, not one shared constant"),
    "prompt-role": (_test_unknown_role_is_refused, "an unknown role is refused instead of building an unparseable prompt"),
    "installer-text": (_test_installer_text_merging, "lenient decode, intent stanzas, managed-block splice"),
    "installer-settings": (_test_installer_settings_merge, "hook refresh keeps user hooks; POSIX rewrite ships bash"),
    "installer-additive": (_test_installer_settings_are_additive, "settings merge adds missing keys and keeps the user's"),
    "installer-rollback": (_test_installer_rollback_receipt, "receipted rollback restores, deletes, and refuses drift"),
    "installer-check": (_test_installer_drift_check, "settings drift detection: missing, current, unparseable"),
    "installer-seed": (_test_installer_seeds_second_agent_config, "second_agent.json is seeded once and never overwritten"),
    "installer-seed-tty": (_test_installer_seed_is_non_interactive_without_a_tty, "no tty, no prompt: CI and e2e cannot hang"),
    "installer-seed-prompt": (_test_installer_seed_prompt_answers, "every answer the provider/model picker accepts"),
    "installer-asks": (_test_installer_asks_every_choice, "a terminal install asks every choice with the stored one as default; --no-input asks nothing"),
    "installer-seed-boundary": (_test_installer_seed_reports_unenforced_provider, "an unenforced provider is named as one while it is chosen, with no opt-in variable"),
    "provider-update": (_test_provider_update, "DEC-057: --update-providers pins npm-global CLIs to stable_version only, verified, failures named"),
    "installer-seed-state": (_test_installer_check_reports_second_agent_default, "--check describes the seed without calling it drift"),
    "installer-e2e": (_test_installer_e2e_deps_are_opt_in, "--with-e2e installs the Playwright extra through the env's own manager (pip, pipx inject, uv pip) and its browser; a failed step fails the install; nothing without it"),
    "install-method": (_test_install_method_detection, "the install method is the env Exoma runs in: uv tool first, then a pipx env, then pip; unknown without any"),
    "metrics-contract": (_test_metrics_contract, "every benchmark metric has an ID, version and method, is documented, and its producers stamp provenance"),
    "verify-runtime-tests": (_test_verify_runtime_tests, "tests main_agent chose run only under the allowlist, without a shell, and decide the verdict beside the review"),
    "verify-reuse": (_test_verify_reuses_unchanged_runs, "verify reuses a run.sh test pass while the tree content is unchanged; an edit or failure runs it again"),
    "verify-reuse-no-git": (_test_verify_reuse_needs_git, "without Git nothing is fingerprinted and nothing is reused"),
    "run-sh-test": (_test_run_sh_test_command, "run.sh test refuses bad input and an empty allowlist by name, and is closed to a second agent"),
    "gate-prompt-field": (_test_intent_gate_reads_the_prompt_field, "intent-gate-set arms the gate from Claude Code's `prompt` field"),
    "gate-runner-parsed": (_test_intent_gate_runner_parsed, "one tokeniser for the Bash and PowerShell tools: one plain call to [bash] <root>/.workflow/{run,check,inspect}.sh passes; $ ; | & < > pass only inside single quotes; run.ps1 is retired"),
    "gate-runner-tamper": (_test_intent_gate_runner_tamper, "a write to the runner scripts is refused, armed or not; other writes pass"),
    "gate-non-ascii-root": (_test_intent_gate_non_ascii_root, "the hooks read the session registry as UTF-8 and the gate takes a non-ASCII project root in a runner path"),
    "hook-launchers": (_test_hooks_run_through_their_launchers, "every hook run through its real bash launcher (Git Bash on Windows) keeps its exit/stdout/file contract, within half its timeout; the DEC-068 failures replay clean"),
    "agy-host": (_test_agy_host_contract, "the Agy host wrapper answers every PreToolUse with a decision, fails open, passes [WORKFLOW_AGENT] turns, and gates through the same preflight_refusal as the Claude hooks (writes, precision reads, verify test selection)"),
    "codex-host": (_test_codex_host_contract, "Codex hooks bind sessions, route delegation, gate tools, and skip second-agent processes"),
    "codex-host-install": (_test_codex_host_install, "Codex host and provider blocks coexist; install and uninstall preserve user text and hooks"),
    "codex-auto-review-context": (check_codex_auto_review, "owned additive reviewer context: preserved user policy, choice, one config transaction, rollback and uninstall"),
    "opencode-host": (_test_opencode_host_contract, "OpenCode hooks bind on the first prompt, gate tools through the shared policy, and skip second-agent processes"),
    "opencode-host-install": (_test_opencode_host_install, "OpenCode host installs beside its provider block, passes --check, and is withdrawn cleanly"),
    "host-contract-render": (_test_contract_renders_per_host, "one contract source rendered per host: no Claude-only text, placeholder or HOST marker outside Claude's"),
    "host-sections": (_test_host_sections_whitespace, "HOST sections, block and inline, kept or dropped, leave no joined words, blank lines or indentation"),
    "host-rule-parity": (_test_rule_registry_parity, "every contract rule is registered, labelled once per host, and its level is backed by a hook call or the runtime"),
    "host-runner-call": (_test_runner_call_single_source, "contract, [INTENT HINT] and refusal name one runner call per host, from host-profile.json"),
    "host-read-limit": (_test_read_limit_from_host_keys, "a read's line span comes from the host's own argument names"),
    "agy-precision-slice": (_test_agy_precision_read_slice, "an Agy slice of a long file passes as a precision read"),
    "codex-grant": (_test_codex_grant, "the install writes the Codex job store grant, keeps a user's own table, and uninstall takes only its block"),
    "claude-second-agent": (_test_claude_second_agent, "Claude second agent runs with read tools only, no settings or MCP, over stdin"),
    "per-host-seed": (_test_per_host_seed, "each host keeps its own provider seed and init copies the one of the host that ran it"),
    "session-export": (_test_session_export, "session export bundles one session, redacted, with its stream rows and review"),
    "e2e-opt-in": (_test_e2e_stays_opt_in, "the browser extra is installed only with --with-e2e"),
    "recursion-guard": (_test_recursion_guard, "a second_agent process cannot delegate, queue or run a job, or change execution state: recursion_refused before any side effect"),
    "same-provider-isolation": (_test_same_provider_child_isolation, "every adapter marks its child second_agent; Claude-on-Claude loads no host settings; the OpenCode plugin checks the marker"),
    "same-provider-export": (_test_same_provider_export_picks_the_host, "with host and child on the same CLI, session export picks the host transcript"),
    "agy-host-python-fallback": (_test_agy_host_falls_back_to_a_plain_interpreter, "a spaced env python (pipx on macOS) gives the Agy hook its plain-path base interpreter; all candidates unsafe is still refused, with how to drop the host"),
    "agy-doctor-refused": (_test_doctor_reports_a_refused_agy_host_once, "doctor reports a chosen Agy host whose hook is refused as one finding with the deselect command, not two missing files"),
    "agy-host-install-whole": (_test_agy_host_install_all_or_nothing, "the Agy instructions and hook install together or not at all; a refused hook takes out an earlier instructions block"),
    "agy-host-uninstall": (_test_agy_host_uninstall, "uninstall takes only the `exoma` hook out of hooks.json, keeps the user's hooks, and receipts the change for --rollback"),
    "agy-host-install": (_test_agy_host_install_merges, "the installer merges only the `exoma` hook name into hooks.json and only the EXOMA-HOST block into GEMINI.md"),
    "agy-doctor": (_test_agy_permissions_and_doctor, "Agy permission rules on with the agy host unless opted out; stored host pairs remain valid; /.doctor reports each chosen host and observes the Agy session only in Agy"),
    "bundle-layout": (_test_bundle_layout_and_agy_agent, "hosts/ and providers/ keys, old keys read as current, the Agy second agent as a custom agent, ~/.agy retired"),
    "agy-host-select": (_test_agy_host_selection, "the host choice picks the targets; Agy files stay under ~/.gemini; permission rules are opt-in and only Exoma's are removed"),
    "hook-python-resolver": (_test_hook_launchers_resolve_python, "a hook launcher picks python3, python or `py -3` by running it, skips a Store alias or an old one, and fails open with none"),
    "python-guard": (_test_entry_points_refuse_an_old_python, "main.py, install.py and check.py refuse a Python older than osutil.MIN_PYTHON with a message, before any project import"),
    "os-resolvers": (_test_find_python_runs_each_candidate, "find_python tries python3, python, `py -3` by running each and returns only one new enough"),
    "doctor-python": (_test_doctor_python_check_follows_the_launchers, "doctor's python check follows the launchers: python.path, then python3/python/py -3 by running them; a missing `python` alias is not an issue"),
    "os-locations": (_test_find_bash_and_os_locations, "find_bash is Git Bash on Windows; cache dir, env location and command splitting follow the OS"),
    "gate-k5-fixtures": (_test_gate_k5_fixtures, "the single-tokeniser gate decides each K5 case as recorded (the #4 field cases included)"),
    "installer-uninstall": (_test_installer_uninstall, "first install writes the managed block alone; --uninstall cuts it and keeps the user's text and hooks; a moved hook leaves its old event"),
    "installer-stale": (_test_installer_leaves_no_stale_files, "an install removes only recorded, unedited, unshipped files, drops retired hooks, refuses an unstamped dist"),
    "audit": (_test_audit_report, "the governance trail reads back and keeps a null provider visible"),
    "audit-torn": (_test_audit_survives_a_torn_row, "a partial final line does not hide the readable trail"),
    "audit-separate": (_test_audit_is_not_telemetry, "audit and usage stay separate readers over separate files"),
    "hardening": (_test_hardening, "v3.7.1: owned locks, honest rollback/receipt/job-lock/telemetry failure paths, project-scoped opencode env, read-path metadata refusal"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Exoma tests")
    parser.add_argument(
        "--only",
        action="append",
        metavar="NAME",
        help="run just this suite; repeatable. Default runs `scenario`, which covers all.",
    )
    parser.add_argument("--list", action="store_true", help="print suite names and exit")
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="run every selected suite even after one fails, then report all failures",
    )
    parser.add_argument(
        "--record",
        metavar="PROJECT_ROOT",
        default=None,
        help=(
            "append the outcome to PROJECT_ROOT/.workflow/quality.jsonl so "
            "`--command report` can show a test pass rate. Off by default: a local run "
            "while debugging is not a data point about the repo's health."
        ),
    )
    args = parser.parse_args()

    if args.list:
        width = max(len(name) for name in SUITES)
        for name, (_, description) in SUITES.items():
            print(f"  {name.ljust(width)}  {description}")
        return 0

    selected = args.only or ["scenario"]
    unknown = [name for name in selected if name not in SUITES]
    if unknown:
        print(f"unknown suite(s): {', '.join(unknown)}", file=sys.stderr)
        print("run with --list to see the names", file=sys.stderr)
        return 2

    failures: list[tuple[str, BaseException]] = []
    for name in selected:
        run, _ = SUITES[name]
        started = time.monotonic()
        try:
            run()
        except BaseException as exc:  # noqa: BLE001 — a failed check is any exception
            failures.append((name, exc))
            print(f"  FAIL  {name}  ({type(exc).__name__}: {exc})")
            if not args.keep_going:
                break
        else:
            print(f"  PASS  {name}  ({time.monotonic() - started:.1f}s)")

    if args.record:
        _record_outcome(args.record, selected, failures)

    if failures:
        print(f"\n{len(failures)} of {len(selected)} suite(s) failed:")
        # Preserve stdout for callers, and keep failures at the end of runtime stderr
        # tails even when expected error-path tests emitted their own diagnostics.
        print(f"\n{len(failures)} of {len(selected)} suite(s) failed:", file=sys.stderr)
        for name, exc in failures:
            print(f"  - {name}: {type(exc).__name__}: {exc}")
            print(f"  - {name}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"\ntests: success ({len(selected)} suite(s))")
    return 0


def _record_outcome(project_root: str, selected: list, failures: list) -> None:
    """Append this run to the quality stream. Never fails the run it is recording.

    Records which suites ran, not just pass/fail. A green `--only contracts` and a green
    full run are both "ok", and a pass rate that cannot tell them apart would let a narrow
    run stand in for a broad one.
    """
    try:
        from datetime import datetime, timezone

        from exoma.core.evidence.runtime_io import write_quality_record

        write_quality_record(
            Path(project_root),
            {
                "kind": "tests",
                "at": datetime.now(timezone.utc).isoformat(),
                "ok": not failures,
                "suites": list(selected),
                "failed": [name for name, _ in failures],
            },
        )
    except Exception as exc:  # noqa: BLE001 — recording must never mask the result
        print(f"  (quality record not written: {type(exc).__name__}: {exc})")


if __name__ == "__main__":
    raise SystemExit(main())
