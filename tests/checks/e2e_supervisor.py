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
    import os
    import subprocess
    from types import SimpleNamespace
    from unittest.mock import patch
    from exoma.core.evidence.e2e import supervisor
    original_popen = subprocess.Popen
    children = []
    def spawn(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child
    def interrupt(_):
        raise KeyboardInterrupt
    clock = SimpleNamespace(monotonic=time.monotonic, sleep=interrupt)
    with patch.object(supervisor, "time", clock), patch.object(supervisor.subprocess, "Popen", side_effect=spawn):
        try:
            supervisor.run_player([sys.executable, "-c", "import time; time.sleep(60)"],
                                  stdin_payload="{}", env=os.environ.copy(), cwd=None,
                                  idle_timeout_s=5, total_timeout_s=10)
            raise AssertionError("interrupt must propagate")
        except KeyboardInterrupt:
            pass
    assert_true(children and children[0].poll() is not None,
                "interrupt after player spawn must clean up its owned process")
    _measure_drain_after_fast_exit()


_BURST = 1500
_BURST_CHILD = (
    "import json, sys, threading\n"
    "def err():\n"
    "    for _ in range(2000):\n"
    "        sys.stderr.write('e' * 200 + '\\n')\n"
    "t = threading.Thread(target=err); t.start()\n"
    f"for i in range({_BURST}):\n"
    "    sys.stdout.write(json.dumps({'type': 'progress', 'step': i, 'pad': 'x' * 200}) + '\\n')\n"
    "    if i % 300 == 0:\n"
    "        sys.stdout.write(json.dumps({'type': 'network', 'chunk': i}) + '\\n')\n"
    "t.join()\n"
    "sys.stdout.write(json.dumps({'type': 'result', 'status': 'finished'}) + '\\n')\n"
    "sys.stdout.flush()\n"
)


def _measure_drain_after_fast_exit() -> None:
    """The shared drain budget against a burst, simultaneous stderr, and an exit right after.

    Measurement first (m8): this pins what the 2 s join budget retains today. A realistic
    callback must keep every event through the terminal result; a callback slower than the
    budget must never leave a run that looks complete while its tail is missing.
    """
    import os

    def busy(seconds):
        def callback(_event):
            end = time.perf_counter() + seconds
            while time.perf_counter() < end:
                pass
        return callback

    for label, per_event in (("realistic", 0.0003), ("slow", 0.002)):
        started = time.monotonic()
        out = run_player([sys.executable, "-c", _BURST_CHILD], stdin_payload="{}", env=os.environ.copy(),
                         cwd=None, idle_timeout_s=20, total_timeout_s=60, on_progress=busy(per_event),
                         poll_interval_s=0.05)
        snapshot = list(out["events"])  # what a consumer reading the result right away sees
        elapsed = round(time.monotonic() - started, 2)
        kinds = [event.get("type") for event in snapshot]
        assert_true(out["returncode"] == 0 and not out["malformed"] and not out["timed_out"],
                    f"{label}: the burst child exits cleanly: {out['returncode']} {out['malformed'][:2]}")
        assert_true(len(out["stderr_tail"]) <= 200, f"{label}: stderr stays bounded")
        if label == "realistic":
            assert_true(kinds[-1:] == ["result"] and kinds.count("progress") == _BURST
                        and kinds.count("network") == len(range(0, _BURST, 300)),
                        f"realistic: every event through the terminal result survives the drain "
                        f"(events={len(kinds)}, last={kinds[-1:]}, {elapsed}s)")
        elif kinds[-1:] != ["result"]:
            report = build_report(snapshot, _SCENARIO)
            assert_true(report["browser_verdict"] == "incomplete",
                        f"slow: a lost tail is classified incomplete, never complete: {report.get('reason')}")
    _measure_callback_spanning_exit()


_QUICK_CHILD = (
    "import json, sys\n"
    "for i in range(3):\n"
    "    sys.stdout.write(json.dumps({'type': 'progress', 'step': i}) + '\\n')\n"
    "sys.stdout.write(json.dumps({'type': 'result', 'status': 'finished'}) + '\\n')\n"
    "sys.stdout.flush()\n"
)


def _measure_callback_spanning_exit() -> None:
    """A callback still running when the child exits: the wait stays bounded, the run honest.

    The child writes a few lines (no pipe backpressure) and exits at once, while the first
    progress callback blocks past the shared 2 s drain budget. So the budget is really
    spent, unlike the burst above where backpressure keeps the child alive.
    """
    import os
    import threading

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking(event):
        if event.get("step") == 0:
            entered.set()
            # Released by the check below, never by the clock in the normal case: the
            # 30 s bound only keeps a broken supervisor from hanging the suite forever.
            release.wait(30)
            finished.set()

    try:
        out = run_player([sys.executable, "-c", _QUICK_CHILD], stdin_payload="{}", env=os.environ.copy(),
                         cwd=None, idle_timeout_s=60, total_timeout_s=60, on_progress=blocking,
                         poll_interval_s=0.05)
        snapshot = list(out["events"])
        # Ordering, not wall-clock: the run came back while its callback was still held.
        returned_while_blocked = entered.is_set() and not finished.is_set()
    finally:
        release.set()  # let the daemon reader finish instead of outliving the check
    kinds = [event.get("type") for event in snapshot]
    assert_true(out["returncode"] == 0 and not out["timed_out"], f"the quick child exits cleanly: {out['returncode']}")
    assert_true(returned_while_blocked,
                f"a blocked callback cannot hold the run past the drain budget "
                f"(entered={entered.is_set()}, finished={finished.is_set()})")
    assert_true(kinds[-1:] != ["result"],
                f"fixture: the drain budget expired before the reader reached the result: {kinds}")
    report = build_report(snapshot, _SCENARIO)
    assert_true(report["browser_verdict"] == "incomplete",
                f"a tail the drain did not reach is classified incomplete, never complete: {report.get('reason')}")
