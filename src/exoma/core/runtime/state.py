"""Workspace creation, session binding, and runtime state updates."""

from exoma.adapters.install.opencode_install import _copy_provider_config
from exoma.core.workspace.workspace_paths import atomic_write_json
from exoma.core.workspace.workspace_paths import atomic_write_text
from exoma.core.workspace.workspace_paths import ensure_valid_json_or_create
from exoma.core.workspace.workspace_paths import now_iso
from exoma.core.workspace.workspace_paths import read_json_file
from exoma.core.workspace.workspace_paths import workflow_paths
from datetime import datetime
from datetime import timezone
from pathlib import Path
from urllib.parse import urlsplit
import json
import os
import secrets
import stat
import tempfile
from exoma.core.runtime.agent_output import extract_lines_by_prefix, maybe_extract_plan_readiness, parse_questions
from exoma.core.runtime.config_defaults import default_command_cache, default_config, default_scope, default_state, merge_config_defaults
from exoma.core.runtime.scripts import _generate_run_scripts
from exoma.core.workspace.workspace_paths import _tool_paths
from exoma.core.runtime.upgrade import _install_project_boundary, ensure_root_gitignore_entry, needs_upgrade, upgrade_workflow_workspace, workspace_versions


def _capabilities_path(project_root: Path) -> Path:
    return workflow_paths(project_root)["capabilities"]

def fanout_capability(project_root: Path) -> bool | None:
    """Learned opencode fan-out capability for this project.

    True/False once a delegated run has revealed it; None while unprobed. Default fan-out
    stays ON — this only flips it OFF after opencode itself reports no spawn tool, so the
    prompt stops carrying a fan-out plan opencode cannot execute.

    A False verdict EXPIRES. It rests on one sentence the second agent wrote about itself,
    and that sentence is not always true. Worse, it is self-sealing: with fan-out off the
    prompt no longer asks for fan-out, so no later run can produce the evidence that would
    turn it back on. Ageing it out means a wrong verdict costs one stale window, not the
    life of the project. True never expires — a capability that was demonstrated once does
    not need re-proving.
    """
    from exoma.config.settings import DEFAULT_FANOUT_RECHECK_HOURS

    try:
        data = read_json_file(_capabilities_path(project_root))
    except (OSError, ValueError):
        return None
    value = data.get("subagent_fanout_capable") if isinstance(data, dict) else None
    if not isinstance(value, bool):
        return None
    if value or DEFAULT_FANOUT_RECHECK_HOURS <= 0:
        return value
    stamped = data.get("subagent_fanout_capable_at")
    if not isinstance(stamped, str):
        # No timestamp means it was written by an older build. Retry rather than honour a
        # verdict whose age cannot be established.
        return None
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(stamped)
    except (TypeError, ValueError):
        return None
    if age.total_seconds() >= DEFAULT_FANOUT_RECHECK_HOURS * 3600:
        return None
    return value

