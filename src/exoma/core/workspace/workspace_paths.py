"""Workspace paths, constants, and the atomic JSON/text primitives.

Bottom layer of the runtime: imports nothing from core, so every other
split module can depend on it without closing a cycle."""

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from exoma.config.settings import TOOL_VERSION
from exoma.utils import osutil
from exoma.utils.path_guard import safe_path_component


def _safe_component(value: str) -> str:
    return safe_path_component(value)


WORKFLOW_DIRNAME = ".workflow"
# The workflow's own tuning for whichever second_agent is selected. Named after the
# role, not the vendor: `.workflow/opencode.json` was the v3.4.2 name and is migrated
# on upgrade. Distinct from OpenCode's own config files (~/.config/opencode/opencode.json
# and <project_root>/opencode.json), which keep their vendor names.
PROVIDER_CONFIG_NAME = "second_agent.json"
# The v3.4.2 name. Kept as a constant rather than a literal because the last rename left
# one reader still spelling it by hand, and a project whose file no longer matched fell
# through to the tool defaults without a word — see resolve_provider_config below.
LEGACY_PROVIDER_CONFIG_NAME = "opencode.json"
# The tool-level TEMPLATE `init` copies into a new workspace, rebuilt by every
# `install.py --apply`. Never read by the runtime: a project without its own
# second_agent.json is refused, not run on whatever this machine was once set to.
PROVIDER_SEED_NAME = "second_agent.seed.json"
LOCK_TTL_SECONDS = 300
JSON_INDENT = 2
ARCHIVE_KEEP = 20
# Derived, not restated. `tools/maintain/stamp_version.py` makes TOOL_VERSION the single source for
# every version string that ships, but this one was a hand-maintained literal outside its
# TARGETS — guarded only by an e2e assertion, which reports the drift after it exists
# rather than preventing it. The config schema is versioned in lockstep with the tool, so
# the two numbers were never independent; only their maintenance was.
CONFIG_VERSION = TOOL_VERSION


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Windows refuses `os.replace` onto a file another process holds open without
# FILE_SHARE_DELETE — which is every plain Python `open()`, so a `check.py` or hook that is
# merely reading the target at that moment fails the write with PermissionError. The
# reader lets go in milliseconds; a short bounded retry outlasts it, a real permission
# problem still raises after the last attempt.
REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)


def replace_with_retry(source: Path, target: Path) -> None:
    """`os.replace` that outlasts a reader holding `target` open on Windows.

    Public for the one writer that cannot hand its bytes to `atomic_write_*`: e2e tagging
    re-checks the target's identity between staging and the move, and that check has to
    sit immediately before this call.
    """
    for delay in REPLACE_RETRY_DELAYS:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(source, target)


def _staging_path(path: Path) -> Path:
    # Per-writer name: a fixed ".tmp" would let two concurrent savers clobber each other's
    # staging file before the replace.
    return path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}."
        f"{secrets.token_hex(8)}.tmp"
    )


def _commit(path: Path, temp: Path) -> None:
    try:
        replace_with_retry(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def atomic_write_text(
    path: Path,
    content: str,
    encoding: str = "utf-8",
    *,
    newline: str | None = None,
    fsync: bool = False,
) -> None:
    """Write `content` to `path` whole or not at all.

    `newline` is passed to `open` unchanged, so the default keeps the platform
    translation every caller had before. `fsync` is opt-in: it costs a disk flush per
    write, and only the writers that already paid for durability ask for it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = _staging_path(path)
    try:
        with temp.open("w", encoding=encoding, newline=newline) as file:
            file.write(content)
            if fsync:
                file.flush()
                os.fsync(file.fileno())
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise
    _commit(path, temp)


def atomic_write_bytes(
    path: Path, data: bytes, *, fsync: bool = False, mode: int | None = None
) -> None:
    """Binary twin of `atomic_write_text`. `mode`, when given, is applied to the staged
    file before the move, so the result does not inherit the staging file's permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = _staging_path(path)
    try:
        with temp.open("wb") as file:
            file.write(data)
            if fsync:
                file.flush()
                os.fsync(file.fileno())
        if mode is not None:
            os.chmod(temp, mode)
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise
    _commit(path, temp)


def atomic_write_json(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, indent=JSON_INDENT))


