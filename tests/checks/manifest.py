"""`src/exoma/_bundle/manifest.json` must describe the dist/ tree that ships.

CI ran `gen_manifest --check` and the local suite did not, so a manifest left stale by an
in-repo edit to dist/ stayed green on the maintainer's machine and only failed after push.
This runs the tool's real entry point, the same way the stamp_version check does: a check
that reimplements the hashing would prove its own copy.
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

from tests.checks.support import assert_true

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "src"))


def _test_manifest_matches_dist() -> None:
    from tools.maintain import gen_manifest

    buffer = io.StringIO()
    original_argv = sys.argv
    sys.argv = ["gen_manifest.py", "--check"]
    try:
        with contextlib.redirect_stdout(buffer):
            code = gen_manifest.main()
    finally:
        sys.argv = original_argv
    assert_true(
        code == 0,
        f"src/exoma/_bundle/manifest.json is stale:\n{buffer.getvalue().strip()}\n"
        "Run: python tools/maintain/gen_manifest.py",
    )


def _run_package_artifacts() -> None:
    """Explicit opt-in callback; ordinary manifest/default checks never build artifacts."""
    _test_manifest_matches_dist()
    _check_package_artifacts()


def _test_package_artifacts_dispatch() -> None:
    """Exercise the real CLI dispatch without building or accessing the network."""
    import runpy
    from unittest.mock import Mock, patch

    # A non-main name loads the real parser/registry without starting the scenario.
    # Restore the entrypoint's environment/path setup after this nested import.
    import os
    with patch.dict(os.environ), patch.object(sys, "path", list(sys.path)):
        runner = runpy.run_path(str(REPO_ROOT / "tests/run.py"), run_name="_artifact_dispatch_regression")

    def invoke(arguments, *, smoke_error=None, manifest_code=0):
        smoke = Mock(side_effect=smoke_error)
        scenario = Mock(side_effect=_test_manifest_matches_dist)
        output = io.StringIO()
        with patch.object(sys, "argv", ["tests/run.py", *arguments]), \
                patch.object(sys.modules[__name__], "_check_package_artifacts", smoke), \
                patch("tools.maintain.gen_manifest.main", return_value=manifest_code), \
                patch.dict(runner["SUITES"], {"scenario": (scenario, "default dispatch control")}), \
                contextlib.redirect_stdout(output):
            code = runner["main"]()
        return code, smoke, scenario, output.getvalue()

    for arguments in (["--only=package-artifacts"], ["--only", "package-artifacts"]):
        code, smoke, _, output = invoke(arguments)
        assert_true(code == 0 and smoke.call_count == 1,
                    f"{arguments} must run exactly one artifact smoke, got {smoke.call_count}: {output}")
        code, smoke, _, output = invoke(arguments, smoke_error=RuntimeError("injected smoke failure"))
        assert_true(code == 1 and smoke.call_count == 1 and "FAIL  package-artifacts" in output
                    and "PASS  package-artifacts" not in output,
                    f"a failed smoke must not pass for {arguments}: {output}")
        code, smoke, _, output = invoke(arguments, manifest_code=1)
        assert_true(code == 1 and smoke.call_count == 0,
                    f"a failed manifest must stop before artifact smoke: {output}")
    for arguments in (["--only", "manifest"], []):
        code, smoke, scenario, output = invoke(arguments)
        assert_true(code == 0 and smoke.call_count == 0 and scenario.call_count == (not arguments),
                    f"manifest/default dispatch must not build artifacts: {output}")


def _check_package_artifacts() -> None:
    """Explicit opt-in: build the snapshot and exercise both installed artifacts."""
    import os
    import shutil
    import subprocess
    import tempfile
    import venv
    from exoma.config.settings import TOOL_VERSION
    from tests.checks.support import no_console

    uv = shutil.which("uv")
    assert_true(uv is not None, "package-artifacts requires the existing uv build tool")
    with tempfile.TemporaryDirectory(prefix="exoma-package-artifacts-") as tmp:
        root = Path(tmp)
        source = root / "source"
        source.mkdir()
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(REPO_ROOT / name, source / name)
        shutil.copytree(REPO_ROOT / "src/exoma", source / "src/exoma",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        home = root / "home"
        home.mkdir()
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), AGENT_HOME=str(home),
                   EXOMA_HOME=str(home / "exoma"), CODEX_HOME=str(home / ".codex"),
                   AGENT_ENV_FILE=str(home / "absent.env"), UV_CACHE_DIR=str(root / "uv-cache"),
                   PIP_CACHE_DIR=str(root / "pip-cache"))
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        for key in ("AGENT_PATH", "EXOMA_AGENT_PATH", "WORKFLOW_AGENT_ROLE"):
            env.pop(key, None)
        def run(args, cwd=source):
            result = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", timeout=180, **no_console())
            assert_true(result.returncode == 0, f"artifact smoke failed: {result.stdout[-1800:]}")
            return result
        run([uv, "build"])
        artifacts = list((source / "dist").glob("*.whl")) + list((source / "dist").glob("*.tar.gz"))
        assert_true(len(artifacts) == 2, "current snapshot produces one wheel and one sdist")
        check_bundle = (
            "import hashlib,json,pathlib,exoma; b=pathlib.Path(exoma.__file__).parent/'_bundle'; "
            "m=json.loads((b/'manifest.json').read_text(encoding='utf-8')); "
            f"assert m['version']=={TOOL_VERSION!r}; "
            "assert all(hashlib.sha256((b/'config'/e['path']).read_text(encoding='utf-8').encode()).hexdigest()==e['sha256'] for e in m['files'])"
        )
        for index, artifact in enumerate(artifacts):
            target = root / f"venv-{index}"
            venv.EnvBuilder(with_pip=True).create(target)
            python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            run([str(python), "-I", "-m", "pip", "install", "--no-deps", str(artifact)], root)
            run([str(python), "-I", "-c", check_bundle], root)
            run([str(python), "-I", "-m", "exoma", "--version"], root)
            project = root / f"project-{index}"
            run([str(python), "-I", "-m", "exoma", "init", "--work-dir", str(project)], root)
            run([str(python), "-I", "-m", "exoma", "--command", "inspect", "--work-dir", str(project)], root)


def _test_malformed_manifest_entry_fails_closed() -> None:
    """A manifest entry doctor cannot read is a broken manifest, not an edited install.

    A non-dict entry raised straight out of doctor; an entry without a hash compared
    unequal and was reported as local drift. Both now stop the check with a named error,
    which doctor turns into an issue."""
    import json
    import shutil
    import tempfile

    from exoma.core.audit.bundle_integrity import _bundle_integrity

    root = Path(tempfile.mkdtemp(prefix="bundle-malformed-"))
    try:
        from unittest.mock import patch
        from exoma import install
        from exoma.installer.check import _run_check
        for body in (b"{broken", b"[]", b"null", b'{"files": null}',
                     b'{"files": [null]}', b"\xff\xfe"):
            manifest = root / "invalid.json"
            manifest.write_bytes(body)
            with patch.object(install, "MANIFEST", manifest), \
                    patch.object(sys, "argv", ["exoma.install", "--check", "--no-input"]), \
                    patch.object(install, "_begin_receipt", side_effect=AssertionError("must not write")), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = install.main()
            assert_true(code == 1 and manifest.read_bytes() == body,
                        "invalid manifests must return a controlled error before any write")
        with contextlib.redirect_stdout(io.StringIO()):
            assert_true(_run_check({"files": None}) == 1,
                        "direct install check rejects malformed manifest shape")
        for label, bad in (
            ("a bare string", "claude/skills/plan.md"),
            ("an entry without a hash", {"path": "claude/skills/plan.md"}),
            ("an entry with an empty path", {"path": "", "sha256": "0" * 64}),
            ("an entry whose hash is not 64 hex characters", {"path": "claude/skills/plan.md", "sha256": "not-a-hash"}),
        ):
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"targets": {}, "files": [{"path": "claude/CLAUDE.md", "sha256": "0" * 64}, bad]}),
                encoding="utf-8",
            )
            result = _bundle_integrity(REPO_ROOT / "src" / "exoma" / "_bundle" / "config", manifest)
            assert_true(
                result.get("malformed") == [1] and "malformed" in str(result.get("error")),
                f"{label}: a malformed entry fails the check by name: {result}",
            )
            assert_true(result.get("checked") == 0, f"{label}: nothing is vouched for from a broken manifest: {result}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_doctor_agrees_with_install_check() -> None:
    """Doctor's bundle integrity judges a clean 1.0 install clean.

    A skill rendered with its host (`{{HOST}}`) never matches the manifest hash, and a
    codex project has no opencode.json because init installs only the selected provider's
    boundary. Doctor reported both as drift while `exoma install --check` said READY.
    """
    import hashlib
    import json
    import shutil
    import tempfile

    from exoma.core.audit.bundle_integrity import _bundle_integrity
    from exoma.installer.base import _render_text

    dist = REPO_ROOT / "src" / "exoma" / "_bundle" / "config"
    skill = "hosts/claude/skills/init.md"
    boundary = "providers/opencode/opencode.project.json"
    root = Path(tempfile.mkdtemp(prefix="bundle-rendered-"))
    try:
        project = root / "project"
        (project / ".workflow").mkdir(parents=True)
        (project / ".workflow" / "second_agent.json").write_text(
            json.dumps({"provider": "codex"}), encoding="utf-8"
        )
        installed = root / "skills"
        installed.mkdir()
        rendered = _render_text(dist / skill, skill, project)
        assert_true("{{HOST}}" not in rendered and rendered != (dist / skill).read_text(encoding="utf-8"),
                    "the fixture skill is one the installer renders")
        (installed / "init.md").write_text(rendered, encoding="utf-8")

        def digest(rel: str) -> str:
            return hashlib.sha256((dist / rel).read_text(encoding="utf-8").encode("utf-8")).hexdigest()

        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({
            "targets": {"hosts/claude/skills": installed.as_posix(), boundary: "{{PROJECT_ROOT}}/opencode.json"},
            "files": [{"path": skill, "sha256": digest(skill)},
                      {"path": boundary, "sha256": digest(boundary), "merge": "merge"}],
        }), encoding="utf-8")
        result = _bundle_integrity(dist, manifest, project)
        assert_true(result["drift"] == [], f"a rendered skill and an unselected provider's boundary are not drift: {result}")
        assert_true({"path": boundary, "reason": "provider_not_selected"} in result["skipped"],
                    f"the unselected boundary is reported as skipped: {result}")

        (installed / "init.md").write_text(rendered + "\nlocal edit\n", encoding="utf-8")
        result = _bundle_integrity(dist, manifest, project)
        assert_true({"path": skill, "reason": "content_differs"} in result["drift"],
                    f"a real edit to a rendered file is still drift: {result}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
