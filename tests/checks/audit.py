"""The governance trail can be read back, and says what it does not cover.

Written alongside the reader itself, because a stream with no reader had no way to fail:
`audit.jsonl` was appended to on every delegated call for a whole release and nothing
ever opened it. A missing reader is invisible in a way a broken one is not.

The assertions below care about two things a summary can get quietly wrong. It must not
launder a null provider into whichever provider ran most, and it must not turn an empty
trail into a clean bill of health — an audit that reports 0 problems over 0 rows is the
same output as one that reviewed a hundred calls and found nothing.
"""

from exoma.core.workspace.workspace_paths import data_dir
import json
import tempfile
from pathlib import Path

from exoma.core.audit.audit import load_audit, report
from exoma.core.evidence.runtime_io import write_audit_record
from tests.checks.support import assert_true


def _workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="agent-workflow-audit-"))
    (root / ".workflow").mkdir(parents=True, exist_ok=True)
    return root


def _test_audit_report() -> None:
    root = _workspace()

    # --- an empty trail must be legible as empty, not as clean ---------------------
    empty = report(root)
    assert_true(
        empty["calls"] == 0 and empty["first_recorded"] is None,
        f"an empty trail did not report itself as empty: {empty}",
    )
    assert_true(
        empty["redactions"]["calls_with_redactions"] == 0
        and empty["scope"],
        "an empty trail must still carry its scope statement — otherwise a reader who "
        "sees zero findings cannot tell whether the runtime audits main-agent edits",
    )

    write_audit_record(root, {
        "at": "2026-01-01T00:00:00+00:00", "command": "explore",
        "provider": "opencode", "model": "m1", "ok": True, "redactions": 0,
    })
    write_audit_record(root, {
        "at": "2026-01-02T00:00:00+00:00", "command": "verify",
        "provider": None, "model": "m1", "ok": False,
        "error_type": "worker_died", "redactions": 3,
    })
    write_audit_record(root, {
        "at": "2026-01-03T00:00:00+00:00", "command": "verify",
        "provider": "codex", "model": "m2", "ok": True, "redactions": 2,
    })

    result = report(root)

    assert_true(result["calls"] == 3, f"expected 3 audited calls, got {result['calls']}")
    assert_true(
        result["first_recorded"] == "2026-01-01T00:00:00+00:00"
        and result["last_recorded"] == "2026-01-03T00:00:00+00:00",
        f"the trail's span is wrong: {result['first_recorded']}..{result['last_recorded']}",
    )
    assert_true(
        result["by_command"] == {"explore": 1, "verify": 2},
        f"per-command tally is wrong: {result['by_command']}",
    )

    # --- a null provider stays visible as unknown ----------------------------------
    assert_true(
        result["by_provider"].get("unknown") == 1,
        "a row with no provider was folded into a named one. The field is genuinely null "
        "on real rows, and an audit that guesses is worse than one that says it does not "
        f"know: {result['by_provider']}",
    )

    # --- failures are counted by type, not lumped ----------------------------------
    assert_true(
        result["failures"]["calls"] == 1
        and result["failures"]["by_error_type"] == {"worker_died": 1},
        f"failure tally is wrong: {result['failures']}",
    )

    # --- the number this stream exists to answer -----------------------------------
    assert_true(
        result["redactions"]["calls_with_redactions"] == 2
        and result["redactions"]["total"] == 5,
        "redaction totals are wrong — this is the figure someone reads after a suspected "
        f"leak: {result['redactions']}",
    )


def _test_audit_survives_a_torn_row() -> None:
    """A process killed mid-write must not make the whole trail unreadable."""
    root = _workspace()
    write_audit_record(root, {"at": "2026-01-01T00:00:00+00:00", "command": "explore", "ok": True})

    path = data_dir(root) / "audit.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"at": "2026-01-02T00:00:00+00:00", "comm')

    rows = load_audit(root)
    assert_true(
        len(rows) == 1,
        "a torn final line took the readable rows down with it. The stream is written "
        f"fail-open, so a partial last line is a normal state: got {len(rows)} rows",
    )
    assert_true(
        report(root)["calls"] == 1,
        "the report disagreed with load_audit about how many rows are readable",
    )


def _test_audit_is_not_telemetry() -> None:
    """The two streams stay separate readers over separate files.

    Usage may be resampled or recomputed under a better definition of a metric; an audit
    row may not. Reading one from the other would eventually let a metric change reshape
    the record.
    """
    root = _workspace()
    write_audit_record(root, {"at": "2026-01-01T00:00:00+00:00", "command": "explore", "ok": True})

    usage_path = data_dir(root) / "usage.jsonl"
    usage_path.write_text(
        json.dumps({"command": "plan", "contract_version": 1}) + "\n", encoding="utf-8"
    )

    assert_true(
        [row.get("command") for row in load_audit(root)] == ["explore"],
        "the audit reader picked up usage rows — the streams must not cross",
    )
