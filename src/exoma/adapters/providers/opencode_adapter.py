import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
import subprocess

from exoma.config.settings import (
    DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER_AGENT,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    OPENCODE_COMMAND,
)
from exoma.core.evidence.contract import make_error as _contract_make_error
from exoma.core.evidence.contract import make_ok as _contract_make_ok
from exoma.core.workspace.workspace_paths import atomic_write_text, detect_project_root
from exoma.utils import osutil
from exoma.utils.redact import redact, redact_value
from exoma.utils.parser import ensure_text, first_non_empty

from exoma.adapters.shared.child_env import project_scoped_env
from exoma.adapters.shared.usage import merge_usage, normalize_usage

# OpenCode's own output shapes. They belong to the adapter, not to a shared parser:
# another provider names its sessions differently and prefixes its logs differently.
_SESSION_ID_PATTERNS = (
    r"(?:session\.id=|service=session\s+id=)(ses_[A-Za-z0-9]+)",
    r"\bid=(ses_[A-Za-z0-9]+)",  # generic key=value log
    r"\b(ses_[A-Za-z0-9]{6,})\b",  # bare session token, last resort
)
_LOG_LINE = re.compile(r"^(TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\s+\d{4}-\d{2}-\d{2}T")
_QUOTED_LINE = re.compile(r"^>\s+")

# OpenCode reports token counts under exactly one condition: `--format json`. In its
# default (formatted) mode the run prints prose and log lines and no usage at all, which
# is why every row this adapter wrote before carried a chars//4 estimate.
#
# The stream it emits instead is one JSON object per line, each with a top-level `type`
# and a `part` payload:
#   {"type":"text","part":{"type":"text","text":"DONE",...}}
#   {"type":"step_finish","part":{"tokens":{"total":30198,"input":14448,"output":6,
#                                 "reasoning":0,"cache":{"write":0,"read":15744}},...}}
_JSON_FORMAT_ARGS = ("--format", "json")


