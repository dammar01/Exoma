import hashlib
import os
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from exoma.config.routing import COMMAND_ROUTES

# The installed `exoma` package directory, and the directory that holds it (what a child
# interpreter needs on PYTHONPATH to run `-m exoma` from a source checkout).
PACKAGE_DIR = Path(__file__).resolve().parent.parent
PACKAGE_PARENT = PACKAGE_DIR.parent
# What the installer ships (skills, hooks, provider bundles) and the manifest that hashes
# it. Read in place: the package is installed unpacked (uv_build/pip wheels), never zipped.
BUNDLE_DIR = PACKAGE_DIR / "_bundle"
BUNDLE_CONFIG_DIR = BUNDLE_DIR / "config"
BUNDLE_MANIFEST = BUNDLE_DIR / "manifest.json"
E2E_REQUIREMENTS_FILE = BUNDLE_DIR / "requirements-e2e.txt"
PROVIDER_EXAMPLE_FILE = PACKAGE_DIR / "config" / "second_agent.example.json"


# Runtime knobs are read from `EXOMA_<NAME>`. Before the package was renamed they were
# `AI_PROXY_<NAME>`, and those spellings are still read as a fallback so an existing shell
# profile keeps working; `doctor` names every legacy variable it finds set
# (`legacy_env_in_use`). The new spelling wins when both are set.
ENV_PREFIX = "EXOMA_"
LEGACY_ENV_PREFIX = "AI_PROXY_"


def legacy_env_name(name: str) -> str | None:
    """The pre-rename spelling of an `EXOMA_*` variable, or None for any other name."""
    if name.startswith(ENV_PREFIX):
        return LEGACY_ENV_PREFIX + name[len(ENV_PREFIX):]
    return None


def getenv(name: str, default=None):
    """`os.getenv` that falls back from `EXOMA_<X>` to the legacy `AI_PROXY_<X>`."""
    value = os.environ.get(name)
    if value is None:
        legacy = legacy_env_name(name)
        if legacy is not None:
            value = os.environ.get(legacy)
    return default if value is None else value


# The `<NAME>` part of every `EXOMA_<NAME>` the runtime reads with the legacy fallback.
# Only these are reported by `legacy_env_in_use`: an `AI_PROXY_*` name outside the list was
# never read (an `AI_PROXY_OPENCODE_COMMAND` is not the `OPENCODE_COMMAND` override), and
# telling the user to rename it would promise an effect it never had. `ALLOW_DEFAULT_SESSION`
# is read by the generated `run.sh` (`core/runtime/scripts.py`), `MODEL_CATALOG` by
# `core/provider/model_catalog.py`; the rest below.
LEGACY_ENV_NAMES = frozenset({
    "PROVIDER", "MAIN_SESSION_CACHE_TTL_SECONDS", "TIMEOUT_SECONDS", "BOOTSTRAP_TIMEOUT_SECONDS",
    "POLL_INTERVAL_SECONDS", "STALL_THRESHOLD_SECONDS", "IDLE_STALL_SECONDS",
    "PROBE_TIMEOUT_SECONDS", "PROBE_RECHECK_SECONDS", "MAX_PROBES", "JOB_MAX_RUNTIME_SECONDS",
    "MAX_TASK_CHARS", "MAX_TASK_CHARS_NO_ARGV", "TASK_TRUNCATION_HARD_RATIO", "SLIM_CONTENT",
    "CONTENT_PREVIEW_CHARS", "SLIM_CONTENT_MIN_CHARS", "OPENCODE_AGENT", "FANOUT_RECHECK_HOURS",
    "JOB_POLL_INTERVAL_SECONDS", "JOB_POLL_TIMEOUT_SECONDS", "MAX_GLOBAL_WORKERS",
    "MODEL_CATALOG", "ALLOW_DEFAULT_SESSION",
})


def legacy_env_in_use(environ=None) -> list[str]:
    """Every set `AI_PROXY_*` variable that still works under its `EXOMA_*` name, sorted."""
    source = os.environ if environ is None else environ
    return sorted(
        key for key in source
        if key.startswith(LEGACY_ENV_PREFIX) and key[len(LEGACY_ENV_PREFIX):] in LEGACY_ENV_NAMES
    )


