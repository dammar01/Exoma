"""Turning elements a run actually drove into `data-e2e` tags in the project's source.

A run already knows something no reader of the codebase does: which element a step really
hit, which of the draft's candidate selectors won, and how many elements matched. That is
worth writing down — but writing it down means editing the user's own templates, so every
step here is narrow on purpose.

What may be tagged: an element whose step PASSED, whose winning selector had provenance
`source`, and whose `selector_provenance.ref` names a `path:line` in this project. That
last condition is the whole boundary. There is no map from a runtime selector back to the
template that rendered it, so the only elements with a real address are the ones the draft
already cited one for. A selector found by heuristic or by probing the live DOM is left
alone rather than guessed at: a tag written to the wrong line is a silent edit to code
nobody asked to change.

What is never used: the live DOM. `page.content()` describes what the browser rendered,
not what the repository contains, and patching source from it would write framework output
back over the template that produced it.

This module proposes and validates; it does not decide. The runner records the plan, the
user confirms the batch, and only then is a line rewritten — the one interactive gate
/.verify-browser keeps, because it is the one step that changes files the user owns.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

from exoma.core.workspace.workspace_paths import replace_with_retry

from exoma.utils import git

TAG_ATTRIBUTE = "data-e2e"
# One run's worth. A scenario long enough to exceed this is proposing a refactor, not a tag.
MAX_PROPOSALS = 50
_REF = re.compile(r"^(?P<path>[^\s:][^\s]*?):(?P<line>\d+)$")
_TAG_VALUE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# The opening tag on a line, with its name captured. Attribute values are consumed as units
# so a `>` inside one does not end the match early.
_OPEN_TAG = re.compile(r"<(?P<name>[A-Za-z][\w.:-]*)(?P<attrs>(?:\"[^\"]*\"|'[^']*'|[^>\"'])*?)(?P<close>/?)>")
# Markup inside an UNCLOSED comment on this line. `(?:(?!-->).)*` stops at the comment's
# end, so `<!-- note --> <button>` is still taggable while `<!-- <button> -->` is not.
_COMMENTED = re.compile(r"<!--(?:(?!-->).)*<[A-Za-z]", re.DOTALL)
# Files that can hold a rendered element. Deliberately a list rather than "anything text":
# a `path:line` pointing at a controller is a real reference and still not a place where an
# attribute belongs.
TEMPLATE_SUFFIXES = frozenset(
    {".html", ".htm", ".vue", ".svelte", ".jsx", ".tsx", ".php", ".erb", ".twig", ".hbs", ".ejs", ".astro", ".blade"}
)


def proposals(report: dict) -> list[dict]:
    """Elements this run drove that carry a source address, newest run's report.

    Reads the trail, not the raw events: by the time a report exists the events have been
    scrubbed of resolved credentials, and a proposal is built from text that will be shown
    to the user and written to disk.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for row in report.get("trail") or []:
        if row.get("status") != "passed" or row.get("selector_provenance") != "source":
            continue
        selection = row.get("selection") or {}
        ref = str(selection.get("source_ref") or "").strip()
        step_id = str(row.get("step_id") or "").strip()
        if not ref or not step_id or step_id in seen:
            continue
        seen.add(step_id)
        out.append(
            {
                "step_id": step_id,
                "action": row.get("action"),
                "ref": ref,
                # Key names only — see Session.resolve. A selector's VALUES can be resolved
                # credentials, and this dict is written to disk and shown to the user.
                "selector_keys": selection.get("selector_keys"),
                "match_counts": selection.get("match_counts"),
                "tag": step_id,
            }
        )
        if len(out) >= MAX_PROPOSALS:
            break
    return out


def tagged_line(text: str, tag: str) -> str | None:
    """`text` with `data-e2e="tag"` added to its one opening tag, or None if it is not clear
    which element is meant.

    The attribute goes immediately after the element name, before whatever the line already
    says. Inserting at the end would mean deciding where a self-closing slash or a template
    expression ends; after the name there is exactly one right position.

    A line with more than one opening tag is refused rather than resolved by "the first
    one". `path:line` addresses a line, not an element, and on `<div><button>…` the step
    almost certainly drove the button while the first match is the div. A line whose markup
    is inside an HTML comment is refused for the same reason: whatever it describes, it is
    not the element the browser clicked.

    Known limit: this is a regex, not a parser. A tag-shaped substring inside a quoted
    string (`const html = "<button>"`) reads as markup here. The containing checks narrow
    the blast radius — the file is a template, the line is one the draft cited, and the user
    sees the exact replacement before it is written — but a template that builds markup in a
    string can still be offered a tag in the wrong place, and the diff is what catches it.
    """
    if _COMMENTED.search(text):
        return None
    matches = _OPEN_TAG.findall(text)
    if len(matches) != 1:
        return None
    match = _OPEN_TAG.search(text)
    at = match.start("attrs")
    return f'{text[:at]} {TAG_ATTRIBUTE}="{tag}"{text[at:]}'


