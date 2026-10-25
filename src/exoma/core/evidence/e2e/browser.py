"""The real browser player: one resolved scenario through Playwright, reported as the
`classify.py` event protocol.

Split in two on purpose. `run_scenario` imports Playwright lazily (an optional extra —
tests/checks/deps.py), launches, and always closes. `Session` drives a page that is
already open, so the step semantics — selector fallback, polling assertions, origin
guard, observers — are checked against a stand-in page without a browser.

What never leaves this process: typed values (fill/select/press echo no input), full
DOM, request or response bodies, queries, cookies. What does leave is scrubbed again by
the runner before anything is stored or prompted.

A write action runs once. A timeout after the click was dispatched may still mean the
server received the request, so nothing here re-sends it; the step fails with evidence.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from exoma.core.evidence.e2e.preflight import is_local_dev_host, same_origin
from exoma.core.evidence.e2e.network import NetworkLedger
from exoma.core.evidence.e2e.redact import sanitize_endpoint
from exoma.core.evidence.e2e.request import blocked_request_target, destructive_request_target, read_only_request_target
from exoma.core.evidence.e2e.spec import DESTRUCTIVE_METHODS, SAFE_METHODS, SELECTOR_RANK, WRITE_METHODS, carries_method_override, e2e_css, navigation_error, request_path_matches, resolve_upload, selector_rank, step_selectors

HEARTBEAT_EVERY_S = 2.0
POLL_S = 0.1
DETAIL_CHARS = 300
STABLE_WAIT_MS = 2000
_BROWSERS = ("chromium", "firefox", "webkit")
_ELEMENT_ACTIONS = frozenset({"click", "fill", "select", "press", "upload"})
# A 307/308 repeats the method and the body at the new address; the others turn a POST
# into a GET, which is a navigation question rather than a write question.
_BODY_KEEPING_REDIRECTS = frozenset({307, 308})
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
MAX_WRITE_REDIRECTS = 5
_DEFAULT_PORTS = {"http": 80, "https": 443}
_SLOW_MO_MAX_MS = 5000


def _slow_mo_ms(config: dict) -> int:
    """settings.slow_mo_ms clamped to 0.._SLOW_MO_MAX_MS; an unreadable value runs at full speed."""
    try:
        return min(_SLOW_MO_MAX_MS, max(0, int(config.get("slow_mo_ms") or 0)))
    except (TypeError, ValueError):
        return 0
_SELECTOR_ERRORS = frozenset({"selector_missing", "selector_ambiguous", "not_visible"})
_STACK_URL = re.compile(r"https?://[^\s()]+")
MAX_HTML_BYTES = 512 * 1024
SECRET_CAPTURE_NOTE = "the scenario resolved ${ENV} values and a rendered page may show them"

# Before any capture, typed values leave the DOM: the serialized HTML and the screenshot
# would otherwise carry what `fill` typed. Hidden inputs go too (CSRF tokens).
_CLEAR_INPUTS_JS = r"""() => {
  document.querySelectorAll("input,textarea").forEach((el) => {
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (["submit", "button", "checkbox", "radio", "image", "reset"].includes(type)) return;
    el.value = "";
    el.removeAttribute("value");
    if (el.tagName === "TEXTAREA") el.textContent = "";
  });
}"""

# Bounded DOM probe (plan §11): visible elements only, text cut, no input values, capped
# at the request's settings.probe_max_elements across all buckets.
_PROBE_JS = r"""(limit) => {
  const cut = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 80);
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const out = {url: location.href, title: cut(document.title), headings: [], buttons: [], inputs: [], links: [], truncated: false};
  let used = 0;
  const take = (bucket, el, item) => {
    if (!visible(el)) return;
    if (used >= limit) { out.truncated = true; return; }
    used += 1;
    out[bucket].push(item);
  };
  document.querySelectorAll("h1,h2,h3").forEach((el) => take("headings", el, cut(el.innerText)));
  document.querySelectorAll("button,[role=button],input[type=submit],input[type=button]").forEach((el) =>
    take("buttons", el, {role: "button", name: cut(el.getAttribute("aria-label") || el.innerText || el.value), e2e: el.getAttribute("data-e2e")}));
  document.querySelectorAll("input:not([type=hidden]):not([type=submit]):not([type=button]),textarea,select").forEach((el) =>
    take("inputs", el, {
      role: el.tagName === "SELECT" ? "combobox" : "textbox",
      label: cut((el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute("aria-label") || el.getAttribute("placeholder")),
      type: el.getAttribute("type") || el.tagName.toLowerCase(),
      e2e: el.getAttribute("data-e2e"),
    }));
  document.querySelectorAll("a[href]").forEach((el) =>
    take("links", el, {name: cut(el.innerText || el.getAttribute("aria-label")), href: cut(el.getAttribute("href")), e2e: el.getAttribute("data-e2e")}));
  return out;
}"""


def _cut(text: object, limit: int = DETAIL_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme else url[:80]


def _endpoint(url: str) -> tuple | None:
    """(scheme, host, port, path) with the default port filled in and the query dropped —
    what one allowed_read_only_requests entry has to match exactly."""
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        return scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme), parts.path or "/"
    except ValueError:  # an unparseable port
        return None


# Why a write was refused, as the step detail and the warning observation phrase it.
_REFUSAL_TEXT = {
    "side_effects_off": "settings.allow_side_effects is false, settings.allow_local_side_effects does not cover this host, and the endpoint is not in allowed_read_only_requests",
    "non_loopback": "writes go only to a loopback or .test host, or one preflight approved and pinned, and this one is not",
    "redirect": "the server redirected the write somewhere the guard does not allow",
    "destructive_unapproved": "a delete needs its exact endpoint in settings.allowed_destructive_requests — approve it and run again",
    "override_unapproved": "this request carries a _method override field, so its verb does not decide what it does — approve the endpoint in settings.allowed_destructive_requests, or remove the field",
    "blocked_by_policy": "the project's e2e/permissions.json lists this endpoint under blocked_requests; no run setting can override it",
    "uninspectable_body": "neither post_data nor post_data_buffer could produce this request's body, so it was never cleared of carrying a _method override. No run setting sends it: allowed_destructive_requests approves DELETEs, and this request never reaches that rule. Drive an upload like this through the project's own test command (config.json e2e.existing_test_command) instead",
}


def _body_readings(text: str) -> list[str]:
    """One body as every text a backend might read it as.

    A UTF-16 body renders each ASCII character with a null beside it, so `_method` matches
    nothing here while the application, reading the declared charset, sees the field
    plainly. Both readings are produced and both are scanned.

    A null RATIO was tried to tell one case from the other and it was a guess dressed as a
    rule: a UTF-16 body of mostly non-ASCII text carries few nulls and slipped under the
    threshold. The cost of being wrong is asymmetric and this is the cheap side of it — a
    binary upload whose bytes happen to spell the field once the nulls are gone is refused
    with a message naming the endpoint, while the other mistake is a delete nobody approved.
    """
    if "\x00" not in text:
        return [text]
    return [text, text.replace("\x00", "")]


def _decode_body(raw: bytes) -> list[str]:
    """A request body's bytes as every text a backend might read them as.

    UTF-8 first, since that is what a browser sends. The readings themselves are
    `_body_readings`, shared with the ordinary string path: applying them only to the
    bytes fallback left a `post_data` string that already contained nulls scanned once,
    unchanged, which is the same hole one accessor further along.
    """
    return _body_readings(raw.decode("utf-8", errors="replace"))


def _blocked_list(config: dict, base_url: str) -> tuple:
    """(method, scheme, host, port, path-template) for every blocked_requests entry.

    `*` stays as the method, matched against anything at check time.
    """
    blocked = []
    for entry in config.get("blocked_requests") or []:
        parsed = blocked_request_target(entry) if isinstance(entry, str) else None
        if parsed is None:
            continue
        method, target = parsed
        key = _endpoint(urljoin(base_url, target) if target.startswith("/") else target)
        if key is not None:
            blocked.append((method, *key))
    return tuple(blocked)


def _destructive_allow_list(config: dict, base_url: str) -> tuple:
    """(method, scheme, host, port, path-template) for every allowed_destructive_requests entry.

    A tuple of templates rather than a set of exact endpoints, because the path is matched
    with `request_path_matches`: `/api/items/:id` covers `/api/items/7`. Approving the
    literal id would be the tighter promise and an unusable one — the id changes every run,
    and naming it here would also write it into the artifacts this package keeps ids out of.
    `:id` spans one segment, never a subtree, so the approval stays the route the user was
    shown rather than everything beneath it.
    """
    allowed = []
    for entry in config.get("allowed_destructive_requests") or []:
        parsed = destructive_request_target(entry) if isinstance(entry, str) else None
        if parsed is None:
            continue
        method, target = parsed
        key = _endpoint(urljoin(base_url, target) if target.startswith("/") else target)
        if key is not None:
            allowed.append((method, *key))
    return tuple(allowed)
_LEDGER_UNFINISHED = "no response before the run ended"
MAX_HIDDEN_CHECK = 20


def _read_only_allow_list(config: dict, base_url: str) -> frozenset:
    """(method, scheme, host, port, path) for every settings.allowed_read_only_requests entry."""
    allowed = set()
    for entry in config.get("allowed_read_only_requests") or []:
        parsed = read_only_request_target(entry) if isinstance(entry, str) else None
        if parsed is None:
            continue
        method, target = parsed
        key = _endpoint(urljoin(base_url, target) if target.startswith("/") else target)
        if key is not None:
            allowed.add((method, *key))
    return frozenset(allowed)


# GraphQL serves reads and writes on ONE endpoint, so admitting the endpoint as a read
# would admit its mutations too. The operation type is read from the body instead: string
# literals and comments are blanked first so a search term cannot spell `mutation {`.
# Conservative on purpose — a field literally named `mutation` taking arguments is refused
# as well, and a refused read costs a retry where an admitted write costs data.
_GRAPHQL_NOISE = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|#[^\n]*')
_GRAPHQL_WRITE = re.compile(r"\b(?:mutation|subscription)\b\s*(?:[_A-Za-z]\w*)?\s*[({@]")
_GRAPHQL_REFUSAL = "a GraphQL mutation or subscription"


def _graphql_write(query: str) -> bool:
    return bool(_GRAPHQL_WRITE.search(_GRAPHQL_NOISE.sub(" ", query)))


def read_only_refusal(body: str | None) -> str | None:
    """Why a POST to a confirmed read-only endpoint must still be refused, or None.

    Only the GraphQL shapes are inspected: a JSON document (or batch) with `query`, a form
    or query-string `query=`, or a raw GraphQL body. A persisted query carries a hash where
    the operation should be, so what it does cannot be read — refused. Any other body is
    the endpoint's own business, which the user confirmed from the handler reference.
    """
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        queries = parse_qs(body).get("query")
        candidates = queries if queries else [body]
        return _GRAPHQL_REFUSAL if any(_graphql_write(query) for query in candidates) else None
    for document in data if isinstance(data, list) else [data]:
        if not isinstance(document, dict):
            continue
        query = document.get("query")
        if isinstance(query, str):
            if _graphql_write(query):
                return _GRAPHQL_REFUSAL
        elif isinstance(document.get("extensions"), dict) and "persistedQuery" in document["extensions"]:
            return "a persisted GraphQL query, whose operation cannot be read"
    return None


def _error_kind(exc: BaseException) -> str:
    """Playwright's own classes are not importable here (tests use a stand-in page), so
    the kind is read from the class name and the network error code in the message."""
    message = str(exc)
    if "ERR_BLOCKED_BY_CLIENT" in message:
        return "navigation_blocked"
    if type(exc).__name__ == "TimeoutError":
        return "timeout"
    if "net::ERR_" in message:
        return "navigation_failed"
    return "action_failed"


class Session:
    """Drives one open page through a scenario and emits progress/observation events."""

    def __init__(
        self,
        page,
        config: dict,
        emit,
        *,
        clock=time.monotonic,
        sleep=None,
        artifacts_dir: str = "",
        has_secrets: bool = False,
        display_scenario: dict | None = None,
    ) -> None:
        self.page = page
        # The same scenario in placeholder form, by step id. What a step EXPECTED is reported
        # from here, never from the resolved step: a selector or text may hold a resolved
        # ${E2E_*} value, and one shorter than redact.MIN_SCRUB_CHARS cannot be put back to
        # its placeholder by substring afterwards.
        self._display: dict[str, dict] = {}
        if isinstance(display_scenario, dict):
            for key in ("steps", "cleanup"):
                for shown in display_scenario.get(key) or []:
                    if isinstance(shown, dict) and shown.get("id") is not None:
                        self._display[str(shown["id"])] = shown
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else None
        self.has_secrets = has_secrets
        # False when a resolved value is too short to scrub by substring (the runner decides):
        # a page's HTML could carry it, so no HTML is kept.
        self.capture_html = config.get("capture_html", True) is not False
        self.failed = False
        self.config = config
        self._emit = emit
        self.clock = clock
        # None = wait through the page. A test drives time itself and passes its own.
        self.sleep = sleep
        self.base_url = str(config.get("base_url") or "")
        self.step_timeout_ms = int(config.get("step_timeout_ms") or 8000)
        self.step_timeout_s = max(0.1, self.step_timeout_ms / 1000)
        self.nav_timeout_ms = int(config.get("nav_timeout_ms") or 15000)
        self.probe_limit = max(1, int(config.get("probe_max_elements") or 200))
        self.fail_on_console_error = bool(config.get("fail_on_console_error"))
        self.blocked: list[str] = []
        # Why each blocked URL was refused, so the step that caused it can say which policy
        # said no. The origin policy and the deny list both land in `blocked` and read
        # nothing alike to whoever has to act on the failure.
        self.blocked_detail: dict[str, str] = {}
        # Addresses already judged, per step. `framenavigated` and the `request` event both
        # report the same landing, and a redirect chain reports every hop; without this the
        # step would fail once per report of one navigation. Keyed on the step as well as the
        # address, because leaving a denied page and coming back to it later is a second
        # violation by a second step, and a run-wide key would have shown only the first.
        self._navigation_seen: set[tuple[str | None, str]] = set()
        # Which entries of `blocked` a step's window already claimed, BY POSITION. A
        # `pushState` scheduled by the last click lands after `_execute` has compared its
        # counter, so without this the address was recorded and then read by nobody. By
        # position and not by address, because the same address can be reached twice: keyed
        # on the URL, a later step claiming it also marked the earlier unattributed landing
        # as handled, and that first violation was never reported by anyone.
        self._navigation_attributed: set[int] = set()
        self.allow_side_effects = bool(config.get("allow_side_effects"))
        # Hosts preflight approved for writes, by name. It resolved each one and found only
        # loopback or private addresses, and pinned the browser's resolver to what it found.
        # Nothing here asks DNS again: a second question could get a second answer, and the
        # gap between the check and the write is the whole rebinding move.
        self.write_hosts = {str(h).lower() for h in config.get("write_hosts") or []}
        self.host_pins = {str(h).lower(): str(a) for h, a in (config.get("host_pins") or {}).items()}
        # Top-level navigations the browser followed through a redirect to an origin the
        # policy refuses. A redirect never reaches the route handler, so this is detected
        # from the `request` event, after the fact, and reported as exactly that.
        self.redirected_off_policy: list[str] = []
        self.read_only_allow = _read_only_allow_list(config, self.base_url)
        # Default true, and narrow: it opens writes to hosts preflight already classified
        # as local development, and it does not open deletes.
        self.allow_local_side_effects = config.get("allow_local_side_effects", True) is not False
        self.destructive_allow = _destructive_allow_list(config, self.base_url)
        # The project's deny list. It comes from the permissions file beside secrets.json,
        # never from a request, and it is checked before anything else — including before
        # a GET is waved through as a read. An endpoint listed here is one nobody wants a
        # browser run touching, and a run that could argue its way past it would make the
        # list advisory.
        self.blocked_requests = _blocked_list(config, self.base_url)
        # Deletes this run refused for want of an approval, as {method, endpoint}. The
        # runner turns them into the permission request the user answers; without the list
        # the refusal would say a delete was blocked without saying which one to approve.
        self.destructive_blocked: list[dict] = []
        # Writes sent under allow_side_effects whose body neither accessor could produce.
        # Reported as a warning, never silently: the delete check reads the body, so a
        # body nothing could read is a check that did not happen.
        self.uninspectable_writes: list[dict] = []
        try:
            self.total_timeout_s = float(config.get("total_timeout_s") or 0)
        except (TypeError, ValueError):
            self.total_timeout_s = 0.0
        # {method, origin, resource_type, attributed, reason[, refusal]}. Origin only: a path
        # or query can carry an id or a token, and this list ends up in events.
        self.mutations_blocked: list[dict] = []
        # {method, origin} of confirmed reads that went through; reported as a count.
        self.read_only_allowed: list[dict] = []
        # The request ledger: every non-GET/HEAD/OPTIONS request, with the step whose window
        # it was sent in. Open records wait for their response, keyed by the request object.
        self.phase = "steps"
        self.current_step: dict | None = None
        self.current_step_id: str | None = None
        self.last_step_id: str | None = None
        self._request_seq = 0
        self._open_requests: dict[int, tuple[dict, float, object]] = {}
        # Every request's metadata, reads included, for page optimization (DEC-014). Apart
        # from the ledger above, whose meaning is "writes a step made".
        self.network = NetworkLedger(clock)
        self._network_flushed = False
        self.run_started = clock()
        self._last_emit = clock()

    # ---- wire -----------------------------------------------------------------------
    def emit(self, event: dict) -> None:
        self._emit(event)
        self._last_emit = self.clock()

    def beat(self) -> None:
        """Long polls still talk: the supervisor's idle timer resets on every line."""
        if self.clock() - self._last_emit >= HEARTBEAT_EVERY_S:
            self.emit({"type": "heartbeat"})

    def pause(self) -> None:
        """One poll interval, spent inside Playwright so its events keep arriving.

        The sync API delivers events — route handlers, `framenavigated`, responses — only
        while this thread is inside a Playwright call. `time.sleep` is not one, and
        `page.url` is a cached attribute rather than a round trip, so a loop of the two
        (`expect_url`, a `url` readiness condition) held every event for the whole step
        timeout: a login POST passing through the write guard sat unrouted until the
        assertion had already failed, finishing ~0.8 s after whatever the timeout was.
        `wait_for_timeout` waits the same interval and dispatches while it does."""
        if self.sleep is not None:
            self.sleep(POLL_S)
            return
        try:
            self.page.wait_for_timeout(POLL_S * 1000)
        except Exception:
            time.sleep(POLL_S)

    def _page_url(self) -> str:
        try:
            return str(self.page.url)
        except Exception:
            return ""

    def _same(self, url: str) -> bool:
        return bool(self.base_url) and same_origin(url, self.base_url)

    # ---- observers and the origin guard ---------------------------------------------
    def on_console(self, message) -> None:
        if getattr(message, "type", None) != "error":
            return
        location = getattr(message, "location", None) or {}
        url = str(location.get("url") or self._page_url()) if isinstance(location, dict) else self._page_url()
        self.emit(
            {
                "type": "observation",
                "kind": "console_error",
                "url": url,
                "status": None,
                "detail": _cut(getattr(message, "text", "")),
                "same_origin": self._same(url),
                "main_request": False,
                "enforced": self.fail_on_console_error,
            }
        )

    def on_page_error(self, error) -> None:
        # The error's own stack names the script that threw. Attributing it to the page URL
        # made a third-party widget failing on our page a same-origin application `fail`.
        stack = str(getattr(error, "stack", None) or "")
        match = _STACK_URL.search(stack)
        url = re.sub(r"(?::\d+){1,2}$", "", match.group(0)) if match else self._page_url()
        self.emit(
            {
                "type": "observation",
                "kind": "page_error",
                "url": url,
                "status": None,
                "detail": _cut(getattr(error, "message", None) or error),
                "same_origin": self._same(url),
                "main_request": False,
            }
        )

    def on_response(self, response) -> None:
        status = int(getattr(response, "status", 0) or 0)
        opened = self._open_requests.get(id(getattr(response, "request", None)))
        if opened is not None:
            opened[0]["status"] = status
        if status < 500:
            return
        try:
            main = bool(response.request.is_navigation_request()) and response.frame == self.page.main_frame
        except Exception:  # service-worker and detached-frame responses have no frame
            main = False
        url = str(response.url)
        self.emit(
            {
                "type": "observation",
                "kind": "http_5xx",
                "url": url,
                "status": status,
                "detail": f"HTTP {status}",
                "same_origin": self._same(url),
                "main_request": main,
            }
        )

    def guard(self, route, request) -> None:
        """Refuse top-level navigation the spec policy would refuse — including clicks, which
        `validate_scenario` cannot see. Subresources and iframes pass: third-party noise is
        observed, not blocked. A redirect is NOT seen here: Chromium follows it without
        calling the route handler again (measured against Playwright 1.60), so a redirected
        navigation is caught by `on_request` after the fact, and a write is sent through
        `_send_write`, which follows its redirects itself.

        Then refuse writes. A step's `side_effect` is only what the scenario declares, so the
        player does not rely on it. With allow_side_effects true a non-GET/HEAD/OPTIONS
        request goes through only to a host preflight approved for writes — the request's
        own host, not base_url's, so an API on some other host is refused even from a local
        page unless that host too resolved private and was pinned. With it
        false every such request is aborted, on any origin, unless it is a POST to an
        endpoint the user confirmed as a read (allowed_read_only_requests) whose body is not
        a GraphQL write. Method-only otherwise: a GET that mutates, a WebSocket message, or a
        service worker's own fetch is not seen here. Every write, sent or refused, enters
        the request ledger."""
        url = str(request.url)
        try:
            top_level = bool(request.is_navigation_request()) and request.frame == self.page.main_frame
        except Exception:
            top_level = False
        if top_level and (refusal := self._navigation_refusal(url)):
            self._note_off_policy(url, refusal, redirected=False)
            route.abort("blockedbyclient")
            return
        wire_method = str(getattr(request, "method", None) or "GET").upper()
        body_unreadable = False
        bodies: list[str] = []
        try:
            raw_body = getattr(request, "post_data", None)
            if isinstance(raw_body, str):
                bodies = _body_readings(raw_body)
        except Exception:  # a body Playwright cannot decode as text (multipart upload)
            # `post_data` refuses a multipart body; `post_data_buffer` hands over the same
            # bytes without trying to be a string. Decoded leniently and only to be read
            # for an override field — a file upload is the common case here and it must
            # keep working, while an upload envelope carrying `_method=DELETE` must not
            # pass as an ordinary write just because Playwright called the body binary.
            try:
                buffered = getattr(request, "post_data_buffer", None)
                if isinstance(buffered, (bytes, bytearray)):
                    bodies = _decode_body(bytes(buffered))
            except Exception:
                bodies = []
            body_unreadable = not bodies
        body = bodies[0] if bodies else None
        try:
            request_headers = dict(request.headers or {})
        except Exception:
            request_headers = {}
        # Whether this request can be judged by its verb at all. Laravel and Rails route a
        # delete as a POST carrying `_method`, so a request holding that field is not the
        # method it is spelled as — and which value a given framework honours, on which
        # verbs, after how many layers of decoding, is not something this guard can know.
        # So it does not try. Carrying the field is the whole test; the value is not read.
        #
        # Asked BEFORE the safe-method exit. Reading the verb first and the field second
        # let `GET /items/7?_method=DELETE` leave through the door held open for reads.
        try:
            query = urlsplit(url).query
        except ValueError:
            query = ""
        # Every reading of the body, because UTF-16 gives two and either could be the one
        # the application parses.
        overridden = carries_method_override(None, request_headers, query) or any(
            carries_method_override(text, None, None) for text in bodies
        )
        method = wire_method
        endpoint = _endpoint(url)
        # Before the read exit, and before every permission below it. A read can still
        # cost something — a mailer fired by a GET, an integration that bills per call —
        # and the deny list is where a project says which endpoints are not this run's to
        # touch at all.
        if self._request_blocked(method, endpoint, any_method=overridden):
            self.mutations_blocked.append(
                {
                    "method": method,
                    "origin": _origin(url),
                    "resource_type": str(getattr(request, "resource_type", None) or ""),
                    "attributed": False,
                    "reason": "blocked_by_policy",
                }
            )
            self._ledger_open(request, url, method, blocked=_REFUSAL_TEXT["blocked_by_policy"])
            route.abort("blockedbyclient")
            return
        if method in SAFE_METHODS and not overridden:
            route.continue_()
            return
        refusal = None
        if method in DESTRUCTIVE_METHODS or overridden:
            # Ahead of every other rule, allow_side_effects included: a delete is approved
            # one endpoint at a time or not at all. Being local is not the question here —
            # a local database still holds work that was not this run's to remove. A
            # request carrying an override joins it, because nothing short of the backend
            # can say it is not one.
            # Approved as `DELETE <endpoint>` either way. A POST carrying the field is
            # asking to be routed as a delete, so that is the permission it needs — and
            # the person granting it is told which endpoint, not which verb.
            if (
                endpoint is not None
                and self._write_host_allowed(endpoint[1])
                and self._destructive_allowed("DELETE", endpoint)
            ):
                # `destructive=True` travels with it. The approval was granted for THIS
                # endpoint, and a 307 re-sends the body to another one — the wire method
                # is POST on an overridden request, so without this flag the redirect
                # check would judge it an ordinary write and let the delete land wherever
                # the server pointed.
                self._send_write(route, request, url, method, read_only=False, destructive=True, overridden=overridden)
                return
            reason = "override_unapproved" if overridden and method not in DESTRUCTIVE_METHODS else "destructive_unapproved"
            self.destructive_blocked.append(
                {"method": method, "endpoint": sanitize_endpoint(url), "origin": _origin(url),
                 "override": overridden}
            )
        elif self.allow_side_effects:
            if endpoint is not None and self._write_host_allowed(endpoint[1]):
                if body_unreadable:
                    # Neither accessor could produce the bytes, so this write was never
                    # cleared of carrying a delete. It used to go anyway, on the reasoning
                    # that `allow_side_effects` is the broad, explicit opt-in and that
                    # refusing would stop uploads that worked before any of this existed.
                    # What that traded away is the guarantee the rule above it makes: a
                    # delete is approved one endpoint at a time. An envelope holding
                    # `_method=DELETE` walks through here unread, and the run reports it
                    # afterwards — which is the wrong end of the request to find out.
                    #
                    # So it is refused — and refused outright, with nothing to turn on.
                    # An endpoint approval was the obvious way out and it is not one: the
                    # destructive allow-list takes DELETEs, this request is a POST whose
                    # verb nobody can confirm, and it never reaches that rule to be
                    # matched against it. Saying otherwise in the refusal would send the
                    # reader to a setting that cannot help them. An upload that has to
                    # happen is the project's own test command's to make.
                    self.uninspectable_writes.append(
                        {"method": method, "origin": _origin(url)}
                    )
                    reason = "uninspectable_body"
                else:
                    self._send_write(route, request, url, method, read_only=False)
                    return
            else:
                reason = "non_loopback"
        elif (
            self.allow_local_side_effects
            and method in WRITE_METHODS
            and not body_unreadable
            and endpoint is not None
            and is_local_dev_host(endpoint[1])
        ):
            # A non-destructive write to a development host. Proving that a form saves is
            # the ordinary reason this command exists, and it used to require the same
            # switch that permits writing to every preflight-approved host.
            #
            # `method in WRITE_METHODS` is what the permission was described as — POST,
            # PUT, PATCH — and the two rungs above it only subtract `SAFE_METHODS` and
            # `DESTRUCTIVE_METHODS`. Everything else fell through: a `PURGE`, a `PROPPATCH`,
            # any verb a stack invents. Being local is not a reason to send a method nobody
            # named; the switch was granted for ordinary form writes, and that is the
            # ladder `spec.py` already defines.
            #
            # `not body_unreadable` is the premise of that permission, not a detail of it.
            # This branch is safe only because the delete check above could read the body
            # and rule one out; a multipart body Playwright cannot decode as text could be
            # carrying `_method=DELETE` and nothing here would know. So an uninspectable
            # write does not receive this permission either. It used to fall through to
            # `allow_side_effects` and be sent under that switch; it is now refused there
            # too, so both rungs say the same thing about a body nobody can read.
            self._send_write(route, request, url, method, read_only=False)
            return
        else:
            reason = "side_effects_off"
            if endpoint is not None and (method, *endpoint) in self.read_only_allow:
                if body_unreadable:
                    refusal = "a request body that cannot be read as text"
                else:
                    # Every reading, not `bodies[0]`. A confirmed read is admitted because
                    # its body was checked for a GraphQL mutation, and a body has more
                    # than one honest reading — a UTF-16 mutation spells nothing in the
                    # first and spells itself plainly in the second. Checking one of them
                    # is the same mistake as trusting the verb.
                    refusal = next(
                        (r for r in (read_only_refusal(text) for text in bodies) if r),
                        None,
                    )
                if refusal is None:
                    self.read_only_allowed.append({"method": method, "origin": _origin(url)})
                    self._send_write(route, request, url, method, read_only=True)
                    return
        record = {
            "method": method,
            "origin": _origin(url),
            "resource_type": str(getattr(request, "resource_type", None) or ""),
            "attributed": False,
            "reason": reason,
        }
        if refusal:
            record["refusal"] = refusal
        self.mutations_blocked.append(record)
        self._ledger_open(request, url, method, blocked=refusal or _REFUSAL_TEXT[reason])
        route.abort("blockedbyclient")

    def _write_host_allowed(self, host: str) -> bool:
        return is_local_dev_host(host) or host in self.write_hosts

    def _destructive_allowed(self, method: str, endpoint: tuple) -> bool:
        """Whether an approved entry covers this exact request."""
        scheme, host, port, path = endpoint
        for allowed_method, a_scheme, a_host, a_port, template in self.destructive_allow:
            if (allowed_method, a_scheme, a_host, a_port) == (method, scheme, host, port) and request_path_matches(template, path):
                return True
        return False

    def _request_blocked(self, method: str, endpoint: tuple | None, *, any_method: bool = False) -> bool:
        """Whether the project's deny list names this request. `*` matches any method.

        `any_method` is for a request carrying an override field. Its verb is not what it
        will be routed as, so matching the deny list against that verb alone let a POST
        carrying `_method` walk past a `DELETE /api/users/:id` entry — the exact endpoint
        the project had said to leave alone. A request that cannot be classified is
        measured against every entry for its endpoint instead of one.
        """
        if endpoint is None:
            return False
        scheme, host, port, path = endpoint
        for blocked_method, b_scheme, b_host, b_port, template in self.blocked_requests:
            if (b_scheme, b_host, b_port) != (scheme, host, port):
                continue
            if not any_method and blocked_method not in ("*", method):
                continue
            if request_path_matches(template, path):
                return True
        return False

    def _redirect_refusal(self, target: str, status: int, method: str, *, read_only: bool, top_level: bool, destructive: bool = False, overridden: bool = False) -> str | None:
        """Why a write's redirect to `target` must not be followed, or None."""
        if status in _BODY_KEEPING_REDIRECTS:
            endpoint = _endpoint(target)
            if endpoint is None:
                return f"a {status} redirect to an unparseable address"
            # First here, as it is first in `guard`. A 307/308 hop is fetched by Playwright
            # rather than issued by the browser, so it never returns through the route
            # handler — this is the only place the deny list gets to see it. Without the
            # check, a write to an allowed endpoint answered `307 → /api/users/1` landed on
            # an endpoint the project had named as not this run's to touch, because the
            # host was the same one and nothing below re-read the list.
            if self._request_blocked(method, endpoint, any_method=overridden):
                return f"a {status} redirect re-sends the body to {sanitize_endpoint(target)}, which the deny list names"
            if read_only:
                if (method, *endpoint) not in self.read_only_allow:
                    return f"a {status} redirect re-sends the body to {sanitize_endpoint(target)}, which is not a confirmed read"
                return None
            if not self._write_host_allowed(endpoint[1]):
                return f"a {status} redirect re-sends the body to {_origin(target)}, which may not take writes"
            if (destructive or method in DESTRUCTIVE_METHODS) and not self._destructive_allowed("DELETE", endpoint):
                # The host check alone was enough while every write was judged by where it
                # went. A delete is judged by WHICH endpoint it reaches, so a 307 from an
                # approved endpoint to a neighbouring path on the same local host would
                # re-send the body somewhere nobody agreed to — the read-only branch above
                # has always re-checked for exactly this reason.
                return f"a {status} redirect re-sends the delete to {sanitize_endpoint(target)}, which is not an approved delete"
            return None
        # 301/302/303: the browser follows with a GET, and that GET is a fresh request
        # through `context.route("**/*", guard)` — so the deny list, the safe-method exit
        # and every permission below them are applied to the target there, not here. Only
        # a top-level navigation needs an answer before the response is handed back, since
        # the page would already be leaving for it.
        if top_level:
            refused = self._navigation_refusal(target)
            if refused:
                return f"a {status} redirect navigates off policy: {refused}"
        return None

    def _pinned_fetch_args(self, target: str, request) -> tuple[dict | None, str | None]:
        """`route.fetch` keyword arguments that keep a pinned host at its pinned address.

        `route.fetch` runs in Playwright's own network stack, where Chromium's
        `--host-resolver-rules` does not apply, so a pinned name would be looked up afresh —
        the rebinding the pin exists to stop. Over http the address goes in the URL and the
        name in `Host`. Over https that would break certificate validation, so it is refused.
        """
        try:
            parts = urlsplit(target)
        except ValueError:
            return None, "an unparseable address"
        host = (parts.hostname or "").lower()
        pin = self.host_pins.get(host)
        if not pin:
            return {}, None
        if parts.scheme.lower() != "http":
            return None, f"{host} is pinned to {pin}, and an https write cannot be held to a pinned address outside the browser"
        address = f"[{pin}]" if ":" in pin else pin
        netloc = f"{address}:{parts.port}" if parts.port else address
        try:
            headers = dict(request.all_headers())
        except Exception:
            headers = {}
        headers["host"] = parts.netloc
        return {"url": parts._replace(netloc=netloc).geturl(), "headers": headers}, None

    def _refuse_sent_write(self, route, request, method: str, url: str, refusal: str) -> None:
        """Close an opened ledger record as blocked, count it, and abort the request."""
        opened = self._open_requests.pop(id(request), None)
        if opened is not None:
            record, started, _ = opened
            record.update({"blocked": True, "failure": f"blocked: {_cut(refusal, 200)}", "duration_ms": int((self.clock() - started) * 1000)})
            self.emit(record)
        self.mutations_blocked.append(
            {
                "method": method,
                "origin": _origin(url),
                "resource_type": str(getattr(request, "resource_type", None) or ""),
                "attributed": False,
                "reason": "redirect",
                "refusal": refusal,
            }
        )
        route.abort("blockedbyclient")

    def _send_write(self, route, request, url: str, method: str, *, read_only: bool, destructive: bool = False, overridden: bool = False) -> None:
        """Send one approved write, following its redirects here instead of in the browser.

        A redirect the browser follows never reaches the route handler, and a 307/308
        re-sends the method AND the body — measured: a POST to an approved `/a` answered
        307 → `/b` delivered the body to `/b` with no guard in between. So the request is
        fetched with `max_redirects=0`; each body-keeping hop is judged by the same rule as
        the original before it is sent, and a GET-turning redirect is handed back to the
        browser once its target passes the navigation policy. The write still goes out
        once: the fulfilled response is the one the server gave.
        """
        self._ledger_open(request, url, method, read_only=read_only)
        fetch = getattr(route, "fetch", None)
        if fetch is None:  # a stand-in route with no network stack of its own
            route.continue_()
            return
        try:
            top_level = bool(request.is_navigation_request()) and request.frame == self.page.main_frame
        except Exception:
            top_level = False
        try:
            body = request.post_data_buffer
        except Exception:
            body = None
        target, hops = url, 0
        kwargs, refusal = self._pinned_fetch_args(target, request)
        while True:
            if refusal:
                self._refuse_sent_write(route, request, method, url, refusal)
                return
            try:
                if hops == 0:
                    response = fetch(max_redirects=0, timeout=self.nav_timeout_ms, **kwargs)
                else:
                    response = fetch(method=method, post_data=body, max_redirects=0, timeout=self.nav_timeout_ms, **{"url": target, **kwargs})
            except Exception as exc:
                self._ledger_close(request, f"fetch failed: {_cut(str(exc).splitlines()[0] if str(exc) else type(exc).__name__, 100)}")
                route.abort("failed")
                return
            status = int(getattr(response, "status", 0) or 0)
            location = (getattr(response, "headers", None) or {}).get("location") if status in _REDIRECTS else None
            if not location:
                break
            next_target = urljoin(target, location)
            refusal = self._redirect_refusal(next_target, status, method, read_only=read_only, top_level=top_level, destructive=destructive, overridden=overridden)
            if refusal or status not in _BODY_KEEPING_REDIRECTS:
                if refusal:
                    continue
                break  # a GET-turning redirect the policy allows: the browser follows it
            hops += 1
            if hops > MAX_WRITE_REDIRECTS:
                refusal = f"more than {MAX_WRITE_REDIRECTS} redirects"
                continue
            target = next_target
            kwargs, refusal = self._pinned_fetch_args(target, request)
        route.fulfill(response=response)

    def _navigation_refusal(self, url: str) -> str | None:
        """Why the page may not be at `url`, or None.

        Two policies, one question. `navigation_error` judges the ORIGIN — whether this run
        may go there at all. The deny list judges the ENDPOINT — what nobody wants a browser
        run touching, whichever origin serves it. Only the first was ever asked of a
        navigation, so a redirect from an allowed origin to a denied path on that same
        origin was waved through: `blocked_requests` was enforced against writes and against
        nothing else, though the list names endpoints, not verbs.

        GET and `*` entries only. A `DELETE /api/users/:id` entry says not to delete that
        record; opening the same address is a read, and refusing it would make the list mean
        something it does not say.
        """
        refused = navigation_error(url, self.config)
        if refused:
            return refused
        if self._request_blocked("GET", _endpoint(url)):
            return f"{sanitize_endpoint(url)} is named by the project's blocked_requests"
        return None

    def _note_off_policy(self, url: str, refusal: str, *, redirected: bool) -> None:
        """Record one address policy refuses, once per step, with the reason it was refused."""
        key = (self.current_step_id, url)
        if key in self._navigation_seen:
            return
        self._navigation_seen.add(key)
        if redirected:
            self.redirected_off_policy.append(url)
        self.blocked.append(url)
        self.blocked_detail[url] = refusal

    def on_frame_navigated(self, frame) -> None:
        """Every address the main frame comes to rest on, whatever put it there.

        `on_request` sees a redirect only when the browser issued a request it can attribute
        to one. A `302` Chromium follows internally, a `history.pushState`, a meta refresh
        and a JS assignment all change the address bar without producing one, and the
        address bar is what the policy is about. So it is read after every change rather
        than inferred from the traffic — which is also the only reading that survives a
        redirect chain the route handler never sees again.

        After the fact: the page is already there. The step that caused it fails saying so,
        the same way a followed redirect has always been reported.
        """
        try:
            if frame != self.page.main_frame:
                return
            url = str(frame.url or "")
        except Exception:
            return
        if not url or not url.lower().startswith(("http://", "https://")):
            return  # about:blank between navigations, and data:/blob: the policy has no say over
        refusal = self._navigation_refusal(url)
        if refusal:
            self._note_off_policy(url, refusal, redirected=True)

    def on_request(self, request) -> None:
        """Catch a top-level navigation that a redirect took off policy.

        The route handler saw only the first request; the browser followed the redirect on
        its own. The page may already be loading the refused origin, so this does not claim
        to have blocked anything: the step that caused it fails, saying the redirect was
        followed.
        """
        try:
            self.network.start(request, self.current_step_id)
        except Exception:  # metadata is never a reason for the run to fail
            pass
        if getattr(request, "redirected_from", None) is None:
            return
        url = str(request.url)
        try:
            top_level = bool(request.is_navigation_request()) and request.frame == self.page.main_frame
        except Exception:
            top_level = False
        if top_level and (refusal := self._navigation_refusal(url)):
            self._note_off_policy(url, refusal, redirected=True)

    # ---- the request ledger ---------------------------------------------------------
    def _ledger_open(self, request, url: str, method: str, *, blocked: str | None = None, read_only: bool = False) -> None:
        """Start one ledger record. A refused request is complete at once; a sent one waits
        for `requestfinished` / `requestfailed`.

        Attribution is the step whose window was open when the request was sent. Outside
        any window (a debounce, a poll, a write fired after the step returned) the record
        says `uncertain` and names the step that ran last — never assigned to it.
        """
        self._request_seq += 1
        step = self.current_step
        try:
            path = urlsplit(url).path or "/"
        except ValueError:
            path = ""
        spec = step.get("request") if isinstance(step, dict) else None
        if step is None or read_only:
            # A confirmed read is not a write the step could have planned: counting it as
            # `planned: false` inflated the run record's unplanned-write figure.
            planned = None
        elif isinstance(spec, dict):
            planned = str(spec.get("method") or "").upper() == method and request_path_matches(str(spec.get("path") or ""), path)
        else:
            planned = False
        record = {
            "type": "request",
            "id": f"r{self._request_seq}",
            "phase": self.phase,
            "step_id": self.current_step_id if step is not None else None,
            "after_step": None if step is not None else self.last_step_id,
            "attribution": "step" if step is not None else "uncertain",
            "method": method,
            "endpoint": sanitize_endpoint(url),
            "resource_type": str(getattr(request, "resource_type", None) or ""),
            "read_only": read_only,
            "blocked": blocked is not None,
            "planned": planned,
            "status": None,
            "failure": f"blocked: {blocked}" if blocked else None,
            "duration_ms": 0 if blocked else None,
        }
        if blocked is not None:
            self.emit(record)
            return
        self._open_requests[id(request)] = (record, self.clock(), request)

    def _ledger_close(self, request, failure: str | None) -> None:
        opened = self._open_requests.pop(id(request), None)
        if opened is None:
            return  # not a write, or refused and already reported
        record, started, _ = opened
        record["duration_ms"] = int((self.clock() - started) * 1000)
        if failure:
            record["failure"] = _cut(failure, 120)
        self.emit(record)

    def on_request_finished(self, request) -> None:
        # HTTP 404 or 500 still "finishes": the status the response event recorded says how.
        self._ledger_close(request, None)

    def on_request_failed(self, request) -> None:
        failure = getattr(request, "failure", None)
        self._ledger_close(request, str(failure) if failure else "request failed")

    # ---- request metadata (DEC-014) ----------------------------------------------------
    # Registered on the context, like `request`: a popup's requests start there, and a
    # page-only response/finished/failed listener would leave them open to the end of the run.
    def on_network_response(self, response) -> None:
        try:
            self.network.response(response)
        except Exception:  # metadata is never a reason for the run to fail
            pass

    def on_network_finished(self, request) -> None:
        try:
            self.network.finish(request, None)
        except Exception:
            pass

    def on_network_failed(self, request) -> None:
        failure = getattr(request, "failure", None)
        try:
            self.network.finish(request, str(failure) if failure else "request failed")
        except Exception:
            pass

    def flush_network(self) -> None:
        """The run's request metadata, once, whether the run ended or crashed."""
        if self._network_flushed:
            return
        self._network_flushed = True
        self.network.finish_open(_LEDGER_UNFINISHED)
        for event in self.network.events():
            self.emit(event)

    def flush_requests(self) -> None:
        """Writes still waiting when the run ends are reported as such, not dropped."""
        for key in list(self._open_requests):
            record, started, _ = self._open_requests.pop(key)
            record["duration_ms"] = int((self.clock() - started) * 1000)
            record["failure"] = _LEDGER_UNFINISHED
            self.emit(record)

    # ---- selectors ------------------------------------------------------------------
    def locator(self, selector: dict, within: dict | None = None):
        """A locator for `selector`, searched inside `within` when the step scopes it — so a
        "Simpan" in a modal is not confused with a "Simpan" on the page behind it."""
        root = self.locator(within) if within else self.page
        key = SELECTOR_RANK[selector_rank(selector)]
        if key == "role":
            if "name" in selector:
                return root.get_by_role(selector["role"], name=selector["name"])
            return root.get_by_role(selector["role"])
        if key == "label":
            return root.get_by_label(selector["label"])
        if key == "e2e":
            return root.locator(e2e_css(selector["e2e"]))
        if key == "text":
            return root.get_by_text(selector["text"])
        return root.locator(selector["css"])

    def resolve(self, step: dict, *, visible: bool = False):
        """Poll candidates strongest-first until one matches exactly one element.

        Returns (locator, provenance, error, selection). The failure provenance is the
        strongest candidate's: if the codebase named a selector and it is gone, that is what
        the classifier must weigh, not the heuristic fallback that also missed. `selection`
        is the runtime half of the element mapping — which candidate matched, how many
        elements each tried candidate matched, whether a fallback was used, and for a
        candidate the codebase named, the `path:line` it came from and its selector's key
        names — never the selector's values, which can be resolved credentials. Only the
        draft's candidates are ever tried.
        """
        candidates = step_selectors(step)
        within = step.get("within") if isinstance(step.get("within"), dict) else None
        counts: list[int | None] = [None] * len(candidates)
        if not candidates:
            return None, None, {"kind": "harness_error", "detail": "step has no usable selector"}, {"candidate": None, "match_counts": [], "fallback_used": False}
        deadline = self.clock() + self.step_timeout_s
        ambiguous = hidden = None
        while True:
            for position, candidate in enumerate(candidates):
                loc = self.locator(candidate["selector"], within)
                count = loc.count()
                counts[position] = count
                if count == 1:
                    if visible and not loc.is_visible():
                        hidden = hidden or candidate
                        continue
                    selection = {"candidate": position, "match_counts": counts[: position + 1], "fallback_used": position > 0}
                    # Only for a candidate the codebase named: this is what the tagging pass
                    # writes back to, and it may only write to an address it was given.
                    if candidate["provenance"] == "source" and candidate.get("ref"):
                        selection["source_ref"] = str(candidate["ref"]).strip()
                        # Key names, never values. A selector may hold a resolved ${ENV}
                        # value, and one shorter than redact.MIN_SCRUB_CHARS cannot be put
                        # back to its placeholder by substring — it would ride out in the
                        # events, the report and the tag proposals. `role+name` says which
                        # kind of selector won, which is all the tagging pass reads.
                        selection["selector_keys"] = sorted(candidate["selector"])
                    return loc, candidate["provenance"], None, selection
                if count > 1:
                    ambiguous = ambiguous or candidate
            if self.clock() >= deadline:
                break
            self.beat()
            self.pause()
        waited = f"within {self.step_timeout_s:g}s"
        selection = {"candidate": None, "match_counts": counts, "fallback_used": False}
        if hidden:
            return None, hidden["provenance"], {"kind": "not_visible", "detail": f"matched element stayed hidden {waited}"}, selection
        if ambiguous:
            return None, ambiguous["provenance"], {"kind": "selector_ambiguous", "detail": f"selector matched more than one element {waited}"}, selection
        return None, candidates[0]["provenance"], {
            "kind": "selector_missing",
            "detail": f"no element matched {len(candidates)} candidate(s) {waited}",
        }, selection

    # ---- readiness ------------------------------------------------------------------
    def _holds(self, condition: dict) -> bool:
        """Whether one declared readiness condition is true right now."""
        try:
            key, value = next(iter(condition.items()))
            if key == "url":
                return str(value) in self._page_url()
            if key == "text":
                loc = self.page.get_by_text(str(value))
                return loc.count() >= 1 and bool(loc.nth(0).is_visible())
            loc = self.locator(value)
            count = loc.count()
            if key == "hidden":
                # A loader that is gone, or still in the DOM but hidden, is gone.
                return all(not loc.nth(i).is_visible() for i in range(min(count, MAX_HIDDEN_CHECK)))
            if count < 1 or not loc.nth(0).is_visible():
                return False
            return key == "visible" or bool(loc.nth(0).is_enabled())
        except Exception:
            return False

    def wait_ready(self, conditions: list, shown: list | None = None) -> dict:
        """Poll the step's readiness conditions until all hold or the step timeout passes.
        Returns {conditions, waited_ms, unmet}; `unmet` names what never became true, in the
        placeholder form `shown` gives when there is one."""
        started = self.clock()
        deadline = started + self.step_timeout_s
        shown = shown if isinstance(shown, list) and len(shown) == len(conditions) else conditions
        while True:
            unmet = [i for i, c in enumerate(conditions) if isinstance(c, dict) and c and not self._holds(c)]
            if not unmet or self.clock() >= deadline:
                break
            self.beat()
            self.pause()
        described = [shown[i] if isinstance(shown[i], dict) else conditions[i] for i in unmet]
        return {
            "conditions": len(conditions),
            "waited_ms": int((self.clock() - started) * 1000),
            "unmet": [_cut(f"{k}: {json.dumps(v, ensure_ascii=False)}", 120) for c in described for k, v in c.items()],
        }

    def _shown(self, step: dict) -> dict:
        """The placeholder form of `step`, or the step itself when none was handed over."""
        return self._display.get(str(step.get("id"))) or step

    # ---- assertions -----------------------------------------------------------------
    def _text_matches(self, actual: str, step: dict) -> bool:
        if "equals" in step:
            target = str(step["equals"])
            return actual == target or (bool(self.base_url) and actual == urljoin(self.base_url, target))
        if "contains" in step:
            return str(step["contains"]) in actual
        if "matches" in step:
            return re.search(str(step["matches"]), actual) is not None
        return False

    def _poll_text(self, read, step: dict) -> tuple[bool, str]:
        deadline = self.clock() + self.step_timeout_s
        while True:
            actual = read()
            if self._text_matches(actual, step):
                return True, actual
            if self.clock() >= deadline:
                return False, actual
            self.beat()
            self.pause()

    def stable(self, step: dict | None = None) -> bool:
        """Whether the page had settled when a step failed — the classifier's app/unknown split.

        Loaded, and every readiness condition the step declared holding. Not `networkidle`:
        a page that polls never reaches it, which made every failure there `unknown`."""
        try:
            self.page.wait_for_load_state("load", timeout=STABLE_WAIT_MS)
            if self.page.evaluate("document.readyState") != "complete":
                return False
            ready = (step or {}).get("ready")
            return all(self._holds(c) for c in ready if isinstance(c, dict) and c) if isinstance(ready, list) else True
        except Exception:
            return False

    # ---- steps ----------------------------------------------------------------------
    def perform(self, step: dict) -> dict:
        action = step.get("action")
        shown = self._shown(step)
        expectation = {k: shown[k] for k in ("contains", "equals", "matches") if k in shown}

        if action == "goto":
            url = urljoin(self.base_url, str(step.get("url"))) if self.base_url else str(step.get("url"))
            refused = self._navigation_refusal(url)
            if refused:
                return {"status": "failed", "error": {"kind": "navigation_blocked", "detail": refused}}
            response = self.page.goto(url, wait_until="load", timeout=self.nav_timeout_ms)
            return {"status": "passed", "actual": {"url": self._page_url(), "status": getattr(response, "status", None)}}

        if action == "expect_url":
            ok, actual = self._poll_text(self._page_url, step)
            if ok:
                return {"status": "passed", "expected": expectation, "actual": {"url": actual}}
            return {"status": "failed", "expected": expectation, "actual": {"url": actual},
                    "error": {"kind": "assertion", "detail": "url did not match"}}

        if action == "expect_title":
            ok, actual = self._poll_text(lambda: str(self.page.title()), step)
            if ok:
                return {"status": "passed", "expected": expectation, "actual": {"title": _cut(actual, 120)}}
            return {"status": "failed", "expected": expectation, "actual": {"title": _cut(actual, 120)},
                    "error": {"kind": "assertion", "detail": "title did not match"}}

        if action == "probe":
            data = self.page.evaluate(_PROBE_JS, self.probe_limit)
            return {"status": "passed", "actual": {"probe": data}}

        visible = action in ("wait_dom", "expect_dom")
        loc, provenance, error, selection = self.resolve(step, visible=visible)
        selector_view = {
            "selector_provenance": provenance,
            "selection": selection,
            "expected": {"selector": shown.get("selector") or [c["selector"] for c in step_selectors(shown)]},
        }
        if error:
            return {"status": "failed", "error": error, **selector_view}
        timeout = self.step_timeout_ms
        try:
            if action == "click":
                loc.click(timeout=timeout)
            elif action == "fill":
                loc.fill(str(step.get("value") or ""), timeout=timeout)
            elif action == "select":
                if step.get("value") is not None:
                    loc.select_option(value=str(step["value"]), timeout=timeout)
                else:
                    loc.select_option(label=str(step.get("label") or ""), timeout=timeout)
            elif action == "press":
                loc.press(str(step.get("key") or ""), timeout=timeout)
            elif action == "upload":
                # Checked again here, not trusted from validation: the file could have been
                # swapped for a link since.
                target = resolve_upload(self.config.get("project_root") or "", step.get("file"))
                if target is None:
                    return {"status": "failed", "error": {"kind": "harness_error", "detail": "upload file is not a file inside the project"}, **selector_view}
                loc.set_input_files(str(target), timeout=timeout)
        except Exception as exc:
            kind = _error_kind(exc)
            if kind == "timeout" and action in _ELEMENT_ACTIONS:
                kind = "not_visible"  # resolved, but never actionable: covered, disabled, detached
            return {"status": "failed", "error": {"kind": kind, "detail": _cut(str(exc).splitlines()[0] if str(exc) else kind)}, **selector_view}
        if action == "expect_dom" and "text" in step:
            wanted = str(step["text"])
            ok, actual = self._poll_text(lambda: str(loc.inner_text()), {"contains": wanted})
            view = {**selector_view, "expected": {**selector_view["expected"], "text": str(shown.get("text", ""))}, "actual": {"text": _cut(actual, 120)}}
            if not ok:
                return {"status": "failed", "error": {"kind": "assertion", "detail": "element text did not match"}, **view}
            return {"status": "passed", **view}
        return {"status": "passed", **selector_view}

    def capture(self, index: int, outcome: dict) -> list[dict]:
        """Failure evidence per plan §16, returned as artifact events.

        A selector miss gets a bounded probe first; a heuristic miss stops there — the
        scenario guessed, and a screenshot adds nothing the probe does not. Any other
        failure on the page keeps bounded HTML and, unless the scenario resolved `${ENV}`
        values, one screenshot. A blocked navigation never touched the page: nothing.
        """
        kind = (outcome.get("error") or {}).get("kind")
        if kind == "navigation_blocked":
            return []
        if kind in _SELECTOR_ERRORS:
            try:
                outcome["probe"] = self.page.evaluate(_PROBE_JS, self.probe_limit)
            except Exception:
                pass
            if outcome.get("selector_provenance") == "heuristic":
                return []
        if self.artifacts_dir is None:
            return []
        stem = f"step{index:02d}"
        events: list[dict] = []
        try:
            self.page.evaluate(_CLEAR_INPUTS_JS)
        except Exception:
            pass
        if not self.capture_html:
            events.append(
                {"type": "artifact", "kind": "skipped", "name": f"{stem}.html", "step": index,
                 "detail": "html skipped: a resolved value is too short to be scrubbed from the page"}
            )
        else:
            try:
                raw = str(self.page.content()).encode("utf-8")
                self.artifacts_dir.mkdir(parents=True, exist_ok=True)
                (self.artifacts_dir / f"{stem}.html").write_bytes(raw[:MAX_HTML_BYTES])
                events.append(
                    {"type": "artifact", "kind": "html", "name": f"{stem}.html", "step": index,
                     "bytes": min(len(raw), MAX_HTML_BYTES), "truncated": len(raw) > MAX_HTML_BYTES}
                )
            except Exception as exc:
                events.append({"type": "artifact", "kind": "error", "name": None, "step": index, "detail": _cut(f"html: {exc}")})
        if self.has_secrets:
            events.append(
                {"type": "artifact", "kind": "skipped", "name": f"{stem}.png", "step": index,
                 "detail": f"screenshot skipped: {SECRET_CAPTURE_NOTE}"}
            )
            return events
        try:
            path = self.artifacts_dir / f"{stem}.png"
            self.page.screenshot(path=str(path))
            events.append(
                {"type": "artifact", "kind": "screenshot", "name": path.name, "step": index,
                 "bytes": path.stat().st_size if path.exists() else None}
            )
        except Exception as exc:
            events.append({"type": "artifact", "kind": "error", "name": None, "step": index, "detail": _cut(f"screenshot: {exc}")})
        return events

    def _execute(self, step: dict, step_id: str) -> dict:
        """One step, test or cleanup: readiness, the action once, then what the guards saw.

        The step's window — the time its requests are attributed to it — is exactly this
        call. `ready` is awaited before the action, or after navigation for a goto.
        """
        blocked_before = len(self.blocked)
        mutations_before = len(self.mutations_blocked)
        self.current_step, self.current_step_id = step, step_id
        ready = step.get("ready") if isinstance(step.get("ready"), list) else None
        readiness = None
        try:
            shown_ready = self._shown(step).get("ready")
            if ready and step.get("action") != "goto":
                readiness = self.wait_ready(ready, shown_ready)
            if readiness and readiness["unmet"]:
                outcome = {"status": "failed", "page_stable": False, "error": {
                    "kind": "not_ready",
                    "detail": f"not ready within {self.step_timeout_s:g}s: {'; '.join(readiness['unmet'])}"}}
            else:
                outcome = self.perform(step)
                if ready and step.get("action") == "goto" and outcome.get("status") == "passed":
                    readiness = self.wait_ready(ready, shown_ready)
                    if readiness["unmet"]:
                        outcome = {**outcome, "status": "failed", "page_stable": False, "error": {
                            "kind": "not_ready",
                            "detail": f"not ready within {self.step_timeout_s:g}s after navigation: {'; '.join(readiness['unmet'])}"}}
        except Exception as exc:
            kind = _error_kind(exc)
            if kind == "action_failed" and step.get("action") in ("expect_url", "expect_title"):
                kind = "harness_error"  # e.g. an invalid `matches` pattern: the spec, not the app
            outcome = {"status": "failed", "error": {"kind": kind, "detail": _cut(str(exc).splitlines()[0] if str(exc) else kind)}}
        finally:
            self.current_step = self.current_step_id = None
            self.last_step_id = step_id
        if readiness is not None:
            outcome["ready"] = readiness
        # Whatever this step's window caught belongs to this step, whether or not it is what
        # the step ends up failing for. Anything left unclaimed when the run ends had no
        # window open and is reported there instead of vanishing.
        #
        # A cleanup step claims nothing. Its failures are reported as `cleanup` events, which
        # the classifier reads as housekeeping and never lets decide a verdict — right for a
        # delete that could not be undone, wrong for a policy refusal, since a cleanup that
        # reached a denied endpoint reached it exactly as a test step would have. Left
        # unclaimed, it surfaces as `harness/navigation_blocked` and the run is `incomplete`.
        if self.phase != "cleanup":
            self._navigation_attributed.update(range(blocked_before, len(self.blocked)))
        if len(self.blocked) > blocked_before and outcome.get("status") != "failed":
            landed = self.blocked[-1]
            # The refusal in its own words: the origin policy and the deny list both end up
            # here, and "outside the origin policy" is a false explanation for an address
            # that was refused by name.
            refusal = self.blocked_detail.get(landed) or "refused by the /.verify-browser navigation policy"
            detail = (
                f"the page reached {_origin(landed)} before this run could refuse it (followed by the browser): {refusal}"
                if landed in self.redirected_off_policy
                else f"navigation to {_origin(landed)} blocked: {refusal}"
            )
            outcome = {**outcome, "status": "failed", "error": {"kind": "navigation_blocked", "detail": detail}}
        elif len(self.blocked) == blocked_before and (
            fresh := [m for m in self.mutations_blocked[mutations_before:] if m["resource_type"] != "ping"]
        ):
            # Overrides any outcome, a failed one included: a write the guard refused
            # explains whatever this step then saw, and leaving an `assertion` failure in
            # place would blame the application for the harness's own refusal. A beacon
            # (`ping`) never decides a step; it is reported after the run instead.
            for mutation in fresh:
                mutation["attributed"] = True
            first = fresh[0]
            if first.get("reason") == "redirect":
                detail = f"{first['method']} to {first['origin']} blocked: {first.get('refusal')}"
            elif first.get("refusal"):
                detail = (f"{first['method']} to {first['origin']} blocked: allowed_read_only_requests covers the endpoint,"
                          f" but the body is {first['refusal']}")
            else:
                detail = f"{first['method']} to {first['origin']} blocked: {_REFUSAL_TEXT[first['reason']]}"
            outcome = {**outcome, "status": "failed", "error": {"kind": "mutation_blocked", "detail": detail}}
        return outcome

    def cleanup_reserve_s(self, count: int) -> float:
        """Time held back from the test steps so cleanup can still run inside total_timeout_s:
        a readiness wait plus an action per cleanup step, never more than half the run. A
        starting point, not a calibrated figure."""
        if not count or self.total_timeout_s <= 0:
            return 0.0
        return min(self.total_timeout_s / 2, count * 2 * self.step_timeout_s)

    def run(self, scenario: dict) -> None:
        """Every step emits exactly one progress event; after the first failure the rest
        are `skipped` — a later step on a page that already diverged proves nothing. Then
        the cleanup steps run, whether the test passed or not, and finally the records
        that belong to the whole run: refused writes outside any step, confirmed reads,
        and writes still waiting for a response."""
        self.run_started = self.clock()
        cleanup = [s for s in scenario.get("cleanup") or [] if isinstance(s, dict)]
        reserve_s = self.cleanup_reserve_s(len(cleanup))
        failed = False
        stop_detail = None
        statuses: dict[str, str] = {}
        for index, step in enumerate(scenario.get("steps") or [], start=1):
            step_id = str(step.get("id") or f"step-{index}")
            event = {
                "type": "progress",
                "step": index,
                "step_id": step_id,
                "action": step.get("action"),
                "claim_id": step.get("claim_id"),
                "selector_provenance": None,
                "page_stable": None,
                "expected": None,
                "actual": None,
                "error": None,
            }
            if not failed and stop_detail is None and reserve_s and self.clock() - self.run_started >= self.total_timeout_s - reserve_s:
                stop_detail = f"stopped to keep {reserve_s:g}s of settings.total_timeout_s for cleanup"
            if failed or stop_detail:
                statuses[step_id] = "skipped"
                self.emit({**event, "status": "skipped", "duration_ms": 0, "url_after": self._page_url(),
                           **({"detail": stop_detail} if stop_detail else {})})
                continue
            started = self.clock()
            outcome = self._execute(step, step_id)
            duration_ms = int((self.clock() - started) * 1000)
            url_after = self._page_url()
            artifacts: list[dict] = []
            if outcome.get("status") == "failed":
                failed = self.failed = True
                if outcome.get("page_stable") is None:
                    outcome["page_stable"] = self.stable(step)
                artifacts = self.capture(index, outcome)
            statuses[step_id] = str(outcome.get("status"))
            self.emit({**event, **outcome, "duration_ms": duration_ms, "url_after": url_after})
            for artifact in artifacts:
                self.emit(artifact)
        self.run_cleanup(cleanup, statuses)
        self.report_unattributed_navigation()
        self.report_unattributed_mutations()
        self.report_read_only_requests()
        self.report_destructive_requests()
        self.report_uninspectable_writes()
        self.flush_requests()
        self.flush_network()

    def run_cleanup(self, cleanup: list[dict], statuses: dict[str, str]) -> None:
        """Cleanup steps, one `cleanup` event each, reported apart from the test.

        A step is `not_needed` when the step it cleans never ran (nothing was written); it is
        tried when that step passed or failed (a failed create may still have created), and
        `skipped` once an earlier step cleaning the same target failed. A cleanup failure
        never changes a test step's result — the report weighs it separately.
        """
        if not cleanup:
            return
        self.phase = "cleanup"
        broken: set[str] = set()
        for index, step in enumerate(cleanup, start=1):
            step_id = str(step.get("id") or f"cleanup-{index}")
            target = str(step.get("cleans") or "")
            event = {"type": "cleanup", "step_id": step_id, "cleans": target, "action": step.get("action"),
                     "expected": None, "actual": None, "error": None, "duration_ms": 0}
            if statuses.get(target) in (None, "skipped"):
                self.emit({**event, "status": "not_needed", "detail": f"'{target}' never ran, so it wrote nothing"})
                continue
            if target in broken:
                self.emit({**event, "status": "skipped", "detail": f"an earlier cleanup step for '{target}' failed"})
                continue
            started = self.clock()
            outcome = self._execute(step, step_id)
            if outcome.get("status") == "failed":
                broken.add(target)
            self.emit({**event, **outcome, "duration_ms": int((self.clock() - started) * 1000), "url_after": self._page_url()})
        self.phase = "steps"

    def report_read_only_requests(self) -> None:
        """Confirmed reads that went past the write guard: one observation per method and
        origin, with a count. A warning-class record, so the run shows what it let through."""
        counts: dict[tuple[str, str], int] = {}
        for item in self.read_only_allowed:
            key = (item["method"], item["origin"])
            counts[key] = counts.get(key, 0) + 1
        self.read_only_allowed = []
        for (method, origin), count in counts.items():
            self.emit(
                {
                    "type": "observation",
                    "kind": "read_only_request_allowed",
                    "url": origin,
                    "status": None,
                    "detail": f"{count} {method} request(s) passed as confirmed reads (settings.allowed_read_only_requests)",
                    "same_origin": self._same(origin),
                    "main_request": False,
                }
            )

    def report_destructive_requests(self) -> None:
        """Deletes this run refused for want of an approval, one observation per endpoint.

        Separate from `mutation_blocked` because this one is answerable: every other
        refusal says a policy forbids the request, while this says nobody has been asked
        yet. The runner turns these into the permission request the user replies to, so
        the endpoint travels in route form — `/api/items/:id`, never the record's id.
        """
        seen: set[tuple[str, str]] = set()
        for blocked in self.destructive_blocked:
            key = (blocked["method"], blocked["endpoint"])
            if key in seen:
                continue
            seen.add(key)
            self.emit(
                {
                    "type": "observation",
                    "kind": "destructive_unapproved",
                    "url": blocked["endpoint"],
                    "status": None,
                    "detail": f"{blocked['method']} {blocked['endpoint']} needs approval before it can run",
                    "same_origin": self._same(blocked["origin"]),
                    "main_request": False,
                }
            )

    def report_uninspectable_writes(self) -> None:
        """Writes refused for an unreadable body, one row per method+origin.

        Kept as its own observation rather than left to the blocked-mutation record: an
        upload that cannot be inspected is a scenario the user has to change something to
        run, and it reads differently from a write the policy simply did not cover.
        """
        counts: dict[tuple[str, str], int] = {}
        for item in self.uninspectable_writes:
            key = (item["method"], item["origin"])
            counts[key] = counts.get(key, 0) + 1
        self.uninspectable_writes = []
        for (method, origin), count in counts.items():
            self.emit(
                {
                    "type": "observation",
                    "kind": "write_uninspected",
                    "url": origin,
                    "status": None,
                    "detail": (
                        f"{count} {method} request(s) to {origin} were refused: their body "
                        "was one neither post_data nor post_data_buffer could produce, so "
                        "they could not be checked for a method override. No setting sends "
                        "them; a scenario that must upload runs through the project's own "
                        "test command instead"
                    ),
                    "same_origin": self._same(origin),
                    "main_request": False,
                }
            )

    def report_unattributed_navigation(self) -> None:
        """Off-policy landings no step's window was open for.

        A step fails on what its own window caught, by comparing a counter before and after
        the action. A navigation scheduled by that action — a debounced `pushState`, a meta
        refresh, a redirect the browser was still following — arrives after the comparison,
        so it was recorded and then read by nobody: the page had reached an address the
        project denied and the run still had no failed step to show for it.

        Reported as `harness`, not as an application failure. The application did nothing
        wrong; the run went somewhere it may not go, and a run that went off policy has not
        proven anything, whatever its assertions saw. `incomplete`, never `pass`.
        """
        for index, url in enumerate(self.blocked):
            if index in self._navigation_attributed:
                continue
            self._navigation_attributed.add(index)
            self.emit(
                {
                    "type": "harness",
                    "reason": "navigation_blocked",
                    "detail": (
                        f"the page reached {sanitize_endpoint(url)} outside any step's window: "
                        f"{self.blocked_detail.get(url) or 'refused by the /.verify-browser navigation policy'}"
                    ),
                }
            )

    def report_unattributed_mutations(self) -> None:
        """Writes refused outside any step's window (beacons, or a request fired after the
        last step returned) still happened and were still stopped: say so, as a warning."""
        for mutation in self.mutations_blocked:
            if mutation["attributed"]:
                continue
            mutation["attributed"] = True
            self.emit(
                {
                    "type": "observation",
                    "kind": "mutation_blocked",
                    "url": mutation["origin"],
                    "status": None,
                    "detail": f"{mutation['method']} {mutation['resource_type'] or 'request'} blocked: {mutation.get('refusal') or _REFUSAL_TEXT[mutation['reason']]}",
                    "same_origin": self._same(mutation["origin"]),
                    "main_request": False,
                }
            )