def _data_dir() -> Path:
    """Per-user state: global job records, the main-session cache, the provider seed.

    Never inside the package: an installed package directory is shared and may be
    read-only. `EXOMA_HOME` overrides the platform default.
    """
    override = os.getenv("EXOMA_HOME")
    if override and override.strip():
        return Path(override.strip()).expanduser()
    if os.name == "nt":
        base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "exoma"


DATA_DIR = _data_dir()
STORAGE_DIR = DATA_DIR / "storage"
SESSION_DIR = STORAGE_DIR / "sessions"
CACHE_FILE = STORAGE_DIR / "cache.json"
JOB_DIR = STORAGE_DIR / "jobs"
# The template `init` copies into a workspace, written by `exoma install` from the shipped
# example. Only installer and init read it; the runtime resolves a project's own
# .workflow/second_agent.json or refuses.
PROVIDER_SEED_FILE = DATA_DIR / "config" / "second_agent.seed.json"
# Which adapter serves as second_agent. Read by adapters.contract.registry; a workspace
# config.json may override it per project.
DEFAULT_PROVIDER = getenv("EXOMA_PROVIDER", "opencode")

TOOL_VERSION = "0.2.0"

# Component stamps allow lazy upgrades and may diverge when only one surface changes.
# v3.6.0 moves both surfaces: `/.verify-browser` ships a new skill and CLAUDE.md registry
# entry (prompt_bundle) and a new background command, request file and optional Playwright
# extra (runtime), so both stamps follow TOOL_VERSION.
# The stamps no longer gate script regeneration — upgrade compares generated content
# directly, so a generator change reaches existing workspaces whether or not this bumps.
#   prompt_bundle : LLM-facing contract shipped to ~/.claude (CLAUDE.md, skills, AGENTS.md)
#   runtime       : machine wiring in .workflow (run/inspect/check scripts, config schema,
#                   opencode adapter) + shipped hooks/settings
COMPONENT_VERSIONS = {
    "prompt_bundle": TOOL_VERSION,
    "runtime": TOOL_VERSION,
}


def _env_number(name: str, default, cast):
    """One env knob, parsed defensively.

    Every knob below is read at import time, so a bare `int(os.getenv(...))` turns a
    mistyped env var into an ImportError-shaped crash before argparse ever runs — and it
    takes `doctor` down with it, the one command whose job is to say what broke. The
    knobs are named `_SECONDS` and `_HOURS`, which is exactly the naming that invites
    someone to write `45m`. Degrade to the built-in default and name the culprit instead.

    Negative values are refused for the same reason: no knob here has a meaning below
    zero, and a negative timeout fails much later, far from the env var that caused it.
    Zero IS allowed — several knobs use it deliberately to mean "disabled".
    """
    raw = os.environ.get(name)
    legacy = legacy_env_name(name)
    if raw is None and legacy is not None and legacy in os.environ:
        # Warnings below name the variable the user actually set, not its new spelling.
        name, raw = legacy, os.environ[legacy]
    if raw is None or not raw.strip():
        return default
    try:
        value = cast(raw.strip())
    except (TypeError, ValueError):
        print(
            f"[config] WARN: {name}={raw!r} is not a valid {cast.__name__}; "
            f"using default {default}",
            file=sys.stderr,
        )
        return default
    if value < 0:
        print(
            f"[config] WARN: {name}={value} is negative; using default {default}",
            file=sys.stderr,
        )
        return default
    return value


def _env_int(name: str, default: int) -> int:
    return _env_number(name, default, int)


def _env_float(name: str, default: float) -> float:
    return _env_number(name, default, float)

# How long a cached "default" session stays reusable with no active job. Sliding: every
# resolve restamps it, so a session in continuous use never expires, while one left behind
# is replaced instead of capturing the next agent's work. Deliberately longer than the lock
# TTLs — a main agent idles for minutes at a time while the user types.
MAIN_SESSION_CACHE_TTL_SECONDS = _env_int(
    "EXOMA_MAIN_SESSION_CACHE_TTL_SECONDS", 3600
)

