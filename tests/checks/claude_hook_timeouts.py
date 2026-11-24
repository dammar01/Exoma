"""Synthetic bindings and rows from the calibrated Claude attachment schema."""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from exoma.core.audit.claude_hook_timeouts import claude_hook_timeouts


def check_timeout_observation():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"AGENT_HOME": tmp}):
        project = Path(tmp).resolve()
        host = project / ".claude"
        transcript = host / "projects" / "fixture" / "session.jsonl"
        transcript.parent.mkdir(parents=True)
        bindings = host / "session_registry.json"
        assert claude_hook_timeouts(project)["count"] is None
        bindings.write_text(json.dumps({"session": {"cwd": str(project), "transcript_path": str(transcript)}}))
        assert claude_hook_timeouts(project)["status"] == "unavailable"
        row = {"type": "attachment", "uuid": "fake-id", "sessionId": "session", "cwd": str(project),
               "version": "2.1.330", "timestamp": datetime.now(timezone.utc).isoformat(),
               "attachment": {"type": "hook_cancelled", "timedOut": False, "command": "private literal"}}

        def write(*rows):
            transcript.write_bytes(b"".join(json.dumps(item).encode() + b"\n" for item in rows))

        write(row)
        normal = claude_hook_timeouts(project)
        assert normal["status"] == "complete" and normal["count"] == 0
        row["attachment"]["timedOut"] = True
        write(row, row)
        result = claude_hook_timeouts(project)
        assert result["count"] == 1 and result["latest"] == row["timestamp"]
        assert "private literal" not in str(result) and str(transcript) not in str(result)
        with transcript.open("ab") as stream:
            stream.write(b'{"torn"')
        assert claude_hook_timeouts(project)["status"] == "partial"
        write({**row, "version": "unknown"})
        assert claude_hook_timeouts(project)["status"] == "unknown"
        write({**row, "cwd": str(project / "other")})
        assert claude_hook_timeouts(project)["count"] is None
        with patch("exoma.core.audit.claude_hook_timeouts._TAIL_BYTES", 15):
            assert claude_hook_timeouts(project)["truncated"]
        for attachment in (None, {}, [], {"type": ""}, {"type": "hook_cancelled", "timedOut": 1}):
            malformed = {**row, "attachment": attachment}
            write(malformed)
            assert claude_hook_timeouts(project)["count"] is None
            write(row, malformed)
            result = claude_hook_timeouts(project)
            assert result["status"] == "partial" and result["count"] == 1

        (project / ".git").mkdir()
        launch = project / "src/pkg"
        launch.mkdir(parents=True)
        bound = {"cwd": str(launch), "transcript_path": str(transcript)}
        bindings.write_text(json.dumps({"session": bound}))
        write({**row, "cwd": str(launch)}, row)
        result = claude_hook_timeouts(project)
        assert result["count"] == 1 and result["status"] == "complete"
        bindings.write_text(json.dumps({"session": bound, "missing": {"cwd": str(project / "missing")}}))
        assert claude_hook_timeouts(project)["status"] == "partial"
        bindings.write_text(json.dumps({"session": bound}))
        for invalid in ({}, {"cwd": 42}):
            bindings.write_text(json.dumps({"session": bound, "unclassified": invalid}))
            result = claude_hook_timeouts(project)
            assert result["status"] == "partial" and result["unavailable_files"] == 1
        bindings.write_text(json.dumps({"session": bound}))
        nested = launch.parent / ".git"
        nested.write_text("gitdir: fixture")
        assert claude_hook_timeouts(project)["files_scanned"] == 0
        nested.unlink()
        nested.mkdir()
        assert claude_hook_timeouts(project)["files_scanned"] == 0
