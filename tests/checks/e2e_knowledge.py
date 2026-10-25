"""Browser knowledge: what a run proved is kept, reused by the next draft, and aged out.

The store sits at the fact store's level — written automatically, anchored, never in Git —
so the checks mirror that one: only proof is recorded, credentials never, a failure
weakens and two in a row retire, a moved anchor survives and a vanished one does not, and
only well-proven anchored entries are offered to /.promote.
"""

from __future__ import annotations
from exoma.core.workspace.workspace_paths import data_dir

import json
import shutil
import tempfile
from pathlib import Path

from exoma.core.evidence.e2e import knowledge
from exoma.core.evidence.e2e.classify import ORIGIN_APP, ORIGIN_UNKNOWN, build_report, classify_step, diagnose
from tests.checks.support import assert_true

BASE = "http://app.test"

_SCENARIO = {
    "version": 1,
    "claims": [{"id": "session", "severity": "blocking"}],
    "steps": [
        {"id": "open-login", "action": "goto", "url": "/login", "ready": [{"visible": {"role": "button", "name": "Masuk"}}]},
        {"id": "user", "action": "fill", "selector": {"label": "Email"}, "value": "${E2E_USER}"},
        {"id": "pass", "action": "fill", "selector": {"label": "Password"}, "value": "${E2E_PASS}"},
        {"id": "note", "action": "fill", "selector": {"label": "Note"}, "value": "typed test data"},
        {"id": "submit", "action": "click", "selector": {"role": "button", "name": "Masuk"},
         "selector_provenance": {"type": "source", "ref": "src/Login.vue:3"}},
        {"id": "at-dashboard", "action": "expect_url", "contains": "/dashboard", "claim_id": "session"},
        {"id": "logout", "action": "click", "selector": {"role": "button", "name": "Keluar"}},
        {"id": "back-at-login", "action": "expect_url", "contains": "/login", "claim_id": "session"},
    ],
}


def _report(statuses: dict[str, str] | None = None) -> dict:
    events = []
    for index, step in enumerate(_SCENARIO["steps"], start=1):
        status = (statuses or {}).get(step["id"], "passed")
        event = {"type": "progress", "step": index, "step_id": step["id"], "action": step["action"],
                 "status": status, "claim_id": step.get("claim_id")}
        if step["id"] == "submit":
            # A selector_missing failure counted its one candidate and matched nothing.
            event["selection"] = (
                {"candidate": None, "match_counts": [0], "fallback_used": False}
                if status == "failed"
                else {"candidate": 0, "match_counts": [1], "fallback_used": False,
                      "source_ref": "src/Login.vue:3", "selector_keys": ["name", "role"]}
            )
        if status == "failed":
            event["error"] = {"kind": "selector_missing"}
            event["selector_provenance"] = "source"
            event["page_stable"] = True
        events.append(event)
    events.append({"type": "result", "status": "finished"})
    return build_report(events, _SCENARIO)


def _project() -> Path:
    root = Path(tempfile.mkdtemp(prefix="aw-e2e-knowledge-"))
    (root / ".workflow").mkdir()
    (root / "src").mkdir()
    (root / "src" / "Login.vue").write_text("<template>\n<form>\n<button>Masuk</button>\n</form>\n</template>\n", encoding="utf-8")
    return root


def _by_kind(root: Path) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for entry in knowledge.load(root):
        grouped.setdefault(entry["kind"], []).append(entry)
    return grouped


def _check_a_run_records_what_it_proved(root: Path) -> None:
    summary = knowledge.ingest(root, _report(), _SCENARIO, BASE, "sid-1", clean_first_attempt=True)
    kinds = _by_kind(root)
    assert_true(summary["added"] > 0 and not summary["weakened"], f"a passing run adds entries: {summary}")
    login = kinds.get("auth.login") or []
    assert_true(
        len(login) == 1 and login[0]["route"] == "/login"
        and [s["action"] for s in login[0]["steps"]] == ["goto", "fill", "fill", "fill", "click", "expect_url"],
        f"the login flow is the goto through the first assertion after the password: {login}",
    )
    assert_true(
        (kinds.get("auth.logout") or [{}])[0].get("steps", [{}])[0].get("selector") == {"role": "button", "name": "Keluar"},
        f"the step followed by a return to the login page is the logout: {kinds.get('auth.logout')}",
    )
    assert_true({"navigation", "page_ready", "selector"} <= set(kinds), f"routes, readiness and selectors are kept: {sorted(kinds)}")
    stored = json.dumps(knowledge.load(root))
    assert_true("${E2E_PASS}" in stored and "typed test data" not in stored,
                "placeholders are kept; a literal typed value is not application knowledge and is dropped")
    # A row cut in the middle of a multi-byte character is skipped, never fatal to the store.
    store = knowledge._store_path(root)
    before = len(knowledge.load(root))
    with open(store, "ab") as fh:
        fh.write('{"id": "torn", "name": "Simpan é'.encode("utf-8")[:-1])
    assert_true(len(knowledge.load(root)) == before, "a torn multi-byte row is skipped, the rest of the store stays readable")
    with open(store, "ab") as fh:
        fh.write(b"\n")
    submit = next(e for e in kinds["selector"] if e.get("step", {}).get("selector") == {"role": "button", "name": "Masuk"})
    assert_true(submit.get("file") == "src/Login.vue" and submit.get("line") == 3 and submit.get("anchor_hash"),
                f"a selector the codebase named is anchored to its line: {submit}")
    assert_true(knowledge.origin_of(BASE) == "http://app.test" and all(e["origin"] == "http://app.test" for e in knowledge.load(root)),
                "entries belong to the application origin")


