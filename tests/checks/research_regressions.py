"""Native P01-P12/G01-G04 from the 2026-11-10 research handoff.

Command strings are data passed to policy functions, never executed. These checks
use the checkout policy and temporary session paths, without installed hooks or Bash.
"""

import importlib.util
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from exoma.core.evidence.contract import validate_verification_contract, verify_exit_status
from exoma.core.evidence.e2e.normalize import _reviewer_sections
from tests.checks.support import assert_true


def _report(item: str) -> str:
    return (
        "[VERIFICATION]\nverdict: DONE\nconfidence: high\nblocking_findings:\n- " + item
        + "\nescalations:\n- none\nnotes:\n- none\nchecks_run:\n- read fixture.py:1"
        "\nnot_verified:\n- none\n"
    )


def _test_research_parser_regressions() -> None:
    cases = (
        ("P01", "none", "pass"),
        ("P02", "none.", "pass"),
        ("P03", "tidak ada", "pass"),
        ("P04", "none - clean", "pass"),
        ("P05", "none; all clean", "pass"),
        ("P06", "none - caller X fails", "fail"),
        ("P07", "[severity:high] [origin:introduced] [scope_relation:in_scope] caller fails [fixture.py:1]", "fail"),
        ("P08", "none.\n  caller X fails [fixture.py:1]", "fail"),
        ("P09", "none (caller X fails)", "fail"),
        ("P10", "none\n  caller X fails [fixture.py:1]", "fail"),
    )
    for case_id, item, expected in cases:
        actual = validate_verification_contract(_report(item))
        assert_true(actual["verdict"] == expected, f"{case_id}: expected {expected}, got {actual}")
        expected_exit = 0 if expected == "pass" else 2
        assert_true(verify_exit_status(actual["verdict"], actual) == expected_exit,
                    f"{case_id}: expected exit {expected_exit}: {actual}")
    for case_id, first in (("P11", "none."), ("P12", "none")):
        item = first + "\n  caller X fails [fixture.py:1]"
        findings = _reviewer_sections(_report(item))["blocking_findings"]
        assert_true(findings == [item], f"{case_id}: retain the whole finding: {findings}")


def _test_research_gate_regressions() -> None:
    policy_path = (Path(__file__).resolve().parents[2] / "src/exoma/_bundle/config/hosts/claude/hooks/workflow_policy.py")
    spec = importlib.util.spec_from_file_location("research_policy_under_test", policy_path)
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    with tempfile.TemporaryDirectory(prefix="exoma-research-gates-") as scratch:
        a, b = (policy.runtime_dir(scratch, sid) for sid in ("session-A", "session-B"))
        assert_true(a != b, "G01: authorization paths must differ between sessions")
        marker_a = os.path.join(a, "commit.marker")
        with patch.object(policy, "marker_fresh", side_effect=lambda path: path == marker_a):
            assert_true(policy.commit_refusal("git commit", a) is None, "G01: authorized A allowed")
            assert_true(policy.commit_refusal("git commit", b) is not None, "G01: B remains refused")
        with patch.object(policy, "marker_fresh", return_value=False):
            refused = policy.commit_refusal("git commit", a)
            assert_true(refused is not None and refused[0] == "commit_unapproved", "G02: unapproved action refused")
            harmless = 'python -c "print(\'git commit\')"'
            assert_true(policy.commit_refusal(harmless, a) is None, "G03: literal print is data")
            # A quoted mention must never exempt an additional action or expansion.
            for command in (
                harmless + "; git commit -m x",
                harmless + "\ngit commit -m x",
                harmless + " | git commit -m x",
                harmless + " && git commit -m x",
                'bash -c "git commit -m x"',
                'python -c "print(\'$(git commit)\')"',
                'python -c "print(\'`git commit`\')"',
                'python -c "print(\'git commit\'); __import__(\'os\').system(\'git commit\')"',
                'python -c "print(__import__(\'os\').system(\'git commit\'))"',
                'python -c "print(f\'git commit {action()}\')"',
                'python -c "print(\'git commit\', file=open(\'output\', \'w\'))"',
                'git.exe commit -m x',
                '"C:/Program Files/Git/cmd/git.exe" -C "repo with spaces" commit -m x',
            ):
                assert_true(policy.commit_refusal(command, a) is not None,
                            f"G03 control: action or unsupported command remains refused: {command!r}")
        assert_true(policy.in_scope("docs/research/schema.yaml", ["docs/research/"]),
                    "G04: existing directory scope matches")
