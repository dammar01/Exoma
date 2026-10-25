"""Config and state defaults, their merge rules, and the policy readers."""

from exoma.config.settings import DEFAULT_PROVIDER
from exoma.core.workspace.workspace_paths import CONFIG_VERSION
from exoma.core.workspace.workspace_paths import _tool_paths
from exoma.core.workspace.workspace_paths import read_json_file
from exoma.core.workspace.workspace_paths import slugify_project_name
from exoma.core.workspace.workspace_paths import workflow_paths
from pathlib import Path


VERIFY_MODES = ("delegated", "syntax")

def default_commands() -> dict:
    return {
        # --- prompt-only: read by main_agent, NOT by this runtime ---
        "allow_analyze_to_plan": True,
        "allow_explore_to_plan": True,
        "auto_sweep_after_execute": True,
        # whether /.execute chains into /.verify on its own. false => /.execute reports
        # `verification: not_run` and must never call itself done. The chain itself is
        # prompt-only: core/runtime/execution.py (DEC-067) records not_run at execute-end,
        # but nothing in the runtime starts /.verify.
        "auto_verify_after_execute": False,
        # --- runtime-consumed ---
        # how deep /.verify goes when it does run:
        #   delegated -> full verification by second_agent
        #   syntax    -> local parse/name check on changed files, no test suite
        "verify_mode": "delegated",
        # tests /.verify may run (core/evidence/verify_tests.py): main_agent writes the
        # commands it chose to the session's verify/tests.json, and the runtime runs only
        # those starting with one of these prefixes, without a shell. Empty: nothing runs,
        # and a requested test is reported as not run.
        "verify_test_commands": [],
        "verify_test_timeout_seconds": 900,
    }

def default_policies() -> dict:
    return {
        # --- prompt-only ---
        "workflow_prefix": "/.",
        "chat_mode_for_plain_text": True,
        "fallback_requires_confirmation": True,
        "max_active_job_per_session": 1,
        # --- runtime-consumed (core/evidence/fact_store.py) ---
        "fact_relevant_limit": 3,
        # per-role limit; a role absent here falls back to fact_relevant_limit
        "fact_relevant_limits": {"exploration": 3, "reasoning": 4},
        # weighted overlap a fact needs to be served: identifier 2, word 1, path-only 0.5
        "fact_min_relevance": 2.0,
        "fact_recurrence_threshold": 5,
        # claims longer than this are refused at ingest, never truncated
        "fact_max_claim_chars": 400,
        # store ceiling; past it the least-used, least-recent facts go first
        "fact_max_facts": 500,
        # --- runtime-consumed (core/graph/graph_index.py) ---
        # inject a ranked file shortlist from graphify-out/graph.json into evidence prompts
        "graph_leads_enabled": True,
        # --- runtime-consumed (core/knowledge/) ---
        # the branch /.promote treats as production. Promotion is refused anywhere else:
        # promoted knowledge describes what is live, and a hypothesis written on a
        # feature branch would enter the shared repository wearing that authority.
        "production_branch": "main",
        # where promoted knowledge files live, relative to the project root. Tracked by
        # Git on purpose — this is the one workflow artifact that is NOT under the
        # gitignored .workflow/ tree, because its whole value is being shared.
        "knowledge_dir": "docs/project-knowledge",
        # how many promoted knowledge documents may ride along in one delegated prompt.
        "knowledge_relevant_limit": 3,
        # --- hook-consumed (execution-gate.py, status-guard.py; DEC-067) ---
        # hybrid: deterministic gates block (write outside root, write outside an active
        # execution's scope, git commit the user did not ask for) and the done-claim
        # guard warns; warn: everything only reports; off: no enforcement hooks act.
        "enforcement": "hybrid",
        # paths outside the project root a write may land in, beyond the temp dir and
        # ~/.claude. The user adds a path here once they have approved writing there.
        "write_allowed_outside_root": [],
        # --- runtime-consumed (core/prompt/prompt_builder.py) ---
        # ask second_agent to spawn one sub-agent per graph cluster (or per role slice
        # when there is no graph). ON: a delegated call that reads the codebase serially
        # is the slow path, and the fan-out instruction is what makes the second agent
        # use the parallel tools it already has. It does cost more quota per call and
        # produces a larger response — set false to go back to a single serial reader.
        "subagent_fanout_enabled": True,
    }

