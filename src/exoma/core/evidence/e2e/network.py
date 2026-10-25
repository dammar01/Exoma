"""Request metadata for page optimization (DEC-014): what a page loads, in what order, how
large and how slow — never what it says.

One record per request the page sends, reads included, kept apart from the request ledger
in `browser.py` (that one decides which writes a step was allowed to make; reads would
change its meaning). A record holds the sanitized URL (`redact.sanitize_endpoint`: no query,
no fragment, no credentials, id-shaped segments as `:id`), method, resource type, status,
timing and sizes. No body, header, cookie or query value is ever read.

Bounded per run as a rolling window: the newest `MAX_ROWS` records are kept, older ones are
dropped and counted. The summary is folded as requests finish, so it covers every request
of the run, dropped rows included — the totals an optimization reads stay whole even when
the per-row detail of the earliest requests does not.

Delivered at the end of the run as `network` events of at most `CHUNK_ROWS` rows and
`CHUNK_BYTES` bytes, plus one `network_summary`, to stay inside the supervisor's line limit.
"""

from __future__ import annotations

import heapq
import json
import re
from collections import deque

from exoma.core.evidence.e2e.redact import sanitize_endpoint

MAX_ROWS = 500
CHUNK_ROWS = 50
# Below the supervisor's 64 KB line limit with room for the event envelope, whatever the
# rows hold: a long or non-ASCII path must not cost the whole chunk.
CHUNK_BYTES = 48 * 1024
MAX_URL_CHARS = 500
SLOWEST = 10
REPEATED = 10
_SIZE_KEYS = (
    ("requestBodySize", "request_body"),
    ("requestHeadersSize", "request_headers"),
    ("responseBodySize", "response_body"),
    ("responseHeadersSize", "response_headers"),
)


_URL_IN_TEXT = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"]+")


def _url(raw: str) -> str:
    return sanitize_endpoint(raw)[:MAX_URL_CHARS]


def _failure_text(text: str) -> str:
    """An engine error string, with any URL in it sanitized like the rest of the record."""
    return _URL_IN_TEXT.sub(lambda match: sanitize_endpoint(match.group(0)), str(text))[:120]


