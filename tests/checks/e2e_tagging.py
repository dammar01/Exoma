"""Tagging proven elements: what may be written back to a template, and what may not.

The boundary under test is narrow on purpose. A run may add `data-e2e` to a line the draft
itself cited, in a template file, inside the project, that Git carries — and to nothing
else. Everything here is a way that boundary can be crossed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from exoma.core.evidence.e2e import tagging
from tests.checks.support import assert_true

_BUTTON = '  <button class="primary" onclick="save()">Simpan</button>'


def _repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="e2e-tagging-")).resolve()
    subprocess.run(["git", "init", "-q"], cwd=root, check=False, capture_output=True)
    (root / ".gitignore").write_text("secret/\n", encoding="utf-8")
    (root / "templates").mkdir()
    (root / "templates" / "form.html").write_text(f'<div>\n{_BUTTON}\n</div>\n  <a href="/x">link</a>\n', encoding="utf-8")
    (root / "secret").mkdir()
    (root / "secret" / "hidden.html").write_text(f"<div>\n{_BUTTON}\n</div>\n", encoding="utf-8")
    (root / "app.py").write_text("def view():\n    return render()\n", encoding="utf-8")
    return root


def _trail(**row) -> dict:
    base = {"step_id": "save-item", "action": "click", "status": "passed", "selector_provenance": "source"}
    return {"trail": [{**base, **row}]}


def _test_e2e_tagging() -> None:
    # --- what becomes a proposal at all ----------------------------------------------
    selection = {"candidate": 0, "source_ref": "templates/form.html:2", "selector_keys": ["name", "role"]}
    assert_true(
        [p["ref"] for p in tagging.proposals(_trail(selection=selection))] == ["templates/form.html:2"],
        "a passed step whose winning selector came from source is a proposal",
    )
    assert_true(
        tagging.proposals(_trail(selection=selection, status="failed")) == [],
        "a step that failed proves nothing about the element it could not find",
    )
    assert_true(
        tagging.proposals(_trail(selection={"candidate": 1}, selector_provenance="heuristic")) == [],
        "a heuristic selector has no address, so it gets no tag rather than a guessed one",
    )
    assert_true(
        tagging.proposals(_trail(selection={"candidate": 0})) == [],
        "and a source selector the draft never cited a line for is left alone too",
    )
    many = {"trail": [{"step_id": f"s{n}", "status": "passed", "selector_provenance": "source", "selection": selection} for n in range(tagging.MAX_PROPOSALS + 10)]}
    assert_true(len(tagging.proposals(many)) == tagging.MAX_PROPOSALS, "one run's worth, not a refactor")

    # --- the line rewrite ------------------------------------------------------------
    # One test attribute across the runtime: what tagging writes is what an `e2e` selector
    # reads. A drift here is the data-testid/data-e2e split this pairing exists to prevent.
    from exoma.core.evidence.e2e.spec import E2E_ATTRIBUTE, e2e_css

    assert_true(tagging.TAG_ATTRIBUTE == E2E_ATTRIBUTE == "data-e2e", "tagging and selectors share one attribute")
    assert_true("testid" not in tagging.tagged_line(_BUTTON, "save-item"), "a proposal never writes data-testid")
    assert_true(e2e_css("save-item") in f'[{tagging.TAG_ATTRIBUTE}="save-item"]', "the selector for a proposed tag is the tag")
    assert_true(
        tagging.tagged_line(_BUTTON, "save-item") == '  <button data-e2e="save-item" class="primary" onclick="save()">Simpan</button>',
        f"the attribute lands right after the element name: {tagging.tagged_line(_BUTTON, 'save-item')}",
    )
    assert_true(
        tagging.tagged_line('<input value="a>b" />', "x") == '<input data-e2e="x" value="a>b" />',
        "a `>` inside an attribute value does not end the tag early",
    )
    assert_true(tagging.tagged_line("just text", "x") is None, "a line with no opening tag cannot be tagged")
    # `path:line` addresses a LINE, not an element. Where the line does not pick one out on
    # its own, the honest answer is to refuse rather than to edit the first thing that matches.
    assert_true(
        tagging.tagged_line("<div><button>x</button></div>", "x") is None,
        "two opening tags on one line name no single element",
    )
    assert_true(
        tagging.tagged_line("<!-- <button>x</button> -->", "x") is None,
        "markup inside a comment is not the element the browser clicked",
    )
    assert_true(
        tagging.tagged_line('<!-- note --> <button>x</button>', "x") is not None,
        "but a comment that CLOSED before the element does not disqualify it",
    )
    assert_true(
        all("selector" not in p or p.get("selector") is None for p in tagging.proposals(_trail(selection=selection))),
        "a proposal carries selector KEY NAMES, never values: a value can be a resolved credential",
    )

    root = _repo()
    try:
        def plan(ref: str, tag: str = "save-item") -> dict:
            return tagging.plan(root, [{"step_id": "save-item", "tag": tag, "ref": ref}])[0]

        ok = plan("templates/form.html:2")
        assert_true(
            ok["status"] == "ready" and ok["path"] == "templates/form.html" and ok["line"] == 2 and 'data-e2e="save-item"' in ok["new_line"],
            f"a cited template line is ready, with the exact replacement recorded: {ok}",
        )

        # --- every way out of the boundary ---------------------------------------------
        for ref, needle, label in (
            ("../outside/form.html:2", "outside the project", "a path that climbs out of the project"),
            (str((root.parent / "form.html")), "names no line", "an absolute path with no line"),
            ("app.py:1", "not a template file", "a reference to code that renders, not markup"),
            ("templates/missing.html:2", "does not exist", "a file that is not there"),
            ("secret/hidden.html:2", "git-ignored", "a file Git is told to ignore"),
            ("templates/form.html:99", "points at 99", "a line past the end of the file"),
            ("templates/form.html:3", "no single, uncommented opening tag", "a closing tag is not an element to name"),
            ("templates/form.html", "names no line", "a reference with no line at all"),
        ):
            entry = plan(ref)
            assert_true(
                entry["status"] == "skipped" and needle in entry["reason"],
                f"{label} is refused: {entry.get('reason')}",
            )
        assert_true(
            plan("templates/form.html:2", tag="Save Item")["status"] == "skipped",
            "a tag value that is not a kebab identifier never reaches a file",
        )
        twice = tagging.plan(root, [
            {"step_id": "a", "tag": "save-item", "ref": "templates/form.html:2"},
            {"step_id": "b", "tag": "save-item", "ref": "templates/form.html:2"},
        ])
        assert_true(
            twice[0]["status"] == "ready" and twice[1]["status"] == "skipped" and "already proposed" in twice[1]["reason"],
            f"one tag value cannot name two elements: {twice[1].get('reason')}",
        )

        # --- applying, and refusing to apply -------------------------------------------
        entries = tagging.plan(root, [{"step_id": "save-item", "tag": "save-item", "ref": "templates/form.html:2"}])
        applied = tagging.apply(root, entries)
        body = (root / "templates" / "form.html").read_text(encoding="utf-8")
        assert_true(
            applied[0]["status"] == "applied" and 'data-e2e="save-item"' in body and body.count("<button") == 1,
            f"the confirmed tag is written once, in place: {body!r}",
        )
        assert_true(
            tagging.plan(root, [{"step_id": "save-item", "tag": "save-item", "ref": "templates/form.html:2"}])[0]["status"] == "skipped",
            "and proposing the same line again finds it already tagged",
        )
        stale = tagging.plan(root, [{"step_id": "other", "tag": "other", "ref": "templates/form.html:4"}])
        assert_true(stale[0]["status"] == "ready", f"fixture assumption: line 4 is taggable before the file moves: {stale[0]}")
        (root / "templates" / "form.html").write_text("<div>\n<p>moved</p>\n</div>\n<span>x</span>\n", encoding="utf-8")
        assert_true(
            tagging.apply(root, stale)[0]["status"] == "skipped",
            "a line that changed while the user was being asked is skipped, not overwritten",
        )

        # --- the gap between the plan and the answer -----------------------------------
        # plan() resolves the path, so a symlink out of the project is caught there. apply()
        # runs AFTER the user has been asked, which is time enough for the path to become
        # one, so it repeats the check instead of trusting the plan.
        outside = root.parent / f"{root.name}-outside.html"
        outside.write_text(f"<div>\n{_BUTTON}\n</div>\n", encoding="utf-8")
        (root / "templates" / "late.html").write_text(f"<div>\n{_BUTTON}\n</div>\n", encoding="utf-8")
        swapped = tagging.plan(root, [{"step_id": "swap", "tag": "swap", "ref": "templates/late.html:2"}])
        assert_true(swapped[0]["status"] == "ready", f"fixture assumption: the file is taggable while it is a real file: {swapped[0]}")
        linked = root / "templates" / "late.html"
        linked.unlink()
        try:
            linked.symlink_to(outside)
        except (OSError, NotImplementedError):
            linked.write_text("", encoding="utf-8")  # no symlink privilege: fall back to the line check
        result = tagging.apply(root, swapped)
        assert_true(
            result[0]["status"] == "skipped",
            f"a path swapped after the plan is refused, not followed: {result[0].get('reason')}",
        )
        assert_true(
            outside.read_text(encoding="utf-8") == f"<div>\n{_BUTTON}\n</div>\n",
            "and nothing outside the project was written",
        )
        outside.unlink()
        # Deterministic on every platform, symlink privilege or not: apply() is handed an
        # entry whose path leaves the project and must refuse on the path alone.
        escaped = tagging.apply(root, [{"status": "ready", "tag": "x", "path": "../escape.html", "line": 1, "old_line": "<p>", "new_line": "<p>"}])
        assert_true(
            escaped[0]["status"] == "skipped" and "outside the project" in escaped[0]["reason"],
            f"a path that leaves the project is refused before the file is opened: {escaped[0].get('reason')}",
        )

        # A hard link makes the same bytes reachable under another name, so "inside the
        # project" stops meaning anything. Refused on the link count, before the write.
        twin = root.parent / f"{root.name}-twin.html"
        linked_in = root / "templates" / "twin.html"
        linked_in.write_text(f'<div>\n{_BUTTON}\n</div>\n', encoding="utf-8")
        hard = tagging.plan(root, [{"step_id": "twin", "tag": "twin", "ref": "templates/twin.html:2"}])
        assert_true(hard[0]["status"] == "ready", "fixture assumption: the file is taggable before it is linked")
        try:
            os.link(linked_in, twin)
        except (OSError, NotImplementedError, AttributeError):
            twin = None
        if twin is not None:
            result = tagging.apply(root, hard)
            assert_true(
                result[0]["status"] == "skipped" and "links" in result[0]["reason"],
                f"a hard-linked template is refused: {result[0].get('reason')}",
            )
            assert_true(
                "data-e2e" not in twin.read_text(encoding="utf-8"),
                "and the name outside the project was not written through",
            )
            twin.unlink()

        # A comment that opened on an earlier line hides this one too, and tagged_line
        # cannot see that far on its own.
        (root / "templates" / "hidden-block.html").write_text(f'<!--\n{_BUTTON}\n-->\n', encoding="utf-8")
        blocked = plan("templates/hidden-block.html:2", tag="hidden")
        assert_true(
            blocked["status"] == "skipped" and "comment that opened earlier" in blocked["reason"],
            f"a multi-line comment is not a place to put a tag: {blocked.get('reason')}",
        )

        # --- partial I/O: the template comes back, it does not come back empty -----------
        big = root / "templates" / "big.html"
        filler = "\n".join(f"  <span>row {n}</span>" for n in range(5000))
        big.write_text(f'<div>\n{_BUTTON}\n{filler}\n</div>\n', encoding="utf-8")
        before = big.read_text(encoding="utf-8")
        entry = tagging.plan(root, [{"step_id": "big", "tag": "big", "ref": "templates/big.html:2"}])
        assert_true(entry[0]["status"] == "ready", "fixture assumption: the big file is taggable")
        real_write = os.write
        state = {"calls": 0}

        def flaky(handle, payload):
            state["calls"] += 1
            if state["calls"] == 1:
                return real_write(handle, payload[: len(payload) // 3])  # a short write, as os.write may do
            if state["calls"] == 2:
                raise OSError("disk went away")  # ...and then the rest never lands
            return real_write(handle, payload)

        tagging.os.write = flaky
        try:
            failed = tagging.apply(root, entry)
        finally:
            tagging.os.write = real_write
        assert_true(
            failed[0]["status"] == "skipped" and "left untouched" in failed[0]["reason"],
            f"a write that cannot finish is reported, not swallowed: {failed[0].get('reason')}",
        )
        assert_true(
            big.read_text(encoding="utf-8") == before,
            "and the user's template is the one they had: the half-written bytes went to a temporary file, never to it",
        )
        assert_true(
            not [q for q in (root / "templates").iterdir() if q.name.startswith(".e2e-tag-")],
            f"no temporary file is left behind: {[q.name for q in (root / 'templates').iterdir()]}",
        )
        assert_true(
            tagging.apply(root, entry)[0]["status"] == "applied" and len(big.read_text(encoding="utf-8")) > len(before),
            "a file larger than one read chunk still round-trips whole",
        )

        # --- the name came to mean a different file between the read and the move --------
        swap_target = root / "templates" / "swap.html"
        swap_target.write_text('<div>\n  <button>x</button>\n</div>\n', encoding="utf-8")
        swap_entry = tagging.plan(root, [{"step_id": "swap2", "tag": "swap2", "ref": "templates/swap.html:2"}])
        assert_true(swap_entry[0]["status"] == "ready", "fixture assumption: the file is taggable before the swap")
        # The window is between closing the descriptor and moving the new file into place,
        # so the swap is staged on the close itself.
        # `tagging.os` is the process-wide `os`, and on POSIX `subprocess` (git.is_ignored)
        # closes pipe descriptors through it too. Swapping on the first close of ANY handle
        # would move the file before it was read; only the target's own descriptor counts.
        real_close = os.close
        swapped_once = {"done": False}
        swap_identity = (os.stat(swap_target).st_dev, os.stat(swap_target).st_ino)

        def close_then_swap(handle):
            is_target = False
            if not swapped_once["done"]:
                try:
                    info = os.fstat(handle)
                    is_target = (info.st_dev, info.st_ino) == swap_identity
                except OSError:
                    pass
            real_close(handle)
            if is_target:
                swapped_once["done"] = True
                swap_target.unlink()  # the name now means a different file than the one read
                swap_target.write_text("<p>someone else's file</p>\n", encoding="utf-8")

        tagging.os.close = close_then_swap
        try:
            raced = tagging.apply(root, swap_entry)
        finally:
            tagging.os.close = real_close
        assert_true(
            raced[0]["status"] == "skipped" and "not the file that was read" in raced[0]["reason"],
            f"a name that now points at another file is refused, not overwritten: {raced[0].get('reason')}",
        )
        assert_true(
            swap_target.read_text(encoding="utf-8") == "<p>someone else's file</p>\n",
            "and the file that took its place keeps its own content",
        )
        assert_true(
            not [q for q in (root / "templates").iterdir() if q.name.startswith(".e2e-tag-")],
            "with no temporary left over",
        )

        claims = tagging.knowledge_claims(applied, root)
        assert_true(
            len(claims) == 1 and claims[0]["id"] == "e2e-tag-save-item" and claims[0]["sources"][0]["path"] == "templates/form.html",
            f"an applied tag becomes one anchored claim /.promote can verify: {claims}",
        )
        assert_true(
            tagging.knowledge_claims([{**applied[0], "status": "skipped"}], root) == [],
            "a tag that was never written claims nothing",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
