"""Read and write the second_agent selection: provider, model, effort.

The picker that drives this lives in the main agent (`AskUserQuestion` has no subagent
form), so the model would otherwise be the thing editing `second_agent.json` by hand.
It is not: the model passes one string and every decision about whether that string is
writable happens here, in code that `tests/checks/provider.py` can run.

Two modes, told apart by whether a payload was given:

    provider ""                            -> the catalog plus what is selected now
    provider "codex|gpt-5.6-luna|high"     -> validate, then write
    provider "codex gpt-5.6-luna high"     -> the same, as typed after `/.provider`
    provider "codex|gpt-5.6-luna|high|explore=gpt-5.5,plan=gpt-5.6-luna"
                                           -> the same, with per-route models

`|` separates the fields because opencode model ids contain `/`.

An apply writes FIVE keys together — provider, provider_command, provider_agent,
default_model, effort. Writing `provider` alone is exactly the failure
`config.settings.foreign_provider_values` exists to report: codex selected, opencode's
binary and opencode's `plan` persona still on disk, and a workspace describing something
that does not exist. So the write is whole, and the report runs afterwards as proof.

`routes` is written with them, for the same reason one field ahead of the others is not
enough. `Router.route` reads `routes.<command>.model` BEFORE `default_model`
(`core/router.py:57`), so a selection that touched only `default_model` left every route
pointing at the model the user had just replaced — the new model written down, the old
one still running, and nothing anywhere saying so. Absent a routes field the payload
means "all of them follow the default", which is the shape that cannot drift.

Only the delegated commands are selectable: the rest of `COMMAND_ROUTES` never reaches a
provider, so offering a model for them would be a question with no consequence. Route
entries are merged key-wise rather than replaced — `timeout_seconds` and `agent` live
there too (`core/router.py:61,75`) and belong to the user, not to this command.
"""

import json
import os
from pathlib import Path

from exoma.config.providers import (
    bundled_providers,
    provider_agent_default,
    provider_boundary_warning,
    provider_command_default,
)
from exoma.config.routing import COMMAND_ROUTES
from exoma.core.provider import model_catalog
from exoma.config.settings import (
    DEFAULT_PROVIDER,
    foreign_provider_values,
    resolve_provider_config_for,
)
from exoma.core.workspace.workspace_paths import (
    JSON_INDENT,
    PROVIDER_CONFIG_NAME,
    atomic_write_text,
    read_json_file,
    resolve_provider_config,
    workflow_paths,
)
from exoma.utils import osutil

FIELD_SEPARATOR = "|"

# The commands that actually reach a provider. `COMMAND_ROUTES` also carries init,
# doctor, submit, status and result — routes that exist so the role lookup is total,
# not because a model choice would change anything about them.
# `e2e_spec` is the verify-browser draft: a real provider call that, left off this list,
# could never be given a model and ran on `default_model` or, with none, no `-m` at all.
SELECTABLE_ROUTES = ("explore", "plan", "analyze", "verify", "e2e_spec")

ROUTE_SEPARATOR = ","
ROUTE_ASSIGN = "="
ROUTES_FOLLOW_DEFAULT = "same"


def run(project_root: Path, payload: str | None = None) -> dict:
    """Entry point for the `provider` command."""
    if not (payload or "").strip():
        return _catalog(project_root)
    return _apply(project_root, str(payload))


# ------------------------------------------------------------------ read


def _catalog(project_root: Path) -> dict:
    resolved = resolve_provider_config_for(project_root)
    config = resolved["config"]

    providers = []
    for name in bundled_providers():
        command = provider_command_default(name, os.getenv)
        installed, detail = osutil.provider_callable(command)
        # The model in use anchors "newest": opencode and agy rank within its family, so
        # a user on one vendor is not offered another vendor's latest release as theirs.
        # Only listing a CLI that is there: a missing one would cost a timeout per row.
        current_model = config.get("default_model") if config.get("provider") == name else None
        if installed:
            listing = model_catalog.catalog(name, current_model)
        else:
            listing = model_catalog.catalog_static(name)
        providers.append(
            {
                "name": name,
                "command": command,
                "installed": installed,
                # The resolved path when found, the reason when not. Both are worth
                # showing: a provider that is "not installed" because PATH points at a
                # different binary is a different problem from one never installed.
                "detail": detail,
                # What choosing it means, when it keeps no read boundary; None when it does.
                # The picker shows it and asks for confirmation before applying.
                "boundary_warning": provider_boundary_warning(name),
                # Newest first, so the picker's first option is the one to recommend.
                "models": listing["models"],
                "newest": listing["newest"],
                # `live`/`cache` = what the installed CLI reports; `static` = the bundled
                # shortlist, used when no live reading was possible.
                "models_source": listing["source"],
            }
        )

    return {
        "ok": True,
        "content": _catalog_summary(providers, config),
        "meta": {
            "providers": providers,
            "current": {
                "provider": config.get("provider"),
                "model": config.get("default_model"),
                "effort": config.get("effort"),
                "provider_command": config.get("provider_command"),
                "provider_agent": config.get("provider_agent"),
                # Per-route models as they stand. The picker needs them to show what a
                # route is on before asking whether to change it, and a route reading
                # something other than `default_model` is the state this whole field
                # exists to make visible.
                "routes": _current_routes(config),
            },
            "selectable_routes": list(SELECTABLE_ROUTES),
            "source": resolved["source"],
            "path": resolved["path"],
            "warnings": resolved["warnings"],
            "error": resolved["error"],
        },
    }


