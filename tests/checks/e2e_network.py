"""Request metadata for page optimization (DEC-014): ordered, bounded, never a body or a value.

Driven with stand-in request objects shaped like Playwright's, so the ledger is checked
without a browser: the rolling window keeps the newest rows while the summary still counts
every request, no query value reaches an event, the events fit the supervisor's line limit,
and the next run against the same origin reports what changed.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from exoma.core.evidence.e2e.classify import build_report
from exoma.core.evidence.e2e.network import CHUNK_ROWS, NetworkLedger
from exoma.core.evidence.e2e.supervisor import MAX_EVENT_LINE
from tests.checks.support import assert_true

_SECRET = "tok_live_9f8e7d6c5b4a"


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class _Request:
    def __init__(self, url: str, *, kind: str = "fetch", method: str = "GET", body: int = 1000, end: float = 50.0, redirected_from=None) -> None:
        self.url = url
        self.method = method
        self.resource_type = kind
        self.redirected_from = redirected_from
        self.timing = {"startTime": 0, "responseStart": end / 2, "responseEnd": end}
        self._body = body

    def is_navigation_request(self) -> bool:
        return self.resource_type == "document"

    def sizes(self) -> dict:
        return {"requestBodySize": 0, "requestHeadersSize": 300, "responseBodySize": self._body, "responseHeadersSize": 200}


class _Response:
    def __init__(self, request, status: int = 200) -> None:
        self.request = request
        self.status = status
        self.from_service_worker = False


def _drive(ledger: NetworkLedger, request: _Request, *, status: int = 200, failure: str | None = None) -> None:
    ledger.start(request, "step-1")
    if failure is None:
        ledger.response(_Response(request, status))
    ledger.finish(request, failure)


def _check_window_and_totals() -> None:
    clock = _Clock()
    ledger = NetworkLedger(clock, max_rows=500)
    for n in range(1, 601):
        clock.now += 0.01
        _drive(ledger, _Request(f"https://app.test/api/items/{n}?token={_SECRET}", body=100, end=float(n)))
    summary = ledger.summary()
    seqs = [row["seq"] for row in ledger.rows]
    assert_true(
        len(ledger.rows) == 500 and seqs[0] == 101 and seqs[-1] == 600 and seqs == sorted(seqs),
        f"the newest 500 rows are kept, in order, the oldest 100 dropped: first={seqs[0]} last={seqs[-1]}",
    )
    assert_true(
        summary["requests"] == 600 and summary["rows_dropped"] == 100 and summary["truncated"]
        and summary["response_bytes"] == 600 * 100 and summary["by_type"]["fetch"]["count"] == 600,
        f"the summary counts every request, dropped rows included: {summary}",
    )
    assert_true(
        [item["duration_ms"] for item in summary["slowest"]][:3] == [600, 599, 598],
        f"the slowest requests are found across the whole run: {summary['slowest'][:3]}",
    )
    assert_true(
        summary["repeated"] and summary["repeated"][0] == {"endpoint": "GET https://app.test/api/items/:id", "count": 600},
        f"repeated calls are counted per sanitized endpoint: {summary['repeated'][:1]}",
    )

    events = ledger.events()
    lines = [json.dumps(event, ensure_ascii=False, default=str) for event in events]
    assert_true(
        len(events) == 500 // CHUNK_ROWS + 1 and events[-1]["type"] == "network_summary"
        and all(len(line.encode("utf-8")) < MAX_EVENT_LINE for line in lines),
        f"rows travel in chunks under the supervisor's line limit, then one summary: {[len(l) for l in lines]}",
    )
    assert_true(
        not any(_SECRET in line for line in lines) and "?" not in "".join(row["url"] for row in ledger.rows),
        "no query value reaches an event: URLs are sanitized to scheme, host and route",
    )
    report = build_report(events + [{"type": "result", "status": "finished"}], {"claims": [], "steps": []})
    network = report.get("network") or {}
    assert_true(
        len(network.get("rows") or []) == 500 and (network.get("summary") or {}).get("requests") == 600,
        f"the report carries the rows and the summary: {(network.get('summary') or {}).get('requests')}",
    )


def _check_failures_redirects_and_unfinished() -> None:
    clock = _Clock()
    ledger = NetworkLedger(clock)
    first = _Request("https://app.test/old", kind="document")
    _drive(ledger, first, status=301)
    _drive(ledger, _Request("https://app.test/new", kind="document", redirected_from=first))
    _drive(ledger, _Request("https://cdn.test/app.js", kind="script"), failure="net::ERR_FAILED")
    hanging = _Request("https://app.test/poll", kind="xhr")
    ledger.start(hanging, None)
    ledger.finish_open("no response before the run ended")
    rows = list(ledger.rows)
    assert_true(
        rows[1]["redirected_from"] == "https://app.test/old" and rows[1]["navigation"] is True,
        f"a redirect names where it came from: {rows[1]}",
    )
    assert_true(
        rows[2]["failure"] == "net::ERR_FAILED" and rows[2]["sizes"] is None
        and rows[3]["failure"] == "no response before the run ended",
        f"a failed request and one still open at the end are recorded as such: {rows[2:]}",
    )
    summary = ledger.summary()
    assert_true(
        summary["failed"] == 2 and summary["by_status"].get("3xx") == 1 and summary["by_status"].get("failed") == 2,
        f"failures and status classes are counted: {summary['by_status']}",
    )


def _check_event_bytes_and_failure_text() -> None:
    clock = _Clock()
    ledger = NetworkLedger(clock)
    long_segment = "é" * 1500  # non-ASCII, several bytes each once encoded
    for n in range(50):
        _drive(ledger, _Request(f"https://app.test/{long_segment}/{n}"))
    _drive(ledger, _Request("https://app.test/broken"), failure=f"net::ERR_FAILED at https://app.test/cb?token={_SECRET}")
    events = ledger.events()
    # Measured the way the player writes a line (player.py: ensure_ascii=False).
    sizes = [len(json.dumps(event, ensure_ascii=False, default=str).encode("utf-8")) for event in events]
    assert_true(
        all(size < MAX_EVENT_LINE for size in sizes) and sum(len(e.get("rows") or []) for e in events) == 51,
        f"chunks are bounded by bytes as well as rows, and no row is lost to the line limit: {sizes}",
    )
    assert_true(
        all(len(row["url"]) <= 500 for row in ledger.rows),
        "a URL is capped in length",
    )
    failure = list(ledger.rows)[-1]["failure"]
    assert_true(
        _SECRET not in failure and "https://app.test/cb" in failure,
        f"a URL inside an engine error is sanitized like the record's own URL: {failure}",
    )


def _check_next_run_reports_the_difference() -> None:
    from exoma.core.evidence.e2e.runner import _network_diff
    from exoma.core.evidence.runtime_io import write_quality_record

    root = Path(tempfile.mkdtemp(prefix="aw-e2e-network-"))
    try:
        (root / ".workflow" / "data").mkdir(parents=True)
        current = {"requests": 40, "failed": 0, "response_bytes": 9000, "by_type": {"script": {"count": 10, "bytes": 8000}, "image": {"count": 30, "bytes": 1000}}}
        assert_true(
            _network_diff(root, "http://app.test", "real", current) is None,
            "the first run against an origin has nothing to compare with",
        )
        write_quality_record(root, {"kind": "e2e_run", "origin": "http://app.test", "run_kind": "real",
                                    "recorded_at": "t1", "network": {"requests": 30, "failed": 1, "response_bytes": 5000,
                                                                     "by_type": {"script": {"count": 10, "bytes": 4000}, "font": {"count": 2, "bytes": 1000}}}})
        write_quality_record(root, {"kind": "e2e_run", "origin": "http://other.test", "run_kind": "real",
                                    "recorded_at": "t2", "network": {"requests": 1, "response_bytes": 1}})
        diff = _network_diff(root, "http://app.test", "real", current)
        assert_true(
            diff["previous_run_at"] == "t1" and diff["requests"] == 10 and diff["response_bytes"] == 4000 and diff["failed"] == -1,
            f"the difference is taken against the last run on the same origin: {diff}",
        )
        assert_true(
            diff["by_type"]["script"] == {"count": 0, "bytes": 4000}
            and diff["by_type"]["font"] == {"count": -2, "bytes": -1000}
            and diff["by_type"]["image"] == {"count": 30, "bytes": 1000},
            f"per-type changes include types that appeared or disappeared: {diff['by_type']}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_run_meta_keeps_preflight_network() -> None:
    """`meta.e2e.network` belongs to preflight (write hosts, resolver pins); the request
    summary must not overwrite it."""
    import time

    from exoma.core.evidence.e2e.runner import _record_run

    root = Path(tempfile.mkdtemp(prefix="aw-e2e-network-meta-"))
    try:
        (root / ".workflow" / "data").mkdir(parents=True)
        preflight = {"write_hosts": ["devbox.lan"], "pins": {"devbox.lan": "192.168.1.40"}, "hosts": []}
        e2e_meta = {"network": dict(preflight), "config": {"base_url": "http://app.test"}}
        ledger = NetworkLedger(_Clock())
        _drive(ledger, _Request("https://app.test/"))
        report = build_report(ledger.events() + [{"type": "result", "status": "finished"}], {"claims": [], "steps": []})
        _record_run(root, "sid", "task", e2e_meta, "pass", time.monotonic(), report, {"claims": [], "steps": []})
        assert_true(
            e2e_meta["network"] == preflight and (e2e_meta.get("page_requests") or {}).get("requests") == 1,
            f"preflight's network report survives and the request summary sits beside it: {e2e_meta}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_e2e_network() -> None:
    _check_run_meta_keeps_preflight_network()
    _check_window_and_totals()
    _check_failures_redirects_and_unfinished()
    _check_event_bytes_and_failure_text()
    _check_next_run_reports_the_difference()
