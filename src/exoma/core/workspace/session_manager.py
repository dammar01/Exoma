import json
import re
from datetime import datetime, timezone
from pathlib import Path

from exoma.config.settings import SESSION_DIR
from exoma.core.workspace.workspace_paths import atomic_write_json

# A provider thread id belongs to the provider that issued it. opencode ids carry a `ses_`
# prefix; codex and agy issue opaque ids that never do. That is the only provenance a
# record written before `provider_sessions` existed can offer, so it is used exactly once,
# to adopt a legacy id, and never to route a live call.
_OPENCODE_ID_PREFIX = "ses_"


def _legacy_id_fits(provider: str, provider_session_id: str) -> bool:
    if provider == "opencode":
        return provider_session_id.startswith(_OPENCODE_ID_PREFIX)
    return not provider_session_id.startswith(_OPENCODE_ID_PREFIX)


def bind_provider(session: dict, provider: str | None) -> bool:
    """Point `provider_session_id` at the thread THIS provider issued. True if changed.

    One record used to hold one thread id whoever had issued it, so switching the second
    agent mid-session handed the new provider the old one's id to resume, and it refused
    (opencode: "Session not found") before doing any work. Threads are now kept per
    provider in `provider_sessions`; `provider_session_id` stays as the active provider's
    entry because every reader of the record (adapters, recovery, the e2e continuation)
    already reads that key.

    A bare id on a record that was never bound to a provider — a pre-3.6.0 record, or one
    written before its first call (recovery stores the captured id that way) — is adopted
    only when its shape fits the provider now selected, otherwise dropped: a fresh thread
    costs one bootstrap, a foreign resume costs the call. Once bound, a record never
    adopts again, so a switch cannot inherit the previous provider's id. codex and agy ids
    are indistinguishable by shape, so an unbound id from one can still be offered to the
    other once.
    """
    if not provider:
        return False
    before = (
        dict(session.get("provider_sessions") or {}),
        session.get("provider_session_id"),
        session.get("provider"),
    )
    threads = session.get("provider_sessions")
    if not isinstance(threads, dict):
        threads = session["provider_sessions"] = {}
    if session.get("provider") is None and provider not in threads:
        unbound = session.get("provider_session_id")
        if isinstance(unbound, str) and unbound and _legacy_id_fits(provider, unbound):
            threads[provider] = unbound
    session["provider"] = provider
    session["provider_session_id"] = threads.get(provider)
    return before != (threads, session["provider_session_id"], provider)


def record_provider_session(session: dict, provider_session_id: str) -> None:
    """Store a captured thread id under the provider the record is bound to."""
    session["provider_session_id"] = provider_session_id
    provider = session.get("provider")
    if provider:
        threads = session.get("provider_sessions")
        if not isinstance(threads, dict):
            threads = session["provider_sessions"] = {}
        threads[provider] = provider_session_id


class SessionManager:
    def __init__(self, session_dir: Path = SESSION_DIR) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def load_or_create(self, session_id: str) -> dict:
        if not session_id or not session_id.strip():
            raise ValueError("session_id is required")

        path = self._path_for(session_id)
        if path.exists():
            with path.open("r", encoding="utf-8") as file:
                session = json.load(file)
            if self._migrate_legacy_key(session):
                self._save(session)
            return session

        now = self._now()
        session = {
            "session_id": session_id,
            "provider_session_id": None,
            "provider_sessions": {},
            "history": {
                "created_at": now,
                "updated_at": now,
                "runs": [],
            },
        }
        self._save(session)
        return session

    @staticmethod
    def _migrate_legacy_key(session: dict) -> bool:
        """Move a v3.4.2 `opencode_session_id` onto the current key. True if changed.

        Runs on every load rather than only on upgrade. The bulk migration in
        core/provider_migration.py fires from `upgrade_workflow_workspace`, but nothing
        forces a user to upgrade before their next delegated call — and a session record
        the reader cannot understand does not fail loudly. It reads as "no session yet",
        so the adapter bootstraps a fresh one: a full model round trip, on every call,
        to rebuild an id that was sitting on disk the whole time.
        """
        if "opencode_session_id" not in session:
            return False
        legacy = session.pop("opencode_session_id")
        if session.get("provider_session_id") is None and legacy:
            session["provider_session_id"] = legacy
        return True

    def update_provider_session_id(self, session: dict, provider_session_id: str) -> None:
        record_provider_session(session, provider_session_id)
        self._save(session)

    def bind_provider(self, session: dict, provider: str | None) -> None:
        if bind_provider(session, provider):
            self._save(session)

    def record_run(self, session: dict, command: str) -> None:
        history = session.setdefault("history", {})
        runs = history.setdefault("runs", [])
        runs.append(
            {
                "command": command,
                "timestamp": self._now(),
            }
        )
        history["updated_at"] = self._now()
        self._save(session)

    def _save(self, session: dict) -> None:
        # The shared primitive, not a hand-rolled `<id>.tmp`: a fixed temp name let two
        # writers of one session clobber each other's temp file, and a bare `replace` lost
        # the provider thread id whenever a reader held the record open on Windows.
        atomic_write_json(self._path_for(session["session_id"]), session)

    def ensure_writable(self, session: dict) -> None:
        """Raise unless this session's record can be written right now.

        Called before a provider is spawned, so storage that cannot take the thread id
        fails the call before any work is paid for, instead of in the middle of it.
        """
        self._save(session)

    def _path_for(self, session_id: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
        return self.session_dir / f"{safe_name}.json"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
