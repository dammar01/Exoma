"""What earlier browser runs learned about the application, reused by the next draft.

The browser-side counterpart of `core/evidence/fact_store.py`, at the same level: written
automatically from runs, anchored where it can be, aged out on its own, never tracked by
Git. Knowledge worth sharing is lifted into Git through `/.promote`
(`promotable_claims`), exactly as facts are.

What a run records, per application origin (scheme://host:port of base_url):

  auth.login    the steps from the login page's goto through the first assertion after the
                password was typed — how this app signs in
  auth.logout   the step that signed out, when a later assertion proved the login page came
                back
  navigation    a route a goto reached
  page_ready    a route's readiness conditions that held
  selector      a selector that matched exactly one element for an action on a route
  failure_hint  a selector a run could not use, with the diagnosis that says why
                (`classify.diagnose`, reusable causes only) — what the next draft should
                not propose again as it was

Only what a run PROVED is recorded: a step that passed, in a run that passed on its first
attempt, that wrote nothing. A step that fails against a recorded entry counts against it,
and STALE_AFTER_FAILS consecutive failures retire it — the page changed, and a hint that
no longer matches is worse than no hint.

Nothing here holds a credential. Entries are built from the PLACEHOLDER scenario the
runner archives (`${E2E_USER}`, never its value), and a `fill` value that is not a
placeholder is dropped rather than stored: typed test data is not application knowledge.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from exoma.core.evidence.e2e.redact import sanitize_endpoint
from exoma.core.workspace.workspace_paths import atomic_write_text, now_iso, workflow_paths
from exoma.utils.owned_lock import OwnedFileLock

FILENAME = "e2e-knowledge.jsonl"
LOCK_FILENAME = "e2e-knowledge.jsonl.lock"
SIDECAR_NAME = "e2e_knowledge.json"
LOCK_TTL_SECONDS = 30
MAX_ENTRIES = 300
# Two failures in a row: once can be a slow page, twice at the same place is a changed page.
STALE_AFTER_FAILS = 2
# Proven this many times before it may leave the project as Git-tracked knowledge.
PROMOTE_AFTER_PASSES = 3
PER_KIND_LIMIT = 5
REPEAT_FAILURE = "repeat_failure"
FAILURE_HINT = "failure_hint"
KIND_ORDER = (REPEAT_FAILURE, FAILURE_HINT, "auth.login", "auth.logout", "navigation", "page_ready", "selector")
_PLACEHOLDER = re.compile(r"^\$\{[A-Z0-9_]+\}$")
_PASSWORD_PLACEHOLDER = "${E2E_PASS}"
_ASSERTIONS = frozenset({"expect_url", "expect_dom", "expect_title"})
# Only what locates an element or proves a page state. Values, markers, and write
# declarations are left behind.
_STEP_KEYS = ("action", "url", "selector", "selector_candidates", "within", "ready", "contains", "equals", "matches", "text", "key")


def _store_path(project_root: Path) -> Path:
    return workflow_paths(Path(project_root))["e2e_knowledge"]


class _Lock(OwnedFileLock):
    """O_EXCL lock around the read-modify-write, with the fact store's ownership rules."""

    def __init__(self, project_root: Path) -> None:
        super().__init__(_store_path(project_root).with_name(LOCK_FILENAME), LOCK_TTL_SECONDS)


def load(project_root: Path) -> list[dict]:
    path = _store_path(project_root)
    if not path.exists():
        return []
    entries: list[dict] = []
    # Replacement, not strict: a line cut in the middle of a multi-byte character becomes
    # unparseable JSON and is skipped like any torn line, instead of failing every read.
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("id"):
            entries.append(entry)
    return entries


def _save(project_root: Path, entries: list[dict]) -> None:
    path = _store_path(project_root)
    # Newest verification last, so the cap drops what has gone longest unproven.
    entries = sorted(entries, key=lambda e: str(e.get("last_verified") or e.get("first_seen") or ""))[-MAX_ENTRIES:]
    atomic_write_text(
        path, "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in entries)
    )


def origin_of(base_url: str) -> str:
    parts = urlsplit(str(base_url or ""))
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}" if parts.scheme and parts.netloc else ""


