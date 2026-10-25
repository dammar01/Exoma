"""Fast local checks: prompts, contracts, result slimming."""

from tools.e2e.e2e_support import (
    Path,
    REPO_ROOT,
    Report,
    SKIP,
    _json_from,
    _run_cli,
    json,
    shutil,
    subprocess,
    tempfile,
)


def local_checks(report: Report) -> None:
    print("\n[LOCAL] no opencode, no quota")

    project = Path(tempfile.mkdtemp(prefix="e2e-project-"))
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    try:
        code, out = _run_cli(
            "--command", "init", "--work-dir", str(project), "--pretty"
        )
        payload = _json_from(out) or {}
        report.check("init returns ok", payload.get("ok") is True, f"rc={code}")
        report.check("init creates .workflow", (project / ".workflow").is_dir())
        report.check(
            "init generates run scripts",
            any((project / ".workflow").glob("run.*")),
        )
        runner = next((project / ".workflow").glob("run.*"), None)
        runner_text = runner.read_text(encoding="utf-8-sig") if runner else ""
        report.check(
            "runner keeps sweep on the local command path",
            "verify','sweep" not in runner_text and "verify sweep " not in runner_text,
        )
        config_path = project / ".workflow" / "config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            from exoma.config.settings import TOOL_VERSION

            report.check(
                "config carries current version",
                config.get("version") == TOOL_VERSION,
                f"{config.get('version')} vs {TOOL_VERSION}",
            )
        else:
            report.record("config carries current version", SKIP, "config.json absent")

        code, out = _run_cli(
            "--command", "doctor", "--work-dir", str(project), "--pretty"
        )
        payload = _json_from(out) or {}
        # A well-formed run emits one of the three real statuses. Which one depends on the
        # install/bundle state of the HOME this runs against (release-integrity can make it
        # NOT_READY when the installed bundle drifts from the manifest), so we assert doctor
        # produces a VALID status, not a specifically green one.
        report.check(
            "doctor reports a status",
            payload.get("meta", {}).get("status")
            in {"READY", "NEEDS_UPGRADE", "NOT_READY"},
            payload.get("meta", {}).get("status", "?"),
        )

        code, out = _run_cli(
            "--command", "inspect", "--work-dir", str(project), "--pretty"
        )
        report.check("inspect returns ok", (_json_from(out) or {}).get("ok") is True)

        code, out = _run_cli(
            "--command",
            "sweep",
            "--session",
            "e2e-local",
            "--work-dir",
            str(project),
            "--pretty",
        )
        sweep = _json_from(out) or {}
        report.check(
            "sweep runs locally and writes a report",
            code == 0
            and sweep.get("ok") is True
            and bool(sweep.get("meta", {}).get("report")),
            f"rc={code}",
        )

        code, out = _run_cli(
            "--command", "upgrade", "--work-dir", str(project), "--pretty"
        )
        report.check(
            "upgrade is exposed by the CLI",
            code == 0 and (_json_from(out) or {}).get("ok") is True,
            f"rc={code}",
        )

        code, out = _run_cli(
            "--command", "clean", "--work-dir", str(project), "--pretty"
        )
        report.check("clean returns ok", (_json_from(out) or {}).get("ok") is True)
    finally:
        shutil.rmtree(project, ignore_errors=True)

    # The shipped example is what a fresh clone actually installs.
    from exoma.config.settings import load_provider_config
    from exoma.core.prompt.router import Router

    example = REPO_ROOT / "src" / "exoma" / "config" / "second_agent.example.json"
    if example.exists():
        routes = Router(load_provider_config(example))
        models = [routes.route(c)["model"] for c in ("explore", "plan", "analyze")]
        report.check(
            "shipped example has no placeholder models",
            all(m is None or "provider_id" not in str(m) for m in models),
            str(models),
        )
    else:
        report.record(
            "shipped example has no placeholder models", SKIP, "example missing"
        )

    from exoma.config.settings import COMPONENT_VERSIONS, TOOL_VERSION
    from exoma.core.workspace.workspace_paths import CONFIG_VERSION

    manifest = json.loads(
        (REPO_ROOT / "src" / "exoma" / "_bundle" / "manifest.json").read_text(encoding="utf-8")
    )
    report.check(
        "release versions are synchronized",
        # Compared against TOOL_VERSION, not a literal: a hardcoded number turns every
        # release bump into a test edit, and the check is about the four staying in sync.
        TOOL_VERSION == CONFIG_VERSION == manifest.get("version")
        and set(COMPONENT_VERSIONS.values()) == {TOOL_VERSION}
        and set((manifest.get("versions") or {}).values()) == {TOOL_VERSION},
        f"tool={TOOL_VERSION}, config={CONFIG_VERSION}, manifest={manifest.get('version')}",
    )
