"""`exoma session export`: one main session as a portable, redacted bundle.

Made for the experiment protocol (docs/team-guide/experiment-protocol.md): a participant
works a real problem in one session, exports it when the work is done, and fills in the
review that rides inside the bundle. The bundle is what gets collected and compared, so it
has to stand on its own without the machine that produced it.

What goes in, all text redacted through utils/redact.py (credential shapes become
`[REDACTED:<kind>]`; the counts, never the values, are recorded in the manifest):

    session/               the session directory: prompts, responses, per-call logs and
                           their archived evidence (`evidence_ref.artifact_path` lives
                           here), runtime sidecars (plan, execution, markers)
    streams/<name>.jsonl   rows of the shared usage/audit/quality streams that name this
                           session — the delegated calls and what they cost
    transcript/            the host's own transcript of the main session when the host
                           records where it is (Claude Code, Codex); otherwise the
                           manifest says `transcript: unavailable` and why
    review.md              the participant's review, from the template below
    manifest.json          what is in the bundle, with sha256 per file

Nothing is uploaded and nothing outside the project's data directory is written; the zip
lands under `<data dir>/exports/` unless `--out` says otherwise. Stdlib only.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from exoma.config.settings import TOOL_VERSION
from exoma.core.workspace.workspace_paths import workflow_paths
from exoma.utils.redact import redact

STREAMS = ("usage_stream", "audit_stream", "quality_stream")
# Larger files are listed in the manifest but not copied: a runaway log is not evidence.
MAX_FILE_BYTES = 8_000_000

REVIEW_TEMPLATE = """# Session review

<!-- Fill this in right after the session, while it is fresh. Plain answers; no need to
polish. Leave a line blank rather than guessing. -->

- session: {session_id}
- project: {project}
- host: {host}
- exported_at: {exported_at}

## The problem
- What real problem were you solving? (one or two sentences)
- Where did it come from? (ticket, bug report, your own backlog)

## Outcome
- solved | partly solved | not solved:
- Was the result verified (tests, manual check, /.verify)? How?

## The second agent
- Which delegated calls helped, and how?
- Which ones were wrong, unnecessary, or ignored? Why?
- Did you have to correct the main agent's routing (wrong command, Esc on [INTENT])?

## Friction
- Where did you lose time? (waiting, re-asking, fixing wrong output)
- Anything you did by hand that the workflow should have done?

## Rating
- usefulness of the second agent (1-5):
- would you work this way again on a similar problem (yes/no, why):
"""


def _unfilled(review: str) -> bool:
    """True while the review holds nothing beyond the template's own lines."""
    template_lines = {line.strip() for line in REVIEW_TEMPLATE.splitlines()}
    header = ("- session:", "- project:", "- host:", "- exported_at:")
    for line in review.splitlines():
        stripped = line.strip()
        if stripped and stripped not in template_lines and not stripped.startswith(header):
            return False
    return True


