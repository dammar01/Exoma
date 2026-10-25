"""Promoted knowledge: determinism, the freshness ladder, reconciliation, and the gates.

The checks that "0 failures elsewhere" would never reach. Every one of them is a
behaviour promote depends on being exactly right, and most are silent when wrong: a
serialiser that drifts produces noisy diffs nobody attributes to the tool, a ladder that
is too eager retires knowledge that is still true, and a gate that does not fire puts a
secret in someone's commit.
"""

from tools.e2e.e2e_support import (
    Path,
    Report,
    _json_from,
    _run_cli,
    json,
    tempfile,
)


def _doc(anchor_hash: str, claim_id: str = "c1", statement: str = "f returns 1") -> dict:
    return {
        "schema_version": 1,
        "id": "auth",
        "title": "Authentication",
        "summary": "how requests authenticate",
        "production": {"ref": "main", "verified_commit": "abcdef1234567"},
        "anchors": {"paths": ["a.py"]},
        "claims": [
            {
                "id": claim_id,
                "type": "behavior",
                "statement": statement,
                "sources": [
                    {
                        "type": "code",
                        "path": "a.py",
                        "lines": {"start": 2, "end": 2},
                        "anchor_hash": anchor_hash,
                    }
                ],
            }
        ],
        "applicability": {"excluded_branches": []},
    }


