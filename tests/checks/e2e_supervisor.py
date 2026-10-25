"""The player supervisor against a real child process: protocol, timeouts, termination."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from exoma.core.evidence.e2e.classify import build_report
from exoma.core.evidence.e2e.supervisor import run_player
from exoma.utils.osutil import process_alive
from tests.checks.support import assert_true

_ROOT = Path(__file__).resolve().parents[2]
_SCENARIO = {
    "version": 1,
    "claims": [{"id": "login", "severity": "blocking"}],
    "steps": [
        {"action": "goto", "url": "/login"},
        {"action": "fill", "selector": {"role": "textbox", "name": "Email"}, "value": "secret-value-1"},
        {"action": "click", "selector": {"role": "button", "name": "Go"}, "selector_provenance": {"type": "heuristic"}},
        {"action": "expect_url", "contains": "/dashboard", "claim_id": "login"},
    ],
}


def _run(fake: str, *, idle: float = 5.0, total: float = 10.0, beats: list | None = None, config: dict | None = None) -> dict:
    payload = json.dumps({"scenario": _SCENARIO, "config": config or {}, "artifacts_dir": "", "fake": fake})
    import os

    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE")}
    env["PYTHONPATH"] = str(_ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    return run_player(
        [sys.executable, "-m", "exoma.core.evidence.e2e.player"],
        stdin_payload=payload,
        env=env,
        cwd=str(_ROOT),
        idle_timeout_s=idle,
        total_timeout_s=total,
        on_progress=(beats.append if beats is not None else None),
        poll_interval_s=0.05,
    )


def _test_e2e_supervisor() -> None:
    # A clean run: every line parsed, heartbeat seen, result finished, exit 0.
    beats: list = []
    out = _run("pass", beats=beats)
    assert_true(out["returncode"] == 0 and not out["malformed"] and not out["timed_out"], f"pass run is clean: {out}")
    kinds = [e["type"] for e in out["events"]]
    assert_true(kinds.count("progress") == 4 and kinds[-1] == "result", f"protocol: 4 progress + result: {kinds}")
    assert_true(len(beats) >= 4, "progress events reach the heartbeat callback")
    assert_true(build_report(out["events"], _SCENARIO)["browser_verdict"] == "pass", "the fake pass run passes")
    assert_true(all("secret-value-1" not in json.dumps(e) for e in out["events"]), "typed values never come back on the wire")

    # Malformed lines are counted, not fatal; the rest of the run still parses.
    out = _run("malformed")
    assert_true(len(out["malformed"]) == 1 and out["returncode"] == 0, f"one bad line is recorded, run continues: {out['malformed']}")
    assert_true(build_report(out["events"], _SCENARIO, run_meta={"malformed": True})["reason"] == "harness_error", "malformed output makes the run incomplete")

    # A crash: no result event, non-zero exit → incomplete.
    out = _run("crash")
    assert_true(out["returncode"] == 3 and [e["type"] for e in out["events"]][-1] != "result", "a crashed player leaves no result event")
    assert_true(build_report(out["events"], _SCENARIO)["browser_verdict"] == "incomplete", "a crash is incomplete")

    # A launch failure reported by the player itself.
    out = _run("launch_fail")
    assert_true(build_report(out["events"], _SCENARIO)["reason"] == "browser_missing", "the player's harness reason survives the wire")

    # Idle timeout: the player goes quiet after step 2; the tree is terminated.
    started = time.monotonic()
    out = _run("stall", idle=0.6, total=30)
    assert_true(out["stalled"] and not out["timed_out"], f"silence trips the idle clock: {out['stalled']} {out['timed_out']}")
    assert_true(time.monotonic() - started < 10, "the idle clock fires promptly")
    assert_true(build_report(out["events"], _SCENARIO, run_meta={"stalled": True})["reason"] == "stuck", "a stall is reported as stuck")

    # Total timeout: a player that keeps sending heartbeats is still bounded.
    started = time.monotonic()
    out = _run("heartbeat_forever", idle=5, total=0.8)
    assert_true(out["timed_out"] and not out["stalled"], f"heartbeats do not extend the total budget: {out['timed_out']}")
    assert_true(time.monotonic() - started < 10, "the total clock fires promptly")
    assert_true(out["kill"] is not None and out["kill"].get("ok"), f"the process tree was terminated: {out['kill']}")

    # An unlaunchable command is a launch error, not an exception.
    out = run_player(["definitely-not-a-real-binary-xyz"], stdin_payload="{}", env={}, cwd=None, idle_timeout_s=1, total_timeout_s=1)
    assert_true(out["launch_error"] and out["events"] == [], f"a missing binary is reported: {out['launch_error']}")
    assert_true(build_report([{"type": "harness", "reason": "launch_failed", "detail": out["launch_error"]}], _SCENARIO)["reason"] == "launch_failed", "launch errors map to launch_failed")

    # The real player, reached through the same child protocol: a browser it cannot launch
    # ends the run aborted with a reason, never with a verdict. An unknown browser name is
    # refused before Playwright is imported, so this holds with or without the extra.
    out = _run("", config={"browser": "netscape"})
    report = build_report(out["events"], _SCENARIO)
    assert_true(out["returncode"] == 2 and not out["malformed"], f"the real player speaks the protocol: {out}")
    assert_true(report["reason"] == "launch_failed" and report["browser_verdict"] == "incomplete", f"an unlaunchable browser is incomplete: {report['reason']}")
