"""Retention preview regression at the public entry point and filesystem boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from exoma.core.audit.retention import retention_report
from exoma.core.evidence.contracts import TASK_STREAM_NAME
from tests.checks.support import no_console


def check_retention_preview():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        fresh_home = root / "empty-home"
        env = {**os.environ, "EXOMA_HOME": str(fresh_home), "AGENT_HOME": str(fresh_home),
               "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
        args = [sys.executable, "-m", "exoma", "session", "prune", "--work-dir", str(root)]
        p = subprocess.run(args, env=env, cwd=root, capture_output=True, check=True, timeout=30, **no_console())
        assert json.loads(p.stdout)["status"] == "absent"
        assert list(root.iterdir()) == []
        data = root / ".workflow/data"
        records = data / "provider-sessions"
        records.mkdir(parents=True)
        (records / "active.json").write_bytes(b"{}")
        active = data / "sessions/active"
        (active / "runtime").mkdir(parents=True)
        (active / "runtime/lock").write_bytes(b"lock")
        (records / "orphan.json").write_bytes(b"orphan")
        for i in range(23):
            path = data / "sessions" / str(i)
            path.mkdir()
            os.utime(path, (1, 1))
        (data / "evidence.jsonl").write_text(json.dumps({"session": "orphan"}) + "\n")
        mirror = root / ".workflow/current"
        mirror.mkdir()
        (mirror / "session.json").write_text(json.dumps({"session_id": "active"}))
        preview = retention_report(root, preview=True, budget_seconds=5)
        assert preview["status"] == "complete"
        assert preview["categories"]["provider-sessions"]["logical_bytes"] == 8
        pair = next(group for group in preview["sessions"] if group["id"] == "active")
        assert len(pair["paths"]) == 2 and pair["logical_bytes"] == 6
        assert pair["retained_reason"] == "protected" and "current_session" in pair["protected_by"]
        orphan = next(group for group in preview["sessions"] if group["id"] == "orphan")
        assert "indexed_evidence" in orphan["protected_by"]
        assert any(group["retained_reason"] == "retirement_unproven" for group in preview["sessions"])
        assert preview["targets"] == [] and preview["target_bytes"] == 0
        clipped = retention_report(root, preview=True, max_entries=1)
        assert clipped["status"] == "partial" and clipped["totals_are_lower_bounds"]
        assert subprocess.run(args + ["--apply"], env=env, cwd=root, capture_output=True, timeout=30, **no_console()).returncode != 0
        assert (active / "runtime/lock").read_bytes() == b"lock"
        for layout in ("data", "legacy"):
            _check_clean(root / ("clean-" + layout), layout, env)
        old = root / "legacy"
        (old / ".workflow/sessions/old").mkdir(parents=True)
        (old / ".workflow/config.json").write_bytes(b"private")
        legacy = retention_report(old)
        assert legacy["layout"] == "legacy" and "other" not in legacy["categories"]
        link = data / "link"
        try:
            link.symlink_to(old, target_is_directory=True)
        except OSError:
            return  # Windows hosts may require developer mode for symlinks.
        linked = retention_report(root)
        assert linked["skipped"] and linked["totals_are_lower_bounds"]


def _check_clean(project, layout, env):
    workflow = project / ".workflow"
    data = workflow / "data" if layout == "data" else workflow
    records = data / "provider-sessions"
    records.mkdir(parents=True)
    sessions = data / "sessions"
    expected = {}
    for sid in ["active", "indexed", "unknown", *(str(i) for i in range(23))]:
        directory = sessions / sid
        directory.mkdir(parents=True)
        artifact = directory / "artifact.txt"
        record = records / (sid + ".json")
        for path in (artifact, record):
            expected[path] = sid.encode()
            path.write_bytes(expected[path])
        if sid == "active":
            lock = directory / "runtime/lock"
            lock.parent.mkdir()
            lock.write_bytes(b"active")
            expected[lock] = b"active"
        if not sid.isdigit():
            for path in (directory, record):
                os.utime(path, (1, 1))
    (data / TASK_STREAM_NAME).write_bytes(b"{}\n")
    report = retention_report(project, budget_seconds=5)
    assert report["layout"] == layout and report["categories"]["telemetry"]["logical_bytes"] == 3
    index = data / "evidence.jsonl"
    graph = project / "graphify-out"
    graph.mkdir()
    for contents in (b'{"session":"indexed"}\n', b"malformed\n", None):
        if contents is None:
            index.unlink(missing_ok=True)
        else:
            index.write_bytes(contents)
        stale_lock = graph / ".refresh.lock"
        stale_lock.write_text(json.dumps({"started": 1, "pid": os.getpid(), "beat": 1}))
        result = subprocess.run([sys.executable, "-m", "exoma", "--command", "clean",
                                 "--work-dir", str(project), "--session", "retention-fixture"],
                                env=env, cwd=project, capture_output=True, check=True, timeout=30, **no_console())
        meta = json.loads(result.stdout)["meta"]
        assert meta["sessions"]["removed"] == 0 and meta["sessions"]["kept"] >= 52, meta["sessions"]
        assert meta["graph_refresh_lock"]["removed"] and not stale_lock.exists()
        assert all(path.read_bytes() == content for path, content in expected.items())
