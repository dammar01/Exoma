"""Offline regressions for the maintained OpenCode Control. No installed host required."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from tests.checks.support import no_console
from tools.maintain import w3_opencode_control as probe


def archive_cases(root):
    payload = root / "data.json"
    payload.write_bytes(b"fixture")
    manifest = root / "sha256-manifest.json"
    manifest.write_text(json.dumps({payload.name: probe.digest(payload.read_bytes())}), encoding="utf-8")
    pin = probe.digest(manifest.read_bytes())
    assert probe.validate_archive(root, pin) == 1
    with redirect_stdout(io.StringIO()):
        assert probe.main(["validate-archive", str(root), "--expected-manifest-sha256", pin]) == 0
    for entry, expected in (("data.json", pin), ("../outside", None), ("absent", None)):
        manifest.write_text(json.dumps({entry: "0" * 64}), encoding="utf-8")
        for value in (expected or probe.digest(manifest.read_bytes()), probe.digest(manifest.read_bytes())):
            try:
                probe.validate_archive(root, value)
            except (OSError, ValueError):
                pass
            else:
                raise AssertionError("mismatched or escaping entry accepted")
    for args in (["-h"], ["probe"], ["diagnostic-interrupt"]):
        text = io.StringIO()
        with redirect_stdout(text), redirect_stderr(text):
            try:
                probe.main(args)
            except SystemExit as exc:
                assert exc.code == (0 if args == ["-h"] else 2)
            else:
                raise AssertionError("unexpected supported command")
        if args == ["-h"]:
            assert "diagnostic-interrupt" not in text.getvalue() and "validate-archive" in text.getvalue()

    # A multi-chunk entry followed by a second file consumes a single shared budget.
    second = root / "second"
    second.write_bytes(b"x")
    payload.write_bytes(b"a" * 65537)
    entries = {file.name: probe.digest(file.read_bytes()) for file in (payload, second)}
    raw = json.dumps(entries).encode()
    manifest.write_bytes(raw)
    pin = probe.digest(raw)
    def reject():
        try:
            probe.validate_archive(root, pin)
        except ValueError:
            return
        raise AssertionError("archive over its limit was accepted")
    with patch.object(probe, "INPUT_BYTE_LIMIT", len(raw)), patch.object(probe, "ARCHIVE_BYTE_LIMIT", 65538):
        assert probe.validate_archive(root, pin) == 2
    with patch.object(probe, "ARCHIVE_BYTE_LIMIT", 65537):
        reject()
    with patch.object(probe, "INPUT_BYTE_LIMIT", len(raw) - 1):
        reject()
    with patch.object(probe, "ARCHIVE_ENTRY_LIMIT", 1):
        reject()
    with patch.object(probe, "CONTROL_02_PIN", pin):
        reject()  # The real-archive route requires exactly 22 entries.

    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "run.py"),
               "--w3-control-archive", str(root)]
    for options, expected in (([], 2), (["--w3-archive-check-id", "0" * 32], 1),
                              (["--w3-archive-check-id", "0" * 32, "--only", "registry"], 2)):
        check = subprocess.run([*command, *options], capture_output=True, timeout=30, **no_console())
        assert check.returncode == expected, check.stderr


def capture_cases(root):
    project = root / "workspace with spaces"
    trace = probe.Capture(project, "private-title")
    def line(message, run="run-1", directory=project, suffix=""):
        fields = f'timestamp=t level=INFO run={run} message={json.dumps(message)} directory={json.dumps(str(directory))}'
        return (fields + suffix + "\n").encode()
    init = line("creating instance")
    created = line("created", suffix=" title=private-title id=private-id")
    data = (line("creating instance", directory=root) + init + line("created", run="foreign")
            + line("created", suffix=" title=other id=private-id") + created + created
            + line("created", suffix=" title=x title=y") + b"broken\n" + b"x" * (probe.LINE_LIMIT * 2)
            + b"\n" + b"partial")
    # Use a producer with a real stderr pipe. Split writes within records.
    program = ("import sys; data=sys.stdin.buffer.read(); stream=sys.stderr.buffer; "
               "stream.write(data[:11]); stream.flush(); stream.write(data[11:]); stream.flush()")
    with subprocess.Popen([sys.executable, "-I", "-c", program], stdin=subprocess.PIPE,
                          stderr=subprocess.PIPE, **no_console()) as child:
        reader = threading.Thread(target=trace.drain, args=(child.stderr,), daemon=True)
        reader.start()
        child.stdin.write(data)
        child.stdin.close()
        assert child.wait(timeout=10) == 0
        reader.join(3)
    result = trace.snapshot(reader.is_alive())
    assert result["stderr_eof"] and not result["drain_timeout"] and not result["read_error"]
    assert result["duplicate_markers"] == 1 and sum(result["lines"].values()) == 10
    assert result["lines"] == {"matched": 3, "unrelated": 3,
                               "malformed": 2, "oversized": 1, "partial": 1}, result
    assert set(result["reader_monotonic_ns"]) == {"workspace_initialization", "title_matched_creation"}
    assert "private-" not in json.dumps(result) and str(project) not in json.dumps(result)
    quoted = b'timestamp=t level=INFO run=r message="creating instance" directory="C:\\\\Work\\\\W3 Test"\n'
    assert probe.log_fields(quoted)["directory"] == r"C:\Work\W3 Test"
    failed = probe.Capture(project, "title")
    failed.drain(Mock(readline=Mock(side_effect=OSError)))
    assert failed.snapshot(False)["read_error"] and not failed.snapshot(False)["stderr_eof"]
    truncated = probe.Capture(project, "title")
    truncated.drain(Mock(readline=Mock(side_effect=[b"x" * (probe.LINE_LIMIT + 1), OSError()])))
    capture = truncated.snapshot(False)
    assert capture["lines"] == {"oversized": 1} and capture["read_error"] and not capture["stderr_eof"]


def transport_cases(root):
    root = root / "workspace & +"
    ready = {"directory": str(root)}
    for reply, expected in ((probe.RequestTimeout("awaiting_headers"), "timeout"),
                            ({"directory": str(root.parent)}, "invalid_workspace"),
                            ({}, "invalid_workspace")):
        client = Mock()
        client.request.side_effect = [{"healthy": True}, reply]
        record = {}
        with patch.object(probe.time, "monotonic", return_value=100.0):
            probe.observe(client, root, "title", record, 160.0)
        assert record["readiness"] == expected
        assert [call.args[0] for call in client.request.call_args_list] == ["GET", "GET"]
        path = urlsplit(client.request.call_args.args[1])
        assert path.path == "/path" and parse_qs(path.query) == {"directory": [str(root)]}
        assert client.request.call_args.args[3] == 130.0
        if expected == "timeout":
            assert record["readiness_timeout"] == "awaiting_headers"
        assert "create_requested_ns" not in record

    good = {"id": "ses_A", "title": "title", "directory": str(root)}
    for failure in ("connecting", "sending_request", "awaiting_headers", "reading_body"):
        for rows, result in (([good], "visible"), ([{**good, "directory": str(root.parent)}], "no_match"),
                             ([], "no_match"), ([None], "malformed"), ([good, {}], "malformed"),
                             ({"items": []}, "malformed"), ([good] * 5, "malformed"),
                             (probe.RequestTimeout("reading_body"), "timeout")):
            client = Mock()
            client.request.side_effect = [{"healthy": True}, ready, probe.RequestTimeout(failure), rows]
            record = {}
            with patch.object(probe.time, "sleep"):
                probe.observe(client, root, "title", record, time.monotonic() + 30)
            calls = client.request.call_args_list
            assert [call.args[0] for call in calls] == ["GET", "GET", "POST", "GET"]
            create, lookup = (urlsplit(call.args[1]) for call in calls[2:])
            assert create.path == lookup.path == "/session"
            assert parse_qs(create.query) == {"directory": [str(root)]}
            assert parse_qs(lookup.query) == {"directory": [str(root)], "search": ["title"], "limit": ["4"]}
            assert record["create_timeout"] == failure and record["lookup"] == result
            assert record["readiness"] == "verified" and "prompt_attempt_ns" not in record
            if result == "timeout":
                assert record["lookup_timeout"] == "reading_body"

    for operation, phase in (("connect", "connecting"), ("request", "sending_request"),
                             ("getresponse", "awaiting_headers")):
        conn = Mock()
        getattr(conn, operation).side_effect = socket.timeout()
        with patch.object(probe.http.client, "HTTPConnection", return_value=conn):
            try:
                probe.Client(1, "fixture").request("GET", "/synthetic", None, time.monotonic() + 1)
            except probe.RequestTimeout as error:
                assert error.stage == phase
            else:
                raise AssertionError("expected transport timeout")
    # A real trickling loopback response makes progress well inside its socket timeout.
    # Keep the HTTP body open beyond the absolute deadline.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        stop = threading.Event()
        def trickle():
            with listener.accept()[0] as peer:
                peer.recv(8192)
                peer.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 100000\r\n\r\n")
                while not stop.wait(.02):
                    try:
                        peer.sendall(b"x")
                    except OSError:
                        return
        producer = threading.Thread(target=trickle, daemon=True)
        producer.start()
        try:
            client = probe.Client(listener.getsockname()[1], "fixture")
            start = time.monotonic()
            try:
                trace = {}
                client.request("GET", "/synthetic", None, start + .5, trace=trace)
            except probe.RequestTimeout as exc:
                assert exc.stage == "reading_body"
                assert trace["status_class"] == 2
                assert trace["connect_started_ns"] <= trace["connected_ns"] <= trace["request_sent_ns"] <= trace["headers_received_ns"]
            else:
                raise AssertionError("trickling body exceeded the absolute deadline")
            assert time.monotonic() - start < 2
        finally:
            stop.set()
            producer.join(3)
    for key, value in (("id", 42), ("id", ""), ("directory", str(root.parent)), ("title", "other")):
        bad = {**good, key: value}
        assert probe.matching_session(bad, root, "title") is None
        try:
            probe.create_session(Mock(return_value=bad), root, "title", {}, time.monotonic() + 10)
        except ValueError:
            pass
        else:
            raise AssertionError("foreign session accepted")
    client = Mock()
    client.request.side_effect = [{"healthy": True}, ready, good, probe.RequestTimeout("reading_body")]
    record = {}
    try:
        probe.observe(client, root, "title", record, time.monotonic() + 10)
    except probe.RequestTimeout:
        pass
    assert record["prompt_attempt_ns"] and "explore_accepted" not in record


def scope_cases(root):
    # Simulate denied accounting, timeout with outstanding members and a clean exit.
    for active, expected in (([0], True), (OSError(), False), ([1, 1], False)):
        scope = probe.HostScope.__new__(probe.HostScope)
        scope.job = Mock()
        proc = Mock()
        scope.enrolled = [proc]
        scope.processes = [proc]
        scope.job.active.side_effect = active
        with patch.object(probe, "remaining", side_effect=TimeoutError() if active == [1, 1] else None,
                          return_value=.1):
            result = scope.close(time.monotonic() + .1)
        assert result["containment_confirmed"] is expected
        scope.job.close.assert_called_once()
    if probe.os.name != "nt":
        return
    # A harmless grandchild outlives the launcher, holds no stderr pipe and exits on
    # its own after 15 seconds even if the fixture is interrupted.
    scope = probe.HostScope()
    marker = root / "forbidden-launch"
    try:
        with patch.object(scope, "enroll", side_effect=OSError()):
            try:
                scope.launch([sys.executable, "-Ic", "from pathlib import Path; Path('forbidden-launch').touch()"],
                             cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
            else:
                raise AssertionError("failed enrollment admitted host")
    finally:
        denied = scope.close(time.monotonic() + 4)
    assert not marker.exists() and denied["launcher_exited"] and not denied["membership_verified"]
    scope = probe.HostScope()
    try:
        program = ("import sys,subprocess; subprocess.Popen([sys.executable,'-Ic','import time;time.sleep(15)'],"
                   "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)")
        child = scope.launch([sys.executable, "-Ic", program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert child.wait(timeout=5) == 0
        assert scope.job.active() > 0, "launcher exit cannot certify cleanup"
    finally:
        observed = scope.close(time.monotonic() + 5)
    assert observed["termination_requested"] and observed["active_members"] == 0
    assert observed["membership_verified"] and observed["containment_confirmed"]


def ack_cases(root):
    state = {"root": str(root), "session": "sid", "generation": "gen", "receipt": "receipt", "job_id": "job",
             "command": "explore", "phase": "collected"}
    actions = [
        ("delegate-select --task explore", {"command": "explore", "phase": "selected", "generation": "gen"}),
        ("explore --request gen", {"job_id": "job", "delegation": {
            "command": "explore", "generation": "gen", "receipt": "receipt"}}),
        ("delegate-ack --task receipt", {"phase": "collected", "job_id": "job"})]
    parts = []
    for index, (args, meta) in enumerate(actions):
        parts.append({"type": "tool", "tool": "bash", "id": f"part-{index}", "sessionID": "ses_A", "state": {
            "status": "completed", "time": {"start": index * 2, "end": index * 2 + 1},
            "input": {"command": f"bash .workflow/run.sh {args} --session sid"},
            "output": json.dumps({"ok": True, "meta": meta})}})
    def messages(parts):
        return [{"info": {"id": "msg_private", "role": "assistant", "sessionID": "ses_A", "finish": "stop",
                          "time": {"completed": 9}},
                 "parts": parts}]

    def accepted(parts, state):
        return bool(probe.Transcript("ses_A", root).inspect(messages(parts), state))
    assert accepted(parts, state)
    lost = deepcopy(parts[1])
    lost["id"] = "first-attempt"
    lost["state"]["output"] = ""
    assert not accepted([parts[0], lost, *parts[1:]], state), "a second Explore cannot replace lost output"
    for error, expected in (("provider_error", True), ("cleanup_pending", False),
                            ("cancellation_pending", False)):
        sample = deepcopy(parts)
        result = json.loads(sample[1]["state"]["output"])
        result.update(ok=False, error_type=error)
        sample[1]["state"]["output"] = "[JOB] {}\n" + json.dumps(result)
        assert accepted(sample, state) == expected
    for key, value in (("receipt", "other"), ("job_id", "other"), ("generation", "other"),
                       ("session", "other"), ("root", str(root.parent)), ("phase", "returned")):
        assert not accepted(parts, {**state, key: value})
    early = deepcopy(parts)
    early[-1]["state"]["time"]["start"] = 1
    assert not accepted(early, state)
    assert not accepted(parts + parts, state)
    assert not accepted(parts[:2], state)
    # Exercise actual orchestration with the native HTTP and workspace boundaries substituted.
    path = root / ".workflow/data/sessions/sid/runtime/delegation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    client = Mock()
    from exoma.core.jobs.diagnostics import _read
    good = {"id": "ses_A", "title": "title", "directory": str(root)}
    for terminal_error, host_version in ((False, "1.19.6"), (True, "1.19.3")):
        sample = deepcopy(parts)
        sample[0]["state"]["input"]["command"] = sample[0]["state"]["input"]["command"].replace(
            "bash .workflow/run.sh", "& 'C:/Program Files/Git/bin/bash.exe' '.workflow/run.sh'")
        if terminal_error:
            doc = json.loads(sample[1]["state"]["output"])
            sample[1]["state"]["output"] = json.dumps({**doc, "ok": False, "error_type": "provider_error"})
        transcript = messages(sample)
        idle_map = {"ses_A": {"type": "idle"}} if terminal_error else {}
        client.request.side_effect = [{"healthy": True}, {"directory": str(root)}, good,
                                      None, transcript, idle_map, transcript]
        record = {}
        probe.observe(client, root, "title", record, time.monotonic() + 30, _read)
        # Exercise the report writer without starting any native host.
        report_cell = root / ("owned-error" if terminal_error else "owned-success")
        report_cell.mkdir()
        version = Mock(returncode=0)
        version.communicate.return_value = (host_version.encode("ascii"), None)
        scope = Mock()
        scope.launch.side_effect = [version, Mock(stderr=io.BytesIO())]
        scope.close.return_value = {"containment_confirmed": True}
        with (patch.object(sys, "platform", "win32"),
              patch.object(probe, "staged_inputs", return_value=(Path(sys.executable), root)) as staging,
              patch.object(probe, "candidate_reader", return_value=_read),
              patch.object(probe, "HostScope", return_value=scope),
              patch.object(probe, "observe", side_effect=lambda c, p, t, r, e, fn: r.update(record))):
            args = ["control", str(report_cell), "--candidate", "a" * 40]
            if host_version != "1.19.3":
                args.extend(["--expected-host-version", host_version])
            assert probe.main(args) == 0
            staging.assert_called_once_with(report_cell, "a" * 40, host_version)
        exported = json.loads((report_cell / "w3-control.json").read_bytes())
        assert exported["observed_host_version"] == exported["expected_host_version"] == host_version
        assert exported["version_check"] == "verified"
        assert exported["original_job_acknowledged"] and exported["dispatch_observed"]
        proof = exported["foreground"]
        events = proof["events"]
        settlement = proof["settlement"]
        assert settlement["native_completed_ms"] == settlement["reread_native_completed_ms"] == 9
        assert settlement["native_completed_ms"] >= events[-1]["native_end_ms"]
        assert settlement["assistant_message_sha256"] == probe.digest(b"msg_private")
        assert settlement["idle_status"] == ("explicit_idle" if terminal_error else "absent_from_status_map")
        assert settlement["idle_observed_monotonic_ns"] <= settlement["final_reread_monotonic_ns"]
        assert [row["action"] for row in events] == ["delegate-select", "explore", "delegate-ack"]
        assert events[1]["outcome"] == ("owned_terminal_error" if terminal_error else "terminal_success")
        assert (events[1]["output_sha256"]["receipt"] == events[2]["input_sha256"]["receipt"]
                == proof["durable"]["binding_sha256"]["receipt"] == probe.digest(b"receipt"))
        assert proof["native_session_sha256"] != events[0]["input_sha256"]["session"]
        raw = json.dumps(exported)
        assert all(private not in raw for private in ('"sid"', '"ses_A"', '"gen"', "msg_private", 'run.sh', str(root)))
    tracker = probe.Transcript("ses_A", root)
    pending = deepcopy(parts[0])
    pending["state"] = {"status": "pending", "input": {}}
    assert tracker.inspect(messages([pending]), {}) is None and not tracker.invalid
    unfinished = deepcopy(parts)
    unfinished[1]["state"]["status"] = "running"
    assert tracker.inspect(messages(unfinished[:2]), {}) is None
    assert tracker.inspect(messages(parts), state) and tracker.inspect(messages(parts), state)
    tracker = probe.Transcript("ses_A", root)
    assert tracker.inspect(messages([parts[0], lost]), {}) is None
    assert not tracker.inspect(messages(parts), state), "lost attempt vanished between polls"
    for invalid in ([None], [{"parts": 3}], messages([None]), messages([{"sessionID": "foreign"}])):
        assert not probe.Transcript("ses_A", root).inspect(invalid, state)
    # End each synthetic poll at the next request so a non-qualifying result cannot
    # later be overwritten by an unrelated qualifying response.
    transcript = messages(parts)
    early = deepcopy(transcript)
    early[-1]["info"]["time"]["completed"] = 4
    changed = deepcopy(transcript)
    changed[-1]["info"]["time"]["completed"] = 10
    for replies in ([early], [transcript, {"ses_A": {"type": "busy"}}], [transcript, []],
                    [transcript, {"other": 17}], [transcript, {}, changed]):
        client.request.side_effect = [{"healthy": True}, {"directory": str(root)}, good, None, *replies, TimeoutError()]
        record = {}
        with patch.object(probe.time, "sleep"):
            try:
                probe.observe(client, root, "title", record, time.monotonic() + 30, _read)
            except TimeoutError:
                pass
        assert not record.get("original_job_acknowledged") and "foreground" not in record
    assert not probe.assistant_completion([{"info": {"role": "assistant", "finish": "tool-calls"}}], 5)
    for data, status in ((b"[]", "malformed"), (b"x" * 262145, "oversized"), (b"{", "unavailable")):
        path.write_bytes(data)
        assert probe.workspace_snapshot(root, _read) == ({}, status)


def assert_relocated_cell(cell, sha):
    try:
        probe.staged_inputs(cell, sha)
    except ValueError:
        return
    raise AssertionError("relocated profile cache admitted")


def assert_prelaunch_refusal(cell, sha, *options):
    with (patch.object(sys, "platform", "win32"), patch.object(probe, "HostScope") as launcher,
          patch.object(probe, "candidate_reader") as reader, redirect_stderr(io.StringIO())):
        assert probe.main(["control", str(cell), "--candidate", sha, *options]) == 1
    reader.assert_not_called()
    launcher.assert_not_called()
    assert not (cell / "w3-control.json").exists()


def version_and_platform_cases(root):
    unused = root / "unsupported"
    with (patch.object(sys, "platform", "linux"), patch.object(probe, "staged_inputs") as staging,
          patch.object(probe, "HostScope") as launcher, redirect_stderr(io.StringIO())):
        assert probe.main(["control", str(unused), "--candidate", "a" * 40]) == 1
    staging.assert_not_called()
    launcher.assert_not_called()
    assert not unused.exists()
    with patch.object(probe, "control") as control, redirect_stderr(io.StringIO()):
        try:
            probe.main(["control", str(unused), "--candidate", "a" * 40, "--expected-host-version", "1.19.4"])
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("unknown host version accepted")
        control.assert_not_called()
    for expected, status, code, output in (("1.19.3", "failed", 1, b"1.19.3"),
                                           ("1.19.3", "mismatch", 0, b"1.19.4"),
                                           ("1.19.6", "mismatch", 0, b"1.19.3")):
        cell = root / f"{expected}-{status}"
        cell.mkdir()
        proc = Mock(returncode=code)
        proc.communicate.return_value = (output, None)
        scope = Mock()
        scope.launch.return_value = proc
        scope.close.return_value = {"containment_confirmed": True}
        with (patch.object(sys, "platform", "win32"), patch.object(probe, "HostScope", return_value=scope),
              patch.object(probe, "staged_inputs", return_value=(Path(sys.executable), cell)),
              patch.object(probe, "candidate_reader")):
            assert not probe.control(cell, "a" * 40, expected)
        scope.launch.assert_called_once()
        assert scope.launch.call_args.args[0][-1] == "--version"
        scope.close.assert_called_once()
        report = json.loads((cell / "w3-control.json").read_bytes())
        assert report["expected_host_version"] == expected and report["version_check"] == status
        if status == "failed":
            assert "observed_host_version" not in report
        else:
            assert report["observed_host_version"] == output.decode("ascii")


def staging_cases(root):
    cell = root / "opencode/fresh"
    project = cell / "project"
    workflow = project / ".workflow"
    workflow.mkdir(parents=True)
    exe = Path(sys.executable).absolute()
    sha = "a" * 40
    runner = (f'ARGS+=(--session "$SESSION" --work-dir "{project.as_posix()}" --pretty)\n'
              + 'exec "%s" "${ARGS[@]}"\n' % exe.as_posix()).encode()
    (workflow / "run.sh").write_bytes(runner)
    (workflow / "config.json").write_text(json.dumps({"project": {"root": str(project)},
                                                      "runtime": {"python": str(exe)}}), encoding="utf-8")
    setup = {"candidate": sha, "host": "opencode", "mode": "fresh", "setup": "pass", "host_version": "1.19.3",
             "host_version_exit": 0, "host_binary": str(exe), "runner_sha256": probe.digest(runner)}
    (cell / "setup.json").write_text(json.dumps(setup), encoding="utf-8")
    profile = cell / "h/.config/opencode"
    hook = profile / "exoma/hooks/opencode-host.py"
    hook.parent.mkdir(parents=True)
    hook.touch()
    plugin = profile / "plugins/exoma.js"
    plugin.parent.mkdir()
    plugin.write_text("const PYTHON = " + json.dumps(str(exe)) + "\nconst HOOK = " + json.dumps(str(hook)),
                      encoding="utf-8")
    dist = Mock()
    site = root / "candidate/Lib/site-packages"
    info = site / "exoma-1.2.2.dist-info"
    info.mkdir(parents=True)
    direct_url = info / "direct_url.json"
    direct_url.write_text(json.dumps({"vcs_info": {"commit_id": sha}}), encoding="utf-8")
    dist.locate_file.return_value = site
    with (patch.object(probe.metadata, "distribution", return_value=dist),
          patch.object(sys, "prefix", str(root / "candidate"))):
        assert probe.staged_inputs(cell, sha) == (exe, project)
        # Trailing JSON whitespace leaves setup semantically valid. Only the actual
        # byte budget should distinguish these two inputs.
        setup_path = cell / "setup.json"
        original_setup = setup_path.read_bytes()
        assert_prelaunch_refusal(cell, sha, "--expected-host-version", "1.19.6")
        setup_path.write_text(json.dumps({**setup, "host_version": "1.19.6"}), encoding="utf-8")
        try:
            assert probe.staged_inputs(cell, sha, "1.19.6") == (exe, project)
            assert_prelaunch_refusal(cell, sha)
        finally:
            setup_path.write_bytes(original_setup)
        setup_path.write_bytes(original_setup.ljust(probe.INPUT_BYTE_LIMIT, b" "))
        assert probe.staged_inputs(cell, sha) == (exe, project)
        setup_path.write_bytes(setup_path.read_bytes() + b" ")
        try:
            assert_prelaunch_refusal(cell, sha)
        finally:
            setup_path.write_bytes(original_setup)
        for path in (workflow / "config.json", direct_url, plugin, workflow / "run.sh"):
            original = path.read_bytes()
            path.write_bytes(original.ljust(probe.INPUT_BYTE_LIMIT + 1, b" "))
            try:
                assert_prelaunch_refusal(cell, sha)
            finally:
                path.write_bytes(original)
        for path, invalid in ((direct_url, b'{"vcs_info": []}'), (workflow / "config.json", b"[]")):
            original = path.read_bytes()
            path.write_bytes(invalid)
            try:
                assert_prelaunch_refusal(cell, sha)
            finally:
                path.write_bytes(original)
        nested = cell / "h/data/opencode"
        nested.mkdir(parents=True)
        sample = nested / "sample"
        sample.write_bytes(b"offline fixture")
        shared = nested / "second-name"
        shared.hardlink_to(sample)
        try:
            assert_prelaunch_refusal(cell, sha)
        finally:
            shared.unlink()
        assert probe.staged_inputs(cell, sha) == (exe, project)
        # Simulate Windows junction metadata on an ordinary nested directory: the
        # same fixture runs on every CI platform without requiring link privileges.
        lstat = Path.lstat
        def reparse(path):
            if path == nested:
                return Mock(st_mode=lstat(path).st_mode, st_file_attributes=probe.stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return lstat(path)
        with patch.object(Path, "lstat", reparse):
            assert_prelaunch_refusal(cell, sha)
        inspect = probe.inspect_cell
        for limits in ({"entry_limit": 2}, {"depth_limit": 1}):
            with patch.object(probe, "inspect_cell", side_effect=lambda path: inspect(path, **limits)):
                assert_prelaunch_refusal(cell, sha)
        with patch.object(probe.os, "scandir", side_effect=PermissionError()):
            assert_prelaunch_refusal(cell, sha)
        (cell / "setup.json").write_text("[]", encoding="utf-8")
        assert_prelaunch_refusal(cell, sha)
        (cell / "setup.json").write_text(json.dumps(setup), encoding="utf-8")
        # Windows can restrict symlink creation. Simulate the resolved target in that case.
        cache = cell / "h/cache"
        try:
            cache.symlink_to(root, target_is_directory=True)
        except OSError:
            original = Path.resolve
            def moved(path, *args, **kwargs):
                return root if path == cache else original(path, *args, **kwargs)
            with patch.object(Path, "resolve", moved):
                assert_relocated_cell(cell, sha)
        else:
            try:
                assert_relocated_cell(cell, sha)
            finally:
                cache.unlink()
        for mode in ("stale-runner", "used-cell"):
            if mode == "stale-runner":
                (workflow / "run.sh").write_bytes(runner + b"changed")
            else:
                (workflow / "run.sh").write_bytes(runner)
                (cell / "w3-control.json").touch()
            try:
                probe.staged_inputs(cell, sha)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid cell admitted: " + mode)



def _test_w3_opencode_control():
    with tempfile.TemporaryDirectory(prefix="w3-control-") as temp:
        root = Path(temp).resolve()
        archive_cases(root)
        staging_cases(root)
        version_and_platform_cases(root)
        capture_cases(root)
        transport_cases(root)
        scope_cases(root)
        ack_cases(root)