def _finish_trace(context, session: Session, tracing: bool, crashed: bool, artifacts_dir: str, has_secrets: bool, emit) -> None:
    """Keep the trace only when the run failed or crashed (plan §16); discard it otherwise.
    With resolved `${ENV}` values no trace was recorded — its snapshots hold typed input."""
    kept = session.failed or crashed
    if tracing and context is not None:
        try:
            if kept:
                path = Path(artifacts_dir) / "trace.zip"
                context.tracing.stop(path=str(path))
                emit({"type": "artifact", "kind": "trace", "name": "trace.zip", "step": None,
                      "bytes": path.stat().st_size if path.exists() else None})
            else:
                context.tracing.stop()
        except Exception as exc:
            emit({"type": "artifact", "kind": "error", "name": None, "step": None, "detail": _cut(f"trace: {exc}")})
    elif kept and has_secrets and artifacts_dir:
        emit({"type": "artifact", "kind": "skipped", "name": "trace.zip", "step": None,
              "detail": f"trace skipped: {SECRET_CAPTURE_NOTE}"})


def _launch_args(config: dict) -> list[str]:
    """Chromium flags that hold every approved write host at the address preflight saw.

    `--host-resolver-rules` replaces the browser's own lookup, so a name cannot come back
    pointing somewhere else mid-run. Preflight already refused the run if a pin was needed
    and the browser could not take one, so an empty list here means nothing needed pinning.
    """
    pins = config.get("host_pins") or {}
    if not pins or str(config.get("browser") or "chromium") != "chromium":
        return []
    rules = ", ".join(f"MAP {host} {f'[{address}]' if ':' in address else address}" for host, address in sorted(pins.items()))
    return [f"--host-resolver-rules={rules}"]


