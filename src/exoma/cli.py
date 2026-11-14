import json
import os
import secrets
import sys

# Before any import that the newer syntax would break: the oldest supported Python is
# utils/osutil.py MIN_PYTHON, repeated here because importing it would fail on the very
# interpreter this names.
if sys.version_info < (3, 11):
    sys.exit(
        f"Exoma needs Python 3.11 or newer; this is {sys.version.split()[0]} "
        f"({sys.executable}). Reinstall it under Python 3.11+, e.g. "
        "`uv tool install --python 3.11 exoma`."
    )

# Looks unused to a linter and is NOT: core/jobs/job_lifecycle.py reaches it as
# `_main().subprocess`, and tests/scenario.py patches `main.subprocess.Popen` to
# intercept worker spawn. Deleting it breaks both, silently and at runtime only.
# Inspect must bypass eager managers, including when invoked through the public CLI.
if (sys.argv[1:2] == ["inspect"] or "--command=inspect" in sys.argv[1:] or
        any(sys.argv[index] in {"--command", "-c"} and sys.argv[index + 1] == "inspect"
            for index in range(1, len(sys.argv) - 1))):
    from exoma.core.jobs.diagnostics import main as diagnostic_main
    raise SystemExit(diagnostic_main(sys.argv[2:] if sys.argv[1:2] == ["inspect"] else sys.argv[1:]))

import subprocess  # noqa: F401
from pathlib import Path

from exoma.config.routing import INTERNAL_COMMANDS
from exoma.core.evidence.contract import make_error
from exoma.core.provider.executor import Executor
from exoma.core.jobs.job_manager import JobManager
from exoma.core.workspace.session_manager import SessionManager
from exoma.utils.path_guard import safe_path_component
from exoma.core.audit.diagnostics import prune_sessions, run_doctor, run_sweep
from exoma.core.workspace.runtime_lock import acquire_runtime_lock, release_runtime_lock
from exoma.core.runtime.state import ensure_workflow_workspace
from exoma.core.runtime.upgrade import needs_upgrade, upgrade_workflow_workspace, workspace_versions
from exoma.core.workspace.workspace_paths import detect_project_root, workflow_paths
from exoma.config.settings import (
    # These two are read as `_main().DEFAULT_*` from core/evidence/result_shaping.py, and the
    # harness lowers them on `main` to exercise the ref_only threshold. Imported here
    # so that indirection has something to find — not unused, just used elsewhere.
    DEFAULT_CONTENT_PREVIEW_CHARS,  # noqa: F401
    DEFAULT_SLIM_CONTENT_MIN_CHARS,  # noqa: F401
    DEFAULT_JOB_POLL_INTERVAL_SECONDS,
    DEFAULT_JOB_POLL_TIMEOUT_SECONDS,
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    load_provider_config,
    load_provider_config_for,
    MAIN_SESSION_CACHE_TTL_SECONDS,
    cached_main_session_age,
    get_cached_main_session_id,
    set_cached_main_session_id,
)
from exoma.utils.parser import generate_main_session_id

SESSION_MANAGER = SessionManager()
_DEFAULT_SESSION_MANAGER = SESSION_MANAGER
EXECUTOR = Executor(session_manager=SESSION_MANAGER)
JOB_MANAGER = JobManager()
BACKGROUND_COMMANDS = {"explore", "plan", "analyze", "verify", "verify-browser"}

# Split out in v3.4.3. Re-exported so every existing caller of `main.<name>`
# keeps working; the singletons above stay here because this is the entry point.
from exoma.core.jobs.job_lifecycle import (  # noqa: E402,F401
    _apply_job_thresholds,
    _job_config,
    _max_probes,
    _probe_recheck_seconds,
    _spawn_worker,
    await_job,
    check_stalled_job,
    get_result,
    get_status,
    run_worker,
    should_run_in_background,
    submit,
)
from exoma.core.runtime import execution  # noqa: E402
from exoma.core.evidence.result_shaping import (  # noqa: E402,F401
    _as_ref_only,
    _finalize_verify_result,
    _HEAVY_META_KEYS,
    _slim_result,
    _verify_exit_code,
    _without_raw_args,
)


def _session_storage_id(session_id: str) -> str:
    return safe_path_component(session_id)


def _session_manager_for(project_root: Path) -> SessionManager:
    if SESSION_MANAGER is not _DEFAULT_SESSION_MANAGER:
        return SESSION_MANAGER
    return SessionManager(
        workflow_paths(project_root)["provider_sessions_dir"]
    )


def resolve_session_id(
    session_id: str, fresh: bool = False, project_root: str | Path | None = None
) -> str:
    """Resolve the effective session ID, using cache or generating a new one."""
    cache_root = (
        detect_project_root(str(project_root)) if project_root is not None else None
    )
    if session_id != "default" and not fresh:
        return session_id
    if not fresh:
        cached = get_cached_main_session_id(cache_root)
        if cached and _cached_session_still_current(cached, cache_root):
            # Restamp: a session that keeps resolving must not age out mid-use.
            set_cached_main_session_id(cached, cache_root)
            return cached
    new_id = f"{generate_main_session_id()}_{secrets.token_hex(3)}"
    if session_id == "default":
        set_cached_main_session_id(new_id, cache_root)
    return new_id


