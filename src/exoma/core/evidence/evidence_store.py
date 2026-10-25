"""Persistent, staleness-checked index over immutable evidence artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from exoma.core.evidence.fact_store import (
    _FILELINE,
    _line_hash,
    _read_lines,
    current_anchor_line,
)
from exoma.core.workspace.workspace_paths import (
    _safe_component,
    atomic_write_text,
    now_iso,
    workflow_paths,
)
from exoma.utils import osutil
from exoma.utils.redact import redact, redact_value

EVIDENCE_FILENAME = "evidence.jsonl"
LOCK_FILENAME = "evidence.jsonl.lock"
LOCK_TIMEOUT_SECONDS = 30
MAX_EVIDENCE = 200
MAX_ANCHORS_PER_ARTIFACT = 40
# How many distinct citations are CERTIFIED, as opposed to stored. The two used to be one
# number: the scan stopped at the 40th anchor, so a run citing a nonexistent path at
# position 41 reported `40/40 certified` — the ratio described the prefix it had read, and
# the reader is told to trust that ratio before opening anything. Storing stays bounded
# (every stored anchor is re-hashed on each reuse check); certifying is one hash per line
# against a per-call file cache, so the scan can afford to read much further.
MAX_ANCHORS_SCANNED = 200
# Uncertified citations named back to the reader, so a cap reason can say WHICH refs failed.
MAX_UNCERTIFIED_LISTED = 10

_EVIDENCE_THREAD_LOCK = threading.Lock()


def _path(project_root: Path) -> Path:
    return workflow_paths(Path(project_root))["evidence_store"]


class _EvidenceLock:
    """Serialize evidence mutations with a persistent native file lock."""

    def __init__(self, project_root: Path):
        self.path = _path(project_root).with_name(LOCK_FILENAME)
        self.handle = None
        self.thread_locked = False
        self.file_locked = False

    def __enter__(self) -> "_EvidenceLock":
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        remaining = max(0.0, deadline - time.monotonic())
        if not _EVIDENCE_THREAD_LOCK.acquire(timeout=remaining):
            raise TimeoutError("timed out waiting for evidence index lock")
        self.thread_locked = True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.handle = osutil.open_lock_file(self.path)

            while not self.file_locked:
                try:
                    osutil.lock_file(self.handle)
                    self.file_locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("timed out waiting for evidence index lock")
                    time.sleep(0.01)
            return self
        except Exception:
            self._release()
            raise

    def __exit__(self, *exc) -> None:
        self._release()

    def _release(self) -> None:
        try:
            if self.file_locked and self.handle is not None:
                osutil.unlock_file(self.handle)
        finally:
            self.file_locked = False
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            if self.thread_locked:
                self.thread_locked = False
                _EVIDENCE_THREAD_LOCK.release()


def _context_json(context: dict | None) -> str:
    return json.dumps(
        context or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _context_hash(context: dict | None) -> str:
    return hashlib.sha256(_context_json(context).encode("utf-8")).hexdigest()[:24]


def _query_hash(command: str, task: str | None, context: dict | None = None) -> str:
    """Hash the literal task plus the effective delegated execution context."""
    payload = {
        "command": str(command or "").strip().lower(),
        "task": task or "",
        "context": context or {},
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _immutable_artifact_path(project_root: Path, entry: dict) -> Path | None:
    value = entry.get("artifact_path")
    if not value or not entry.get("artifact_hash"):
        return None
    try:
        artifact = Path(value).resolve()
        # Relative to the data directory, where sessions/ lives in either layout.
        data = workflow_paths(Path(project_root))["data_dir"].resolve()
        relative = artifact.relative_to(data)
    except (OSError, ValueError):
        return None
    parts = relative.parts
    expected_session = _safe_component(str(entry.get("session") or ""))
    if (
        len(parts) != 5
        or parts[0] != "sessions"
        or parts[1] != expected_session
        or parts[2] != "logs"
        or not parts[3]
        or parts[4] != "output.raw.md"
    ):
        return None
    return artifact


def read_artifact_with_redactions(
    project_root: Path, entry: dict
) -> tuple[str | None, list[dict]]:
    """Read a hash-checked artifact and report any defensive redactions."""
    artifact = _immutable_artifact_path(project_root, entry)
    if artifact is None or not artifact.is_file():
        return None, []
    try:
        content = artifact.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, []
    if _content_hash(content) != entry.get("artifact_hash"):
        return None, []
    return redact(content)


def read_artifact(project_root: Path, entry: dict) -> str | None:
    """Read an indexed artifact only when its location and digest are unchanged."""
    return read_artifact_with_redactions(project_root, entry)[0]


def _extract_anchors(project_root: Path, content: str) -> tuple[list[dict], bool]:
    """Return unique anchors and whether every citation was certifiable.

    An uncertifiable citation no longer ends the scan. It used to: the first anchor that
    could not be hashed — one outside the project root, or a file that has since moved —
    returned whatever had been collected so far, and when that anchor came first the
    answer was an empty list. A cross-project analysis whose every claim named a file
    reported `anchors: 0`, which reads as "no evidence" rather than "evidence this process
    cannot open".

    So each anchor is kept and marked. `certified` says whether its line was hashed;
    `hash` is None when it was not. The completeness flag still goes False, so nothing
    downstream treats a partly-uncertified artifact as reusable — `_is_fresh` demands
    every anchor hold, and it cannot check one it never hashed.

    Kept as the stored-prefix view of `scan_anchors`, which also counts what lies past it.
    """
    scan = scan_anchors(project_root, content)
    return scan["anchors"], scan["complete"]


def scan_anchors(project_root: Path, content: str) -> dict:
    """Certify every cited anchor up to the scan limit; keep the first few for reuse.

    Returns:
      anchors        the stored prefix (at most MAX_ANCHORS_PER_ARTIFACT), as before
      complete       reuse eligibility: every citation was stored AND certified
      cited          distinct citations certified-or-not (bounded by MAX_ANCHORS_SCANNED)
      certified      how many of `cited` hash to a line in this project
      scanned_all    False when the scan limit cut the content short — the ratio then
                     describes a prefix, and a reader must not take it for the whole
      uncertified    up to MAX_UNCERTIFIED_LISTED `file:line` strings that did not certify

    `complete` and `scanned_all` answer different questions and are kept apart: evidence
    with 60 good anchors is fully certified (scanned_all, certified == cited) yet not
    reusable, because only 40 of them are stored to be re-checked later.
    """
    seen: set[tuple[str, int]] = set()
    anchors: list[dict] = []
    uncertified: list[str] = []
    cited = 0
    certified = 0
    complete = True
    scanned_all = True
    line_cache: dict = {}
    for m in _FILELINE.finditer(content or ""):
        file, line = m.group(1), int(m.group(2))
        key = (file, line)
        if key in seen:
            continue
        seen.add(key)
        if cited >= MAX_ANCHORS_SCANNED:
            scanned_all = False
            break
        cited += 1
        h = _cached_anchor_hash(project_root, file, line, line_cache)
        if h is None:
            complete = False
            if len(uncertified) < MAX_UNCERTIFIED_LISTED:
                uncertified.append(f"{file}:{line}")
        else:
            certified += 1
        if len(anchors) < MAX_ANCHORS_PER_ARTIFACT:
            anchors.append(
                {"file": file, "line": line, "hash": h, "certified": h is not None}
            )
        else:
            complete = False  # cited but not stored: a later reuse could not re-check it
    return {
        "anchors": anchors,
        "complete": complete and scanned_all,
        "cited": cited,
        "certified": certified,
        "scanned_all": scanned_all,
        "uncertified": uncertified,
    }


def certify_claim_refs(project_root: Path, claims: list[dict]) -> list[dict]:
    """Give every claim's refs a status: `certified`, `unverified`, or `not_a_file`.

    `meta.claims[].refs` was presentation only — lifted out of the prose and never opened —
    so a grounded claim citing a path that does not exist looked exactly like one citing a
    real line. The anchor ratio catches that in aggregate; this says WHICH claim.

    `not_a_file` covers refs that are not `file:line` at all (`req:<id>`, labels with no
    path in them); they are bookkeeping, not evidence, and are neither counted for nor
    against the claim. A label that wraps a path (`proof:core/x.py:200`) is judged by that
    path. Returns new dicts; the input is unchanged.
    """
    cache: dict = {}
    out: list[dict] = []
    for claim in claims or []:
        statuses: list[str] = []
        for ref in claim.get("refs") or []:
            m = _FILELINE.search(str(ref))
            if not m:
                statuses.append("not_a_file")
                continue
            h = _cached_anchor_hash(project_root, m.group(1), int(m.group(2)), cache)
            statuses.append("certified" if h is not None else "unverified")
        out.append({**claim, "ref_status": statuses})
    return out


def _cached_anchor_hash(
    project_root: Path, file: str, line: int, cache: dict
) -> str | None:
    """`_anchor_hash` with the file read once per scan, not once per cited line."""
    if file not in cache:
        cache[file] = _read_lines(project_root, file)
    lines = cache[file]
    if not line or lines is None or line < 1 or line > len(lines):
        return None
    return _line_hash(lines[line - 1])


def _load(project_root: Path) -> list[dict]:
    p = _path(project_root)
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except ValueError:
            continue  # skip a corrupt line rather than lose the whole index
        if isinstance(row, dict):
            out.append(row)
    return out


def _save(project_root: Path, rows: list[dict]) -> None:
    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    atomic_write_text(_path(project_root), payload + ("\n" if payload else ""))


def _is_fresh(project_root: Path, entry: dict) -> bool:
    """An artifact is fresh only if EVERY cited anchor still hashes to the recorded value.

    No anchors -> cannot certify -> not fresh (never serve unverifiable cached evidence).
    """
    if read_artifact(project_root, entry) is None:
        return False
    anchors = entry.get("anchors") or []
    if not anchors or entry.get("anchors_complete") is not True:
        return False
    # An anchor that merely MOVED still points at the same source text, so it does not make
    # the artifact stale — see `fact_store.current_anchor_line`. This matters more here than
    # it does for a single fact: freshness demands EVERY anchor hold, so one insertion above
    # one cited line was enough to discard a whole piece of evidence and re-run the call.
    index_cache: dict = {}
    for a in anchors:
        if (
            current_anchor_line(
                project_root, a.get("file"), a.get("line"), a.get("hash"), index_cache
            )
            is None
        ):
            return False
    return True


def record(
    project_root: Path,
    command: str,
    task: str | None,
    session_id: str,
    digest,
    artifact_path,
    content: str,
    context: dict | None = None,
) -> dict:
    """Index one delegated evidence artifact. Returns the stored entry."""
    _, content_redactions = redact(content)
    if content_redactions:
        raise ValueError("evidence content must be redacted before indexing")
    safe_session, _ = redact(session_id)
    resolved_artifact = None
    if artifact_path:
        raw_artifact = Path(artifact_path)
        resolved_artifact = (
            raw_artifact.resolve()
            if raw_artifact.is_absolute()
            else (Path(project_root) / raw_artifact).resolve()
        )
    provisional = {
        "artifact_path": str(resolved_artifact) if resolved_artifact else None,
        "artifact_hash": _content_hash(content),
        "session": safe_session,
    }
    if _immutable_artifact_path(project_root, provisional) is None:
        raise ValueError("evidence artifact must be an immutable logs/<run>/output.raw.md")
    try:
        stored_content = resolved_artifact.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError("evidence artifact is not readable") from exc
    if _content_hash(stored_content) != provisional["artifact_hash"]:
        raise ValueError("evidence artifact content does not match delegated output")
    task_preview, _ = redact((task or "")[:200])
    safe_digest, _ = redact_value(digest)
    safe_command, _ = redact(command)
    scan = scan_anchors(project_root, content)
    anchors, anchors_complete = scan["anchors"], scan["complete"]
    entry = {
        "query_hash": _query_hash(command, task, context),
        "context_hash": _context_hash(context),
        "command": safe_command,
        "task_preview": task_preview,
        "digest": safe_digest,
        **provisional,
        "anchors": anchors,
        "anchors_complete": anchors_complete,
        # Rows written before this field carry none of it; readers fall back to the
        # stored-prefix count, which is what those rows always reported.
        "anchor_scan": {
            "cited": scan["cited"],
            "certified": scan["certified"],
            "scanned_all": scan["scanned_all"],
            "uncertified": scan["uncertified"],
        },
        "captured_at": now_iso(),
        "session": safe_session,
    }
    with _EvidenceLock(project_root):
        rows = _load(project_root)
        rows.append(entry)
        if len(rows) > MAX_EVIDENCE:
            rows = rows[-MAX_EVIDENCE:]
        _save(project_root, rows)
    return entry


def find_fresh(
    project_root: Path,
    command: str,
    task: str | None,
    context: dict | None = None,
) -> dict | None:
    """Most-recent artifact for this EXACT query whose anchors are all still fresh.

    If the most-recent match is stale, return None (re-delegate) rather than reaching for
    an older, necessarily-staler entry.
    """
    return find_fresh_with_reason(project_root, command, task, context)[0]


def find_fresh_with_reason(
    project_root: Path,
    command: str,
    task: str | None,
    context: dict | None = None,
) -> tuple[dict | None, str]:
    """`find_fresh`, plus why it missed: `hit`, `no_prior`, or `stale`.

    The reason is what CASE-009 could not measure. A zero reuse rate made of queries that
    never repeat and one made of repeats whose anchors moved call for different fixes.
    """
    qh = _query_hash(command, task, context)
    with _EvidenceLock(project_root):
        rows = _load(project_root)
        usable = [
            entry
            for entry in rows
            if _immutable_artifact_path(project_root, entry) is not None
        ]
        if len(usable) != len(rows):
            _save(project_root, usable)
    for entry in reversed(usable):
        if entry.get("query_hash") == qh:
            if _is_fresh(project_root, entry):
                return entry, "hit"
            return None, "stale"
    return None, "no_prior"