# Overall delegated-call timeout; 0 disables it.
DEFAULT_TIMEOUT_SECONDS = _env_int("EXOMA_TIMEOUT_SECONDS", 1800)
# Bootstrap only says "READY"; it must never inherit the long task budget.
DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = _env_int(
    "EXOMA_BOOTSTRAP_TIMEOUT_SECONDS", 180
)
# Gap between liveness ticks while opencode runs.
DEFAULT_POLL_INTERVAL_SECONDS = _env_float("EXOMA_POLL_INTERVAL_SECONDS", 2.0)
# No heartbeat for this long while the PID is still alive => stalled, probe it.
DEFAULT_STALL_THRESHOLD_SECONDS = _env_int(
    "EXOMA_STALL_THRESHOLD_SECONDS", 360
)
# Unlike heartbeat age, this measures how long the output stream has been idle.
DEFAULT_IDLE_STALL_SECONDS = _env_int("EXOMA_IDLE_STALL_SECONDS", 240)
DEFAULT_PROBE_TIMEOUT_SECONDS = _env_int("EXOMA_PROBE_TIMEOUT_SECONDS", 45)
# Cadence for re-probing a stalled job.
DEFAULT_PROBE_RECHECK_SECONDS = _env_int("EXOMA_PROBE_RECHECK_SECONDS", 120)
# Max fresh-session probes for one stalled job before probing stops entirely. Each probe
# spends quota, so a job that stays alive-but-silent must not re-probe forever — after the
# budget the job_max_runtime backstop is what ends it. The recheck interval also backs off
# (doubles) between probes so the budget is not spent in one tight burst.
DEFAULT_MAX_PROBES = _env_int("EXOMA_MAX_PROBES", 3)
# Hard ceiling: a job running past this is failed even if its PID looks alive.
# Backstop for the OOM case, where the worker dies in a way PID checks miss.
DEFAULT_JOB_MAX_RUNTIME_SECONDS = _env_int(
    "EXOMA_JOB_MAX_RUNTIME_SECONDS", 5400
)
# The delegated prompt is passed as ONE CLI arg (Windows caps argv at 8191 chars; the
# adapter's _too_long_for_cmd is the hard backstop). Scaffolding is fixed cost, so the
# task string is the only variable-size part worth capping here — cap it before assembly
# so a long task degrades to a visible truncation instead of a deterministic call failure.
DEFAULT_MAX_TASK_CHARS = _env_int("EXOMA_MAX_TASK_CHARS", 3000)
# The cap for transports that have no command-line limit to respect: opencode attaches
# its prompt as a file, codex writes it to stdin. Neither can overflow argv, and both
# were being cut at the number chosen for the transport that can — the arrangement was
# backwards, with the only provider under a real OS ceiling getting ~31k measured
# characters while the two under none got 3000. Zero means unbounded, which is the
# default: length was never the property worth bounding here. What matters is how wide
# the resulting change is, and `quick_verify.scope_width` measures that directly instead
# of guessing at it from the size of the instruction.
DEFAULT_MAX_TASK_CHARS_NO_ARGV = _env_int("EXOMA_MAX_TASK_CHARS_NO_ARGV", 0)
# Fraction of the task that may be cut before the call is refused instead of degraded.
# A tail trimmed off a long instruction usually costs nothing; losing a third of it means
# the second agent answered a different question than the one asked — and it answers with
# full confidence either way, which is the part that misleads.
DEFAULT_TASK_TRUNCATION_HARD_RATIO = _env_float(
    "EXOMA_TASK_TRUNCATION_HARD_RATIO", 0.2
)
# A delegated result ships the evidence text in `content` AND the path it was archived to
# in `evidence_ref.artifact_path` — the same bytes, twice. Delegation exists to keep raw
# code out of main_agent's context window, and carrying both puts it straight back in.
# Opt-in rather than default: a consumer that reads `content` without checking
# `meta.content_mode` would get a preview and have no way to notice.
SLIM_CONTENT_ENV = "EXOMA_SLIM_CONTENT"
DEFAULT_CONTENT_PREVIEW_CHARS = _env_int("EXOMA_CONTENT_PREVIEW_CHARS", 500)
# Below roughly twice the preview there is nothing to reclaim and a whole answer to lose,
# so short results are left whole no matter what the flag says.
DEFAULT_SLIM_CONTENT_MIN_CHARS = _env_int(
    "EXOMA_SLIM_CONTENT_MIN_CHARS", DEFAULT_CONTENT_PREVIEW_CHARS * 2
)
OPENCODE_COMMAND = os.getenv("OPENCODE_COMMAND", "opencode")
# The opencode agent that runs delegated calls. `plan` is opencode's own read-only
# primary, and the workflow deliberately adds no second primary: every wf-* agent ships
# as a subagent that `plan` spawns through the `task` tool.
DEFAULT_PROVIDER_AGENT = getenv("EXOMA_OPENCODE_AGENT", "plan")
# How long a learned "this project cannot fan out" verdict stands before it is retried.
# It is inferred from one sentence the second agent wrote about itself, and that sentence
# has been wrong: an agent listed `task` among its tools and still called itself incapable.
# Without an expiry the wrong verdict is permanent, because a project with fan-out off
# stops sending the fan-out plan and can never produce the evidence that would undo it.
DEFAULT_FANOUT_RECHECK_HOURS = _env_float("EXOMA_FANOUT_RECHECK_HOURS", 24.0)
DEFAULT_JOB_POLL_INTERVAL_SECONDS = _env_float("EXOMA_JOB_POLL_INTERVAL_SECONDS", 2.0)
DEFAULT_JOB_POLL_TIMEOUT_SECONDS = _env_int("EXOMA_JOB_POLL_TIMEOUT_SECONDS", 0)
# Global ceiling on concurrent in-flight delegated workers across ALL sessions. Per-session
# concurrency is already 1 (session lock); this bounds the machine-wide fan-out so a burst of
# parallel main-agents cannot spawn unbounded opencode processes. Admission is serialized
# across processes so the configured ceiling is a hard bound for this runtime version.
DEFAULT_MAX_GLOBAL_WORKERS = _env_int("EXOMA_MAX_GLOBAL_WORKERS", 6)