def _cached_session_still_current(session_id: str, cache_root) -> bool:
    """Whether a cached "default" session may still claim new work.

    The cache used to be written once and read forever, so the first session ever run
    against a project captured every later call that fell back to "default" — its jobs,
    its lock, its logs. A cached session stays current only while it is still working,
    or while it was resolved recently enough to still be the one at the keyboard.
    """
    try:
        from exoma.core.jobs.job_manager import JobManager

        if JobManager().active_job_for_session(session_id):
            return True
    except (OSError, ValueError):
        # Job state unreadable — fall through to the age check rather than guessing.
        pass
    age = cached_main_session_age(cache_root)
    if age is None:
        # Pre-TTL cache file with no stamp. Honour it once; the restamp above gives it one.
        return True
    return age < MAIN_SESSION_CACHE_TTL_SECONDS


def _promote(command: str, project_root: Path, task: str) -> dict:
    """The three promote stages, each reading its document from the path in `--prompt`.

    A path rather than the JSON itself: the task becomes one argv entry, and Windows caps
    that at 8191 characters — the same limit that moved evidence leads out of the prompt
    and into sidecars. A knowledge document with a dozen claims clears it easily.
    """
    from exoma.core.knowledge import reconcile, retrieve, schema, store, verify
    from exoma.core.runtime.config_defaults import production_branch
    from exoma.utils import git

    if not task or not str(task).strip():
        return make_error(
            "promote_input_missing",
            f"{command} needs the path to the knowledge JSON in --prompt",
            next_action="Write the candidate document to a file and pass its path.",
        )

    source = Path(str(task).strip())
    try:
        doc = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return make_error(
            "promote_input_unreadable",
            f"cannot read {source} as JSON: {exc}",
            next_action="Fix the candidate document, then retry.",
        )

    if command == "promote-validate":
        errors = schema.validate(doc)
        return {
            "ok": not errors,
            "content": "knowledge document is valid" if not errors else "knowledge document rejected",
            "meta": {"errors": errors, "path": str(source)},
        }

    if command == "promote-verify":
        result = verify.verify_doc(project_root, doc)
        existing = store.load(project_root, doc.get("id", ""))
        if not existing.get("ok"):
            return make_error(
                "promote_existing_unreadable",
                existing["error"],
                next_action="Repair or remove the malformed knowledge file, then retry.",
            )
        # Reconciled here, while the freshness verdicts are in hand, so the agent gets
        # one report instead of having to join two.
        merged = reconcile.reconcile(
            (existing.get("doc") or {}).get("claims"), doc.get("claims"), result["claims"]
        )
        return {
            "ok": True,
            "content": (
                f"{result['fresh_count']} fresh, {result['stale_count']} stale; "
                + ", ".join(f"{k}={v}" for k, v in sorted(merged["counts"].items()))
            ),
            "meta": {
                "verification": result,
                "reconciliation": merged,
                "existing_path": existing.get("path"),
            },
        }

    # promote-write. The branch gate lives here rather than in the store because it is a
    # policy about when promotion may happen, not about whether a document is well
    # formed — and every other caller of store.write deserves to be refused for the
    # shape of its document, not for where someone's HEAD happens to be.
    branch = git.current_branch(project_root)
    expected = production_branch(project_root)
    if branch is None:
        return make_error(
            "promote_not_on_branch",
            "not on a branch (detached HEAD, mid-rebase, or a repository with no commits)",
            next_action=f"Check out {expected}, then retry.",
        )
    if branch != expected:
        return make_error(
            "promote_not_production_branch",
            f"on {branch!r}, but promotion is only allowed on {expected!r}",
            next_action=f"Check out {expected}, or change policies.production_branch.",
            meta={"branch": branch, "production_branch": expected},
        )

    pruned = retrieve.prune_dead_exclusions(project_root, doc)
    written = store.write(project_root, pruned["doc"])
    if not written.get("ok"):
        return make_error(
            written.get("error_type", "promote_write_rejected"),
            "; ".join(written.get("errors") or ["write refused"]),
            next_action="Fix the reported problems, then retry.",
            meta={"errors": written.get("errors") or []},
        )
    return {
        "ok": True,
        "content": (
            f"wrote {written['claims']} claim(s) to {written['path']}"
            if written["written"]
            else f"{written['path']} already matched; nothing to write"
        ),
        "meta": {**written, "dropped_exclusions": pruned["dropped"], "branch": branch},
    }


