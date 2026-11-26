"""Measure one installed Exoma arm on isolated two-round pricing fixtures.

Run with the wheel's python -I. Replay substitutes only the reviewer. Live uses
the configured Codex provider. Each case writes its own machine-readable report.
"""
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/verify_followup"
CASES = ("replacement", "persistent", "deleted", "unexercised", "repair", "clean")
COMMAND = "python -B check_quantity.py"
A = "Negative quantity(-2) returns -2 at pricing.py:3; expected zero."
B = "total(3, 4) returns 7 at pricing.py:8; expected 12."
TAG = "severity: high | origin: introduced | scope_relation: in_scope"
SESSION = "review"
REVIEW = """[VERIFICATION]
verdict: {verdict}
blocking_findings:
- {finding}
escalations:
- none
notes:
- none
checks_run:
- inspected pricing and its callers
not_verified:
- none
confidence: high
[DIGEST]
summary: inspected current change
"""


class Replay:
    def __init__(self):
        self.report = ""

    def run(self, prompt, session, model=None, work_dir=None):
        return {"ok": True, "content": self.report,
                "meta": {"provider_session_id": "fixture-reviewer", "simulated": True}}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# The experiment owns these sources and both variants. Observe the input/output
# of quantity and the actual assertion line even on the successful run.
PROBE = '''import json
import runpy
import sys
from pathlib import Path
import pricing

target = Path("replacement.py").resolve()
quantity = pricing.quantity.__code__
seen = {"calls": [], "lines": [], "assertion_failures": []}

def trace(frame, event, arg):
    if frame.f_code is quantity and event == "return":
        seen["calls"].append([frame.f_locals["value"], arg])
    if frame.f_code.co_filename == str(target):
        if event == "line":
            seen["lines"].append(frame.f_lineno)
        elif event == "exception" and arg[0] is AssertionError:
            seen["assertion_failures"].append(frame.f_lineno)
    return trace

sys.settrace(trace)
try:
    runpy.run_path(str(target))
finally:
    sys.settrace(None)
    print(json.dumps(seen))
'''
REPLACEMENTS = {
    "moved": "assert quantity(-2) == 0",
    "print_only": "print(quantity(-2))",
    "skipped": "if False:\n    assert quantity(-2) == 0",
    "wrong_trigger": "assert quantity(-3) == 0",
    "unrelated_failure": "if quantity(-2) < 0:\n    raise RuntimeError('unrelated setup error')\nassert quantity(-2) == 0",
}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _replacement_arm(root, variant, pricing, replacement):
    from exoma.core.evidence import verify_tests
    from exoma.core.runtime.state import ensure_workflow_workspace
    from exoma.core.workspace.workspace_paths import workflow_paths

    root.mkdir()
    ensure_workflow_workspace(root)
    config_path = workflow_paths(root)["config"]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["commands"]["verify_test_commands"] = [COMMAND]
    write_json(config_path, config)
    for name, data in (("pricing.py", pricing), ("replacement.py", replacement), ("check_quantity.py", PROBE.encode())):
        (root / name).write_bytes(data)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    identity = lambda: verify_tests._identity(root)
    before = identity()
    outcome, = verify_tests.run_tests(root, {"commands": [COMMAND]}, identity=identity, session_id=SESSION)
    log = Path(outcome["log_ref"]).read_bytes() if outcome.get("log_ref") else b""
    return {"variant": variant, "pricing_sha256": _sha(pricing), "assertion_sha256": _sha(replacement),
            "probe_sha256": _sha(PROBE.encode()), "input": before, "observed_input": outcome.get("fingerprint"),
            "command": outcome["command"], "reused": outcome.get("reused", False), "status": outcome["status"],
            "exit": outcome.get("exit_code"), "diagnostic": log.decode("utf-8"), "log_sha256": _sha(log)}


def _supports_replacement(arms, sources, replacement):
    from ast import Assert, parse, walk

    assertions = [node.lineno for node in walk(parse(replacement)) if isinstance(node, Assert)]
    if len(assertions) != 1 or len(arms) != 2:
        return False
    line, = assertions
    for arm, variant, expected_exit, value in zip(arms, ("broken", "fixed"), (1, 0), (-2, 0)):
        if (arm["variant"] != variant or arm["pricing_sha256"] != _sha(sources[variant])
                or arm["assertion_sha256"] != _sha(replacement) or arm["probe_sha256"] != _sha(PROBE.encode())
                or not arm["input"] or arm["observed_input"] != arm["input"] or arm["reused"]
                or arm["command"] != COMMAND or arm["exit"] != expected_exit
                or arm["status"] != ("failed" if expected_exit else "passed")
                or not arm["diagnostic"] or _sha(arm["diagnostic"].encode()) != arm["log_sha256"]):
            return False
        try:
            receipt = json.loads(arm["diagnostic"])
            observed = json.loads(receipt["stdout"].splitlines()[-1])
            supported = (receipt["exit_code"] == expected_exit and receipt["status"] == arm["status"]
                         and observed["calls"] == [[-2, value]] and line in observed["lines"]
                         and observed["assertion_failures"] == ([line] if expected_exit else []))
        except (ValueError, IndexError, KeyError, TypeError, AttributeError):
            return False
        if not supported:
            return False
    return arms[0]["input"] != arms[1]["input"]


