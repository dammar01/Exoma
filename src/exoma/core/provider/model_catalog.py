"""The models a provider CLI offers right now, and which of them is the newest.

The shortlists in config/providers.py are refreshed by hand, so they age between
releases: a CLI that already ships a newer model is still offered the old one in the
`provider` picker. This module reads what the installed CLI itself says and orders it
newest first, so the picker can put the newest model at the top of its menu.

Each provider exposes its list differently, and none of them in a documented format:

    opencode  `opencode models` for what is available on this machine, plus the
              models.dev catalog it caches at ~/.cache/opencode/models.json for
              `family`, `release_date` and `reasoning_options`.
    codex     ~/.codex/models_cache.json (the CLI has no `models` subcommand): `slug`,
              `priority`, `visibility` and `supported_reasoning_levels`. No dates.
    agy       `agy models`: tab-separated id and display name, nothing else. The effort
              lives in the id itself (`-low`/`-medium`/`-high`), so variants are folded
              into one base model whose `efforts` are the suffixes it ships with.

"Newest" is decided per provider, from what each one can actually answer:

    opencode  latest `release_date` within the family of the model in use; with no
              model in use (or one the catalog does not know), across everything listed.
    codex     lowest `priority` among the models the cache marks `visibility: list`.
    agy       highest version within the family of the model in use; otherwise the
              first one the CLI prints, which is newest first.

Fail-open throughout. A CLI that is missing, slow, or prints a shape this module does
not recognize yields no live entries, and the caller falls back to the bundled
shortlist. A provider release that changes its cache format (CASE-015) therefore
costs freshness, never a broken picker.

Readings are kept on disk for `CACHE_TTL_SECONDS` so the catalog read and the apply
that follows it in the same picker session see the same list without a second spawn.
The window is short on purpose: the point of this module is to be current.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from exoma.config.providers import provider_command_default, provider_models
from exoma.core.workspace.workspace_paths import atomic_write_text
from exoma.utils import osutil

# `AI_PROXY_MODEL_CATALOG=static` turns the live read off (offline machines, or a CLI
# whose listing is known to hang). Tests flip `LIVE_ENABLED` instead.
CATALOG_ENV = "AI_PROXY_MODEL_CATALOG"
LIVE_ENABLED = True
CACHE_TTL_SECONDS = 600
# Overridable for tests; None resolves the per-user location below.
CACHE_PATH: Path | None = None
_LIST_TIMEOUT_SECONDS = 30

# At most this many models per provider reach the picker. `opencode models` alone lists
# a couple of hundred. The list is cut after ordering — family of the model in use first,
# newest first — so the cut drops the least relevant entries, not the newest ones.
# `lookup` still searches the full list, so an id typed through "Other" is not refused.
MODEL_CATALOG_LIMIT = 20

_AGY_EFFORT_SUFFIX = re.compile(r"^(?P<base>.+)-(?P<effort>low|medium|high)$")
_EFFORT_ORDER = ("low", "medium", "high")
_VERSION_PART = re.compile(r"\d+")


def live_enabled() -> bool:
    return LIVE_ENABLED and (os.getenv(CATALOG_ENV) or "").strip().lower() != "static"


# ------------------------------------------------------------------ public


def catalog(provider: str, current_model: str | None = None) -> dict:
    """`{models, newest, source}` for `provider`, newest first.

    `models` entries are `{id, efforts}`, plus `effort_in_id: True` for an agy base
    model whose effort is a suffix of the id rather than a flag. `source` is `live`,
    `cache` (a live reading from the last `CACHE_TTL_SECONDS`) or `static` (the bundled
    shortlist, when no live reading exists).
    """
    entries, source = _live(provider)
    if entries:
        ordered = _order(provider, entries, current_model)[:MODEL_CATALOG_LIMIT]
        models = [_public(entry) for entry in ordered]
        return {"models": models, "newest": models[0]["id"], "source": source}
    return catalog_static(provider)


def catalog_static(provider: str) -> dict:
    """The bundled shortlist in `catalog`'s shape, with no CLI asked."""
    static = [
        {"id": entry["id"], "efforts": list(entry.get("efforts") or ())}
        for entry in provider_models(provider)
    ][:MODEL_CATALOG_LIMIT]
    return {
        "models": static,
        "newest": static[0]["id"] if static else None,
        "source": "static",
    }