def _test_command(project_root: Path, session_id: str, task: str | None, main_host: str | None = None) -> dict:
    """`run.sh test`: run project tests through the runtime and record them for /.verify.

    `--task` is one command line, or a JSON list of them (at most five). The policy is the
    verify one (core/evidence/verify_tests.py): `commands.verify_test_commands` prefixes, no
    shell syntax, the verify timeout. Each outcome is recorded with the tree's fingerprint,
    so a later /.verify that requests the same command on an unchanged tree reuses the pass
    instead of running it again.
    """
    from exoma.core.evidence import verify_tests
    from exoma.config.hosts import command_invocation

    verify_invocation = command_invocation(main_host or "claude", "verify")

    text = str(task or "").strip()
    commands: object = text
    if text.startswith("["):
        try:
            commands = json.loads(text)
        except ValueError:
            commands = None
    commands = [commands] if isinstance(commands, str) and commands else commands
    if not isinstance(commands, list) or not commands or not all(isinstance(c, str) for c in commands):
        return make_error(
            "test_input_invalid",
            "test needs a command line in --task, or a JSON list of command lines",
            next_action="Pass one allowlisted command, e.g. --task 'python -m pytest tests/x.py'.",
        )
    if not verify_tests.configured(project_root):
        return make_error(
            "test_not_configured",
            "commands.verify_test_commands in .workflow/config.json is empty: no command may run",
            next_action="List the project's test command prefixes there, then retry.",
        )
    ran = verify_tests.run_and_record(project_root, session_id, commands)
    outcomes = ran["outcomes"]
    if not outcomes and ran.get("record_error"):
        # Nothing ran: a run the session cannot record would leave an older pass in charge.
        return make_error(
            "session_state_error",
            f"tests not run: {ran['record_error']}",
            next_action="Make the session's .workflow/data/sessions/<id>/verify/ directory "
            "writable, then run the tests again.",
            meta={"record_error": ran["record_error"],
                  "record": str(verify_tests.runs_path(project_root, session_id))},
        )
    lines = []
    for outcome in outcomes:
        detail = outcome.get("detail") or (
            f"exit {outcome['exit_code']}" if "exit_code" in outcome else outcome["status"]
        )
        lines.append(f"- `{outcome['command']}` {outcome['status']} ({detail}, {outcome.get('seconds', 0)} s)")
        if outcome["status"] in ("failed", "timeout", "error") and outcome.get("tail"):
            lines.append("  tail: " + " ".join(outcome["tail"].split())[-600:])
    passed = all(o["status"] == "passed" for o in outcomes)
    # From `reusable` alone, never the batch fingerprint: a batch can keep its identity while
    # a command's latest outcome failed or another run's marker withholds it. Per command,
    # too: a batch whose setup step changed the tree still records the tests after it.
    reusable = ran.get("reusable") or []
    passes = len({o["command"] for o in outcomes if o["status"] == "passed"})
    if ran.get("record_error") and reusable:
        note = f"recorded, with a warning: {ran['record_error']}"
    elif ran.get("record_error"):
        note = f"not reusable: the run could not be recorded ({ran['record_error']})"
    elif reusable and len(reusable) == passes:
        note = f"recorded: a later {verify_invocation} reuses each pass while its inputs stay unchanged"
    elif reusable:
        note = (f"recorded: {len(reusable)} of {passes} passes reusable; the others changed "
                f"their inputs, failed later in the list, or are withheld, so a later {verify_invocation} "
                "runs them again")
    else:
        note = ("not reusable: no pass is bound to the tree (no Git, a failure, or the run "
                "changed its inputs), or another run's marker withholds it")
    content = "\n".join(["[TESTS]", *lines, note])
    meta = {
        "outcomes": [{k: v for k, v in o.items() if k != "tail"} for o in outcomes],
        "fingerprint": ran["fingerprint"],
        "reusable": reusable,
        "record_error": ran.get("record_error"),
        "record": str(verify_tests.runs_path(project_root, session_id)),
    }
    if not passed:
        return make_error(
            "tests_failed",
            content,
            next_action="Fix the failure (or a refused command), then run the tests again; "
            f"{verify_invocation} reuses only passing runs.",
            meta=meta,
        )
    return {"ok": True, "content": content, "meta": meta}


