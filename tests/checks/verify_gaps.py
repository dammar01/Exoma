"""Verify honesty: unchecked files must not read as a pass, and the routing table
must decide what blocks — in both directions."""

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from exoma import check
from exoma import cli as main
from exoma.adapters.providers.opencode_adapter import OpenCodeAdapter
from exoma.core.provider.executor import Executor
from exoma.core.jobs.job_manager import JobManager
from exoma.core.prompt.prompt_builder import build_prompt
from exoma.core.runtime.state import ensure_workflow_workspace

from tests.checks.support import (
    FakeJobProcess,
    FakeOpenCodeAdapter,
    RecordingOpenCodeAdapter,
    assert_true,
    clean_output,
    extract_session_id,
)

def _check_scope_width() -> None:
    """The working tree's width is measured, scoped only when a plan declared a scope, and
    called wide past the ceiling rather than at it."""
    from exoma.core.evidence import quick_verify

    repo = Path(tempfile.mkdtemp(prefix="scope-width-"))
    git = ["git", "-c", "user.email=t@example.test", "-c", "user.name=t"]
    original_files = quick_verify.SCOPE_WIDE_FILES
    original_lines = quick_verify.SCOPE_WIDE_LINES
    try:
        assert_true(
            main.subprocess.run(["git", "init", "-q", str(repo)], capture_output=True).returncode == 0,
            "scope width git init failed",
        )
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("one\ntwo\n", encoding="utf-8")
        main.subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        committed = main.subprocess.run(
            [*git, "commit", "-q", "-m", "base"], cwd=repo, capture_output=True, text=True
        )
        assert_true(committed.returncode == 0, f"scope width commit failed: {committed.stderr}")

        (repo / "src" / "a.py").write_text("one\nTWO\nthree\n", encoding="utf-8")  # +2 -1
        (repo / "notes.md").write_text("a\nb\nc\n", encoding="utf-8")  # untracked, +3

        unscoped = quick_verify.scope_width(repo, None)
        assert_true(
            unscoped["files"] == 2 and unscoped["lines"] == 6,
            f"tracked changes and untracked files are both measured: {unscoped}",
        )
        assert_true(
            unscoped["scope_declared"] is False and unscoped["outside_scope"] == [],
            f"with no plan scope, no file is accused of being unplanned: {unscoped}",
        )
        assert_true(unscoped["wide"] is False, f"two files is not wide: {unscoped}")

        scoped = quick_verify.scope_width(repo, ["src/", "  "])
        assert_true(
            scoped["scope_declared"] is True and scoped["outside_scope"] == ["notes.md"],
            f"only the file outside the declared scope is named: {scoped}",
        )
        backslash = quick_verify.scope_width(repo, ["SRC\\a.py", "notes.md"])
        assert_true(
            backslash["outside_scope"] == [],
            f"scope entries match case- and separator-insensitively: {backslash}",
        )

        quick_verify.SCOPE_WIDE_FILES = 2
        assert_true(
            quick_verify.scope_width(repo, None)["wide"] is False,
            "wide is past the file ceiling, not at it",
        )
        quick_verify.SCOPE_WIDE_FILES = 1
        assert_true(
            quick_verify.scope_width(repo, None)["wide"] is True,
            "more files than the ceiling reads as wide",
        )
        quick_verify.SCOPE_WIDE_FILES = original_files
        quick_verify.SCOPE_WIDE_LINES = 5
        assert_true(
            quick_verify.scope_width(repo, None)["wide"] is True,
            "more changed lines than the ceiling reads as wide on its own",
        )
    finally:
        quick_verify.SCOPE_WIDE_FILES = original_files
        quick_verify.SCOPE_WIDE_LINES = original_lines
        shutil.rmtree(repo, ignore_errors=True)