def default_e2e() -> dict:
    """Project-level defaults for /.verify-browser, mirroring the request contract.

    Single source: the shipped values live in `core/evidence/e2e/request.py`, and this
    section is what a project pins so a run needs no interview. The import is lazy
    because request.py reads this file's section back — module-level it would cycle.
    """
    from exoma.core.evidence.e2e.request import default_settings

    return default_settings()


def _tunable_sections() -> tuple[tuple[str, dict], ...]:
    """The user-tunable config sections and the defaults each is checked against."""
    return (
        ("commands", default_commands()),
        ("policies", default_policies()),
        ("e2e", default_e2e()),
    )


def _rewrite_superseded_keys(config: dict) -> bool:
    """Carry a config written by an earlier build onto the current key names.

    Additive backfill alone cannot do this: it would leave the retired key in place next
    to its replacement, and the user could not tell which one the runtime obeys.
    """
    commands = config.get("commands")
    if not isinstance(commands, dict) or "autoverify" not in commands:
        return False
    # `autoverify` was the single boolean that later split into two settings.
    legacy = commands.pop("autoverify")
    commands.setdefault("verify_mode", "delegated" if legacy else "syntax")
    policies = config.get("policies")
    if isinstance(policies, dict) and policies.get("fact_recurrence_threshold") == 3:
        # This compatibility rewrite only affects the short-lived schema that shipped
        # the old default; current user values are otherwise preserved.
        policies["fact_recurrence_threshold"] = default_policies()[
            "fact_recurrence_threshold"
        ]
    return True

def merge_config_defaults(config: dict) -> tuple[dict, bool]:
    """Keep config.json current WITHOUT writing defaults into it.

    config.json holds only what the user chose. The tunable sections (`commands`,
    `policies`, `e2e`) are overrides: a key that is absent means the shipped default, which
    every reader applies itself (`verify_mode`, `_policy`, `request.config_settings`, ...).
    Backfilling the defaults used to freeze each project at the build that first wrote
    them, so a default changed in a later release never reached it, and an `e2e` section
    that merely held defaults looked like a project somebody had configured.

    What still changes here: retired key names are carried onto their replacements, and
    the derived version stamps follow the running build.
    """
    changed = _rewrite_superseded_keys(config)
    if config.get("version") != CONFIG_VERSION:
        config["version"] = CONFIG_VERSION
        changed = True

    # tool_version is derived, not a user setting: left alone it pins an upgraded
    # workspace to the version that first wrote it, and `doctor` then reports a
    # tool version the runtime is no longer running.
    from exoma.config.settings import COMPONENT_VERSIONS, TOOL_VERSION

    runtime = config.get("runtime")
    if isinstance(runtime, dict):
        if runtime.get("tool_version") != TOOL_VERSION:
            runtime["tool_version"] = TOOL_VERSION
            changed = True
        # runtime_version is derived like tool_version; backfill so an existing config
        # gains the component stamp the lazy-upgrade gate reads.
        if runtime.get("runtime_version") != COMPONENT_VERSIONS["runtime"]:
            runtime["runtime_version"] = COMPONENT_VERSIONS["runtime"]
            changed = True
    return config, changed

def effective_section(config: dict, section: str) -> dict:
    """One tunable section as the runtime applies it: shipped defaults, then overrides."""
    defaults = dict(dict(_tunable_sections())[section])
    current = config.get(section)
    if isinstance(current, dict):
        defaults.update({k: v for k, v in current.items() if k in defaults})
    return defaults


def strip_to_overrides(config: dict) -> list[str]:
    """Remove what an override-only config.json must not hold. Returns what was removed.

    Two kinds of key go: a value identical to the shipped default (it overrides nothing,
    and left in place it would silently pin the project if that default ever changes), and
    a key the tunable section does not know (a retired or misspelled knob that does
    nothing). A section left empty is removed too. Used by the upgrade migration, which
    reports every removal.
    """
    removed: list[str] = []
    for section, defaults in _tunable_sections():
        current = config.get(section)
        if not isinstance(current, dict):
            continue
        for key in list(current):
            if key not in defaults:
                removed.append(f"{section}.{key} (unknown or retired)")
                del current[key]
            elif current[key] == defaults[key]:
                removed.append(f"{section}.{key} (same as the shipped default)")
                del current[key]
        if not current:
            del config[section]
    return removed