def lookup(provider: str, model: str | None) -> dict | None:
    """The catalog entry for `model`, or None when the catalog does not list it.

    Searches the full live list (not the picker's cut of it) and then the shortlist, so
    an apply accepts every model the picker could have offered. For agy, a full variant
    id (`claude-opus-5-5-high`) resolves to a model that takes no further effort.
    """
    if not model:
        return None
    entries, _ = _live(provider)
    for entry in entries:
        if entry["id"] == model:
            return _public(entry)
        if model in entry.get("variants", ()):
            return {"id": model, "efforts": []}
    for entry in provider_models(provider):
        if entry.get("id") == model:
            return {"id": model, "efforts": list(entry.get("efforts") or ())}
    return None


def compose_model_id(model: str, effort: str) -> str:
    """The agy id carrying `effort` as its suffix: `claude-opus-5-5` + `high`."""
    return f"{model}-{effort}"


# ------------------------------------------------------------------ order


def _public(entry: dict) -> dict:
    public = {"id": entry["id"], "efforts": list(entry.get("efforts") or ())}
    if entry.get("effort_in_id"):
        public["effort_in_id"] = True
    return public


def _order(provider: str, entries: list[dict], current_model: str | None) -> list[dict]:
    if provider == "codex":
        return sorted(entries, key=lambda entry: entry.get("priority", 1 << 30))

    if provider == "opencode":
        by_date = sorted(entries, key=lambda entry: entry.get("released") or "", reverse=True)
        # The model in use may not be listed any more (its provider logged out); the
        # models.dev catalog still knows its family.
        family = _family_of(entries, current_model) or (
            (_opencode_info(current_model) or {}).get("family") if current_model else None
        )
        if not family:
            return by_date
        mates = [entry for entry in by_date if entry.get("family") == family]
        return mates + [entry for entry in by_date if entry.get("family") != family]

    if provider == "agy":
        family = _family_of(entries, _agy_base(current_model))
        if not family:
            return entries  # CLI order, newest first
        mates = sorted(
            (entry for entry in entries if entry.get("family") == family),
            key=lambda entry: _version(entry["id"]),
            reverse=True,
        )
        return mates + [entry for entry in entries if entry.get("family") != family]

    return entries


def _family_of(entries: list[dict], model: str | None) -> str | None:
    if not model:
        return None
    for entry in entries:
        if entry["id"] == model:
            return entry.get("family")
    return None


def _agy_base(model: str | None) -> str | None:
    if not model:
        return None
    match = _AGY_EFFORT_SUFFIX.match(model)
    return match.group("base") if match else model


def _version(model_id: str) -> tuple[int, ...]:
    return tuple(int(part) for part in _VERSION_PART.findall(model_id))


def _agy_family(base: str) -> str:
    """`gemini-3.8-flash` -> `gemini-flash`, `claude-opus-5-5` -> `claude-opus`."""
    parts = [part for part in base.split("-") if not re.fullmatch(r"[\d.]+", part)]
    return "-".join(parts) or base


# ------------------------------------------------------------------ read


def _live(provider: str) -> tuple[list[dict], str]:
    if not live_enabled():
        return [], "static"
    reader = _READERS.get(provider)
    if reader is None:
        return [], "static"

    cached = _cache_lookup(provider)
    if cached is not None:
        return cached, "cache"
    try:
        entries = reader() or []
    except Exception:
        entries = []
    if entries:
        _cache_store(provider, entries)
    return entries, "live"


