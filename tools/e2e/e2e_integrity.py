"""Workspace integrity: init, upgrade, locks, drift."""

from tools.e2e.e2e_support import (
    Path,
    REPO_ROOT,
    Report,
    SKIP,
    _json_from,
    json,
    shutil,
    subprocess,
    sys,
    tempfile,
)
from exoma.utils.osutil import find_bash  # noqa: E402 - after e2e_support puts the repo on sys.path
from exoma.core.workspace.workspace_paths import data_dir  # noqa: E402 - after e2e_support puts the repo on sys.path


def integrity_checks(report: Report) -> None:
    """Cover the data-integrity + upgrade code that "0 failures" otherwise never touches:
    fact-store provenance, config validation, and the lazy runtime-upgrade gate."""
    print("\n[INTEGRITY] fact store, config validation, lazy upgrade")

    from exoma.config.settings import COMPONENT_VERSIONS
    from exoma.core.evidence import fact_store
    from exoma.core.runtime.config_defaults import validate_config
    from exoma.core.runtime.state import ensure_workflow_workspace
    from exoma.core.runtime.upgrade import needs_upgrade, upgrade_workflow_workspace
    from exoma.core.workspace.workspace_paths import workflow_paths

    # --- claim anchors + structural output checks (pure) ---
    from exoma.core.evidence.contract import STRUCTURAL_KINDS, contract_warnings, split_claim

    bracketed = split_claim("entry point defined [app/main.py:1]")
    bare = split_claim("dispatch happens in app/main.py:48 before the lock")
    report.check(
        "split_claim extracts anchors bracketed AND bare",
        # The bare shape is the one `grounded:` asked for, and the only one never collected:
        # every claim in a live run could name its file and still report refs=[]. It went
        # unnoticed because the sim fixture bracketed both of its claims.
        bracketed["refs"] == ["app/main.py:1"]
        and bare["refs"] == ["app/main.py:48"]
        # A bracketed anchor is bookkeeping and leaves the prose; a bare one is grammar and
        # stays in it.
        and "app/main.py:1" not in bracketed["text"]
        and "app/main.py:48" in bare["text"],
        f"bracketed={bracketed}, bare={bare}",
    )

    _EV = "[EVIDENCE]\ngrounded:\n- thing defined [app/main.py:1]\nuncertainties:\n- none\n"
    truncated_kinds = {w["kind"] for w in contract_warnings("plan", _EV)}
    report.check(
        "contract_warnings flags a missing digest",
        # Observed: a plan returned ok:true with content cut mid-word and no [DIGEST] at
        # all, and meta.contract_warnings was absent — no rule read the terminal section, so
        # a fragment and a finished answer were indistinguishable to the caller.
        "digest_missing" in truncated_kinds,
        str(sorted(truncated_kinds)),
    )

    _CLOSED = _EV + "[DIGEST]\nsummary: ok.\nconfidence: high\n"
    noisy_kinds = {
        w["kind"]
        for w in contract_warnings("plan", _CLOSED + "\nWhat would you like to do next?\n")
    }
    report.check(
        "contract_warnings flags a trailing question to the user",
        "trailing_non_evidence" in noisy_kinds and "digest_missing" not in noisy_kinds,
        str(sorted(noisy_kinds)),
    )
    report.check(
        "contract_warnings stays quiet on a well-formed payload",
        contract_warnings("plan", _CLOSED) == [],
        str(contract_warnings("plan", _CLOSED)),
    )

    odd_fence_kinds = {
        w["kind"] for w in contract_warnings("plan", _CLOSED + "\n```\ndangling\n")
    }
    report.check(
        "an odd code fence warns but never caps confidence",
        # It is reported, because a reader may want to know. It is NOT structural, because
        # membership there costs a cap to `low`, and this signal is both duplicated by the
        # digest checks and wrong often enough — prose quoting a lone fence reaches it too.
        "unbalanced_code_fence" in odd_fence_kinds
        and "unbalanced_code_fence" not in STRUCTURAL_KINDS
        and not odd_fence_kinds & set(STRUCTURAL_KINDS),
        f"kinds={sorted(odd_fence_kinds)}, structural={list(STRUCTURAL_KINDS)}",
    )

    # --- config validation (pure, no workspace needed) ---
    from exoma.core.runtime.config_defaults import default_commands, default_policies

    clean = {"commands": default_commands(), "policies": default_policies()}
    report.check(
        "validate_config: clean config has no warnings",
        validate_config(clean) == [],
        str(validate_config(clean)[:2]),
    )
    dirty = {
        "commands": {"verify_mode": 123, "typo_key": True},
        "policies": {"fact_relevant_limit": "3"},
    }
    warns = validate_config(dirty)
    report.check(
        "validate_config: flags unknown key + wrong types",
        any("typo_key" in w for w in warns)
        and any("verify_mode" in w for w in warns)
        and any("fact_relevant_limit" in w for w in warns),
        f"{len(warns)} warning(s)",
    )

    project = Path(tempfile.mkdtemp(prefix="e2e-integrity-"))
    (project / ".git").mkdir()
    try:
        ensure_workflow_workspace(project, str(REPO_ROOT / "main.py"))

        # --- fact-store provenance + lock plumbing ---
        (project / "sample.py").write_text("anchor_line = 1\n", encoding="utf-8")
        content = (
            "durable_facts:\n- [config] sample defines anchor_line [sample.py:1]\n"
        )
        added = fact_store.ingest(project, content, "e2e-integrity")
        report.check(
            "fact_store: ingest promotes a durable fact", added == 1, f"added={added}"
        )
        facts_path = workflow_paths(project)["data_dir"] / "facts.jsonl"
        stored = []
        if facts_path.exists():
            stored = [
                json.loads(l)
                for l in facts_path.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
        report.check(
            "fact_store: stored fact carries provenance origin",
            bool(stored) and stored[0].get("origin") == "discovered",
            str(stored[0].get("origin") if stored else "no fact"),
        )
        # config.json holds overrides only, so this fallback is the value every project
        # without the key actually runs on; it must be the documented default, not a copy.
        from exoma.core.runtime.config_defaults import default_policies

        report.check(
            "fact_store: recurrence fallback equals the config default",
            fact_store.RECURRENCE_THRESHOLD
            == default_policies()["fact_recurrence_threshold"],
            str(fact_store.RECURRENCE_THRESHOLD),
        )
        # lock is re-entrant per call and releases cleanly
        with fact_store._FactLock(project):
            lock_held = (facts_path.with_name("facts.jsonl.lock")).exists()
        lock_freed = not (facts_path.with_name("facts.jsonl.lock")).exists()
        report.check("fact_store: lock acquires and releases", lock_held and lock_freed)

        # --- lazy upgrade gate ---
        cfg_path = workflow_paths(project)["config"]
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
        report.check(
            "config: runtime_version stamped",
            config.get("runtime", {}).get("runtime_version")
            == COMPONENT_VERSIONS["runtime"],
            str(config.get("runtime", {}).get("runtime_version")),
        )
        # init on a workspace stamped by an older build must CLOSE the drift, not merely
        # report it. Reporting was the old contract: init handed back a stale workspace and
        # said so in a return field nobody read, so every fix reached only the users who
        # remembered to run upgrade as a separate step. init now runs that upgrade itself
        # (core/runtime/state.py ensure_workflow_workspace), which is why the assertion below is the
        # inverse of the one it replaces — the config IS rewritten, on purpose.
        stale_config = json.loads(cfg_path.read_text(encoding="utf-8"))
        stale_config["version"] = "0.0.0"
        cfg_path.write_text(json.dumps(stale_config, indent=2), encoding="utf-8")
        stale_init = ensure_workflow_workspace(project, str(REPO_ROOT / "main.py"))
        after_init = json.loads(cfg_path.read_text(encoding="utf-8"))
        report.check(
            "init: closes version drift by upgrading in place",
            isinstance(stale_init.get("auto_upgrade"), dict)
            and stale_init.get("upgrade_needed") is False
            and after_init.get("version") == config.get("version"),
            f"auto_upgrade={type(stale_init.get('auto_upgrade')).__name__} "
            f"upgrade_needed={stale_init.get('upgrade_needed')} "
            f"config={after_init.get('version')}",
        )
        # `auto_upgrade` carries a STRING when the upgrade could not run (a live job, an
        # unreadable config) — init still succeeds at scaffolding and hands back the command
        # to run. Asserting the dict above without naming this would leave the skip path
        # reading like a pass to whoever changes this next.
        report.check(
            "init: a blocked auto-upgrade is reported, not swallowed",
            not isinstance(stale_init.get("auto_upgrade"), str)
            or "--command upgrade" in stale_init["auto_upgrade"],
            str(stale_init.get("auto_upgrade")),
        )
        cfg_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

        # nothing changed since init -> scripts must NOT be rewritten (lazy skip)
        r1 = upgrade_workflow_workspace(project, str(REPO_ROOT / "main.py"))
        report.check(
            "upgrade: skips script regen when runtime unchanged",
            r1["regenerated_scripts"] == [],
            f"{len(r1['regenerated_scripts'])} script(s)",
        )
        # upgrade is what people run on a workspace that has been lived in, so it must
        # rebuild the directories only init used to create. A workspace that lost reports/
        # stayed broken through every upgrade and recovered only by re-running init.
        shutil.rmtree(data_dir(project) / "reports", ignore_errors=True)
        restored = upgrade_workflow_workspace(project, str(REPO_ROOT / "main.py"))
        report.check(
            "upgrade: rebuilds scaffolding directories init used to own",
            (data_dir(project) / "reports").is_dir()
            and any("reports" in d for d in restored.get("restored_dirs", [])),
            f"restored_dirs={restored.get('restored_dirs')}",
        )
        report.check(
            "upgrade: reports nothing restored when nothing was missing",
            upgrade_workflow_workspace(project, str(REPO_ROOT / "main.py")).get(
                "restored_dirs"
            )
            == [],
            "second pass must be a no-op",
        )

        # simulate an older build -> version alone must NOT trigger a rewrite any more
        config["runtime"]["runtime_version"] = "0.0.0"
        cfg_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        # ...but it MUST register as drift. runtime_version was reported and then ignored by
        # needs_upgrade, so a runtime-only bump would ship to workspaces never told they
        # were stale — invisible today only because every component version derives from
        # TOOL_VERSION and they always move together.
        report.check(
            "needs_upgrade: a runtime-only bump counts as drift",
            needs_upgrade(project) is True,
            f"needs_upgrade={needs_upgrade(project)}",
        )
        r2 = upgrade_workflow_workspace(project, str(REPO_ROOT / "main.py"))
        report.check(
            "upgrade: a version bump alone rewrites nothing",
            r2["regenerated_scripts"] == [],
            f"{len(r2['regenerated_scripts'])} script(s)",
        )
        script = project / ".workflow" / "run.sh"
        script = script if script.exists() else None
        if script is None:
            report.record(
                "upgrade: rewrites a drifted script", SKIP, "no run script generated"
            )
        else:
            script.write_text("# drifted\n", encoding="utf-8")
            from exoma.core.runtime.scripts import script_drift

            seen = script_drift(project)
            report.check(
                "doctor: reports a drifted entry script",
                any(
                    d["script"] == script.name and d["state"] == "content_differs"
                    for d in seen
                ),
                json.dumps(seen),
            )
            r3 = upgrade_workflow_workspace(project, str(REPO_ROOT / "main.py"))
            report.check(
                "upgrade: rewrites a drifted script",
                len(r3["regenerated_scripts"]) > 0,
                f"{len(r3['regenerated_scripts'])} script(s)",
            )
            report.check(
                "doctor: drift clears once upgrade has rewritten the script",
                script_drift(project) == [],
            )
            # A run.ps1 an earlier release generated is unmaintained by every generator
            # now, so it must be reported and then removed — not quietly kept.
            other = script.with_suffix(".ps1")
            other.write_text("# retired PowerShell runner\n", encoding="utf-8")
            report.check(
                "doctor: flags a retired PowerShell entry script",
                any(
                    d["script"] == other.name and d["state"] == "retired_leftover"
                    for d in script_drift(project)
                ),
            )
            upgrade_workflow_workspace(project, str(REPO_ROOT / "main.py"))
            report.check(
                "upgrade: removes the retired PowerShell entry script",
                not other.exists(),
            )

            # The entry script is the only sanctioned way in, and local commands take no
            # task: an empty `--prompt` reaching argparse as a bare flag once killed every
            # local command at the door. Exercised through the script, under the bash the
            # skills use (Git Bash on Windows); calling main.py directly would never catch it.
            # No bare `bash` fallback: on Windows it can be WSL's, and the check would fail
            # for the wrong reason. A machine without bash is named as one.
            bash = find_bash()
            report.check("entry script: a bash is found to run it", bash is not None)
            runner = [bash, str(script)] if bash else []
            for command in ("doctor", "inspect") if runner else ():
                res = subprocess.run(
                    [*runner, command],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(project),
                )
                payload = _json_from(res.stdout or "")
                # What is under test is that the command STARTS: argparse dying on a bare
                # `--prompt` exits 2 and prints no JSON at all. The verdict itself is not:
                # `doctor` exits 1 whenever the workspace is NOT_READY, and a CI runner
                # with no provider CLI installed is exactly that — an environment fact,
                # not a defect in the generated script. Asserting rc==0 here made the
                # check pass only on a developer machine that happened to have the
                # provider installed.
                report.check(
                    f"entry script: `{command}` runs with no task argument",
                    payload is not None and res.returncode in (0, 1),
                    f"rc={res.returncode} "
                    f"status={((payload or {}).get('meta') or {}).get('status')} "
                    f"{(res.stderr or '')[:160]}",
                )

        # --- fan-out capability probe ---
        from exoma.core.evidence.contract import reported_no_spawn_tool
        from exoma.core.runtime.state import fanout_capability, set_fanout_capability

        report.check(
            "fan-out: detects opencode 'no spawn tool' self-report",
            reported_no_spawn_tool("subagents: none (no spawn tool; tools: read, grep)")
            and not reported_no_spawn_tool("subagents: c1, c2"),
        )
        report.check(
            "fan-out: capability unprobed is None", fanout_capability(project) is None
        )
        set_fanout_capability(project, False)
        report.check(
            "fan-out: capability persists OFF once learned",
            fanout_capability(project) is False,
        )

        # A dispatch that forgot its [cN] tags stays unconfirmed — but it must not read as
        # "no fan-out was attempted", which is the one thing that did not happen.
        from exoma.core.evidence.contract import FANOUT_MISMATCH, detect_subagent_usage

        untagged = detect_subagent_usage(
            "subagents: c0, c1 (dispatched wf-slice)\n- Router routes by command [x.py:1]"
        )
        report.check(
            "fan-out: an untagged dispatch keeps its declared clusters visible",
            untagged["mode"] == FANOUT_MISMATCH
            and untagged["used"] is False
            and untagged["fanout_clusters"] == []
            and untagged["declared_clusters"] == ["c0", "c1"],
            json.dumps(untagged),
        )
        corroborated = detect_subagent_usage(
            "subagents: c0, c1\n[c0] Router routes by command [x.py:1]"
        )
        report.check(
            "fan-out: declaration plus [cN] tags still counts as a real fan-out",
            corroborated["used"] is True
            and corroborated["fanout_clusters"] == ["c0", "c1"],
        )
    finally:
        shutil.rmtree(project, ignore_errors=True)

    # --- active fan-out contract and compact prompt ---
    from exoma.config.roles import ROLE_EXPLORATION
    from exoma.core.prompt.prompt_builder import build_prompt

    explore_prompt = build_prompt(
        role=ROLE_EXPLORATION,
        task="find the router",
        session_id="e2e",
        command="explore",
        project_root=str(REPO_ROOT),
        runtime_dir=str(data_dir(REPO_ROOT) / "sessions" / "e2e" / "runtime"),
        has_leads=True,
        subagent_fanout=True,
    )
    verify_prompt = build_prompt(
        role="verification",
        task="verify the change",
        session_id="e2e",
        command="verify",
        project_root=str(REPO_ROOT),
    )
    from exoma.config.providers import bundle_for

    agents_contract = (
        REPO_ROOT
        / "src"
        / "exoma"
        / "_bundle"
        / "config"
        / "opencode"
        / bundle_for("opencode")["instructions"][0]
    ).read_text(encoding="utf-8")
    report.check(
        "verify prompt keeps the compact AGENTS.md contract",
        "introduced/regression + in_scope" not in verify_prompt
        and "AGENTS.md" in verify_prompt,
    )
    report.check(
        "evidence prompt points to sidecars and the canonical fan-out contract",
        "AGENTS.md" in explore_prompt
        and "leads.json" in explore_prompt
        and "FAN-OUT call" in explore_prompt
        and "wf-slice" in agents_contract,
    )
    report.check(
        "OUTPUT_FORMAT skeleton remains enforceable",
        "[EVIDENCE]" in explore_prompt
        and "[DIGEST]" in explore_prompt
        and "[VERIFICATION]" in verify_prompt,
    )
    report.check(
        "verify prompt stays under the Windows 8191-char argv cap",
        len(verify_prompt) < 8191,
        f"{len(verify_prompt)} chars",
    )
    report.check(
        "fan-out: the shipped roster is allowlisted in permission.task",
        (
            lambda policy: policy.get("*") == "deny"
            and {
                path.stem
                for path in (
                    REPO_ROOT / "src" / "exoma" / "_bundle" / "config" / "opencode" / "agents"
                ).glob("*.md")
            }
            <= {name for name, action in policy.items() if action == "allow"}
        )(
            json.loads(
                (
                    REPO_ROOT / "src" / "exoma" / "_bundle" / "config" / "opencode" / "opencode.template.json"
                ).read_text(encoding="utf-8")
            )["agent"]["plan"]["permission"].get("task", {})
        ),
    )

    # Shipped docs quote a version back at the user; the code has exactly one.
    stamped = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "maintain" / "stamp_version.py"), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
    )
    report.check(
        "docs carry the same version the code reports",
        stamped.returncode == 0,
        # The report prints through the console codepage; stamp_version's output carries
        # an em dash, so keep the detail ASCII rather than crashing on cp1252.
        (stamped.stdout or "")
        .encode("ascii", "ignore")
        .decode()
        .strip()
        .replace("\n", " ")[:200],
    )

    # --- stdout slimming: drop heavy diagnostic meta on success only ---
    from exoma.cli import _slim_result

    success = {
        "ok": True,
        "content": "evidence",
        "digest": {"summary": "x"},
        "meta": {
            "args": ["opencode", "run", "huge prompt"],
            "stderr": "kilobytes of opencode logs",
            "bootstrap": {"args": ["..."]},
            "policy": {"verify_mode": "delegated"},
            "session_reset": False,
            "job_id": "job_1",
        },
    }
    slim = _slim_result(success)
    report.check(
        "slim: success drops heavy meta (args/stderr/bootstrap)",
        all(k not in slim["meta"] for k in ("args", "stderr", "bootstrap")),
    )
    report.check(
        "slim: success keeps content, digest, and actionable meta (policy/session_reset)",
        slim["content"] == "evidence"
        and "digest" in slim
        and slim["meta"]["policy"]["verify_mode"] == "delegated"
        and slim["meta"]["job_id"] == "job_1",
    )
    secret = "sk-" + "A" * 36
    failure = {
        "ok": False,
        "content": f"boom {secret}",
        "meta": {"stderr": f"trace {secret}", "args": ["opencode", secret]},
    }
    slim_failure = _slim_result(failure)
    report.check(
        "slim: failure keeps sanitized diagnostics without raw argv",
        secret not in json.dumps(slim_failure)
        and "args" not in slim_failure.get("meta", {})
        and slim_failure.get("meta", {}).get("argv_count") == 2
        and "stderr" in slim_failure.get("meta", {}),
    )

    # --- content slimming (opt-in) ---
    # The result carries the full evidence text AND evidence_ref.artifact_path, which points
    # at the same bytes on disk. Delegation exists to keep raw code out of main_agent's
    # window; shipping both puts it back. Opt-in, because a consumer reading `content`
    # without checking `content_mode` would silently see a preview.
    ARTIFACT = r"C:\logs\run_1\output.raw.md"
    long_evidence = "[EVIDENCE]\n" + ("grounded claim padding. " * 200)
    with_ref = {
        "ok": True,
        "content": long_evidence,
        "digest": {"summary": "s"},
        "evidence_ref": {"artifact_path": ARTIFACT, "anchors": 2, "reused": False},
        "meta": {"job_id": "job_2"},
    }
    default_slim = _slim_result(with_ref)
    report.check(
        "slim: content stays full unless asked",
        default_slim["content"] == long_evidence
        and "content_mode" not in default_slim["meta"],
        f"chars={len(default_slim['content'])}",
    )

    ref_only = _slim_result(with_ref, slim_content=True)
    report.check(
        "slim: ref_only swaps content for a pointer to the artifact",
        len(ref_only["content"]) < len(long_evidence)
        and ARTIFACT in ref_only["content"]
        and ref_only["meta"]["content_mode"] == "ref_only"
        and ref_only["meta"]["content_full_chars"] == len(long_evidence)
        # The digest is the whole point of slimming — it must survive intact, and so must
        # the pointer the reader needs to recover what was withheld.
        and ref_only["digest"] == {"summary": "s"}
        and ref_only["evidence_ref"]["artifact_path"] == ARTIFACT,
        f"chars={len(ref_only['content'])} of {len(long_evidence)}",
    )

    no_ref = {key: value for key, value in with_ref.items() if key != "evidence_ref"}
    report.check(
        "slim: ref_only refuses when there is no artifact to point at",
        # Without a readable copy on disk, dropping the text destroys the only one there is.
        _slim_result(no_ref, slim_content=True)["content"] == long_evidence,
        "content preserved",
    )

    short = {**with_ref, "content": "[EVIDENCE]\nshort."}
    report.check(
        "slim: ref_only leaves short content alone",
        # Below the threshold there is nothing to save and everything to lose.
        _slim_result(short, slim_content=True)["content"] == "[EVIDENCE]\nshort.",
        "below threshold",
    )