def _as_int(value: object) -> int | None:
    """A reported count, or None when it is not one. Bools are not counts.

    A negative count is not one either. Nothing can be billed minus-twelve tokens, so a
    negative here means the field was malformed, and carrying it through would let one
    bad step silently subtract from the rest of the run's total — a smaller number with
    nothing to mark it as wrong. Unreported is the honest reading.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _sum_reported(*values: "int | None") -> int | None:
    """Add the counts that were actually reported; None when none of them were.

    An absent field is unreported, not zero — so it neither contributes to the sum nor
    drags a reported sibling down to None.
    """
    present = [value for value in values if value is not None]
    return sum(present) if present else None
# The event types OpenCode's `--format json` stream is made of. Named rather than inferred
# from "the line parses as JSON with a type field", because the fallback below has to tell
# this stream apart from an evidence body that merely quotes a JSON object — and an
# [EVIDENCE] block that happens to contain one is not a machine stream to be thrown away.
_EVENT_TYPES = frozenset(
    {
        "step_start",
        "step_finish",
        "text",
        "reasoning",
        "tool_use",
        "tool",
        "file",
        "error",
    }
)

# Upper bound on captured stdout/stderr per stream (~4MB of text). A well-behaved evidence
# run is a few hundred KB; this only clamps a runaway/pathological process so it cannot
# grow the in-memory buffer without limit. The tail is kept (evidence blocks live at the end).
MAX_CAPTURE_CHARS = 4_000_000

# Substrings that signal opencode refused a path rather than a real crash.
_PERMISSION_SIGNS = (
    "permission denied",
    "eacces",
    "not permitted",
    "access is denied",
    "outside",
    "forbidden",
)

_RATE_LIMIT_SIGNS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "429",
    "too many requests",
    "quota",
    "usage limit",
    "insufficient credits",
    "out of credits",
    "overloaded",
    "capacity",
)


def _is_rate_limited(*texts: str) -> bool:
    """True when any text carries a provider quota/rate-limit signature."""
    blob = " ".join(t.lower() for t in texts if t)
    return any(sign in blob for sign in _RATE_LIMIT_SIGNS)


_STREAM_FAIL_SIGNS = (
    "streaming response failed",
    "stream closed",
    "stream error",
    "connection reset",
    "connection closed",
    "socket hang up",
    "premature close",
    "econnreset",
    "epipe",
)


def _is_stream_failure(*texts: str) -> bool:
    """True when any text carries a dropped-provider-stream signature."""
    blob = " ".join(t.lower() for t in texts if t)
    return any(sign in blob for sign in _STREAM_FAIL_SIGNS)


_ERROR_TAIL_CHARS = 1600


def _error_tail(*texts: str) -> str:
    """The trailing slice of the combined error text — opencode's own terminal error,
    isolated from the agent transcript that precedes it."""
    blob = "\n".join(t for t in texts if t)
    return blob[-_ERROR_TAIL_CHARS:]


_STREAM_FAIL_NEXT_ACTION = (
    "Transient — the provider stream dropped mid-answer, the request itself was fine. "
    "Retry once; if it dies again, split the task into two narrower delegated calls. "
    "Do NOT wait for a quota reset, this is not a limit."
)


_CMD_LINE_LIMIT = 8191
_CMD_LINE_HEADROOM = 400
_CMD_LINE_SIGNS = ("command line is too long", "the input line is too long")

# The prompt travels as a FILE, never as argv. On Windows `opencode` resolves to the npm
# shim `opencode.cmd`, and a .cmd is parsed by cmd.exe — which does not understand the
# `\"` escaping `subprocess` applies. A prompt with an odd run of quotes flips cmd's quote
# state, and whatever follows is shell syntax: `"<value>"` became an input redirect from a
# file named `value` ("The system cannot find the file specified", every e2e_spec call),
# and `x" & echo INJECTED & "` executed the echo. Measured, not theorised: both reproduced
# through a dummy shim. `-f` hands opencode the file; argv carries one static sentence.
_ATTACHED_PROMPT_INSTRUCTION = "Follow the instructions in the attached file exactly."
_PROMPT_FILE_NAME = "opencode-prompt.md"
# What cmd.exe acts on inside a .cmd/.bat invocation. `!` is left out: the shim does not
# enable delayed expansion. Newlines are in because cmd ends the command at one.
_CMD_METACHARS = frozenset('"&|<>^%\r\n')
_CMD_SCRIPT_SUFFIXES = (".cmd", ".bat")
_UNSAFE_NEXT_ACTION = (
    "An argument handed to opencode's .cmd shim contains characters cmd.exe would run as "
    "shell syntax, so the call was refused before spawning. Remove them from "
    "provider_command / provider_agent / model / effort in .workflow/second_agent.json, "
    "or move the project to a path without them."
)


def _argv_meta(args: list[str]) -> dict:
    encoded = "\0".join(str(arg) for arg in args).encode("utf-8", errors="replace")
    return {
        "argv_count": len(args),
        "argv_chars": sum(len(str(arg)) for arg in args),
        "argv_sha256": hashlib.sha256(encoded).hexdigest()[:16],
    }


# Re-exported so call sites inside this module keep addressing `make_error` / `make_ok`
# unqualified. The accounting they carry is shared: see adapters/redaction.py for why it
# cannot live in each adapter.
from exoma.adapters.shared.redaction import (  # noqa: E402
    attach_redactions as _attach_redactions,
    make_error,
    make_ok,
    sanitize_meta as _sanitize_meta,
)


def _too_long_for_cmd(args: list[str]) -> int | None:
    """Total command-line length when it will not fit, else None. Windows only."""
    if not osutil.IS_WINDOWS:
        return None
    total = sum(len(str(a)) + 3 for a in args)  # +3: two quotes and a separator
    return total if total > (_CMD_LINE_LIMIT - _CMD_LINE_HEADROOM) else None


def _cmd_parsing_applies() -> bool:
    """Whether a .cmd/.bat launch goes through cmd.exe here. A named seam, like
    `prompt_builder._argv_limit_enforced`: forging `osutil.IS_WINDOWS` to test this on
    Linux would reroute lock and job code into branches that import msvcrt."""
    return osutil.IS_WINDOWS


def _cmd_shell_hazards(args: list[str]) -> list[dict]:
    """Arguments cmd.exe would interpret, when `args` runs through a .cmd/.bat. Windows only.

    Every argument is checked, the executable path included: an unquoted `C:\\a&b\\x.cmd`
    splits the command just as a prompt would.
    """
    if not _cmd_parsing_applies() or not args:
        return []
    if not str(args[0]).lower().endswith(_CMD_SCRIPT_SUFFIXES):
        return []
    hazards = []
    for index, arg in enumerate(args):
        found = sorted({char for char in str(arg) if char in _CMD_METACHARS})
        if found:
            hazards.append({"argv_index": index, "chars": "".join(found).encode("unicode_escape").decode()})
    return hazards


def _unsafe_command_error(hazards: list[dict], cwd: str | None) -> dict:
    return make_error(
        "unsafe_command_line",
        "refused to run opencode: cmd.exe would interpret characters in its arguments",
        next_action=_UNSAFE_NEXT_ACTION,
        meta={"hazards": hazards, "cwd": cwd, "checked": "pre_spawn"},
    )


def _guess_blocked_paths(stderr: str) -> list[str]:
    """Best-effort pull of path-like tokens from an opencode permission error."""
    import re

    tokens = re.findall(r"(?:[A-Za-z]:[\\/]|/|~)[^\s\"']+", stderr or "")
    seen: set[str] = set()
    return [t for t in tokens if not (t in seen or seen.add(t))][:8]


class OpenCodeAdapter:
    adapter = "opencode"

    def __init__(
        self,
        command: str = OPENCODE_COMMAND,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        on_progress=None,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.no_timeout = timeout_seconds is None or timeout_seconds <= 0
        self.on_progress = on_progress
        self.poll_interval = DEFAULT_POLL_INTERVAL_SECONDS
        self.bootstrap_timeout_seconds = DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS
        # `plan` is the read-only fallback when project config omits a custom agent.
        self.agent = DEFAULT_PROVIDER_AGENT
        # Late-bound from the route, like command/agent. None = send no --variant.
        self.effort: str | None = None
        self.last_call_meta: dict = {}
        self.on_session_created = None

    def _agent_args(self) -> list[str]:
        return ["--agent", self.agent or DEFAULT_PROVIDER_AGENT]

    def _effort_args(self) -> list[str]:
        """`--variant <effort>`, or nothing. Spelling lives in the provider bundle."""
        from exoma.config.providers import effort_args

        return effort_args("opencode", self.effort)

    @staticmethod
    def extract_session_id(text: str) -> str | None:
        """First `ses_*` token OpenCode reports, searched most specific first."""
        body = ensure_text(text)
        for pattern in _SESSION_ID_PATTERNS:
            match = re.search(pattern, body)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _json_events(text: str) -> list[dict]:
        """The `--format json` stream, parsed leniently.

        Lenient because the stream is shared with anything else that reached the same
        pipe: a plugin banner, a log line, a truncation marker this adapter added itself.
        A line that is not a JSON object is skipped rather than treated as a parse
        failure, so one stray line cannot cost the run its answer or its token counts.
        """
        events = []
        for line in ensure_text(text).splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                event = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    @staticmethod
    def clean_output(text: str) -> str:
        """The answer, whichever of OpenCode's two output modes produced it.

        Under `--format json` the answer arrives as `text` events, and everything else on
        the stream — steps, tool calls, file reads, token counts — is not the answer.
        Parts are keyed by id and the last value for an id wins, so a build that streams a
        part in growing pieces yields the finished text rather than every prefix of it.

        The line-stripping fallback still handles bootstrap, which stays on the formatted
        output, and every error path that hands this method a stderr tail. It also covers
        the case worth being careful about: a JSON run that died before emitting a single
        text event. Returning the raw event objects as the answer would put a wall of
        machine output where evidence belongs, so the fallback runs when there was no text
        event to find — not merely when the text it found looks short or empty.
        """
        body = ensure_text(text)
        events = OpenCodeAdapter._json_events(body)
        ordered: list[str] = []
        parts: dict[str, str] = {}
        for event in events:
            if event.get("type") != "text":
                continue
            part = event.get("part")
            if not isinstance(part, dict):
                continue
            value = part.get("text")
            if not isinstance(value, str):
                continue
            key = part.get("id")
            if not isinstance(key, str):
                key = "__anon_%d" % len(ordered)
            if key not in parts:
                ordered.append(key)
            parts[key] = value
        if ordered:
            return "\n".join(parts[key] for key in ordered).strip()

        if any(event.get("type") in _EVENT_TYPES for event in events):
            # An event stream that produced no text produced no answer. Saying so as an
            # empty result is what lets the caller report an empty output; handing back
            # the event objects instead would put a wall of JSON where the evidence
            # contract belongs and read, upstream, as a run that succeeded and returned
            # nonsense.
            return ""

        kept = []
        for line in body.splitlines():
            stripped = line.strip()
            if _LOG_LINE.match(stripped) or _QUOTED_LINE.match(stripped):
                continue
            kept.append(line)
        return "\n".join(kept).strip()

    @staticmethod
    def _step_tokens(tokens: object) -> dict | None:
        """One `step_finish` token object, mapped onto the four counts a usage row holds.

        The mapping is not a rename, because OpenCode disagrees with the shared normaliser
        about what its own numbers contain. Measured against live runs, its `total` is
        `input + output + reasoning + cache.read` — every field a sibling of every other.
        The usage row means the opposite by the same words: cached input is part of input,
        reasoning is part of output, and adding either again bills a token twice. So
        `cache.read` is folded into input and `reasoning` into output HERE, where this
        provider's arithmetic is known, and the shared rule is left alone for the
        providers that already satisfy it.

        A field OpenCode did not report stays None rather than becoming a zero: a missing
        measurement is something telemetry knows how to describe, and an invented zero is
        not.
        """
        if not isinstance(tokens, dict):
            return None
        cache = tokens.get("cache")
        cached = _as_int(cache.get("read")) if isinstance(cache, dict) else None
        fresh_input = _as_int(tokens.get("input"))
        output = _as_int(tokens.get("output"))
        reasoning = _as_int(tokens.get("reasoning"))
        normalized = {
            "input_tokens": _sum_reported(fresh_input, cached),
            "output_tokens": _sum_reported(output, reasoning),
            "reasoning_tokens": reasoning,
            "cached_input_tokens": cached,
        }
        if all(value is None for value in normalized.values()):
            return None
        return normalized

    @staticmethod
    def extract_usage(text: str) -> dict | None:
        """Token counts OpenCode reported, summed across the run.

        Summed rather than last-wins because the counts are per API request, not a running
        total: a two-step run reported 79 output tokens and then 6, and a cumulative
        counter cannot decrease. Each step is a separately billed request, so the run costs
        their sum — the same reading codex applies to its `turn.completed` events.

        The older shape — any object carrying a top-level `usage` member — is still
        accepted. It costs one branch, and it is the shape a future build (or a different
        `opencode` on PATH) would most plausibly emit; dropping it would trade a working
        measurement for nothing.

        None remains a correct answer rather than a failure: a run that reported no usage
        keeps its chars//4 estimate and says `token_source: estimated`, which is the honest
        description of a row nobody measured.
        """
        total: dict | None = None
        for event in OpenCodeAdapter._json_events(text):
            if event.get("type") == "step_finish":
                part = event.get("part")
                if isinstance(part, dict):
                    total = merge_usage(
                        total, OpenCodeAdapter._step_tokens(part.get("tokens"))
                    )
                continue
            if "usage" in event:
                total = merge_usage(total, normalize_usage(event.get("usage")))
        return total

    def _popen_capture(
        self,
        args: list[str],
        env: dict,
        cwd: str | None,
        timeout: int | None,
        phase: str,
    ) -> dict:
        """Run `args`, draining stdout/stderr on threads while the main loop polls.

        Threaded draining allows heartbeat polling and whole-process-tree termination
        on timeout.

        Returns {'returncode', 'stdout', 'stderr', 'timed_out', 'duration_seconds',
                 'pid', 'kill'} — never raises for timeout.
        """
        started = time.monotonic()
        self.last_call_meta = _sanitize_meta(
            {
                "phase": phase,
                **_argv_meta(args),
                "cwd": cwd,
                "timeout_seconds": timeout,
                **(getattr(self, "_env_meta", None) or {}),
            }
        )
        proc = subprocess.Popen(
            args,
            # Closed stdin is not tidiness, it is the difference between a run and a
            # hang. Under `--format json` an inherited (open, idle) stdin leaves opencode
            # blocked immediately after its `init` log line: no model call, no output, no
            # error — it sits there until the timeout kills it. The identical argv with
            # stdin at /dev/null returns in under two seconds. The default-format path
            # never showed this, so the cost of leaving it out lands entirely on the mode
            # this adapter now depends on.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=cwd,
            **osutil.hidden_run_kwargs(),
        )

        chunks: dict[str, list[str]] = {"stdout": [], "stderr": []}
        sizes: dict[str, int] = {"stdout": 0, "stderr": 0}
        truncated: dict[str, bool] = {"stdout": False, "stderr": False}
        last_output = {"at": started}

        def _drain(stream, key: str) -> None:
            # Bounded capture: a runaway or pathological opencode run must not grow the
            # buffer without limit and exhaust RAM. The evidence contract ([EVIDENCE] /
            # [DIGEST]) is emitted at the END, so when we exceed the cap we drop from the
            # FRONT and keep the tail — the part the main_agent actually consumes.
            buf = chunks[key]
            try:
                for line in iter(stream.readline, ""):
                    buf.append(line)
                    sizes[key] += len(line)
                    last_output["at"] = time.monotonic()
                    while sizes[key] > MAX_CAPTURE_CHARS and len(buf) > 1:
                        sizes[key] -= len(buf.pop(0))
                        truncated[key] = True
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        def _joined(key: str) -> str:
            text = "".join(chunks[key])
            if truncated[key]:
                return (
                    f"[...TRUNCATED: earlier {key} dropped, kept last "
                    f"~{MAX_CAPTURE_CHARS // 1000}KB...]\n" + text
                )
            return text

        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, "stdout"), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, "stderr"), daemon=True),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        kill_info: dict = {}
        interval = max(0.2, float(self.poll_interval or 2))
        while True:
            if proc.poll() is not None:
                break
            if timeout and (time.monotonic() - started) >= timeout:
                timed_out = True
                kill_info = osutil.terminate_tree(proc)
                break
            now = time.monotonic()
            self._tick(phase, now - started, now - last_output["at"])
            time.sleep(interval)

        drained = True
        for reader in readers:
            reader.join(timeout=10)
            if reader.is_alive():
                drained = False
        try:
            proc.wait(timeout=10)
        except Exception:
            pass

        meta = {
            "output_complete": drained,
            "returncode": proc.returncode,
            "stdout": _joined("stdout"),
            "stderr": _joined("stderr"),
            "timed_out": timed_out,
            "duration_seconds": round(time.monotonic() - started, 3),
            "idle_seconds": round(time.monotonic() - last_output["at"], 1),
            "pid": proc.pid,
            "kill": kill_info or None,
        }
        self.last_call_meta = _sanitize_meta(
            {
                "phase": phase,
                **_argv_meta(args),
                "cwd": cwd,
                "returncode": meta["returncode"],
                "timed_out": timed_out,
                "duration_seconds": meta["duration_seconds"],
                "timeout_seconds": timeout,
                "kill": kill_info or None,
                "output_complete": drained,
                "idle_seconds": meta["idle_seconds"],
                "stderr_tail": meta["stderr"][-2000:],
                # Read from the captured stdout while it is still raw. Expected to be
                # None on current OpenCode builds; see extract_usage for why that is
                # recorded as an absent measurement rather than as a zero.
                "provider_usage": self.extract_usage(meta["stdout"]),
                # Which environment the child ran with, as counts — so an auth failure
                # after the switch to project-scoped env reads as that, not as a mystery.
                **(getattr(self, "_env_meta", None) or {}),
            }
        )
        return meta

    def _tick(self, phase: str, elapsed: float, idle: float = 0.0) -> None:
        """Emit one liveness beat. Never let a bad callback kill the run.

        A heartbeat only proves this loop is turning; `idle_seconds` distinguishes
        active output from waiting.
        """
        if not self.on_progress:
            return
        try:
            self.on_progress(
                {
                    "phase": phase,
                    "elapsed_seconds": round(elapsed, 1),
                    "idle_seconds": round(idle, 1),
                }
            )
        except Exception:
            pass

    def _effective_timeout(self) -> int | None:
        return None if self.no_timeout else self.timeout_seconds

    def init_session(
        self,
        model: str | None = None,
        work_dir: str | None = None,
        workflow_session_id: str | None = None,
    ) -> tuple[str | None, dict]:
        """Capture a new OpenCode session id by running bootstrap and waiting for completion."""
        command = self._resolve_command()
        args = [command, "run", "Initialize session. Reply READY."]
        args.extend(self._agent_args())
        if model:
            args.extend(["-m", model])
        # Bootstrap carries it too: the session opened here is the one every later call
        # resumes, and a session opened at one effort then resumed at another is a
        # difference nobody would think to look for.
        args.extend(self._effort_args())
        args.extend(["--print-logs", "--log-level", "INFO"])

        env = self._child_env(work_dir)

        cwd = self._resolve_work_dir(work_dir)
        meta: dict = {**_argv_meta(args), "cwd": cwd}
        if workflow_session_id:
            meta["workflow_session_id"] = workflow_session_id

        hazards = _cmd_shell_hazards(args)
        if hazards:
            meta.update(
                {
                    "error": "unsafe_command_line: cmd.exe would interpret characters in the bootstrap arguments",
                    "hazards": hazards,
                    "returncode": 1,
                    "provider_session_id": None,
                }
            )
            return None, _sanitize_meta(meta)

        # Bootstrap uses a separate budget so a hung init cannot wait indefinitely.
        budget = self.bootstrap_timeout_seconds
        if budget is not None and budget <= 0:
            budget = None

        try:
            outcome = self._popen_capture(args, env, cwd, budget, "bootstrap")
        except (OSError, FileNotFoundError) as exc:
            meta.update(
                {"error": str(exc), "returncode": 1, "provider_session_id": None}
            )
            return None, _sanitize_meta(meta)

        meta["duration_seconds"] = outcome["duration_seconds"]
        if outcome["kill"]:
            meta["kill"] = outcome["kill"]

        if outcome["timed_out"]:
            error_tail = _error_tail(outcome["stderr"], outcome["stdout"])
            meta.update(
                {
                    "error": f"bootstrap timeout after {budget}s",
                    "returncode": 1,
                    "provider_session_id": None,
                    "salvaged_session_id": self.extract_session_id(
                        outcome["stdout"] + outcome["stderr"]
                    ),
                    "timed_out": True,
                    "rate_limited": _is_rate_limited(error_tail),
                    "stderr_tail": outcome["stderr"].strip()[-2000:],
                    "stdout_tail": outcome["stdout"].strip()[-500:],
                }
            )
            return None, _sanitize_meta(meta)

        combined = outcome["stdout"] + outcome["stderr"]
        session_id = self.extract_session_id(combined)
        meta.update(
            {
                "returncode": outcome["returncode"],
                "provider_session_id": session_id,
                "stderr_tail": outcome["stderr"].strip()[-2000:],
                "stdout_tail": outcome["stdout"].strip()[-500:],
            }
        )
        return session_id, _sanitize_meta(meta)

    @staticmethod
    def _prompt_file_path(work_dir: str | None, workflow_session_id: str | None) -> Path:
        """Where this call's prompt is written for `-f`.

        Inside the session's runtime dir, next to the other sidecars the agent reads: it is
        within the project boundary opencode already runs under, and per-session, so two
        main agents on one project never hand each other their prompts.
        """
        from exoma.core.workspace.workspace_paths import workflow_paths

        if work_dir:
            runtime_dir = workflow_paths(Path(work_dir).resolve(), workflow_session_id)["runtime_dir"]
        else:
            import tempfile

            runtime_dir = Path(tempfile.gettempdir()) / "exoma-opencode"
        return Path(runtime_dir) / _PROMPT_FILE_NAME

    def _agent_args_for(self, prompt_path: Path, model: str | None, session_id: str) -> list[str]:
        """The one argv shape an agent call has. Shared by the run and the pre-spawn checks
        so what is measured is what is sent."""
        args = [
            self._resolve_command(),
            "run",
            _ATTACHED_PROMPT_INSTRUCTION,
            "-f",
            str(prompt_path),
            *self._agent_args(),
        ]
        if model:
            args.extend(["-m", model])
        args.extend(self._effort_args())
        args.extend(_JSON_FORMAT_ARGS)
        args.extend(["-s", session_id])
        return args

    def run_agent(
        self,
        prompt: str,
        session_id: str,
        model: str | None = None,
        work_dir: str | None = None,
        workflow_session_id: str | None = None,
    ) -> dict:
        """Spawn workflow agent in existing session. The prompt goes in a file (`-f`)."""
        prompt_path = self._prompt_file_path(work_dir, workflow_session_id)
        args = self._agent_args_for(prompt_path, model, session_id)
        hazards = _cmd_shell_hazards(args)
        if hazards:
            return _unsafe_command_error(hazards, self._resolve_work_dir(work_dir))
        try:
            atomic_write_text(prompt_path, prompt)
        except OSError as exc:
            return make_error(
                "unknown",
                f"could not write the prompt file opencode reads: {exc}",
                next_action="Check write access to the session's .workflow runtime directory, then rerun.",
                meta={"error": type(exc).__name__, "prompt_path": str(prompt_path)},
            )
        return self._run_args(args, work_dir)

    def _prospective_agent_args(
        self,
        model: str | None,
        work_dir: str | None = None,
        workflow_session_id: str | None = None,
    ) -> list[str]:
        """The argv run_agent WILL build. `-s <ses_id>` exists only after bootstrap; a real
        id is ~30 chars, so a stand-in keeps the pre-bootstrap measurement honest."""
        return self._agent_args_for(
            self._prompt_file_path(work_dir, workflow_session_id), model, "ses_" + "0" * 26
        )

    def run(
        self,
        prompt: str,
        session: dict,
        model: str | None = None,
        work_dir: str | None = None,
    ) -> dict:
        if not model:
            # Without `-m`, opencode runs its configured model or else the LAST ONE USED on
            # this machine — for any project, whoever used it. That is how a free model
            # picked once somewhere kept answering calls configured for nothing at all.
            return make_error(
                "model_unset",
                "no model is set for this opencode call",
                next_action=(
                    "Choose one with /.provider, or set default_model (or "
                    "routes.<command>.model) in .workflow/second_agent.json."
                ),
                meta={"checked": "pre_bootstrap", "cwd": self._resolve_work_dir(work_dir)},
            )

        # Fail fast: both refusals are knowable BEFORE bootstrap, so neither may first pay
        # ~10-60s spawning an opencode session only to be rejected. The prompt itself is
        # no longer on the command line; what is checked here is everything else (command
        # path, prompt-file path, model, agent, effort), which config and project location
        # decide.
        prospective = self._prospective_agent_args(model, work_dir, session.get("session_id"))
        hazards = _cmd_shell_hazards(prospective)
        if hazards:
            return _unsafe_command_error(hazards, self._resolve_work_dir(work_dir))
        oversize = _too_long_for_cmd(prospective)
        if oversize is not None:
            return make_error(
                "prompt_too_long",
                f"command line is {oversize} chars; the Windows shell caps it at {_CMD_LINE_LIMIT}",
                next_action=(
                    "The prompt travels as a file, so the task is not what overflowed. "
                    "Shorten provider_command / model / agent in .workflow/second_agent.json "
                    "or move the project to a shorter path."
                ),
                meta={
                    "command_line_chars": oversize,
                    "limit": _CMD_LINE_LIMIT,
                    "cwd": self._resolve_work_dir(work_dir),
                    "checked": "pre_bootstrap",
                },
            )

        provider_session_id = session.get("provider_session_id")
        bootstrap_meta = None

        if not provider_session_id:
            provider_session_id, bootstrap_meta = self.init_session(
                model, work_dir, workflow_session_id=session.get("session_id")
            )
            if not provider_session_id:
                provider_session_id, bootstrap_meta = self.init_session(
                    model, work_dir, workflow_session_id=session.get("session_id")
                )

        if not provider_session_id:
            meta = dict(bootstrap_meta or {})
            raw_tail = first_non_empty(
                meta.get("error"), meta.get("stderr_tail"), meta.get("stdout_tail")
            )[:500]
            if meta.get("rate_limited"):
                return make_error(
                    "rate_limited",
                    "opencode could not open a session: the provider is refusing on quota",
                    next_action=(
                        "Second agent is out of quota. Wait for the limit to reset, switch "
                        "model in .workflow/second_agent.json, or check the provider account — "
                        "do NOT resubmit immediately."
                    ),
                    meta=meta,
                    raw_tail=raw_tail,
                )
            return make_error(
                "session_capture_failed",
                "init_session failed: opencode session id not captured after retry",
                next_action="Check opencode is logged in and `opencode run` prints a ses_ id; rerun the command.",
                meta=meta,
                raw_tail=raw_tail,
            )

        session["provider_session_id"] = provider_session_id
        if bootstrap_meta is not None and self.on_session_created:
            try:
                self.on_session_created(provider_session_id)
            except Exception as exc:
                return make_error(
                    "session_capture_failed",
                    "captured OpenCode session id but failed to persist it",
                    next_action=(
                        "Check session storage permissions, then retry; the delegated task "
                        "was not started."
                    ),
                    meta={
                        "error": f"{type(exc).__name__}: {exc}",
                        "provider_session_id": provider_session_id,
                    },
                )

        result = self.run_agent(
            prompt, provider_session_id, model, work_dir, workflow_session_id=session.get("session_id")
        )

        if bootstrap_meta is not None:
            result["meta"]["bootstrap"] = bootstrap_meta
            result["meta"]["provider_session_id"] = (
                result["meta"].get("provider_session_id") or provider_session_id
            )

        return result

    def probe(
        self,
        model: str | None = None,
        work_dir: str | None = None,
        timeout_seconds: int = 45,
    ) -> dict:
        """Liveness probe: trivial prompt in a BRAND NEW opencode session.

        Answers the one question PID liveness cannot: is the second agent alive but
        waiting (rate limited / thinking), or actually gone? Deliberately does NOT
        reuse `-s <session>` — the stuck session is the thing under suspicion.

        Returns {'alive', 'reason', 'returncode', 'duration_seconds', 'timed_out'}.
        Its own short timeout matters: without it the watchdog hangs like its patient.
        """
        command = self._resolve_command()
        args = [command, "run", "PING. Reply PONG.", *self._agent_args()]
        if model:
            args.extend(["-m", model])

        env = self._child_env(work_dir)
        cwd = self._resolve_work_dir(work_dir)

        hazards = _cmd_shell_hazards(args)
        if hazards:
            return _sanitize_meta(
                {
                    "alive": False,
                    "reason": "unsafe_command_line",
                    "hazards": hazards,
                    "returncode": None,
                    "duration_seconds": None,
                    "timed_out": False,
                }
            )

        try:
            outcome = self._popen_capture(
                args, env, cwd, max(5, int(timeout_seconds)), "probe"
            )
        except (OSError, FileNotFoundError) as exc:
            return _sanitize_meta(
                {
                    "alive": False,
                    "reason": "command_not_found",
                    "error": str(exc),
                    "returncode": None,
                    "duration_seconds": None,
                    "timed_out": False,
                }
            )

        probe_tail = _error_tail(outcome["stderr"], outcome["stdout"])
        rate_limited = _is_rate_limited(probe_tail)
        stream_failed = _is_stream_failure(probe_tail)
        if rate_limited:
            reason = "probe_rate_limited"
        elif stream_failed:
            reason = "probe_stream_failed"
        elif outcome["timed_out"]:
            reason = "probe_timeout"
        elif outcome["returncode"] != 0:
            reason = "probe_error"
        else:
            reason = "probe_ok"

        if not probe_tail and outcome["timed_out"]:
            rate_limited = None

        return _sanitize_meta(
            {
                "alive": reason == "probe_ok",
                "reason": reason,
                "rate_limited": rate_limited,
                "no_probe_output": not probe_tail,
                "stream_failed": stream_failed,
                "returncode": outcome["returncode"],
                "duration_seconds": outcome["duration_seconds"],
                "timed_out": outcome["timed_out"],
                "stderr_tail": outcome["stderr"].strip()[-500:],
            }
        )

    def _run_args(self, args: list[str], work_dir: str | None = None) -> dict:
        hazards = _cmd_shell_hazards(args)
        if hazards:
            return _unsafe_command_error(hazards, self._resolve_work_dir(work_dir))
        oversize = _too_long_for_cmd(args)
        if oversize is not None:
            return make_error(
                "prompt_too_long",
                f"command line is {oversize} chars; the Windows shell caps it at {_CMD_LINE_LIMIT}",
                next_action=(
                    "The prompt travels as a file, so the task is not what overflowed. "
                    "Shorten provider_command / model / agent in .workflow/second_agent.json "
                    "or move the project to a shorter path."
                ),
                meta={
                    "command_line_chars": oversize,
                    "limit": _CMD_LINE_LIMIT,
                    "cwd": work_dir,
                },
            )

        env = self._child_env(work_dir)
        cwd = self._resolve_work_dir(work_dir)

        try:
            outcome = self._popen_capture(
                args, env, cwd, self._effective_timeout(), "agent"
            )
        except FileNotFoundError as exc:
            return make_error(
                "command_not_found",
                f"command not found: {args[0]}",
                next_action="Install opencode or fix provider_command in .workflow/second_agent.json.",
                meta={"error": str(exc), **_argv_meta(args), "cwd": cwd},
            )
        except OSError as exc:
            return make_error(
                "unknown",
                str(exc),
                next_action="Inspect .workflow/data/sessions/<session>/logs and rerun; report if it persists.",
                meta={"error": type(exc).__name__, **_argv_meta(args), "cwd": cwd},
            )

        if outcome["timed_out"]:
            raw = first_non_empty(
                outcome["stderr"],
                outcome["stdout"],
                f"timeout after {self.timeout_seconds}s",
            )
            timeout_meta = {
                "timeout_seconds": self.timeout_seconds,
                **_argv_meta(args),
                "cwd": cwd,
                "duration_seconds": outcome["duration_seconds"],
                "idle_seconds": outcome.get("idle_seconds"),
                "kill": outcome["kill"],
            }
            if _is_rate_limited(_error_tail(outcome["stderr"], outcome["stdout"])):
                return make_error(
                    "rate_limited",
                    self.clean_output(raw) or "opencode hit a provider rate limit",
                    next_action=(
                        "Second agent is out of quota. Wait for the limit to reset, switch "
                        "model in .workflow/second_agent.json, or check the provider account — "
                        "do NOT resubmit immediately."
                    ),
                    meta=timeout_meta,
                )
            return make_error(
                "timeout",
                self.clean_output(raw),
                next_action="Increase timeout_seconds (0 = no limit) or narrow the task, then retry.",
                meta=timeout_meta,
            )

        raw = first_non_empty(outcome["stdout"], outcome["stderr"])
        combined = "\n".join(
            part
            for part in (ensure_text(outcome["stdout"]), ensure_text(outcome["stderr"]))
            if part
        )
        meta = {
            "returncode": outcome["returncode"],
            "stderr": ensure_text(outcome["stderr"]).strip(),
            **_argv_meta(args),
            "cwd": cwd,
            "duration_seconds": outcome["duration_seconds"],
            "idle_seconds": outcome.get("idle_seconds"),
            "provider_session_id": self.extract_session_id(combined),
        }
        cleaned = self.clean_output(raw)

        if outcome["returncode"] != 0:
            err_tail = _error_tail(meta["stderr"])
            tail_low = err_tail.lower()
            if _is_rate_limited(err_tail):
                return make_error(
                    "rate_limited",
                    cleaned
                    or "opencode refused: provider rate limit / quota exhausted",
                    next_action=(
                        "Second agent is out of quota. Wait for the limit to reset, switch "
                        "model in .workflow/second_agent.json, or check the provider account — "
                        "do NOT resubmit immediately."
                    ),
                    meta=meta,
                )
            if _is_stream_failure(err_tail):
                return make_error(
                    "streaming_failed",
                    cleaned or "opencode lost the provider stream mid-response",
                    next_action=_STREAM_FAIL_NEXT_ACTION,
                    meta=meta,
                )
            if any(sign in tail_low for sign in _CMD_LINE_SIGNS):
                return make_error(
                    "prompt_too_long",
                    cleaned or "the shell refused the command line as too long",
                    next_action=(
                        "Shorten the task text — split it into two narrower delegated calls."
                    ),
                    meta=meta,
                )
            if any(sign in tail_low for sign in _PERMISSION_SIGNS):
                return make_error(
                    "permission_denied",
                    cleaned or "opencode refused access",
                    next_action="Grant explicit access to the path or move the context inside the project, then retry.",
                    meta=meta,
                    blocked_paths=_guess_blocked_paths(meta["stderr"]),
                )
            return make_error(
                "unknown",
                cleaned or f"opencode exited {outcome['returncode']}",
                next_action="Inspect .workflow/data/sessions/<session>/logs for the raw output and rerun.",
                meta=meta,
            )

        if not cleaned.strip():
            return make_error(
                "empty_output",
                "opencode returned no content",
                next_action="Rephrase the task or check .workflow/data/sessions/<session>/logs raw_tail; the run succeeded but produced nothing.",
                meta=meta,
                raw_tail=raw[:500],
            )

        # make_ok sanitizes content and metadata before either can reach persistence.
        return make_ok(cleaned, meta)

    @staticmethod
    def _error(content: str, meta: dict) -> dict:
        return make_error(
            "unknown",
            ensure_text(content),
            next_action="Inspect .workflow/data/sessions/<session>/logs and rerun.",
            meta=meta,
        )

    def _resolve_command(self) -> str:
        return osutil.resolve_exe(self.command)

    def _child_env(self, work_dir: str | None) -> dict:
        """Allowlisted environment plus the active project's .env; see adapters/shared/child_env.py."""
        env, self._env_meta = project_scoped_env(detect_project_root(work_dir))
        return env

    @staticmethod
    def _resolve_work_dir(work_dir: str | None) -> str | None:
        return str(Path(work_dir).resolve()) if work_dir else None
