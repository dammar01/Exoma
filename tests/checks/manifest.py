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