def _current_routes(config: dict) -> dict:
    """`{route: model}` for the selectable routes, None where the route inherits."""
    configured = config.get("routes")
    routes = configured if isinstance(configured, dict) else {}
    current: dict[str, str | None] = {}
    for name in SELECTABLE_ROUTES:
        entry = routes.get(name)
        current[name] = entry.get("model") if isinstance(entry, dict) else None
    return current


def _catalog_summary(providers: list[dict], config: dict) -> str:
    installed = [entry["name"] for entry in providers if entry["installed"]]
    default_model = config.get("default_model")
    routes = _current_routes(config)
    # Named only when a route disagrees with the default: on a coherent workspace this
    # line would otherwise repeat the same model five times and bury the one case worth
    # reading, which is a route still on the model the user thought they replaced.
    drifted = [
        f"{name}={model}" for name, model in routes.items() if model and model != default_model
    ]
    return (
        f"provider={config.get('provider')} "
        f"model={default_model} "
        f"effort={config.get('effort')}; "
        f"installed: {', '.join(installed) or 'none'}"
        + (f"; routes off default: {', '.join(drifted)}" if drifted else "")
    )


# ----------------------------------------------------------------- write


def _parse_routes(spec: str, default_model: str | None) -> tuple[dict, str | None]:
    """Per-route models from the fourth payload field. Returns (models, error).

    Absent or `same` means every selectable route follows `default_model`. That is the
    default on purpose: the alternative — leaving routes untouched — is the drift this
    field exists to end, and it fails silently, which is the worst way for a model
    selection to be wrong.

    A route named with an empty value (`explore=`) is cleared to None rather than
    defaulted. `Router.route` then falls through to `default_model` anyway, so the two
    agree on the model that runs; they differ in what the file says, and a user who
    types the empty value is saying it deliberately.

    Routes the caller does not mention follow the default too. Every selectable route is
    written on every apply, so there is no third state where one of them keeps a value
    nobody chose this time.
    """
    cleaned = (spec or "").strip()
    if not cleaned or cleaned.lower() == ROUTES_FOLLOW_DEFAULT:
        return {name: default_model for name in SELECTABLE_ROUTES}, None

    models: dict[str, str | None] = {name: default_model for name in SELECTABLE_ROUTES}
    seen: set[str] = set()
    for chunk in cleaned.split(ROUTE_SEPARATOR):
        item = chunk.strip()
        if not item:
            continue
        if ROUTE_ASSIGN not in item:
            return {}, (
                f"{item!r} is not a route assignment "
                f"(expected name{ROUTE_ASSIGN}model)"
            )
        name, _, value = item.partition(ROUTE_ASSIGN)
        name = name.strip().lower()
        if name not in SELECTABLE_ROUTES:
            return {}, f"unknown route {name!r}"
        if name in seen:
            return {}, f"route {name!r} was given twice"
        seen.add(name)
        models[name] = value.strip() or None
    return models, None


def _split_payload(payload: str) -> list[str]:
    """Fields of an apply payload: `|`-separated, or whitespace-separated when there is no `|`.

    The whitespace form is what a user types (`/.provider codex gpt-6-luna low`). It is
    unambiguous because no provider, model id or effort contains a space — opencode ids
    contain `/`, which is why `|` exists, not whitespace.
    """
    if FIELD_SEPARATOR in payload:
        return [part.strip() for part in payload.split(FIELD_SEPARATOR)]
    return payload.split()


