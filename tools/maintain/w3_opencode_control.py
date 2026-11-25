"""Maintainer probe for one fresh W3 foreground Control on a prepared OpenCode cell."""
import argparse
import base64
from collections import Counter
import hashlib
import http.client
from importlib import metadata
import json
import os
from pathlib import Path, PureWindowsPath
from queue import Empty, Queue
import re
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

CONTROL_02_PIN = "9b046aa298a269abb31197b2e8878547c8e80030a5e51358b98d127e20c560d1"
CONTROL_02_ENTRIES = 22
INPUT_BYTE_LIMIT = 256 * 1024
ARCHIVE_BYTE_LIMIT = 1024 * 1024
ARCHIVE_ENTRY_LIMIT = 256
LINE_LIMIT = 8192
CELL_ENTRY_LIMIT = 8192
CELL_DEPTH_LIMIT = 32
EXPECTED_HOST_VERSION = "1.19.3"
CONTROL_HOST_VERSIONS = (EXPECTED_HOST_VERSION, "1.19.6")
BODY_LIMIT = 4 * 1024 * 1024
FIELD = re.compile(r'([^\s=]+)=("(?:\\.|[^"\\])*"|[^\s"]+)(?: +|$)')
PROMPT = ("Use the installed exoma-explore skill to describe README.md. Select once, dispatch on "
          "the original foreground transport, wait for its terminal result and acknowledge the "
          "actual receipt in a separate call. Stop after this job.")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unique_fields(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("duplicate field")
        fields[key] = value
    return fields


def bounded_bytes(path):
    """Read one sentinel byte to detect overflow even if file size metadata is stale."""
    with path.open("rb") as stream:
        data = stream.read(INPUT_BYTE_LIMIT + 1)
    if len(data) > INPUT_BYTE_LIMIT:
        raise ValueError("preflight input exceeds byte limit")
    return data


def object_file(path):
    obj = json.loads(bounded_bytes(path), object_pairs_hook=unique_fields)
    if not isinstance(obj, dict):
        raise ValueError("expected JSON object")
    return obj


def archive_file(path):
    if path.resolve(strict=True) != path:
        raise ValueError("relocated archive file")
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("invalid archive file")
    return path


def validate_archive(root, pin):
    root = root.absolute()
    manifest = root / "sha256-manifest.json"
    raw = bounded_bytes(archive_file(manifest))
    if digest(raw) != pin:
        raise ValueError("manifest digest mismatch")
    entries = json.loads(raw, object_pairs_hook=unique_fields)
    if not isinstance(entries, dict) or not 1 <= len(entries) <= ARCHIVE_ENTRY_LIMIT:
        raise ValueError("empty or malformed manifest")
    if pin == CONTROL_02_PIN and len(entries) != CONTROL_02_ENTRIES:
        raise ValueError("incomplete Control-02 manifest")
    remaining_bytes = ARCHIVE_BYTE_LIMIT
    for name, expected in entries.items():
        if not isinstance(expected, str) or not re.fullmatch("[a-f0-9]{64}", expected):
            raise ValueError("invalid archive digest")
        if (not name or "\\" in name or PureWindowsPath(name).drive or ":" in name
                or any(part in ("", ".", "..") for part in name.split("/"))):
            raise ValueError("invalid manifest path")
        checksum = hashlib.sha256()
        with archive_file(root / name).open("rb") as stream:
            while chunk := stream.read(min(64 * 1024, remaining_bytes + 1)):
                remaining_bytes -= len(chunk)
                if remaining_bytes < 0:
                    raise ValueError("archive exceeds cumulative byte limit")
                checksum.update(chunk)
        if checksum.hexdigest() != expected:
            raise ValueError("manifest entry mismatch")
    return len(entries)


def log_fields(raw):
    line = raw.decode("utf-8").rstrip("\r\n")
    pos = 0
    pairs = []
    while pos < len(line):
        match = FIELD.match(line, pos)
        if not match:
            raise ValueError("invalid log fields")
        key, value = match.groups()
        pairs.append((key, json.loads(value) if value.startswith('"') else value))
        pos = match.end()
    fields = unique_fields(pairs)
    if not all(fields.get(key) for key in ("timestamp", "level", "run", "message")):
        raise ValueError("incomplete log record")
    return fields


def same_workspace(value, project):
    return isinstance(value, str) and Path(value).is_absolute() and Path(os.path.normpath(value)) == project


class Capture:
    """Only the reader thread mutates counters. Snapshot under the same lock."""
    def __init__(self, project, title):
        self.project = project
        self.title = title
        self.run_id = None
        self.counts = Counter()
        self.markers = {}
        self.duplicates = 0
        self.eof = False
        self.read_error = False
        self.lock = threading.Lock()

    def classify(self, line):
        try:
            fields = log_fields(line)
        except (ValueError, UnicodeError):
            return "malformed"
        if not same_workspace(fields.get("directory"), self.project):
            return "unrelated"
        message = fields["message"]
        if message == "creating instance" and self.run_id is None:
            self.run_id = fields["run"]
        if self.run_id != fields["run"]:
            return "unrelated"
        if message == "creating instance":
            stage = "workspace_initialization"
        elif message == "created" and fields.get("title") == self.title and fields.get("id"):
            stage = "title_matched_creation"
        else:
            return "unrelated"
        if stage in self.markers:
            self.duplicates += 1
        else:
            self.markers[stage] = time.monotonic_ns()
        return "matched"

    def drain(self, stream):
        try:
            while line := stream.readline(LINE_LIMIT + 1):
                if len(line) > LINE_LIMIT:
                    with self.lock:
                        self.counts["oversized"] += 1
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(LINE_LIMIT + 1)
                    continue
                elif not line.endswith(b"\n"):
                    kind = "partial"
                else:
                    with self.lock:
                        self.counts[self.classify(line)] += 1
                    continue
                with self.lock:
                    self.counts[kind] += 1
            with self.lock:
                self.eof = True
        except (OSError, ValueError):
            with self.lock:
                self.read_error = True

    def snapshot(self, reader_alive):
        with self.lock:
            return {"lines": dict(self.counts), "duplicate_markers": self.duplicates,
                    "reader_monotonic_ns": dict(self.markers), "stderr_eof": self.eof,
                    "read_error": self.read_error, "drain_timeout": reader_alive}


def remaining(end):
    seconds = end - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("deadline")
    return seconds


class RequestTimeout(TimeoutError):
    def __init__(self, stage):
        super().__init__(stage)
        self.stage = stage


class Client:
    """Bound the caller's elapsed time even if the response trickles below the socket timeout."""
    def __init__(self, port, password):
        self.port = port
        self.auth = "Basic " + base64.b64encode(("opencode:" + password).encode()).decode()

    def request(self, method, route, data, end, *, trace=None):
        try:
            timeout = remaining(end)
        except TimeoutError:
            raise RequestTimeout("before_connect") from None
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        result = Queue(maxsize=1)
        stage = "connecting"
        milestones = {"connect_started_ns": time.monotonic_ns()}
        cancelled = threading.Event()
        active_socket = []

        def exchange():
            nonlocal stage
            try:
                conn.connect()
                active_socket.append(conn.sock)
                if cancelled.is_set():
                    return
                stage = "sending_request"
                milestones["connected_ns"] = time.monotonic_ns()
                conn.request(method, route, None if data is None else json.dumps(data).encode(),
                             {"Authorization": self.auth, "Content-Type": "application/json"})
                stage = "awaiting_headers"
                milestones["request_sent_ns"] = time.monotonic_ns()
                with conn.getresponse() as response:
                    stage = "reading_body"
                    milestones["headers_received_ns"] = time.monotonic_ns()
                    milestones["status_class"] = response.status // 100
                    if not 200 <= response.status < 300:
                        raise ValueError("http status")
                    body = response.read(BODY_LIMIT + 1)
                    if len(body) > BODY_LIMIT:
                        raise ValueError("response size")
                    value = json.loads(body) if body.strip() else None
                result.put((value, None))
            except Exception as error:
                result.put((None, error))
            finally:
                conn.close()

        threading.Thread(target=exchange, daemon=True).start()
        try:
            value, error = result.get(timeout=remaining(end))
            if error:
                raise error
            remaining(end)
            return value
        except (TimeoutError, Empty):
            timed_out_at = stage
            cancelled.set()
            if active_socket:
                try:
                    active_socket[0].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            raise RequestTimeout(timed_out_at) from None
        finally:
            if trace is not None:
                trace.update(milestones.copy())


def matching_session(doc, project, title):
    if not isinstance(doc, dict) or doc.get("title") != title or not same_workspace(doc.get("directory"), project):
        return None
    sid = doc.get("id")
    return sid if isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", sid) else None


def lookup_outcome(rows, project, title):
    if not isinstance(rows, list) or len(rows) > 4:
        return "malformed"
    visible = False
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("title"), str)
                or not isinstance(row.get("directory"), str) or not Path(row["directory"]).is_absolute()
                or not isinstance(row.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", row["id"])):
            return "malformed"
        if matching_session(row, project, title):
            visible = True
    return "visible" if visible else "no_match"


def create_session(request, project, title, record, end):
    record["create_requested_ns"] = time.monotonic_ns()
    route = "/session?" + urlencode({"directory": str(project)})
    record["create_transport"] = {}
    try:
        doc = request("POST", route, {"title": title}, min(end, time.monotonic() + 8),
                      trace=record["create_transport"])
    except RequestTimeout as error:
        record["create_timeout"] = error.stage
        record["create_timeout_ns"] = time.monotonic_ns()
        cutoff = min(end, time.monotonic() + 10)
        time.sleep(max(0, cutoff - time.monotonic() - 2))
        try:
            route = "/session?" + urlencode({"directory": str(project), "search": title, "limit": 4})
            record["lookup_requested_ns"] = time.monotonic_ns()
            record["lookup_transport"] = {}
            rows = request("GET", route, None, min(cutoff, time.monotonic() + 2),
                           trace=record["lookup_transport"])
            record["lookup"] = lookup_outcome(rows, project, title)
        except RequestTimeout as error:
            record["lookup_timeout"] = error.stage
            record["lookup"] = "timeout"
        except TimeoutError:
            record["lookup"] = "timeout"
        except (json.JSONDecodeError, UnicodeDecodeError):
            record["lookup"] = "malformed"
        except (ValueError, OSError, http.client.HTTPException):
            record["lookup"] = "unavailable"
        record["lookup_ended_ns"] = time.monotonic_ns()
        time.sleep(max(0, cutoff - time.monotonic()))
        return None
    sid = matching_session(doc, project, title)
    if sid is None:
        raise ValueError("invalid session response")
    record["session_received_ns"] = time.monotonic_ns()
    return sid


def runner_attempt(part, project):
    """Recognize invocation before interpreting tool status, stdout or runner JSON."""
    if part.get("type") != "tool" or part.get("tool") != "bash":
        return None
    state = part.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("input"), dict):
        raise ValueError("ambiguous shell part")
    command = state["input"].get("command")
    if not isinstance(command, str):
        raise ValueError("missing shell command")
    if not any(leaf in command.casefold() for leaf in ("run.sh", "run.ps1")):
        return None
    if any(char in command for char in "\r\n;|$\x60<>"):
        raise ValueError("ambiguous runner command")
    args = shlex.split(command)
    if args[:1] == ["&"]:
        args = args[1:]
    if args and PureWindowsPath(args[0]).name.lower() in ("bash", "bash.exe"):
        args = args[1:]
    if (len(args) < 2 or len(args) % 2 or "&" in args
            or (project / args[0]).resolve() != project / ".workflow/run.sh"):
        raise ValueError("unrecognized runner invocation")
    return args[1], unique_fields(zip(args[2::2], args[3::2]))