def route_of(url: object, base_url: str) -> str:
    """The path a goto reaches: the route every step after it runs on. Shared with the spec
    validator, so `proven` is checked on the same route it was recorded on.

    Identifier segments read as `:id`, by the request ledger's rule (`sanitize_endpoint`):
    `/users/123/edit` and `/users/456/edit` are one page, so a selector proven on one is
    proven on the other, and no row is keyed by an id or token. Route words (`/users`,
    `/settings/profile`) stay apart."""
    try:
        sanitized = sanitize_endpoint(urljoin(base_url or "http://localhost/", str(url or "/")))
    except ValueError:
        return "/"
    if sanitized.startswith("["):
        return "/"
    return urlsplit(sanitized).path or "/"


_route = route_of


def proven_scope(route: str, within: object) -> dict:
    """Where a proven selector holds: the route and the `within` it matched under. The same
    selector on another page, or under another container, may name a different element or
    none, so it is neither offered as proven nor demoted there."""
    return {"route": route, "within": within if isinstance(within, dict) and within else None}


def _entry_id(kind: str, origin: str, key: str) -> str:
    return hashlib.sha256(f"{kind}\0{origin}\0{key}".encode("utf-8")).hexdigest()[:16]


def _step_view(step: dict) -> dict:
    """The part of a step worth remembering, in placeholder form only."""
    view = {key: step[key] for key in _STEP_KEYS if key in step}
    value = step.get("value")
    if isinstance(value, str) and _PLACEHOLDER.match(value):
        view["value"] = value
    return view


def _matched_selector(step: dict, selection: dict) -> dict | None:
    from exoma.core.evidence.e2e.spec import step_selectors

    index = selection.get("candidate")
    candidates = step_selectors(step)
    if not isinstance(index, int) or not 0 <= index < len(candidates):
        return None
    return candidates[index]["selector"]


def _proven_missed(step: dict, selection: dict, outcome: str) -> list[dict]:
    """`proven` candidates this step tried that did not match.

    Every proven candidate before the one that matched, or every one counted when none did. Matched
    against the store by selector, not by the step's key: a draft that puts a proven
    selector first has a different candidate list, so its key names a different entry.
    """
    from exoma.core.evidence.e2e.spec import step_selectors

    candidates = step_selectors(step)
    # Only a candidate the browser actually counted was tried. A step that failed before
    # resolve ran (not ready, an exception) has no selection: it proved nothing either way.
    counts = selection.get("match_counts")
    if not isinstance(counts, list):
        return []
    index = selection.get("candidate")
    # A step can fail after its selector matched (the click timed out, the fill was
    # refused); then only the candidates before the one that matched missed.
    pool = candidates[:index] if isinstance(index, int) else candidates
    tried = [c for c, count in zip(pool, counts) if count is not None]
    return [c["selector"] for c in tried if c.get("provenance") == "proven"]


def _uses_password(step: dict) -> bool:
    return step.get("value") == _PASSWORD_PLACEHOLDER


def _anchor(project_root: Path, ref: object) -> tuple[str | None, int | None, str | None]:
    from exoma.core.evidence.fact_store import _anchor_hash

    match = re.match(r"^(.+):(\d+)$", str(ref or "").strip())
    if not match:
        return None, None, None
    path, line = match.group(1), int(match.group(2))
    return path, line, _anchor_hash(project_root, path, line)