def replacement_controls(root):
    fixed = (FIXTURE / "pricing.py").read_bytes()
    broken = fixed.replace(b"max(0, value)", b"value")
    assert broken != fixed
    sources = {"broken": broken, "fixed": fixed}
    reports = {}
    for name, check in REPLACEMENTS.items():
        replacement = ("from pricing import quantity\n\n# Moved regression check\n" + check + "\n").encode()
        arms = [_replacement_arm(root / (name + "-" + variant), variant, source, replacement)
                for variant, source in sources.items()]
        reports[name] = {"supported": _supports_replacement(arms, sources, replacement), "arms": arms}
    # Exercise receipt validation independently of which command exited zero.
    for name, change in (("reused", {"reused": True}), ("missing", {"diagnostic": ""}),
                         ("stale", {"observed_input": "older-input"})):
        arms = [dict(arm) for arm in reports["moved"]["arms"]]
        arms[1].update(change)
        replacement = (root / "moved-fixed/replacement.py").read_bytes()
        reports[name] = {"supported": _supports_replacement(arms, sources, replacement), "altered_receipt": change}
    return reports


def drive(root, case, mode, model):
    from exoma import cli
    from exoma.core.runtime import execution
    from exoma.core.runtime.state import ensure_workflow_workspace
    from exoma.core.provider.executor import Executor
    from exoma.core.workspace.workspace_paths import workflow_paths

    replay = Replay()
    if mode == "replay":
        cli.EXECUTOR = Executor(adapter=replay)
    ensure_workflow_workspace(root)
    paths = workflow_paths(root, SESSION)
    config = json.loads(paths["config"].read_text(encoding="utf-8"))
    config["commands"].update(verify_mode="delegated", verify_test_commands=[COMMAND])
    write_json(paths["config"], config)
    write_json(root / ".workflow/second_agent.json",
               {"provider": "codex", "provider_command": "codex", "default_model": model})
    for file in FIXTURE.glob("*.py"):
        shutil.copy(file, root / file.name)
    (root / ".gitignore").write_text(".workflow/\n__pycache__/\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Fixture", "-c",
                 "user.email=fixture@example.invalid", "commit", "-qm", "fixture"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    # Start both arms from the same synthetic, already-implemented execution.
    execution_id = uuid.uuid4().hex
    selection = {"version": 1, "execution_id": execution_id, "tier": "targeted",
                 "reason": "exercise the quantity behavior", "obligations": [
                     {"id": "quantity", "due": "verify", "behavior": "negative quantity becomes zero",
                      "commands": [COMMAND], "assertion": "check_quantity.py:3"}]}
    write_json(paths["runtime_dir"] / "execution.json",
               {"execution_id": execution_id, "status": "implemented", "selection": selection})
    source = (root / "pricing.py").read_text(encoding="utf-8")
    rows = []
    for index in (1, 2):
        broken = case != "clean" and (index == 1 or case in ("persistent", "deleted", "unexercised"))
        code = source.replace("max(0, value)", "value") if broken else source
        if case == "replacement" and index == 2:
            code = code.replace("price * units", "price + units")
        if case == "persistent" and index == 2:
            code = "\n" + code
        if case == "deleted" and index == 2:
            (root / "check_quantity.py").write_text("from pricing import quantity\n", encoding="utf-8")
        if case == "unexercised" and index == 2:
            (root / "check_quantity.py").write_text(
                "from pricing import quantity\n\n# replacement\nprint('check complete')\n", encoding="utf-8")
            selection["obligations"][0]["equivalent_assertion"] = {
                "reference": "check_quantity.py:4", "reason": "replacement quantity check"}
        (root / "pricing.py").write_text(code, encoding="utf-8")
        prepared = cli.run("verify-prepare", "", SESSION, str(root))["meta"]
        write_json(Path(prepared["request"]["path"]),
                   {"commands": [COMMAND], "selection": selection, "selection_input": prepared["selection_input"]})
        finding = A if broken and (index == 1 or case not in ("deleted", "unexercised")) else None
        if case == "persistent" and index == 2:
            finding = "Negative inputs escape the clamp at pricing.py:4."
        if case == "replacement" and index == 2:
            finding = B
        rows_in = prepared.get("verify_followup", {}).get("open", [])
        follow = []
        for old in rows_in:
            follow.append({"id": old["id"], "status": "present" if case == "persistent" else "resolved",
                           "diagnosis": "Observed pricing and current assertion for the original negative input",
                           "strategy": "Inspect changed implementation and rerun the original quantity check",
                           "evidence": "check_quantity.py:3", "obligation": "quantity",
                           "blocker": 1 if case == "persistent" else None})
            if case == "unexercised":
                follow[-1].update(evidence="check_quantity.py:4", equivalence="Checks the same input.")
        if "verify_followup" in prepared and finding == A and not follow:
            follow.append({"blocker": 1, "obligation": "quantity"})
        section = "[FOLLOWUP_V1]\n" + "\n".join(json.dumps(row) for row in follow) + "\n" if follow else ""
        replay.report = section + REVIEW.format(verdict="NEEDS FIX" if finding else "DONE",
                                                finding=f"{TAG} — {finding}" if finding else "none")
        usage_path = paths["usage_stream"]
        offset = usage_path.stat().st_size if usage_path.exists() else 0
        started = time.monotonic()
        result = cli.run("verify", "Review the current pricing changes and affected behaviors.",
                         SESSION, str(root), model=model)
        meta = result.get("meta", {})
        diagnostics = []
        for outcome in meta.get("runtime_tests", {}).get("outcomes", []):
            log = Path(outcome["log_ref"]) if outcome.get("log_ref") else None
            record = json.loads(log.read_text(encoding="utf-8")) if log and log.is_file() else {}
            diagnostics.append({"command": outcome["command"], "status": outcome["status"],
                                "exit": outcome.get("exit_code"), "stdout": record.get("stdout"),
                                "stderr": record.get("stderr"), "fingerprint": outcome.get("fingerprint")})
        usage = []
        if usage_path.exists():
            with usage_path.open("rb") as stream:
                stream.seek(offset)
                for line in stream:
                    call = json.loads(line)
                    usage.append({k: call.get(k) for k in ("token_source", "actual_input_tokens",
                                 "actual_output_tokens", "actual_cached_input_tokens", "actual_reasoning_tokens")})
        rows.append({"round": index, "input": prepared["selection_input"], "prior": rows_in,
                     "seconds": round(time.monotonic()-started, 2), "ok": result["ok"],
                     "verdict": meta.get("verdict"), "review": result.get("content"),
                     "followup": meta.get("verify_followup"), "diagnostics": diagnostics,
                     "reviewer_blockers": meta.get("review_observations", {}).get("blockers"),
                     "error_type": meta.get("error_type"), "usage": usage,
                     "provider_version": meta.get("provider_version")})
        if not result["ok"]:
            break
    # Receipts embed transient absolute paths. Keep diagnostic contents by value.
    return json.loads(json.dumps(rows, ensure_ascii=False).replace(
        json.dumps(str(root))[1:-1], "<fixture>").replace(str(root.as_posix()), "<fixture>"))


def verified_package(wheel):
    import exoma

    package = Path(exoma.__file__).resolve().parent
    installed = {p.relative_to(package.parent).as_posix(): p for p in package.rglob("*")
                 if p.is_file() and "__pycache__" not in p.relative_to(package).parts}
    with zipfile.ZipFile(wheel) as archive:
        expected = {name for name in archive.namelist() if name.startswith("exoma/") and not name.endswith("/")}
        differences = set(installed) ^ expected
        differences.update(name for name in expected & installed.keys()
                           if installed[name].read_bytes() != archive.read(name))
        if not expected or differences:
            raise ValueError(f"Installed Exoma differs from {wheel.name}: {sorted(differences)[:5]}")
        dist = metadata.distribution("exoma")
        (manifest,) = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if dist.locate_file(manifest).read_bytes() != archive.read(manifest):
            raise ValueError(f"Installed metadata differs from {wheel.name}")
    return {"distribution_version": dist.version, "package_files_verified": len(expected),
            "package": str(Path(exoma.__file__).resolve().relative_to(Path(sys.prefix)))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("replay", "live"), required=True)
    parser.add_argument("--case", choices=CASES + ("all", "assertion-controls"), default="all")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--model", default="gpt-6.2-sol")
    args = parser.parse_args()
    output = {"mode": args.mode, "model": args.model if args.mode == "live" else "scripted",
              "revision": args.revision, "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
              **verified_package(args.wheel),
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "cases": {}}
    if args.case == "assertion-controls":
        with tempfile.TemporaryDirectory(prefix="pricing-assertions-") as temp:
            os.environ["EXOMA_HOME"] = str(Path(temp) / "store")
            output["assertion_controls"] = replacement_controls(Path(temp))
        write_json(args.report, output)
        return 0 if all(r["supported"] == (name == "moved") for name, r in output["assertion_controls"].items()) else 1
    for case in CASES if args.case == "all" else (args.case,):
        with tempfile.TemporaryDirectory(prefix="pricing-eval-") as temp:
            root = Path(temp) / "project"
            root.mkdir()
            os.environ["EXOMA_HOME"] = str(Path(temp) / "store")
            os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
            output["cases"][case] = drive(root, case, args.mode, args.model)
            write_json(args.report, output)
    return 0 if all(len(rows) == 2 and all(r["ok"] and r["diagnostics"] for r in rows)
                    for rows in output["cases"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