def set_fanout_capability(project_root: Path, capable: bool) -> None:
    """Persist the observed fan-out capability, refreshing when it is merely reconfirmed.

    An unchanged verdict used to be a no-op. Once False verdicts expire, that silently
    broke them: a run that observed the same limitation again left the old timestamp in
    place, so the verdict aged out despite being confirmed every single time. The stamp
    records when the evidence was last SEEN, not when the answer last changed.
    """
    path = _capabilities_path(project_root)
    try:
        data = read_json_file(path)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["subagent_fanout_capable"] = capable
    data["subagent_fanout_capable_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)

def ensure_workflow_workspace(
    project_root: Path, agent_workflow_path: str | None = None, host: str | None = None
) -> dict:
    """Create STATIC scaffolding only. Per-session state/scope/cache/runtime are
    created lazily under sessions/<sid>/ on first delegated call (see load_workspace_state).

    `agent_workflow_path` is the legacy alias agent-workflow callers pass; the scripts
    always call the running build (`_tool_paths`).
    """
    paths = workflow_paths(project_root)
    paths["workflow_dir"].mkdir(parents=True, exist_ok=True)
    paths["reports_dir"].mkdir(parents=True, exist_ok=True)
    # logs are per-session: created lazily under sessions/<sid>/logs on first delegated
    # call. Init only scaffolds the sessions/ root — no vestigial .workflow/logs.
    paths["sessions_dir"].mkdir(parents=True, exist_ok=True)

    created_files: list[str] = []
    existing_files: list[str] = []

    status, _payload = ensure_valid_json_or_create(
        paths["config"], lambda: default_config(project_root, agent_workflow_path)
    )
    (created_files if status == "created" else existing_files).append(
        str(paths["config"])
    )

    if paths["gitignore"].exists():
        existing_files.append(str(paths["gitignore"]))
    else:
        atomic_write_text(paths["gitignore"], "*\n")
        created_files.append(str(paths["gitignore"]))

    tool = _tool_paths()
    opencode_copied = _copy_provider_config(project_root, host=host)
    # Comes after the config.json write above: the boundary dispatch reads
    # runtime.second_agent from it to learn which provider this workspace runs.
    project_boundary = _install_project_boundary(project_root)
    generated_scripts = _generate_run_scripts(project_root)

    gitignore_updated = ensure_root_gitignore_entry(project_root)

    # A pre-existing config.json is left untouched above, so init on an older workspace
    # used to hand back one still stamped by a previous build and merely SAY so in a
    # return field nobody reads. Every later fix then propagates only to whoever
    # remembered to run upgrade separately. Close it here: upgrade is idempotent and
    # writes only what differs, so running it is cheap and running it twice is a no-op.
    auto_upgrade: dict | str | None = None
    if status != "created" and needs_upgrade(project_root):
        try:
            auto_upgrade = upgrade_workflow_workspace(project_root, agent_workflow_path)
        except ValueError as exc:
            # A live job or an unreadable config. Init still succeeded at scaffolding, so
            # do not fail it — but hand back the exact next command instead of a flag.
            auto_upgrade = (
                f"SKIPPED — {exc}. Resolve that, then run "
                f"`--command upgrade --work-dir {project_root}`"
            )

    return {
        "project_root": str(project_root),
        "workflow_dir": str(paths["workflow_dir"]),
        "created_files": created_files,
        "existing_files": existing_files,
        "gitignore_updated": gitignore_updated,
        "auto_upgrade": auto_upgrade,
        # Recomputed AFTER the auto-upgrade above, so this reports the state the caller
        # is actually left with rather than the one it walked in on.
        "upgrade_needed": needs_upgrade(project_root),
        "versions": workspace_versions(project_root),
        "provider_config": opencode_copied,
        "project_opencode": project_boundary,
        "generated_scripts": generated_scripts,
        "tool": tool,
    }

def load_workspace_state(project_root: Path, session_id: str | None = None) -> dict:
    """Load workspace state for a session, lazily creating per-session defaults.
    config.json is static (must exist from init); state/scope/cache are per-session."""
    paths = workflow_paths(project_root, session_id)
    paths["session_dir"].mkdir(parents=True, exist_ok=True)
    _, state = ensure_valid_json_or_create(
        paths["state"], lambda: default_state(project_root)
    )
    _, scope = ensure_valid_json_or_create(paths["scope"], default_scope)
    _, command_cache = ensure_valid_json_or_create(
        paths["command_cache"], default_command_cache
    )
    config, config_changed = merge_config_defaults(read_json_file(paths["config"]))
    if config_changed:
        atomic_write_json(paths["config"], config)
    return {
        "config": config,
        "state": state,
        "scope": scope,
        "command_cache": command_cache,
        "paths": paths,
    }

def reset_active_workflow_state(project_root: Path, session_id: str) -> dict:
    loaded = load_workspace_state(project_root, session_id)
    state = default_state(project_root)
    state["session"] = {"id": session_id, "bound_at": now_iso()}
    scope = default_scope()
    scope["session_id"] = session_id
    command_cache = default_command_cache()
    command_cache["session_id"] = session_id
    atomic_write_json(loaded["paths"]["state"], state)
    atomic_write_json(loaded["paths"]["scope"], scope)
    atomic_write_json(loaded["paths"]["command_cache"], command_cache)
    return {
        "state": state,
        "scope": scope,
        "command_cache": command_cache,
        "paths": loaded["paths"],
    }

def bind_session(project_root: Path, session_id: str) -> dict:
    """Bind a session to its OWN per-session state dir. Because state/scope/cache are
    keyed by session_id (sessions/<sid>/), two concurrent main agents on the same
    project never collide — so there is no cross-session mismatch and no reset. The
    session record is stamped on first bind."""
    loaded = load_workspace_state(project_root, session_id)
    state = loaded["state"]
    current = state.get("session") or {}
    current_id = current.get("id") if isinstance(current, dict) else None
    if current_id != session_id:
        state["session"] = {"id": session_id, "bound_at": now_iso()}
        atomic_write_json(loaded["paths"]["state"], state)
        scope = loaded["scope"]
        scope["session_id"] = session_id
        atomic_write_json(loaded["paths"]["scope"], scope)
        command_cache = loaded["command_cache"]
        command_cache["session_id"] = session_id
        atomic_write_json(loaded["paths"]["command_cache"], command_cache)
        loaded["state"] = state
        loaded["scope"] = scope
        loaded["command_cache"] = command_cache
    loaded["session_reset"] = False
    return loaded

def update_state_from_agent_output(
    project_root: Path, command: str, objective: str, content: str, session_id: str
) -> dict:
    loaded = bind_session(project_root, session_id=session_id)
    state = loaded["state"]
    state["workflow"]["stage"] = command
    state["workflow"]["last_command"] = command
    state["workflow"]["objective"] = objective
    state["workflow"]["plan_readiness"] = maybe_extract_plan_readiness(content)
    state["context"]["evidence_summary"] = [
        line.strip() for line in content.splitlines() if line.strip()
    ][:10]
    state["context"]["affected_files"] = extract_lines_by_prefix(
        content, ("file:", "path:")
    )
    state["context"]["affected_symbols"] = extract_lines_by_prefix(
        content, ("symbol:",)
    )
    state["context"]["assumptions"] = extract_lines_by_prefix(content, ("assumption:",))
    state["context"]["risks"] = extract_lines_by_prefix(content, ("risk:",))
    questions = parse_questions(content)
    state["context"]["open_questions"] = questions["open_questions"]
    state["context"]["resolvable_uncertainties"] = questions["resolvable_uncertainties"]
    state["guards"]["state_version"] = int(state["guards"].get("state_version", 0)) + 1
    atomic_write_json(loaded["paths"]["state"], state)
    return state

def stamp_task_chain(
    project_root: Path, session_id: str, correlation_id: str
) -> dict:
    """Record a plan's correlation id as this session's active task chain.

    The execute and verify that follow a plan are the same piece of work, but their task
    texts differ, so deriving a correlation id from each produced three ids per chain —
    rework and first-pass correctness had no subject. The plan's own derived id is the
    chain's identity; recording it here is the single hop that lets later commands adopt
    it. A new plan overwrites the chain: the latest plan is what execute/verify follow.
    """
    loaded = load_workspace_state(project_root, session_id)
    state = loaded["state"]
    state["chain"] = {"correlation_id": str(correlation_id), "started_at": now_iso()}
    atomic_write_json(loaded["paths"]["state"], state)
    return state

def active_chain_correlation(project_root: Path, session_id: str) -> str | None:
    """The correlation id of the chain the last plan opened, or None outside a chain.

    Fail-open by contract: this feeds telemetry, and an unreadable state file must make
    the caller fall back to deriving an id, never fail the call it is measuring.
    """
    try:
        loaded = load_workspace_state(project_root, session_id)
    except (OSError, ValueError):
        return None
    chain = loaded["state"].get("chain")
    value = chain.get("correlation_id") if isinstance(chain, dict) else None
    return str(value) if value else None

def update_plan_scope(project_root: Path, content: str, session_id: str) -> dict:
    loaded = load_workspace_state(project_root, session_id)
    scope = loaded["scope"]
    before = json.dumps(scope, sort_keys=True)
    scope["session_id"] = session_id
    scope["editable_scope"] = extract_lines_by_prefix(content, ("editable:",))
    scope["readable_scope"] = extract_lines_by_prefix(content, ("readable:",))
    scope["impact_radius"] = extract_lines_by_prefix(content, ("impact:",))
    scope["out_of_scope"] = extract_lines_by_prefix(content, ("out_of_scope:",))
    scope["verification_targets"] = extract_lines_by_prefix(content, ("verify:",))
    after = json.dumps(scope, sort_keys=True)

    state = loaded["state"]
    if before != after:
        state["guards"]["scope_version"] = (
            int(state["guards"].get("scope_version", 0)) + 1
        )
        atomic_write_json(loaded["paths"]["state"], state)
    atomic_write_json(loaded["paths"]["scope"], scope)
    return scope

# How many times a browser run may end the same way before the runtime stops starting
# another one. Three, because two is still a fair test of flakiness and the fourth has
# never once been the attempt that worked.
E2E_REPEAT_LIMIT = 3


def e2e_repeat_state(project_root: Path, session_id: str) -> dict:
    """This session's record of browser runs that keep ending the same way."""
    try:
        loaded = load_workspace_state(project_root, session_id)
    except (OSError, ValueError):
        return {}
    record = loaded["state"].get("e2e_repeat")
    if not isinstance(record, dict) or not record:
        return {}
    # Coerced here rather than trusted at the reader. This file is on disk and can be
    # edited, truncated or written by an older build, and a brake that raises on a
    # malformed streak would abort the run it exists to protect.
    try:
        streak = int(record.get("streak") or 0)
    except (TypeError, ValueError):
        return {}
    bucket = _repeat_bucket_of(record)
    # An `app` streak from a build that counted by bucket alone says nothing about WHICH
    # failure repeated — it may be three different ones. Read as no streak rather than as
    # a limit reached: the next identical failure counts again from one.
    if bucket == "app" and not record.get("signature"):
        return {}
    return {**record, "streak": streak, "bucket": bucket}


def _repeat_bucket_of(record: dict) -> str | None:
    """This record's bucket, deriving one for a record written before buckets existed.

    The same comment above applies: the file is on disk and an older build may have
    written it. That build keyed on the exact reason, so its records carry `reason` and no
    `bucket` — and without a bucket the next run's outcome never matches, restarting a
    streak that had already counted to two. Derived from the reason it does carry, through
    the one mapping, rather than re-stated here.
    """
    bucket = record.get("bucket")
    if bucket:
        return str(bucket)
    reason = record.get("reason")
    if not reason:
        return None
    from exoma.core.evidence.e2e.classify import repeat_bucket

    return repeat_bucket("incomplete", str(reason))


def _windows_receipt_key(data: bytes, *, protect: bool) -> bytes:
    """DPAPI binds the stored key to this Windows user, including on Python 3.11."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]

    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.c_void_p))
    output = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    convert = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    if not convert(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(output.data))


def browser_receipt_key(project_root: Path) -> bytes | None:
    """Publish one complete key per user. Temporary files and losing creators stay private."""
    from exoma.config.settings import DATA_DIR

    try:
        directory = (DATA_DIR / "private").resolve()
        if directory.is_relative_to(project_root.resolve()):
            return None
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            info = directory.stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return None
        path = directory / "browser-receipt.key"
        if not path.exists():
            key = secrets.token_bytes(32)
            encoded = _windows_receipt_key(key, protect=True) if os.name == "nt" else key
            fd, name = tempfile.mkstemp(dir=directory)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "wb") as pending:
                    pending.write(encoded)
                    pending.flush()
                    os.fsync(pending.fileno())
                os.link(temporary, path)
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4096:
            return None
        if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
            return None
        encoded = path.read_bytes()
        key = _windows_receipt_key(encoded, protect=False) if os.name == "nt" else encoded
        return key if len(key) == 32 else None
    except OSError:
        return None


def e2e_coverage_gaps(project_root: Path, session_id: str, origin: str) -> list[dict]:
    state = load_workspace_state(project_root, session_id)["state"]
    by_origin = state.get("e2e_coverage_gaps", {})
    if not isinstance(by_origin, dict):
        raise ValueError("invalid browser coverage history")
    gaps = []
    canonical = reservation_origin(origin)
    for spelling, history in by_origin.items():
        if reservation_origin(spelling) == canonical:
            if not isinstance(history, list):
                raise ValueError("invalid browser assertion history")
            gaps.extend(history)
    if not all(
        isinstance(gap, dict) and type(gap.get("attempt")) is int
        and all(isinstance(gap.get(field), str) for field in ("proof", "claim", "step", "run"))
        and (gap.get("target") is None or isinstance(gap["target"], str))
        and all(_receipt_digest(gap[k]) for k in ("miss_page", "input_token")
                if k in gap and not (k == "input_token" and gap[k] is None))
        and isinstance(gap.get("legacy_urls", {}), dict)
        and all(_receipt_digest(digest) for digest in gap.get("legacy_urls", {}).values())
        and isinstance(gap.get("spent_slots", []), list)
        and all(_receipt_digest(slot) for slot in gap.get("spent_slots", []))
        and all(type(gap.get(flag, False)) is bool for flag in ("unseen", "removed"))
        for gap in gaps
    ):
        raise ValueError("invalid browser assertion history")
    return gaps


def _receipt_digest(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def e2e_dispatch_reserved(record: dict, prefix: list[str], *, alternatives=None) -> bool:
    reservations = record.get("admitted", [])
    # Older builds kept a single digest that could include edits to the unused
    # tail. Without the original prefix that reservation cannot be compared.
    if not isinstance(reservations, list):
        return True
    choices = alternatives if alternatives is not None else [{key} for key in prefix]
    for prior in reservations:
        if not isinstance(prior, list) or not all(_receipt_digest(key) for key in prior):
            return True
        if all(key in allowed for key, allowed in zip(prior, choices)):
            return True
    return False


def reservation_origin(origin: str) -> str:
    """A project-test reservation follows the HTTP origin, independent of URL spelling."""
    try:
        parts = urlsplit(origin)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return origin  # preflight diagnoses an invalid URL
    defaults = {"http": 80, "https": 443}
    if not host or parts.scheme not in defaults:
        return origin
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != defaults[parts.scheme]:
        authority += f":{port}"
    return f"{parts.scheme}://{authority}"


def e2e_pending_tests(project_root: Path, session_id: str, origin: str) -> dict:
    """Move interrupted tests out of legacy streaks before any gate can replace them."""
    loaded = load_workspace_state(project_root, session_id)
    state = loaded["state"]
    by_origin = state.setdefault("e2e_pending_tests", {})
    if not isinstance(by_origin, dict):
        raise ValueError("invalid pending project tests")
    repeat = state.get("e2e_repeat")
    if isinstance(repeat, dict):
        # The oldest combined reservation did not identify the test command.
        legacy = repeat.get("test_dispatches", [None] if "admitted" in repeat else [])
        if legacy != []:
            previous_origin = repeat.get("origin")
            if not isinstance(previous_origin, str) or not previous_origin:
                raise ValueError("missing origin of pending project tests")
            prior = by_origin.setdefault(previous_origin, [])
            if not isinstance(prior, list):
                raise ValueError("invalid pending project tests")
            prior.extend(legacy if isinstance(legacy, list) else [None])
            repeat["test_dispatches"] = []
            atomic_write_json(loaded["paths"]["state"], state)
    histories = {}
    for spelling, pending in by_origin.items():
        if reservation_origin(spelling) == origin:
            if not isinstance(pending, list):
                raise ValueError("invalid pending project tests")
            if pending:
                histories[spelling] = pending  # legacy HMACs bind the original spelling
    return histories


def record_e2e_test_dispatch(project_root: Path, session_id: str, origin: str, receipt: dict, *, completed=False) -> None:
    """Reserve before launch or retire this command after its report, under the invocation lock."""
    loaded = load_workspace_state(project_root, session_id)
    state = loaded["state"]
    by_origin = state.setdefault("e2e_pending_tests", {})
    if not isinstance(by_origin, dict):
        raise ValueError("invalid pending project tests")
    pending = by_origin.setdefault(origin, [])
    if not isinstance(pending, list):
        raise ValueError("invalid pending project tests")
    if completed:
        pending.remove(receipt)
        if not pending:
            del by_origin[origin]
    else:
        pending.append(receipt)
    atomic_write_json(loaded["paths"]["state"], state)


def e2e_tests_reserved(history: list, candidate: dict) -> bool:
    for prior in history:
        if not isinstance(prior, dict) or not _receipt_digest(prior.get("key")) or prior["key"] != candidate["key"]:
            return True
        files = prior.get("files")
        if not isinstance(files, list) or not files or not all(_receipt_digest(item) for item in files):
            return True
        if not set(candidate["files"]).isdisjoint(files):
            return True
    return False


def save_e2e_retry(project_root: Path, session_id: str, origin: str, gaps: list[dict],
                   admitted: list[str] | None = None) -> None:
    """Under the invocation lock, reserve a strategy and retain outstanding evidence."""
    loaded = load_workspace_state(project_root, session_id)
    state = loaded["state"]
    by_origin = state.setdefault("e2e_coverage_gaps", {})
    origin = reservation_origin(origin)
    for spelling in list(by_origin):
        if reservation_origin(spelling) == origin:
            del by_origin[spelling]
    if gaps:
        by_origin[origin] = gaps
    if admitted is not None:
        repeat = state["e2e_repeat"]
        if "admitted" not in repeat:
            repeat.setdefault("test_dispatches", [])
        reservations = repeat.setdefault("admitted", [])
        if admitted not in reservations:
            reservations.append(admitted)
    atomic_write_json(loaded["paths"]["state"], state)


def note_e2e_outcome(
    project_root: Path,
    session_id: str,
    origin: str,
    bucket: str | None,
    reason: str | None = None,
    source: str = "browser",
    fingerprint: str | None = None,
    signature: str | None = None,
    runtime: str | None = None,
    cause: str | None = None,
    strategy: dict | None = None,
    legacy_signature: str | None = None,
    reset_streak: bool = False,
) -> int:
    """Fold one finished browser run into the repeat counter; return the new streak.

    Keyed on origin and failure BUCKET, deliberately not on the scenario. The runner
    already stops retrying within a run when two attempts match step for step; what it
    could not see was the same failure arriving across separate invocations, each one with
    a slightly edited scenario. That is the shape of the loop worth breaking: twelve runs
    against one environment problem, every one of them editing steps that were never what
    was wrong. Editing the scenario changes the steps, so a step-level key would reset each
    time and count to one forever.

    The bucket rather than the exact reason, because one environment problem answers to
    several reasons in turn — `base_url_unreachable`, then `stuck`, then `harness_error` —
    and an exact key started a new streak of one at every rename. `reason` still travels,
    as the detail the brake quotes back; it is what the person has to fix, and the bucket
    is only how the runtime decides it has seen enough.

    A pass clears the streak, since what it was counting is gone.

    `fingerprint` is the project's source state when this run failed
    (`core/evidence/e2e/fingerprint.py`); the newest one is kept. The runner compares it
    with the project's state before braking: a different one means someone changed the
    application since the streak's last failure, and the brake lets that run through
    instead of demanding a new session.

    `source` is which gate wrote this — `preflight` or `browser` — and it exists because
    the reason alone cannot tell them apart. The player emits `playwright_missing` and
    `browser_missing` of its own when a launch fails, the same words preflight uses, so a
    caller deciding "preflight proved this one fixed, retire it" by reading the reason
    would retire a streak of real launch failures too. It is recorded rather than derived
    for the same cause: nothing about the record after the fact says where it came from.

    `signature` narrows the `app` bucket, and only that one (`classify.failure_signature`):
    the page, the field and the condition that failed. The paragraph above is about the
    environment, where a step-level key would never count past one; an application failure
    is the opposite case, where three different failures on one origin are three different
    things to fix, not one loop. So an `app` streak continues only when the signature
    matches, and `timeout`/`harness` stay keyed on the bucket.

    `runtime` is the runtime's own identity when this run failed (version and a digest of
    the browser package's source). The runner releases a streak whose runtime differs, as
    it does for the project fingerprint: a fix to the runtime is a change the project's
    fingerprint cannot see.

    `cause` is the run's diagnosis cause (`classify.diagnose`), newest kept, so a brake that
    fires can say what kept failing in the same terms a single run does.
    """
    try:
        loaded = load_workspace_state(project_root, session_id)
    except (OSError, ValueError):
        return 0
    state = loaded["state"]
    previous = state.get("e2e_repeat") if isinstance(state.get("e2e_repeat"), dict) else {}
    previous_bucket = _repeat_bucket_of(previous) if previous else None
    try:
        previous_streak = int(previous.get("streak") or 0)
    except (TypeError, ValueError):
        previous, previous_streak = {}, 0
    if not bucket:
        record = {}
    elif (
        not reset_streak and isinstance(previous.get("origin"), str)
        and reservation_origin(previous["origin"]) == reservation_origin(origin)
        and previous_bucket == bucket
        and (bucket != "app" or (previous.get("signature") and previous["signature"] in (signature, legacy_signature)))
    ):
        # The newest reason replaces the stored one: when a streak is quoted back it should
        # name what the last run actually said, not the first name the problem went by.
        # `bucket` written explicitly, not inherited from `previous`: a record migrated
        # from an older build has none, and spreading it forward would leave every
        # subsequent run deriving it again from a reason that keeps changing. `source`
        # travels the same way, and for the same reason: a record from an older build
        # carries none, and a streak continued by this gate belongs to this gate.
        # A continued streak keeps the provenance of the WHOLE of it, not of its last
        # run. Two browser failures and then a preflight one all land in `harness`, and
        # writing the newest source over the record turned a streak that was mostly a
        # browser refusing to start into one preflight was free to retire. Anything not
        # written end to end by a single gate is `mixed`, which is nobody's to clear. A
        # record from a build before this field carries no source at all, so it mixes too:
        # unknown provenance is not preflight's.
        record = {
            **previous,
            "origin": origin,
            "bucket": bucket,
            "reason": reason,
            "streak": previous_streak + 1,
            "source": source if previous.get("source") == source else "mixed",
            "fingerprint": fingerprint,
            "signature": signature,
            "runtime": runtime,
            "cause": cause,
        }
    else:
        record = {
            "origin": origin, "bucket": bucket, "reason": reason, "streak": 1, "source": source,
            "fingerprint": fingerprint, "signature": signature, "runtime": runtime, "cause": cause,
        }
    if bucket == "app":
        record.pop("admitted", None)
        record.pop("test_dispatches", None)
        record.pop("strategy", None)
        if strategy is not None:
            record["strategy"] = strategy
    state["e2e_repeat"] = record
    atomic_write_json(loaded["paths"]["state"], state)
    return int(record.get("streak") or 0)


def update_command_cache(project_root: Path, key: str, value, session_id: str) -> dict:
    loaded = load_workspace_state(project_root, session_id)
    command_cache = loaded["command_cache"]
    command_cache["session_id"] = session_id
    command_cache[key] = value
    atomic_write_json(loaded["paths"]["command_cache"], command_cache)
    return command_cache
