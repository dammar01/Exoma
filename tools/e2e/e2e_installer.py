"""Installer behaviour against a throwaway HOME."""

from config.providers import bundle_for, provider_home
from tools.e2e.e2e_support import (
    Path,
    REPO_ROOT,
    Report,
    SKIP,
    json,
    os,
    shutil,
    subprocess,
    sys,
    tempfile,
)


def _seed_project(project: Path, provider: str) -> None:
    """Give `project` its own second_agent.json from the shipped example before init.

    init never overwrites an existing one, so this pins the provider the checks expect
    instead of whatever this machine's config/second_agent.seed.json selected.
    """
    config = json.loads(
        (REPO_ROOT / "config" / "second_agent.example.json").read_text(encoding="utf-8")
    )
    config["provider"] = provider
    (project / ".workflow").mkdir(parents=True, exist_ok=True)
    (project / ".workflow" / "second_agent.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )


def installer_checks(report: Report) -> None:
    """Install into a throwaway HOME. Installing onto the machine that produced dist/
    only ever proves idempotency — it never exercises the create path a new user hits.
    """
    print("\n[INSTALLER] against a throwaway HOME")

    if not (REPO_ROOT / "dist" / "manifest.json").exists():
        report.record("installer dry run", SKIP, "dist/ not extracted")
        return

    fake_home = Path(tempfile.mkdtemp(prefix="e2e-home-"))
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUTF8": "1",
        "USERPROFILE": str(fake_home),
        "HOME": str(fake_home),
        "AGENT_HOME": str(fake_home),
    }
    try:
        dry = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py")],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check("dry run exits clean", dry.returncode == 0, f"rc={dry.returncode}")
        report.check("dry run writes nothing", not (fake_home / ".claude").exists())
        report.check("dry run says so", "DRY RUN" in dry.stdout)

        # Hooks run through bash on every OS, so with no bash (no Git Bash on Windows)
        # --apply refuses before its first write. PATH is narrowed to python's own dir;
        # where bash or git sits there too, no bash-less PATH can be built.
        bare = str(Path(sys.executable).parent)
        if shutil.which("bash", path=bare) or shutil.which("git", path=bare):
            report.record("apply without bash refuses", SKIP, "bash or git sits beside python")
        else:
            no_bash_home = Path(tempfile.mkdtemp(prefix="e2e-nobash-"))
            try:
                refused = subprocess.run(
                    [sys.executable, str(REPO_ROOT / "install.py"), "--apply"],
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env={
                        **env,
                        "PATH": bare,
                        "USERPROFILE": str(no_bash_home),
                        "HOME": str(no_bash_home),
                        "AGENT_HOME": str(no_bash_home),
                    },
                    cwd=str(no_bash_home),
                )
                report.check(
                    "apply without bash refuses and writes nothing",
                    refused.returncode == 6 and not (no_bash_home / ".claude").exists(),
                    f"rc={refused.returncode}",
                )
            finally:
                shutil.rmtree(no_bash_home, ignore_errors=True)

        applied = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--apply"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check(
            "apply exits clean", applied.returncode == 0, f"rc={applied.returncode}"
        )

        claude_md = fake_home / ".claude" / "CLAUDE.md"
        report.check("CLAUDE.md installed", claude_md.exists())
        skills = list((fake_home / ".claude" / "skills").glob("*.md"))
        report.check("skills installed", len(skills) >= 10, f"{len(skills)} file(s)")
        # Every skill needs the wrapper that makes `/.name` typeable, so a count on its own
        # is not enough: /.promote shipped with a skill body and no wrapper, and a loose
        # `>= 10` on skills alone had nothing to say about it. Compare the two sets.
        wrappers = {
            p.stem.lstrip(".")
            for p in (fake_home / ".claude" / "commands").glob(".*.md")
        }
        missing_wrappers = sorted({p.stem for p in skills} - wrappers)
        report.check(
            "every installed skill has a command wrapper",
            not missing_wrappers,
            f"{len(wrappers)} wrapper(s); missing={missing_wrappers}",
        )
        settings = fake_home / ".claude" / "settings.json"
        report.check("settings.json installed", settings.exists())
        intent_map = fake_home / ".claude" / "hooks" / "intent-map.json"
        report.check("intent-map.json installed", intent_map.exists())
        manifest = json.loads(
            (REPO_ROOT / "dist" / "manifest.json").read_text(encoding="utf-8")
        )
        report.check(
            "manifest tracks intent-map.json",
            any(
                entry.get("path") == "claude/hooks/intent-map.json"
                for entry in manifest.get("files", [])
            ),
        )

        hooks_dir = fake_home / ".claude" / "hooks"
        report.check(
            "statusline launcher, its module and the dispatcher installed",
            all((hooks_dir / name).exists() for name in ("workflow-statusline.sh", "workflow-statusline.py", "workflow_hooks.py")),
            "expected workflow-statusline.sh, workflow-statusline.py and workflow_hooks.py",
        )

        if settings.exists():
            try:
                loaded = json.loads(settings.read_text(encoding="utf-8"))
                report.check(
                    "settings.json is valid JSON", True, f"{len(loaded)} key(s)"
                )
                hook_text = json.dumps(loaded.get("hooks", {}))
                report.check(
                    "hook path resolved to this HOME",
                    "{{HOME}}" not in hook_text,
                    "placeholder left unresolved" if "{{HOME}}" in hook_text else "",
                )
                status_cmd = json.dumps(loaded.get("statusLine", {}))
                report.check(
                    "statusLine registered and resolved",
                    "workflow-statusline" in status_cmd and "{{HOME}}" not in status_cmd,
                    status_cmd[:120],
                )
                # `statusLine` sits BESIDE `hooks`, so the renderer has to reach it
                # separately. Getting this wrong ships a statusline naming no interpreter,
                # and Claude Code renders a broken statusline as an empty one — no error,
                # just a bar that stops saying anything.
                report.check(
                    "statusLine runs the bash launcher, interpreter filled in",
                    "workflow-statusline.sh" in status_cmd
                    and "{{HOOK_BASH}}" not in json.dumps(loaded)
                    and "powershell" not in status_cmd,
                    status_cmd[:120],
                )
            except json.JSONDecodeError as exc:
                report.check("settings.json is valid JSON", False, str(exc))

        leftover = [
            str(p.relative_to(fake_home))
            for p in fake_home.rglob("*")
            if p.is_file()
            and "{{HOME}}" in p.read_text(encoding="utf-8", errors="ignore")
        ]
        report.check("no unresolved placeholders", not leftover, str(leftover[:3]))

        second = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--apply"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check(
            "re-install is idempotent",
            "replace: " not in second.stdout and "create: " not in second.stdout,
            "second apply reported writes" if "create: " in second.stdout else "",
        )

        checked = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--check"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check(
            "apply followed by check is clean",
            checked.returncode == 0,
            f"rc={checked.returncode}",
        )

        loaded = json.loads(settings.read_text(encoding="utf-8"))
        prompt_entries = loaded.setdefault("hooks", {}).setdefault(
            "UserPromptSubmit", []
        )
        prompt_entries[0].setdefault("hooks", []).append(
            {"type": "command", "command": "user-hook --keep"}
        )
        settings.write_text(json.dumps(loaded, indent=2) + "\n", encoding="utf-8")
        command_only = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "install.py"),
                "--apply",
                "--only-command",
            ],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        loaded = json.loads(settings.read_text(encoding="utf-8"))
        hook_text = json.dumps(loaded.get("hooks", {}))
        report.check(
            "only-command removes the shipped intent hook from an existing install",
            command_only.returncode == 0 and "intent-gate-set" not in hook_text,
            f"rc={command_only.returncode}",
        )
        report.check(
            "only-command preserves a user hook in the shared entry",
            "user-hook --keep" in hook_text,
        )
        checked = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--check"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check(
            "command-only install passes check",
            checked.returncode == 0,
            f"rc={checked.returncode}",
        )

        auto_intent = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--apply", "--auto-intent"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        loaded = json.loads(settings.read_text(encoding="utf-8"))
        hook_text = json.dumps(loaded.get("hooks", {}))
        report.check(
            "auto-intent restores the shipped intent hook",
            auto_intent.returncode == 0 and "intent-gate-set" in hook_text,
            f"rc={auto_intent.returncode}",
        )
        report.check(
            "auto-intent still preserves the user hook",
            "user-hook --keep" in hook_text,
        )

        loaded["hooks"].pop("SessionStart", None)
        settings.write_text(json.dumps(loaded, indent=2) + "\n", encoding="utf-8")
        settings_drift = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--check"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check(
            "check detects installed settings drift",
            settings_drift.returncode == 1
            and "claude/settings.json" in settings_drift.stdout,
            f"rc={settings_drift.returncode}",
        )
        settings_repair = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--apply", "--auto-intent"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check(
            "apply repairs installed settings drift", settings_repair.returncode == 0
        )

        opencode_bundle = bundle_for("opencode")
        opencode_home = provider_home("opencode", fake_home)
        provider_config = opencode_home / opencode_bundle["global_config"][1]
        opencode = json.loads(provider_config.read_text(encoding="utf-8"))
        permission = opencode["agent"]["plan"]["permission"]
        permission["external_directory"] = "allow"
        permission["bash"]["cat *"] = "allow"
        provider_config.write_text(
            json.dumps(opencode, indent=2) + "\n", encoding="utf-8"
        )
        opencode_drift = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--check"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check(
            "check detects installed OpenCode policy drift",
            opencode_drift.returncode == 1
            and "opencode/opencode.json" in opencode_drift.stdout,
            f"rc={opencode_drift.returncode}",
        )
        opencode_repair = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--apply", "--auto-intent"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        repaired_opencode = json.loads(provider_config.read_text(encoding="utf-8"))
        repaired_permission = repaired_opencode["agent"]["plan"]["permission"]
        repaired_bash = repaired_permission["bash"]
        report.check(
            "apply restores enforced OpenCode plan policy",
            opencode_repair.returncode == 0
            and repaired_permission["external_directory"] == "deny"
            and next(iter(repaired_bash.items())) == ("*", "deny")
            and "cat *" not in repaired_bash,
            f"rc={opencode_repair.returncode}",
        )
        repaired_check = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--check"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check("repaired install passes check", repaired_check.returncode == 0)

        reordered = json.loads(provider_config.read_text(encoding="utf-8"))
        reordered_bash = reordered["agent"]["plan"]["permission"]["bash"]
        catch_all = reordered_bash.pop("*")
        reordered_bash["*"] = catch_all
        provider_config.write_text(
            json.dumps(reordered, indent=2) + "\n", encoding="utf-8"
        )
        order_drift = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--check"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check(
            "check detects semantic OpenCode rule-order drift",
            order_drift.returncode == 1
            and "opencode/opencode.json" in order_drift.stdout,
            f"rc={order_drift.returncode}",
        )
        order_repair = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--apply", "--auto-intent"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        canonical = json.loads(provider_config.read_text(encoding="utf-8"))
        canonical_bash = canonical["agent"]["plan"]["permission"]["bash"]
        report.check(
            "apply restores canonical OpenCode rule order",
            order_repair.returncode == 0
            and next(iter(canonical_bash.items())) == ("*", "deny"),
            f"rc={order_repair.returncode}",
        )

        valid_settings = settings.read_text(encoding="utf-8")
        settings.write_text("[]\n", encoding="utf-8")
        non_object_settings = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--check"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check(
            "check rejects non-object settings JSON without traceback",
            non_object_settings.returncode == 1
            and "claude/settings.json" in non_object_settings.stdout
            and "Traceback" not in non_object_settings.stderr,
            f"rc={non_object_settings.returncode}",
        )
        settings.write_text(valid_settings, encoding="utf-8")

        valid_opencode = provider_config.read_text(encoding="utf-8")
        provider_config.write_text("[]\n", encoding="utf-8")
        non_object_opencode = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--check"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(fake_home),
        )
        report.check(
            "check rejects non-object OpenCode JSON without traceback",
            non_object_opencode.returncode == 1
            and "opencode/opencode.json" in non_object_opencode.stdout
            and "Traceback" not in non_object_opencode.stderr,
            f"rc={non_object_opencode.returncode}",
        )
        provider_config.write_text(valid_opencode, encoding="utf-8")

        global_agents = opencode_home / opencode_bundle["agents_dir"]
        report.check(
            "plain install places custom agents in the global fallback",
            len(list(global_agents.glob("wf-*.md"))) >= 5,
        )
        project = Path(tempfile.mkdtemp(prefix="e2e-install-project-"))
        try:
            (project / ".git").mkdir()
            # The machine's config/second_agent.seed.json would otherwise pick this
            # project's provider: init runs as a subprocess, out of reach of the suite's
            # in-process seed swap, and the fake home does not cover a file in the repo.
            _seed_project(project, "opencode")
            # init owns scaffolding now; the installer finds the workspace by cwd instead
            # of taking a --init-project flag.
            project_init = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "main.py"),
                    "--command",
                    "init",
                    "--work-dir",
                    str(project),
                ],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(fake_home),
            )
            report.check(
                "init installs the project opencode boundary",
                project_init.returncode == 0 and (project / "opencode.json").exists(),
                f"rc={project_init.returncode}",
            )
            # The codex half does NOT get the same guarantee, and this is where that is
            # nailed down. Codex loads no project config layer, so init writes no file; the
            # `-c` permission flags that were supposed to replace it turned out to stop
            # nothing (probed on codex-cli 0.147.0 — deny `**`, still read the file, exit 0).
            # What must hold is that switching a workspace to codex leaves it SAYING the
            # boundary is not enforceable, rather than printing a count that reads like
            # protection, and that no stray project config appears to suggest otherwise.
            codex_boundary = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json,sys,pathlib;"
                    "sys.path.insert(0, sys.argv[1]);"
                    "from config.providers import provider_install_module;"
                    "m = provider_install_module('codex');"
                    "print(json.dumps(m.install_project_config(pathlib.Path(sys.argv[2]), sys.argv[1])))",
                    str(REPO_ROOT),
                    str(project),
                ],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            try:
                codex_report = json.loads(codex_boundary.stdout.strip() or "{}")
            except json.JSONDecodeError:
                codex_report = {}
            report.check(
                "codex reports an unenforceable boundary and writes no project file",
                codex_boundary.returncode == 0
                and codex_report.get("status") == "not_enforceable"
                and codex_report.get("permissions_enforced") == 0
                and codex_report.get("permissions_declared", 0) > 0
                and codex_report.get("path") is None
                and not (project / "codex.project.json").exists()
                and not (project / ".codex" / "config.toml").exists(),
                f"rc={codex_boundary.returncode} report={codex_report}",
            )
            project_install = subprocess.run(
                [sys.executable, str(REPO_ROOT / "install.py"), "--apply"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(project),
            )
            report.check(
                "install inside a project keeps custom agents global",
                project_install.returncode == 0
                and len(list(global_agents.glob("wf-*.md"))) >= 5
                and not (project / ".opencode" / "agents").exists(),
                f"rc={project_install.returncode}",
            )
            project_check = subprocess.run(
                [sys.executable, str(REPO_ROOT / "install.py"), "--check"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(project),
            )
            report.check(
                "project-scoped install passes check",
                project_check.returncode == 0,
                f"rc={project_check.returncode}",
            )
            # A codex workspace gets no opencode boundary from init, so --check must not
            # ask for one: that drift had no command that could repair it.
            codex_project = project / "codex-workspace"
            (codex_project / ".git").mkdir(parents=True)
            _seed_project(codex_project, "codex")
            codex_init = subprocess.run(
                [sys.executable, str(REPO_ROOT / "main.py"), "--command", "init",
                 "--work-dir", str(codex_project)],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(fake_home),
            )
            codex_check = subprocess.run(
                [sys.executable, str(REPO_ROOT / "install.py"), "--check"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(codex_project),
            )
            report.check(
                "check skips the opencode boundary in a codex project",
                codex_init.returncode == 0
                and not (codex_project / "opencode.json").exists()
                and codex_check.returncode == 0
                and "project runs codex" in codex_check.stdout,
                f"init rc={codex_init.returncode} check rc={codex_check.returncode}",
            )
            # And it stays fail-closed where the boundary belongs: an opencode project
            # whose opencode.json is gone is still MISSING.
            (project / "opencode.json").unlink()
            opencode_missing = subprocess.run(
                [sys.executable, str(REPO_ROOT / "install.py"), "--check"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(project),
            )
            report.check(
                "check still requires the boundary in an opencode project",
                opencode_missing.returncode == 1
                and "opencode/opencode.project.json" in opencode_missing.stdout,
                f"rc={opencode_missing.returncode}",
            )
        finally:
            shutil.rmtree(project, ignore_errors=True)
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)

    rollback_home = Path(tempfile.mkdtemp(prefix="e2e-rollback-home-"))
    rollback_env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUTF8": "1",
        "USERPROFILE": str(rollback_home),
        "HOME": str(rollback_home),
        "AGENT_HOME": str(rollback_home),
    }
    try:
        installed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--apply"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=rollback_env,
            # Never the repo: install.py upgrades the workspace it finds from its cwd.
            cwd=str(rollback_home),
        )
        receipts = sorted(
            (rollback_home / ".claude" / "backups").glob(
                "install_*/install_receipt.json"
            )
        )
        receipt = (
            json.loads(receipts[-1].read_text(encoding="utf-8")) if receipts else {}
        )
        receipt_keys = {entry.get("key") for entry in receipt.get("entries", [])}
        report.check(
            "receipt v2 covers settings and install mode with post hashes",
            installed.returncode == 0
            and receipt.get("schema_version") == 2
            and {"claude/settings.json", "claude/workflow-install-mode.json"}
            <= receipt_keys
            and all(entry.get("post_sha256") for entry in receipt.get("entries", [])),
            f"rc={installed.returncode}, entries={len(receipt.get('entries', []))}",
        )

        rolled_back = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--rollback", "--apply"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=rollback_env,
            # Never the repo: install.py upgrades the workspace it finds from its cwd.
            cwd=str(rollback_home),
        )
        report.check(
            "clean rollback removes files created by the install",
            rolled_back.returncode == 0
            and not (rollback_home / ".claude" / "CLAUDE.md").exists()
            and not (rollback_home / ".claude" / "settings.json").exists()
            and not (
                rollback_home / ".claude" / ".workflow-install-mode.json"
            ).exists(),
            f"rc={rolled_back.returncode}",
        )

        subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--apply"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=rollback_env,
            check=True,
            # Never the repo: install.py upgrades the workspace it finds from its cwd.
            cwd=str(rollback_home),
        )
        edited = rollback_home / ".claude" / "CLAUDE.md"
        edited.write_text(
            edited.read_text(encoding="utf-8") + "\nuser edit\n", encoding="utf-8"
        )
        untouched_peer = rollback_home / ".claude" / "settings.json"
        refused = subprocess.run(
            [sys.executable, str(REPO_ROOT / "install.py"), "--rollback", "--apply"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=rollback_env,
            # Never the repo: install.py upgrades the workspace it finds from its cwd.
            cwd=str(rollback_home),
        )
        report.check(
            "rollback refuses edited destinations without partial mutation",
            refused.returncode == 2
            and edited.exists()
            and edited.read_text(encoding="utf-8").endswith("user edit\n")
            and untouched_peer.exists(),
            f"rc={refused.returncode}",
        )
    finally:
        shutil.rmtree(rollback_home, ignore_errors=True)