def observations(report: dict, scenario: dict, base_url: str) -> list[dict]:
    """What one run proved and disproved, as `{kind, key, route, outcome, body}` rows.

    `scenario` is the placeholder form. Steps are matched to the report's trail by id, which
    the spec requires to be stable.
    """
    steps = [s for s in (scenario or {}).get("steps") or [] if isinstance(s, dict)]
    status = {str(row.get("step_id")): row for row in report.get("trail") or []}
    rows: list[dict] = []
    route = "/"
    login: dict | None = None
    for index, step in enumerate(steps):
        row = status.get(str(step.get("id")))
        if row is None or row.get("status") not in ("passed", "failed"):
            continue
        outcome = "pass" if row["status"] == "passed" else "fail"
        # `side_effect: "none"` is the spec's word for no write (spec.SIDE_EFFECTS); read as
        # truthy it made every step a draft labelled explicitly a write, and no selector
        # was ever recorded from it (DEC-044).
        writes = step.get("side_effect") not in (None, "none") or bool(step.get("request"))
        action = step.get("action")
        if action == "goto":
            route = _route(step.get("url"), base_url)
            rows.append({"kind": "navigation", "key": route, "route": route, "outcome": outcome, "body": {"url": step.get("url")}})
            if isinstance(step.get("ready"), list) and step["ready"]:
                rows.append({"kind": "page_ready", "key": route, "route": route, "outcome": outcome, "body": {"ready": step["ready"]}})
        elif isinstance(step.get("ready"), list) and step["ready"] and not writes:
            rows.append({"kind": "page_ready", "key": route, "route": route, "outcome": outcome, "body": {"ready": step["ready"]}})
        selection = row.get("selection") or {}
        if action not in ("goto", "probe") and not writes and (step.get("selector") or step.get("selector_candidates")):
            key = json.dumps([route, action, step.get("within"), step.get("selector") or step.get("selector_candidates")], sort_keys=True)
            body = {"step": _step_view(step), "source_ref": selection.get("source_ref")}
            # Which candidate matched, from the placeholder scenario (never the resolved
            # one): what a later draft may cite as `proven` and rank first (DEC-016), on
            # this route and under this `within` only.
            matched = _matched_selector(step, selection)
            scope = proven_scope(route, step.get("within"))
            if matched is not None and outcome == "pass":
                body["matched"] = matched
                body["proven_scope"] = scope
            row_out = {"kind": "selector", "key": key, "route": route, "scope": scope, "outcome": outcome, "body": body}
            if matched is not None and outcome == "fail":
                # It failed after its selector matched (the click timed out, the fill was
                # refused): the selector found its element, so the failure is not its own.
                row_out["selector_matched"] = True
            missed = _proven_missed(step, selection, outcome)
            if missed:
                row_out["proven_missed"] = missed
            rows.append(row_out)
        if _uses_password(step) and outcome == "pass" and login is None:
            start = max((i for i in range(index + 1) if steps[i].get("action") == "goto"), default=None)
            if start is not None:
                login = {"start": start, "password_at": index, "route": _route(steps[start].get("url"), base_url)}
        if login is not None and "end" not in login and index > login["password_at"] and action in _ASSERTIONS:
            login["end"] = index
    if login is not None and "end" in login:
        sequence = steps[login["start"] : login["end"] + 1]
        passed = all(status.get(str(s.get("id")), {}).get("status") == "passed" for s in sequence)
        rows.append(
            {
                "kind": "auth.login",
                "key": login["route"],
                "route": login["route"],
                "outcome": "pass" if passed else "fail",
                "body": {"steps": [_step_view(s) for s in sequence]},
            }
        )
        # A later action followed by an assertion that the login page is back is the logout.
        for index in range(login["end"] + 1, len(steps) - 1):
            action_step, check = steps[index], steps[index + 1]
            if action_step.get("action") not in ("click", "press") or check.get("action") != "expect_url":
                continue
            expected = str(check.get("contains") or check.get("equals") or "")
            if not expected or login["route"] not in _route(expected, base_url):
                continue
            outcome_ok = all(status.get(str(s.get("id")), {}).get("status") == "passed" for s in (action_step, check))
            rows.append(
                {
                    "kind": "auth.logout",
                    "key": login["route"],
                    "route": login["route"],
                    "outcome": "pass" if outcome_ok else "fail",
                    "body": {"steps": [_step_view(action_step), _step_view(check)]},
                }
            )
            break
    return rows