def _inside_open_comment(before: list[str]) -> bool:
    """Whether a comment opened in the lines before the target and never closed.

    `tagged_line` can only see the line it is given, and a comment can span lines. Counting
    the markers over everything above the target is not a parser, but it answers the one
    question that matters here: was this element commented out somewhere the caller cannot
    see.
    """
    text = "\n".join(before)
    return text.count("<!--") > text.count("-->")


def _refuse(proposal: dict, reason: str) -> dict:
    return {**proposal, "status": "skipped", "reason": reason}


def plan(project_root: Path, items: list[dict]) -> list[dict]:
    """Each proposal checked against the repository as it is right now.

    Every entry comes back, `ready` or `skipped` with the reason. Nothing is dropped
    silently: a proposal that cannot be applied is the more interesting half of the
    report, because it usually means the draft's reference has drifted from the code.
    """
    project_root = Path(project_root).resolve()
    out: list[dict] = []
    claimed: dict[str, str] = {}
    for item in items:
        tag = str(item.get("tag") or "")
        if not _TAG_VALUE.match(tag):
            out.append(_refuse(item, f"tag '{tag}' is not a kebab-case identifier"))
            continue
        match = _REF.match(str(item.get("ref") or ""))
        if not match:
            out.append(_refuse(item, "selector_provenance.ref names no line; an element needs `path:line` to be tagged"))
            continue
        raw_path, line_no = match.group("path"), int(match.group("line"))
        candidate = Path(raw_path)
        target = (candidate if candidate.is_absolute() else project_root / candidate).resolve()
        try:
            relative = target.relative_to(project_root).as_posix()
        except ValueError:
            out.append(_refuse(item, f"'{raw_path}' resolves outside the project"))
            continue
        if target.suffix.lower() not in TEMPLATE_SUFFIXES:
            out.append(_refuse(item, f"'{relative}' is not a template file ({', '.join(sorted(TEMPLATE_SUFFIXES))})"))
            continue
        if not target.is_file():
            out.append(_refuse(item, f"'{relative}' does not exist"))
            continue
        if git.is_ignored(project_root, relative):
            # The tag is only worth writing because Git carries it to the next reader.
            out.append(_refuse(item, f"'{relative}' is git-ignored: a tag nobody else receives is not knowledge"))
            continue
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            out.append(_refuse(item, f"'{relative}': {type(exc).__name__}"))
            continue
        if not 1 <= line_no <= len(lines):
            out.append(_refuse(item, f"'{relative}' has {len(lines)} lines; the reference points at {line_no}"))
            continue
        if _inside_open_comment(lines[: line_no - 1]):
            out.append(_refuse(item, f"'{relative}:{line_no}' sits inside an HTML comment that opened earlier"))
            continue
        old = lines[line_no - 1]
        if TAG_ATTRIBUTE in old:
            out.append(_refuse(item, f"'{relative}:{line_no}' already carries {TAG_ATTRIBUTE}"))
            continue
        new = tagged_line(old, tag)
        if new is None:
            out.append(_refuse(item, f"'{relative}:{line_no}' holds no single, uncommented opening tag to name"))
            continue
        owner = claimed.get(tag)
        if owner:
            out.append(_refuse(item, f"tag '{tag}' is already proposed for {owner}"))
            continue
        claimed[tag] = f"{relative}:{line_no}"
        out.append(
            {
                **item,
                "status": "ready",
                "path": relative,
                "line": line_no,
                "old_line": old,
                "new_line": new,
            }
        )
    return out


def ready(entries: list[dict]) -> list[dict]:
    return [entry for entry in entries if entry.get("status") == "ready"]


def _closing(handle: int, reason: str):
    """Refuse, and give the descriptor back. Every path out of `_open_checked` after the
    open runs through here, so none of them can leave a handle behind."""
    os.close(handle)
    return None, reason