def _redacted_bytes(raw: bytes) -> tuple[bytes | None, int]:
    """(clean bytes, redaction count); None when the bytes are not UTF-8 text.

    Redaction works on text. Bytes it cannot read as text cannot be checked for secrets, so
    they are never copied as they are: the caller lists the file under `skipped` instead.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, 0
    clean, hits = redact(text)
    return clean.encode("utf-8"), sum(hit["count"] for hit in hits)


def _inside(path: Path, base: Path) -> bool:
    """True when `path`, every link resolved, is still inside `base`."""
    try:
        path.resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return False
    return True


def _rows_for(path: Path, session_id: str) -> list[str]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and session_id in (row.get("session_id"), row.get("main_session_id")):
            rows.append(line)
    return rows


def _claude_transcript(session_id: str, home: Path) -> tuple[Path | None, str]:
    registry = home / ".claude" / "session_registry.json"
    try:
        entries = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "no Claude Code session registry"
    for entry in (entries or {}).values():
        if isinstance(entry, dict) and entry.get("main_session_id") == session_id:
            path = entry.get("transcript_path")
            if path and Path(path).is_file():
                return Path(path), "claude"
            return None, "Claude Code transcript path recorded but missing"
    return None, "session not in the Claude Code registry"


def _codex_transcript(session_id: str, home: Path) -> tuple[Path | None, str]:
    bindings = home / ".codex" / "exoma" / "sessions"
    for binding in bindings.glob("*.json") if bindings.is_dir() else ():
        try:
            data = json.loads(binding.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("main_session_id") == session_id:
            codex_sid = binding.stem
            found = sorted((home / ".codex" / "sessions").rglob(f"*{codex_sid}*.jsonl"))
            if found:
                return found[-1], "codex"
            return None, "Codex session bound but its rollout file was not found"
    return None, "session not bound by the Codex host"


def find_host_transcript(session_id: str, home: Path | None = None) -> tuple[Path | None, str, str]:
    """(path, host, reason). host is `unknown` when no host recorded this session."""
    home = home or Path.home()
    reasons = []
    for host, finder in (("claude", _claude_transcript), ("codex", _codex_transcript)):
        path, reason = finder(session_id, home)
        if path:
            return path, host, "found"
        reasons.append(reason)
    return None, "unknown", "; ".join(reasons) + " (opencode and agy transcripts are not collected)"


def export_session(
    project_root,
    session_id: str,
    out: Path | None = None,
    include_transcript: bool = True,
    home: Path | None = None,
) -> dict:
    """Write the bundle; returns {ok, path, manifest} or {ok: False, error}."""
    from exoma.utils.path_guard import safe_path_component

    # The id names files below (the zip, the review). Only an id that is already a safe
    # path component is accepted: workflow_paths sanitizes it for the session directory,
    # and an id with separators or `..` would otherwise steer those names elsewhere.
    if not session_id or safe_path_component(session_id) != session_id:
        return {"ok": False, "error": f"not a session id: {session_id!r}"}
    root = Path(project_root)
    paths = workflow_paths(root, session_id)
    session_dir = paths["session_dir"]
    if not session_id or not session_dir.is_dir():
        return {"ok": False, "error": f"no session directory for {session_id!r} under {paths['sessions_dir']}"}

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    dest = Path(out) if out else paths["data_dir"] / "exports" / f"{session_id}_{stamp}.zip"
    if dest.suffix != ".zip":
        dest = dest / f"{session_id}_{stamp}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)

    files: list[dict] = []
    skipped: list[dict] = []
    redactions = 0
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:

        def add(name: str, raw: bytes) -> bool:
            nonlocal redactions
            clean, count = _redacted_bytes(raw)
            if clean is None:
                skipped.append({"path": name, "reason": "not UTF-8 text: cannot be redacted, so not copied"})
                return False
            redactions += count
            bundle.writestr(name, clean)
            files.append({"path": name, "bytes": len(clean), "sha256": hashlib.sha256(clean).hexdigest()})
            return True

        for path in sorted(p for p in session_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(session_dir).as_posix()
            # A link can point anywhere on the machine; only what really lives in the
            # session directory is the session's to hand over.
            if path.is_symlink() or not _inside(path, session_dir):
                skipped.append({"path": "session/" + rel, "reason": "symbolic link or outside the session directory"})
                continue
            if path.name == "lock" or path.stat().st_size > MAX_FILE_BYTES:
                skipped.append({"path": "session/" + rel, "reason": "lock file" if path.name == "lock" else "too large"})
                continue
            add("session/" + rel, path.read_bytes())

        for key in STREAMS:
            rows = _rows_for(paths[key], session_id)
            if rows:
                add(f"streams/{paths[key].name}", ("\n".join(rows) + "\n").encode("utf-8"))

        transcript = {"status": "skipped", "reason": "--no-transcript"}
        host = "unknown"
        if include_transcript:
            path, host, reason = find_host_transcript(session_id, home)
            if path and path.stat().st_size <= MAX_FILE_BYTES:
                if add("transcript/" + path.name, path.read_bytes()):
                    transcript = {"status": "included", "host": host, "file": "transcript/" + path.name}
                else:
                    transcript = {"status": "unavailable", "reason": f"{path.name} is not UTF-8 text"}
            elif path:
                transcript = {"status": "unavailable", "reason": f"{path.name} is larger than {MAX_FILE_BYTES} bytes"}
            else:
                transcript = {"status": "unavailable", "reason": reason}

        # The review lives beside the bundles, not inside one: the participant fills it in
        # once, and every later export of the same session picks the filled one up.
        review_path = dest.parent / f"{session_id}.review.md"
        template = REVIEW_TEMPLATE.format(
            session_id=session_id, project=root.name, host=host, exported_at=now.isoformat()
        )
        if not review_path.is_file():
            review_path.write_text(template, encoding="utf-8")
        try:
            review_text = review_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Saved in another encoding, or unreadable: it cannot be redacted, so it is not
            # copied, and the export still completes and says so.
            review_text = None
            review_status = "unreadable"
            skipped.append({"path": "review.md", "reason": "review file is not readable UTF-8 text"})
        if review_text is not None:
            review_status = "template" if _unfilled(review_text) else "filled"
            add("review.md", review_text.encode("utf-8"))

        manifest = {
            "schema": 1,
            "exoma_version": TOOL_VERSION,
            "session_id": session_id,
            "project": root.name,
            "exported_at": now.isoformat(),
            "host": host,
            "transcript": transcript,
            "redactions": redactions,
            "files": files,
            "skipped": skipped,
            "review": {"status": review_status, "source": str(review_path)},
        }
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")

    dest.write_bytes(buffer.getvalue())
    return {"ok": True, "path": str(dest), "manifest": manifest}


def main(argv: list[str]) -> int:
    """`exoma session export <id> [--work-dir DIR] [--out PATH] [--no-transcript]`."""
    import argparse

    parser = argparse.ArgumentParser(prog="exoma session", description="Session tools")
    sub = parser.add_subparsers(dest="action", required=True)
    export = sub.add_parser("export", help="bundle one main session for review/collection")
    export.add_argument("session_id")
    export.add_argument("--work-dir", default=None, help="project root (default: cwd)")
    export.add_argument("--out", default=None, help="zip path or directory (default: <data dir>/exports/)")
    export.add_argument("--no-transcript", action="store_true", help="leave the host transcript out")
    args = parser.parse_args(argv)

    from exoma.core.workspace.workspace_paths import detect_project_root

    root = detect_project_root(args.work_dir or str(Path.cwd()))
    result = export_session(root, args.session_id, Path(args.out) if args.out else None,
                            include_transcript=not args.no_transcript)
    if not result["ok"]:
        print(f"[SESSION EXPORT] failed: {result['error']}")
        return 1
    manifest = result["manifest"]
    print(f"[SESSION EXPORT] {result['path']}")
    print(f"  files: {len(manifest['files'])} | redactions: {manifest['redactions']} | "
          f"transcript: {manifest['transcript']['status']}")
    if manifest["transcript"].get("reason"):
        print(f"  transcript note: {manifest['transcript']['reason']}")
    review = manifest["review"]
    if review["status"] == "unreadable":
        print(f"  review: UNREADABLE — save {review['source']} as UTF-8, then run this export again")
    elif review["status"] == "template":
        print(f"  review: NOT FILLED — fill {review['source']}, then run this export again")
    else:
        print(f"  review: filled ({review['source']})")
    return 0
