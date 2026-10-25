"""Run the player as a child process and read its JSONL without trusting it.

Same shape as the provider adapters' `_popen_capture`: stdout and stderr drained on
threads, the main loop polls, and a timeout ends the WHOLE process tree — a browser
that outlives its player is the leak this exists to prevent. Two clocks instead of
one: `idle_timeout_s` fires when the player stops talking (a hung navigation, a modal
nobody dismisses), `total_timeout_s` bounds the run regardless.

The player's stdin receives the resolved scenario as JSON so real credential values
never sit in argv (visible to `ps`) or in a file.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque

from exoma.utils.osutil import hidden_run_kwargs, terminate_tree

MAX_EVENT_LINE = 64 * 1024
MAX_STDERR_LINES = 200
MAX_EVENTS = 5000


def run_player(
    args: list[str],
    *,
    stdin_payload: str,
    env: dict,
    cwd: str | None,
    idle_timeout_s: float,
    total_timeout_s: float,
    on_progress=None,
    poll_interval_s: float = 0.2,
) -> dict:
    """Returns {events, stderr_tail, returncode, duration_seconds, timed_out, stalled,
    truncated, malformed, launch_error}. Never raises for the child's behaviour."""
    started = time.monotonic()
    events: list[dict] = []
    malformed: list[str] = []
    stderr_tail: deque[str] = deque(maxlen=MAX_STDERR_LINES)
    last_line_at = [started]
    truncated = [False]

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=cwd,
            **hidden_run_kwargs(),
        )
    except (OSError, ValueError) as exc:
        return {
            "events": [],
            "stderr_tail": [],
            "returncode": None,
            "duration_seconds": 0.0,
            "timed_out": False,
            "stalled": False,
            "truncated": False,
            "malformed": [],
            "launch_error": f"{type(exc).__name__}: {exc}",
        }

    def feed_stdin() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(stdin_payload)
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    def drain_stdout() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            last_line_at[0] = time.monotonic()
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            if len(line) > MAX_EVENT_LINE:
                malformed.append(f"line over {MAX_EVENT_LINE} bytes dropped")
                continue
            if len(events) >= MAX_EVENTS:
                truncated[0] = True
                continue
            try:
                event = json.loads(line)
            except ValueError:
                malformed.append(line[:200])
                continue
            if not isinstance(event, dict):
                malformed.append(line[:200])
                continue
            events.append(event)
            if on_progress is not None and event.get("type") in ("progress", "heartbeat", "harness"):
                try:
                    on_progress(event)
                except Exception:
                    pass

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            stderr_tail.append(raw.rstrip("\r\n")[:2000])

    threads = [
        threading.Thread(target=feed_stdin, daemon=True),
        threading.Thread(target=drain_stdout, daemon=True),
        threading.Thread(target=drain_stderr, daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    stalled = False
    kill: dict | None = None
    while True:
        if proc.poll() is not None:
            break
        now = time.monotonic()
        if now - started > total_timeout_s:
            timed_out = True
            kill = terminate_tree(proc)
            break
        if now - last_line_at[0] > idle_timeout_s:
            stalled = True
            kill = terminate_tree(proc)
            break
        time.sleep(poll_interval_s)

    for thread in threads[1:]:
        thread.join(timeout=5)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        terminate_tree(proc)

    return {
        "events": events,
        "stderr_tail": list(stderr_tail),
        "returncode": proc.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "timed_out": timed_out,
        "stalled": stalled,
        "truncated": truncated[0],
        "malformed": malformed,
        "launch_error": None,
        "kill": kill,
    }