def run_scenario(scenario: dict, config: dict, artifacts_dir: str, emit, *, has_secrets: bool = False, display_scenario: dict | None = None) -> int:
    """Launch, run, close. Returns the player exit code; always ends with a `result` event."""
    def abort(reason: str, detail: str) -> int:
        emit({"type": "harness", "reason": reason, "detail": _cut(detail)})
        emit({"type": "result", "status": "aborted"})
        return 2

    browser_name = str(config.get("browser") or "chromium")
    if browser_name not in _BROWSERS:
        return abort("launch_failed", f"settings.browser '{browser_name}' is not one of {', '.join(_BROWSERS)}")
    try:
        from playwright.sync_api import sync_playwright  # lazy: optional extra
    except ImportError as exc:
        return abort("playwright_missing", f"playwright.sync_api import failed: {exc}")

    try:
        with sync_playwright() as pw:
            try:
                browser = getattr(pw, browser_name).launch(headless=bool(config.get("headless", False)), slow_mo=_slow_mo_ms(config), args=_launch_args(config))
            except Exception as exc:
                first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                reason = "browser_missing" if "Executable doesn't exist" in str(exc) else "launch_failed"
                return abort(reason, first)
            context = None
            session = Session(None, config, emit, artifacts_dir=artifacts_dir, has_secrets=has_secrets, display_scenario=display_scenario)
            tracing = crashed = False
            try:
                context = browser.new_context()
                context.set_default_timeout(session.step_timeout_ms)
                context.set_default_navigation_timeout(session.nav_timeout_ms)
                if artifacts_dir and not has_secrets:
                    context.tracing.start(screenshots=True, snapshots=True)
                    tracing = True
                session.page = context.new_page()
                context.route("**/*", session.guard)
                context.on("request", session.on_request)
                context.on("response", session.on_network_response)
                context.on("requestfinished", session.on_network_finished)
                context.on("requestfailed", session.on_network_failed)
                session.page.on("framenavigated", session.on_frame_navigated)
                session.page.on("console", session.on_console)
                session.page.on("pageerror", session.on_page_error)
                session.page.on("response", session.on_response)
                session.page.on("requestfinished", session.on_request_finished)
                session.page.on("requestfailed", session.on_request_failed)
                session.run(scenario)
            except Exception:
                crashed = True
                raise
            finally:
                if crashed:
                    # A run that crashed mid-request still sent what it sent; the ledger says so.
                    # Writes that went out unchecked count for the same reason: a crash is
                    # not a reason for the one unverified request to go unmentioned.
                    try:
                        session.report_uninspectable_writes()
                    except Exception:
                        pass
                    try:
                        session.flush_requests()
                    except Exception:
                        pass
                    try:
                        session.flush_network()
                    except Exception:
                        pass
                _finish_trace(context, session, tracing, crashed, artifacts_dir, has_secrets, emit)
                for closable in (context, browser):
                    try:
                        if closable is not None:
                            closable.close()
                    except Exception:
                        pass
    except Exception as exc:  # driver crash, closed pipe: the run cannot vouch for anything
        return abort("harness_error", f"{type(exc).__name__}: {exc}")
    emit({"type": "result", "status": "finished"})
    return 0