def ingest(project_root: Path, report: dict, scenario: dict, base_url: str, session_id: str, *, clean_first_attempt: bool) -> dict:
    """Fold one run into the store. Returns `{added, confirmed, weakened, retired}`.

    A pass is recorded only when the run passed on its first attempt: a pass reached by a
    retry is the flaky result this package refuses to call clean. Failures always count —
    a failure against a recorded entry is information whatever the run's verdict — but an
    entry is weakened at most once per run, however many of its steps missed: DEC-016's
    "one miss demotes, two retire" counts runs, not steps.
    """
    origin = origin_of(base_url)
    summary = {"added": 0, "confirmed": 0, "weakened": 0, "retired": 0}
    if not origin:
        return summary
    rows = observations(report, scenario, base_url)
    if not rows:
        return summary
    now = now_iso()
    with _Lock(project_root):
        entries = {entry["id"]: entry for entry in load(project_root)}
        # One set for the whole run: two steps that miss the same proven selector are one
        # miss of it, not two, or a single run would retire what DEC-016 retires after two.
        weakened_here: set[str] = set()
        for row in rows:
            # A proven selector that missed demotes every entry it was proven by on the same
            # route under the same `within`, whatever the key of the step that tried it
            # (DEC-016: one miss demotes, two retire). An entry recorded before scopes were
            # stored has no `proven_scope`: it was never offered as proven, so no proven
            # miss is its.
            for missed in row.get("proven_missed") or []:
                for proven_entry in entries.values():
                    if (
                        proven_entry.get("kind") == "selector"
                        and proven_entry.get("origin") == origin
                        and proven_entry.get("matched") == missed
                        and proven_entry.get("proven_scope") == row.get("scope")
                        and not proven_entry.get("stale")
                        and proven_entry["id"] not in weakened_here
                    ):
                        weakened_here.add(proven_entry["id"])
                        proven_entry["fail_count"] = int(proven_entry.get("fail_count") or 0) + 1
                        proven_entry["consecutive_fails"] = int(proven_entry.get("consecutive_fails") or 0) + 1
                        summary["weakened"] += 1
                        if proven_entry["consecutive_fails"] >= STALE_AFTER_FAILS:
                            proven_entry["stale"] = True
                            summary["retired"] += 1
            entry_id = _entry_id(row["kind"], origin, row["key"])
            entry = entries.get(entry_id)
            if row["outcome"] == "fail":
                # Already weakened this run, above or by an earlier step. A step whose
                # selector matched before it failed is no miss of that selector (DEC-031).
                if entry is None or entry.get("stale") or entry_id in weakened_here or row.get("selector_matched"):
                    continue
                weakened_here.add(entry_id)
                entry["fail_count"] = int(entry.get("fail_count") or 0) + 1
                entry["consecutive_fails"] = int(entry.get("consecutive_fails") or 0) + 1
                summary["weakened"] += 1
                if entry["consecutive_fails"] >= STALE_AFTER_FAILS:
                    entry["stale"] = True
                    summary["retired"] += 1
                continue
            if not clean_first_attempt:
                continue
            if entry is None:
                entry = {
                    "id": entry_id,
                    "kind": row["kind"],
                    "origin": origin,
                    "route": row["route"],
                    "pass_count": 0,
                    "fail_count": 0,
                    "consecutive_fails": 0,
                    "stale": False,
                    "first_seen": now,
                }
                entries[entry_id] = entry
                summary["added"] += 1
            else:
                summary["confirmed"] += 1
            entry.update(row["body"])
            ref = row["body"].get("source_ref")
            if ref:
                path, line, anchor = _anchor(project_root, ref)
                entry.update({"file": path, "line": line, "anchor_hash": anchor})
            entry["pass_count"] = int(entry.get("pass_count") or 0) + 1
            entry["consecutive_fails"] = 0
            entry["stale"] = False
            entry["last_verified"] = now
            entry["last_session"] = session_id
        _save(project_root, list(entries.values()))
    return summary


def note_repeat_failure(project_root: Path, origin: str, record: dict, session_id: str) -> None:
    """Remember that runs against `origin` kept ending the same way.

    Written when the repeat brake reaches its limit. The brake itself lives in one
    session's state and lets go when the project changes; this entry is what outlives
    both, so the next draft — in this session or a new one — reads that the environment
    was failing, and why, before it proposes the same run again. A hint, never a gate.
    """
    from exoma.core.evidence.e2e.classify import diagnosis_for_cause

    bucket = str(record.get("bucket") or "")
    if not origin or not bucket:
        return
    diagnosis = diagnosis_for_cause(record.get("cause"), bucket, reason=record.get("reason"), repeat=True)
    now = now_iso()
    with _Lock(project_root):
        entries = {entry["id"]: entry for entry in load(project_root)}
        # One entry per distinct app failure, not one per bucket: two different failures
        # are two different things for the next draft to read. The environment buckets
        # carry no signature and keep one entry each.
        signature = str(record.get("signature") or "")
        entry_id = _entry_id(REPEAT_FAILURE, origin, f"{bucket}\0{signature}" if signature else bucket)
        entry = entries.get(entry_id) or {"id": entry_id, "kind": REPEAT_FAILURE, "origin": origin, "first_seen": now, "pass_count": 0}
        entry.update(
            {
                "bucket": bucket,
                "reason": record.get("reason"),
                "streak": int(record.get("streak") or 0),
                "source": record.get("source"),
                "fingerprint": record.get("fingerprint"),
                "signature": record.get("signature"),
                "cause": diagnosis["cause"],
                "next_step": diagnosis["next_step"],
                "fix_hint": diagnosis["fix_hint"],
                "stale": False,
                "last_seen": now,
                "last_verified": now,
                "last_session": session_id,
                "note": "Runs against this origin kept failing the same way. Act on `cause`/`fix_hint` before drafting the same flow.",
            }
        )
        entries[entry_id] = entry
        _save(project_root, list(entries.values()))


