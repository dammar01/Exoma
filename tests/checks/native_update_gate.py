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
             "version_before": "1.1.1.post1", "version_after": "1.1.1.post2",
             "stages": {name: {"started_at": index + 2, "exit_code": 0}
                        for index, name in enumerate(("package", "probe", "install", "check"))}}
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


def _check_transition_cells(gate):
    strict = _report(gate, sys.platform, "3.11", "uv", transition="real_a_to_b", release_acceptance=True)
    contained = {**strict, "mode": "contained", "independent_worker_verified": False, "release_acceptance": False}
    for mode, report in (("strict", strict), ("contained", contained)):
        assert_true(gate.check_transition_report(report, SHA, sys.platform, mode, "3.11", "uv") == [],
                    mode + " cell qualifies within its explicit scope")
        for name in ("package", "probe", "install", "check"):
            for replacement in (None, {}, {"exit_code": 1}, {"exit_code": False}, {"exit_code": 0.0}):
                stages = dict(report["state"]["stages"])
                if replacement is None:
                    stages.pop(name)
                else:
                    stages[name] = replacement
                inconsistent = {**report, "state": {**report["state"], "stages": stages}}
                assert_true(gate.check_transition_report(inconsistent, SHA, sys.platform, mode, "3.11", "uv"),
                            f"{mode} refuses missing/failed/invalid {name} despite positive producer flags")
                if mode == "strict":
                    assert_true(gate.check_report(inconsistent, SHA, sys.platform),
                                "strict native matrix also checks stage results")
    for changes in ({"commit": "b" * 40}, {"commit_dirty": True}, {"mode": "strict"},
                    {"independent_worker_verified": True}, {"release_acceptance": True},
                    {"no_op_verified": False}, {"actual_version_transition": False}, {"passed": False},
                    {"manual_recovery": True}, {"transition": None}, {"manager": "pip"},
                    {"python_version": "3.13.1"}, {"state": {"phase": "cleanup_pending"}}):
        assert_true(gate.check_transition_report({**contained, **changes}, SHA, sys.platform,
                                                "contained", "3.11", "uv"),
                    "contained evidence refuses wrong/missing proof: " + str(changes))
    assert_true(gate.check_report(contained, SHA, sys.platform), "contained CI success is never strict acceptance")
    assert_true(gate.check_transition_report(contained, SHA, sys.platform, "strict", "3.11", "uv"),
                "strict CI cannot be satisfied by contained evidence")
    assert_true(gate.check_transition_report({**contained, "state": {**contained["state"],
                    "version_after": contained["state"]["version_before"]}}, SHA, sys.platform,
                    "contained", "3.11", "uv"), "a no-op cannot be labelled as a real transition")
    with tempfile.TemporaryDirectory(prefix="transition-cell-") as tmp:
        path = Path(tmp) / "report.json"
        args = ["check-cell", str(path), "--commit", SHA, "--mode", "contained", "--python", "3.11", "--manager", "uv"]
        assert_true(gate.main(args) == 1, "missing report fails the CI cell")
        path.write_text("{broken", encoding="utf-8")
        assert_true(gate.main(args) == 1, "unreadable report fails the CI cell")
        path.write_text(json.dumps(contained), encoding="utf-8")
        assert_true(gate.main(args) == 0, "CLI accepts correctly scoped contained coverage")


def _test_native_update_gate():
    gate = _load()
    _check_transition_cells(gate)
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

    # Assert independent outcomes, not only parity: two predicates can agree and both be wrong.
    proof = {"phase": "succeeded", "cleanup_confirmed": True, "callers_exited_at": 1,
             "stages": {name: {"started_at": index + 2, "exit_code": 0}
                        for index, name in enumerate(("package", "probe", "install", "check"))},
             "host_job": {"in_job": False}}
    # Each row names the expected POSIX and Windows result explicitly.
    variants = [
        ("finite integer timestamps", proof, True, True),
        ("finite float timestamps", {**proof, "callers_exited_at": 1.5,
                                     "stages": {**proof["stages"], "package": {"started_at": 2.5, "exit_code": 0}}}, True, True),
        ("equal timestamps", {**proof, "callers_exited_at": 2}, True, True),
        ("failed phase", {**proof, "phase": "failed"}, False, False),
        ("pending cleanup", {**proof, "phase": "cleanup_pending"}, False, False),
        ("unconfirmed cleanup", {**proof, "cleanup_confirmed": False}, False, False),
        ("numeric cleanup flag", {**proof, "cleanup_confirmed": 1}, False, False),
        ("package before caller exit", {**proof, "callers_exited_at": 3}, False, False),
        ("inherited Windows job", {**proof, "host_job": {"in_job": True}}, True, False),
        ("numeric job flag", {**proof, "host_job": {"in_job": 0}}, True, False),
        ("null state", None, False, False),
        ("list state", [], False, False),
        ("missing evidence", {"phase": "succeeded"}, False, False),
        ("missing caller exit", {key: value for key, value in proof.items()
                                 if key != "callers_exited_at"}, False, False),
        ("missing package start", {**proof, "stages": {"package": {}}}, False, False),
    ]
    for label, value in (("null", None), ("string", "1"), ("true", True), ("false", False),
                         ("NaN", float("nan")), ("infinity", float("inf")),
                         ("negative infinity", float("-inf")), ("oversized integer", 10 ** 400)):
        variants.extend((
            (label + " caller exit", {**proof, "callers_exited_at": value}, False, False),
            (label + " package start", {**proof, "stages": {"package": {"started_at": value}}}, False, False),
        ))
    variants.append(("both timestamps infinite", {**proof, "callers_exited_at": float("inf"),
                                                  "stages": {"package": {"started_at": float("inf")}}}, False, False))
    for label, value in (("null", None), ("boolean", True), ("string", "bad"), ("list", [])):
        variants.extend((
            (label + " stages", {**proof, "stages": value}, False, False),
            (label + " package", {**proof, "stages": {"package": value}}, False, False),
            (label + " host job", {**proof, "host_job": value}, True, False),
        ))
    for platform in gate.PLATFORMS:
        for label, state, posix, windows in variants:
            expected = windows if platform == "win32" else posix
            assert_true(_handoff_independence_verified(platform, False, state) is expected,
                        f"producer: {platform} {label} must yield {expected}")
            assert_true(gate._independent(platform, state) is expected,
                        f"collector: {platform} {label} must yield {expected}")
            report = _report(gate, platform, "3.11", "uv", state=state)
            assert_true((not gate.check_report(report, SHA, platform)) is expected,
                        f"receipt: {platform} {label} must qualify only with valid evidence")
            assert_true(_handoff_independence_verified(platform, True, state) is False,
                        f"contained execution never proves independence: {platform} {label}")
    assert_true(_handoff_independence_verified("unknown", False, proof) is False,
                "an unsupported platform never attests native independence")

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