def _execution_command(command: str, project_root: Path, session_id: str, task: str | None) -> dict:
    """plan-record / execute-begin / execute-end (core/runtime/execution.py).

    plan-record reads its plan from the path in `--prompt`, the way promote reads its
    document: one argv entry, whatever the plan's size.
    """
    if command == "plan-record":
        if not task or not str(task).strip():
            return make_error(
                "plan_input_missing",
                "plan-record needs the path to the plan JSON in --prompt",
                next_action="Write the [PLAN] as JSON to a file and pass its path.",
            )
        source = Path(str(task).strip())
        try:
            doc = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return make_error(
                "plan_input_unreadable",
                f"cannot read {source} as JSON: {exc}",
                next_action="Fix the plan document, then retry.",
            )
        recorded = execution.record_plan(project_root, session_id, doc)
        if not recorded["ok"]:
            return make_error(
                "plan_invalid",
                "; ".join(recorded["errors"]),
                next_action=(
                    "Fix the plan. A plan with open questions or a solution_path below "
                    "high is recorded as clarify, not proceed."
                ),
                meta={"errors": recorded["errors"]},
            )
        return {
            "ok": True,
            "content": f"plan {recorded['plan_id']} recorded ({recorded['decision']})",
            "meta": recorded,
        }

    if command == "execute-begin":
        begun = execution.begin_execution(project_root, session_id)
        if not begun["ok"]:
            kind, next_action = {
                "no_plan": ("execute_no_plan", "Run /.plan and record it with plan-record first."),
                "plan_not_proceed": (
                    "execute_plan_not_proceed",
                    "Resolve the plan's open questions and record it again as proceed.",
                ),
                "not_approved": (
                    "execute_not_approved",
                    "Ask the user to type `/.execute -y`; nothing else approves an execution.",
                ),
            }[begun["reason"]]
            return make_error(kind, begun["error"], next_action=next_action)
        scope = begun["execution"]["scope"]
        return {
            "ok": True,
            "content": f"execution open for plan {begun['execution']['plan_id']}; scope {len(scope)} path(s)",
            "meta": begun["execution"],
        }

    ended = execution.end_execution(project_root, session_id)
    if not ended["ok"]:
        return make_error(
            "execution_not_active",
            ended["error"],
            next_action="Nothing to close; execute-begin opens an execution.",
        )
    closed = ended["execution"]
    from exoma.core.runtime.config_defaults import verification_settings
    settings, config_warnings = verification_settings(project_root)
    required = settings["auto_verify_after_execute"]
    metadata = {
        **closed,
        "policy": {"auto_verify_after_execute": required, "verify_mode": settings["verify_mode"]},
        "config_warnings": config_warnings,
        "next_step": {
            "action": "verify_prepare" if required else "offer_verify", "required": required,
            "session_id": session_id, "execution_id": closed.get("execution_id"), "automatic": False,
        },
    }
    changed, outside = closed.get("changed_files"), closed.get("outside_scope")
    if changed is None:
        summary = "changed files unknown (no tree snapshot to compare)"
    else:
        summary = f"{len(changed)} file(s) changed since execute-begin"
        if outside:
            summary += f"; {len(outside)} outside the plan's scope: " + ", ".join(outside)
    return {
        "ok": True,
        "content": f"execution closed: implemented, verification not_run; {summary}",
        "meta": metadata,
    }


# What a second_agent child may not ask the runtime for: delegating again, queueing or
# running a job, or changing execution state. Read-only commands (inspect, status, result,
# report, audit, ...) stay open. The marker is the one every provider adapter sets on its
# child (WORKFLOW_AGENT_ROLE=second_agent). It stops inherited recursion; it is not a
# security boundary — a child that can run commands can also unset it (docs/security.md).
RECURSION_REFUSED_COMMANDS = frozenset({
    "explore", "plan", "analyze", "verify", "verify-browser",
    "submit", "await", "worker", "cancel",
    "plan-record", "execute-begin", "execute-end", "test",
})
SECOND_AGENT_ROLE_ENV = "WORKFLOW_AGENT_ROLE"


def _recursion_refusal(command: str, job_command: str | None = None) -> dict | None:
    """The structured refusal when a second_agent process asks for a refused command."""
    if os.environ.get(SECOND_AGENT_ROLE_ENV, "").strip().lower() != "second_agent":
        return None
    normalized = (command or "").strip().lower()
    if normalized not in RECURSION_REFUSED_COMMANDS:
        return None
    meta = {"command": normalized}
    if normalized in {"submit", "await"} and job_command:
        meta["job_command"] = job_command
    return make_error(
        "recursion_refused",
        f"'{normalized}' is refused inside a second_agent process "
        f"({SECOND_AGENT_ROLE_ENV}=second_agent): a delegated call cannot delegate again",
        next_action="Gather the evidence with your own read tools and return it; only the "
        "main agent dispatches workflow commands.",
        meta=meta,
    )