def diverged_defaults(config: dict) -> list[dict]:
    """Settings whose stored value differs from what this build ships as the default.

    Reported, never rewritten. A value already in the file may be a deliberate choice
    or may just be the previous build's default frozen in place, and this code cannot
    tell them apart — the backfill is additive precisely so it never has to guess. What
    it can do is stop the difference from being invisible: a default that changed
    between builds otherwise reaches only projects that never ran the old one.
    """
    out: list[dict] = []
    for section, defaults in _tunable_sections():
        current = config.get(section)
        if not isinstance(current, dict):
            continue
        for key, shipped in defaults.items():
            if key in current and current[key] != shipped:
                out.append(
                    {
                        "key": f"{section}.{key}",
                        "yours": current[key],
                        "shipped_default": shipped,
                    }
                )
    return out

def validate_config(config: dict) -> list[str]:
    """Structural sanity of config.json's user-tunable sections.

    Two silent failure modes this surfaces: an UNKNOWN key (a misspelled knob does nothing,
    yet reads as "configured"), and a TYPE mismatch (a bool knob set to a string is ignored
    by the reader that expects a bool, again silently). Reported, never fatal or rewritten —
    same posture as diverged_defaults; the runtime readers all fall back safely. Only
    commands/, policies/ and e2e/ are user knobs; version/project/runtime are structural.
    """
    warnings: list[str] = []
    for section, section_defaults in _tunable_sections():
        current = config.get(section)
        if current is None:
            continue  # an absent section means every key is at its shipped default
        if not isinstance(current, dict):
            warnings.append(f"{section}: {type(current).__name__}, expected object")
            continue
        for key, value in current.items():
            if key not in section_defaults:
                warnings.append(
                    f"{section}.{key}: unknown key (typo? the runtime ignores it)"
                )
                continue
            default = section_defaults[key]
            # bool is a subclass of int, so an int knob set to True (or a bool knob set to
            # 0/1) would slip past a plain isinstance — compare bool-ness explicitly.
            if isinstance(default, bool):
                if not isinstance(value, bool):
                    warnings.append(
                        f"{section}.{key}: {type(value).__name__}, expected bool"
                    )
            elif isinstance(value, bool) or not isinstance(value, type(default)):
                warnings.append(
                    f"{section}.{key}: {type(value).__name__}, expected {type(default).__name__}"
                )
    commands = config.get("commands")
    if isinstance(commands, dict):
        mode = commands.get("verify_mode")
        if mode is not None and mode not in VERIFY_MODES:
            warnings.append(
                f"commands.verify_mode: '{mode}' not in {VERIFY_MODES} (falls back to delegated)"
            )
    return warnings

def verify_mode(project_root: Path) -> str:
    """commands.verify_mode. Anything unreadable or unrecognised falls back to
    'delegated' — an unclear setting must not silently downgrade verification."""
    try:
        config = read_json_file(workflow_paths(project_root)["config"])
    except (OSError, ValueError):
        return "delegated"
    commands = config.get("commands")
    if not isinstance(commands, dict):
        return "delegated"
    mode = commands.get("verify_mode", "delegated")
    return mode if mode in VERIFY_MODES else "delegated"

def auto_verify_after_execute(project_root: Path) -> bool:
    """commands.auto_verify_after_execute.

    Still prompt-only in effect — /.execute has a runtime path since DEC-067
    (core/runtime/execution.py), but it only records `verification: not_run`; nothing
    here can make it chain into /.verify. What this does buy: the value now rides out on every
    delegated result, so main_agent reads the project's actual setting instead of
    recalling what the config said.
    """
    try:
        config = read_json_file(workflow_paths(project_root)["config"])
    except (OSError, ValueError):
        return bool(default_commands()["auto_verify_after_execute"])
    commands = config.get("commands")
    if not isinstance(commands, dict):
        return bool(default_commands()["auto_verify_after_execute"])
    return bool(
        commands.get(
            "auto_verify_after_execute",
            default_commands()["auto_verify_after_execute"],
        )
    )

def graph_leads_enabled(project_root: Path) -> bool:
    """policies.graph_leads_enabled. Defaults to on; an unreadable config must not
    silently disable the shortlist (a missing graph already degrades to no leads)."""
    try:
        config = read_json_file(workflow_paths(project_root)["config"])
    except (OSError, ValueError):
        return True
    policies = config.get("policies")
    if not isinstance(policies, dict):
        return True
    return bool(policies.get("graph_leads_enabled", True))