def terminal(part):
    state = part["state"]
    if state.get("status") != "completed" or not isinstance(state.get("output"), str):
        raise ValueError("unfinished result")
    times = state.get("time")
    if (not isinstance(times, dict) or any(type(times.get(k)) is not int for k in ("start", "end"))
            or not 0 <= times["start"] <= times["end"]):
        raise ValueError("invalid tool timing")
    text = "\n".join(line for line in state["output"].splitlines() if not line.startswith("[JOB] "))
    doc = json.loads(text, object_pairs_hook=unique_fields)
    if not isinstance(doc, dict) or type(doc.get("ok")) is not bool or not isinstance(doc.get("meta"), dict):
        raise ValueError("invalid terminal envelope")
    return doc, times


def hashed(**fields):
    return {key: digest(value.encode()) for key, value in fields.items()}


class Transcript:
    def __init__(self, native_id, project):
        self.native_id = native_id
        self.project = project
        self.pending_shell = set()
        self.seen = {}
        self.invalid = False
        self.dispatch_observed = False

    def collect(self, messages):
        """Polls are snapshots. A new part is an attempt even when its output is lost."""
        rows = []
        current = set()
        if not isinstance(messages, list):
            raise ValueError("invalid messages")
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
                raise ValueError("invalid native message")
            info = message.get("info")
            if not isinstance(info, dict) or info.get("sessionID") != self.native_id:
                raise ValueError("foreign native message")
            for part in message["parts"]:
                if not isinstance(part, dict) or part.get("sessionID") != self.native_id:
                    raise ValueError("foreign native part")
                part_id = part.get("id")
                if part.get("type") == "tool" and part.get("tool") == "bash":
                    if not isinstance(part_id, str) or not part_id:
                        raise ValueError("missing native shell part identity")
                    state = part.get("state")
                    inputs = state.get("input") if isinstance(state, dict) else None
                    if (isinstance(state, dict) and state.get("status") == "pending"
                            and isinstance(inputs, dict) and not inputs.get("command")):
                        self.pending_shell.add(part_id)
                        continue
                    self.pending_shell.discard(part_id)
                attempt = runner_attempt(part, self.project)
                if attempt is None:
                    continue
                action, flags = attempt
                if part_id in current:
                    raise ValueError("repeated native part")
                signature = (action, flags)
                if self.seen.setdefault(part_id, signature) != signature or len(self.seen) > 3:
                    raise ValueError("ambiguous or extra invocation")
                current.add(part_id)
                if action == "explore":
                    self.dispatch_observed = True
                rows.append((action, flags, part))
        return rows, current

    def inspect(self, messages, durable):
        if self.invalid:
            return None
        try:
            rows, current = self.collect(messages)
        except (ValueError, OSError, RuntimeError):
            self.invalid = True
            return None
        if (self.pending_shell or current != self.seen.keys() or [action for action, _, _ in rows]
                != ["delegate-select", "explore", "delegate-ack"]):
            return None
        if not isinstance(durable, dict) or durable.get("phase") != "collected" or durable.get("command") != "explore":
            return None
        if not same_workspace(durable.get("root"), self.project):
            return None
        sid, gen, job, receipt = (durable.get(k) for k in ("session", "generation", "job_id", "receipt"))
        if not all(isinstance(v, str) and v.strip() for v in (sid, gen, job, receipt)):
            return None
        events = []
        last_end = -1
        for action, flags, part in rows:
            try:
                doc, times = terminal(part)
            except (ValueError, RecursionError):
                return None
            if flags.get("--session") != sid or doc.get("session_id", sid) != sid or times["start"] < last_end:
                return None
            allowed = {"--session", "--main-host", "--task"}
            if action == "explore":
                allowed.update(("--request", "--task-file"))
            if set(flags) - allowed or flags.get("--main-host", "opencode") != "opencode":
                return None
            last_end = times["end"]
            meta = doc["meta"]
            inputs = hashed(session=flags["--session"])
            outputs = hashed(session=doc["session_id"]) if "session_id" in doc else {}
            outcome = "success"
            if action == "delegate-select":
                if (flags.get("--task") != "explore" or not doc["ok"] or meta.get("phase") != "selected"
                        or meta.get("generation") != gen or meta.get("command") != "explore"):
                    return None
                outputs.update(hashed(generation=meta["generation"]))
            elif action == "explore":
                owner = meta.get("delegation")
                if (flags.get("--request") != gen or meta.get("job_id") != job or not isinstance(owner, dict)
                        or owner.get("command") != "explore" or owner.get("generation") != gen
                        or owner.get("receipt") != receipt or (doc.get("error_type") or meta.get("error_type"))
                        in ("cleanup_pending", "cancellation_pending")):
                    return None
                inputs.update(hashed(generation=flags["--request"]))
                outputs.update(hashed(generation=owner["generation"], receipt=owner["receipt"], job_id=meta["job_id"]))
                outcome = "terminal_success" if doc["ok"] else "owned_terminal_error"
            else:
                if (flags.get("--task") != receipt or not doc["ok"]
                        or meta.get("phase") != "collected" or meta.get("job_id") != job):
                    return None
                inputs.update(hashed(receipt=flags["--task"]))
                outputs.update(hashed(job_id=meta["job_id"]))
            shell_meta = part["state"].get("metadata")
            exit_code = shell_meta.get("exit") if isinstance(shell_meta, dict) else None
            event = {"action": action, "part_sha256": digest(part["id"].encode()), "shell_status": "completed",
                     "exit_code": exit_code if type(exit_code) is int else None, "outcome": outcome,
                     "native_start_ms": times["start"], "native_end_ms": times["end"],
                     "input_sha256": inputs, "output_sha256": outputs}
            if action in ("delegate-select", "delegate-ack"):
                event["output_phase"] = meta["phase"]
            events.append(event)
        return {"native_session_sha256": digest(self.native_id.encode()), "events": events,
                "durable": {"phase": "collected", "binding_sha256": hashed(session=sid, generation=gen,
                                                                         job_id=job, receipt=receipt)}}