def detect_project_root(work_dir: str | None = None) -> Path:
    start = Path(work_dir).resolve() if work_dir else Path.cwd().resolve()
    current = start

    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return start


def slugify_project_name(name: str) -> str:
    safe = []
    for char in name.lower():
        safe.append(char if char.isalnum() else "-")
    slug = "".join(safe).strip("-")
    return slug or "project"


DATA_DIRNAME = "data"
# What a v3.5.x-layout workspace kept at the .workflow root that a v3.6.0 one keeps under data/.
# Its presence without data/ marks a workspace the upgrade migration has not moved yet.
_LEGACY_MARKERS = (
    "sessions",
    "provider-sessions",
    "reports",
    "audit.jsonl",
    "usage.jsonl",
    "quality.jsonl",
    "facts.jsonl",
    "evidence.jsonl",
    "redactions.jsonl",
)


def data_dir(project_root: Path) -> Path:
    """Where this workspace keeps everything that is not a file the user edits.

    `.workflow/` itself holds only what a person opens: config.json, second_agent.json,
    e2e/secrets.json, the run/check/inspect scripts, and current/. The rest — streams,
    stores, caches, sessions — lives in `.workflow/data/`.

    A workspace written before v3.6.0 (the v3.5.x layout) keeps those at the root until `upgrade` moves
    them. Until then this returns the root, so an un-migrated workspace keeps working
    exactly as before instead of silently starting an empty history beside its real one.
    The rule is the directory's presence, and the shipped hooks and run scripts apply the
    same rule, so every reader agrees on one layout at any moment.
    """
    workflow_dir = Path(project_root) / WORKFLOW_DIRNAME
    data = workflow_dir / DATA_DIRNAME
    if data.is_dir():
        return data
    if any((workflow_dir / name).exists() for name in _LEGACY_MARKERS):
        return workflow_dir
    return data


def is_legacy_layout(project_root: Path) -> bool:
    return data_dir(project_root) == Path(project_root) / WORKFLOW_DIRNAME


def workflow_paths(
    project_root: Path, session_id: str | None = None
) -> dict[str, Path]:
    """Resolve workflow paths — the one place a `.workflow` path is spelled.

    Mutable per-flow state (state/scope/cache/runtime/logs) lives under
    `sessions/<sid>/` so concurrent main agents on the SAME project never clobber each
    other. Shared streams and stores sit beside `sessions/` in the data directory
    (`data_dir`). Only user-editable files stay at the `.workflow` root.

    session_id=None → session-scoped keys point at the data directory itself: init
    scaffolding and no-session tooling, never a session's state.
    """
    project_root = Path(project_root)
    workflow_dir = project_root / WORKFLOW_DIRNAME
    data = data_dir(project_root)
    sessions_dir = data / "sessions"
    reports_dir = data / "reports"
    session_dir = sessions_dir / _safe_component(session_id) if session_id else data
    runtime_dir = session_dir / "runtime"
    logs_dir = session_dir / "logs"
    sweep_report = (
        session_dir / "reports" / "sweep.last.md"
        if session_id
        else reports_dir / "sweep.last.md"
    )
    return {
        "project_root": project_root,
        "workflow_dir": workflow_dir,
        "data_dir": data,
        # --- user-editable, at the .workflow root -------------------------------------
        "config": workflow_dir / "config.json",
        "provider_config": workflow_dir / PROVIDER_CONFIG_NAME,
        "secrets": workflow_dir / "e2e" / "secrets.json",
        # Beside secrets.json, and for the same reason: both answer "what is this run
        # allowed to do here", and both are the project's standing answer rather than one
        # run's. Approvals are appended to it as they are given, so an endpoint agreed to
        # once is not asked about again next session; the deny list beside them is the one
        # thing a run cannot talk its way past.
        "e2e_permissions": workflow_dir / "e2e" / "permissions.json",
        "gitignore": workflow_dir / ".gitignore",
        "current_dir": workflow_dir / "current",
        # --- internal, in the data directory -------------------------------------------
        "sessions_dir": sessions_dir,
        "provider_sessions_dir": data / "provider-sessions",
        "reports_dir": reports_dir,
        "doctor_report": reports_dir / "doctor.json",
        "audit_stream": data / "audit.jsonl",
        "usage_stream": data / "usage.jsonl",
        "quality_stream": data / "quality.jsonl",
        "redactions_stream": data / "redactions.jsonl",
        "evidence_store": data / "evidence.jsonl",
        "facts_store": data / "facts.jsonl",
        "recurrence_cache": data / "recurrence-cache.json",
        "e2e_knowledge": data / "e2e-knowledge.jsonl",
        "capabilities": data / "capabilities.json",
        "graph_meta": data / "graph-meta.json",
        "graph_stale": data / "graph-stale.json",
        "promote_lock": data / "promote.lock",
        "backups_dir": data / "backups",
        # --- per session -------------------------------------------------------------
        "session_dir": session_dir,
        "state": session_dir / "state.json",
        "scope": session_dir / "scope.json",
        "command_cache": session_dir / "command-cache.json",
        "runtime_dir": runtime_dir,
        "prompt": runtime_dir / "prompt.txt",
        "prompt_meta": runtime_dir / "prompt.meta.json",
        "response_last": runtime_dir / "response.last.md",
        # Evidence sidecars: dynamic leads/facts the second agent reads for itself,
        # instead of them riding in the prompt.
        "leads": runtime_dir / "leads.json",
        "facts": runtime_dir / "facts.json",
        "lock": runtime_dir / "lock",
        "sweep_report": sweep_report,
        "logs_dir": logs_dir,
    }


