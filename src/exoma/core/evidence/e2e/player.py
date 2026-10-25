"""The child process that drives the browser and reports JSONL on stdout.

Invocation: `python -m core.evidence.e2e.player` with a JSON document on stdin:
  {"scenario": {...resolved scenario...}, "config": {...request settings...},
   "artifacts_dir": "<dir>", "fake": null | "<mode>"}

Protocol: see `classify.py`. Every line on stdout is one JSON object; anything else
the supervisor counts as malformed. Typed values are never echoed back.

`fake` modes exist so the whole lifecycle — supervisor, classifier, normaliser, exit
codes — is testable without a browser: `pass`, `app_fail`, `harness_fail`, `scenario_fail`,
`unknown`, `launch_fail`, `malformed`, `crash`, `stall`, `heartbeat_forever`, `artifacts`.

A non-fake invocation drives a real browser through `browser.run_scenario` — Playwright
is imported there, lazily, so this module stays importable without the optional extra.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

from exoma.core.evidence.e2e.spec import ASSERTION_ACTIONS


def _emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _fake_run(mode: str, scenario: dict, artifacts_dir: str = "", display_scenario: dict | None = None) -> int:
    steps = scenario.get("steps") or []
    # What a step expected is reported in placeholder form, exactly as the real player does.
    shown_by_id = {
        str(item.get("id")): item
        for key in ("steps", "cleanup")
        for item in ((display_scenario or {}).get(key) or [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    if mode == "launch_fail":
        _emit({"type": "harness", "reason": "browser_missing", "detail": "fake: chromium binary absent"})
        _emit({"type": "result", "status": "aborted"})
        return 1
    if mode == "heartbeat_forever":
        while True:
            _emit({"type": "heartbeat"})
            time.sleep(0.05)
    url = "/"
    failed_once = False
    for index, step in enumerate(steps, start=1):
        action = step.get("action")
        cid = step.get("claim_id")
        provenance = (step.get("selector_provenance") or {}).get("type")
        ref = (step.get("selector_provenance") or {}).get("ref")
        base = {
            "type": "progress",
            "step": index,
            "step_id": step.get("id"),
            "action": action,
            "claim_id": cid,
            "duration_ms": 5,
            "selector_provenance": provenance,
            "page_stable": True,
        }
        if step.get("selector"):
            # The real player reports which candidate won and, for one the codebase named,
            # where it came from. The fake reports it too: the tagging pass downstream reads
            # this field, so a fake that omitted it would exercise the lifecycle around the
            # feature without ever exercising the feature.
            base["selection"] = {
                "candidate": 0,
                "match_counts": [1],
                "fallback_used": False,
                **({"source_ref": str(ref), "selector_keys": sorted(step["selector"])} if provenance == "source" and ref else {}),
            }
        if action == "goto":
            url = step.get("url") or "/"
        if mode == "stall" and index == 2:
            _emit({**base, "status": "passed", "url_after": url})
            time.sleep(3600)
        if mode == "crash" and index == 2:
            _emit({**base, "status": "passed", "url_after": url})
            sys.exit(3)
        if mode == "malformed" and index == 2:
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
        if mode in ("app_fail", "artifacts") and action in ASSERTION_ACTIONS and not failed_once:
            failed_once = True
            shown = shown_by_id.get(str(step.get("id"))) or step
            expected = {k: shown[k] for k in ("contains", "equals", "matches", "text", "selector") if k in shown}
            _emit(
                {
                    **base,
                    "status": "failed",
                    "expected": expected,
                    "actual": {"url": url + "?error=1"} if action == "expect_url" else {"found": False},
                    "url_after": url + "?error=1",
                    "error": {"kind": "assertion", "detail": "fake: assertion did not hold"},
                }
            )
            continue
        if mode in ("harness_fail", "scenario_fail", "unknown") and action in ("click", "fill", "expect_dom") and not failed_once:
            failed_once = True
            # harness_fail: the runtime broke on the step. scenario_fail: a guessed selector
            # matched nothing on a settled page. unknown: a grounded one, page unsettled.
            _emit(
                {
                    **base,
                    "status": "failed",
                    "selector_provenance": "source" if mode == "unknown" else "heuristic",
                    "page_stable": mode != "unknown",
                    "expected": {"selector": (shown_by_id.get(str(step.get("id"))) or step).get("selector")},
                    "actual": {"found": False},
                    "url_after": url,
                    "error": (
                        {"kind": "harness_error", "detail": "fake: the step's driver call failed"}
                        if mode == "harness_fail"
                        else {"kind": "selector_missing", "detail": "fake: no element matched"}
                    ),
                }
            )
            continue
        if failed_once and action in ASSERTION_ACTIONS and mode not in ("app_fail", "artifacts"):
            _emit({**base, "status": "skipped", "url_after": url})
            continue
        _emit({**base, "status": "passed", "url_after": url})
    _fake_cleanup(scenario)
    if mode == "artifacts" and artifacts_dir:
        # A page that echoed the typed value raw and URL-encoded, and a trace too large for
        # a small budget: the runner has to scrub the first and prune the second.
        typed = next((str(s.get("value")) for s in steps if s.get("action") == "fill"), "")
        out = Path(artifacts_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "step02.html").write_text(
            f"<p>Signed in as {typed}</p><a href='/me?u={quote(typed, safe='')}'>me</a>", encoding="utf-8"
        )
        (out / "trace.zip").write_bytes(b"\0" * (2 * 1024 * 1024))
        _emit({"type": "artifact", "kind": "html", "name": "step02.html", "step": 2, "bytes": None})
        _emit({"type": "artifact", "kind": "trace", "name": "trace.zip", "step": None, "bytes": 2 * 1024 * 1024})
    if mode == "third_party_noise":
        _emit(
            {
                "type": "observation",
                "kind": "http_5xx",
                "url": "https://analytics.example/collect",
                "status": 503,
                "detail": "fake: background beacon",
                "same_origin": False,
                "main_request": False,
            }
        )
    _emit({"type": "result", "status": "finished"})
    return 0


def _fake_cleanup(scenario: dict) -> None:
    """One `cleanup` event per cleanup step, as the real player sends them.

    The fake runs every test step it reaches (a failed mode only fails one), so every
    target counts as having run and each cleanup step passes. Without these events a fake
    run with `scenario.cleanup` could only ever end `not_run`, and the pass path for
    cleanup was untestable.
    """
    for index, step in enumerate(scenario.get("cleanup") or [], start=1):
        if not isinstance(step, dict):
            continue
        _emit(
            {
                "type": "cleanup",
                "step_id": str(step.get("id") or f"cleanup-{index}"),
                "cleans": str(step.get("cleans") or ""),
                "action": step.get("action"),
                "status": "passed",
                "expected": None,
                "actual": None,
                "error": None,
                "duration_ms": 5,
            }
        )


def _real_run(scenario: dict, config: dict, artifacts_dir: str, has_secrets: bool = False, display_scenario: dict | None = None) -> int:
    from exoma.core.evidence.e2e.browser import run_scenario

    return run_scenario(scenario, config, artifacts_dir, _emit, has_secrets=has_secrets, display_scenario=display_scenario)


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        _emit({"type": "harness", "reason": "harness_error", "detail": f"stdin is not JSON: {exc}"})
        _emit({"type": "result", "status": "aborted"})
        return 2
    scenario = payload.get("scenario") or {}
    config = payload.get("config") or {}
    fake = payload.get("fake")
    display = payload.get("display_scenario") if isinstance(payload.get("display_scenario"), dict) else None
    if fake:
        return _fake_run(str(fake), scenario, str(payload.get("artifacts_dir") or ""), display)
    return _real_run(scenario, config, str(payload.get("artifacts_dir") or ""), bool(payload.get("has_secrets")), display)


if __name__ == "__main__":
    sys.exit(main())