def _test_quick_verify_gaps() -> None:
    """Unavailable, deleted, and name-check failures cannot produce a pass verdict."""
    import shutil as _shutil

    from exoma.core.evidence import quick_verify

    _check_scope_width()

    root = Path(tempfile.mkdtemp(prefix="quick-verify-"))
    original_discover = quick_verify._discover_changed_files
    original_which = quick_verify.shutil.which
    original_run = quick_verify._run
    try:
        (root / "sample.php").write_text("<?php echo 1;\n", encoding="utf-8")
        quick_verify._discover_changed_files = lambda _root: (["sample.php"], [])
        quick_verify.shutil.which = lambda _tool: None
        result = quick_verify.run(root, "verify-gaps")
        assert_true(
            result.get("meta", {}).get("verdict") == "incomplete",
            f"missing toolchain must be incomplete: {result}",
        )

        quick_verify._discover_changed_files = lambda _root: (["deleted.py"], [])
        result = quick_verify.run(root, "verify-deleted")
        assert_true(
            result.get("meta", {}).get("verdict") == "incomplete",
            f"deleted changed files must be incomplete: {result}",
        )

        (root / "names.py").write_text("print(missing_name)\n", encoding="utf-8")
        quick_verify._discover_changed_files = lambda _root: (["names.py"], [])
        quick_verify.shutil.which = lambda _tool: None
        result = quick_verify.run(root, "verify-name-tool-missing")
        assert_true(
            result.get("meta", {}).get("verdict") == "incomplete",
            f"missing Python name checker must be incomplete: {result}",
        )

        quick_verify.shutil.which = lambda _tool: "pyflakes"
        quick_verify._run = lambda _argv, _root: (1, "undefined name 'missing_name'")
        result = quick_verify.run(root, "verify-names")
        assert_true(
            result.get("meta", {}).get("verdict") == "fail",
            f"name findings must fail verification: {result}",
        )

        quick_verify._discover_changed_files = lambda _root: (
            [],
            ["git diff --name-only --: not a git repository"],
        )
        result = quick_verify.run(root, "verify-discovery-error")
        assert_true(
            result.get("meta", {}).get("verdict") == "incomplete"
            and result.get("meta", {}).get("discovery_errors"),
            f"change-discovery failures must be incomplete: {result}",
        )

        quick_verify._discover_changed_files = original_discover
        quick_verify._run = original_run
        quick_verify.shutil.which = original_which
        unborn = Path(tempfile.mkdtemp(prefix="quick-unborn-"))
        try:
            initialized = main.subprocess.run(
                ["git", "init", "-q", str(unborn)], capture_output=True, text=True
            )
            assert_true(initialized.returncode == 0, "quick verify git init failed")
            (unborn / "staged.json").write_text('{"ok": true}\n', encoding="utf-8")
            staged = main.subprocess.run(
                ["git", "add", "staged.json"], cwd=unborn, capture_output=True, text=True
            )
            assert_true(staged.returncode == 0, f"quick verify git add failed: {staged.stderr}")
            result = quick_verify.run(unborn, "verify-unborn-staged")
            assert_true(
                result.get("meta", {}).get("verdict") == "pass"
                and "staged.json" in result.get("meta", {}).get("quick_verify", {}).get("passed", []),
                f"unborn staged files must be verified: {result}",
            )
        finally:
            _shutil.rmtree(unborn, ignore_errors=True)
    finally:
        quick_verify._discover_changed_files = original_discover
        quick_verify.shutil.which = original_which
        quick_verify._run = original_run
        _shutil.rmtree(root, ignore_errors=True)