def _check_retry_passes_are_not_proof(root: Path) -> None:
    other = _project()
    try:
        summary = knowledge.ingest(other, _report(), _SCENARIO, BASE, "sid-1", clean_first_attempt=False)
        assert_true(summary["added"] == 0 and knowledge.load(other) == [], f"a pass reached by a retry records nothing: {summary}")
    finally:
        shutil.rmtree(other, ignore_errors=True)


def _check_failures_retire_and_prune(root: Path) -> None:
    for session in ("sid-2", "sid-3"):
        knowledge.ingest(root, _report({"submit": "failed"}), _SCENARIO, BASE, session, clean_first_attempt=True)
    submit = [e for e in knowledge.load(root) if e["kind"] == "selector" and e.get("file") == "src/Login.vue"]
    assert_true(submit and submit[0]["stale"] and submit[0]["consecutive_fails"] == 2,
                f"two failures in a row retire the entry: {submit}")
    offered = knowledge.relevant(root, BASE)
    assert_true(not any(e.get("file") == "src/Login.vue" and e["kind"] == "selector" for e in offered),
                "a retired entry is never offered to a draft")
    assert_true(all(e["origin"] == "http://app.test" for e in offered) and knowledge.relevant(root, "http://other.test") == [],
                "and a draft for another origin is offered nothing from this one")
    before = len(knowledge.load(root))
    result = knowledge.prune(root)
    assert_true(result["removed"] >= 1 and len(knowledge.load(root)) == before - result["removed"], f"prune drops retired entries: {result}")


def _check_anchor_moves_and_vanishes(root: Path) -> None:
    other = _project()
    try:
        knowledge.ingest(other, _report(), _SCENARIO, BASE, "sid-1", clean_first_attempt=True)
        source = other / "src" / "Login.vue"
        source.write_text("<!-- moved -->\n" + source.read_text(encoding="utf-8"), encoding="utf-8")
        knowledge.prune(other)
        moved = [e for e in knowledge.load(other) if e.get("file") == "src/Login.vue"]
        assert_true(moved and moved[0]["line"] == 4, f"a line that only moved keeps its entry at the new line: {moved}")
        source.write_text(source.read_text(encoding="utf-8").replace("<button>Masuk</button>", "<button>Login</button>"), encoding="utf-8")
        knowledge.prune(other)
        assert_true(not [e for e in knowledge.load(other) if e.get("file") == "src/Login.vue"],
                    "a line whose content is gone takes its entry with it")
    finally:
        shutil.rmtree(other, ignore_errors=True)


def _check_promotable_after_repeated_proof() -> None:
    other = _project()
    try:
        for n in range(knowledge.PROMOTE_AFTER_PASSES):
            assert_true(knowledge.promotable_claims(other, BASE) == [] or n > 0, "nothing is promotable before it is proven")
            knowledge.ingest(other, _report(), _SCENARIO, BASE, f"sid-{n}", clean_first_attempt=True)
        claims = knowledge.promotable_claims(other, BASE)
        assert_true(
            len(claims) == 1 and claims[0]["sources"][0]["path"] == "src/Login.vue" and claims[0]["sources"][0]["anchor"],
            f"only an anchored entry proven {knowledge.PROMOTE_AFTER_PASSES} times becomes a /.promote claim: {claims}",
        )
    finally:
        shutil.rmtree(other, ignore_errors=True)