def _policy(project_root: Path, key: str, fallback):
    """One policy value, falling back rather than failing.

    An unreadable config must not decide policy by accident: every reader here follows
    graph_leads_enabled's precedent and degrades to the declared default, so a malformed
    file changes what the user can fix, not what the runtime silently does.
    """
    try:
        config = read_json_file(workflow_paths(project_root)["config"])
    except (OSError, ValueError):
        return fallback
    policies = config.get("policies")
    if not isinstance(policies, dict):
        return fallback
    value = policies.get(key, fallback)
    # `bool` subclasses `int`, so a plain isinstance check accepts `true` where an integer
    # is expected and hands back 1 — a config that reads as "on" silently becoming a limit
    # of one document. The int case has to exclude bool explicitly; nothing else can.
    if isinstance(fallback, bool):
        acceptable = isinstance(value, bool)
    elif isinstance(fallback, int):
        acceptable = isinstance(value, int) and not isinstance(value, bool)
    else:
        acceptable = isinstance(value, type(fallback))
    return value if acceptable else fallback


def production_branch(project_root: Path) -> str:
    """policies.production_branch. The only branch /.promote will write from."""
    return _policy(project_root, "production_branch", default_policies()["production_branch"])


def knowledge_relevant_limit(project_root: Path) -> int:
    """policies.knowledge_relevant_limit, floored at zero (0 turns the sidecar off)."""
    value = _policy(
        project_root, "knowledge_relevant_limit", default_policies()["knowledge_relevant_limit"]
    )
    return max(0, int(value))


def subagent_fanout_enabled(project_root: Path) -> bool:
    """policies.subagent_fanout_enabled. Defaults to ON.

    An unreadable config falls back to the default rather than to off: the previous
    fail-closed behaviour meant a malformed config silently downgraded every call to a
    serial read, with nothing in the output saying so.
    """
    try:
        config = read_json_file(workflow_paths(project_root)["config"])
    except (OSError, ValueError):
        return True
    policies = config.get("policies")
    if not isinstance(policies, dict):
        return True
    return bool(policies.get("subagent_fanout_enabled", True))

def default_config(project_root: Path, agent_workflow_path: str | None = None) -> dict:
    project_name = project_root.name
    tool = _tool_paths(agent_workflow_path)
    return {
        "version": CONFIG_VERSION,
        "project": {
            "name": project_name,
            "slug": slugify_project_name(project_name),
            "root": str(project_root),
        },
        "runtime": {
            "python": tool["python"],
            "package_root": tool["package_root"],
            "tool_version": tool["tool_version"],
            "runtime_version": tool["runtime_version"],
            # Live selector as of v3.4.3, not a label: adapters.contract.registry resolves the
            # adapter from this value.
            "second_agent": DEFAULT_PROVIDER,
            "main_agent": "agnostic",
            "provider_config": ".workflow/second_agent.json",
            "sessions_dir": ".workflow/data/sessions",
            "per_session_layout": "sessions/<session_id>/: state.json, scope.json, command-cache.json, runtime/{prompt.txt,response.last.md,prompt.meta.json,lock}, logs/",
        },
        # Overrides only: an absent key is the shipped default, applied by each reader.
        # Nothing here is copied from default_commands()/default_policies()/default_e2e().
        "commands": {},
        "policies": {},
    }

def default_state(project_root: Path) -> dict:
    project_name = project_root.name
    return {
        "version": 1,
        "project": {
            "name": project_name,
            "root": str(project_root),
            "slug": slugify_project_name(project_name),
        },
        "session": None,
        "workflow": {
            "stage": "initialized",
            "last_command": None,
            "objective": None,
            "plan_readiness": "unknown",
        },
        "context": {
            "evidence_summary": [],
            "affected_files": [],
            "affected_symbols": [],
            "assumptions": [],
            "risks": [],
            "open_questions": [],
        },
        "guards": {
            "state_version": 1,
            "scope_version": 0,
            "last_prompt_id": None,
        },
    }

def default_scope() -> dict:
    return {
        "session_id": None,
        "editable_scope": [],
        "readable_scope": [],
        "impact_radius": [],
        "out_of_scope": [],
        "verification_targets": [],
        "post_edit_checks": [
            "git_diff",
            "changed_symbol_reference_search",
            "caller_callee_check",
            "type_contract_check",
            "config_flag_check",
            "ui_api_consumer_check",
        ],
    }

def default_command_cache() -> dict:
    return {
        "session_id": None,
        "last_explore_result": None,
        "last_analyze_result": None,
        "last_plan_result": None,
        "last_execute_diff": None,
        "last_sweep_result": None,
    }