def _provider_defaults(provider: str) -> tuple[str, str | None]:
    """(`provider_command`, `provider_agent`) for `provider`, env overrides applied.

    Reads the provider's own bundle rather than two module constants. With constants, a
    config file that said `"provider": "codex"` and nothing else came back holding
    opencode's binary and opencode's `plan` persona — a provider selected in name only,
    with no warning anywhere. An unregistered name (a typo, a provider from a newer build)
    falls back to itself as the command: wrong, but wrong in the direction that fails
    loudly at spawn instead of quietly running the wrong CLI.
    """
    from exoma.config.providers import provider_agent_default, provider_command_default

    try:
        return (
            provider_command_default(provider, getenv),
            provider_agent_default(provider, getenv),
        )
    except ValueError:
        return provider, None


def default_provider_config(provider: str | None = None) -> dict:
    """Tool defaults for one provider. Key SET is provider-independent; values are not."""
    name = provider or DEFAULT_PROVIDER
    command, agent = _provider_defaults(name)
    return {
        "provider": name,
        "provider_command": command,
        "provider_agent": agent,
        "default_model": None,
        # Reasoning effort, provider-neutral here and translated to argv by the adapter.
        # None means "say nothing", which is what every workspace predating this key
        # already does — the provider's own default then stands, unchanged.
        "effort": None,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "bootstrap_timeout_seconds": DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
        "stall_threshold_seconds": DEFAULT_STALL_THRESHOLD_SECONDS,
        "idle_stall_seconds": DEFAULT_IDLE_STALL_SECONDS,
        "probe_timeout_seconds": DEFAULT_PROBE_TIMEOUT_SECONDS,
        "probe_recheck_seconds": DEFAULT_PROBE_RECHECK_SECONDS,
        "max_probes": DEFAULT_MAX_PROBES,
        "job_poll_interval_seconds": DEFAULT_JOB_POLL_INTERVAL_SECONDS,
        # `job_max_runtime_seconds`, `job_poll_timeout_seconds` and `agent_workflow_path`
        # were written here and read nowhere — see RETIRED_PROVIDER_KEYS below.
        "routes": COMMAND_ROUTES,
    }


# Keys this file used to write into `second_agent.json` and no reader ever consumed. They
# were not typos and not deprecated features: the runtime always took those three values
# from somewhere else, so editing them in the workspace changed nothing while reading as
# configuration. README said so in prose; the file kept offering the knob anyway.
#
#   job_max_runtime_seconds  -> DEFAULT_JOB_MAX_RUNTIME_SECONDS (env EXOMA_*), core/jobs/job_manager.py
#   job_poll_timeout_seconds -> the CLI's --poll-timeout, main.py
#   agent_workflow_path      -> .workflow/config.json `runtime.agent_workflow_path`, or AGENT_PATH
#
# They stay named here so `validate_provider_config` can tell a retired key from a typo.
# Upgrade is additive — it adds keys missing from the defaults and never deletes — so every
# workspace written before this change still carries them, and calling those files
# malformed would be blaming the user for a default this tool shipped.
RETIRED_PROVIDER_KEYS: dict[str, str] = {
    "job_max_runtime_seconds": "the hard ceiling comes from EXOMA_JOB_MAX_RUNTIME_SECONDS or its default",
    "job_poll_timeout_seconds": "polling timeout comes from the CLI flag --poll-timeout",
    "agent_workflow_path": "the tool path comes from .workflow/config.json runtime.agent_workflow_path or AGENT_PATH",
}