def _ms(value) -> int | None:
    """A Playwright timing value, or None where it reports -1 (not available)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number < 0 else int(round(number))


class NetworkLedger:
    def __init__(self, clock, *, max_rows: int = MAX_ROWS) -> None:
        self.clock = clock
        self.started = clock()
        self.max_rows = max_rows
        self.rows: deque[dict] = deque(maxlen=max_rows)
        self.dropped = 0
        self._seq = 0
        self._open: dict[int, tuple[dict, float]] = {}
        self._total = 0
        self._failed = 0
        self._bytes = 0
        self._by_type: dict[str, dict] = {}
        self._by_status: dict[str, int] = {}
        self._slowest: list[tuple[int, int, str]] = []
        self._endpoints: dict[str, int] = {}

    def start(self, request, step_id: str | None) -> None:
        self._seq += 1
        try:
            method = str(request.method).upper()
            url = _url(str(request.url))
        except Exception:
            method, url = "?", "[unreadable url]"
        try:
            navigation = bool(request.is_navigation_request())
        except Exception:
            navigation = False
        try:
            redirected = getattr(request, "redirected_from", None)
            redirected_url = _url(str(redirected.url)) if redirected is not None else None
        except Exception:  # an odd redirect must not cost the request its record
            redirected_url = "[unreadable url]"
        record = {
            "seq": self._seq,
            "start_ms": int((self.clock() - self.started) * 1000),
            "method": method,
            "url": url,
            "resource_type": str(getattr(request, "resource_type", None) or ""),
            "navigation": navigation,
            "redirected_from": redirected_url,
            "step_id": step_id,
            "status": None,
            "from_service_worker": None,
            "failure": None,
            "duration_ms": None,
            "ttfb_ms": None,
            "sizes": None,
        }
        self._open[id(request)] = (record, self.clock())

    def response(self, response) -> None:
        opened = self._open.get(id(getattr(response, "request", None)))
        if opened is None:
            return
        record = opened[0]
        record["status"] = int(getattr(response, "status", 0) or 0)
        try:
            record["from_service_worker"] = bool(response.from_service_worker)
        except Exception:
            record["from_service_worker"] = None

    def finish(self, request, failure: str | None) -> None:
        opened = self._open.pop(id(request), None)
        if opened is None:
            return
        record, started = opened
        timing = getattr(request, "timing", None)
        if isinstance(timing, dict):
            record["ttfb_ms"] = _ms(timing.get("responseStart"))
            record["duration_ms"] = _ms(timing.get("responseEnd"))
        if record["duration_ms"] is None:
            record["duration_ms"] = int((self.clock() - started) * 1000)
        if failure:
            record["failure"] = _failure_text(failure)
        else:
            try:
                sizes = request.sizes()
                record["sizes"] = {short: int(sizes.get(key) or 0) for key, short in _SIZE_KEYS}
            except Exception:
                record["sizes"] = None
        self._fold(record)

    def finish_open(self, failure: str) -> None:
        """Requests still open when the run ends are closed as such, not dropped."""
        for key in list(self._open):
            record, started = self._open.pop(key)
            record["duration_ms"] = int((self.clock() - started) * 1000)
            record["failure"] = failure
            self._fold(record)

    def _fold(self, record: dict) -> None:
        if len(self.rows) == self.max_rows:
            self.dropped += 1
        self.rows.append(record)
        self._total += 1
        if record["failure"]:
            self._failed += 1
        body = int((record["sizes"] or {}).get("response_body") or 0)
        self._bytes += body
        kind = record["resource_type"] or "other"
        bucket = self._by_type.setdefault(kind, {"count": 0, "bytes": 0})
        bucket["count"] += 1
        bucket["bytes"] += body
        status = record["status"]
        status_class = f"{status // 100}xx" if status else ("failed" if record["failure"] else "none")
        self._by_status[status_class] = self._by_status.get(status_class, 0) + 1
        duration = record["duration_ms"] or 0
        item = (duration, -record["seq"], record["url"])
        if len(self._slowest) < SLOWEST:
            heapq.heappush(self._slowest, item)
        elif item > self._slowest[0]:
            heapq.heapreplace(self._slowest, item)
        endpoint = f"{record['method']} {record['url']}"
        self._endpoints[endpoint] = self._endpoints.get(endpoint, 0) + 1

    def summary(self) -> dict:
        repeated = sorted(
            ((count, endpoint) for endpoint, count in self._endpoints.items() if count > 1),
            key=lambda pair: (-pair[0], pair[1]),
        )[:REPEATED]
        return {
            "requests": self._total,
            "failed": self._failed,
            "response_bytes": self._bytes,
            "by_type": dict(sorted(self._by_type.items())),
            "by_status": dict(sorted(self._by_status.items())),
            "slowest": [
                {"url": url, "seq": -negative_seq, "duration_ms": duration}
                for duration, negative_seq, url in sorted(self._slowest, reverse=True)
            ],
            "repeated": [{"endpoint": endpoint, "count": count} for count, endpoint in repeated],
            "rows_kept": len(self.rows),
            "rows_dropped": self.dropped,
            "truncated": self.dropped > 0,
        }

    def events(self) -> list[dict]:
        """The run's network record as events: row chunks, then the summary."""
        events: list[dict] = []
        chunk: list[dict] = []
        size = 0
        for row in self.rows:
            row_size = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 2
            if chunk and (len(chunk) >= CHUNK_ROWS or size + row_size > CHUNK_BYTES):
                events.append({"type": "network", "rows": chunk})
                chunk, size = [], 0
            chunk.append(row)
            size += row_size
        if chunk:
            events.append({"type": "network", "rows": chunk})
        events.append({"type": "network_summary", **self.summary()})
        return events