def run(
    command: str,
    task: str,
    session_id: str,
    work_dir: str | None = None,
    model: str | None = None,
    on_progress=None,
    allow_reuse: bool = True,
    require_provider_session: bool = False,
    main_host: str | None = None,
) -> dict:
    normalized_command = command.strip().lower()
    refusal = _recursion_refusal(normalized_command)
    if refusal is not None:
        return refusal
    if normalized_command in INTERNAL_COMMANDS:
        # An internal route reached by name is a caller that skipped the stage that owns
        # it. Refused here, not in the executor: the executor is exactly where the owning
        # stage calls it from.
        return make_error(
            "routing_error",
            f"'{normalized_command}' is an internal stage, not a command",
            next_action="Use a supported command (explore/plan/analyze/verify/sweep).",
            meta={"command": normalized_command},
        )
    project_root = detect_project_root(work_dir)

    if normalized_command == "init":
        try:
            # Which host ran /.init decides which provider seed the project starts from
            # (second_agent.seed.<host>.json); main() records --host here.
            meta = ensure_workflow_workspace(project_root, host=_INIT_HOST)
        except (OSError, ValueError) as exc:
            return make_error(
                "workflow_init_error",
                str(exc),
                next_action="Fix the reported workspace path/config issue, then retry init.",
                meta={"project_root": str(project_root)},
            )
        # init scaffolds; it deliberately never rewrites an existing config.json — that is
        # upgrade's job, and only upgrade holds the active-job guard for it. Say so, or a
        # workspace built by an older tool looks freshly initialized until the next
        # delegated call prints the drift warning out of nowhere.
        _warn_if_workspace_stale(work_dir)
        return {"ok": True, "content": "workflow workspace initialized", "meta": meta}

    if normalized_command == "doctor":
        config = load_provider_config_for(project_root)
        if main_host is not None:
            return run_doctor(project_root, config.get("provider_command"), session_id, main_host=main_host)
        return run_doctor(project_root, config.get("provider_command"), session_id)

    if normalized_command == "upgrade":
        try:
            meta = upgrade_workflow_workspace(project_root)
        except (OSError, ValueError) as exc:
            return make_error(
                "workflow_upgrade_error",
                str(exc),
                next_action=(
                    "Initialize the workspace first or finish active jobs, then retry upgrade."
                ),
                meta={"project_root": str(project_root)},
            )
        lines = ["workflow workspace upgraded"]
        for step in meta.get("migrations") or []:
            lines.append(
                f"migrated layout {step.get('from')} -> {step.get('to')}: moved {len(step.get('moved') or [])} item(s) "
                f"into .workflow/data/, removed {', '.join(step.get('removed') or []) or 'nothing'}; "
                f"backup {step.get('backup')}"
            )
            if step.get("config_stripped"):
                lines.append(f"config.json now holds overrides only; removed: {', '.join(step['config_stripped'])}")
            if step.get("secrets_converted"):
                lines.append("e2e/secrets.json converted to the profile-list format (original in the backup)")
            if step.get("stray"):
                lines.append(f"left untouched at the .workflow root (not recognised): {', '.join(step['stray'])}")
        return {
            "ok": True,
            "content": "\n".join(lines),
            "meta": meta,
        }

    if normalized_command == "clean":
        from exoma.core.evidence import fact_store

        locks = JOB_MANAGER.release_stale_session_locks()
        summary = JOB_MANAGER.prune_jobs()
        facts = fact_store.prune(project_root)
        from exoma.core.evidence.e2e import knowledge as e2e_knowledge

        browser_knowledge = e2e_knowledge.prune(project_root)
        from exoma.core.workspace.runtime_lock import prune_runtime_guards

        # Before the session sweep, so a guard is judged by its lock, not dropped with its dir.
        guards = prune_runtime_guards(workflow_paths(project_root)["sessions_dir"])
        sessions = prune_sessions(project_root)
        from exoma.core.graph import graph_index

        # Only a lock no worker beats any more; a live refresh is never touched, and no
        # process is ever killed from here (DEC-042).
        graph_lock = graph_index.release_stale_refresh_lock(project_root)
        return {
            "ok": True,
            "content": (
                f"released {len(locks['released'])} stale session lock(s), "
                f"kept {locks['kept']}; "
                f"graph refresh lock {'removed (stale)' if graph_lock['removed'] else graph_lock['state']}; "
                f"pruned {summary['removed']} job(s), kept {summary['kept']}; "
                f"logs removed {summary['logs_removed']}; "
                f"facts kept {facts['kept']}, dropped {facts['removed']} stale; "
                f"browser knowledge kept {browser_knowledge['kept']}, dropped {browser_knowledge['removed']}; "
                f"runtime guards removed {guards['removed']}, kept {guards['kept']}; "
                f"sessions removed {sessions['removed']}, kept {sessions['kept']}"
            ),
            "meta": {**summary, "locks": locks, "graph_refresh_lock": graph_lock, "facts": facts, "e2e_knowledge": browser_knowledge, "runtime_guards": guards, "sessions": sessions},
        }

    if normalized_command == "inspect":
        return _inspect(project_root, session_id)

    if normalized_command == "provider":
        from exoma.core.provider import provider_select
        return provider_select.run(project_root, task)

    if normalized_command == "test":
        return _test_command(project_root, session_id, task, main_host)

    if normalized_command == "verify-prepare":
        from exoma.core.evidence import verify_tests
        return verify_tests.prepare(project_root, session_id)

    if normalized_command == "sweep":
        output = run_sweep(project_root, session_id)
        output["session_id"] = session_id
        return output

    # The three promote stages. Staged as separate --command values rather than one
    # command with a mode flag, following submit/await/status/result: that is already
    # how this CLI expresses a flow whose steps are driven from outside.
    #
    # They sit above the runtime lock with the other local commands. The lock serialises
    # delegated calls against one provider session; promote never reaches a provider.
    #
    # The split exists because approval happens between the stages. A CLI cannot ask the
    # user anything — AskUserQuestion lives in the main agent's thread — so verification
    # ends at a verdict, the agent runs the review, and writing is a separate call that
    # can only happen after it.
    if normalized_command in ("promote-verify", "promote-validate", "promote-write"):
        return _promote(normalized_command, project_root, task)

    # The plan -> execute chain. Local like promote: nothing here reaches a provider, and
    # execute-begin must answer while a delegated job of the same session may hold the lock.
    if normalized_command in ("plan-record", "execute-begin", "execute-end"):
        try:
            return _execution_command(normalized_command, project_root, session_id, task)
        except (OSError, ValueError) as exc:
            return make_error("runtime_lock", str(exc), next_action="Inspect execution state and retry after the active mutation.")

    lock_path = workflow_paths(project_root, session_id)["lock"]
    lock_claim = acquire_runtime_lock(lock_path, normalized_command, session_id)
    if not lock_claim.get("ok"):
        payload = lock_claim.get("payload") or {}
        return make_error(
            "runtime_lock",
            f"runtime lock active for session {payload.get('session_id') or 'unknown'}",
            next_action="Wait for the in-flight call on this session to finish, then retry.",
            meta={"lock": payload, "lock_path": str(lock_path)},
        )
    try:
        verification_execution = execution.execution_identity(execution.current_execution(project_root, session_id))
        provider_sessions = _session_manager_for(project_root)
        try:
            session = provider_sessions.load_or_create(_session_storage_id(session_id))
        except (OSError, ValueError) as exc:
            return make_error(
                "session_state_error",
                str(exc),
                next_action="Repair or remove the project-local provider session file, then retry.",
                meta={"project_root": str(project_root)},
            )
        if require_provider_session and not session.get("provider_session_id"):
            return make_error(
                "session_capture_failed",
                "cannot recover interrupted job because no provider session ID was captured",
                next_action=(
                    "The session lock was released. Run the original task again as a clean invocation."
                ),
                meta={"reason": "recovery_session_unavailable"},
            )
        output = EXECUTOR.execute(
            command,
            task,
            session,
            work_dir,
            model,
            on_progress=on_progress,
            allow_reuse=allow_reuse,
            session_manager=provider_sessions,
            workflow_session_id=session_id,
            _runtime_lock=lock_claim,
        )
        finalize_as = normalized_command
        if normalized_command == "verify-browser":
            # A browser run ends in the canonical [VERIFICATION] and is judged as one
            # (meta.command == "verify"); a draft is not a verification at all.
            finalize_as = str((output.get("meta") or {}).get("command") or normalized_command)
        output = _finalize_verify_result(finalize_as, output)
        if finalize_as == "verify":
            try:
                execution.record_verification(project_root, session_id, output, expected=verification_execution)
            except (OSError, ValueError) as exc:
                output.setdefault("meta", {})["execution_state_error"] = f"{type(exc).__name__}: {exc}"
        try:
            provider_sessions.record_run(session, command)
        except (OSError, ValueError) as exc:
            output.setdefault("meta", {})[
                "session_history_error"
            ] = f"{type(exc).__name__}: {exc}"
        output["session_id"] = session_id
        return output
    finally:
        release_runtime_lock(lock_path, session_id, lock_claim.get("token"))