def _run_list(provider: str, args: list[str]) -> str | None:
    command = provider_command_default(provider, os.getenv)
    try:
        code, stdout, _ = osutil.run_bounded(
            [osutil.resolve_exe(command), *args], _LIST_TIMEOUT_SECONDS
        )
    except Exception:
        return None
    return stdout if code == 0 else None


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _opencode_catalog() -> dict:
    """opencode's cached models.dev catalog: `{provider: {models: {model: info}}}`."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    cache_root = Path(xdg) if xdg else Path.home() / ".cache"
    return _read_json(cache_root / "opencode" / "models.json") or {}


def _opencode_info(model_id: str, catalog_data: dict | None = None) -> dict | None:
    if catalog_data is None:
        catalog_data = _opencode_catalog()
    provider_key, _, model_key = model_id.partition("/")
    provider_entry = catalog_data.get(provider_key)
    models = provider_entry.get("models") if isinstance(provider_entry, dict) else None
    info = models.get(model_key) if isinstance(models, dict) else None
    return info if isinstance(info, dict) else None


def _read_opencode() -> list[dict]:
    listed = _run_list("opencode", ["models"])
    if not listed:
        return []
    available = [line.strip() for line in listed.splitlines() if "/" in line.strip()]
    catalog_data = _opencode_catalog()

    entries = []
    for model_id in available:
        info = _opencode_info(model_id, catalog_data)
        if info is None:
            # Listed by the CLI but unknown to the catalog: no date to rank it by and no
            # effort to offer. Kept, ranked last.
            entries.append({"id": model_id, "efforts": (), "family": None, "released": ""})
            continue
        efforts: tuple[str, ...] = ()
        for option in info.get("reasoning_options") or ():
            if isinstance(option, dict) and option.get("type") == "effort":
                efforts = tuple(str(value) for value in option.get("values") or ())
                break
        entries.append(
            {
                "id": model_id,
                "efforts": efforts,
                "family": info.get("family"),
                "released": str(info.get("release_date") or ""),
            }
        )
    return entries


def _read_codex() -> list[dict]:
    home = os.environ.get("CODEX_HOME")
    root = Path(home) if home else Path.home() / ".codex"
    data = _read_json(root / "models_cache.json") or {}
    entries = []
    for model in data.get("models") or ():
        if not isinstance(model, dict) or model.get("visibility") != "list":
            # `hide` entries are internal routes (auto-review and the like), offered to
            # nobody by the CLI itself.
            continue
        slug = model.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        levels = model.get("supported_reasoning_levels") or ()
        efforts = tuple(
            str(level.get("effort"))
            for level in levels
            if isinstance(level, dict) and level.get("effort")
        )
        priority = model.get("priority")
        entries.append(
            {
                "id": slug,
                "efforts": efforts,
                "priority": priority if isinstance(priority, int) else 1 << 30,
            }
        )
    return entries


def _read_agy() -> list[dict]:
    listed = _run_list("agy", ["models"])
    if not listed:
        return []
    bases: dict[str, dict] = {}
    for line in listed.splitlines():
        model_id = line.split("\t", 1)[0].strip()
        if not model_id or " " in model_id:
            continue  # a banner or a status line, not an id
        match = _AGY_EFFORT_SUFFIX.match(model_id)
        base, effort = (match.group("base"), match.group("effort")) if match else (model_id, None)
        entry = bases.setdefault(
            base,
            {"id": base, "efforts": [], "variants": [], "family": _agy_family(base)},
        )
        if effort:
            entry["efforts"].append(effort)
            entry["variants"].append(model_id)
    entries = []
    for entry in bases.values():
        if entry["efforts"]:
            entry["efforts"] = tuple(e for e in _EFFORT_ORDER if e in entry["efforts"])
            entry["effort_in_id"] = True
        else:
            entry["efforts"] = ()
        entries.append(entry)
    return entries


_READERS = {"opencode": _read_opencode, "codex": _read_codex, "agy": _read_agy}


# ------------------------------------------------------------------ cache


def _cache_path() -> Path | None:
    if CACHE_PATH is not None:
        return Path(CACHE_PATH)
    try:
        if osutil.IS_WINDOWS:
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            root = Path(base) if base else None
        else:
            xdg = os.environ.get("XDG_CACHE_HOME")
            root = Path(xdg) if xdg else Path.home() / ".cache"
        return root / "exoma" / "model-catalog.json" if root else None
    except Exception:
        return None


def _cache_key(provider: str) -> str:
    return f"{provider}|{provider_command_default(provider, os.getenv)}"


def _cache_lookup(provider: str) -> list[dict] | None:
    if CACHE_TTL_SECONDS <= 0:
        return None
    path = _cache_path()
    if path is None:
        return None
    entry = (_read_json(path) or {}).get(_cache_key(provider))
    if not isinstance(entry, dict):
        return None
    read_at = entry.get("read_at")
    models = entry.get("models")
    if not isinstance(read_at, (int, float)) or time.time() - read_at > CACHE_TTL_SECONDS:
        return None
    if not isinstance(models, list) or not all(isinstance(m, dict) and m.get("id") for m in models):
        return None
    return models


def _cache_store(provider: str, entries: list[dict]) -> None:
    if CACHE_TTL_SECONDS <= 0:
        return
    path = _cache_path()
    if path is None:
        return
    try:
        data = _read_json(path) or {}
        data[_cache_key(provider)] = {"models": entries, "read_at": time.time()}
        atomic_write_text(path, json.dumps(data, indent=1))
    except Exception:
        pass


def forget(provider: str) -> None:
    """Drop `provider`'s cached listing: a CLI that changed release can list other models.

    For a caller that just changed the release (install.py --update-providers), so the next
    picker reads the CLI instead of the old release's listing.
    """
    path = _cache_path()
    if path is None:
        return
    try:
        data = _read_json(path) or {}
        if data.pop(_cache_key(provider), None) is not None:
            atomic_write_text(path, json.dumps(data, indent=1))
    except Exception:
        pass