def _check_the_next_draft_is_offered_it() -> None:
    from tests.checks.e2e_routing import _adapter, _flow, _workspace

    root = _workspace("e2e-knowledge-flow-")
    try:
        adapter = _adapter()
        draft, result = _flow(root, adapter, "pass", {"E2E_USER": "user@example.test"})
        first_prompt = next(c["prompt"] for c in adapter.calls if c["command"] == "e2e_spec")
        assert_true(
            "e2e_knowledge.json" not in first_prompt and (draft["meta"]["e2e"].get("knowledge") or {}).get("offered") == 0,
            "an empty store offers nothing, and the prompt does not name a sidecar with nothing in it",
        )
        ingested = result["meta"]["e2e"].get("knowledge") or {}
        assert_true(ingested.get("added", 0) > 0, f"the run fed the store: {ingested}")

        adapter = _adapter()
        second, _ = _flow(root, adapter, "pass", {"E2E_USER": "user@example.test"})
        prompt = next(c["prompt"] for c in adapter.calls if c["command"] == "e2e_spec")
        offered = (second["meta"]["e2e"].get("knowledge") or {}).get("offered")
        sidecar = data_dir(root) / "sessions" / "e2e-session" / "runtime" / knowledge.SIDECAR_NAME
        assert_true(offered and "e2e_knowledge.json" in prompt and sidecar.exists(),
                    f"the next draft is pointed at what the run proved: offered={offered}")
        assert_true("user@example.test" not in sidecar.read_text(encoding="utf-8"),
                    "the sidecar holds placeholders, never a resolved credential")
    finally:
        shutil.rmtree(root, ignore_errors=True)


_SAVE_ROLE = {"role": "button", "name": "Simpan"}
_SAVE_E2E = {"e2e": "save"}


def _proven(root: Path) -> list[dict]:
    """The selectors offered as proven, wherever they hold."""
    return [item["selector"] for item in knowledge.proven_selectors(root, BASE)]


def _save_scenario(candidates: list[dict]) -> dict:
    return {
        "version": 1,
        "claims": [{"id": "saved", "severity": "blocking", "source_refs": ["src/Login.vue:3"]}],
        "steps": [
            {"id": "open", "action": "goto", "url": "/form"},
            {"id": "save", "action": "click", "selector_candidates": candidates},
            {"id": "done", "action": "expect_title", "contains": "Form", "claim_id": "saved"},
        ],
    }


def _save_report(scenario: dict, winner: int | None, *, status: str = "passed", source_ref: str | None = None,
                 error_kind: str = "action_timeout", resolved: bool = True) -> dict:
    """`winner=None`: every candidate was counted and none matched. `resolved=False`: the
    step failed before its selector was tried (not ready, an exception), so no selection."""
    events = []
    for index, step in enumerate(scenario["steps"], start=1):
        event = {"type": "progress", "step": index, "step_id": step["id"], "action": step["action"],
                 "status": "passed", "claim_id": step.get("claim_id")}
        if step["id"] == "save":
            event["status"] = status
            if not resolved:
                pass
            elif winner is None:
                event["selection"] = {"candidate": None, "match_counts": [0] * len(step["selector_candidates"]), "fallback_used": False}
            else:
                event["selection"] = {"candidate": winner, "match_counts": [0] * winner + [1], "fallback_used": winner > 0}
            if source_ref and "selection" in event:
                event["selection"]["source_ref"] = source_ref
            if status == "failed":
                event["error"] = {"kind": error_kind}
                event["page_stable"] = True
        events.append(event)
    events.append({"type": "result", "status": "finished"})
    return build_report(events, scenario)