def foreign_provider_values(config: dict) -> dict[str, str]:
    """Values in a workspace config that belong to a provider OTHER than the selected one.

    The key SET in `second_agent.json` is provider-independent, so switching `provider` is
    accepted with no complaint while `provider_command`, `provider_agent` and the model
    names keep pointing at the provider that was there before. The file then describes a
    workspace that does not exist: codex selected, opencode's binary and opencode's `plan`
    persona still written down.

    Backfill cannot fix this — it only adds absent keys, and these are present. So the
    values are reported rather than rewritten: a stale `provider_command` is a decision the
    user may have made deliberately (a wrapper script, a pinned path), and silently
    overwriting it would be a worse failure than naming it.

    Returns `{key: why}` for whatever looks foreign; empty when the file is coherent.
    """
    from exoma.config.providers import bundled_providers, provider_command_default

    selected = str(config.get("provider") or DEFAULT_PROVIDER)
    others = [name for name in bundled_providers() if name != selected]
    findings: dict[str, str] = {}

    command = config.get("provider_command")
    for name in others:
        try:
            if command and command == provider_command_default(name, lambda _k, d=None: d):
                findings["provider_command"] = f"{command!r} is {name}'s binary, not {selected}'s"
        except ValueError:
            continue

    # Model names are namespaced by provider in practice (`opencode/mimo-...`). A prefix
    # naming another registered provider is the one case flat enough to call with certainty.
    def _foreign_model(value) -> str | None:
        if not isinstance(value, str) or "/" not in value:
            return None
        prefix = value.split("/", 1)[0]
        return prefix if prefix in others else None

    owner = _foreign_model(config.get("default_model"))
    if owner:
        findings["default_model"] = f"{config['default_model']!r} is namespaced to {owner}"
    for command_name, route in (config.get("routes") or {}).items():
        if not isinstance(route, dict):
            continue
        owner = _foreign_model(route.get("model"))
        if owner:
            findings[f"routes.{command_name}.model"] = (
                f"{route['model']!r} is namespaced to {owner}"
            )
    return findings


def _main_session_cache_path(project_root=None) -> Path:
    if project_root is None:
        return Path(CACHE_FILE)
    normalized = os.path.normcase(str(Path(project_root).resolve()))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return Path(CACHE_FILE).parent / "main-sessions" / f"{digest}.json"


def get_cached_main_session_id(project_root=None) -> str | None:
    """Read the cached main agent session ID from cache file."""
    return (read_cached_main_session(project_root) or {}).get("main_session_id")


def read_cached_main_session(project_root=None) -> dict | None:
    """Cached session record, stamp included, so callers can age it out."""
    cache_path = _main_session_cache_path(project_root)
    try:
        with cache_path.open("r", encoding="utf-8") as file:
            cache = json.load(file)
        if isinstance(cache, dict) and cache.get("main_session_id"):
            return cache
    except (OSError, json.JSONDecodeError):
        pass
    return None


def cached_main_session_age(project_root=None) -> float | None:
    """Seconds since the cached session was last resolved; None if unknown."""
    cache = read_cached_main_session(project_root) or {}
    stamp = cache.get("updated_at")
    if not stamp:
        return None
    try:
        recorded = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - recorded).total_seconds())


def set_cached_main_session_id(session_id: str, project_root=None) -> None:
    """Write the main agent session ID to cache file."""
    cache_path = _main_session_cache_path(project_root)
    cache: dict = {}
    try:
        with cache_path.open("r", encoding="utf-8") as file:
            cache = json.load(file)
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, json.JSONDecodeError):
        cache = {}

    cache["main_session_id"] = session_id
    # Restamped on every resolve, not only on first write: the stamp is what separates a
    # session still in use from one that was abandoned.
    cache["updated_at"] = datetime.now(timezone.utc).isoformat()
    if project_root is not None:
        cache["project_root"] = str(Path(project_root).resolve())
    # Lazy: workspace_paths imports this module for TOOL_VERSION.
    from exoma.core.workspace.workspace_paths import atomic_write_text

    atomic_write_text(cache_path, json.dumps(cache, indent=2), fsync=True)