def _test_verification_routing() -> None:
    """A finding blocks because the table routes it there, not because of its heading.

    The table was applied one way. A note-class finding written into `blocking_findings`
    was flagged `finding_misrouted` and then counted as blocking anyway, so a run that had
    found nothing worth blocking still came back a failure — with the runtime naming a
    blocking finding it had just said belonged in notes.
    """
    from exoma.core.evidence.contract import validate_verification_contract

    def _report(section: str, finding: str) -> str:
        sections = {
            "blocking_findings": "- none",
            "escalations": "- none",
            "notes": "- none",
        }
        sections[section] = f"- {finding}"
        return (
            "[VERIFICATION]\n"
            "verdict: NEEDS FIX\n"
            f"blocking_findings:\n{sections['blocking_findings']}\n"
            f"escalations:\n{sections['escalations']}\n"
            f"notes:\n{sections['notes']}\n"
            "checks_run:\n- read the diff\n"
            "not_verified: none\n"
            "confidence: medium — read only\n"
        )

    note_class = (
        "severity: low | origin: introduced | scope_relation: in_scope — naming drift "
        "[core/executor.py:808]"
    )
    demoted = validate_verification_contract(_report("blocking_findings", note_class))
    assert_true(
        demoted["blocking_findings"] == 0
        and any(w["kind"] == "finding_misrouted" for w in demoted["warnings"]),
        f"a note-class finding filed under blocking must not count as blocking: {demoted}",
    )

    # The other direction is what keeps this fail-closed and must not move.
    blocking_class = (
        "severity: critical | origin: introduced | scope_relation: in_scope — data loss "
        "[core/executor.py:808]"
    )
    promoted = validate_verification_contract(_report("notes", blocking_class))
    assert_true(
        promoted["blocking_findings"] == 1 and promoted["verdict"] == "fail",
        f"a blocking-class finding filed under notes must still block: {promoted}",
    )

    # Tags the table cannot read are no argument for ignoring the section they sit in.
    untagged = validate_verification_contract(
        _report("blocking_findings", "something is wrong [core/executor.py:808]")
    )
    assert_true(
        untagged["blocking_findings"] == 1
        and any(w["kind"] == "invalid_finding_tags" for w in untagged["warnings"]),
        f"an unroutable finding in blocking_findings must keep blocking: {untagged}",
    )