def resolve_provider_config(project_root) -> tuple[Path | None, str]:
    """Locate a project's provider config. Returns (path, source).

    `source` is part of the answer, not a debugging extra. When v3.4.3 renamed the file
    from `opencode.json` to `second_agent.json`, one resolver kept the old literal — so
    every project silently ran on the tool defaults: another model, another timeout, no
    error anywhere. The only visible symptom was quota burning on a provider nobody had
    selected. Resolution lives here, next to the names it resolves, so the next rename
    has one place to miss instead of several.

    A `None` path means no project-local file exists. That used to fall back to the
    tool-level config/second_agent.json, and that fallback is how one stale machine-wide
    choice (`opencode/mimo-v2.5-free`) reached every project initialised from it. There is
    no fallback any more: the executor refuses a `missing` config with a next_action.
    """
    workflow_dir = Path(project_root) / WORKFLOW_DIRNAME
    for name, source in (
        (PROVIDER_CONFIG_NAME, "project"),
        (LEGACY_PROVIDER_CONFIG_NAME, "project_legacy"),
    ):
        candidate = workflow_dir / name
        if candidate.exists():
            return candidate, source
    return None, "missing"


# What agent-workflow recorded to reach a checkout's main.py/check.py. Exoma runs as
# `<python> -m exoma`, so upgrade removes these from a workspace it takes over.
LEGACY_RUNTIME_KEYS = ("agent_workflow_path", "main_py_path", "check_py_path", "tool_dir")


def _tool_paths(agent_workflow_path: str | None = None) -> dict:
    """How .workflow's scripts reach this runtime: `<python> -m exoma`, both absolute.

    `package_root` is the directory holding the `exoma` package (site-packages, or a source
    checkout's src/); the scripts put it on PYTHONPATH so `-m exoma` resolves to this build
    whatever the interpreter has installed. `agent_workflow_path` is accepted from callers
    written against agent-workflow, which pointed a workspace at a checkout's main.py; the
    workspace always records the build that is running.
    """
    from exoma.config.settings import COMPONENT_VERSIONS, PACKAGE_PARENT, TOOL_VERSION
    from exoma.utils import osutil

    return {
        "python": str(Path(osutil.python_exe())),
        "package_root": str(PACKAGE_PARENT),
        "tool_version": TOOL_VERSION,
        "runtime_version": COMPONENT_VERSIONS["runtime"],
    }


def read_json_file(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"invalid JSON object in {path}")
    return data


def ensure_valid_json_or_create(path: Path, factory) -> tuple[str, dict]:
    if path.exists():
        return "existing", read_json_file(path)
    payload = factory()
    atomic_write_json(path, payload)
    return "created", payload

