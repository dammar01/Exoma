"""Prompt handoff, evidence sidecars, and response/meta snapshots."""

import hashlib
import json
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from exoma.core.evidence.contracts import (
    AUDIT_STREAM_NAME,
    QUALITY_STREAM_NAME,
    TASK_STREAM_NAME,
    USAGE_STREAM_NAME,
)
from exoma.core.workspace.runtime_lock import (
    _runtime_lock_payload,
    acquire_runtime_lock,
    runtime_lock_owned,
)
from exoma.core.workspace.workspace_paths import (
    ARCHIVE_KEEP,
    JSON_INDENT,
    atomic_write_json,
    atomic_write_text,
    now_iso,
    workflow_paths,
)

def _prune_archive(logs_dir: Path, keep: int = ARCHIVE_KEEP) -> None:
    """Keep only the newest `keep` per-run archive folders."""
    if not logs_dir.exists():
        return
    runs = sorted(
        (p for p in logs_dir.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for stale in runs[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def _archive_prompt(logs_dir: Path, prompt_id: str, prompt: str) -> None:
    run_dir = logs_dir / prompt_id
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(run_dir / "prompt.md", prompt)
    atomic_write_text(
        run_dir / "prompt.sha256", hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    )
    _prune_archive(logs_dir)


def write_prompt_handoff(
    project_root: Path,
    command: str,
    session_id: str,
    prompt: str,
    lock_claim: dict | None = None,
) -> dict:
    from exoma.core.runtime.state import load_workspace_state

    loaded = load_workspace_state(project_root, session_id)
    loaded["paths"]["runtime_dir"].mkdir(parents=True, exist_ok=True)
    if lock_claim is not None:
        token = str(lock_claim.get("token") or "")
        owned = bool(token) and runtime_lock_owned(
            loaded["paths"]["lock"], session_id, token
        )
        lock_result = (
            dict(lock_claim)
            if owned
            else {
                "ok": False,
                "stale": False,
                "payload": _runtime_lock_payload(loaded["paths"]["lock"])
                or {"invalid": True},
            }
        )
    else:
        lock_result = acquire_runtime_lock(
            loaded["paths"]["lock"], command, session_id
        )
    if not lock_result["ok"]:
        payload = lock_result.get("payload") or {}
        holder = payload.get("session_id") or "unknown"
        return {
            "ok": False,
            "content": f"runtime lock active for session {holder}",
            "meta": {
                "error_type": "runtime_lock",
                "next_action": "Wait for the in-flight delegated call on this session to finish, then retry; if it is stuck, clear the session's runtime/lock under .workflow/data/sessions/<sid>/.",
                "lock": payload,
                "lock_path": str(loaded["paths"]["lock"]),
            },
        }

    state = loaded["state"]
    prompt_id = (
        f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_"
        f"{command}_{secrets.token_hex(4)}"
    )
    meta = {
        "prompt_id": prompt_id,
        "session_id": session_id,
        "project_root": str(project_root),
        "command": command,
        "state_version": state.get("guards", {}).get("state_version", 1),
        "scope_version": state.get("guards", {}).get("scope_version", 0),
        "created_at": now_iso(),
        "status": "ready",
        "lock_token": lock_result["token"],
    }
    if lock_result["stale"]:
        meta["stale_lock_replaced"] = True

    atomic_write_text(loaded["paths"]["prompt"], prompt)
    atomic_write_json(loaded["paths"]["prompt_meta"], meta)

    _archive_prompt(loaded["paths"]["logs_dir"], prompt_id, prompt)

    state["guards"]["last_prompt_id"] = prompt_id
    atomic_write_json(loaded["paths"]["state"], state)
    return {"ok": True, "meta": meta, "paths": loaded["paths"]}


def write_evidence_sidecars(
    project_root: Path,
    session_id: str | None,
    graph_leads: dict | None,
    known_facts: list[str] | None,
    known_knowledge: list[dict] | None = None,
) -> dict:
    """Persist the task-ranked leads and facts to runtime files for the second agent.

    These used to be injected into the command-line prompt; on Windows that prompt is
    one argv capped at 8191 chars, and an uncapped graph-lead list is what pushed real
    calls over it. The ranking is still computed here (main_agent's runtime), only the
    TRANSPORT moves: the second agent reads leads.json/facts.json itself, keeping the
    prompt focused on the task.

    Rewritten whenever the CONTENT differs — a stale file from a prior task must never be
    read as this task's leads. An identical rewrite is skipped: it produced the same bytes
    at the cost of two temp-file-and-rename cycles on every delegated call, and back-to-back
    calls on an unchanged repo produce identical leads by design.

    Returns the two paths, each with whether it was rewritten.
    """
    paths = workflow_paths(project_root, session_id)
    paths["runtime_dir"].mkdir(parents=True, exist_ok=True)

    def _write_if_changed(path: Path, payload) -> bool:
        # `null`/`[]` are meaningful: they say "computed, nothing relevant", which the
        # second agent must be able to tell apart from a leftover file. So compare against
        # the serialised form, never against emptiness.
        # Must match atomic_write_json byte for byte, or the comparison never matches and
        # the skip silently never fires.
        want = json.dumps(payload, indent=JSON_INDENT)
        try:
            if path.read_text(encoding="utf-8") == want:
                return False
        except (OSError, ValueError):
            pass
        atomic_write_json(path, payload)
        return True

    # Reviewed, Git-tracked project knowledge. Kept in its own sidecar rather than
    # folded into facts.json because the two carry different authority: facts are
    # unreviewed runtime residue, knowledge went through a human. Merging them would
    # hand the second agent one undifferentiated list and no way to weigh it.
    knowledge_path = paths["runtime_dir"] / "knowledge.json"

    leads_written = _write_if_changed(paths["leads"], graph_leads)
    facts_written = _write_if_changed(paths["facts"], known_facts or [])
    knowledge_written = _write_if_changed(knowledge_path, known_knowledge or [])
    return {
        "leads": str(paths["leads"]),
        "facts": str(paths["facts"]),
        "knowledge": str(knowledge_path),
        "leads_written": leads_written,
        "facts_written": facts_written,
        "knowledge_written": knowledge_written,
    }


def write_response_snapshot(
    project_root: Path,
    content: str,
    prompt_id: str | None = None,
    session_id: str | None = None,
) -> None:
    paths = workflow_paths(project_root, session_id)
    atomic_write_text(paths["response_last"], content)
    if prompt_id:
        run_dir = paths["logs_dir"] / prompt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(run_dir / "output.raw.md", content)


def write_first_reply(
    project_root: Path,
    content: str,
    prompt_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Archive the reply a continuation was asked to complete.

    `output.raw.md` holds what the run finally returned. When the continuation supersedes
    the first reply instead of merging with it, the text that failed the contract is the
    one thing kept nowhere — only its length survives, in `continuation_first_reply_chars`.
    Asking afterwards WHY a contract failed then means guessing the shape of a reply that
    no longer exists, and the recovery destroys the evidence for the failure it recovered
    from.
    """
    if not prompt_id or not content:
        return
    try:
        paths = workflow_paths(project_root, session_id)
        run_dir = paths["logs_dir"] / prompt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(run_dir / "output.first.md", content)
    except (OSError, TypeError, ValueError):
        # Fail-open like write_call_meta, and for the same reason: this is diagnostics for
        # a call that already succeeded. A recovered run must not be reported as failed
        # because the archive of its first attempt could not be written.
        pass


def write_redaction_audit(
    project_root: Path,
    session_id: str | None,
    command: str,
    redactions: list[dict],
) -> None:
    """Append-only note that a run produced credential-shaped output.

    Kind and count only — never the matched text. The value was scrubbed at the adapter
    boundary precisely so it would exist nowhere on disk; writing it into the audit trail
    would relocate the leak into the file people grep when investigating one.
    """
    if not redactions:
        return
    paths = workflow_paths(project_root, session_id)
    line = json.dumps(
        {
            "at": now_iso(),
            "session_id": session_id,
            "command": command,
            "redactions": [
                {"kind": item.get("kind"), "count": item.get("count")}
                for item in redactions
            ],
        },
        ensure_ascii=False,
    )
    path = Path(paths["redactions_stream"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _append_stream(project_root: Path, name: str, record: dict) -> None:
    """Append one JSON row to a project-local stream. Never raises.

    Fail-open for the same reason everywhere it is used: these streams observe the call,
    they must not be able to fail it. A dropped row costs one point on a chart; an
    exception escaping here costs the answer the user was waiting for.
    """
    try:
        paths = workflow_paths(project_root, None)
        path = Path(paths["data_dir"]) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except (OSError, TypeError, ValueError):
        pass


def write_audit_record(project_root: Path, record: dict) -> None:
    """Append one delegated call to the project's audit trail.

    Distinct from the usage stream despite overlapping fields, because the two answer to
    different readers and must be able to diverge. Usage is measurement: it may be
    resampled, filtered, or recomputed under a new definition. Audit is a record of what
    was done and by which provider — the thing you read after an incident, where a row
    quietly reinterpreted later is worse than no row at all.

    Scope is honest and narrow: this covers DELEGATED calls, the only actions this runtime
    performs. Edits made by the main agent never pass through this process and are not
    audited here; claiming otherwise would make the trail look complete when it is not.
    """
    _append_stream(project_root, AUDIT_STREAM_NAME, record)


def write_quality_record(project_root: Path, record: dict) -> None:
    """Append one check-run outcome (tests, security sweep) to the quality stream.

    Kept out of the usage stream on purpose: usage rows are delegated calls the runtime
    made, quality rows are checks someone ran against the repo. Merging them would let a
    green test run inflate the count of delegated work.
    """
    _append_stream(project_root, QUALITY_STREAM_NAME, record)


def write_task_event(project_root: Path, record: dict) -> None:
    """Append one task lifecycle event (DEC-015). Fail-open like every observing stream."""
    _append_stream(project_root, TASK_STREAM_NAME, record)


def write_usage_record(project_root: Path, record: dict) -> None:
    """Append one delegated call to the project's usage stream.

    Append-only JSONL beside redactions.jsonl, for the same reason: a metric is only
    worth reading if the history behind it was never rewritten. The per-run archive under
    logs/ already holds richer detail, but it is pruned to the newest ARCHIVE_KEEP runs —
    so it can answer "what happened on that call" and can never answer "what did the last
    hundred calls cost".

    Fail-open like write_call_meta, and for the same reason: this measures the call, it
    must not be able to fail it.
    """
    _append_stream(project_root, USAGE_STREAM_NAME, record)


def write_call_meta(
    project_root: Path,
    prompt_id: str | None,
    session_id: str | None,
    meta: dict,
) -> None:
    """Archive one delegated call's raw outcome next to its prompt/output.

    Exit code, duration, whether it timed out, how it was killed, stderr tail —
    the ground truth needed to characterise real failure modes (rate limits,
    hangs, orphaned children) instead of guessing at them.
    """
    if not prompt_id:
        return
    try:
        paths = workflow_paths(project_root, session_id)
        run_dir = paths["logs_dir"] / prompt_id
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(meta or {})
        payload["recorded_at"] = now_iso()
        atomic_write_text(
            run_dir / "call.meta.json", json.dumps(payload, indent=JSON_INDENT)
        )
    except (OSError, TypeError, ValueError):
        pass  # instrumentation must never break the call it is measuring