def _warn_if_workspace_stale(work_dir: str | None) -> None:
    """Warn when the workspace version differs; upgrade remains explicit."""
    try:
        project_root = detect_project_root(work_dir)
        if not needs_upgrade(project_root):
            return
        versions = workspace_versions(project_root)
    except (OSError, ValueError):
        return
    print(
        f"[workflow] WARN: .workflow was built by tool "
        f"{versions['installed_tool_version']} / config {versions['installed_config_version']}, "
        f"running {versions['current_tool_version']} / {versions['current_config_version']} — "
        f'run: exoma upgrade --work-dir "{project_root}"',
        file=sys.stderr,
    )


def probe_second_agent(work_dir: str | None = None, model: str | None = None) -> dict:
    """Ask the second agent a trivial question in a fresh session.

    This is the only signal that separates "rate limited, still coming back" from
    "hung, never coming back" — the worker PID looks identical in both cases.
    """
    project_root = detect_project_root(work_dir)
    config = load_provider_config_for(project_root)
    from exoma.adapters.contract.registry import resolve_adapter, selected_provider

    overrides = (
        {"command": config["provider_command"]}
        if config.get("provider_command")
        else {}
    )
    # Named explicitly rather than left to be re-derived from `config`: the merged config
    # always carries a `provider` key, defaulted when the file never chose one, so passing
    # it here probed whichever provider the DEFAULT named while the executor ran the one
    # the workspace selected. A watchdog that tests a different CLI than the one that hung
    # is worse than no watchdog.
    adapter = resolve_adapter(
        selected_provider(project_root),
        project_root=project_root,
        config=config,
        **overrides,
    )
    return adapter.probe(
        model=model or config.get("default_model"),
        work_dir=str(project_root),
        timeout_seconds=int(
            config.get("probe_timeout_seconds", DEFAULT_PROBE_TIMEOUT_SECONDS)
        ),
    )


def _inspect(project_root, session_id: str | None = None) -> dict:
    from exoma.core.jobs.diagnostics import snapshot
    return snapshot(project_root, session_id)




# Defined above the `__main__` block on purpose: that block runs top to bottom when the
# file is executed as a script, so a helper placed after it does not exist yet when called.
def _browser_exit_commands(command: str, result: dict, job_command: str) -> tuple[str, str]:
    """verify-browser exits like verify once it ran a scenario; a draft exits like any
    non-verify command. Decided by the result's meta.command, which the runner sets."""
    via_job = command in {"await", "result"}
    if (job_command if via_job else command) != "verify-browser":
        return command, job_command
    payload = result
    if command == "result" and isinstance(result, dict) and isinstance(result.get("output"), dict):
        payload = result["output"]
    meta = (payload.get("meta") or {}) if isinstance(payload, dict) else {}
    mapped = "verify" if meta.get("command") == "verify" else "verify-browser"
    return (command, mapped) if via_job else (mapped, job_command)


