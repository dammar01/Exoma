"""Real delegated calls. Opt-in: spends provider quota."""

from tools.e2e.e2e_support import (
    FAIL,
    Path,
    Report,
    SKIP,
    _json_from,
    _run_cli,
    os,
    shutil,
    subprocess,
    tempfile,
)
from exoma.utils.osutil import find_bash  # noqa: E402 - after e2e_support puts the repo on sys.path


def delegated_checks(report: Report, session_id: str) -> None:
    """Real opencode calls. Minutes and real quota — only via --full."""
    print("\n[DELEGATED] real opencode — costs quota")
    from exoma.core.evidence.contract import REQUIRED_FIELDS

    project = Path(tempfile.mkdtemp(prefix="e2e-delegated-project-"))
    try:
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        (project / "app.py").write_text(
            "def main():\n    return 'ready'\n",
            encoding="utf-8",
        )
        code, output = _run_cli(
            "--command", "init", "--work-dir", str(project), "--pretty"
        )
        script = project / ".workflow" / "run.sh"
        if code != 0 or not script.exists():
            report.record(
                "delegated commands", SKIP, "fresh workflow runner was not generated"
            )
            return

        # bash on every OS (Git Bash on Windows), as the skills call it. No bare `bash`
        # fallback: on Windows it can be WSL's, and the run would fail for the wrong reason.
        bash = find_bash()
        if not bash:
            report.record(
                "delegated commands", FAIL, "no bash found (Git Bash on Windows): the runner cannot start"
            )
            return

        # sweep is local and returns a git-diff report, not delegated evidence.
        cases = [
            ("explore", "Sebutkan entry point utama. Maksimal 3 baris.", "evidence"),
            ("sweep", "scan git diff, identify impact", "local-report"),
        ]
        for command, task, contract in cases:
            runner = [bash, str(script)]
            result = subprocess.run(
                [*runner, command, task, session_id],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(project),
            )
            payload = _json_from(result.stdout or "")
            if payload is None:
                report.record(f"{command} returns JSON", FAIL, "no JSON in output")
                continue
            if not report.check(
                f"{command} returns ok",
                payload.get("ok") is True,
                str(payload.get("meta", {}).get("error_type", "")),
            ):
                continue
            content = (payload.get("content") or "").lower()

            if contract == "evidence":
                report.check(
                    f"{command} carries evidence markers",
                    any(m in content for m in ("[evidence]", "[digest]", "grounded:")),
                )
                for field in REQUIRED_FIELDS.get(command, ()):
                    report.check(
                        f"{command} has required field '{field}'", field in content
                    )
            else:
                report.check(
                    f"{command} reports changed files",
                    any(
                        k in content
                        for k in ("changed", "diff", "modified", "no changes")
                    ),
                    "local git report, not an evidence block",
                )
    finally:
        shutil.rmtree(project, ignore_errors=True)
