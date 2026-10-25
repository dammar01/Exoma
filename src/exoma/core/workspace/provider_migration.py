"""One-shot migration of the v3.4.2 OpenCode-named keys to provider-agnostic ones.

The rename in v3.4.3 is a hard one — no read-side aliases — so this is the only thing
standing between an existing workspace and silent data loss. Worth being precise about
why `_merge_provider_config` cannot do the job: that function is additive by contract
("an existing value is the user's tuning"), so on its own it would write the new keys at
their DEFAULTS, leave the old keys orphaned, and the user's tuning would vanish without
an error anywhere. Migration therefore moves values, then removes the old key.

Three surfaces, each idempotent and safe to re-run:
  * .workflow/second_agent.json  <- .workflow/opencode.json  (file + key rename)
  * .workflow/config.json        runtime.opencode_config -> runtime.provider_config
  * storage/sessions/*.json      opencode_session_id -> provider_session_id

The session rewrite matters more than it looks: those files carry the resumable
provider session, and dropping the field would make every existing session re-bootstrap
— quota spent to reproduce state that was already on disk.
"""

from pathlib import Path

from exoma.core.workspace.workspace_paths import (
    LEGACY_PROVIDER_CONFIG_NAME,
    PROVIDER_CONFIG_NAME,
    WORKFLOW_DIRNAME,
    atomic_write_json,
    read_json_file,
    workflow_paths,
)

# One definition, shared with the resolver in core/workspace_paths. Two hand-written
# copies of a filename is what let the reader and the writer drift apart in the first place.
LEGACY_CONFIG_NAME = LEGACY_PROVIDER_CONFIG_NAME

# old key -> new key
CONFIG_KEY_MOVES = {
    "opencode_command": "provider_command",
    "opencode_agent": "provider_agent",
}
RUNTIME_KEY_MOVES = {"opencode_config": "provider_config"}
SESSION_KEY_MOVES = {"opencode_session_id": "provider_session_id"}


def _move_keys(payload: dict, moves: dict) -> list[str]:
    """Rename in place, preserving the value. Returns the keys actually moved."""
    moved = []
    for old, new in moves.items():
        if old not in payload:
            continue
        value = payload.pop(old)
        # A new key already present wins: it means migration ran before and the user
        # has since tuned the new name. Never let a stale legacy value overwrite it.
        payload.setdefault(new, value)
        moved.append(f"{old}->{new}")
    return moved


def migrate_provider_config(project_root: Path) -> dict:
    """Rename .workflow/opencode.json to second_agent.json and translate its keys."""
    workflow_dir = Path(project_root) / WORKFLOW_DIRNAME
    legacy = workflow_dir / LEGACY_CONFIG_NAME
    current = workflow_dir / PROVIDER_CONFIG_NAME
    result = {"file": None, "keys": [], "status": "nothing_to_do"}

    if not legacy.exists():
        if current.exists():
            try:
                payload = read_json_file(current)
            except (OSError, ValueError):
                return {**result, "status": "unreadable"}
            moved = _move_keys(payload, CONFIG_KEY_MOVES)
            if moved:
                atomic_write_json(current, payload)
                return {"file": str(current), "keys": moved, "status": "keys_migrated"}
        return result

    try:
        payload = read_json_file(legacy)
    except (OSError, ValueError):
        # Malformed legacy config: leave it alone rather than lose whatever is in it.
        return {**result, "file": str(legacy), "status": "unreadable"}

    moved = _move_keys(payload, CONFIG_KEY_MOVES)
    if current.exists():
        # Both files present — the new one is authoritative; the legacy one is residue
        # from a partly-applied upgrade and is simply retired.
        legacy.unlink()
        return {"file": str(current), "keys": moved, "status": "legacy_discarded"}

    atomic_write_json(current, payload)
    legacy.unlink()
    return {"file": str(current), "keys": moved, "status": "migrated"}


def migrate_workspace_config(project_root: Path) -> dict:
    """Point runtime.provider_config at the renamed file."""
    config_path = Path(project_root) / WORKFLOW_DIRNAME / "config.json"
    result = {"file": str(config_path), "keys": [], "status": "nothing_to_do"}
    if not config_path.exists():
        return {**result, "status": "absent"}
    try:
        payload = read_json_file(config_path)
    except (OSError, ValueError):
        return {**result, "status": "unreadable"}

    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        return result

    moved = _move_keys(runtime, RUNTIME_KEY_MOVES)
    stale = runtime.get("provider_config")
    if isinstance(stale, str) and stale.endswith(LEGACY_CONFIG_NAME):
        runtime["provider_config"] = f"{WORKFLOW_DIRNAME}/{PROVIDER_CONFIG_NAME}"
        moved.append("provider_config->second_agent.json")
    if not moved:
        return result
    atomic_write_json(config_path, payload)
    return {"file": str(config_path), "keys": moved, "status": "migrated"}


def migrate_session_store(session_dir: Path) -> dict:
    """Rename the provider session field across stored sessions."""
    directory = Path(session_dir)
    result = {"dir": str(directory), "migrated": 0, "status": "nothing_to_do"}
    if not directory.is_dir():
        return {**result, "status": "absent"}
    migrated = 0
    for path in directory.glob("*.json"):
        try:
            payload = read_json_file(path)
        except (OSError, ValueError):
            continue  # a corrupt session is not worth failing an upgrade over
        if _move_keys(payload, SESSION_KEY_MOVES):
            atomic_write_json(path, payload)
            migrated += 1
    return {
        "dir": str(directory),
        "migrated": migrated,
        "status": "migrated" if migrated else "nothing_to_do",
    }


def session_store_dirs(project_root: Path, session_dir: Path | None = None) -> list[Path]:
    """Every directory that can hold provider session records.

    Two of them, and missing the second is what made the first version of this module
    useless in practice: `config.settings.SESSION_DIR` is only the fallback. The store
    the runtime actually reads is project-local — `main._session_manager_for()` points
    SessionManager at `.workflow/data/provider-sessions/` (the root on a v3.5.x layout) whenever the default manager is in
    play, which is every real run.
    """
    if session_dir is None:
        from exoma.config.settings import SESSION_DIR

        session_dir = SESSION_DIR
    return [
        workflow_paths(Path(project_root))["provider_sessions_dir"],
        Path(session_dir),
    ]


def migrate_provider_keys(project_root: Path, session_dir: Path | None = None) -> dict:
    """Run every migration surface. Idempotent; safe on a fresh workspace."""
    stores = [migrate_session_store(d) for d in session_store_dirs(project_root, session_dir)]
    return {
        "provider_config": migrate_provider_config(project_root),
        "workspace_config": migrate_workspace_config(project_root),
        "sessions": {
            "stores": stores,
            "migrated": sum(s.get("migrated", 0) for s in stores),
        },
    }