def promote_checks(report: Report) -> None:
    """Cover core/knowledge/: schema, deterministic store, freshness ladder, reconciler."""
    print("\n[PROMOTE] knowledge schema, determinism, freshness, reconciliation")

    from exoma.core.knowledge import reconcile, retrieve, schema, store, verify
    from exoma.core.runtime.config_defaults import knowledge_relevant_limit, production_branch

    root = Path(tempfile.mkdtemp(prefix="e2e-promote-"))
    (root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    anchor = verify.anchor_for(root, "a.py", 2)
    report.check("anchor_for hashes the cited line", bool(anchor), f"anchor={anchor}")

    doc = _doc(anchor)

    # --- schema ---
    report.check("valid document passes validation", schema.validate(doc) == [], "no errors")

    sourceless = json.loads(json.dumps(doc))
    sourceless["claims"][0]["sources"] = []
    report.check(
        "claim without a source is rejected",
        any("sources" in e for e in schema.validate(sourceless)),
        "an unsourced claim is what promotion exists to prevent",
    )

    duplicated = json.loads(json.dumps(doc))
    duplicated["claims"].append(json.loads(json.dumps(doc["claims"][0])))
    report.check(
        "duplicate claim ids are rejected",
        any("duplicate claim id" in e for e in schema.validate(duplicated)),
        "in-place revision needs one claim per id",
    )

    bad_slug = json.loads(json.dumps(doc))
    bad_slug["id"] = "../escape"
    report.check(
        "document id must be a slug (blocks path traversal)",
        any("slug" in e for e in schema.validate(bad_slug)),
        "doc_path builds a filename from this id",
    )

    ghost_exclusion = json.loads(json.dumps(doc))
    ghost_exclusion["applicability"]["excluded_branches"] = [
        {"pattern": "feature/.*", "reason": "reworking auth", "affected_claims": ["nope"]}
    ]
    report.check(
        "exclusion naming an unknown claim is rejected",
        any("no claim" in e for e in schema.validate(ghost_exclusion)),
        "an exclusion that protects nothing reads as though it does",
    )

    # --- determinism ---
    shuffled = json.loads(json.dumps(doc))
    shuffled["claims"][0]["sources"].append(
        {"type": "user", "text": "product decided this", "evidence_ids": ["b", "a"]}
    )
    reordered = json.loads(json.dumps(shuffled))
    reordered["claims"][0]["sources"].reverse()
    # The user source is now first. Re-order ITS evidence_ids too, so the two documents
    # differ only in ordering — the one thing canonicalisation is supposed to erase.
    reordered["claims"][0]["sources"][0]["evidence_ids"] = ["a", "b"]
    report.check(
        "serialisation is independent of input order",
        store.serialize(shuffled) == store.serialize(reordered),
        "a Git-tracked artifact cannot reorder itself between runs",
    )
    report.check(
        "serialised document ends with exactly one newline",
        store.serialize(doc).endswith("}\n") and not store.serialize(doc).endswith("\n\n"),
    )

    # --- write path ---
    first = store.write(root, doc)
    report.check("first promotion writes the document", first.get("ok") and first["written"], first.get("path", ""))
    second = store.write(root, doc)
    report.check(
        "re-promoting unchanged knowledge writes nothing",
        second.get("ok") and not second["written"],
        "zero-byte diff is the whole point of canonicalisation",
    )
    report.check(
        "the schema file lands beside the documents",
        (store.knowledge_dir(root) / store.SCHEMA_FILENAME).exists(),
    )

    leaky = json.loads(json.dumps(doc))
    leaky["claims"][0]["statement"] = "the service authenticates with sk-abcdefghijklmnopqrst"
    refused = store.write(root, leaky)
    report.check(
        "credential-shaped statement is refused, not redacted",
        not refused.get("ok") and refused.get("error_type") == "secret_shaped_content",
        str(refused.get("errors", ""))[:90],
    )

    invalid = json.loads(json.dumps(doc))
    invalid["claims"][0]["type"] = "lesson"
    rejected = store.write(root, invalid)
    report.check(
        "unknown claim type never reaches disk",
        not rejected.get("ok") and rejected.get("error_type") == "invalid_knowledge",
    )

    # --- freshness ladder ---
    verdicts = verify.verify_doc(root, doc)
    report.check(
        "intact anchor verifies fresh",
        [c["status"] for c in verdicts["claims"]] == [verify.FRESH_ANCHOR],
        f"stale_count={verdicts['stale_count']}",
    )

    (root / "a.py").write_text("# inserted above\ndef f():\n    return 1\n", encoding="utf-8")
    moved = verify.verify_doc(root, doc)
    relocated = moved["claims"][0]["sources"][0]
    report.check(
        "a line pushed down by an insert is a relocation, not staleness",
        moved["claims"][0]["status"] == verify.FRESH_ANCHOR and relocated.get("line") == 3,
        f"line 2 -> {relocated.get('line')}",
    )

    (root / "a.py").write_text("def f():\n    return 99\n", encoding="utf-8")
    edited = verify.verify_doc(root, doc)
    report.check(
        "an edited cited line needs verification",
        edited["claims"][0]["status"] == verify.NEEDS_VERIFICATION,
    )

    (root / "a.py").unlink()
    gone = verify.verify_doc(root, doc)
    report.check(
        "a deleted source file is reported missing, not fresh",
        gone["claims"][0]["status"] == verify.SOURCE_MISSING,
    )

    (root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    user_only = json.loads(json.dumps(doc))
    user_only["claims"][0]["sources"] = [{"type": "user", "text": "decided in review"}]
    report.check(
        "a user-sourced claim is not invalidated by code it never cited",
        verify.verify_doc(root, user_only)["claims"][0]["status"] == verify.FRESH_COMMIT,
        "intent the code cannot prove must not be judged by the code",
    )

    # --- reconciliation ---
    fresh = verify.verify_doc(root, doc)["claims"]
    same = reconcile.reconcile(doc["claims"], doc["claims"], fresh)
    report.check("identical claim reconciles UNCHANGED", same["counts"].get(reconcile.UNCHANGED) == 1)

    rewrapped = json.loads(json.dumps(doc["claims"]))
    rewrapped[0]["statement"] = "  f   returns\n1  "
    report.check(
        "re-wrapped whitespace is not a change",
        reconcile.reconcile(doc["claims"], rewrapped, fresh)["counts"].get(reconcile.UNCHANGED) == 1,
    )

    revised = json.loads(json.dumps(doc["claims"]))
    revised[0]["statement"] = "f returns 99"
    report.check(
        "a fresh, differently-worded claim is an UPDATE",
        reconcile.reconcile(doc["claims"], revised, fresh)["counts"].get(reconcile.UPDATE) == 1,
    )

    retyped = json.loads(json.dumps(doc["claims"]))
    retyped[0]["type"] = "invariant"
    report.check(
        "a claim that changed kind is a CONFLICT, not a revision",
        reconcile.reconcile(doc["claims"], retyped, fresh)["counts"].get(reconcile.CONFLICT) == 1,
    )

    intent = json.loads(json.dumps(doc["claims"]))
    intent[0]["sources"] = [{"type": "user", "text": "caching is off by design"}]
    code_only = json.loads(json.dumps(doc["claims"]))
    code_only[0]["statement"] = "responses are cached for 5 minutes"
    report.check(
        "code contradicting stated intent goes to the user",
        reconcile.reconcile(intent, code_only, fresh)["counts"].get(reconcile.CONFLICT) == 1,
        "neither side is obviously right, so neither wins automatically",
    )

    stale_verdicts = [{"id": "c1", "status": verify.NEEDS_VERIFICATION}]
    unverified = reconcile.reconcile(doc["claims"], revised, stale_verdicts)
    report.check(
        "an unverified candidate never overwrites a verified claim",
        unverified["counts"].get(reconcile.UNVERIFIED) == 1,
        "refusing to write is the recoverable direction",
    )

    kept = reconcile.apply_reconciliation(doc["claims"], revised, {"c1": reconcile.UNVERIFIED})
    report.check(
        "applying UNVERIFIED keeps the existing claim text",
        kept[0]["statement"] == "f returns 1",
    )
    applied = reconcile.apply_reconciliation(doc["claims"], revised, {"c1": reconcile.UPDATE})
    report.check("applying UPDATE takes the candidate text", applied[0]["statement"] == "f returns 99")

    silent = reconcile.reconcile(doc["claims"], [], fresh)
    report.check(
        "a claim nobody re-derived is reported, not deleted",
        silent["untouched_existing"] == ["c1"],
        "silence in one run is not evidence a claim stopped being true",
    )

    # --- policy readers ---
    (root / ".workflow").mkdir(parents=True, exist_ok=True)
    (root / ".workflow" / "config.json").write_text(
        json.dumps({"policies": {"knowledge_relevant_limit": True, "production_branch": 5}}),
        encoding="utf-8",
    )
    report.check(
        "a bool where an int belongs falls back (bool subclasses int)",
        knowledge_relevant_limit(root) == 3,
        "isinstance(True, int) is True, so the guard has to exclude bool by hand",
    )
    report.check(
        "a non-string production_branch falls back", production_branch(root) == "main"
    )

    # --- applicability ---
    empty_anchor_doc = json.loads(json.dumps(doc))
    empty_anchor_doc["anchors"]["paths"] = []
    verdict = retrieve.applicability(root, doc, branch="main", production_ref="main")
    report.check(
        "on the production branch everything applies",
        verdict["status"] == retrieve.APPLICABLE,
    )
    excluded_doc = json.loads(json.dumps(doc))
    excluded_doc["applicability"]["excluded_branches"] = [
        {"pattern": "feature/auth", "reason": "reworking auth"}
    ]
    report.check(
        "an explicit branch exclusion wins over the diff",
        retrieve.applicability(
            root, excluded_doc, branch="feature/auth", production_ref="main"
        )["status"]
        == retrieve.EXCLUDED,
    )
    report.check(
        "exclusion patterns are anchored, not substring matches",
        not store.matches_branch("feature/auth", "feature/authorization-rework"),
    )
    report.check(
        "an unresolvable diff fails closed",
        retrieve.applicability(
            root, doc, branch="some-branch", production_ref="main"
        )["status"]
        == retrieve.NEEDS_BRANCH_VERIFICATION,
        "no git repo here, so the comparison cannot be made",
    )

    # --- doctor readiness ---
    from exoma.core.audit.diagnostics import run_doctor

    checks = run_doctor(root, "does-not-exist", "e2e-promote")["meta"].get("checks", {})
    if not checks:
        # run_doctor puts its checks in the report file, not the returned meta, on some
        # paths. Read them back from the written report rather than skipping the checks.
        from exoma.core.workspace.workspace_paths import read_json_file, workflow_paths

        report_path = workflow_paths(root, "e2e-promote")["doctor_report"]
        checks = read_json_file(report_path).get("checks", {}) if report_path.exists() else {}

    report.check(
        "doctor reports the knowledge document count",
        checks.get("knowledge_documents", {}).get("count") == 1,
        f"count={checks.get('knowledge_documents', {}).get('count')}",
    )
    report.check(
        "doctor reports whether the current branch may promote",
        checks.get("promote_branch", {}).get("production_branch") == "main",
        str(checks.get("promote_branch")),
    )

    (store.knowledge_dir(root) / "broken.json").write_text("{ not json", encoding="utf-8")
    broken_checks = run_doctor(root, "does-not-exist", "e2e-promote")["meta"]
    report.check(
        "doctor surfaces a malformed knowledge document as an issue",
        any("knowledge documents" in i for i in broken_checks.get("issues", [])),
        "; ".join(broken_checks.get("issues", []))[:100],
    )

    # --- the CLI's refusals ---
    # Everything above calls core/knowledge/ directly, which is why every gate could pass
    # here while every refusal crashed in practice: make_error raises on an error_type
    # nobody registered, so a refusal the library returned cleanly left the CLI as a
    # traceback. These go through main.py so a JSON envelope is what actually gets checked.
    cli_root = Path(tempfile.mkdtemp(prefix="e2e-promote-cli-"))

    def _cli_refusal(command: str, prompt: str | None) -> dict:
        args = ["--command", command, "--work-dir", str(cli_root)]
        if prompt is not None:
            args += ["--prompt", prompt]
        code, output = _run_cli(*args, cwd=cli_root)
        return {"code": code, "output": output, "payload": _json_from(output)}

    missing_arg = _cli_refusal("promote-verify", None)
    report.check(
        "a promote stage with no document path refuses as JSON, not a traceback",
        (missing_arg["payload"] or {}).get("meta", {}).get("error_type") == "promote_input_missing",
        f"rc={missing_arg['code']} {missing_arg['output'].strip()[-120:]}",
    )

    unreadable = _cli_refusal("promote-verify", str(cli_root / "does-not-exist.json"))
    report.check(
        "an unreadable candidate refuses as JSON, not a traceback",
        (unreadable["payload"] or {}).get("meta", {}).get("error_type")
        == "promote_input_unreadable",
        f"rc={unreadable['code']} {unreadable['output'].strip()[-120:]}",
    )

    off_branch_doc = cli_root / "candidate.json"
    off_branch_doc.write_text(json.dumps(_doc("0" * 16)), encoding="utf-8")
    off_branch = _cli_refusal("promote-write", str(off_branch_doc))
    off_branch_type = (off_branch["payload"] or {}).get("meta", {}).get("error_type")
    report.check(
        "promote-write off the production branch refuses as JSON, not a traceback",
        off_branch_type in {"promote_not_on_branch", "promote_not_production_branch"},
        f"rc={off_branch['code']} error_type={off_branch_type}",
    )
    report.check(
        "the branch gate wrote nothing on its way out",
        not (cli_root / "docs" / "project-knowledge").exists(),
        "a refusal that still writes is worse than one that crashes",
    )

    # Every error_type the promote path can name has to be one make_error will accept.
    # Registering them one at a time is how the last six went missing; assert the set.
    from exoma.core.evidence.contract import ERROR_TYPES

    promote_types = {
        "promote_input_missing",
        "promote_input_unreadable",
        "promote_existing_unreadable",
        "promote_not_on_branch",
        "promote_not_production_branch",
        "promote_write_rejected",
        "invalid_knowledge",
        "secret_shaped_content",
        "ignored_source",
        "ignored_destination",
    }
    report.check(
        "every promote error_type is registered with the output contract",
        promote_types <= ERROR_TYPES,
        f"unregistered={sorted(promote_types - ERROR_TYPES)}",
    )