# `--host` of `exoma init`: the host whose provider seed a new project copies.
_INIT_HOST: str | None = None

COMMANDS = (
    "init",
    "upgrade",
    "doctor",
    "explore",
    "plan",
    "analyze",
    "verify",
    "verify-prepare",
    "verify-browser",
    "sweep",
    "clean",
    "inspect",
    "provider",
    "promote-verify",
    "promote-validate",
    "promote-write",
    "plan-record",
    "execute-begin",
    "execute-end",
    "test",
    "report",
    "audit",
    "graph-meta",
    "submit",
    "await",
    "status",
    "result",
    "worker",
    "cancel",
)


def _run_check(argv: list[str]) -> int:
    from exoma import check

    sys.argv = ["exoma check", *argv]
    return check.main()


def _run_install(argv: list[str]) -> int:
    from exoma import install

    sys.argv = ["exoma install", *argv]
    return install.main()


def _run_update(argv: list[str]) -> int:
    from exoma import self_update

    return self_update.main(argv)


def _run_session(argv: list[str]) -> int:
    from exoma.core.audit import session_export

    return session_export.main(argv)


# Entry points that keep their own argument parser: `exoma check ...` is the old
# `python check.py ...`, `exoma install ...` the old `python install.py ...`.
_DELEGATED_ENTRY_POINTS = {"check": _run_check, "install": _run_install, "update": _run_update,
                           "session": _run_session}