def validate_provider_config(loaded: dict) -> list[str]:
    """Knob-level sanity for a provider config file. Returns warnings, never raises.

    Same silent failure the file-level check already guards against, one level down: a
    misspelled key does nothing at all, yet reads as "configured". `"timeout_second"` is
    accepted, the runtime keeps using 1800, and doctor reports perfect health while the
    user waits for a timeout they believe they changed.

    Warnings, not errors: an unknown key must not discard the keys that ARE correct.
    """
    warnings: list[str] = []
    known = default_provider_config()
    for key, value in loaded.items():
        if key in RETIRED_PROVIDER_KEYS:
            # Distinguished from a typo deliberately. Both are ignored by the runtime, but
            # only one is the user's mistake — and "unknown key (typo?)" against a key this
            # tool wrote itself sends them looking for a spelling error that is not there.
            warnings.append(
                f"{key}: retired — never read; {RETIRED_PROVIDER_KEYS[key]}. Safe to delete."
            )
            continue
        if key not in known:
            warnings.append(f"{key}: unknown key (typo? the runtime ignores it)")
            continue
        default = known[key]
        if value is None or default is None:
            # None means "unset" on the way in, and a None default (default_model,
            # agent_workflow_path) carries no type to check against.
            continue
        # bool is a subclass of int, so an int knob set to `true` would slip past a
        # plain isinstance — compare bool-ness explicitly, as validate_config does.
        if isinstance(default, bool):
            if not isinstance(value, bool):
                warnings.append(f"{key}: {type(value).__name__}, expected bool")
        elif isinstance(value, bool) or not isinstance(value, type(default)):
            warnings.append(
                f"{key}: {type(value).__name__}, expected {type(default).__name__}"
            )
        elif isinstance(default, (int, float)) and value < 0:
            warnings.append(f"{key}: {value} is negative (the runtime ignores it)")
    warnings.extend(_effort_warnings(loaded))
    warnings.extend(_route_warnings(loaded))
    return warnings


def _route_warnings(loaded: dict) -> list[str]:
    """Entry-level shape for `routes`, which the type loop above cannot see into.

    `routes` passes that loop as soon as it is a dict, so everything inside it — a route
    whose value is a bare string, a model set to `42` — is accepted in full silence and
    then ignored one by one at read time by `Router.route`.

    Nothing here checks the model NAME against the provider: the shortlist is a menu, and
    pinning something off it is supported (`_effort_warnings` says the same). Only the
    shape is wrong-or-right, and a wrong shape is always a mistake.
    """
    routes = loaded.get("routes")
    # Not a dict is already reported by the type loop; saying it twice reads as two
    # separate problems.
    if not isinstance(routes, dict):
        return []

    warnings: list[str] = []
    for name, entry in routes.items():
        if not isinstance(entry, dict):
            warnings.append(
                f"routes.{name}: {type(entry).__name__}, expected an object "
                "(the runtime ignores it)"
            )
            continue
        model = entry.get("model")
        if model is None:
            continue
        if not isinstance(model, str) or not model.strip():
            warnings.append(
                f"routes.{name}.model: {type(model).__name__}, "
                "expected a non-empty string"
            )
    return warnings


def _effort_warnings(loaded: dict) -> list[str]:
    """`effort` checked against the MODEL, which is the only thing that constrains it.

    The type loop above cannot reach this: the default is None, so it carries no type to
    compare against and every value slips through. And the check is not type-shaped
    anyway — `"max"` is a perfectly good string that codex's gpt-5.5 rejects outright.

    Silence when the model is absent or off the shortlist. The shortlist is a picker's
    menu, and a user pinning a model it never listed is a supported thing to do; warning
    there would train people to ignore the warning that matters.
    """
    from exoma.config.providers import model_efforts, model_is_listed

    effort = loaded.get("effort")
    if effort is None:
        return []
    if not isinstance(effort, str) or not effort.strip():
        return [f"effort: {type(effort).__name__}, expected a non-empty string"]

    provider = str(loaded.get("provider") or DEFAULT_PROVIDER)
    model = loaded.get("default_model")
    name = model if isinstance(model, str) else None
    try:
        on_menu = model_is_listed(provider, name)
        allowed = model_efforts(provider, name)
    except ValueError:  # unregistered provider — already reported elsewhere
        return []
    if not on_menu:
        return []
    if not allowed:
        return [f"effort: {model} takes no reasoning effort (the runtime sends none)"]
    if effort not in allowed:
        return [
            f"effort: {effort!r} is not accepted by {model} "
            f"(it takes {', '.join(allowed)})"
        ]
    return []