def _check_proven_selectors_rank_first_and_demote() -> None:
    """DEC-016: the candidate that matched is recorded, may lead the next draft as `proven`,
    and drops back to the usual order after one miss and retires after two."""
    from exoma.core.evidence.e2e.spec import validate_scenario

    root = _project()
    try:
        first = _save_scenario([
            {"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}},
            {"selector": _SAVE_E2E, "selector_provenance": {"type": "heuristic"}},
        ])
        knowledge.ingest(root, _save_report(first, winner=1), first, BASE, "sid-1", clean_first_attempt=True)
        assert_true(
            knowledge.proven_selectors(root, BASE) == [{"selector": _SAVE_E2E, "route": "/form", "within": None}],
            "the candidate that matched is what is proven, not the list, on the route it matched on: "
            f"{knowledge.proven_selectors(root, BASE)}",
        )
        knowledge.write_sidecar(root, "sid-2", BASE)
        sidecar = json.loads(
            (data_dir(root) / "sessions" / "sid-2" / "runtime" / knowledge.SIDECAR_NAME).read_text(encoding="utf-8")
        )
        assert_true(
            sidecar.get("proven") == [{"selector": _SAVE_E2E, "route": "/form", "within": None}],
            f"the next draft is told what it may cite as proven, and where: {sidecar}",
        )

        leading = _save_scenario([
            {"selector": _SAVE_E2E, "selector_provenance": {"type": "proven"}},
            {"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}},
        ])
        policy = {"base_url": BASE, "proven_selectors": knowledge.proven_selectors(root, BASE)}
        assert_true(not validate_scenario(leading, policy), f"a proven selector may lead: {validate_scenario(leading, policy)}")
        (root / "src" / "Form.vue").write_text(
            '<template>\n<button data-e2e="save">Simpan</button>\n</template>\n', encoding="utf-8"
        )
        plain = {"base_url": BASE, "project_root": str(root)}
        for provenance, policy_for, may_lead in (
            ({"type": "source", "ref": "src/Form.vue:2"}, plain, True),
            # A citation that is well formed but points at nothing is still a guess.
            ({"type": "source", "ref": "src/Form.vue:1"}, plain, False),
            ({"type": "source", "ref": "src/Missing.vue:2"}, plain, False),
            ({"type": "source", "ref": "src/Form.vue"}, plain, False),
            ({"type": "source", "ref": "../outside.vue:2"}, plain, False),
            # A line number past int()'s digit limit reads as not cited instead of raising.
            ({"type": "source", "ref": "src/Form.vue:" + "9" * 5000}, plain, False),
            ({"type": "heuristic"}, plain, False),
            # No project to check the citation against: it may not lead.
            ({"type": "source", "ref": "src/Form.vue:2"}, {"base_url": BASE}, False),
        ):
            source_first = _save_scenario([
                {"selector": _SAVE_E2E, "selector_provenance": provenance},
                {"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}},
            ])
            errors = validate_scenario(source_first, policy_for)
            assert_true(
                (not errors) == may_lead,
                f"an e2e with provenance {provenance} {'may' if may_lead else 'may not'} lead role: {errors}",
            )
        # The citation is read like the tagging pass reads a line: the exact attribute value,
        # in an opening tag, in a template file, outside a comment.
        from exoma.core.evidence.e2e.spec import _cites_e2e

        (root / "src" / "Cases.vue").write_text(
            '<div data-e2e="save-delete">save</div>\n'
            '<!-- <button data-e2e="save"> -->\n'
            "<p>data-e2e save</p>\n"
            '<y :data-e2e="save">\n'
            "<x data-e2e=save />\n",
            encoding="utf-8",
        )
        (root / "src" / "builder.py").write_text('html = "<b data-e2e=\\"save\\">"\n', encoding="utf-8")
        for ref, cited in (
            ("src/Cases.vue:1", False),  # another value that starts the same
            ("src/Cases.vue:2", False),  # commented out
            ("src/Cases.vue:3", False),  # prose, not an attribute
            ("src/Cases.vue:4", False),  # a binding, not a literal
            ("src/Cases.vue:5", True),   # unquoted literal
            ("src/builder.py:1", False),  # not a template
            ("src/Cases\x00.vue:1", False),  # malformed: refused, not raised
        ):
            assert_true(
                _cites_e2e(str(root), ref, "save") is cited,
                f"`{ref}` {'carries' if cited else 'does not carry'} data-e2e=\"save\"",
            )
        role_first = _save_scenario([
            {"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}},
            {"selector": _SAVE_E2E, "selector_provenance": {"type": "heuristic"}},
        ])
        assert_true(not validate_scenario(role_first, plain), "an old role-first draft stays valid")
        unbacked = validate_scenario(leading, {"base_url": BASE, "proven_selectors": []})
        assert_true(
            any("proven" in error for error in unbacked),
            f"`proven` the store does not back is refused, so a draft cannot promote its own guess: {unbacked}",
        )

        # The proven selector misses once (the role fallback wins): demoted, still a hint.
        knowledge.ingest(root, _save_report(leading, winner=1), leading, BASE, "sid-2", clean_first_attempt=True)
        after_miss = _proven(root)
        assert_true(
            _SAVE_E2E not in after_miss and _SAVE_ROLE in after_miss,
            "one miss demotes the proven selector; the fallback that matched in that clean run "
            f"is proven in its place: {after_miss}",
        )
        demoted = [e for e in knowledge.load(root) if e.get("matched") == _SAVE_E2E]
        assert_true(
            demoted and all(not e.get("stale") for e in demoted) and demoted[0]["consecutive_fails"] == 1,
            f"one miss weakens the entry once, it does not retire it: {demoted}",
        )
        # A step that fails AFTER its proven selector matched (the click timed out) is not a
        # miss of that selector.
        before = [e["consecutive_fails"] for e in knowledge.load(root) if e.get("matched") == _SAVE_E2E]
        knowledge.ingest(root, _save_report(leading, winner=0, status="failed"), leading, BASE, "sid-2b", clean_first_attempt=True)
        after = [e["consecutive_fails"] for e in knowledge.load(root) if e.get("matched") == _SAVE_E2E]
        assert_true(before == after, f"a failure after the proven selector matched does not demote it: {before} -> {after}")
        # A step that fails BEFORE its selector was tried (the page never became ready, the
        # step raised) carries no selection: the proven selector was not tried, so not missed.
        for kind in ("not_ready", "action_failed"):
            knowledge.ingest(root, _save_report(leading, winner=None, status="failed", error_kind=kind, resolved=False),
                             leading, BASE, f"sid-2c-{kind}", clean_first_attempt=True)
            after = [e["consecutive_fails"] for e in knowledge.load(root) if e.get("matched") == _SAVE_E2E]
            assert_true(before == after, f"a {kind} step that never tried its selectors does not demote it: {before} -> {after}")
        # A second miss — every candidate counted, none matched — retires it.
        knowledge.ingest(root, _save_report(leading, winner=None, status="failed", error_kind="selector_missing"),
                         leading, BASE, "sid-3", clean_first_attempt=True)
        retired = [e for e in knowledge.load(root) if e.get("matched") == _SAVE_E2E]
        assert_true(retired and all(e.get("stale") for e in retired), f"two misses retire it: {retired}")

        # Anchored proof whose source line is gone is not offered, even before `clean` runs.
        anchored = _save_scenario([
            {"selector": {"e2e": "save-anchored"}, "selector_provenance": {"type": "source", "ref": "src/Form.vue:2"}},
        ])
        knowledge.ingest(root, _save_report(anchored, winner=0, source_ref="src/Form.vue:2"), anchored, BASE, "sid-4", clean_first_attempt=True)
        assert_true({"e2e": "save-anchored"} in _proven(root), "an anchored proof is offered while its line exists")
        (root / "src" / "Form.vue").write_text("<template>\n</template>\n", encoding="utf-8")
        assert_true(
            {"e2e": "save-anchored"} not in _proven(root),
            "a proof whose anchored line is gone is not offered as proven",
        )

        # `matched` comes from the placeholder scenario: a placeholder stays a placeholder.
        placeholder = _save_scenario([
            {"selector": {"label": "${E2E_FIELD_LABEL}"}, "selector_provenance": {"type": "heuristic"}},
        ])
        knowledge.ingest(root, _save_report(placeholder, winner=0), placeholder, BASE, "sid-5", clean_first_attempt=True)
        assert_true(
            {"label": "${E2E_FIELD_LABEL}"} in _proven(root),
            "the proven selector is stored in placeholder form, never a resolved value",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _two_saves(candidates: list[dict], *, url: str = "/form", within: dict | None = None) -> dict:
    """Two steps on one page that drive the same element with the same candidate list."""
    step = {"action": "click", "selector_candidates": candidates, **({"within": within} if within else {})}
    return {
        "version": 1,
        "claims": [{"id": "saved", "severity": "blocking", "source_refs": ["src/Login.vue:3"]}],
        "steps": [
            {"id": "open", "action": "goto", "url": url},
            {"id": "save", **step},
            {"id": "save-again", **step},
            {"id": "done", "action": "expect_title", "contains": "Form", "claim_id": "saved"},
        ],
    }


def _steps_report(scenario: dict, selections: dict[str, tuple[str, dict | None]]) -> dict:
    """`selections`: step id -> (status, selection). Every other step passed."""
    events = []
    for index, step in enumerate(scenario["steps"], start=1):
        status, selection = selections.get(step["id"], ("passed", None))
        event = {"type": "progress", "step": index, "step_id": step["id"], "action": step["action"],
                 "status": status, "claim_id": step.get("claim_id")}
        if selection is not None:
            event["selection"] = selection
        if status == "failed":
            event["error"] = {"kind": "selector_missing"}
            event["page_stable"] = True
        events.append(event)
    events.append({"type": "result", "status": "finished"})
    return build_report(events, scenario)


_FALLBACK_WON = {"candidate": 1, "match_counts": [0, 1], "fallback_used": True}
_NONE_MATCHED = {"candidate": None, "match_counts": [0, 0], "fallback_used": False}
_FIRST_WON = {"candidate": 0, "match_counts": [1], "fallback_used": False}


def _check_one_run_weakens_an_entry_once() -> None:
    """DEC-016 counts runs: two steps of one run that miss the same proven selector demote
    it once; it takes a second run to retire it."""
    root = _project()
    try:
        first = _save_scenario([
            {"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}},
            {"selector": _SAVE_E2E, "selector_provenance": {"type": "heuristic"}},
        ])
        knowledge.ingest(root, _save_report(first, winner=1), first, BASE, "sid-1", clean_first_attempt=True)
        twice = _two_saves([
            {"selector": _SAVE_E2E, "selector_provenance": {"type": "proven"}},
            {"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}},
        ])
        # Both steps counted every candidate and matched none: a failing run, so the generic
        # path also sees both rows of one key.
        summary = knowledge.ingest(root, _steps_report(twice, {"save": ("failed", _NONE_MATCHED), "save-again": ("failed", _NONE_MATCHED)}),
                                   twice, BASE, "sid-2", clean_first_attempt=False)
        proven_entry = [e for e in knowledge.load(root) if e.get("matched") == _SAVE_E2E]
        assert_true(
            proven_entry and proven_entry[0]["consecutive_fails"] == 1 and not proven_entry[0].get("stale"),
            f"two misses in one run weaken the entry once and do not retire it: {proven_entry} {summary}",
        )
        assert_true(summary["retired"] == 0, f"nothing is retired by one run: {summary}")
        knowledge.ingest(root, _steps_report(twice, {"save": ("failed", _NONE_MATCHED)}), twice, BASE, "sid-3", clean_first_attempt=False)
        proven_entry = [e for e in knowledge.load(root) if e.get("matched") == _SAVE_E2E]
        assert_true(proven_entry and proven_entry[0].get("stale"), f"a miss in a second run retires it: {proven_entry}")

        # The generic path is counted per run too: one key that fails twice in one run.
        other = _project()
        try:
            plain = _two_saves([{"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}}])
            knowledge.ingest(other, _steps_report(plain, {"save": ("passed", _FIRST_WON), "save-again": ("passed", _FIRST_WON)}),
                             plain, BASE, "sid-1", clean_first_attempt=True)
            missed = {"candidate": None, "match_counts": [0], "fallback_used": False}
            knowledge.ingest(other, _steps_report(plain, {"save": ("failed", missed), "save-again": ("failed", missed)}),
                             plain, BASE, "sid-2", clean_first_attempt=False)
            entry = [e for e in knowledge.load(other) if e.get("kind") == "selector"]
            assert_true(
                len(entry) == 1 and entry[0]["consecutive_fails"] == 1 and not entry[0].get("stale"),
                f"a step key that fails twice in one run is weakened once: {entry}",
            )
        finally:
            shutil.rmtree(other, ignore_errors=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_a_failure_after_the_match_is_not_a_miss() -> None:
    """DEC-031: a step whose selector matched and then failed (the click timed out) did not
    miss, so its own entry — same key, same candidate list — is not weakened."""
    root = _project()
    try:
        first = _save_scenario([
            {"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}},
            {"selector": _SAVE_E2E, "selector_provenance": {"type": "heuristic"}},
        ])
        knowledge.ingest(root, _save_report(first, winner=1), first, BASE, "sid-1", clean_first_attempt=True)
        for n in range(knowledge.STALE_AFTER_FAILS):
            knowledge.ingest(root, _save_report(first, winner=1, status="failed"), first, BASE, f"sid-f{n}", clean_first_attempt=False)
        entry = [e for e in knowledge.load(root) if e.get("matched") == _SAVE_E2E]
        assert_true(
            entry and entry[0]["consecutive_fails"] == 0 and not entry[0].get("stale") and _SAVE_E2E in _proven(root),
            f"failures after the selector matched leave it proven: {entry}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_dynamic_routes_share_a_scope() -> None:
    """DEC-040: identifier segments are one route. A selector proven on `/users/123/edit`
    is proven on `/users/456/edit`, and no row is keyed by the id (a navigation row keeps
    the step's own URL in its body, the address a next draft can open); a page with other
    route words (`/users/123/view`, `/users`) is still another route."""
    from exoma.core.evidence.e2e.spec import validate_scenario

    for url, route in (
        ("/users/123/edit", "/users/:id/edit"),
        (BASE + "/orders/550e8400-e29b-41d4-a716-446655440000?tab=2", "/orders/:id"),
        ("/settings/profile", "/settings/profile"),
        ("/posts/my-first-article", "/posts/my-first-article"),
        # A numeric route word is folded like an id: `/reports/2024` and `/reports/2025`
        # are one route. Deliberate: the scope reuses the redaction sanitizer, which cannot
        # tell a year from a record id, and pages one template renders share their selectors.
        ("/reports/2024", "/reports/:id"),
        ("/reports/2025", "/reports/:id"),
        ("/", "/"),
    ):
        got = knowledge.route_of(url, BASE)
        assert_true(got == route, f"route_of({url!r}) is {route!r}: {got!r}")

    root = _project()
    try:
        heuristic = [{"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}}]
        first = _two_saves(heuristic, url="/users/123/edit")
        knowledge.ingest(root, _steps_report(first, {"save": ("passed", _FIRST_WON), "save-again": ("passed", _FIRST_WON)}),
                         first, BASE, "sid-1", clean_first_attempt=True)
        assert_true(
            {"selector": _SAVE_ROLE, "route": "/users/:id/edit", "within": None} in knowledge.proven_selectors(root, BASE),
            f"proven on the route with its id as :id: {knowledge.proven_selectors(root, BASE)}",
        )
        routes = {e.get("route") for e in knowledge.load(root)}
        assert_true(routes == {"/users/:id/edit"}, f"every row is keyed by the route, never the id: {routes}")
        policy = {"base_url": BASE, "proven_selectors": knowledge.proven_selectors(root, BASE)}
        proven = [{"selector": _SAVE_ROLE, "selector_provenance": {"type": "proven"}}]
        other_id = _two_saves(proven, url="/users/456/edit")
        assert_true(not validate_scenario(other_id, policy),
                    f"another id is the same page: {validate_scenario(other_id, policy)}")
        for url in ("/users/123/view", "/users", "/users/123/edit/extra"):
            errors = validate_scenario(_two_saves(proven, url=url), policy)
            assert_true(any("proven" in e and "route" in e for e in errors), f"{url} is another route: {errors}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_proven_is_scoped_by_route_and_within() -> None:
    """A selector proven on one route under one `within` is proven there only: another route
    or container neither may cite it as proven nor demotes it. A store row written before
    scopes were recorded stays readable and is a hint, not proven."""
    from exoma.core.evidence.e2e.spec import validate_scenario

    root = _project()
    try:
        first = _save_scenario([
            {"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}},
            {"selector": _SAVE_E2E, "selector_provenance": {"type": "heuristic"}},
        ])
        knowledge.ingest(root, _save_report(first, winner=1), first, BASE, "sid-1", clean_first_attempt=True)
        policy = {"base_url": BASE, "proven_selectors": knowledge.proven_selectors(root, BASE)}
        leads = [
            {"selector": _SAVE_E2E, "selector_provenance": {"type": "proven"}},
            {"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}},
        ]
        here = _save_scenario(leads)
        assert_true(not validate_scenario(here, policy), f"proven on its own route and scope: {validate_scenario(here, policy)}")
        for label, scenario in (
            ("another route", _two_saves(leads, url="/settings")),
            ("an absolute URL to another route", _two_saves(leads, url=BASE + "/settings?tab=1")),
            ("another within", _two_saves(leads, within={"css": "#dialog"})),
        ):
            errors = validate_scenario(scenario, policy)
            assert_true(any("proven" in e and "route" in e for e in errors), f"{label}: `proven` is refused: {errors}")
        # The same route reached by an absolute URL with a query is the same route.
        same = _two_saves(leads, url=BASE + "/form?step=2")
        assert_true(not validate_scenario(same, policy), f"the route is the path, whatever the query: {validate_scenario(same, policy)}")

        # A proven miss on another route or under another within does not touch this entry.
        elsewhere = _two_saves(leads, url="/settings")
        scoped = _two_saves(leads, within={"css": "#dialog"})
        for scenario in (elsewhere, scoped):
            knowledge.ingest(root, _steps_report(scenario, {"save": ("failed", _NONE_MATCHED)}), scenario, BASE, "sid-x", clean_first_attempt=False)
        entry = [e for e in knowledge.load(root) if e.get("matched") == _SAVE_E2E]
        assert_true(entry and entry[0]["consecutive_fails"] == 0 and _SAVE_E2E in _proven(root),
                    f"a miss elsewhere does not demote the selector proven here: {entry}")
        # A miss on its own route and scope does.
        knowledge.ingest(root, _steps_report(here, {"save": ("failed", _NONE_MATCHED)}), here, BASE, "sid-y", clean_first_attempt=False)
        entry = [e for e in knowledge.load(root) if e.get("matched") == _SAVE_E2E]
        assert_true(entry and entry[0]["consecutive_fails"] == 1, f"a miss on its own route demotes it: {entry}")

        # Proven under a within, offered with it.
        inside = _two_saves([{"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}}], within={"css": "#dialog"})
        knowledge.ingest(root, _steps_report(inside, {"save": ("passed", _FIRST_WON), "save-again": ("passed", _FIRST_WON)}),
                         inside, BASE, "sid-z", clean_first_attempt=True)
        assert_true(
            {"selector": _SAVE_ROLE, "route": "/form", "within": {"css": "#dialog"}} in knowledge.proven_selectors(root, BASE),
            f"a selector proven under a within is offered with it: {knowledge.proven_selectors(root, BASE)}",
        )

        # An entry from before scopes were recorded: readable, a hint, never offered as proven.
        store = knowledge._store_path(root)
        old = {"id": "old0000000000000", "kind": "selector", "origin": BASE, "route": "/old", "pass_count": 4,
               "fail_count": 0, "consecutive_fails": 0, "stale": False, "first_seen": "2026-09-01T00:00:00Z",
               "last_verified": "2026-09-01T00:00:00Z", "matched": {"e2e": "legacy"},
               "step": {"action": "click", "selector": {"e2e": "legacy"}}}
        with open(store, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(old) + "\n")
        assert_true(any(e["id"] == old["id"] for e in knowledge.relevant(root, BASE)), "an old row is still offered as a hint")
        assert_true({"e2e": "legacy"} not in _proven(root), "but not as proven until a clean run re-proves it on its route")
        legacy = _two_saves([{"selector": {"e2e": "legacy"}, "selector_provenance": {"type": "heuristic"}}], url="/old")
        knowledge.ingest(root, _steps_report(legacy, {"save": ("failed", {"candidate": None, "match_counts": [0], "fallback_used": False})}),
                         legacy, BASE, "sid-old", clean_first_attempt=False)
        knowledge.ingest(root, _steps_report(legacy, {"save": ("passed", _FIRST_WON), "save-again": ("passed", _FIRST_WON)}),
                         legacy, BASE, "sid-new", clean_first_attempt=True)
        assert_true(
            {"selector": {"e2e": "legacy"}, "route": "/old", "within": None} in knowledge.proven_selectors(root, BASE),
            f"re-proven on its route, it is offered again: {knowledge.proven_selectors(root, BASE)}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_a_failed_run_proves_nothing() -> None:
    """The runner records passes only from a run that passed on its first attempt: a run
    that failed (here an application failure, never retried) adds nothing."""
    from tests.checks.e2e_routing import _adapter, _flow, _workspace

    root = _workspace("e2e-knowledge-failed-")
    try:
        _, result = _flow(root, _adapter(), "app_fail", {"E2E_USER": "user@example.test"})
        meta = result["meta"]["e2e"]
        assert_true(meta.get("browser_verdict") == "fail" and not meta.get("attempts"), f"fixture: one failed attempt: {meta.get('browser_verdict')}")
        ingested = meta.get("knowledge") or {}
        assert_true(
            ingested.get("added") == 0 and not any(int(e.get("pass_count") or 0) for e in knowledge.load(root)),
            f"the passing steps of a failed run are not proof: {ingested} {knowledge.load(root)}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_a_missing_proven_selector_is_grounded() -> None:
    """A `proven` selector that is gone is classed like a source-grounded one: a passed run
    matched it on this origin, so its absence from a settled page is the application's."""
    missing = {"action": "click", "step_id": "save", "error": {"kind": "selector_missing"}, "selector_provenance": "proven"}
    assert_true(classify_step({**missing, "page_stable": True}) == ORIGIN_APP, "a proven selector gone from a settled page is the app's")
    assert_true(classify_step({**missing, "page_stable": False}) == ORIGIN_UNKNOWN, "on an unsettled page it is unknown, as for source")
    scenario = _save_scenario([{"selector": _SAVE_E2E, "selector_provenance": {"type": "proven"}}])
    events = [
        {"type": "progress", "step": 1, "step_id": "open", "action": "goto", "status": "passed"},
        {"type": "progress", "step": 2, "step_id": "save", "action": "click", "status": "failed",
         "selector_provenance": "proven", "page_stable": True, "error": {"kind": "selector_missing"},
         "selection": {"candidate": None, "match_counts": [0], "fallback_used": False}},
        {"type": "result", "status": "finished"},
    ]
    cause = diagnose(build_report(events, scenario)).get("cause")
    assert_true(cause == "selector_not_rendered", f"diagnosed like a grounded miss, not a scenario selector: {cause}")


def _check_side_effect_none_is_not_a_write() -> None:
    """DEC-044: `side_effect: "none"` is the spec's word for no write. Read as truthy it
    made every step of a draft that spells it out a write, and no selector was recorded."""
    candidates = [{"selector": _SAVE_ROLE, "selector_provenance": {"type": "heuristic"}}]
    for effect, recorded in ((None, True), ("none", True), ("creates_test_data", False)):
        scenario = _two_saves(candidates)
        if effect is not None:
            for step in scenario["steps"]:
                step["side_effect"] = effect
        report = _steps_report(scenario, {"save": ("passed", _FIRST_WON), "save-again": ("passed", _FIRST_WON)})
        kinds = [row["kind"] for row in knowledge.observations(report, scenario, BASE)]
        assert_true(
            ("selector" in kinds) is recorded,
            f"side_effect={effect!r}: a passed click {'is' if recorded else 'is not'} a selector observation; got {kinds}",
        )


def _test_e2e_knowledge() -> None:
    _check_side_effect_none_is_not_a_write()
    _check_proven_selectors_rank_first_and_demote()
    _check_one_run_weakens_an_entry_once()
    _check_a_failure_after_the_match_is_not_a_miss()
    _check_proven_is_scoped_by_route_and_within()
    _check_dynamic_routes_share_a_scope()
    _check_a_failed_run_proves_nothing()
    _check_a_missing_proven_selector_is_grounded()
    root = _project()
    try:
        _check_a_run_records_what_it_proved(root)
        _check_retry_passes_are_not_proof(root)
        _check_failures_retire_and_prune(root)
        _check_anchor_moves_and_vanishes(root)
        _check_promotable_after_repeated_proof()
        _check_the_next_draft_is_offered_it()
    finally:
        shutil.rmtree(root, ignore_errors=True)