def _read_all(handle: int) -> bytes:
    """Everything left in the file. `os.read` is allowed to return less than asked for, and
    a single call sized from `st_size` would silently drop the tail of a large file."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(handle, 1 << 20)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _write_all(handle: int, body: bytes) -> None:
    """Every byte, or an exception. `os.write` may accept only part of the buffer, and the
    difference between "wrote 40 bytes" and "wrote the file" is the user's template."""
    written = 0
    while written < len(body):
        sent = os.write(handle, body[written:])
        if sent <= 0:
            raise OSError(f"short write: {written} of {len(body)} bytes")
        written += sent


def _file_identity(info: os.stat_result) -> tuple:
    """(device, inode) plus size and mtime.

    The inode alone is not enough: once the descriptor is closed, a file deleted and
    recreated under the same name can be given the inode number just freed (ext4 does this
    readily), and the swap would pass as the same file. A recreated file with the same
    number, size AND nanosecond mtime is not a realistic accident.
    """
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _identity(target: Path) -> tuple | None:
    """What file this name points at right now, or None when it points at nothing."""
    try:
        info = os.lstat(target)
    except OSError:
        return None
    return _file_identity(info)


def _discard(handle: int | None, temporary: str) -> None:
    """Get rid of a temporary that will not be used. Failures here are not worth raising:
    the file that matters was never touched, and a leftover temporary is a mess, not a loss."""
    if handle is not None:
        try:
            os.close(handle)
        except OSError:
            pass
    try:
        os.unlink(temporary)
    except OSError:
        pass


def _replace_contents(target: Path, body: bytes, mode: int, identity: tuple | None) -> None:
    """Put `body` in `target`, or leave `target` exactly as it was.

    Written to a temporary file beside it and moved into place, rather than over the
    original in place. Rewriting in place means there is a moment when the file holds
    neither the old content nor the new one, and no amount of care about ordering removes
    it — a restore is just another write, and it can fail for the same reason the first one
    did. `os.replace` has no such moment: either the name points at the finished file or it
    still points at the untouched one.

    `identity` is the `_file_identity` of the file whose contents `body` was derived from.
    It is checked again immediately before the move, so a name that has come to point at a
    DIFFERENT file since it was read is refused rather than overwritten with content that
    was never its own.

    That check narrows the race; it cannot close it. There is no identity-checked rename in
    the standard library on either platform, so a swap landing between this check and the
    move would still be followed. The residue is the same class as the parent-directory
    boundary documented in `_open_checked`, and it is bounded the same way: reaching it
    requires write access to the project tree, and anything reachable from there is a file
    the attacker could already edit directly.

    The temporary lands in the same directory so the move stays on one filesystem, and it
    is removed on any failure, so a failed tagging pass leaves the project as it found it.
    """
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=".e2e-tag-", suffix=".tmp")
    try:
        _write_all(handle, body)
        os.fsync(handle)
    except BaseException:
        _discard(handle, temporary)
        raise
    try:
        os.close(handle)
    except OSError:
        _discard(None, temporary)
        raise
    try:
        if _identity(target) != identity:
            raise OSError(f"'{target.name}' is not the file that was read any more")
        # mkstemp makes the file private to this user; the template keeps the permissions
        # it had, or the tag would arrive with a mode change nobody asked for.
        os.chmod(temporary, mode)
        # Not `atomic_write_bytes`: the identity check above has to sit immediately
        # before the move, so this writer keeps its own staging and borrows only the retry.
        replace_with_retry(Path(temporary), target)
    except OSError:
        _discard(None, temporary)
        raise