def _load_provider_config_checked(path) -> tuple[dict, str | None, list[str], bool]:
    """Read one provider config file. Returns (config, error, warnings, provider_explicit).

    A missing file is not an error — only the caller knows whether absence is expected.
    A file that EXISTS but cannot be parsed is: returning defaults there is how a stray
    comma discards every tuned value in silence, and the runtime then reports perfect
    health while running on settings nobody wrote.

    `error` means the file was unusable and tool defaults were substituted wholesale.
    `warnings` means individual knobs were ignored while the rest of the file applied —
    a distinct outcome that must not be reported as a broken file.

    `provider_explicit` says whether the FILE named a provider, as opposed to inheriting
    the built-in one. Callers need the difference to honour the documented selection
    order: a defaulted `provider` must not outrank `.workflow/config.json`, and once
    defaults are merged in there is no other way to tell the two apart.
    """
    try:
        with Path(path).open("r", encoding="utf-8") as file:
            loaded = json.load(file)
    except FileNotFoundError:
        return default_provider_config(), None, [], False
    except (OSError, json.JSONDecodeError) as exc:
        return default_provider_config(), f"{type(exc).__name__}: {exc}", [], False

    if not isinstance(loaded, dict):
        return (
            default_provider_config(),
            "provider config is not a JSON object",
            [],
            False,
        )

    # Defaults are built for the provider THIS file selects, so provider_command and
    # provider_agent follow the choice instead of contradicting it.
    explicit = loaded.get("provider")
    config = default_provider_config(explicit if isinstance(explicit, str) else None)
    warnings = validate_provider_config(loaded)
    config.update({key: value for key, value in loaded.items() if value is not None})
    if not isinstance(config.get("routes"), dict):
        config["routes"] = COMMAND_ROUTES
    return config, None, warnings, bool(explicit)


def load_provider_config(path: Path) -> dict:
    config, _, _, _ = _load_provider_config_checked(path)
    return config


def resolve_provider_config_for(project_root) -> dict:
    """Resolve a project's effective provider config, with where it came from.

    Returns `{config, source, path, error, warnings}`. The provenance travels with the
    values so a wrong-looking model can be traced to the file that supplied it in one
    step, instead of being reconstructed from three files after the fact.
    """
    # Deferred: core.workspace.workspace_paths imports TOOL_VERSION from this module, so a
    # top-level import here would close the cycle. Same pattern as _tool_paths.
    from exoma.core.workspace.workspace_paths import resolve_provider_config

    from exoma.core.workspace.workspace_paths import PROVIDER_CONFIG_NAME, WORKFLOW_DIRNAME

    path, source = resolve_provider_config(project_root)
    if path is None:
        # Built-in defaults give readers (doctor, the provider picker) a shape to report
        # on. They are NOT a config to run on: the executor refuses `missing` before any
        # provider is called. `path` names the file the user has to create.
        return {
            "config": default_provider_config(),
            "source": "missing",
            "path": str(Path(project_root) / WORKFLOW_DIRNAME / PROVIDER_CONFIG_NAME),
            "error": None,
            "warnings": [],
            "provider_explicit": False,
        }

    config, error, warnings, explicit = _load_provider_config_checked(path)
    if error is not None:
        # Refused downstream, not substituted. Running a typo'd project on a machine-wide
        # file looked like resilience and was how a model nobody picked kept spending.
        # `path` stays on the file we tried, because that is the one the user has to fix.
        source = "invalid"
    return {
        "config": config,
        "source": source,
        "path": str(path),
        "error": error,
        "warnings": warnings,
        "provider_explicit": explicit,
    }


def load_provider_config_for(project_root) -> dict:
    """The project-local .workflow/second_agent.json, or built-in defaults when it is
    missing or unreadable — see `resolve_provider_config_for` for why those are refused."""
    return resolve_provider_config_for(project_root)["config"]
