"""The real player's step semantics, against a stand-in page — no browser, no Playwright.

What is pinned here is the contract the classifier depends on: one progress event per
step, the right error kind, the strongest candidate's provenance on a miss, page
stability on failure, observations shaped like classify.py expects, and the origin
guard refusing what the spec policy refuses. A real Chromium run lives in the smoke
suite; this proves the logic that run relies on.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from exoma.core.evidence.e2e import browser as e2e_browser
from exoma.core.evidence.e2e.browser import Session, run_scenario
from exoma.core.evidence.e2e.classify import ORIGIN_APP, ORIGIN_HARNESS, ORIGIN_SCENARIO, ORIGIN_UNKNOWN, build_report, classify_step
from exoma.core.evidence.e2e.normalize import evidence_block
from exoma.core.evidence.e2e.spec import e2e_css
from tests.checks.support import assert_true


def _css_match(element: dict, css: str) -> bool:
    """A fake element matches a CSS selector by its `css`, or by its `e2e` attribute when
    the selector is the one the runtime builds for an `e2e` key. There is no
    `get_by_test_id` on the fake: a runtime that reached for it would fail here."""
    return element.get("css") == css or (element.get("e2e") is not None and e2e_css(element["e2e"]) == css)

BASE = "http://localhost:8000"
FakeTimeout = type("TimeoutError", (Exception,), {})


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _flag(page, element: dict, key: str) -> bool:
    """An element flag, or a callable of the page for one that changes over (fake) time."""
    value = element.get(key, True)
    return bool(value(page) if callable(value) else value)


class _Locator:
    def __init__(self, page, predicate) -> None:
        self.page = page
        self.predicate = predicate

    def _matches(self) -> list[dict]:
        return [el for el in self.page.elements if self.predicate(el)]

    def count(self) -> int:
        return len(self._matches())

    def nth(self, index: int) -> "_Locator":
        return _Locator(self.page, lambda el, index=index: el is self._matches()[index])

    def is_visible(self) -> bool:
        return _flag(self.page, self._matches()[0], "visible")

    def is_enabled(self) -> bool:
        return _flag(self.page, self._matches()[0], "enabled")

    def _inside(self, predicate) -> "_Locator":
        # A child is an element whose "in" names a matched container.
        return _Locator(self.page, lambda el: predicate(el) and any(el.get("in") is not None and el.get("in") == c.get("name") for c in self._matches()))

    def get_by_role(self, role, name=None):
        return self._inside(lambda el: el.get("role") == role and (name is None or el.get("name") == name))

    def get_by_label(self, label):
        return self._inside(lambda el: el.get("label") == label)

    def get_by_text(self, text):
        return self._inside(lambda el: el.get("text") == text)

    def locator(self, css):
        return self._inside(lambda el: _css_match(el, css))

    def _one(self) -> dict:
        element = self._matches()[0]
        if not element.get("actionable", True):
            raise FakeTimeout("Timeout 8000ms exceeded waiting for element to be enabled")
        return element

    def click(self, timeout=None) -> None:
        element = self._one()
        self.page.clicks.append(element.get("e2e") or element.get("name"))
        if element.get("on_click"):
            element["on_click"](self.page)

    def fill(self, value, timeout=None) -> None:
        self._one()["value"] = value

    def select_option(self, value=None, label=None, timeout=None) -> None:
        self._one()["selected"] = value if value is not None else label

    def press(self, key, timeout=None) -> None:
        self._one()["pressed"] = key

    def inner_text(self) -> str:
        return self._matches()[0].get("inner_text", "")


class _Page:
    def __init__(self, elements: list[dict] | None = None, *, title: str = "Home", settled: bool = True) -> None:
        self.elements = elements or []
        self.url = "about:blank"
        self._title = title
        self.settled = settled
        self.clicks: list = []
        self.gotos: list[str] = []
        self.main_frame = object()
        self.goto_error: Exception | None = None
        self.calls: list[str] = []
        self.html = "<html><body><p>page</p></body></html>"
        self.handlers: dict = {}
        self.load_states: list[str] = []

    def goto(self, url, wait_until=None, timeout=None):
        self.gotos.append(url)
        if self.goto_error:
            raise self.goto_error
        self.url = url
        return SimpleNamespace(status=200)

    def title(self) -> str:
        return self._title

    def evaluate(self, script, arg=None):
        if "readyState" in script:
            return "complete"
        if "removeAttribute" in script:
            self.calls.append("clear_inputs")
            return None
        self.calls.append("probe")
        return {"url": self.url, "limit": arg, "headings": ["Masuk"], "buttons": [{"name": "Masuk", "e2e": "login-submit"}], "inputs": [], "links": []}

    def content(self) -> str:
        self.calls.append("content")
        return self.html

    def screenshot(self, path=None, **_kwargs) -> None:
        self.calls.append("screenshot")
        Path(path).write_bytes(b"\x89PNG fake")

    def on(self, name, handler) -> None:
        self.handlers[name] = handler

    def wait_for_load_state(self, state, timeout=None) -> None:
        self.load_states.append(state)
        if not self.settled:
            raise FakeTimeout(f"{state} not reached")

    def get_by_role(self, role, name=None):
        return _Locator(self, lambda el: el.get("role") == role and (name is None or el.get("name") == name))

    def get_by_label(self, label):
        return _Locator(self, lambda el: el.get("label") == label)

    def get_by_text(self, text):
        return _Locator(self, lambda el: el.get("text") == text)

    def locator(self, css):
        return _Locator(self, lambda el: _css_match(el, css))


def _session(page: _Page, **config) -> tuple[Session, list[dict], _Clock]:
    events: list[dict] = []
    clock = _Clock()
    merged = {"base_url": BASE, "step_timeout_ms": 500, "nav_timeout_ms": 1000, "probe_max_elements": 7, **config}
    return Session(page, merged, events.append, clock=clock, sleep=clock.advance), events, clock


def _progress(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] == "progress"]


def _login_page() -> _Page:
    def to_dashboard(page: _Page) -> None:
        page.url = BASE + "/dashboard"
        page._title = "Dashboard"

    return _Page(
        [
            {"role": "textbox", "label": "Email"},
            {"role": "button", "name": "Masuk", "e2e": "login-submit", "on_click": to_dashboard},
            {"e2e": "banner", "inner_text": "Welcome back, operator"},
        ]
    )


class _DispatchPage(_Page):
    """A page that behaves like the sync Playwright API: events queued by the browser are
    delivered only while the caller is inside a Playwright call. `wait_for_timeout` is one;
    reading `page.url` is not."""

    def __init__(self, clock: _Clock, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.clock = clock
        self.pending: list = []

    def wait_for_timeout(self, ms) -> None:
        self.clock.advance(ms / 1000)
        while self.pending:
            self.pending.pop(0)(self)


class _TickingClock(_Clock):
    """Moves a little on every read, so a poll that never reaches `wait_for_timeout` still
    hits its deadline and fails — instead of hanging the suite on a clock nothing advances."""

    def __call__(self) -> float:
        self.advance(0.01)
        return super().__call__()


def _check_polling_keeps_events_flowing() -> None:
    """A redirect that arrives as an event must reach a polled `expect_url` or `url` readiness.

    Regression: the poll slept with `time.sleep` and read the cached `page.url`, so nothing
    dispatched for the whole step timeout — a login POST held by the write guard finished
    just after the assertion had failed, whatever the timeout was."""

    def land(page: _Page) -> None:
        page.url = BASE + "/home"

    config = {"base_url": BASE, "step_timeout_ms": 500, "nav_timeout_ms": 1000}
    step = {"id": "after-login", "action": "expect_url", "contains": "/home"}

    clock = _TickingClock()
    page = _DispatchPage(clock)
    page.url = BASE + "/login"
    page.pending.append(land)
    result = Session(page, config, [].append, clock=clock).perform(step)
    assert_true(result["status"] == "passed", f"expect_url sees a navigation delivered while it waits: {result}")

    clock = _TickingClock()
    page = _DispatchPage(clock)
    page.url = BASE + "/login"
    page.pending.append(land)
    ready = Session(page, config, [].append, clock=clock).wait_ready([{"url": "/home"}])
    assert_true(ready["unmet"] == [], f"a url readiness condition sees it too: {ready}")

    # The contrast that makes the check mean something: a wait outside Playwright never
    # delivers the event, so the same step fails on the same page.
    clock = _TickingClock()
    page = _DispatchPage(clock)
    page.url = BASE + "/login"
    page.pending.append(land)
    starved = Session(page, config, [].append, clock=clock, sleep=clock.advance).perform(step)
    assert_true(starved["status"] == "failed" and page.pending, f"a non-dispatching wait starves the event: {starved}")


_CLAIMS = [{"id": "login", "severity": "blocking", "source_refs": ["src/Login.tsx:1"]}]


def _test_e2e_browser_session() -> None:
    _check_polling_keeps_events_flowing()

    # --- a passing flow: every action, one event per step, no typed value on the wire --------------
    page = _login_page()
    session, events, _ = _session(page)
    scenario = {
        "claims": _CLAIMS,
        "steps": [
            {"action": "goto", "url": "/login"},
            {"action": "fill", "selector": {"label": "Email"}, "selector_provenance": {"type": "source"}, "value": "typed-secret-value"},
            {"action": "click", "selector_candidates": [
                {"selector": {"role": "button", "name": "Sign in"}, "selector_provenance": {"type": "source"}},
                {"selector": {"e2e": "login-submit"}, "selector_provenance": {"type": "existing_test"}},
            ]},
            {"action": "expect_url", "contains": "/dashboard", "claim_id": "login"},
            {"action": "expect_title", "equals": "Dashboard", "claim_id": "login"},
            {"action": "expect_dom", "selector": {"e2e": "banner"}, "text": "Welcome back", "claim_id": "login"},
            {"action": "probe"},
        ],
    }
    session.run(scenario)
    progress = _progress(events)
    assert_true([e["status"] for e in progress] == ["passed"] * 7, f"every step passes: {[(e['action'], e['status'], e.get('error')) for e in progress]}")
    assert_true(page.gotos == [BASE + "/login"], f"a relative goto resolves under base_url: {page.gotos}")
    assert_true(progress[2]["selector_provenance"] == "existing_test", "the candidate that matched reports its own provenance")
    assert_true(
        progress[2]["selection"] == {"candidate": 1, "match_counts": [0, 1], "fallback_used": True},
        f"the runtime mapping names the candidate used, each candidate's match count, and the fallback: {progress[2]['selection']}",
    )
    assert_true([e["step_id"] for e in progress][:2] == ["step-1", "step-2"], "a step without an id still gets a stable one in events")
    assert_true("typed-secret-value" not in json.dumps(events), "a typed value never comes back on the wire")
    assert_true(progress[6]["actual"]["probe"]["limit"] == 7, "the probe is capped by settings.probe_max_elements")
    report = build_report([*events, {"type": "result", "status": "finished"}], scenario)
    assert_true(report["browser_verdict"] == "pass", f"the classifier reads the stream as pass: {report['reason']}")

    # --- a grounded selector gone from a settled page is the app's; the rest is skipped ----------
    page = _login_page()
    session, events, _ = _session(page)
    session.run({"steps": [
        {"action": "click", "selector": {"role": "button", "name": "Logout"}, "selector_provenance": {"type": "source"}},
        {"action": "expect_url", "contains": "/", "claim_id": "login"},
    ]})
    first, second = _progress(events)
    assert_true(first["error"]["kind"] == "selector_missing" and first["page_stable"] is True, f"missing + settled: {first}")
    assert_true(classify_step(first) == ORIGIN_APP, "grounded + settled classifies as app")
    assert_true(second["status"] == "skipped", "a step after a failure proves nothing and is skipped")

    # --- every candidate missing: the failure carries the STRONGEST candidate's provenance -------
    session, events, _ = _session(_login_page())
    session.run({"steps": [{"action": "click", "selector_candidates": [
        {"selector": {"role": "button", "name": "Logout"}, "selector_provenance": {"type": "source"}},
        {"selector": {"css": "#logout"}, "selector_provenance": {"type": "heuristic"}},
    ]}]})
    missed = _progress(events)[0]
    assert_true(missed["selector_provenance"] == "source" and classify_step(missed) == ORIGIN_APP, f"a grounded selector that vanished is not excused by a heuristic fallback: {missed}")

    # --- the same miss on a heuristic selector is the scenario's; on an unsettled page, unknown ---
    session, events, _ = _session(_login_page())
    session.run({"steps": [{"action": "click", "selector": {"css": "#logout"}, "selector_provenance": {"type": "heuristic"}}]})
    assert_true(classify_step(_progress(events)[0]) == ORIGIN_SCENARIO, "a heuristic miss is the scenario's")
    session, events, _ = _session(_Page([{"role": "button", "name": "Masuk"}], settled=False))
    session.run({"steps": [{"action": "click", "selector": {"role": "button", "name": "Logout"}, "selector_provenance": {"type": "source"}}]})
    assert_true(_progress(events)[0]["page_stable"] is False and classify_step(_progress(events)[0]) == ORIGIN_UNKNOWN, "unsettled page: unknown")

    # --- ambiguity, hidden elements, non-actionable elements --------------------------------------
    session, events, _ = _session(_Page([{"role": "button", "name": "Save"}, {"role": "button", "name": "Save"}]))
    session.run({"steps": [{"action": "click", "selector": {"role": "button", "name": "Save"}}]})
    assert_true(_progress(events)[0]["error"]["kind"] == "selector_ambiguous", "two matches are ambiguous, not a coin flip")
    assert_true(_progress(events)[0]["selection"] == {"candidate": None, "match_counts": [2], "fallback_used": False}, f"and the report says how many matched: {_progress(events)[0]['selection']}")

    # --- `within` scopes a selector: the modal's Simpan, not the page's ---------------------------
    page = _Page([
        {"role": "button", "name": "Simpan"},
        {"role": "dialog", "name": "Tambah barang"},
        {"role": "button", "name": "Simpan", "in": "Tambah barang", "e2e": "modal-save"},
    ])
    session, events, _ = _session(page)
    session.run({"steps": [{"id": "save", "action": "click", "selector": {"role": "button", "name": "Simpan"}, "within": {"role": "dialog", "name": "Tambah barang"}}]})
    assert_true(_progress(events)[0]["status"] == "passed" and page.clicks == ["modal-save"], f"the scoped selector clicks the modal's button only: {page.clicks} {_progress(events)[0].get('error')}")

    # --- a timed-out write action is never sent twice ----------------------------------------------
    def dispatched_then_timeout(p: _Page) -> None:
        raise FakeTimeout("Timeout 500ms exceeded waiting for navigation after click")

    page = _Page([{"role": "button", "name": "Kirim", "on_click": dispatched_then_timeout}])
    session, events, _ = _session(page)
    session.run({"steps": [{"id": "send", "action": "click", "selector": {"role": "button", "name": "Kirim"}}, {"id": "after", "action": "expect_url", "contains": "/", "claim_id": "login"}]})
    assert_true(page.clicks == ["Kirim"] and _progress(events)[0]["status"] == "failed", f"one dispatch, then a failed step with evidence: {page.clicks}")
    assert_true(_progress(events)[1]["status"] == "skipped", "and nothing after it runs")

    # --- readiness: wait for the app's own indicator, then act once --------------------------------
    page = _Page()
    session, events, clock = _session(page, step_timeout_ms=2000)
    page.elements = [
        {"e2e": "loader", "visible": lambda p: clock.now < 0.5},
        {"role": "button", "name": "Simpan", "enabled": lambda p: clock.now >= 0.3},
        {"text": "3 items"},
    ]
    button = {"role": "button", "name": "Simpan"}
    session.run({"steps": [
        {"id": "open", "action": "goto", "url": "/items", "ready": [{"text": "3 items"}]},
        {"id": "save", "action": "click", "selector": button, "ready": [{"hidden": {"e2e": "loader"}}, {"enabled": button}]},
    ]})
    opened, saved = _progress(events)
    assert_true(opened["status"] == "passed" and opened["ready"]["unmet"] == [], f"a goto waits for its content after navigating: {opened}")
    assert_true(
        saved["status"] == "passed" and saved["ready"]["waited_ms"] >= 500 and saved["ready"]["unmet"] == [] and page.clicks == ["Simpan"],
        f"the click waited for the loader to go and the button to enable, then ran once: {saved.get('ready')} {page.clicks}",
    )
    page = _Page()
    session, events, clock = _session(page, step_timeout_ms=1000)
    page.elements = [{"e2e": "loader"}, {"role": "button", "name": "Simpan"}]
    session.run({"steps": [{"id": "save", "action": "click", "selector": button, "ready": [{"hidden": {"e2e": "loader"}}]}]})
    stuck = _progress(events)[0]
    assert_true(
        stuck["error"]["kind"] == "not_ready" and "hidden" in stuck["error"]["detail"] and stuck["page_stable"] is False and page.clicks == [],
        f"a loader that never goes fails the step before any action, naming the condition: {stuck}",
    )
    assert_true(classify_step(stuck) == ORIGIN_UNKNOWN, "a page that never became ready is unknown, not the app's")
    assert_true("networkidle" not in page.load_states, f"stability never waits for network idle (a polling page never reaches it): {page.load_states}")
    session, events, _ = _session(_Page([{"e2e": "toast", "visible": False}]))
    session.run({"steps": [{"action": "expect_dom", "selector": {"e2e": "toast"}, "claim_id": "login"}]})
    assert_true(_progress(events)[0]["error"]["kind"] == "not_visible", "an element that stays hidden fails expect_dom as not_visible")
    session, events, _ = _session(_Page([{"role": "button", "name": "Pay", "actionable": False}]))
    session.run({"steps": [{"action": "click", "selector": {"role": "button", "name": "Pay"}, "selector_provenance": {"type": "source"}}]})
    assert_true(_progress(events)[0]["error"]["kind"] == "not_visible", "resolved but never actionable reports not_visible")

    # --- assertions poll, then fail with expected vs actual; long polls keep the wire alive ------
    page = _Page()
    page.url = BASE + "/login?error=1"
    session, events, _ = _session(page, step_timeout_ms=5000)
    session.run({"steps": [{"action": "expect_url", "contains": "/dashboard", "claim_id": "login"}]})
    failed = _progress(events)[0]
    assert_true(failed["error"]["kind"] == "assertion" and failed["actual"] == {"url": BASE + "/login?error=1"}, f"url assertion: {failed}")
    assert_true(failed["expected"] == {"contains": "/dashboard"} and classify_step(failed) == ORIGIN_APP, "expected is carried, origin app")
    assert_true(any(e["type"] == "heartbeat" for e in events), "a 5s poll emits heartbeats so the idle timeout does not fire")
    session, events, _ = _session(_Page())
    session.run({"steps": [{"action": "expect_title", "matches": "([unclosed", "claim_id": "login"}]})
    assert_true(classify_step(_progress(events)[0]) == ORIGIN_HARNESS, "an invalid pattern is the spec's fault, not the app's")
    page = _Page([{"e2e": "banner", "inner_text": "Goodbye"}])
    session, events, _ = _session(page)
    session.run({"steps": [{"action": "expect_dom", "selector": {"e2e": "banner"}, "text": "Welcome", "claim_id": "login"}]})
    assert_true(_progress(events)[0]["error"]["kind"] == "assertion" and _progress(events)[0]["actual"] == {"text": "Goodbye"}, "element text mismatch is an assertion")

    # --- navigation: timeouts are unknown; the origin guard is harness, before and after goto -----
    page = _Page()
    page.goto_error = FakeTimeout("Timeout 1000ms exceeded")
    session, events, _ = _session(page)
    session.run({"steps": [{"action": "goto", "url": "/slow"}]})
    assert_true(_progress(events)[0]["error"]["kind"] == "timeout" and classify_step(_progress(events)[0]) == ORIGIN_UNKNOWN, "a navigation timeout is unknown")
    page = _Page()
    session, events, _ = _session(page)
    session.run({"steps": [{"action": "goto", "url": "https://evil.example/"}]})
    assert_true(page.gotos == [] and _progress(events)[0]["error"]["kind"] == "navigation_blocked", "an off-origin goto never reaches the page")

    routes: list[str] = []
    route = SimpleNamespace(abort=lambda code: routes.append(f"abort:{code}"), continue_=lambda: routes.append("continue"))
    page = _Page()
    session, events, _ = _session(page)

    def request(url, *, navigation=True, main=True):
        return SimpleNamespace(url=url, is_navigation_request=lambda: navigation, frame=page.main_frame if main else object())

    session.guard(route, request("https://evil.example/phish"))
    session.guard(route, request(BASE + "/dashboard"))
    session.guard(route, request("https://cdn.example/app.js", navigation=False))
    session.guard(route, request("https://video.example/embed", main=False))
    assert_true(routes == ["abort:blockedbyclient", "continue", "continue", "continue"], f"only top-level off-origin navigation is refused: {routes}")

    def redirect_off_origin(p: _Page) -> None:
        session.guard(route, request("https://sso.evil.example/login"))

    page.elements = [{"role": "button", "name": "Continue", "on_click": redirect_off_origin}]
    session.run({"steps": [{"action": "click", "selector": {"role": "button", "name": "Continue"}, "selector_provenance": {"type": "source"}}]})
    blocked = _progress(events)[0]
    assert_true(blocked["error"]["kind"] == "navigation_blocked" and classify_step(blocked) == ORIGIN_SCENARIO, f"a click that navigates off-origin fails as the scenario's: {blocked}")
    assert_true("sso.evil.example" in blocked["error"]["detail"] and "/login" not in blocked["error"]["detail"], "the detail names the origin, not the full URL")

    # --- write guard: allow_side_effects false refuses non-GET/HEAD/OPTIONS on any origin -------------
    def call(method, url, *, resource_type="fetch", navigation=False, **config):
        seen: list[str] = []
        hop = SimpleNamespace(abort=lambda code: seen.append(f"abort:{code}"), continue_=lambda: seen.append("continue"))
        guarded, emitted, _ = _session(_Page(), **config)
        guarded.guard(hop, SimpleNamespace(url=url, method=method, resource_type=resource_type,
                                           is_navigation_request=lambda: navigation, frame=object()))
        guarded.ledger = emitted
        return seen, guarded

    for method in ("GET", "HEAD", "OPTIONS", "get"):
        seen, guarded = call(method, BASE + "/api/items")
        assert_true(seen == ["continue"] and guarded.ledger == [], f"{method} is a read, passes, and is not a ledger entry")
    # allow_local_side_effects is on by default, so a write to a development host goes
    # through without the global switch. Turning it off restores the old blanket refusal.
    for method in ("POST", "PUT", "PATCH"):
        seen, _ = call(method, BASE + "/api/items/7")
        assert_true(seen == ["continue"], f"{method} to a local host writes by default: {seen}")
        seen, guarded = call(method, BASE + "/api/items/7?token=abc", allow_local_side_effects=False)
        assert_true(seen == ["abort:blockedbyclient"], f"{method} is refused with local writes off: {seen}")
        assert_true(
            guarded.mutations_blocked == [{"method": method, "origin": BASE, "resource_type": "fetch", "attributed": False, "reason": "side_effects_off"}],
            f"the record keeps the origin, never the path or query: {guarded.mutations_blocked}",
        )
        record = guarded.ledger[0]
        assert_true(
            record["type"] == "request" and record["blocked"] and record["endpoint"] == BASE + "/api/items/:id" and "token" not in json.dumps(record)
            and record["attribution"] == "uncertain" and record["planned"] is None and record["duration_ms"] == 0,
            f"a refused write enters the ledger at once, route kept, id and query gone: {record}",
        )

    # --- a delete is its own rung: local is not enough, and neither is allow_side_effects --
    for config in ({}, {"allow_side_effects": True}, {"allow_local_side_effects": True}):
        seen, guarded = call("DELETE", BASE + "/api/items/7", **config)
        assert_true(
            seen == ["abort:blockedbyclient"]
            and guarded.mutations_blocked[0]["reason"] == "destructive_unapproved",
            f"a delete is refused until its endpoint is approved, whatever else is on ({config}): "
            f"{seen} {guarded.mutations_blocked}",
        )
        assert_true(
            guarded.destructive_blocked
            and guarded.destructive_blocked[0]["method"] == "DELETE",
            f"the refusal names the delete so the user can approve that one endpoint: "
            f"{guarded.destructive_blocked}",
        )
    assert_true(
        call("DELETE", BASE + "/api/items/7", allowed_destructive_requests=["DELETE /api/items/7"])[0] == ["continue"],
        "an approved delete endpoint goes through",
    )
    assert_true(
        call("DELETE", BASE + "/api/items/8", allowed_destructive_requests=["DELETE /api/items/7"])[0] == ["abort:blockedbyclient"],
        "approval is per endpoint: the neighbouring record was never agreed to",
    )

    # --- a delete spelled as a POST is still a delete -----------------------------------
    for body, label in (
        ("_method=DELETE&id=7", "a Laravel/Rails form override"),
        ('{"_method":"DELETE"}', "a JSON override field"),
    ):
        seen: list[str] = []
        hop = SimpleNamespace(abort=lambda code: seen.append(f"abort:{code}"), continue_=lambda: seen.append("continue"))
        guarded, _emitted, _ = _session(_Page())
        guarded.guard(hop, SimpleNamespace(url=BASE + "/api/items/7", method="POST", resource_type="fetch",
                                           is_navigation_request=lambda: False, frame=object(),
                                           post_data=body, headers={}))
        assert_true(
            seen == ["abort:blockedbyclient"]
            and guarded.mutations_blocked[0]["reason"] in ("destructive_unapproved", "override_unapproved"),
            f"{label} must not ride through the door opened for local writes: {seen} {guarded.mutations_blocked}",
        )
    def overridden(url, method, *, post_data=None, headers=None, **config):
        seen: list[str] = []
        hop = SimpleNamespace(abort=lambda code: seen.append(f"abort:{code}"), continue_=lambda: seen.append("continue"))
        guarded, _emitted, _ = _session(_Page(), **config)
        guarded.guard(hop, SimpleNamespace(url=url, method=method, resource_type="fetch",
                                           is_navigation_request=lambda: False, frame=object(),
                                           post_data=post_data, headers=headers or {}))
        return seen, guarded

    seen, guarded = overridden(BASE + "/api/items/7", "POST", post_data="id=7",
                               headers={"X-HTTP-Method-Override": "DELETE"})
    assert_true(
        seen == ["abort:blockedbyclient"] and guarded.mutations_blocked[0]["reason"] in ("destructive_unapproved", "override_unapproved"),
        f"a header override is read too: {seen} {guarded.mutations_blocked}",
    )
    # An override is resolved BEFORE the safe-method exit. Reading the wire method first
    # let a delete leave through the door held open for reads.
    for label, kwargs in (
        ("a query override on a GET", {"url": BASE + "/api/items/7?_method=DELETE", "method": "GET"}),
        ("a header override on a GET", {"url": BASE + "/api/items/7", "method": "GET",
                                        "headers": {"X-HTTP-Method-Override": "DELETE"}}),
    ):
        seen, guarded = overridden(**kwargs)
        assert_true(
            seen == ["abort:blockedbyclient"] and guarded.mutations_blocked[0]["reason"] in ("destructive_unapproved", "override_unapproved"),
            f"{label} must not pass as a read: {seen} {guarded.mutations_blocked}",
        )
    # The value is not read, so a harmless-looking one is not a way past the gate either.
    # An earlier design ranked values and spent six verification rounds on which rank a
    # given spelling deserved; this one asks whether the field is there and stops.
    seen, guarded = overridden(BASE + "/api/items", "POST", post_data="_method=GET&q=1")
    assert_true(
        seen == ["abort:blockedbyclient"]
        and guarded.mutations_blocked[0]["reason"] == "override_unapproved",
        f"a request carrying the field needs approval whatever the field says: "
        f"{seen} {guarded.mutations_blocked}",
    )
    for label, kwargs in (
        ("a body override", {"post_data": "_method=POST"}),
        ("a header override", {"headers": {"X-HTTP-Method-Override": "POST"}}),
    ):
        seen, guarded = overridden(BASE + "/api/items/7", "DELETE", **kwargs)
        assert_true(
            seen == ["abort:blockedbyclient"]
            and guarded.mutations_blocked[0]["reason"] in ("destructive_unapproved", "override_unapproved"),
            f"a DELETE demoted to POST by {label} is still a delete: {seen} {guarded.mutations_blocked}",
        )

    # A body the harness cannot read cannot be cleared of carrying `_method=DELETE`, so it
    # does not get the permission that exists because deletes can be ruled out. It falls
    # back to allow_side_effects, which is where uploads were handled before any of this.
    # A percent-encoded key decodes to `_method` at the backend like any other, so the
    # override check runs the parser rather than testing the raw string for "_method=".
    seen, guarded = overridden(BASE + "/api/items/7?%5Fmethod=DELETE", "GET")
    assert_true(
        seen == ["abort:blockedbyclient"]
        and guarded.mutations_blocked[0]["reason"] in ("destructive_unapproved", "override_unapproved"),
        f"an encoded override key is still an override: {seen} {guarded.mutations_blocked}",
    )

    class _MultipartBody:
        """A request Playwright will not decode as text but will hand over as bytes —
        which is what an ordinary file upload looks like at this guard."""

        url = BASE + "/upload"
        method = "POST"
        resource_type = "fetch"
        headers: dict = {}
        frame = object()

        def __init__(self, buffer):
            self._buffer = buffer

        @property
        def post_data(self):
            raise ValueError("multipart body is not text")

        @property
        def post_data_buffer(self):
            if self._buffer is None:
                raise ValueError("no buffer either")
            return self._buffer

        def is_navigation_request(self):
            return False

    def multipart(buffer, **config):
        seen: list[str] = []
        hop = SimpleNamespace(abort=lambda code: seen.append(f"abort:{code}"), continue_=lambda: seen.append("continue"))
        guarded, _emitted, _ = _session(_Page(), **config)
        guarded.guard(hop, _MultipartBody(buffer))
        return seen, guarded

    upload = b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n\r\n\x89PNG\r\n--x--'
    assert_true(
        multipart(upload)[0] == ["continue"],
        "a real multipart upload to a local host still writes: the bytes are readable "
        "even when the text accessor refuses them",
    )
    seen, guarded = multipart(
        b'--x\r\nContent-Disposition: form-data; name="_method"\r\n\r\nDELETE\r\n--x--'
    )
    assert_true(
        seen == ["abort:blockedbyclient"]
        and guarded.mutations_blocked[0]["reason"] in ("destructive_unapproved", "override_unapproved"),
        f"an upload envelope hiding a delete does not pass as a write: {seen} {guarded.mutations_blocked}",
    )
    # Neither accessor works: the body was never cleared, so it does not get the new
    # local permission — and the broad opt-in does not rescue it either.
    seen, guarded = multipart(None)
    assert_true(
        seen == ["abort:blockedbyclient"]
        and guarded.mutations_blocked[0]["reason"] == "side_effects_off",
        f"a wholly unreadable body does not receive the local-write permission: {seen} {guarded.mutations_blocked}",
    )
    seen, guarded = multipart(None, allow_side_effects=True)
    assert_true(
        seen == ["abort:blockedbyclient"]
        and guarded.mutations_blocked
        and guarded.mutations_blocked[0]["reason"] == "uninspectable_body"
        and guarded.uninspectable_writes,
        f"under allow_side_effects it is refused, not sent, and says why: "
        f"{seen} {guarded.mutations_blocked} {guarded.uninspectable_writes}",
    )
    # The refusal has to be honest about there being no way out. `allow_side_effects` is
    # the switch the caller already threw, and an endpoint approval cannot send this
    # either: that list takes DELETEs and this request never reaches the rule that reads
    # it. What is left is the project's own test command, so that is what it names.
    refusal_text = e2e_browser._REFUSAL_TEXT["uninspectable_body"]
    assert_true(
        "existing_test_command" in refusal_text,
        f"the refusal points at the one thing that can still do this: {refusal_text}",
    )
    assert_true(
        "No run setting sends it" in refusal_text,
        f"and says plainly that nothing turns this into a sent request: {refusal_text}",
    )
    # And nothing changed for a body that CAN be read: the same switch, an ordinary
    # write, still goes. Fail-closed on the unreadable case only.
    seen, guarded = multipart(b"name=value", allow_side_effects=True)
    assert_true(
        seen == ["continue"] and not guarded.uninspectable_writes,
        f"a readable body under allow_side_effects is unaffected: {seen} {guarded.uninspectable_writes}",
    )
    # An upload whose FILENAME happens to spell the override field IS refused. The rule
    # asks only whether the field is present, so this is the cost side of that trade: a
    # refusal naming the endpoint, in exchange for a rule with nothing left to interpret.
    seen, guarded = multipart(
        b'--x\r\nContent-Disposition: form-data; name="file"; '
        b'filename="_method=DELETE.txt"\r\n\r\ndata\r\n--x--'
    )
    assert_true(
        seen == ["abort:blockedbyclient"]
        and guarded.mutations_blocked[0]["reason"] == "override_unapproved",
        f"anything spelling the field asks for approval, filenames included: "
        f"{seen} {guarded.mutations_blocked}",
    )
    # The value is never read, so a harmless one is not a way through. This is the whole
    # point of the rewrite: six rounds of verification went on deciding what values meant.
    for label, url, method, kwargs in (
        ("a harmless value", BASE + "/api/items?_method=GET", "GET", {}),
        ("a duplicated field", BASE + "/api/items?_method=GET&_method=DELETE", "GET", {}),
        ("a demoted delete", BASE + "/api/items/7", "DELETE", {"post_data": "_method=POST"}),
        ("a folded header", BASE + "/api/items", "GET",
         {"headers": {"X-HTTP-Method-Override": "POST, DELETE"}}),
        ("a header with nonsense in it", BASE + "/api/items", "GET",
         {"headers": {"X-HTTP-Method-Override": "not-a-method"}}),
    ):
        seen, guarded = overridden(url, method, **kwargs)
        assert_true(
            seen == ["abort:blockedbyclient"] and guarded.mutations_blocked,
            f"{label}: a request carrying the field needs approval whatever it says: "
            f"{seen} {guarded.mutations_blocked}",
        )

    # The local permission was described, and granted, as one for ordinary form writes.
    # The two rungs above it subtract reads and deletes; everything else used to fall
    # through to it, so a verb no rule had ever named reached a development host on the
    # strength of a switch that says nothing about it.
    for verb in ("PROPFIND", "PURGE", "TRACE", "LOCK"):
        seen, guarded = overridden(BASE + "/api/items", verb, post_data="x=1")
        assert_true(
            seen == ["abort:blockedbyclient"]
            and guarded.mutations_blocked[0]["reason"] == "side_effects_off",
            f"{verb} is not a write the local permission was granted for: "
            f"{seen} {guarded.mutations_blocked}",
        )
    for verb in ("POST", "PUT", "PATCH"):
        assert_true(
            overridden(BASE + "/api/items", verb, post_data="x=1")[0] == ["continue"],
            f"{verb} to a development host still writes: it is what the switch is for",
        )
    # And the wider opt-in is unchanged by the narrowing: it was never a list of verbs.
    assert_true(
        overridden(BASE + "/api/items", "PURGE", post_data="x=1", allow_side_effects=True)[0]
        == ["continue"],
        "allow_side_effects still admits any non-destructive method, as it always did",
    )

    from exoma.core.evidence.e2e.spec import carries_method_override

    part = (
        '--x\r\nContent-Disposition: form-data; name="_method"\r\n\r\nDELETE\r\n--x--'
    )
    # Every spelling a backend might resolve the field from. Only the NAME is looked for,
    # so this list is the entire surface the guard still has to be right about.
    for label, body, query, want in (
        ("an escaped JSON key", '{"\\u005fmethod":"DELETE"}', "", True),
        ("the field named in prose", '{"note":"nothing here"}', "", False),
        ("an override after a large part", "x" * 70000 + part, "", True),
        ("a neighbouring field", part.replace('name="_method"', 'name="_method_choice"'), "", False),
        ("whitespace around the attribute", part.replace('name="_method"', 'name = "_method"'), "", True),
        ("a partly encoded key", None, "%5F%6Dethod=DELETE", True),
        ("a fully encoded key", None, "%5F%6D%65%74%68%6F%64=DELETE", True),
        ("a doubly encoded key", None, "%255Fmethod=DELETE", True),
        ("an ordinary query", None, "q=1&sort=name", False),
        ("a clean upload", '--x\r\nContent-Disposition: form-data; name="f"; '
                           'filename="a.png"\r\n\r\nPNG\r\n--x--', "", False),
    ):
        got = carries_method_override(body, {}, query)
        assert_true(got is want, f"{label}: override detection gave {got}, expected {want}")

    # A UTF-16 body carries a null beside every ASCII character, and one made mostly of
    # non-ASCII text carries few nulls overall — which is how a ratio test missed it.
    # Both readings are produced and either finding the field is enough.
    from exoma.core.evidence.e2e.browser import _decode_body

    utf16 = ("漢字" * 500 + "&_method=DELETE").encode("utf-16-le")
    assert_true(
        any(carries_method_override(text, {}, "") for text in _decode_body(utf16)),
        "a UTF-16 body's override must be found in one of its readings",
    )

    # --- the deny list: checked first, and no setting reaches it ------------------------
    deny = {"blocked_requests": ["* /api/payments", "DELETE /api/users/:id"]}
    for label, method, url, config in (
        ("a read", "GET", BASE + "/api/payments", {}),
        ("a write", "POST", BASE + "/api/payments", {}),
        ("with side effects on", "POST", BASE + "/api/payments", {"allow_side_effects": True}),
        ("an approved delete", "DELETE", BASE + "/api/users/7",
         {"allowed_destructive_requests": ["DELETE /api/users/:id"]}),
    ):
        seen, guarded = call(method, url, **deny, **config)
        assert_true(
            seen == ["abort:blockedbyclient"]
            and guarded.mutations_blocked[0]["reason"] == "blocked_by_policy",
            f"{label} to a blocked endpoint is refused before anything else: "
            f"{seen} {guarded.mutations_blocked}",
        )
    assert_true(
        call("GET", BASE + "/api/items", **deny)[0] == ["continue"],
        "an endpoint the deny list does not name is untouched by it",
    )
    assert_true(
        call("DELETE", BASE + "/api/users/7", **deny)[1].mutations_blocked[0]["reason"]
        == "blocked_by_policy",
        "a method-specific deny entry still wins over the approval that would allow it",
    )
    # A request carrying the override field has no verb to match a deny entry against, so
    # it is measured against every entry for its endpoint. Matching on the wire verb let a
    # POST carrying `_method` walk past the `DELETE` entry naming that exact endpoint.
    seen, guarded = overridden(
        BASE + "/api/users/7", "POST", post_data="_method=DELETE",
        allowed_destructive_requests=["DELETE /api/users/:id"], **deny,
    )
    assert_true(
        seen == ["abort:blockedbyclient"]
        and guarded.mutations_blocked[0]["reason"] == "blocked_by_policy",
        f"an override cannot route around the deny entry for its endpoint: "
        f"{seen} {guarded.mutations_blocked}",
    )
    # And the readings apply to an ordinary body too, not only the bytes fallback: a
    # `post_data` string that already carries nulls was being scanned once, unchanged.
    seen, guarded = overridden(
        BASE + "/api/items/7", "POST", post_data="\x00".join("_method=DELETE"),
    )
    assert_true(
        seen == ["abort:blockedbyclient"]
        and guarded.mutations_blocked[0]["reason"] == "override_unapproved",
        f"a NUL-interleaved override in a plain body is still found: "
        f"{seen} {guarded.mutations_blocked}",
    )
    # A confirmed read is admitted because its body was cleared of being a mutation, so
    # that clearing has to read every reading too — one of them is the whole point.
    seen, guarded = overridden(
        BASE + "/graphql", "POST",
        post_data="\x00".join('{"query":"mutation{x}"}'),
        allowed_read_only_requests=["POST /graphql"],
        allow_local_side_effects=False,
    )
    assert_true(
        seen == ["abort:blockedbyclient"]
        and "GraphQL" in (guarded.mutations_blocked[0].get("refusal") or ""),
        f"a mutation hidden in an alternate body reading is still a mutation: "
        f"{seen} {guarded.mutations_blocked}",
    )

    assert_true(call("POST", "http://localhost:9000/api/items")[0] == ["continue"], "another local port is a local host")
    assert_true(call("POST", "https://analytics.example/collect", resource_type="ping")[0] == ["abort:blockedbyclient"], "a third-party beacon is refused too")
    assert_true(call("POST", BASE + "/api/items", allow_side_effects=True)[0] == ["continue"], "allow_side_effects true sends a write to a loopback host")
    for url in ("http://127.0.0.1:9000/api/items", "http://[::1]:8080/x", "http://app.localhost/x", "http://127.0.0.2/x"):
        assert_true(call("PUT", url, allow_side_effects=True)[0] == ["continue"], f"every loopback form counts: {url}")
    seen, guarded = call("POST", "https://api.example/items", allow_side_effects=True)
    assert_true(
        seen == ["abort:blockedbyclient"] and guarded.mutations_blocked[0]["reason"] == "non_loopback" and "loopback" in guarded.ledger[0]["failure"],
        f"with side effects on, a write to a remote host is still refused — the request's host decides, not base_url's: {seen} {guarded.mutations_blocked}",
    )
    assert_true(
        call("POST", BASE + "/login", allowed_mutation_paths=["/login"], allow_local_side_effects=False)[0] == ["abort:blockedbyclient"],
        "a leftover allowed_mutation_paths opens nothing",
    )
    # `.test` is local development, like localhost: it takes a write with no approval.
    for url in ("http://app.test/api/items", "http://other.test:8080/x"):
        assert_true(call("POST", url, allow_side_effects=True)[0] == ["continue"], f"a .test host writes like localhost: {url}")
    # Any other host preflight resolved to a private address and pinned may take a write.
    # Only that host: the approval is a list of names, not a relaxed rule.
    assert_true(
        call("POST", "http://devbox.lan/api/items", allow_side_effects=True, write_hosts=["devbox.lan"])[0] == ["continue"],
        "a write host preflight approved and pinned may take a write",
    )
    seen, guarded = call("POST", "http://api.devbox.lan/api/items", allow_side_effects=True, write_hosts=["devbox.lan"])
    assert_true(
        seen == ["abort:blockedbyclient"] and guarded.mutations_blocked[0]["reason"] == "non_loopback",
        f"a sibling name nobody approved is not covered by it: {seen}",
    )
    assert_true(
        call("POST", "http://devbox.lan/api/items", allow_side_effects=True)[0] == ["abort:blockedbyclient"],
        "and without the approval the same name is refused: the guard trusts the decision, not the suffix",
    )

    # --- redirects: a write's redirect is followed by the guard, never by the browser ----------
    # Measured against Playwright 1.60: a POST answered 307 is re-sent WITH its body to the
    # new address, and the route handler never sees that second request. So an approved write
    # is fetched with max_redirects=0 and each hop judged before it goes out.
    def redirected(url, answers, *, navigation=False, method="POST", post_data="x=1", **config):
        """`answers` maps a URL to (status, location). Returns (route calls, fetch calls, session)."""
        seen: list[str] = []
        fetched: list[dict] = []

        def fetch(**kwargs):
            target = kwargs.get("url") or url
            fetched.append({"url": target, **{k: v for k, v in kwargs.items() if k != "url"}})
            status, location = answers.get(target, (200, None))
            return SimpleNamespace(status=status, headers={"location": location} if location else {})

        hop = SimpleNamespace(
            abort=lambda code: seen.append(f"abort:{code}"),
            continue_=lambda: seen.append("continue"),
            fetch=fetch,
            fulfill=lambda response: seen.append(f"fulfill:{response.status}"),
        )
        guarded, emitted, _ = _session(_Page(), **config)
        guarded.guard(
            hop,
            SimpleNamespace(url=url, method=method, resource_type="fetch", post_data=post_data, post_data_buffer=post_data.encode(),
                            is_navigation_request=lambda: navigation, frame=guarded.page.main_frame if navigation else object(),
                            all_headers=lambda: {"cookie": "sid=1"}),
        )
        guarded.ledger = emitted
        return seen, fetched, guarded

    seen, fetched, guarded = redirected(BASE + "/api/items", {BASE + "/api/items": (307, "https://evil.example/collect")}, allow_side_effects=True)
    assert_true(
        seen == ["abort:blockedbyclient"] and len(fetched) == 1 and guarded.mutations_blocked[0]["reason"] == "redirect"
        and "evil.example" in guarded.mutations_blocked[0]["refusal"],
        f"a 307 that would re-send the body off the write policy is refused, not followed: {seen} {fetched} {guarded.mutations_blocked}",
    )
    assert_true(guarded.ledger and guarded.ledger[0]["blocked"], f"and the ledger records the write as blocked: {guarded.ledger}")
    seen, fetched, _ = redirected(BASE + "/api/items", {BASE + "/api/items": (307, "/api/items/v2")}, allow_side_effects=True)
    assert_true(
        seen == ["fulfill:200"] and [f["url"] for f in fetched] == [BASE + "/api/items", BASE + "/api/items/v2"]
        and fetched[1]["method"] == "POST" and fetched[1]["post_data"] == b"x=1",
        f"a 307 to an allowed target is followed by the guard with the same method and body: {seen} {fetched}",
    )
    loop = {BASE + f"/r{i}": (308, f"/r{i + 1}") for i in range(10)}
    seen, fetched, guarded = redirected(BASE + "/r0", loop, allow_side_effects=True)
    assert_true(seen == ["abort:blockedbyclient"] and len(fetched) == 6, f"a redirect loop stops at the hop limit: {seen} {len(fetched)}")
    seen, fetched, _ = redirected(BASE + "/login", {BASE + "/login": (303, "/dashboard")}, allow_side_effects=True, navigation=True)
    assert_true(seen == ["fulfill:303"] and len(fetched) == 1, f"a 303 to an allowed page is handed back to the browser as a GET: {seen}")
    seen, _, _ = redirected(BASE + "/login", {BASE + "/login": (303, "https://evil.example/")}, allow_side_effects=True, navigation=True)
    assert_true(seen == ["abort:blockedbyclient"], f"a 303 that navigates off policy is refused: {seen}")
    # A delete is judged by which ENDPOINT it reaches, so its redirect is re-checked the
    # way a confirmed read's is. The host check alone let an approved delete be re-sent to
    # a neighbouring path on the same local host.
    approved_delete = {"allowed_destructive_requests": ["DELETE /api/items/:id"]}
    seen, fetched, guarded = redirected(
        BASE + "/api/items/7", {BASE + "/api/items/7": (307, "/api/orders/7")},
        method="DELETE", **approved_delete,
    )
    assert_true(
        seen == ["abort:blockedbyclient"]
        and "not an approved delete" in (guarded.mutations_blocked[0].get("refusal") or ""),
        f"an approved delete redirected to an unapproved endpoint is refused: {seen} {guarded.mutations_blocked}",
    )
    seen, fetched, _ = redirected(
        BASE + "/api/items/7", {BASE + "/api/items/7": (307, "/api/items/8")},
        method="DELETE", **approved_delete,
    )
    assert_true(
        seen == ["fulfill:200"] and len(fetched) == 2,
        f"a delete redirected within the approved route is followed: {seen} {fetched}",
    )
    # The same, for a delete spelled as a POST. Its wire method is POST all the way
    # through, so the approval has to travel with the request rather than be re-derived
    # from the verb at each hop.
    seen, _fetched, guarded = redirected(
        BASE + "/api/items/7", {BASE + "/api/items/7": (307, "/api/orders/7")},
        method="POST", post_data="_method=DELETE", **approved_delete,
    )
    assert_true(
        seen == ["abort:blockedbyclient"]
        and "not an approved delete" in (guarded.mutations_blocked[0].get("refusal") or ""),
        f"an approved override redirected off its route is refused too: "
        f"{seen} {guarded.mutations_blocked}",
    )

    # The deny list, across a hop. A 307/308 is fetched here rather than issued by the
    # browser, so it never comes back through the route handler — this loop is the only
    # place the list gets a second look, and it had none. A write to an endpoint nobody
    # denied, answered `307 → /api/payments`, landed on the one endpoint the project had
    # said to leave alone.
    denied = {"blocked_requests": ["* /api/payments", "DELETE /api/users/:id"]}
    seen, fetched, guarded = redirected(
        BASE + "/api/items", {BASE + "/api/items": (307, "/api/payments")},
        allow_side_effects=True, **denied,
    )
    assert_true(
        seen == ["abort:blockedbyclient"] and len(fetched) == 1
        and "deny list" in (guarded.mutations_blocked[0].get("refusal") or ""),
        f"a 307 onto a denied endpoint is refused, not followed: {seen} {fetched} {guarded.mutations_blocked}",
    )
    # Through a hop that is itself allowed, because the list is re-read at every one and
    # not only at the target the first answer named.
    seen, fetched, guarded = redirected(
        BASE + "/api/items", {BASE + "/api/items": (308, "/api/items/v2"), BASE + "/api/items/v2": (308, "/api/payments")},
        allow_side_effects=True, **denied,
    )
    assert_true(
        seen == ["abort:blockedbyclient"] and len(fetched) == 2,
        f"and at the second hop as readily as the first: {seen} {fetched}",
    )
    # A delete spelled as a POST has no verb for the method-specific entry to match, so
    # across a hop it is measured against every entry for the endpoint — the same rule the
    # first request is measured by, carried rather than re-derived.
    seen, _fetched, guarded = redirected(
        BASE + "/api/items/7", {BASE + "/api/items/7": (307, "/api/users/7")},
        method="POST", post_data="_method=DELETE",
        allowed_destructive_requests=["DELETE /api/items/:id", "DELETE /api/users/:id"], **denied,
    )
    assert_true(
        seen == ["abort:blockedbyclient"]
        and "deny list" in (guarded.mutations_blocked[0].get("refusal") or ""),
        f"an override redirected onto a method-specific deny entry is refused: "
        f"{seen} {guarded.mutations_blocked}",
    )
    # A 301/302/303 needs nothing here: the browser follows it with a fresh request, and
    # that request arrives at `guard` through `context.route`, where the deny list is the
    # first thing it meets. Pinned so the division stays visible — if the GET ever stopped
    # returning through the handler, this is where the missing check would have to go.
    seen, fetched, guarded = redirected(
        BASE + "/api/items", {BASE + "/api/items": (303, "/api/payments")},
        allow_side_effects=True, **denied,
    )
    assert_true(seen == ["fulfill:303"], f"a 303 is handed back for the browser to re-issue: {seen}")
    assert_true(
        guarded.guard(
            SimpleNamespace(abort=lambda code: seen.append(f"abort:{code}"), continue_=lambda: seen.append("continue")),
            SimpleNamespace(url=BASE + "/api/payments", method="GET", resource_type="fetch",
                            is_navigation_request=lambda: False, frame=object(), post_data=None, headers={}),
        ) is None and seen[-1] == "abort:blockedbyclient",
        f"and the deny list stops it when it comes back around: {seen}",
    )

    # Local writes off, so this exercises the read-only path rather than the ordinary
    # local-write one: the point here is that a CONFIRMED READ keeps its narrower promise
    # across a redirect, which only means something while the wider permission is closed.
    confirmed = {"allowed_read_only_requests": ["POST /api/search"], "allow_local_side_effects": False}
    seen, _, guarded = redirected(BASE + "/api/search", {BASE + "/api/search": (307, "/api/items")}, **confirmed)
    assert_true(
        seen == ["abort:blockedbyclient"] and "not a confirmed read" in guarded.mutations_blocked[0]["refusal"],
        f"a confirmed read redirected with its body to an unconfirmed endpoint is refused: {seen} {guarded.mutations_blocked}",
    )
    seen, fetched, _ = redirected("http://devbox.lan/api/items", {}, allow_side_effects=True, write_hosts=["devbox.lan"], host_pins={"devbox.lan": "192.168.1.40"})
    assert_true(
        seen == ["fulfill:200"] and fetched[0]["url"] == "http://192.168.1.40/api/items" and fetched[0]["headers"]["host"] == "devbox.lan"
        and fetched[0]["headers"]["cookie"] == "sid=1",
        f"a pinned host is fetched at its pinned address, outside Chromium's resolver rules: {fetched}",
    )
    seen, fetched, guarded = redirected("https://devbox.lan/api/items", {}, allow_side_effects=True, write_hosts=["devbox.lan"], host_pins={"devbox.lan": "192.168.1.40"})
    assert_true(
        seen == ["abort:blockedbyclient"] and not fetched and "https" in guarded.mutations_blocked[0]["refusal"],
        f"an https write to a pinned host cannot be held to the pin, so it is refused unsent: {seen} {fetched}",
    )

    # A top-level navigation redirected off policy never reached the guard; it is caught from
    # the request event and fails the step, saying the browser had already followed it.
    watcher, watched, _ = _session(_login_page())
    watcher.on_request(SimpleNamespace(url="https://evil.example/landing", redirected_from=object(),
                                       is_navigation_request=lambda: True, frame=watcher.page.main_frame))
    watcher.on_request(SimpleNamespace(url=BASE + "/dashboard", redirected_from=object(),
                                       is_navigation_request=lambda: True, frame=watcher.page.main_frame))
    assert_true(watcher.redirected_off_policy == ["https://evil.example/landing"], f"only the off-policy redirect is recorded: {watcher.redirected_off_policy}")
    stepper, _, _ = _session(_login_page())
    off_policy = SimpleNamespace(url="https://evil.example/landing", redirected_from=object(),
                                 is_navigation_request=lambda: True, frame=stepper.page.main_frame)
    stepper.perform = lambda step: (stepper.on_request(off_policy), {"status": "passed"})[1]
    outcome = stepper._execute({"id": "submit", "action": "click", "selector": {"text": "Masuk"}}, "submit")
    assert_true(
        outcome["status"] == "failed" and outcome["error"]["kind"] == "navigation_blocked"
        and "followed by the browser" in outcome["error"]["detail"],
        f"the step whose action redirected off policy fails, and says the redirect was already followed: {outcome}",
    )

    # --- the deny list judges navigation too, not only writes ------------------------------------
    # The list names endpoints. It was read for every write and for no navigation, so a
    # redirect from an allowed origin onto a denied path of that same origin passed both
    # checks: the origin policy had nothing to object to, and the only reader of the list
    # never saw a GET.
    nav_deny = {"blocked_requests": ["* /api/payments", "DELETE /api/users/:id"]}
    denier, _, _ = _session(_login_page(), **nav_deny)
    denier.on_request(SimpleNamespace(url=BASE + "/api/payments", redirected_from=object(),
                                      is_navigation_request=lambda: True, frame=denier.page.main_frame))
    assert_true(
        denier.blocked == [BASE + "/api/payments"] and "blocked_requests" in denier.blocked_detail[BASE + "/api/payments"],
        f"a navigation redirected onto a denied endpoint is recorded, naming the deny list: {denier.blocked} {denier.blocked_detail}",
    )
    # And at the route handler, where it can still be stopped rather than reported.
    routed, _, _ = _session(_login_page(), **nav_deny)
    routed_seen: list[str] = []
    nav_route = SimpleNamespace(
        abort=lambda reason="failed": routed_seen.append(f"abort:{reason}"),
        continue_=lambda **kw: routed_seen.append("continue"),
        fetch=lambda **kw: None,
    )
    routed.guard(nav_route, SimpleNamespace(url=BASE + "/api/payments", method="GET", post_data=None,
                                            is_navigation_request=lambda: True, frame=routed.page.main_frame,
                                            all_headers=lambda: {}))
    assert_true(routed_seen == ["abort:blockedbyclient"], f"a top-level navigation to a denied endpoint is aborted at the guard: {routed_seen}")
    # A `goto` never starts: the step says which policy refused it, before any request.
    refuser, _, _ = _session(_login_page(), **nav_deny)
    outcome = refuser.perform({"id": "pay", "action": "goto", "url": "/api/payments"})
    assert_true(
        outcome["status"] == "failed" and outcome["error"]["kind"] == "navigation_blocked"
        and "blocked_requests" in outcome["error"]["detail"] and refuser.page.gotos == [],
        f"a goto at a denied endpoint is refused before navigating: {outcome} {refuser.page.gotos}",
    )
    # A method-specific entry is not a navigation rule. `DELETE /api/users/:id` says not to
    # delete that record; opening the same address is a read, and refusing it would make the
    # list mean something it does not say.
    reader, _, _ = _session(_login_page(), **nav_deny)
    assert_true(
        reader._navigation_refusal(BASE + "/api/users/7") is None
        and reader._navigation_refusal(BASE + "/api/payments") is not None,
        "a DELETE-only entry leaves navigation to that address alone, while a `*` entry does not",
    )

    # --- every URL change is read, not only the ones that produced a request ---------------------
    # `history.pushState`, a meta refresh and a redirect Chromium follows internally all move
    # the address bar without a request event to attribute, and the address bar is what the
    # policy is about. `framenavigated` reads it after every change.
    subframe, _, _ = _session(_login_page(), **nav_deny)
    subframe.on_frame_navigated(SimpleNamespace(url=BASE + "/api/payments"))
    assert_true(subframe.blocked == [], "a frame that is not the main frame is not judged")
    main_nav, _, _ = _session(_login_page(), **nav_deny)
    frame = SimpleNamespace(url=BASE + "/api/payments")
    main_nav.page.main_frame = frame
    main_nav.on_frame_navigated(frame)
    main_nav.on_frame_navigated(frame)  # the same landing reported twice stays one failure
    assert_true(
        main_nav.blocked == [BASE + "/api/payments"] and main_nav.redirected_off_policy == [BASE + "/api/payments"],
        f"a URL change with no request behind it is caught once: {main_nav.blocked}",
    )
    blank, _, _ = _session(_login_page(), **nav_deny)
    for address in ("about:blank", "", "data:text/html,<p>x</p>"):
        blank.page.main_frame = SimpleNamespace(url=address)
        blank.on_frame_navigated(blank.page.main_frame)
    assert_true(blank.blocked == [], f"addresses the policy has no say over are left alone: {blank.blocked}")

    # A landing that arrives after the step window closed — a debounced `pushState`, a
    # redirect still being followed — was recorded and then read by nobody: `_execute`
    # compares its counter before the navigation happens, so the page had reached a denied
    # address and the run still had no failed step to show for it.
    late, late_events, _ = _session(_login_page(), **nav_deny)
    late.page.main_frame = SimpleNamespace(url=BASE + "/api/payments")
    late.on_frame_navigated(late.page.main_frame)  # no step is current
    late.report_unattributed_navigation()
    harness = [e for e in late_events if e.get("type") == "harness"]
    assert_true(
        len(harness) == 1 and harness[0]["reason"] == "navigation_blocked" and "blocked_requests" in harness[0]["detail"],
        f"an off-policy landing outside every step window is reported as harness: {harness}",
    )
    late.report_unattributed_navigation()
    assert_true(len([e for e in late_events if e.get("type") == "harness"]) == 1, "and reported once")
    # What a step's own window caught stays the step's, and is not repeated at the end.
    owned, owned_events, _ = _session(_login_page(), **nav_deny)
    owned.perform = lambda step: (owned._note_off_policy(BASE + "/api/payments", "denied", redirected=True), {"status": "passed"})[1]
    outcome = owned._execute({"id": "pay", "action": "click", "selector": {"text": "Masuk"}}, "pay")
    owned.report_unattributed_navigation()
    assert_true(
        outcome["status"] == "failed" and not [e for e in owned_events if e.get("type") == "harness"],
        f"a landing a step caught is that step's failure, not a run-level one: {outcome}",
    )
    # Leaving a denied address and coming back is a second violation by a second step.
    twice, _, _ = _session(_login_page(), **nav_deny)
    twice.current_step_id = "one"
    twice._note_off_policy(BASE + "/api/payments", "denied", redirected=True)
    twice.current_step_id = "two"
    twice._note_off_policy(BASE + "/api/payments", "denied", redirected=True)
    assert_true(len(twice.blocked) == 2, f"the same address under a later step is recorded again: {twice.blocked}")
    # A cleanup step that reaches a denied endpoint reached it exactly as a test step would
    # have. Its own failure is a `cleanup` event, which the classifier treats as
    # housekeeping and never lets decide a verdict — so the violation has to leave the
    # cleanup unclaimed and be reported at the run level, or a run that went off policy
    # during cleanup still reports `pass`.
    cleaner, cleaner_events, _ = _session(_login_page(), **nav_deny)
    cleaner.phase = "cleanup"
    cleaner.perform = lambda step: (cleaner._note_off_policy(BASE + "/api/payments", "denied", redirected=True), {"status": "passed"})[1]
    cleaner._execute({"id": "undo", "action": "click", "selector": {"text": "Masuk"}}, "undo")
    cleaner.report_unattributed_navigation()
    cleanup_harness = [e for e in cleaner_events if e.get("type") == "harness"]
    assert_true(
        len(cleanup_harness) == 1 and cleanup_harness[0]["reason"] == "navigation_blocked",
        f"a cleanup that goes off policy reaches the verdict instead of staying housekeeping: {cleanup_harness}",
    )

    # An unattributed landing is still reported when a LATER step reaches the same address.
    # Attribution keyed on the URL let the step's claim cover the earlier landing too, and
    # the violation nobody owned went unreported.
    both, both_events, _ = _session(_login_page(), **nav_deny)
    both.page.main_frame = SimpleNamespace(url=BASE + "/api/payments")
    both.on_frame_navigated(both.page.main_frame)  # outside every step
    both.perform = lambda step: (both._note_off_policy(BASE + "/api/payments", "denied", redirected=True), {"status": "passed"})[1]
    both._execute({"id": "pay", "action": "click", "selector": {"text": "Masuk"}}, "pay")
    both.report_unattributed_navigation()
    assert_true(
        len([e for e in both_events if e.get("type") == "harness"]) == 1,
        "the landing no step claimed is reported even after a step claims the same address",
    )

    # --- confirmed reads: a POST the user confirmed as read-only passes; a GraphQL write never does ---
    import json as _json

    from exoma.core.evidence.e2e.browser import read_only_refusal

    def post(url, body=None, **config):
        seen: list[str] = []
        hop = SimpleNamespace(abort=lambda code: seen.append(f"abort:{code}"), continue_=lambda: seen.append("continue"))
        guarded, _, _ = _session(_Page(), **config)
        guarded.guard(hop, SimpleNamespace(url=url, method="POST", resource_type="fetch", post_data=body,
                                           is_navigation_request=lambda: False, frame=object()))
        return seen, guarded

    # Local writes off throughout this block: a confirmed read is a narrower permission
    # than a local write, and it can only be observed while the wider one is closed.
    reads = {
        "allowed_read_only_requests": ["POST /api/search", "POST /graphql"],
        "allow_local_side_effects": False,
    }
    seen, guarded = post(BASE + "/api/search?page=2", '{"q": "laptop"}', **reads)
    assert_true(
        seen == ["continue"] and guarded.read_only_allowed == [{"method": "POST", "origin": BASE}] and guarded.mutations_blocked == [],
        f"a confirmed read passes and is recorded as a read, origin only: {seen} {guarded.read_only_allowed}",
    )
    assert_true(post(BASE + "/api/search/export", "{}", **reads)[0] == ["abort:blockedbyclient"], "a confirmed read is exact, not a prefix")
    assert_true(post("http://localhost:9000/api/search", "{}", **reads)[0] == ["abort:blockedbyclient"], "a bare path belongs to base_url's origin only")
    assert_true(
        post(BASE + "/api/search", "{}", allow_local_side_effects=False)[0] == ["abort:blockedbyclient"],
        "nothing is a read until the request settings name it",
    )
    assert_true(post(BASE + "/graphql", _json.dumps({"query": "query Items { items { id } }"}), **reads)[0] == ["continue"], "a GraphQL query is a read")
    seen, guarded = post(BASE + "/graphql", _json.dumps({"query": "mutation Drop { deleteItem(id: 7) { id } }"}), **reads)
    assert_true(
        seen == ["abort:blockedbyclient"] and guarded.mutations_blocked[0].get("refusal") == "a GraphQL mutation or subscription" and guarded.read_only_allowed == [],
        f"a GraphQL mutation on a confirmed read endpoint is still refused: {seen} {guarded.mutations_blocked}",
    )

    class _BinaryBody:
        url = BASE + "/api/search"
        method = "POST"
        resource_type = "fetch"
        frame = object()

        def is_navigation_request(self):
            return False

        @property
        def post_data(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    binary_hop: list[str] = []
    binary_session, _, _ = _session(_Page(), **reads)
    binary_session.guard(SimpleNamespace(abort=lambda code: binary_hop.append(code), continue_=lambda: binary_hop.append("continue")), _BinaryBody())
    assert_true(binary_hop == ["blockedbyclient"] and "cannot be read" in binary_session.mutations_blocked[0]["refusal"], f"an unreadable body is refused, not guessed: {binary_hop}")

    graphql_write = "a GraphQL mutation or subscription"
    for body, expected in (
        (None, None),
        ('{"q": "mutation { x }"}', None),
        ('{"query": "{ items(filter: \\"mutation {\\") { id } }"}', None),
        ('{"query": "# mutation {\\n{ items { id } }"}', None),
        ('[{"query": "{ a }"}, {"query": "mutation { b }"}]', graphql_write),
        ('{"query": "subscription OnItem { item { id } }"}', graphql_write),
        ('{"query": "mutation($id: ID!) { drop(id: $id) }"}', graphql_write),
        ('{"extensions": {"persistedQuery": {"sha256Hash": "abc"}}}', "a persisted GraphQL query, whose operation cannot be read"),
        ("query=mutation%20%7B%20x%20%7D", graphql_write),
        ("mutation { x }", graphql_write),
        ("q=laptop&page=2", None),
    ):
        assert_true(read_only_refusal(body) == expected, f"read_only_refusal({body!r}) = {read_only_refusal(body)!r}, expected {expected!r}")

    # A confirmed read inside a step passes the step and surfaces once, as a counted observation.
    page = _Page()
    session, events, _ = _session(page, **reads)

    def search(p: _Page) -> None:
        for _ in range(2):
            session.guard(route, SimpleNamespace(url=BASE + "/api/search?q=secret-term", method="POST", resource_type="fetch", post_data="{}",
                                                 is_navigation_request=lambda: False, frame=object()))

    page.elements = [{"role": "button", "name": "Search", "on_click": search}]
    session.run({"steps": [{"action": "click", "selector": {"role": "button", "name": "Search"}, "claim_id": "login"}]})
    assert_true(_progress(events)[0]["status"] == "passed", f"a confirmed read does not fail its step: {_progress(events)[0]}")
    allowed = [e for e in events if e["type"] == "observation" and e["kind"] == "read_only_request_allowed"]
    assert_true(
        len(allowed) == 1 and allowed[0]["url"] == BASE and allowed[0]["detail"].startswith("2 POST") and "secret-term" not in _json.dumps(events),
        f"one counted observation per method and origin, no path or query: {allowed}",
    )
    report = build_report([*events, {"type": "result", "status": "finished"}], {"claims": _CLAIMS})
    assert_true(report["browser_verdict"] == "pass" and report["app_errors"] == [], f"a confirmed read is a warning-class record, never an app error: {report['browser_verdict']}")

    page = _Page()
    session, events, _ = _session(page, **reads)

    def graphql_drop(p: _Page) -> None:
        session.guard(route, SimpleNamespace(url=BASE + "/graphql", method="POST", resource_type="fetch", post_data=_json.dumps({"query": "mutation { drop }"}),
                                             is_navigation_request=lambda: False, frame=object()))

    page.elements = [{"role": "button", "name": "Drop", "on_click": graphql_drop}]
    session.run({"steps": [{"action": "click", "selector": {"role": "button", "name": "Drop"}, "claim_id": "login"}]})
    dropped = _progress(events)[0]
    assert_true(
        dropped["error"]["kind"] == "mutation_blocked" and "allowed_read_only_requests covers the endpoint" in dropped["error"]["detail"] and "GraphQL" in dropped["error"]["detail"],
        f"the step names why a confirmed endpoint was still refused: {dropped['error']}",
    )

    # A click whose write is refused fails that step as harness, whatever the step saw after it.
    page = _Page()
    session, events, _ = _session(page)

    def submit_delete(p: _Page) -> None:
        session.guard(route, SimpleNamespace(url=BASE + "/api/items/7", method="DELETE", resource_type="fetch",
                                             is_navigation_request=lambda: False, frame=object()))

    page.elements = [{"role": "button", "name": "Delete", "on_click": submit_delete}]
    session.run({"steps": [{"action": "click", "selector": {"role": "button", "name": "Delete"}, "claim_id": "login"},
                           {"action": "expect_url", "contains": "/items"}]})
    refused = _progress(events)[0]
    assert_true(refused["status"] == "failed" and refused["error"]["kind"] == "mutation_blocked", f"the refused write fails its step: {refused}")
    assert_true(classify_step(refused) == ORIGIN_SCENARIO, "a refused write is the scenario's, never the app's")
    assert_true("DELETE" in refused["error"]["detail"] and "/api/items" not in refused["error"]["detail"], f"the detail names method and origin only: {refused['error']}")
    assert_true(_progress(events)[1]["status"] == "skipped", "the steps after it prove nothing and are skipped")
    report = build_report([*events, {"type": "result", "status": "finished"}], {"claims": _CLAIMS})
    assert_true(report["browser_verdict"] == "incomplete" and report["reason"] == "scenario_error", f"a refused write leaves the run incomplete, not failed: {report['browser_verdict']} {report['reason']}")
    assert_true(not [e for e in events if e.get("kind") == "mutation_blocked" and e["type"] == "observation"], "an attributed write is not reported twice")

    # A beacon never decides a step; it surfaces after the run as a warning observation.
    page = _Page()
    session, events, _ = _session(page)

    def beacon(p: _Page) -> None:
        session.guard(route, SimpleNamespace(url="https://analytics.example/collect?uid=9", method="POST", resource_type="ping",
                                             is_navigation_request=lambda: False, frame=object()))

    page.elements = [{"role": "button", "name": "Open", "on_click": beacon}]
    session.run({"steps": [{"action": "click", "selector": {"role": "button", "name": "Open"}, "claim_id": "login"}]})
    assert_true(_progress(events)[0]["status"] == "passed", f"a refused beacon does not fail the step: {_progress(events)[0]}")
    warnings = [e for e in events if e["type"] == "observation" and e["kind"] == "mutation_blocked"]
    assert_true(len(warnings) == 1 and warnings[0]["url"] == "https://analytics.example" and not warnings[0]["same_origin"], f"one observation, origin only: {warnings}")
    report = build_report([*events, {"type": "result", "status": "finished"}], {"claims": _CLAIMS})
    assert_true(report["browser_verdict"] == "pass" and report["app_errors"] == [], f"the beacon stays a warning: {report['browser_verdict']} {report['app_errors']}")

    # --- request ledger: attributed to the step's window, status and duration from the lifecycle ----
    from exoma.core.evidence.e2e.redact import sanitize_endpoint

    for url, expected in (
        ("http://localhost:8000/items/42?token=abc#x", "http://localhost:8000/items/:id"),
        ("https://user:pw@api.example:443/v1/reset/3f2a9c1e0b7d4e5f6a7b8c9d", "https://api.example/v1/reset/:id"),
        ("http://127.0.0.1/users/someone%40internal.example/avatar", "http://127.0.0.1/users/:id/avatar"),
        ("http://[::1]:9000/orders/0b8e6a5c-2f4d-4e1a-9c3b-7d6e5f4a3b2c", "http://[::1]:9000/orders/:id"),
        ("http://localhost/api/v2/items", "http://localhost/api/v2/items"),
        ("http://localhost:bad/x", "[unparseable url]"),
    ):
        assert_true(sanitize_endpoint(url) == expected, f"sanitize_endpoint({url!r}) = {sanitize_endpoint(url)!r}, expected {expected!r}")

    page = _Page()
    session, events, clock = _session(page, allow_side_effects=True)
    created = SimpleNamespace(url=BASE + "/items/42?csrf=abc", method="POST", resource_type="fetch", is_navigation_request=lambda: False, frame=object(), failure=None)
    extra = SimpleNamespace(url=BASE + "/audit", method="POST", resource_type="fetch", is_navigation_request=lambda: False, frame=object(), failure=None)
    late = SimpleNamespace(url=BASE + "/autosave", method="PATCH", resource_type="xhr", is_navigation_request=lambda: False, frame=object(), failure="net::ERR_ABORTED")
    hanging = SimpleNamespace(url=BASE + "/export", method="POST", resource_type="fetch", is_navigation_request=lambda: False, frame=object(), failure=None)

    def save(p: _Page) -> None:
        for sent, status in ((created, 201), (extra, 204)):
            session.guard(route, sent)
            clock.advance(0.25)
            session.on_response(SimpleNamespace(status=status, url=sent.url, request=sent, frame=p.main_frame))
            session.on_request_finished(sent)

    page.elements = [{"role": "button", "name": "Simpan", "on_click": save}]
    session.run({"steps": [{"id": "create-item", "action": "click", "selector": {"role": "button", "name": "Simpan"},
                            "request": {"method": "POST", "path": "/items/:id"}}]})
    session.guard(route, late)
    session.on_request_failed(late)
    session.guard(route, hanging)
    session.flush_requests()
    ledger = [e for e in events if e["type"] == "request"]
    first, second, third, fourth = ledger
    assert_true(
        first["step_id"] == "create-item" and first["attribution"] == "step" and first["planned"] is True
        and first["status"] == 201 and first["duration_ms"] == 250 and first["endpoint"] == BASE + "/items/:id" and "csrf" not in json.dumps(ledger),
        f"the planned write: its step, planned, status, duration, sanitised endpoint: {first}",
    )
    assert_true(second["planned"] is False and second["status"] == 204, f"an extra write in the same window is recorded as not planned: {second}")
    assert_true(
        third["attribution"] == "uncertain" and third["step_id"] is None and third["after_step"] == "create-item" and third["planned"] is None and "ERR_ABORTED" in third["failure"],
        f"a write after the window is uncertain, never pinned to the last step: {third}",
    )
    assert_true(fourth["failure"] == "no response before the run ended", f"a write still waiting at the end is reported, not dropped: {fourth}")
    report = build_report([*events, {"type": "result", "status": "finished"}], {"claims": []})
    assert_true(
        report["trail"][0]["requests"] == [first["id"], second["id"]] and len(report["requests"]) == 4,
        f"the trail ties the step to its requests: {report['trail']}",
    )
    block = evidence_block(report)
    assert_true("trail:" in block and "requests:" in block and "HTTP 201" in block and "uncertain (after create-item)" in block, f"the reviewer sees the trail and the ledger:\n{block}")

    # --- cleanup: runs after the steps, pass or fail, reported apart ---------------------------------
    from exoma.core.evidence.e2e.normalize import to_verification

    def cleanup_page(*, delete_button: bool = True) -> _Page:
        page = _Page()

        def to_items(p: _Page) -> None:
            p.url = BASE + "/items"

        page.elements = [{"role": "button", "name": "Simpan", "on_click": to_items}, {"e2e": "row", "inner_text": "e2e-item-1"}]
        if delete_button:
            page.elements.append({"role": "button", "name": "Hapus e2e-item-1", "on_click": to_items})
        return page

    crud = {
        "claims": _CLAIMS,
        "steps": [
            {"id": "create-item", "action": "click", "selector": {"role": "button", "name": "Simpan"}, "side_effect": "creates_test_data"},
            {"id": "assert-row", "action": "expect_dom", "selector": {"e2e": "row"}, "text": "e2e-item-1", "claim_id": "login"},
        ],
        "cleanup": [
            {"id": "delete-item", "cleans": "create-item", "action": "click", "selector": {"role": "button", "name": "Hapus e2e-item-1"}},
            {"id": "assert-gone", "cleans": "create-item", "action": "expect_url", "contains": "/items"},
        ],
    }
    clean_review = "[VERIFICATION]\nverdict: DONE\n\nblocking_findings:\n- none\n\nescalations:\n- none\n\nnotes:\n- none\n\nchecks_run:\n- read the code\n\nnot_verified:\n- none\n\nconfidence: high — fine\n"

    def crud_run(page: _Page, scenario: dict = crud, **config) -> tuple[list[dict], dict]:
        session, events, _ = _session(page, **config)
        session.run(scenario)
        return events, build_report([*events, {"type": "result", "status": "finished"}], scenario)

    events, report = crud_run(cleanup_page())
    cleanup_events = [e for e in events if e["type"] == "cleanup"]
    assert_true([e["status"] for e in cleanup_events] == ["passed", "passed"] and report["cleanup"]["status"] == "passed", f"a clean cleanup: {cleanup_events}")
    assert_true(report["browser_verdict"] == "pass" and to_verification(report, reviewer_content=clean_review)["verdict"] == "pass", "test and cleanup both clean: pass")
    assert_true(events.index(next(e for e in events if e["type"] == "cleanup")) > max(i for i, e in enumerate(events) if e["type"] == "progress"), "cleanup runs after every test step")

    events, report = crud_run(cleanup_page(delete_button=False))
    assert_true(
        [e["status"] for e in events if e["type"] == "cleanup"] == ["failed", "skipped"] and report["cleanup"]["status"] == "failed",
        f"a failed cleanup step stops its group: {[e['status'] for e in events if e['type'] == 'cleanup']}",
    )
    assert_true(report["browser_verdict"] == "pass" and report["claims"]["login"]["status"] == "proven", "the test's own result is untouched by its cleanup")
    from exoma.core.evidence.contract import validate_verification_contract, verify_exit_status

    norm = to_verification(report, reviewer_content=clean_review)
    assert_true(
        norm["verdict"] == "incomplete" and norm["declared"] == "INCOMPLETE" and "e2e cleanup: 'create-item' failed" in norm["content"]
        and verify_exit_status(norm["verdict"], validate_verification_contract(norm["content"])) != 0,
        f"a failed cleanup makes a passing test incomplete with a non-zero exit: {norm['verdict']}/{norm['declared']}\n{norm['content']}",
    )
    assert_true("e2e:login: pass" in norm["content"], "and the proven claim is still reported as proven")

    failing_test = json.loads(json.dumps(crud))
    failing_test["steps"][1]["text"] = "something else"
    events, report = crud_run(cleanup_page(), failing_test)
    assert_true(report["browser_verdict"] == "fail" and report["cleanup"]["status"] == "passed", f"a failed test still cleans up: {report['browser_verdict']} {report['cleanup']['status']}")
    assert_true(to_verification(report, reviewer_content=clean_review)["verdict"] == "fail", "and a fail stays a fail")

    never = json.loads(json.dumps(crud))
    never["steps"].insert(0, {"id": "gate", "action": "expect_url", "contains": "/nowhere", "claim_id": "login"})
    events, report = crud_run(cleanup_page(), never)
    assert_true(report["cleanup"]["status"] == "not_needed", f"nothing was created, nothing to clean: {report['cleanup']}")

    killed = [e for e in crud_run(cleanup_page())[0] if e["type"] != "cleanup"]
    report = build_report([*killed, {"type": "result", "status": "finished"}], crud)
    assert_true(report["cleanup"]["status"] == "not_run" and to_verification(report, reviewer_content=clean_review)["verdict"] == "incomplete", "a cleanup the player never reached is not_run, never a pass")

    slow = cleanup_page()
    slow.elements[0]["on_click"] = lambda p: session_clock.advance(6)
    session, events, session_clock = _session(slow, total_timeout_s=10, step_timeout_ms=2000)
    session.run(crud)
    steps_seen = _progress(events)
    assert_true(
        steps_seen[1]["status"] == "skipped" and "for cleanup" in steps_seen[1].get("detail", "") and [e["status"] for e in events if e["type"] == "cleanup"] == ["passed", "passed"],
        f"test steps stop early to leave cleanup its share of total_timeout_s: {[(e['step_id'], e['status']) for e in steps_seen]}",
    )

    # --- observers: shapes the classifier reads, third-party kept apart ----------------------------
    page = _Page()
    page.url = BASE + "/dashboard"
    session, events, _ = _session(page)
    same = SimpleNamespace(is_navigation_request=lambda: True)
    session.on_response(SimpleNamespace(status=503, url=BASE + "/dashboard", request=same, frame=page.main_frame))
    session.on_response(SimpleNamespace(status=503, url="https://analytics.example/beacon", request=SimpleNamespace(is_navigation_request=lambda: False), frame=page.main_frame))
    session.on_response(SimpleNamespace(status=404, url=BASE + "/missing", request=same, frame=page.main_frame))
    session.on_page_error(SimpleNamespace(message="TypeError: x is undefined"))
    session.on_console(SimpleNamespace(type="error", text="boom", location={"url": BASE + "/app.js"}))
    session.on_console(SimpleNamespace(type="log", text="hello", location={}))
    kinds = [(e["kind"], e["same_origin"], e["main_request"]) for e in events]
    assert_true(
        kinds == [("http_5xx", True, True), ("http_5xx", False, False), ("page_error", True, False), ("console_error", True, False)],
        f"observations: 5xx main + third-party, page error, console error; 404 and logs ignored: {kinds}",
    )
    report = build_report([*events, {"type": "result", "status": "finished"}], {"claims": []})
    assert_true(report["browser_verdict"] == "fail" and len(report["app_errors"]) == 2, "same-origin main 5xx and page error fail the run")

    # A page error is attributed to the script that threw (its stack), not to the page URL:
    # a third-party widget failing on our page is noise. Found against real Chromium.
    session, events, _ = _session(page)
    session.on_page_error(SimpleNamespace(message="widget failed", stack="Uncaught Error: widget failed\n    at widget (https://widgets.example/w.js:0:29)"))
    session.on_page_error(SimpleNamespace(message="own boom", stack=f"Error: own boom\n    at own ({BASE}/app.js:3:31)"))
    assert_true(
        [(e["url"], e["same_origin"]) for e in events] == [("https://widgets.example/w.js", False), (BASE + "/app.js", True)],
        f"page errors carry the throwing script's URL and origin: {[(e['url'], e['same_origin']) for e in events]}",
    )
    assert_true(build_report([events[0], {"type": "result", "status": "finished"}], {"claims": []})["browser_verdict"] == "pass", "a third-party page error does not fail the run")

    def console_only(enforced: bool) -> str:
        session, events, _ = _session(_Page(), fail_on_console_error=enforced)
        session.on_console(SimpleNamespace(type="error", text="boom", location={"url": BASE + "/app.js"}))
        session.on_console(SimpleNamespace(type="error", text="third party", location={"url": "https://ads.example/x.js"}))
        return build_report([*events, {"type": "result", "status": "finished"}], {"claims": []})["browser_verdict"]

    assert_true(console_only(False) == "pass", "console errors are warnings by default")
    assert_true(console_only(True) == "fail", "fail_on_console_error turns a same-origin console error into a failure")

    # --- no Playwright: the player says so instead of crashing ------------------------------------
    emitted: list[dict] = []
    saved = sys.modules.get("playwright.sync_api", "absent")
    sys.modules["playwright.sync_api"] = None  # import now raises ImportError
    try:
        code = run_scenario({"steps": []}, {"browser": "chromium"}, "", emitted.append)
    finally:
        if saved == "absent":
            sys.modules.pop("playwright.sync_api", None)
        else:
            sys.modules["playwright.sync_api"] = saved
    assert_true(code == 2 and emitted[0]["reason"] == "playwright_missing" and emitted[-1] == {"type": "result", "status": "aborted"}, f"missing package: {emitted}")
    emitted.clear()
    assert_true(run_scenario({"steps": []}, {"browser": "netscape"}, "", emitted.append) == 2 and emitted[0]["reason"] == "launch_failed", "an unknown browser name is refused")
    assert_true(e2e_browser.HEARTBEAT_EVERY_S < 30, "heartbeats come well inside the default idle timeout")

    # --- failure capture (plan §16): probe on selector misses, HTML + screenshot on page failures --
    art = Path(tempfile.mkdtemp(prefix="e2e-capture-"))
    finished = {"type": "result", "status": "finished"}
    try:
        def captured(page: _Page, steps: list[dict], *, secrets: bool = False):
            events: list[dict] = []
            clock = _Clock()
            config = {"base_url": BASE, "step_timeout_ms": 300, "nav_timeout_ms": 500, "probe_max_elements": 5}
            session = Session(page, config, events.append, clock=clock, sleep=clock.advance, artifacts_dir=str(art), has_secrets=secrets)
            session.run({"steps": steps})
            return session, events

        page = _Page()
        page.url = BASE + "/login?error=1"
        session, failure_events = captured(page, [{"action": "expect_url", "contains": "/dashboard", "claim_id": "login"}])
        kinds = [(e["kind"], e["name"]) for e in failure_events if e["type"] == "artifact"]
        assert_true(kinds == [("html", "step01.html"), ("screenshot", "step01.png")], f"an assertion failure keeps bounded HTML and one screenshot: {kinds}")
        assert_true((art / "step01.html").read_text(encoding="utf-8") == page.html and (art / "step01.png").exists(), "the files are where the events say")
        assert_true(
            page.calls.index("clear_inputs") < page.calls.index("content") < page.calls.index("screenshot"),
            f"typed values are cleared before anything is captured: {page.calls}",
        )
        assert_true(session.failed, "the session remembers it failed; the trace decision reads this")
        order = [e["type"] for e in failure_events]
        assert_true(order.index("progress") < order.index("artifact"), "artifacts follow the step they document")
        report = build_report([*failure_events, finished], {"claims": _CLAIMS})
        assert_true([a["name"] for a in report["artifacts"]] == ["step01.html", "step01.png"], f"the report lists artifacts: {report['artifacts']}")
        block = evidence_block(dict(report, artifacts=[*report["artifacts"], {"kind": "trace", "name": "trace.zip", "step": None, "pruned": True}]), artifacts=str(art))
        assert_true("- step01.png (screenshot, step 1)" in block and "- trace.zip (trace) — pruned" in block, f"the evidence block names kept and pruned files:\n{block}")

        page = _Page()
        page.url = BASE + "/login?error=1"
        _, events = captured(page, [{"action": "expect_url", "contains": "/dashboard", "claim_id": "login"}], secrets=True)
        artifacts = [e for e in events if e["type"] == "artifact"]
        assert_true([a["kind"] for a in artifacts] == ["html", "skipped"] and "screenshot" not in page.calls, "with resolved ${ENV} values no screenshot is taken")
        assert_true("${ENV}" in artifacts[1]["detail"], "and the skip says why")

        page = _Page()
        page.url = BASE + "/login?error=1"
        short_events: list[dict] = []
        short_clock = _Clock()
        Session(page, {"base_url": BASE, "step_timeout_ms": 300, "capture_html": False}, short_events.append, clock=short_clock, sleep=short_clock.advance,
                artifacts_dir=str(art), has_secrets=True).run({"steps": [{"action": "expect_url", "contains": "/dashboard", "claim_id": "login"}]})
        assert_true(
            [a["kind"] for a in short_events if a["type"] == "artifact"] == ["skipped", "skipped"] and "content" not in page.calls,
            "a value too short to scrub keeps no HTML either",
        )

        page = _login_page()
        _, events = captured(page, [{"action": "click", "selector": {"css": "#nope"}, "selector_provenance": {"type": "heuristic"}}])
        miss = _progress(events)[0]
        assert_true(miss["probe"]["buttons"][0]["name"] == "Masuk" and not [e for e in events if e["type"] == "artifact"], "a heuristic miss gets a probe and nothing else")
        block = evidence_block(build_report([*events, finished], {"claims": _CLAIMS}))
        assert_true("probe: headings: Masuk | buttons: Masuk" in block, f"the probe reaches the reviewer as one bounded line:\n{block}")

        page = _login_page()
        _, events = captured(page, [{"action": "click", "selector": {"role": "button", "name": "Logout"}, "selector_provenance": {"type": "source"}}])
        assert_true(
            "probe" in _progress(events)[0] and [e["kind"] for e in events if e["type"] == "artifact"] == ["html", "screenshot"],
            "a grounded miss gets the probe and the page evidence",
        )

        page = _login_page()
        _, events = captured(page, [{"action": "goto", "url": "https://evil.example/"}])
        assert_true(not [e for e in events if e["type"] == "artifact"] and "content" not in page.calls, "a blocked navigation never touched the page and keeps nothing")

        page = _login_page()
        _, events = captured(page, [{"action": "goto", "url": "/login"}, {"action": "probe"}])
        assert_true(not [e for e in events if e["type"] == "artifact"] and "content" not in page.calls, "a passing run captures nothing")
        probe_report = build_report([*events, finished], {"claims": []})
        assert_true(probe_report["probes"][0]["step"] == 2 and "probes:\n- step 2: headings: Masuk" in evidence_block(probe_report), "a probe step lands in the report and the evidence block")

        # --- trace policy through run_scenario, with a stand-in Playwright module -------------------
        tracing_calls: list[tuple] = []
        context_events: list[str] = []

        class _Tracing:
            def start(self, **kwargs):
                tracing_calls.append(("start", kwargs))

            def stop(self, path=None):
                tracing_calls.append(("stop", path))
                if path:
                    Path(path).write_bytes(b"PK fake trace")

        class _Context:
            tracing = _Tracing()

            def set_default_timeout(self, ms):
                pass

            def set_default_navigation_timeout(self, ms):
                pass

            def new_page(self):
                return _login_page()

            def route(self, pattern, handler):
                pass

            def on(self, event, handler):
                context_events.append(event)

            def close(self):
                tracing_calls.append(("close_context", None))

        class _Browser:
            def new_context(self):
                return _Context()

            def close(self):
                tracing_calls.append(("close_browser", None))

        launch_calls: list[dict] = []

        @contextlib.contextmanager
        def fake_sync_playwright():
            def launch(**kwargs):
                launch_calls.append(kwargs)
                return _Browser()

            yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        def real_run(steps: list[dict], *, secrets: bool, **config) -> tuple[int, list[dict]]:
            tracing_calls.clear()
            emitted: list[dict] = []
            saved_module = sys.modules.get("playwright.sync_api", "absent")
            sys.modules["playwright.sync_api"] = SimpleNamespace(sync_playwright=fake_sync_playwright)
            try:
                code = run_scenario({"steps": steps}, {"browser": "chromium", "base_url": BASE, "step_timeout_ms": 200, **config}, str(art), emitted.append, has_secrets=secrets)
            finally:
                if saved_module == "absent":
                    sys.modules.pop("playwright.sync_api", None)
                else:
                    sys.modules["playwright.sync_api"] = saved_module
            return code, emitted

        failing = [{"action": "goto", "url": "/login"}, {"action": "expect_url", "contains": "/dashboard", "claim_id": "login"}]
        code, emitted = real_run(failing, secrets=False)
        assert_true(tracing_calls[0][0] == "start" and ("stop", str(art / "trace.zip")) in tracing_calls, f"a failed run keeps its trace: {tracing_calls}")
        assert_true(
            code == 0 and any(e.get("kind") == "trace" for e in emitted) and emitted[-1] == finished,
            "the kept trace is announced before the result",
        )
        assert_true(tracing_calls[-2:] == [("close_context", None), ("close_browser", None)], f"context and browser close after the trace is saved: {tracing_calls}")
        code, emitted = real_run([{"action": "goto", "url": "/login"}], secrets=False)
        assert_true(("stop", None) in tracing_calls and not any(e.get("type") == "artifact" for e in emitted), "a passing run discards its trace")
        code, emitted = real_run(failing, secrets=True)
        assert_true(not any(call[0] == "start" for call in tracing_calls), f"no trace is recorded once ${{ENV}} values resolved: {tracing_calls}")
        assert_true(any(e.get("kind") == "skipped" and e.get("name") == "trace.zip" for e in emitted), "and the skipped trace is named")

        # slow_mo_ms reaches the launch, clamped; absent or unreadable runs at full speed
        for given, expected in ((None, 0), (700, 700), (-5, 0), (99999, 5000), ("slow", 0)):
            real_run([{"action": "goto", "url": "/login"}], secrets=False, **({} if given is None else {"slow_mo_ms": given}))
            assert_true(
                launch_calls[-1] == {"headless": False, "slow_mo": expected, "args": []},
                f"slow_mo_ms={given!r} launches with {launch_calls[-1]}",
            )
        # Request metadata listens on the context, not the page: a popup's requests start on
        # the context and must also finish there, or they read as unfinished (DEC-014).
        assert_true(
            {"request", "response", "requestfinished", "requestfailed"} <= set(context_events),
            f"every network lifecycle event is registered on the context: {sorted(set(context_events))}",
        )
        # headed is the shipped default; a config that asks for headless still gets it
        real_run([{"action": "goto", "url": "/login"}], secrets=False, headless=True)
        assert_true(launch_calls[-1]["headless"] is True, f"headless: true reaches the launch: {launch_calls[-1]}")

        # preflight's write decision reaches the browser as a resolver pin, so the name it
        # approved cannot come back pointing somewhere else mid-run
        real_run([{"action": "goto", "url": "/login"}], secrets=False, host_pins={"devbox.lan": "192.168.1.40"})
        assert_true(
            launch_calls[-1]["args"] == ["--host-resolver-rules=MAP devbox.lan 192.168.1.40"],
            f"an approved write host is pinned at launch: {launch_calls[-1]}",
        )
        from exoma.core.evidence.e2e.browser import _launch_args

        assert_true(
            _launch_args({"host_pins": {"devbox.lan": "192.168.1.40"}, "browser": "firefox"}) == [] and _launch_args({"browser": "chromium"}) == [],
            "a browser that cannot take the flag is never handed it, and no pin means no flag",
        )
        assert_true(
            _launch_args({"host_pins": {"devbox.lan": "fd00::40"}}) == ["--host-resolver-rules=MAP devbox.lan [fd00::40]"],
            "an IPv6 pin is bracketed, or Chromium reads its colons as a port",
        )
    finally:
        shutil.rmtree(art, ignore_errors=True)