def _open_checked(project_root: Path, relative: str):
    """A read-write descriptor on a file that is inside the project, or (None, reason).

    Checking the path and then writing to the path is two operations on a name, and a name
    can be repointed between them. So the name is used exactly once, to open; every check
    after that runs against the DESCRIPTOR, which stays bound to the file that was opened
    no matter what happens to the path.

    `O_NOFOLLOW` refuses a symlink outright where the platform has it; where it does not
    (Windows), `lstat` is compared with `fstat` so a symlink is caught by identity instead.
    A link count above one is refused too: the same bytes are then reachable under another
    name, possibly outside the project, and "inside the project" would stop meaning
    anything.

    What this does not cover: a PARENT DIRECTORY swapped between the resolve and the open.
    Closing that needs handle-based traversal the stdlib does not offer portably — and an
    attacker who can rewrite directories inside the project can edit the template directly,
    so the boundary buys nothing in that case.
    """
    target = project_root / relative
    try:
        resolved = target.resolve()
        resolved.relative_to(project_root)
    except (ValueError, OSError):
        return None, f"'{relative}' now resolves outside the project"
    if resolved.suffix.lower() not in TEMPLATE_SUFFIXES:
        return None, f"'{relative}' is no longer a template file"
    if git.is_ignored(project_root, relative):
        return None, f"'{relative}' is git-ignored now"
    try:
        link_state = os.lstat(resolved)
    except OSError as exc:
        return None, f"{type(exc).__name__} while checking '{relative}'"
    if stat.S_ISLNK(link_state.st_mode):
        return None, f"'{relative}' is a symlink now, not the file that was planned"
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(resolved, flags)
    except OSError as exc:
        return None, f"{type(exc).__name__} while opening '{relative}'"
    try:
        info = os.fstat(handle)
        if info.st_nlink > 1:
            return _closing(handle, f"'{relative}' has {info.st_nlink} links: the same bytes are reachable under another name")
        if (info.st_dev, info.st_ino) != (link_state.st_dev, link_state.st_ino):
            return _closing(handle, f"'{relative}' was replaced between the check and the open")
    except OSError as exc:
        return _closing(handle, f"{type(exc).__name__} while checking the open file")
    return handle, None


def apply(project_root: Path, entries: list[dict]) -> list[dict]:
    """Write the confirmed tags, re-checking the FILE and the line before replacing them.

    Everything is checked again here rather than trusted from the plan. The plan was made
    before the user was asked, and the answer to "may I edit these files" takes as long as
    the user takes — long enough for a path to become a symlink pointing out of the project,
    which `plan()` would have caught and this function would otherwise follow. So the file
    is opened once, under the checks in `_open_checked`, and the read goes through that one
    descriptor; a line that changed in between is skipped, not overwritten.

    The descriptor is then closed and the new content is moved into place by name.
    `os.replace` targets the NAME, so it cannot write through a symlink the way an in-place
    write could — the containment the descriptor was protecting survives the handover — and
    Windows refuses to replace a file this process still holds open, so closing first is
    what makes the move possible at all. The file's identity is carried across that handover
    and checked again before the move, so the name having come to mean a different file is
    a refusal rather than an overwrite.
    """
    project_root = Path(project_root).resolve()
    done: list[dict] = []
    for entry in ready(entries):
        relative = str(entry["path"])
        handle, refusal = _open_checked(project_root, relative)
        if handle is None:
            done.append(_refuse(entry, refusal or "could not be opened safely"))
            continue
        try:
            info = os.fstat(handle)
            mode, identity = stat.S_IMODE(info.st_mode), _file_identity(info)
            text = _read_all(handle).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            done.append(_refuse(entry, f"{type(exc).__name__} while re-reading"))
            continue
        finally:
            os.close(handle)
        lines = text.splitlines(keepends=True)
        index = int(entry["line"]) - 1
        if not 0 <= index < len(lines) or lines[index].splitlines()[0] != entry["old_line"]:
            done.append(_refuse(entry, "the line changed since the plan was made"))
            continue
        ending = lines[index][len(entry["old_line"]):]
        lines[index] = entry["new_line"] + ending
        try:
            _replace_contents(project_root / relative, "".join(lines).encode("utf-8"), mode, identity)
        except OSError as exc:
            done.append(_refuse(entry, f"{type(exc).__name__} while writing ({exc}); the file was left untouched"))
            continue
        done.append({**entry, "status": "applied"})
    return done


def knowledge_claims(entries: list[dict], project_root: Path) -> list[dict]:
    """Applied tags as promotable claims, each anchored to the line it now lives on.

    Built only from entries that were actually written: a claim about a tag that was
    proposed and never applied would enter Git describing code that does not say that.
    The anchor is taken with the same function /.promote will later check it with.
    """
    from exoma.core.knowledge.verify import anchor_for

    claims: list[dict] = []
    for entry in entries:
        if entry.get("status") != "applied":
            continue
        path, line = str(entry["path"]), int(entry["line"])
        claims.append(
            {
                "id": f"e2e-tag-{entry['tag']}",
                "statement": (
                    f"The element step '{entry['step_id']}' drives is tagged "
                    f"{TAG_ATTRIBUTE}=\"{entry['tag']}\", verified by a browser run."
                ),
                "sources": [{"type": "code", "path": path, "line": line, "anchor": anchor_for(project_root, path, line)}],
            }
        )
    return claims