def note_failure_hint(project_root: Path, origin: str, diagnosis: dict, scenario: dict, session_id: str) -> None:
    """Remember the step a run could not use, and why, for the next draft against `origin`.

    Only reusable causes (`diagnosis["reusable"]`): a selector the draft guessed that did not
    fit, or one cited from source that the page did not render. Keyed by the step's selector
    view, so the same bad selector is one entry however often it recurs. Built from the
    placeholder scenario; the step view drops typed values. Retired by a passing run
    against the origin, like a repeat failure.
    """
    failed = diagnosis.get("failed") or {}
    step_id = failed.get("step_id")
    if not origin or not diagnosis.get("reusable") or not step_id:
        return
    step = next((s for s in (scenario or {}).get("steps") or [] if isinstance(s, dict) and str(s.get("id")) == str(step_id)), None)
    if step is None:
        return
    view = _step_view(step)
    now = now_iso()
    with _Lock(project_root):
        entries = {entry["id"]: entry for entry in load(project_root)}
        entry_id = _entry_id(FAILURE_HINT, origin, json.dumps([diagnosis["cause"], view], sort_keys=True))
        entry = entries.get(entry_id) or {
            "id": entry_id, "kind": FAILURE_HINT, "origin": origin, "first_seen": now, "pass_count": 0, "seen": 0,
        }
        entry.update(
            {
                "cause": diagnosis["cause"],
                "next_step": diagnosis["next_step"],
                "fix_hint": diagnosis["fix_hint"],
                "error_kind": failed.get("error_kind"),
                "step": view,
                "seen": int(entry.get("seen") or 0) + 1,
                "stale": False,
                "last_seen": now,
                "last_verified": now,
                "last_session": session_id,
            }
        )
        entries[entry_id] = entry
        _save(project_root, list(entries.values()))


def resolve_repeat_failures(project_root: Path, origin: str) -> int:
    """A passing run against `origin` retires its repeat-failure and failure-hint entries;
    returns how many."""
    if not origin or not _store_path(project_root).exists():
        return 0
    now = now_iso()
    resolved = 0
    with _Lock(project_root):
        entries = load(project_root)
        for entry in entries:
            if entry.get("kind") in (REPEAT_FAILURE, FAILURE_HINT) and entry.get("origin") == origin and not entry.get("stale"):
                entry.update({"stale": True, "resolved_at": now})
                resolved += 1
        if resolved:
            _save(project_root, entries)
    return resolved


def _uses_retired_selector(entry: dict) -> bool:
    """An entry recorded before `testid` was retired. Offering it would hand the draft a
    selector the validator now refuses, so it is left out rather than rewritten: whether
    the element also carries `data-e2e` is something only the source can say."""
    from exoma.core.evidence.e2e.spec import RETIRED_SELECTOR_KEYS

    found: list[bool] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            if any(key in RETIRED_SELECTOR_KEYS for key in item):
                found.append(True)
            for value in item.values():
                visit(value)
        elif isinstance(item, list):
            for value in item:
                visit(value)

    visit(entry)
    return bool(found)


def relevant(project_root: Path, base_url: str) -> list[dict]:
    """Live entries for this origin, the best proven first, bounded per kind."""
    origin = origin_of(base_url)
    live = [
        e for e in load(project_root)
        if e.get("origin") == origin and not e.get("stale") and not _uses_retired_selector(e)
    ]
    chosen: list[dict] = []
    for kind in KIND_ORDER:
        of_kind = sorted((e for e in live if e.get("kind") == kind), key=lambda e: (-int(e.get("pass_count") or 0), str(e.get("route"))))
        chosen.extend(of_kind[:PER_KIND_LIMIT])
    return [{k: v for k, v in entry.items() if k not in ("last_session",)} for entry in chosen]