def _normalize_argv(argv: list[str]) -> list[str]:
    """`exoma <command> ...` is `--command <command> ...`; the flag form stays accepted."""
    if argv and argv[0] in COMMANDS:
        return ["--command", argv[0], *argv[1:]]
    return argv


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json

    # A Windows pipe defaults to the ANSI codepage, so the text reports (`install --check`,
    # `session export`) reached the agent with every em dash as a replacement character.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = _normalize_argv(list(sys.argv[1:] if argv is None else argv))
    if argv and argv[0] in _DELEGATED_ENTRY_POINTS:
        raise SystemExit(_DELEGATED_ENTRY_POINTS[argv[0]](argv[1:]))
    if len(argv) >= 2 and argv[0] in ("--command", "-c") and argv[1] == "update":
        raise SystemExit(_run_update(argv[2:]))
    if argv in (["--version"], ["-V"]):
        from exoma.config.settings import TOOL_VERSION

        print(f"exoma {TOOL_VERSION}")
        raise SystemExit(0)

    parser = argparse.ArgumentParser(
        prog="exoma", description="Exoma CLI",
        epilog="Package commands: exoma install, exoma check, exoma update [--check | --status], exoma session export",
    )
    parser.add_argument(
        "--command",
        "-c",
        required=True,
        choices=COMMANDS,
    )
    parser.add_argument("--prompt", "-p", default=None)
    parser.add_argument(
        "--prompt-file",
        default=None,
        help="path to file containing the prompt (alternative to --prompt)",
    )
    parser.add_argument("--session", "-s", default="default")
    parser.add_argument(
        "--fresh-session",
        action="store_true",
        help="force a new main session ID and bypass evidence reuse",
    )
    parser.add_argument(
        "--work-dir",
        "-w",
        default=None,
        help="project directory context for cache keys (default: cwd)",
    )
    parser.add_argument(
        "--model",
        "-m",
        default=None,
        help="model override for the selected second_agent provider (e.g. provider/model_key for OpenCode)",
    )
    parser.add_argument(
        "--job-id", default=None, help="job ID for status/result/worker"
    )
    parser.add_argument(
        "--job-command",
        default="explore",
        choices=["explore", "plan", "analyze", "verify", "verify-browser"],
        help="workflow command to execute asynchronously via submit",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_JOB_POLL_INTERVAL_SECONDS,
        help="seconds between job status polls for await mode",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=DEFAULT_JOB_POLL_TIMEOUT_SECONDS,
        help="max seconds to wait in await mode; 0 means wait forever",
    )
    parser.add_argument("--pretty", action="store_true", help="pretty print output")
    parser.add_argument("--main-host", choices=("claude", "agy", "codex", "opencode"), default=None,
                        help="presentation host for first-party guidance only; does not select a provider or grant permission")
    parser.add_argument(
        "--host",
        choices=("claude", "agy", "codex", "opencode"),
        default=None,
        help="init only: the main-agent host running it, which picks that host's provider "
        "seed (second_agent.seed.<host>.json) for the new project",
    )
    args = parser.parse_args(argv)
    # Before the prompt checks and any session or job side effect, so the refusal is the
    # structured result on stdout rather than an argument error.
    refusal = _recursion_refusal(args.command, args.job_command)
    if refusal is not None:
        print(json.dumps(refusal, indent=2) if args.pretty else json.dumps(refusal))
        raise SystemExit(_verify_exit_code(args.command, refusal, args.job_command))
    global _INIT_HOST
    _INIT_HOST = args.host

    work_dir = str(Path(args.work_dir).resolve()) if args.work_dir else str(Path.cwd())

    prompt = args.prompt
    if args.prompt_file:
        if prompt:
            raise SystemExit("cannot use both --prompt and --prompt-file")
        try:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SystemExit(f"prompt file not found: {args.prompt_file}")
        except IsADirectoryError:
            raise SystemExit(f"prompt file is a directory: {args.prompt_file}")
        except OSError as exc:
            raise SystemExit(f"cannot read prompt file: {exc}")
    if (
        args.command in {"explore", "plan", "analyze", "verify", "verify-browser", "submit", "await"}
        and not prompt
    ):
        raise SystemExit("--prompt or --prompt-file is required for this command")
    if args.command in {"status", "result", "worker", "cancel"} and not args.job_id:
        raise SystemExit("--job-id is required for this command")
    # The local commands that read or write one session's state need that session named:
    # resolved from `default`, a plan or an execution lands in whichever session the cache
    # holds, not the one the host bound. A delegated command run by hand keeps the cache —
    # a person at a terminal has one session — and run.sh refuses `default` for those.
    from exoma.core.runtime.scripts import SESSION_REQUIRED

    if (
        args.command in SESSION_REQUIRED
        and args.command not in {"explore", "plan", "analyze", "verify", "verify-browser"}
        and args.session == "default"
        and not (os.environ.get("EXOMA_ALLOW_DEFAULT_SESSION") or os.environ.get("AI_PROXY_ALLOW_DEFAULT_SESSION"))
    ):
        raise SystemExit(
            f"--session is required for {args.command}: it reads or writes session state. "
            "Pass MAIN_SESSION_ID (the value from the [SESSION BINDING] block); "
            "to use the cached session deliberately, set EXOMA_ALLOW_DEFAULT_SESSION=1."
        )

    effective_session = resolve_session_id(
        args.session, fresh=args.fresh_session, project_root=work_dir
    )
    allow_reuse = not args.fresh_session
    if args.command == "submit":
        result = submit(
            args.job_command,
            prompt,
            effective_session,
            work_dir,
            args.model,
            allow_reuse,
        )
    elif args.command == "await":
        result = await_job(
            args.job_command,
            prompt,
            effective_session,
            work_dir,
            args.model,
            args.poll_interval,
            args.poll_timeout,
            allow_reuse,
        )
    elif args.command == "report":
        from exoma.core.audit import telemetry
        from exoma.core.evidence.contract import make_ok

        root = detect_project_root(work_dir)
        result = make_ok("", meta={"command": "report", "project_root": str(root)})
        result["report"] = telemetry.report(root)
    elif args.command == "audit":
        from exoma.core.audit import audit
        from exoma.core.evidence.contract import make_ok

        root = detect_project_root(work_dir)
        result = make_ok("", meta={"command": "audit", "project_root": str(root)})
        result["audit"] = audit.report(root)
    elif args.command == "graph-meta":
        from exoma.core.graph import graph_meta
        from exoma.core.evidence.contract import make_error, make_ok

        root = detect_project_root(work_dir)
        built = graph_meta.build(root)
        if not built.get("ok"):
            result = make_error(
                "workflow_init_error",
                str(built.get("reason") or "graph sidecar could not be built"),
                next_action="Run `graphify update` so graphify-out/graph.json exists, then retry.",
                meta={"command": "graph-meta", "project_root": str(root)},
            )
        else:
            result = make_ok("", meta={"command": "graph-meta", "project_root": str(root)})
            result["graph_meta"] = {"built": built, "verified": graph_meta.verify(root)}
    elif args.command == "status":
        result = get_status(args.job_id)
    elif args.command == "result":
        result = get_result(args.job_id)
    elif args.command == "cancel":
        result = JOB_MANAGER.cancel(args.job_id, args.session, args.work_dir or str(Path.cwd()),
                                    reason=args.prompt or "requested by user")
    elif args.command == "worker":
        result = run_worker(args.job_id)
    elif should_run_in_background(args.command):
        result = submit(
            args.command,
            prompt,
            effective_session,
            work_dir,
            args.model,
            allow_reuse,
        )
    else:
        result = run(
            args.command,
            prompt,
            effective_session,
            work_dir,
            args.model,
            allow_reuse=allow_reuse,
            main_host=args.main_host,
        )
    exit_job_command = args.job_command
    if args.command == "result" and args.job_id:
        stored_job = JOB_MANAGER.get_job(args.job_id)
        if stored_job:
            exit_job_command = str(stored_job.get("command") or exit_job_command)
    result = _slim_result(result)
    from exoma.config.hosts import present_result
    result = present_result(result, args.main_host, args.command)
    print(json.dumps(result, indent=2) if args.pretty else json.dumps(result))
    exit_command, exit_job_command = _browser_exit_commands(args.command, result, exit_job_command)
    raise SystemExit(_verify_exit_code(exit_command, result, exit_job_command))


if __name__ == "__main__":
    main()