def _apply(project_root: Path, payload: str) -> dict:
    parts = _split_payload(payload)
    if len(parts) > 4:
        return _refuse(
            f"expected at most 4 fields separated by {FIELD_SEPARATOR!r}, "
            f"got {len(parts)}",
            next_action=(
                f"Use: provider{FIELD_SEPARATOR}model{FIELD_SEPARATOR}effort"
                f"{FIELD_SEPARATOR}routes"
            ),
        )
    provider = parts[0]
    model = parts[1] if len(parts) > 1 and parts[1] else None
    effort = parts[2] if len(parts) > 2 and parts[2] else None
    routes_spec = parts[3] if len(parts) > 3 else ""

    known = bundled_providers()
    if provider not in known:
        return _refuse(
            f"unknown provider {provider!r}",
            next_action=f"Known providers: {', '.join(known)}",
        )

    # A provider that enforces no read-only boundary is selectable without any gate: the
    # choice is the user's, made here. Its warning travels in `meta.warnings` on every apply,
    # so a selection that skipped the picker's question still says what it accepted.
    boundary_warning = provider_boundary_warning(provider)
    warnings: list[str] = [boundary_warning] if boundary_warning else []
    # Checked against what the CLI lists now, falling back to the shortlist: a model the
    # picker offered from the live catalog must not be refused for being newer than it.
    entry = model_catalog.lookup(provider, model)
    on_menu = entry is not None
    allowed = tuple(entry["efforts"]) if entry else ()
    has_menu = bool(model_catalog.catalog_static(provider)["models"])

    # agy carries effort in the id (`claude-opus-5-5-high`), and `--effort` on top of
    # it has never been verified. The picker asks model then effort like any provider;
    # the two are joined here into the id the CLI lists, and no effort flag is written.
    effort_in_id = bool(entry and entry.get("effort_in_id"))
    variant_effort = effort if effort_in_id else None
    if effort_in_id:
        if not effort:
            return _refuse(
                f"{model} is offered in variants; an effort picks one",
                next_action=f"{model} takes: {', '.join(allowed)}",
            )
        if effort not in allowed:
            return _refuse(
                f"{effort!r} is not accepted by {model}",
                next_action=f"{model} takes: {', '.join(allowed)}",
            )
        model = model_catalog.compose_model_id(model, effort)
        effort = None
        allowed = ()

    if model and not on_menu and has_menu:
        # Not an error. The shortlist is a menu, not the set of models that exist, and
        # pinning something off-menu is a supported choice — it just means the effort
        # below cannot be checked against anything.
        warnings.append(
            f"{model!r} is not in the shortlist for {provider}; "
            "its accepted effort values are unknown here"
        )

    if effort and on_menu and not allowed:
        # Listed with an empty effort set is a declaration, not a gap: this model takes
        # none. Refusing is the whole point of declaring it — the alternative is an argv
        # the upstream rejects, discovered a minute later inside a delegated run.
        return _refuse(
            f"{model} takes no reasoning effort",
            next_action=f"Select {model} with no effort, or pick a model that takes one.",
        )
    if effort and allowed and effort not in allowed:
        return _refuse(
            f"{effort!r} is not accepted by {model}",
            next_action=f"{model} takes: {', '.join(allowed)}",
        )
    if effort and not on_menu:
        warnings.append(
            f"effort {effort!r} was not verified: no accepted values are declared "
            f"for {model or 'the selected model'}"
        )

    route_models, route_error = _parse_routes(routes_spec, model)
    if route_error:
        return _refuse(
            route_error,
            next_action=(
                f"Use {ROUTES_FOLLOW_DEFAULT!r} for one model everywhere, or "
                f"name{ROUTE_ASSIGN}model pairs separated by {ROUTE_SEPARATOR!r} "
                f"from: {', '.join(SELECTABLE_ROUTES)}"
            ),
        )
    for name, route_model in route_models.items():
        if not route_model or route_model == model:
            continue
        route_entry = model_catalog.lookup(provider, route_model)
        if route_entry and route_entry.get("effort_in_id"):
            # A route names an agy base model; it runs on the variant matching the effort
            # chosen above, or is refused when that variant does not exist.
            if variant_effort not in route_entry["efforts"]:
                return _refuse(
                    f"routes.{name}: {route_model} has no {variant_effort or 'default'!r} variant",
                    next_action=f"{route_model} takes: {', '.join(route_entry['efforts'])}",
                )
            route_models[name] = model_catalog.compose_model_id(route_model, variant_effort)
            continue
        # Same standard the default model is held to: the shortlist is a menu, so an
        # off-menu pin is reported and kept. Effort is global (`Router._effort_for`), so
        # there is nothing per-route to check it against.
        if route_entry is None and has_menu:
            warnings.append(
                f"routes.{name}: {route_model!r} is not in the catalog for {provider}"
            )

    command = provider_command_default(provider, os.getenv)
    agent = provider_agent_default(provider, os.getenv)

    path, _ = resolve_provider_config(project_root)
    if path is None:
        path = workflow_paths(Path(project_root))["workflow_dir"] / PROVIDER_CONFIG_NAME

    # The file on disk, NOT the defaults-merged view: merging first would write every
    # tool default into the workspace as though the user had chosen it, and the next
    # default that changes upstream would then be pinned to the old value here.
    existing: dict = {}
    if Path(path).exists():
        try:
            existing = read_json_file(Path(path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return _refuse(
                f"cannot read the existing config at {path}: {exc}",
                next_action="Fix or remove that file, then retry.",
            )

    written_routes = _merge_routes(existing.get("routes"), route_models)

    updated = dict(existing)
    updated.update(
        {
            "provider": provider,
            "provider_command": command,
            "provider_agent": agent,
            "default_model": model,
            "effort": effort,
            "routes": written_routes,
        }
    )
    try:
        atomic_write_text(
            Path(path), json.dumps(updated, indent=JSON_INDENT) + "\n"
        )
    except OSError as exc:
        return _refuse(
            f"cannot write {path}: {exc}",
            next_action="Check the workspace is writable, then retry.",
        )

    hint = _sync_config_hint(project_root, provider)

    # Proof rather than assertion: re-read through the normal resolution path and ask
    # the same function doctor asks whether the file now describes one provider.
    resolved = resolve_provider_config_for(project_root)
    foreign = foreign_provider_values(resolved["config"])
    warnings.extend(resolved["warnings"])

    uniform = all(value == model for value in route_models.values())
    return {
        "ok": True,
        "content": (
            f"second_agent set to {provider} "
            f"(model={model or 'provider default'}, effort={effort or 'provider default'}, "
            f"routes={'all follow the default' if uniform else _routes_summary(route_models)})"
        ),
        "meta": {
            "path": str(path),
            "written": {
                "provider": provider,
                "provider_command": command,
                "provider_agent": agent,
                "default_model": model,
                "effort": effort,
                "routes": {name: route_models[name] for name in SELECTABLE_ROUTES},
            },
            "routes_uniform": uniform,
            "config_hint": hint,
            "foreign_values": foreign,
            "warnings": warnings,
        },
    }


def _routes_summary(route_models: dict) -> str:
    return ", ".join(
        f"{name}={route_models[name] or 'default'}" for name in SELECTABLE_ROUTES
    )


def _merge_routes(existing, route_models: dict) -> dict:
    """The routes block with only `model` replaced, on the routes that were selected.

    Key-wise because a route entry is not only a model: `Router.route` reads
    `timeout_seconds` and `agent` from it too (`core/router.py:61,75`). Replacing the
    entry would drop a per-route timeout the user set by hand, and the loss would show up
    as a delegated call that runs to the global limit — nothing pointing back here.

    Routes outside the selectable set are copied through untouched. They are not this
    command's to decide, and a workspace that has tuned one is telling us so.
    """
    merged: dict = {}
    if isinstance(existing, dict):
        for name, entry in existing.items():
            merged[name] = dict(entry) if isinstance(entry, dict) else entry

    for name, model in route_models.items():
        entry = merged.get(name)
        if not isinstance(entry, dict):
            # A missing or malformed entry is rebuilt from the code-side defaults rather
            # than invented here, so `role` keeps whatever routing.py says it is.
            entry = dict(COMMAND_ROUTES.get(name) or {})
        entry["model"] = model
        merged[name] = entry
    return merged


def _sync_config_hint(project_root: Path, provider: str) -> dict:
    """Keep `runtime.second_agent` in config.json agreeing with the real selection.

    That key selects nothing (`adapters/contract/registry.py` reads the provider config), but
    doctor prints it and users read it. Left behind, it is a second answer to "which
    provider is this workspace on" that contradicts the first.

    Best-effort: the selection is already written and valid, so a missing or unreadable
    config.json must not turn a successful apply into a failure.
    """
    path = workflow_paths(Path(project_root))["config"]
    if not path.exists():
        return {"updated": False, "reason": "config.json not found"}
    try:
        config = read_json_file(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"updated": False, "reason": f"unreadable: {exc}"}

    runtime = config.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
        config["runtime"] = runtime
    if runtime.get("second_agent") == provider:
        return {"updated": False, "reason": "already in step"}
    runtime["second_agent"] = provider
    try:
        atomic_write_text(path, json.dumps(config, indent=JSON_INDENT) + "\n")
    except OSError as exc:
        return {"updated": False, "reason": f"unwritable: {exc}"}
    return {"updated": True, "path": str(path)}


def _refuse(message: str, next_action: str) -> dict:
    """Nothing is written on any refusal — a half-applied selection is the one state
    this command must never leave behind."""
    from exoma.core.evidence.contract import make_error

    return make_error(
        "invalid_provider_selection",
        message,
        next_action=next_action,
        meta={"known_providers": list(bundled_providers()), "default": DEFAULT_PROVIDER},
    )