def proven_selectors(project_root: Path, base_url: str) -> list[dict]:
    """Selectors a draft may cite as `proven` and rank first (DEC-016), each as
    `{selector, route, within}`: proven on that route under that `within`, nowhere else.

    The matched candidate of a live selector entry that has not missed since it last
    passed: one miss demotes it to the usual order (it stays a hint), two retire it
    (`STALE_AFTER_FAILS`), and `prune` drops an entry whose anchored source line is gone.
    An entry recorded before scopes were stored (no `proven_scope`) stays a hint, and is
    offered again once a clean run re-proves it on its route.
    The application fingerprint is deliberately not consulted: it changes on every commit,
    and would retire every proven selector with it.
    """
    from exoma.core.evidence.fact_store import current_anchor_line

    origin = origin_of(base_url)
    if not origin:
        return []
    proven: list[dict] = []
    cache: dict = {}
    for entry in load(project_root):
        matched = entry.get("matched")
        scope = entry.get("proven_scope")
        if not (
            entry.get("kind") == "selector"
            and entry.get("origin") == origin
            and not entry.get("stale")
            and not int(entry.get("consecutive_fails") or 0)
            and isinstance(matched, dict)
            and matched
            and isinstance(scope, dict)
            and isinstance(scope.get("route"), str)
            and not _uses_retired_selector(entry)
        ):
            continue
        offer = {"selector": matched, "route": scope["route"], "within": scope.get("within")}
        if offer in proven:
            continue
        # Checked here rather than left to `prune`: that one runs on `--command clean`, and
        # a selector whose anchored line is gone must not lead the very next draft.
        if entry.get("anchor_hash") and current_anchor_line(
            project_root, entry.get("file"), entry.get("line"), entry.get("anchor_hash"), cache
        ) is None:
            continue
        proven.append(offer)
    return proven


def write_sidecar(project_root: Path, session_id: str, base_url: str) -> int:
    """Write the draft's `e2e_knowledge.json`; returns how many entries it offers."""
    entries = relevant(project_root, base_url)
    runtime_dir = workflow_paths(Path(project_root), session_id)["runtime_dir"]
    runtime_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "origin": origin_of(base_url),
        "note": (
            "Hints from earlier browser runs against this origin. Re-derive every selector "
            "from the code before using it; an entry whose file:line no longer matches is stale. "
            "A selector listed under `proven` matched in a clean earlier run and has not "
            "missed since. On a step whose route (the path of the last goto) and `within` equal "
            "the listed ones, cite it with selector_provenance {\"type\": \"proven\"} and it may "
            "lead the candidate list, ahead of role and label; on any other route or `within` "
            "it is not proven."
        ),
        "entries": entries,
        "proven": proven_selectors(project_root, base_url),
    }
    atomic_write_text(
        runtime_dir / SIDECAR_NAME, json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return len(entries)


def prune(project_root: Path) -> dict:
    """Drop retired entries and entries whose anchored source line is gone."""
    from exoma.core.evidence.fact_store import current_anchor_line

    if not _store_path(project_root).exists():
        return {"kept": 0, "removed": 0}
    with _Lock(project_root):
        entries = load(project_root)
        cache: dict = {}
        kept = []
        for entry in entries:
            if entry.get("stale"):
                continue
            if entry.get("anchor_hash"):
                placed = current_anchor_line(project_root, entry.get("file"), entry.get("line"), entry.get("anchor_hash"), cache)
                if placed is None:
                    continue
                entry["line"] = placed
            kept.append(entry)
        _save(project_root, kept)
        return {"kept": len(kept), "removed": len(entries) - len(kept)}


def promotable_claims(project_root: Path, base_url: str | None = None) -> list[dict]:
    """Entries proven often enough to become Git-tracked knowledge, as /.promote claims.

    Only anchored entries qualify: a claim /.promote writes must point at code it can
    re-check, and an unanchored route is a runtime observation with nothing to verify.
    """
    from exoma.core.knowledge.verify import anchor_for

    origin = origin_of(base_url) if base_url else None
    claims: list[dict] = []
    for entry in load(project_root):
        if entry.get("stale") or int(entry.get("pass_count") or 0) < PROMOTE_AFTER_PASSES:
            continue
        if origin and entry.get("origin") != origin:
            continue
        if not entry.get("file") or not entry.get("line"):
            continue
        path, line = str(entry["file"]), int(entry["line"])
        step = entry.get("step") or {}
        claims.append(
            {
                "id": f"e2e-{entry['kind'].replace('.', '-')}-{entry['id'][:8]}",
                "statement": (
                    f"On {entry.get('route')}, the {step.get('action', 'step')} target "
                    f"{json.dumps(step.get('selector') or step.get('selector_candidates'), ensure_ascii=False)} "
                    f"matched exactly one element in {entry['pass_count']} browser runs."
                ),
                "sources": [{"type": "code", "path": path, "line": line, "anchor": anchor_for(project_root, path, line)}],
            }
        )
    return claims