def assistant_completion(messages, ack_end_ms):
    """Compare timestamps from the native clock and identify the final message."""
    if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
        return None
    info = messages[-1].get("info")
    if (not isinstance(info, dict) or info.get("role") != "assistant" or info.get("finish") != "stop"
            or info.get("error") or not isinstance(info.get("time"), dict)):
        return None
    completed = info["time"].get("completed")
    message_id = info.get("id")
    if type(completed) is not int or completed < ack_end_ms or not isinstance(message_id, str) or not message_id:
        return None
    return {"assistant_message_sha256": digest(message_id.encode()), "native_completed_ms": completed}


def idle_observation(status, sid):
    if not isinstance(status, dict) or any(
        not isinstance(item, dict) or item.get("type") not in ("busy", "retry", "idle") for item in status.values()
    ):
        return None
    if sid not in status:
        return "absent_from_status_map"
    if status[sid] == {"type": "idle"}:
        return "explicit_idle"
    return None


def canonical(path, *, directory=False, optional=False):
    if path.resolve() != path:
        raise ValueError("relocated staged input")
    if optional and not path.exists():
        return path
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError("invalid staged input type")
    return path


def inspect_cell(cell, entry_limit=CELL_ENTRY_LIMIT, depth_limit=CELL_DEPTH_LIMIT):
    """Inspect pre-existing metadata while the operator has exclusive use of this cell."""
    if not cell.is_absolute() or ".." in cell.parts:
        raise ValueError("expected a canonical cell path")

    def checked_mode(path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("linked staging entry")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("shared staging file")
        return info.st_mode

    for directory in (*reversed(cell.parents), cell):
        if not stat.S_ISDIR(checked_mode(directory)):
            raise ValueError("invalid staging ancestry")
    pending = [(cell, 0)]
    count = 1
    while pending:
        directory, depth = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > entry_limit or depth + 1 > depth_limit:
                    raise ValueError("staging inspection limit")
                path = Path(entry.path)
                mode = checked_mode(path)
                if stat.S_ISDIR(mode):
                    pending.append((path, depth + 1))
                elif not stat.S_ISREG(mode):
                    raise ValueError("special staging entry")


def staged_inputs(cell, candidate, expected_host_version=EXPECTED_HOST_VERSION):
    """Run with the candidate interpreter against the disposable install's own bindings."""
    if expected_host_version not in CONTROL_HOST_VERSIONS:
        raise ValueError("unsupported Control host version")
    project = cell / "project"
    workflow = project / ".workflow"
    if cell.name != "fresh" or cell.parent.name != "opencode" or not re.fullmatch("[a-f0-9]{40}", candidate):
        raise ValueError("expected a fresh OpenCode cell and full candidate SHA")
    inspect_cell(cell)
    canonical(workflow / "data/sessions", directory=True, optional=True)
    for item in ("setup.json", "project/.workflow/run.sh", "project/.workflow/config.json",
                 "h/.config/opencode/plugins/exoma.js", "h/.config/opencode/exoma/hooks/opencode-host.py"):
        canonical(cell / item)
    for directory in profile_paths(cell).values():
        canonical(directory, directory=True, optional=True)
    setup = object_file(cell / "setup.json")
    if (setup.get("candidate"), setup.get("host"), setup.get("mode"), setup.get("setup"),
            setup.get("host_version"), setup.get("host_version_exit")) != (
            candidate, "opencode", "fresh", "pass", expected_host_version, 0):
        raise ValueError("incompatible setup")
    if any((cell / name).exists() for name in ("lost-output-diagnostic-control.json",
                                               "session-create-trace.json", "w3-control.json")):
        raise ValueError("cell has already been used")
    if any((workflow / "data/sessions").glob("*")):
        raise ValueError("fresh workspace already has sessions")
    runner = bounded_bytes(workflow / "run.sh")
    lines = runner.decode().splitlines()
    config = object_file(workflow / "config.json")
    exe = Path(sys.executable).absolute()
    plugin_dir = cell / "h/.config/opencode"
    plugin = bounded_bytes(plugin_dir / "plugins/exoma.js").decode("utf-8")
    host = Path(setup["host_binary"])
    site = Path(metadata.distribution("exoma").locate_file("")).resolve(strict=True)
    if not site.is_relative_to(Path(sys.prefix).resolve()):
        raise ValueError("candidate distribution outside interpreter prefix")
    installs = site.glob("exoma-*.dist-info")
    install = next(installs, None)
    if install is None or next(installs, None) is not None:
        raise ValueError("missing or ambiguous Exoma installation")
    vcs = object_file(canonical(install / "direct_url.json")).get("vcs_info")
    if not isinstance(vcs, dict) or vcs.get("commit_id") != candidate:
        raise ValueError("candidate commit mismatch")
    work_dirs = [line for line in lines if line.startswith("ARGS+=(") and "--work-dir" in line]
    if not (digest(runner) == setup["runner_sha256"]
            and same_workspace(config["project"]["root"], project)
            and Path(config["runtime"]["python"]).absolute() == exe
            and lines[-1:] == ['exec "%s" "${ARGS[@]}"' % exe.as_posix()]
            and work_dirs == [f'ARGS+=(--session "$SESSION" --work-dir "{project.as_posix()}" --pretty)']
            and host.is_absolute() and host.is_file()):
        raise ValueError("candidate or runner binding mismatch")
    for key, expected in (("PYTHON", exe), ("HOOK", plugin_dir / "exoma/hooks/opencode-host.py")):
        values = re.findall(r"^const " + key + r" = (.+)$", plugin, re.M)
        if len(values) != 1 or Path(json.loads(values[0])).absolute() != expected or not expected.is_file():
            raise ValueError("plugin binding mismatch")
    return host, project


def profile_paths(cell):
    home = cell / "h"
    paths = {"HOME": ".", "USERPROFILE": ".", "AGENT_HOME": ".", "CODEX_HOME": ".codex",
             "CLAUDE_CONFIG_DIR": ".claude", "XDG_CONFIG_HOME": ".config", "XDG_DATA_HOME": "data",
             "XDG_STATE_HOME": "state", "XDG_CACHE_HOME": "cache", "EXOMA_HOME": "exoma",
             "LOCALAPPDATA": "local", "APPDATA": "roaming", "TEMP": "tmp", "TMP": "tmp"}
    return {key: home / suffix for key, suffix in paths.items()}


def profile_environment(cell):
    keys = ("PATH PATHEXT SYSTEMROOT SYSTEMDRIVE WINDIR COMSPEC PROGRAMFILES PROGRAMFILES(X86) PROGRAMW6432 "
            "NUMBER_OF_PROCESSORS PROCESSOR_ARCHITECTURE OS NVM_HOME NVM_SYMLINK").split()
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update({key: str(path) for key, path in profile_paths(cell).items()})
    env.update(PATH=str(Path(sys.executable).parent) + os.pathsep + env["PATH"],
               OPENCODE_SERVER_PASSWORD=secrets.token_urlsafe(30), PYTHONDONTWRITEBYTECODE="1",
               GIT_TERMINAL_PROMPT="0", PYTHONUTF8="1")
    return env


# The bootstrap cannot start a target until its parent verifies job membership.
# A closed pipe before release is a normal refusal (including parent death).
PARKED = (
    "import os,subprocess,sys\n"
    "if sys.stdin.buffer.readline() != (str(os.getpid())+'\\n').encode(): sys.exit(125)\n"
    "sys.exit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))"
)


class HostScope:
    def __init__(self):
        from exoma._update_worker import ChildJob, Win32
        self.job = ChildJob(Win32(), "W3-" + secrets.token_hex(16))
        self.processes = []
        self.enrolled = []

    def enroll(self, proc):
        import ctypes
        from ctypes import wintypes
        member = wintypes.BOOL()
        api = self.job.api
        api.require(api.k.AssignProcessToJobObject(self.job.handle, int(proc._handle)))
        api.require(api.k.IsProcessInJob(int(proc._handle), self.job.handle, ctypes.byref(member)))
        if not member.value:
            raise OSError("job membership unconfirmed")

    def launch(self, argv, **kwargs):
        # A Windows venv redirector can spawn the real interpreter before assignment.
        # Use the base interpreter and bind the release message to its exact OS pid.
        proc = subprocess.Popen([sys._base_executable, "-I", "-S", "-c", PARKED, *argv], stdin=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
        self.processes.append(proc)
        try:
            self.enroll(proc)
            self.enrolled.append(proc)
            proc.stdin.write(f"{proc.pid}\n".encode())
        finally:
            proc.stdin.close()
            proc.stdin = None
        return proc

    def close(self, end):
        observed = {"scope": "windows_job",
                    "membership_verified": bool(self.enrolled) and self.enrolled == self.processes,
                    "termination_requested": False, "active_members": None, "launcher_exited": False,
                    "containment_confirmed": False}
        try:
            active = self.job.active()
            if active:
                self.job.api.require(self.job.api.k.TerminateJobObject(self.job.handle, 1))
                observed["termination_requested"] = True
            while active:
                time.sleep(min(.02, remaining(end)))
                active = self.job.active()
            observed["active_members"] = active
            for proc in self.processes:
                proc.wait(timeout=remaining(end))
            observed["launcher_exited"] = bool(self.processes)
            observed["containment_confirmed"] = (observed["membership_verified"] and
                                                 observed["active_members"] == 0 and observed["launcher_exited"])
        except (OSError, TimeoutError, subprocess.TimeoutExpired):
            observed["cleanup_error"] = True
        finally:
            self.job.close()
        return observed


def candidate_reader():
    from exoma.core.jobs import diagnostics
    expected = metadata.distribution("exoma").locate_file("exoma/core/jobs/diagnostics.py")
    if Path(diagnostics.__file__).resolve() != expected.resolve(strict=True):
        raise ValueError("snapshot reader outside candidate")
    return diagnostics._read


def workspace_snapshot(project, reader):
    sessions = project / ".workflow/data/sessions"
    canonical(sessions, directory=True, optional=True)
    paths = list(sessions.glob("*/runtime/delegation.json"))
    if len(paths) != 1:
        return {}, "missing_or_ambiguous"
    path = canonical(paths[0])
    data = reader(path)
    if not isinstance(data, dict) or data.get("unavailable") == "not_object":
        return {}, "malformed"
    if data.get("unavailable") == "size_limit":
        return {}, "oversized"
    if data.get("session") != path.parent.parent.name:
        return {}, "unavailable"
    return data, "observed"


def observe(client, project, title, report, end, reader=None):
    health_end = min(end, time.monotonic() + 30)
    while True:
        remaining(health_end)
        try:
            health = client.request("GET", "/global/health", None, min(health_end, time.monotonic() + 2))
            if isinstance(health, dict) and health.get("healthy") is True:
                break
        except (OSError, ValueError, http.client.HTTPException):
            pass
        time.sleep(min(.25, remaining(health_end)))
    report["health_observed"] = True
    report["health_observed_ns"] = time.monotonic_ns()
    # Global health does not await OpenCode's project bootstrap. GET /path does.
    route = "/path?" + urlencode({"directory": str(project)})
    report["readiness_transport"] = {}
    report["readiness_requested_ns"] = time.monotonic_ns()
    try:
        workspace = client.request("GET", route, None, min(end, time.monotonic() + 30),
                                   trace=report["readiness_transport"])
    except RequestTimeout as error:
        report["readiness"] = "timeout"
        report["readiness_timeout"] = error.stage
        return
    except (ValueError, OSError, http.client.HTTPException):
        report["readiness"] = "unavailable"
        return
    finally:
        report["readiness_ended_ns"] = time.monotonic_ns()
    if not isinstance(workspace, dict) or not same_workspace(workspace.get("directory"), project):
        report["readiness"] = "invalid_workspace"
        return
    report["readiness"] = "verified"
    sid = create_session(client.request, project, title, report, end)
    if sid is None:
        return
    endpoint = "/session/" + sid
    report["prompt_attempt_ns"] = time.monotonic_ns()
    client.request("POST", endpoint + "/prompt_async", {"parts": [{"type": "text", "text": PROMPT}]},
                   min(end, time.monotonic() + 8))
    report["explore_accepted"] = True
    report["prompt_accepted_ns"] = time.monotonic_ns()
    poll_end = min(end, time.monotonic() + 210)
    transcript = Transcript(sid, project)
    while True:
        messages = client.request("GET", endpoint + "/message", None, min(poll_end, time.monotonic() + 8))
        state, report["snapshot_status"] = workspace_snapshot(project, reader or candidate_reader())
        evidence = transcript.inspect(messages, state)
        report["dispatch_observed"] = transcript.dispatch_observed
        report["runner_attempts_observed"] = len(transcript.seen)
        if transcript.invalid:
            report["transcript_ambiguous"] = True
            return
        completion = assistant_completion(messages, evidence["events"][-1]["native_end_ms"]) if evidence else None
        if completion:
            status = client.request("GET", "/session/status", None, min(poll_end, time.monotonic() + 8))
            idle_ns = time.monotonic_ns()
            idle = idle_observation(status, sid)
            if idle:
                final = client.request("GET", endpoint + "/message", None, min(poll_end, time.monotonic() + 8))
                reread_ns = time.monotonic_ns()
                state, report["snapshot_status"] = workspace_snapshot(project, reader or candidate_reader())
                confirmed = transcript.inspect(final, state)
                report["runner_attempts_observed"] = len(transcript.seen)
                if transcript.invalid:
                    report["transcript_ambiguous"] = True
                    return
                final_completion = assistant_completion(final, evidence["events"][-1]["native_end_ms"])
                if confirmed == evidence and final_completion == completion:
                    confirmed["settlement"] = {**completion, "idle_status": idle,
                                               "idle_observed_monotonic_ns": idle_ns,
                                               "reread_native_completed_ms": final_completion["native_completed_ms"],
                                               "final_reread_monotonic_ns": reread_ns}
                    report["foreground"] = confirmed
                    report["original_job_acknowledged"] = True
                    return
        time.sleep(min(.4, remaining(poll_end)))


def control(cell, candidate, expected_host_version=EXPECTED_HOST_VERSION):
    if sys.platform != "win32":
        raise ValueError("native Control requires Windows Job Object containment")
    host, project = staged_inputs(cell, candidate, expected_host_version)
    snapshot_reader = candidate_reader()
    env = profile_environment(cell)
    title = "w3-control-" + secrets.token_hex(16)
    capture = Capture(project, title)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with (cell / "w3-control.json").open("x", encoding="utf-8") as stream:
        start = time.monotonic()
        end = start + 270
        report = {"candidate": candidate, "maintainer_tool_sha256": digest(Path(__file__).read_bytes()),
                  "expected_host_version": expected_host_version, "version_check": "not_run",
                  "started_unix_ns": time.time_ns(),
                  "original_job_acknowledged": False, "cleanup_confirmed": False}
        scope = reader = proc = version = None
        try:
            scope = HostScope()
            report["version_check"] = "failed"
            version = scope.launch([str(host), "--version"], cwd=project, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            output, _ = version.communicate(timeout=min(8, remaining(end - 10)))
            if version.returncode or not re.fullmatch(rb"[0-9]{1,8}\.[0-9]{1,8}\.[0-9]{1,8}", output.strip()):
                raise ValueError("host_version")
            report["observed_host_version"] = output.strip().decode("ascii")
            if report["observed_host_version"] != expected_host_version:
                report["version_check"] = "mismatch"
                raise ValueError("host_version")
            report["version_check"] = "verified"
            proc = scope.launch([str(host), "serve", "--hostname", "127.0.0.1", "--port", str(port),
                                 "--print-logs", "--log-level", "INFO"], cwd=project, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            reader = threading.Thread(target=capture.drain, args=(proc.stderr,), daemon=True)
            reader.start()
            observe(Client(port, env["OPENCODE_SERVER_PASSWORD"]), project, title, report, end - 10, snapshot_reader)
        except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError) as error:
            report["error"] = type(error).__name__
        finally:
            if scope is not None:
                report["cleanup"] = scope.close(end)
            if reader is not None:
                reader.join(max(0, end - time.monotonic()))
            report["capture"] = capture.snapshot(reader is not None and reader.is_alive())
            if proc is not None and not report["capture"]["drain_timeout"]:
                proc.stderr.close()
            report["cleanup_confirmed"] = report.get("cleanup", {}).get("containment_confirmed", False)
            if version is not None and version.poll() is not None and version.stdout:
                version.stdout.close()
            report["elapsed_seconds"] = round(time.monotonic() - start, 3)
            json.dump(report, stream, indent=2)
            stream.write("\n")
    return (report["original_job_acknowledged"] and report["cleanup_confirmed"]
            and report["capture"]["stderr_eof"] and not report["capture"]["read_error"]
            and not report["capture"]["drain_timeout"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("validate-archive", help="check the manifest pin and every listed file")
    archive.add_argument("directory", type=Path)
    archive.add_argument("--expected-manifest-sha256", default=CONTROL_02_PIN)
    fresh = commands.add_parser("control", help="run one Windows Control with the staged candidate's Python")
    fresh.add_argument("cell", type=Path, help="unused, prepared opencode/fresh directory")
    fresh.add_argument("--candidate", required=True, help="full installed Exoma commit SHA")
    fresh.add_argument("--expected-host-version", choices=CONTROL_HOST_VERSIONS, default=EXPECTED_HOST_VERSION,
                       help="exact OpenCode version required at setup and launch (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-archive":
            count = validate_archive(args.directory, args.expected_manifest_sha256)
            print(f"{count} archive entries match the pinned manifest")
            return 0
        return 0 if control(args.cell.absolute(), args.candidate, args.expected_host_version) else 1
    except (OSError, ValueError, KeyError, TypeError, RecursionError, metadata.PackageNotFoundError) as error:
        print(f"Control precondition or archive check failed ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())


