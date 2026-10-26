"""Real Chromium against a local fixture app, through the whole e2e runner (plan §22).

Opt-in: runs only with WORKFLOW_E2E_SMOKE=1, and even then skips cleanly when Playwright
or its browser is not installed, so a default `tests/run.py` never needs a browser.
Every rule the pipeline applies is proven elsewhere without one; this is the check that
the real player, the real browser, and the real process tree behave the way those
checks assume.

No spec provider is involved: every case is a `run` request carrying its scenario. The
hybrid review always runs, so a stand-in reviewer answers it with a clean review.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, quote_plus

from exoma.core.evidence.e2e import preflight as e2e_preflight
from exoma.core.evidence.e2e.request import request_path
from exoma.core.evidence.e2e.runner import FAKE_ENV
from exoma.core.evidence.result_shaping import _finalize_verify_result, _verify_exit_code
from exoma.core.provider.executor import Executor
from exoma.core.runtime.state import ensure_workflow_workspace
from tests.checks.e2e_routing import _REVIEW_CLEAN
from tests.checks.support import assert_true
from tests.checks.support import no_console

SMOKE_ENV = "WORKFLOW_E2E_SMOKE"
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "e2e_app"
THIRD_PARTY_TOKEN = "{{THIRD_PARTY}}"
USER = "smoke.operator@internal.example"
PASSWORD = "Sm0ke pass&word"
_CLAIMS = [{"id": "login", "severity": "blocking", "source_refs": ["req:SMOKE-LOGIN"]}]  # runs in a temp project: no files to cite


class _Reviewer:
    """Answers the stage-3 review; a spec request from this suite is a bug in the runner."""

    last_call_meta = None

    def run(self, prompt, session, model=None, work_dir=None):
        if "command: e2e_spec" in prompt:
            raise AssertionError("a run request carries its scenario; the smoke suite must never ask for a spec")
        self.last_call_meta = {"returncode": 0, "duration_seconds": 0.01}
        return {"ok": True, "content": _REVIEW_CLEAN, "meta": {"provider_session_id": "ses_smoke"}}


def _send(handler: http.server.BaseHTTPRequestHandler, status: int, body: bytes, content_type: str) -> None:
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass  # the player was killed mid-request; nothing to answer


# A fetch-driven CRUD page, served inline: a loader shown while data loads (with a delay, so
# readiness has something to wait for), a list rendered from the API, and a poll that keeps
# the network busy forever — the page `networkidle` never settles on.
_CRUD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Items</title></head>
<body>
  <div data-e2e="loader">Loading...</div>
  <p data-e2e="count"></p>
  <ul data-e2e="items"></ul>
  <form id="add"><label>Name <input name="name"></label><button type="submit">Add item</button></form>
  <script>
    const loader = document.querySelector('[data-e2e=loader]');
    const list = document.querySelector('[data-e2e=items]');
    const count = document.querySelector('[data-e2e=count]');
    async function call(method, path, body) {
      loader.hidden = false;
      await fetch(path, {method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
      await refresh();
    }
    async function refresh() {
      loader.hidden = false;
      const items = await (await fetch('/api/items')).json();
      await new Promise((done) => setTimeout(done, 600));
      list.innerHTML = '';
      for (const item of items) {
        const row = document.createElement('li');
        row.dataset.e2e = 'row-' + item.name;
        row.textContent = item.name + ' ' + item.status + ' ';
        for (const [label, method, body] of [['Mark done', 'PATCH', {status: 'done'}], ['Reset', 'PUT', {name: item.name, status: 'open'}], ['Delete', 'DELETE', null]]) {
          const button = document.createElement('button');
          button.textContent = label;
          button.onclick = () => call(method, '/api/items/' + item.id, body);
          row.append(button);
        }
        list.append(row);
      }
      count.textContent = items.length + ' items';
      loader.hidden = true;
    }
    document.querySelector('#add').onsubmit = (event) => { event.preventDefault(); call('POST', '/api/items', {name: event.target.name.value, status: 'open'}); };
    setInterval(() => fetch('/api/ping'), 250);
    refresh();
  </script>
</body></html>
"""
# A form whose POST the server answers with a 307 to another origin: the browser would re-send
# the body there without the route handler ever seeing it.
_BOUNCE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Bounce</title></head>
<body><form method="post" action="/items/bounce"><input type="hidden" name="id" value="7"><button type="submit">Send and bounce</button></form></body></html>
"""
# Login the way a SPA does it: a fetch POST, then a client-side navigation once it answers.
# The POST crosses the write guard's route handler, which Playwright sync only runs while the
# player is inside a Playwright call — a poll that sleeps outside one holds it (DEC-007).
# Both start a second after the click, so they cannot finish inside the click's own
# auto-wait: they happen while the next step, expect_url, is polling.
_GUARDED_LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Guarded login</title></head>
<body><button type="button" id="login">Sign in</button>
<script>
  document.getElementById('login').onclick = () => setTimeout(async () => {
    await fetch('/api/session', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    location.href = '/dashboard.html';
  }, 1000);
</script></body></html>
"""
_SLOW_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Slow</title></head>
<body><form method="post" action="/items/slow"><button type="submit">Send slowly</button></form></body></html>
"""


class _AppHandler(http.server.BaseHTTPRequestHandler):
    third_party = ""
    writes: list[str] = []
    items: dict[int, dict] = {}
    next_id = [1]
    fail_delete = [False]

    def log_message(self, *args) -> None:
        pass

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def _item_id(self, path: str) -> int | None:
        head, _, tail = path.rpartition("/")
        return int(tail) if head == "/api/items" and tail.isdigit() else None

    def do_POST(self) -> None:
        # Counted before anything else: the write guard's proof is that this list stays empty.
        path = self.path.split("?", 1)[0]
        self.writes.append(path)
        if path == "/api/items":
            body = self._body()
            item_id = self.next_id[0]
            self.next_id[0] += 1
            self.items[item_id] = {"id": item_id, "name": str(body.get("name")), "status": str(body.get("status") or "open")}
            return _send(self, 201, json.dumps(self.items[item_id]).encode("utf-8"), "application/json")
        self._body()
        if path == "/items/delete":
            return _send(self, 200, b"<!doctype html><title>Deleted</title><p>deleted</p>", "text/html; charset=utf-8")
        if path == "/items/bounce":
            self.send_response(307)
            self.send_header("Location", "http://collector.example/collect")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path == "/api/session":
            time.sleep(0.3)
            return _send(self, 200, b"{}", "application/json")
        if path == "/items/slow":
            time.sleep(4)
            return _send(self, 200, b"<!doctype html><title>Sent</title><p>sent</p>", "text/html; charset=utf-8")
        if path == "/search":
            return _send(self, 200, b"<!doctype html><title>Results</title><p>1 result</p>", "text/html; charset=utf-8")
        return _send(self, 404, b"not found", "text/plain")

    def _update(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        self.writes.append(f"{method} {path}")
        body = self._body()
        item_id = self._item_id(path)
        if item_id not in self.items:
            return _send(self, 404, b"not found", "text/plain")
        if method == "PUT":
            self.items[item_id] = {"id": item_id, "name": str(body.get("name")), "status": str(body.get("status"))}
        else:
            self.items[item_id].update({k: str(v) for k, v in body.items() if k in ("name", "status")})
        return _send(self, 200, json.dumps(self.items[item_id]).encode("utf-8"), "application/json")

    def do_PUT(self) -> None:
        self._update("PUT")

    def do_PATCH(self) -> None:
        self._update("PATCH")

    def do_DELETE(self) -> None:
        path = self.path.split("?", 1)[0]
        self.writes.append(f"DELETE {path}")
        item_id = self._item_id(path)
        if self.fail_delete[0] or item_id not in self.items:
            return _send(self, 500, b"cannot delete", "text/plain")
        del self.items[item_id]
        return _send(self, 204, b"", "text/plain")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/boom":
            return _send(self, 503, b"down", "text/plain")
        if path == "/hang":
            time.sleep(20)
            return _send(self, 200, b"late", "text/plain")
        if path == "/api/items":
            return _send(self, 200, json.dumps(list(self.items.values())).encode("utf-8"), "application/json")
        if path == "/api/ping":
            return _send(self, 200, b"{}", "application/json")
        if path == "/crud.html":
            return _send(self, 200, _CRUD_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/go-away":
            self.send_response(302)
            self.send_header("Location", f"{self.third_party}/phish")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path == "/bounce.html":
            return _send(self, 200, _BOUNCE_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/slow.html":
            return _send(self, 200, _SLOW_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/guarded_login.html":
            return _send(self, 200, _GUARDED_LOGIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        page = FIXTURE_DIR / path.lstrip("/")
        if path.endswith(".html") and page.parent == FIXTURE_DIR and page.is_file():
            body = page.read_text(encoding="utf-8").replace(THIRD_PARTY_TOKEN, self.third_party)
            return _send(self, 200, body.encode("utf-8"), "text/html; charset=utf-8")
        return _send(self, 404, b"not found", "text/plain")


class _ThirdPartyHandler(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self.hits.append(self.path)
        if self.path.startswith("/widget.js"):
            return _send(self, 200, b"throw new Error('third-party widget failed');", "application/javascript")
        if self.path.startswith("/phish"):
            return _send(self, 200, b"<title>Phish</title>", "text/html")
        return _send(self, 503, b"down", "text/plain")


def _serve(handler) -> tuple[http.server.ThreadingHTTPServer, str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _playwright_browser_processes() -> int | None:
    """Browser processes started from Playwright's own browser cache; None when unknown."""
    if os.name == "nt":
        command = [
            "powershell", "-NoProfile", "-Command",
            "@(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*ms-playwright*' }).Count",
        ]
    else:
        command = ["sh", "-c", "ps -eo args | grep -c '[m]s-playwright'"]
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=30, **no_console())
        return int((out.stdout or "").strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _leaks(result: dict, directory: Path) -> list[str]:
    needles = {form.encode("utf-8") for value in (USER, PASSWORD) for form in (value, quote(value, safe=""), quote_plus(value))}
    found = [p.name for p in directory.iterdir() if p.is_file() and any(n in p.read_bytes() for n in needles)]
    rendered = json.dumps(result, ensure_ascii=False)
    if any(n.decode("utf-8") in rendered for n in needles):
        found.append("result")
    return found


def _test_e2e_real_browser_smoke() -> None:
    if os.environ.get(SMOKE_ENV) != "1":
        print(f"  e2e-smoke: skipped (set {SMOKE_ENV}=1 to drive real Chromium)")
        return
    ok, detail = e2e_preflight.playwright_available()
    if ok:
        ok, detail = e2e_preflight.browser_available("chromium")
    if not ok:
        print(f"  e2e-smoke: skipped ({detail})")
        return

    saved = {name: os.environ.get(name) for name in (FAKE_ENV, "E2E_USER", "E2E_PASS")}
    third, third_url = _serve(_ThirdPartyHandler)
    app, base = _serve(type("FixtureApp", (_AppHandler,), {"third_party": third_url}))
    roots: list[Path] = []
    try:
        os.environ.pop(FAKE_ENV, None)
        os.environ["E2E_USER"] = USER
        os.environ["E2E_PASS"] = PASSWORD
        before = _playwright_browser_processes()

        def case(steps: list[dict], cleanup: list[dict] | None = None, **e2e) -> tuple[dict, dict, set[str], Path]:
            steps = [dict(step, id=step.get("id") or f"step-{n}") for n, step in enumerate(steps, 1)]
            root = Path(tempfile.mkdtemp(prefix="e2e-smoke-"))
            roots.append(root)
            ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
            request = request_path(root, "e2e-smoke")
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "phase": "run",
                        "settings": {"base_url": base, "step_timeout_ms": 3000, "nav_timeout_ms": 10000, "headless": True, **e2e},
                        "scenario": {"version": 1, "feature": "smoke", "claims": _CLAIMS, "steps": steps, **({"cleanup": cleanup} if cleanup else {})},
                    }
                ),
                encoding="utf-8",
            )
            result = Executor(adapter=_Reviewer()).execute("verify-browser", "smoke", {"session_id": "e2e-smoke"}, str(root))
            assert_true(result["meta"].get("command") == "verify", f"a run request is judged as a verification: {result['meta']}")
            result = _finalize_verify_result("verify", result)
            directory = Path(result["meta"]["e2e"]["artifacts"])
            report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
            return result, report, {p.name for p in directory.iterdir() if p.is_file()}, directory

        login = [
            {"action": "goto", "url": "/login.html"},
            {"action": "fill", "selector": {"label": "Email"}, "selector_provenance": {"type": "source"}, "value": "${E2E_USER}"},
            {"action": "fill", "selector": {"label": "Password"}, "selector_provenance": {"type": "source"}, "value": "${E2E_PASS}"},
            {"action": "click", "selector_candidates": [
                {"selector": {"role": "button", "name": "Sign in"}, "selector_provenance": {"type": "heuristic"}},
                {"selector": {"role": "button", "name": "Masuk"}, "selector_provenance": {"type": "source"}},
            ]},
        ]

        # --- a clean flow passes; the page echoes the typed address and nothing keeps it ----------
        result, report, files, directory = case([
            *login,
            {"action": "expect_url", "contains": "/dashboard.html", "claim_id": "login"},
            {"action": "expect_dom", "selector": {"e2e": "banner"}, "text": "Welcome back", "claim_id": "login"},
        ])
        assert_true(report["browser_verdict"] == "pass", f"pass: {report['reason']} {report['failures']}")
        assert_true(result["meta"]["verdict"] == "pass" and _verify_exit_code("verify", result) == 0, f"browser pass + clean review: pass, exit 0: {result['meta']['verdict']}")
        assert_true("trace.zip" not in files and not any(name.endswith(".png") for name in files), f"a pass keeps no screenshot or trace: {sorted(files)}")
        # Request metadata from a real Chromium (DEC-014): the page's loads were seen, with
        # sizes and timing, and no URL carries a query.
        network = report.get("network") or {}
        summary = network.get("summary") or {}
        rows = network.get("rows") or []
        assert_true(
            summary.get("requests", 0) > 0 and rows and any(row.get("navigation") for row in rows)
            and any(isinstance(row.get("sizes"), dict) for row in rows)
            and all("?" not in str(row.get("url")) for row in rows),
            f"a real run records the page's requests with sizes and sanitized URLs: {summary}",
        )
        assert_true(_leaks(result, directory) == [], f"no raw or encoded credential anywhere: {_leaks(result, directory)}")

        # --- the same flow failing: HTML kept and scrubbed, screenshot and trace withheld ---------
        result, report, files, directory = case([*login, {"action": "expect_url", "contains": "/settings", "claim_id": "login"}])
        assert_true(report["browser_verdict"] == "fail" and _verify_exit_code("verify", result) == 2, f"a failed assertion fails: {report['reason']}")
        assert_true("step05.html" in files and "step05.png" not in files and "trace.zip" not in files, f"with credentials in play only text is kept: {sorted(files)}")
        assert_true("${E2E_USER}" in (directory / "step05.html").read_text(encoding="utf-8"), "the rendered address is a placeholder in the kept HTML")
        assert_true(_leaks(result, directory) == [], f"no raw or encoded credential anywhere: {_leaks(result, directory)}")
        assert_true("screenshot skipped" in (directory / "evidence.md").read_text(encoding="utf-8"), "the skip is stated in the evidence")

        # --- DOM assertion fails without credentials: screenshot and trace kept ---------------------
        result, report, files, _ = case([
            {"action": "goto", "url": "/wrong_text.html"},
            {"action": "expect_dom", "selector": {"e2e": "banner"}, "selector_provenance": {"type": "source"}, "text": "Welcome back", "claim_id": "login"},
        ])
        assert_true(report["browser_verdict"] == "fail" and report["claims"]["login"]["origin"] == "app", f"wrong text is the app's: {report['claims']}")
        assert_true({"step02.html", "step02.png", "trace.zip"} <= files, f"page evidence and trace kept on failure: {sorted(files)}")

        # --- wrong redirect ---------------------------------------------------------------------------
        _, report, _, _ = case([
            {"action": "goto", "url": "/broken_login.html"},
            {"action": "click", "selector": {"role": "button", "name": "Masuk"}, "selector_provenance": {"type": "source"}},
            {"action": "expect_url", "contains": "/dashboard.html", "claim_id": "login"},
        ])
        assert_true(
            report["browser_verdict"] == "fail" and report["failures"][0]["action"] == "expect_url" and "error=1" in json.dumps(report["failures"][0]["actual"]),
            f"a wrong redirect fails with the URL it landed on: {report['failures']}",
        )

        # --- a guessed selector that misses is the scenario's; the probe shows what was there -------
        result, report, files, directory = case([
            {"action": "goto", "url": "/login.html"},
            {"action": "click", "selector": {"css": "#does-not-exist"}, "selector_provenance": {"type": "heuristic"}},
            {"action": "expect_url", "contains": "/dashboard.html", "claim_id": "login"},
        ])
        evidence = (directory / "evidence.md").read_text(encoding="utf-8")
        assert_true(report["browser_verdict"] == "incomplete" and report["failures"][0]["origin"] == "scenario" and report["reason"] == "scenario_error", f"heuristic miss: {report['failures']}")
        assert_true("probe:" in evidence and "Masuk" in evidence and not any(name.endswith(".png") for name in files), f"probe instead of a screenshot:\n{evidence}")

        # --- an uncaught same-origin page error fails a run whose assertions passed -----------------
        _, report, _, _ = case([
            {"action": "goto", "url": "/page_error.html"},
            {"action": "expect_title", "equals": "Page error", "claim_id": "login"},
        ])
        assert_true(report["browser_verdict"] == "fail" and [e["kind"] for e in report["app_errors"]] == ["page_error"], f"page error: {report['app_errors']}")

        # --- main-request 5xx ---------------------------------------------------------------------------
        _, report, _, _ = case([
            {"action": "goto", "url": "/boom"},
            {"action": "expect_title", "contains": "", "claim_id": "login"},
        ])
        assert_true(
            report["browser_verdict"] == "fail" and any(e["kind"] == "http_5xx" and e["main_request"] for e in report["app_errors"]),
            f"a 5xx main request fails: {report['app_errors']}",
        )

        # --- third-party widget error and beacon 503 stay warnings ----------------------------------
        _, report, _, _ = case([
            {"action": "goto", "url": "/third_party.html"},
            {"action": "wait_dom", "selector": {"e2e": "beacon-done"}, "selector_provenance": {"type": "source"}},
            {"action": "expect_dom", "selector": {"e2e": "greeting"}, "selector_provenance": {"type": "source"}, "text": "Hello", "claim_id": "login"},
        ])
        noise = {(o["kind"], o["same_origin"]) for o in report["observations"]}
        assert_true(report["browser_verdict"] == "pass", f"third-party noise must not fail the run: {report['app_errors']}")
        assert_true(("page_error", False) in noise and ("http_5xx", False) in noise, f"and it is still observed: {noise}")

        # --- a click that leaves the origin is stopped before the request is sent -------------------
        _ThirdPartyHandler.hits.clear()
        _, report, _, _ = case([
            {"action": "goto", "url": "/offsite.html"},
            {"action": "click", "selector": {"role": "button", "name": "Continue"}, "selector_provenance": {"type": "source"}},
            {"action": "expect_url", "contains": "/phish", "claim_id": "login"},
        ])
        assert_true(
            report["browser_verdict"] == "incomplete" and "blocked" in report["failures"][0]["detail"],
            f"off-origin navigation is a scenario stop: {report['failures']}",
        )
        assert_true("/phish" not in _ThirdPartyHandler.hits, f"the blocked origin never received the request: {_ThirdPartyHandler.hits}")

        # --- a redirect off the origin: never routed, so caught after the browser followed it -------
        _, report, _, _ = case([
            {"action": "goto", "url": "/go-away"},
            {"action": "expect_url", "contains": "/phish", "claim_id": "login"},
        ])
        assert_true(
            report["browser_verdict"] == "incomplete" and report["failures"]
            and "followed by the browser" in report["failures"][0]["detail"],
            f"a navigation redirected off the origin policy fails its step and says it was followed: {report['failures']}",
        )

        # --- a form POST is refused while every write switch is off: the server never sees it -------
        # allow_local_side_effects (default on) sends ordinary writes to a loopback host by design;
        # the refusal is only provable with it switched off too.
        delete = [
            {"action": "goto", "url": "/mutate.html"},
            {"action": "click", "selector": {"role": "button", "name": "Delete item"}, "selector_provenance": {"type": "source"}},
            {"action": "expect_title", "equals": "Deleted", "claim_id": "login"},
        ]
        _AppHandler.writes.clear()
        _, report, _, _ = case(delete, allow_local_side_effects=False)
        assert_true(_AppHandler.writes == [], f"the refused write never reached the app: {_AppHandler.writes}")
        assert_true(
            report["browser_verdict"] == "incomplete" and report["failures"] and report["failures"][0]["origin"] == "scenario"
            and "allow_side_effects" in report["failures"][0]["detail"],
            f"a refused write is a scenario stop, not an app failure: {report['browser_verdict']} {report['failures']}",
        )

        blocked = [r for r in report["requests"] if r["blocked"]]
        assert_true(
            len(blocked) == 1 and blocked[0]["method"] == "POST" and blocked[0]["endpoint"].endswith("/items/delete") and blocked[0]["step_id"] == "step-2",
            f"and the refused write is in the ledger, on its step: {report['requests']}",
        )

        # --- side effects on against a loopback app: the write is sent once --------------------------
        _AppHandler.writes.clear()
        _, report, _, _ = case(delete, allow_side_effects=True)
        assert_true(_AppHandler.writes == ["/items/delete"] and report["browser_verdict"] == "pass", f"allow_side_effects sends a write to a loopback app: {_AppHandler.writes} {report['reason']}")

        # --- a guarded write followed by expect_url: the poll keeps the route handler running (DEC-007)
        # Default settings: the login POST reaches loopback through allow_local_side_effects, the
        # path a real login takes.
        _AppHandler.writes.clear()
        _, report, _, _ = case([
            {"id": "open", "action": "goto", "url": "/guarded_login.html"},
            {"id": "sign-in", "action": "click", "selector": {"role": "button", "name": "Sign in"}, "selector_provenance": {"type": "source"}},
            {"id": "landed", "action": "expect_url", "contains": "/dashboard.html", "claim_id": "login"},
        ], step_timeout_ms=5000)
        sent = [(r["method"], r["step_id"]) for r in report["requests"]]
        assert_true(
            report["browser_verdict"] == "pass" and _AppHandler.writes == ["/api/session"],
            f"the guarded POST is released and the redirect lands: {report['reason']} {report['failures']} {_AppHandler.writes}",
        )
        assert_true(
            sent == [("POST", "landed")],
            f"the POST went through the guard while expect_url was polling, not inside the click: {sent}",
        )

        # --- a write the server redirects off policy is not re-sent: the guard follows it itself -------
        bounce = [
            {"action": "goto", "url": "/bounce.html"},
            {"action": "click", "selector": {"role": "button", "name": "Send and bounce"}, "selector_provenance": {"type": "source"}},
            {"action": "expect_title", "equals": "Collected", "claim_id": "login"},
        ]
        _AppHandler.writes.clear()
        _, report, _, _ = case(bounce, allow_side_effects=True)
        refused = [r for r in report["requests"] if r["blocked"]]
        assert_true(
            _AppHandler.writes == ["/items/bounce"] and report["browser_verdict"] == "incomplete"
            and refused and "collector.example" in refused[0]["failure"],
            f"a 307 that would carry the body off policy is refused before the second send: {_AppHandler.writes} {report['browser_verdict']} {report['requests']}",
        )

        # --- CRUD on a fetch-driven page: readiness, scoped selectors, ledger, cleanup ----------------
        name = "e2e-smoke-item"
        row = {"e2e": f"row-{name}"}
        loaded = [{"hidden": {"e2e": "loader"}}]
        crud_steps = [
            {"id": "open", "action": "goto", "url": "/crud.html", "ready": [*loaded, {"text": "0 items"}]},
            {"id": "fill-name", "action": "fill", "selector": {"label": "Name"}, "selector_provenance": {"type": "source"}, "value": name},
            {"id": "create", "action": "click", "selector": {"role": "button", "name": "Add item"}, "selector_provenance": {"type": "source"},
             "side_effect": "creates_test_data", "test_environment_required": True, "test_data": {"marker": name}, "request": {"method": "POST", "path": "/api/items"}},
            {"id": "assert-created", "action": "expect_dom", "selector": row, "text": f"{name} open", "claim_id": "login", "ready": [{"visible": row}, *loaded]},
            {"id": "mark-done", "action": "click", "selector": {"role": "button", "name": "Mark done"}, "within": row,
             "side_effect": "modifies_test_data", "test_environment_required": True, "test_data": {"marker": name},
             "no_cleanup_reason": "the row is deleted by the cleanup of create", "request": {"method": "PATCH", "path": "/api/items/:id"}},
            {"id": "assert-done", "action": "expect_dom", "selector": row, "text": f"{name} done", "claim_id": "login"},
            {"id": "reset", "action": "click", "selector": {"role": "button", "name": "Reset"}, "within": row, "ready": loaded,
             "side_effect": "modifies_test_data", "test_environment_required": True, "test_data": {"marker": name},
             "no_cleanup_reason": "the row is deleted by the cleanup of create", "request": {"method": "PUT", "path": "/api/items/:id"}},
            {"id": "assert-open", "action": "expect_dom", "selector": row, "text": f"{name} open", "claim_id": "login"},
        ]
        crud_cleanup = [
            {"id": "reopen", "cleans": "create", "action": "goto", "url": "/crud.html", "ready": loaded},
            {"id": "delete", "cleans": "create", "action": "click", "selector": {"role": "button", "name": "Delete"}, "within": row,
             "request": {"method": "DELETE", "path": "/api/items/:id"}},
            {"id": "assert-deleted", "cleans": "create", "action": "expect_dom", "selector": {"e2e": "count"}, "text": "0 items", "ready": loaded},
        ]
        # DELETE is never covered by a write switch: the cleanup's endpoint is approved explicitly
        crud_settings = {"allow_side_effects": True, "step_timeout_ms": 6000, "allowed_destructive_requests": ["DELETE /api/items/:id"]}
        _AppHandler.writes.clear()
        _AppHandler.items.clear()
        result, report, _, _ = case(crud_steps, crud_cleanup, **crud_settings)
        writes = [w for w in _AppHandler.writes if not w.startswith("GET")]
        assert_true(report["browser_verdict"] == "pass" and report["cleanup"]["status"] == "passed", f"CRUD passes and cleans up: {report['reason']} {report['failures']} {report['cleanup']}")
        assert_true(
            writes == ["/api/items", "PATCH /api/items/1", "PUT /api/items/1", "DELETE /api/items/1"] and _AppHandler.items == {},
            f"each write reached the app exactly once and the data is gone: {writes} {_AppHandler.items}",
        )
        ledger = [(r["method"], r["step_id"], r["status"], r["planned"], r["attribution"]) for r in report["requests"]]
        assert_true(
            ledger == [("POST", "create", 201, True, "step"), ("PATCH", "mark-done", 200, True, "step"), ("PUT", "reset", 200, True, "step"), ("DELETE", "delete", 204, True, "step")],
            f"the ledger ties every write to its step, planned, with its status; polls are not writes: {ledger}",
        )
        assert_true(all(r["endpoint"].endswith(("/api/items", "/api/items/:id")) and r["duration_ms"] is not None for r in report["requests"]), f"endpoints sanitised, durations measured: {report['requests']}")
        assert_true(result["meta"]["verdict"] == "pass" and _verify_exit_code("verify", result) == 0, f"a clean CRUD run is a pass: {result['meta']['verdict']}")

        _AppHandler.writes.clear()
        _AppHandler.items.clear()
        _AppHandler.fail_delete[0] = True
        try:
            result, report, _, directory = case(crud_steps, crud_cleanup, **crud_settings)
        finally:
            _AppHandler.fail_delete[0] = False
        assert_true(
            report["browser_verdict"] == "pass" and report["cleanup"]["status"] == "failed" and result["meta"]["verdict"] == "incomplete"
            and _verify_exit_code("verify", result) == 2,
            f"a cleanup that could not delete keeps a passing test from passing, non-zero: {report['cleanup']} {result['meta']['verdict']}",
        )
        assert_true("e2e cleanup: 'create' failed" in result["content"] and "cleanup: failed" in (directory / "evidence.md").read_text(encoding="utf-8"), "and says so in the contract and the evidence")
        _AppHandler.items.clear()

        # --- a write whose response outlives the step is sent once, never again ---------------------
        _AppHandler.writes.clear()
        _, report, _, _ = case([
            {"action": "goto", "url": "/slow.html"},
            {"action": "click", "selector": {"role": "button", "name": "Send slowly"}, "selector_provenance": {"type": "source"},
             "side_effect": "modifies_test_data", "test_environment_required": True, "test_data": {"marker": "slow"}, "no_cleanup_reason": "the fixture keeps nothing"},
            {"action": "expect_title", "equals": "Sent", "claim_id": "login"},
        ], allow_side_effects=True, step_timeout_ms=1000)
        time.sleep(5)
        assert_true(_AppHandler.writes == ["/items/slow"], f"the slow write reached the app exactly once: {_AppHandler.writes}")
        assert_true(report["browser_verdict"] != "pass", f"and the step that timed out is not a pass: {report['browser_verdict']} {report['failures']}")

        # --- a search form POSTs to read: refused by default, sent once when confirmed as a read ----
        search = [
            {"action": "goto", "url": "/search.html"},
            {"action": "click", "selector": {"role": "button", "name": "Search"}, "selector_provenance": {"type": "source"}},
            {"action": "expect_title", "equals": "Results", "claim_id": "login"},
        ]
        _AppHandler.writes.clear()
        _, report, _, _ = case(search, allow_local_side_effects=False)
        assert_true(_AppHandler.writes == [] and report["browser_verdict"] == "incomplete", f"an unconfirmed read-shaped POST is still refused: {_AppHandler.writes} {report['browser_verdict']}")
        _AppHandler.writes.clear()
        _, report, _, _ = case(search, allow_local_side_effects=False, allowed_read_only_requests=["POST /search"])
        assert_true(_AppHandler.writes == ["/search"] and report["browser_verdict"] == "pass", f"a confirmed read reaches the app once and the flow completes: {_AppHandler.writes} {report['reason']} {report['failures']}")
        assert_true(
            any(o["kind"] == "read_only_request_allowed" for o in report["observations"]),
            f"and the run reports what it let through: {[o['kind'] for o in report['observations']]}",
        )

        # --- a hung navigation: idle timeout, process tree killed, no browser left behind -----------
        _, report, _, _ = case([
            {"action": "goto", "url": "/hang"},
            {"action": "expect_url", "contains": "/hang", "claim_id": "login"},
        ], idle_timeout_s=4, nav_timeout_ms=25000)
        assert_true(report["browser_verdict"] == "incomplete" and report["reason"] == "stuck", f"a hung page is stuck: {report['reason']}")
        time.sleep(2)
        after = _playwright_browser_processes()
        if before is not None and after is not None:
            assert_true(after <= before, f"the killed player left no browser behind: before={before} after={after}")
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for server in (app, third):
            server.shutdown()
            server.server_close()
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)
