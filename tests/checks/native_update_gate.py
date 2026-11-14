"""The native update-gate collector accepts only complete, clean, commit-bound strict receipts."""

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.checks.support import assert_true

REPO = Path(__file__).resolve().parents[2]
SHA = "a" * 40


def _load():
    spec = importlib.util.spec_from_file_location("native_update_gate", REPO / "tools/maintain/native_update_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(gate, platform, python, manager, **override):
    state = {"phase": "succeeded", "cleanup_confirmed": True, "callers_exited_at": 1,
             "stages": {"package": {"started_at": 2}}}
    if platform == "win32":
        state["host_job"] = {"in_job": False}
    report = {"kind": gate.KIND, "schema_version": gate.SCHEMA_VERSION, "mode": "strict", "commit": SHA,
              "commit_dirty": False, "platform": platform, "python_version": python + ".4", "manager": manager,
              "passed": True, "actual_version_transition": True, "independent_worker_verified": True,
              "no_op_verified": True, "manual_recovery": False, "state": state}
    report.update(override)
    return report


def _write_all(gate, directory, platform, **override):
    for python in gate.PYTHONS:
        for manager in gate.MANAGERS:
            (directory / f"{gate.cell_name(platform, python, manager)}.json").write_text(
                json.dumps(_report(gate, platform, python, manager, **override)), encoding="utf-8")


def _test_native_update_gate():
    gate = _load()
    from tests.checks.self_update import _handoff_independence_verified

    with tempfile.TemporaryDirectory(prefix="native-gate-") as tmp:
        root = Path(tmp)
        for platform in gate.PLATFORMS:
            directory = root / platform
            directory.mkdir()
            _write_all(gate, directory, platform)
            assert_true(gate.validate(directory, SHA, platform) == [], f"six complete {platform} cells qualify")
            assert_true(len(list(directory.glob("*.json"))) == 6, "two Pythons times three managers")

        def problems(mutate, platform="linux"):
            directory = root / ("case-" + str(len(list(root.iterdir()))))
            directory.mkdir()
            _write_all(gate, directory, platform)
            mutate(directory)
            return gate.validate(directory, SHA, platform)

        def edit(name, **changes):
            def apply(directory):
                path = directory / name
                report = json.loads(path.read_text(encoding="utf-8"))
                report.update(changes)
                path.write_text(json.dumps(report), encoding="utf-8")
            return apply

        first = f"linux-py{gate.PYTHONS[0]}-{gate.MANAGERS[0]}.json"
        for label, mutate, needle in (
            ("a missing cell", lambda d: (d / first).unlink(), "missing cell"),
            ("a stale commit", edit(first, commit="b" * 40), "commit does not match"),
            ("a dirty checkout", edit(first, commit_dirty=True), "not clean"),
            ("a contained report", edit(first, mode="contained"), "only strict"),
            ("a legacy report without a kind", edit(first, kind=None, schema_version=None), "kind/schema_version"),
            ("a failed no-op", edit(first, no_op_verified=False), "no_op_verified"),
            ("unverified independence", edit(first, independent_worker_verified=False), "independent_worker_verified"),
            ("manual recovery", edit(first, manual_recovery=True), "manual recovery"),
            ("a reported error", edit(first, error="boom"), "error"),
            ("another platform", edit(first, platform="darwin"), "platform"),
            ("an unsupported Python", edit(first, python_version="3.12.1"), "Python"),
            ("unconfirmed cleanup", edit(first, state={"phase": "cleanup_pending"}), "independent"),
            ("a null Python version", edit(first, python_version=None), "Python"),
            ("a list-valued manager", edit(first, manager=["uv"]), "manager"),
            ("a null caller-exit time", edit(first, state={"phase": "succeeded", "cleanup_confirmed": True,
                                                           "callers_exited_at": None,
                                                           "stages": {"package": {"started_at": 2}}}), "independent"),
            ("a string package start", edit(first, state={"phase": "succeeded", "cleanup_confirmed": True,
                                                           "callers_exited_at": 1,
                                                           "stages": {"package": {"started_at": "2"}}}), "independent"),
            ("an infinite caller-exit and package-start time", edit(first, state={
                "phase": "succeeded", "cleanup_confirmed": True, "callers_exited_at": float("inf"),
                "stages": {"package": {"started_at": float("inf")}}}), "independent"),
            ("a Python version that only starts like 3.11", edit(first, python_version="3.110.4"), "Python"),
            ("a 401-digit integer timestamp", edit(first, state={
                "phase": "succeeded", "cleanup_confirmed": True, "callers_exited_at": 10 ** 400,
                "stages": {"package": {"started_at": 10 ** 400}}}), "independent"),
            ("a Python version with a malformed suffix", edit(first, python_version="3.11.xyz"), "Python"),
            ("a boolean schema version", edit(first, schema_version=True), "kind/schema_version"),
            ("a duplicate cell", lambda d: (d / "extra.json").write_text((d / first).read_text(encoding="utf-8")), "duplicates"),
        ):
            result = problems(mutate)
            assert_true(any(needle in item for item in result), f"{label} is refused: {result}")

        windows = f"win32-py{gate.PYTHONS[0]}-{gate.MANAGERS[0]}.json"
        in_job = edit(windows, state={"phase": "succeeded", "cleanup_confirmed": True, "callers_exited_at": 1,
                                      "stages": {"package": {"started_at": 2}}, "host_job": {"in_job": True}})
        assert_true(any("independent" in item for item in problems(in_job, "win32")),
                    "Windows refuses a helper still inside an inherited job")
        (root / "broken").mkdir()
        (root / "broken" / "x.json").write_text("{", encoding="utf-8")
        assert_true(any("unreadable" in item for item in gate.validate(root / "broken", SHA, "linux")),
                    "a malformed report is a problem, not a crash")

    # A relative --output must not depend on the caller's working directory.
    import os
    with tempfile.TemporaryDirectory(prefix="native-gate-cwd-") as other, tempfile.TemporaryDirectory(prefix="native-gate-run-") as out:
        previous = os.getcwd()
        os.chdir(other)
        try:
            seen = []
            def fake_cell(executable, manager, report, log):
                seen.append(Path(report))
                return 1
            with patch.object(gate, "probe_interpreter", return_value=None), \
                 patch.object(gate, "checkout_identity", return_value=(SHA, False)), \
                 patch.object(gate, "run_cell", side_effect=fake_cell):
                gate.matrix({"3.11": "a", "3.13": "b"}, os.path.relpath(out, other))
        finally:
            os.chdir(previous)
        assert_true(seen and all(path.is_absolute() and str(path).startswith(str(Path(out).resolve())) for path in seen),
                    "cell report paths are absolute and under the requested output whatever the caller's cwd")

    # Real interpreter version strings, including pre-release and development builds, still map to a cell.
    for text, cell in (("3.13.1", "3.13"), ("3.11.9", "3.11"), ("3.13.0rc1", "3.13"), ("3.14.0a1+", "3.14")):
        assert_true(gate._cell({"python_version": text, "manager": "uv"})[0] == cell, f"{text} names its major.minor cell")
    assert_true(gate._cell({"python_version": sys.version.split()[0], "manager": "uv"})[0] ==
                "%d.%d" % sys.version_info[:2], "the running interpreter's own version string parses")

    # Path-bearing interpreters are made absolute; bare command names stay PATH lookups.
    assert_true(gate._interpreter("python3.13") == "python3.13", "a command name stays a PATH lookup")
    with tempfile.TemporaryDirectory(prefix="native-gate-interp-") as other:
        previous = os.getcwd()
        os.chdir(other)
        try:
            resolved = gate._interpreter(os.path.join(".", "venv311", "bin", "python"))
        finally:
            os.chdir(previous)
        assert_true(Path(resolved).is_absolute() and resolved.startswith(str(Path(other).resolve())),
                    "a relative interpreter path is resolved against the caller's cwd before probing and launching")

    # The validator's acceptance rule must stay identical to the one the producing test applies.
    proof = {"phase": "succeeded", "cleanup_confirmed": True, "callers_exited_at": 1,
             "stages": {"package": {"started_at": 2}}, "host_job": {"in_job": False}}
    variants = (proof, {**proof, "phase": "failed"}, {**proof, "cleanup_confirmed": False},
                {**proof, "callers_exited_at": 3}, {**proof, "host_job": {"in_job": True}}, {"phase": "succeeded"})
    for platform in gate.PLATFORMS:
        for state in variants:
            assert_true(gate._independent(platform, state) == _handoff_independence_verified(platform, False, state),
                        f"validator and producer agree on {platform} {state}")

    # Interpreter and checkout preconditions, and one full matrix with the cell command replaced.
    wrong = gate.probe_interpreter(sys.executable, "2.7")
    assert_true(wrong and "expected 2.7" in wrong, "an interpreter of the wrong version is refused")
    minor = "%d.%d" % sys.version_info[:2]
    assert_true(gate.probe_interpreter(sys.executable, minor) is None, "the running interpreter passes its own probe")
    with patch.object(gate, "probe_interpreter", return_value=None):
        with patch.object(gate, "checkout_identity", return_value=(SHA, True)):
            assert_true(gate.matrix({"3.11": "a", "3.13": "b"}, tempfile.gettempdir()) == 2,
                        "a dirty checkout stops the matrix before any cell")
        with tempfile.TemporaryDirectory(prefix="native-gate-run-") as out:
            platform, calls = sys.platform, []

            def fake_cell(executable, manager, report, log):
                calls.append((executable, manager))
                python = "3.11" if executable == "py311" else "3.13"
                Path(report).write_text(json.dumps(_report(gate, platform, python, manager)), encoding="utf-8")
                return 0

            with patch.object(gate, "checkout_identity", return_value=(SHA, False)), \
                 patch.object(gate, "run_cell", side_effect=fake_cell):
                assert_true(gate.matrix({"3.11": "py311", "3.13": "py313"}, out) == 0 and len(calls) == 6,
                            "a complete matrix of qualifying cells passes")
            assert_true([item[1] for item in calls[:3]] == list(gate.MANAGERS), "every manager runs per interpreter")
            with patch.object(gate, "checkout_identity", return_value=(SHA, False)), \
                 patch.object(gate, "run_cell", return_value=1):
                assert_true(gate.matrix({"3.11": "py311", "3.13": "py313"}, out) == 1,
                            "a failing cell fails the matrix and every cell is still attempted")