def _test_empty_section_is_not_a_finding() -> None:
    """An empty section holds nothing — not the heading that follows it.

    `_section_items` matched the heading with `\\s*` after the colon, and `\\s` includes the
    newline. Under re.MULTILINE that walked off the end of the heading line: a section with
    nothing under it swallowed the blank line and captured the NEXT heading, so
    `blocking_findings:` came back holding one item, the literal string "escalations:".

    That is exactly the shape a CLEAN verify has, so the phantom item collided with
    `verdict: DONE` and every honest pass was returned as `fail` — naming a blocking
    finding that was a section header. Two real delegated verifies failed that way before
    the cause was found, which is why this pins the parse and the verdict, not just one.

    The only spelling the parser accepted was a literal `- none` line, so that path is
    asserted here too: it is what most reports use, and loosening the heading match must
    not quietly change it.
    """
    from exoma.core.evidence.contract import _section_items, validate_verification_contract

    note = (
        "severity: low | origin: introduced | scope_relation: in_scope — checked "
        "[core/executor.py:808]"
    )

    def _report(blocking: str, escalations: str, not_verified: str = "none") -> str:
        return (
            "[VERIFICATION]\n"
            "verdict: DONE\n"
            f"blocking_findings:{blocking}\n"
            f"escalations:{escalations}\n"
            f"notes:\n- {note}\n"
            "checks_run:\n- read the diff\n"
            f"not_verified: {not_verified}\n"
            "confidence: high — read the diff\n"
        )

    empty = _report("\n", "\n")
    assert_true(
        _section_items(empty, "blocking_findings") == [],
        "an empty section must not absorb the heading that follows it: "
        f"{_section_items(empty, 'blocking_findings')}",
    )
    assert_true(
        _section_items(empty, "escalations") == [],
        "the same holds one heading further down, where the bug repeated",
    )
    assert_true(
        len(_section_items(empty, "notes")) == 1,
        "a section that does have an item still yields exactly that item",
    )

    assessed = validate_verification_contract(empty)
    assert_true(
        assessed["blocking_findings"] == 0
        and not any(w["kind"] == "verdict_mismatch" for w in assessed["warnings"]),
        f"an empty section is not a blocking finding and cannot conflict: {assessed}",
    )
    # Still not a pass: an empty section is ambiguous between "found nothing" and "did not
    # fill this in", and the contract answers that with `- none`. Only the phantom finding
    # was the bug — the demand for an explicit answer is the rule.
    assert_true(
        assessed["verdict"] == "incomplete"
        and any(w["kind"] == "empty_section" for w in assessed["warnings"]),
        f"an unanswered section stays incomplete rather than passing: {assessed}",
    )

    spelled_out = validate_verification_contract(_report("\n- none", "\n- none"))
    assert_true(
        spelled_out["verdict"] == "pass"
        and spelled_out["blocking_findings"] == 0
        and not spelled_out["warnings"],
        f"an explicit none is still the clean pass it was: {spelled_out}",
    )

    # The inline form shares the heading regex; it is the reason the match cannot simply
    # stop at the end of the heading word.
    inline = validate_verification_contract(
        _report("\n- none", "\n- none", not_verified="none")
    )
    assert_true(
        inline["verdict"] == "pass",
        f"`not_verified: none` on the heading line still parses: {inline}",
    )
    padded = _report("   \n", "\n").replace("notes:", "  notes:  ")
    assert_true(
        len(_section_items(padded, "notes")) == 1,
        "indentation and trailing spaces around a heading stay tolerated",
    )

    # The other spellings agents write for "nothing found". Each used to read as an
    # untagged blocking finding, so a clean DONE came back `fail` (CASE-009).
    for clean in (
        "none.",
        "None; all clean",
        "none (checked core/evidence/contract.py:590 and its callers, clean)",
        "none - semua bersih",
        "none - clean",
        "none: nothing blocking",
        "(none)",
        "none (tidak ada blocking findings)",
        "tidak ada",
        "Tidak ada temuan",
        "no blocking findings",
        "nothing to report",
        "none (checked hooks and telemetry)",
    ):
        verdict = validate_verification_contract(_report(f"\n- {clean}", f"\n- {clean}"))
        assert_true(
            verdict["verdict"] == "pass" and verdict["blocking_findings"] == 0,
            f"`- {clean}` says nothing was found and must stay a clean pass: {verdict}",
        )
    # And what must still be a finding: a sentinel word running into a claim, a note that
    # carries tags, or a phrase that only starts like one.
    for finding in (
        "none of the callers handle a missing token",
        "none; severity: high | origin: introduced | scope_relation: in_scope",
        "tidak ada validasi untuk input kosong",
        "no check on the empty path",
        "nothing stops a second writer",
        # A note after a separator must end clean and name no problem.
        "none - caller X fails",
        "none; caller X fails, rest clean",
        "none - see below",
        # A blocklist of problem words lost to each of these in review; only a closed list
        # of neutral phrases may follow a separator. Explanation goes in parentheses.
        "none - issue in caller X, clean",
        "none - defect found, clean",
        "none - concern remains, clean",
        "none; all inspected paths are clean",
    ):
        verdict = validate_verification_contract(_report(f"\n- {finding}", "\n- none"))
        assert_true(
            verdict["verdict"] == "fail" and verdict["blocking_findings"] == 1,
            f"`- {finding}` is a claim, not an empty section, and keeps blocking: {verdict}",
        )

    # The browser reviewer's normalizer reads the same sentinel; it must not keep as a
    # finding what the validator above now drops.
    from exoma.core.evidence.contract import _NONE_ITEM

    assert_true(
        not _NONE_ITEM.fullmatch("none - clean\ncaller X fails"),
        "text spanning lines is never a sentinel, whatever its first line says",
    )

    from exoma.core.evidence.e2e.normalize import _reviewer_sections

    reviewed = _reviewer_sections(_report("\n- none; all clean", "\n- tidak ada"))
    assert_true(
        not reviewed.get("blocking_findings") and not reviewed.get("escalations"),
        f"the E2E normalizer drops the same sentinels as the validator: {reviewed}",
    )
    # The normalizer joins continuation lines into the item and judges the first line. A
    # separator note there must not hide what follows; the plain forms keep their old reach.
    hidden = _reviewer_sections(_report("\n- none - clean\n  caller X fails on retry", "\n- none"))
    assert_true(
        len(hidden.get("blocking_findings") or []) == 1,
        f"`none - clean` followed by a continuation line is kept as an item: {hidden}",
    )
    plain = _reviewer_sections(_report("\n- none (checked the retry path)\n  and the timeout path", "\n- none"))
    assert_true(
        not plain.get("blocking_findings"),
        f"a plain `none (why)` with a continuation line stays empty, as before: {plain}",
    )
    # Only the old pattern's forms: the wider spellings never reached this far before.
    for first in ("none.", "(none).", "none!", "no findings", "nothing found", "tidak ada", "none (severity: high)"):
        kept = _reviewer_sections(_report(f"\n- {first}\n  caller X fails on retry", "\n- none"))
        assert_true(
            len(kept.get("blocking_findings") or []) == 1,
            f"`{first}` followed by a continuation line is kept as an item: {kept}",
        )
