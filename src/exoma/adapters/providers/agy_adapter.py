"""agy CLI as a second_agent provider.

Five facts about `agy` decided the shape of this file. Each was probed against the
installed binary rather than read off `--help`, because two of them contradict what the
flag names suggest.

1. The prompt is an ARGUMENT (`-p`), not stdin. That puts agy on the same side as
   opencode and against codex: the command line has a length ceiling this adapter has to
   measure before it spawns, or the failure arrives from the OS as an unreadable error
   with the prompt nowhere in it.

2. The session id arrives in the FIRST line of `--output-format stream-json`:
   `{"event":"init","conversation_id":"<uuid>",...}` on stdout. So there is no bootstrap
   call to pay for and no log file to tail. The `--log-file` route works too — the file
   carries `Created conversation <uuid>` from agy's own server log — but it arrives later,
   needs a temp file, and is a plain-text format with no promise behind it. The pattern
   for it is kept as a fallback, unused on the happy path.

3. Continuing is a FLAG, not a subcommand: `--conversation <id>`. Unlike codex, whose
   `exec resume` accepts a narrower option set, agy takes the same flags either way. The
   two branches here differ by exactly one argument.

4. `--print-timeout` defaults to five minutes. The runtime's own budget defaults to
   thirty. Left alone, agy dies at minute five and the runtime waits out the other
   twenty-five for a process that is already gone, then reports a timeout against the
   wrong cause. So the flag is derived from the effective timeout on every call.

5. There is no read-only mode. This is the fact that matters most and the one the flag
   names hide. `--sandbox` and `--mode plan` were both probed: 56 tools before, 56 tools
   after, `permission_mode: always-proceed` in both, with `write_to_file`,
   `replace_file_content`, `sed_file`, `notebook_edit`, `delete_knowledge` and
   `run_command` present throughout. Dropping `--dangerously-skip-permissions` yields
   `permission_mode: request-review`, which refuses every tool — a write test left no
   file behind, and a plain READ of a source file failed the same way, returning an empty
   response. agy keeps no config directory to install a policy into.

   The choice is therefore binary: every tool including writes, or no tools at all and a
   provider that cannot gather evidence. This adapter takes the first and pairs it with
   `core/agy_guard.py`, which compares the working tree before and after each call. That
   is detection, not prevention. An agy second_agent CAN write to the project it is
   pointed at, and the only thing asking it not to is the prompt.

`--disable-slash-commands` rides every call. agy expands slash commands and skills in
print mode, and the workflow's own prompts are full of `/.explore`-shaped text that must
reach the model as characters rather than as an instruction to agy's CLI.
"""

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from exoma.config.providers import effort_args
from exoma.config.settings import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
)
from exoma.core.policy import agy_guard
from exoma.core.evidence.contract import make_error as _contract_make_error
from exoma.core.evidence.contract import make_ok as _contract_make_ok
from exoma.utils import osutil
from exoma.utils.redact import redact, redact_value

AGY_COMMAND = os.getenv("AGY_COMMAND", "agy")

# Upper bound on captured stdout/stderr per stream (~4MB), matching the other adapters.
# The FRONT is dropped: the evidence contract is emitted at the end of a run, so the tail
# is the part the runtime consumes.
MAX_CAPTURE_CHARS = 4_000_000

# Windows caps a process command line at 32767 characters. opencode's adapter measures
# against 8191 instead — that is `cmd.exe`'s limit, and the right number for a call that
# may route through a shell. Nothing here does: `subprocess.Popen` is handed an argv LIST,
# which reaches `CreateProcess` directly. Using 8191 here would reject prompts the OS
# accepts. The stderr signs below are the second net for the day that reasoning is wrong.
_CMD_LINE_LIMIT = 32767
_CMD_LINE_HEADROOM = 1024
_CMD_LINE_SIGNS = ("command line is too long", "the input line is too long")

# First pattern is the live one, verified against the installed binary: line 1 of
# `--output-format stream-json` is the `init` event and carries `conversation_id`. The
# second reads agy's server log (`Created conversation <uuid>`), which is what `--log-file`
# collects; this adapter does not pass that flag, so the pattern only fires if a future
# build moves the id out of the stream. Both are anchored on their own key so an id quoted
# inside the evidence text cannot match.
_CONVERSATION_ID_PATTERNS = (
    r'"conversation_id"\s*:\s*"([0-9a-fA-F][0-9a-fA-F-]{15,})"',
    r"(?im)created\s+conversation\s+([0-9a-fA-F][0-9a-fA-F-]{15,})",
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
)

_STREAM_FAIL_SIGNS = (
    "stream closed",
    "stream error",
    "connection reset",
    "connection closed",
    "socket hang up",
    "premature close",
    "econnreset",
    "epipe",
)

_ERROR_TAIL_CHARS = 1600


def _error_tail(*texts: str) -> str:
    blob = "\n".join(text for text in texts if text)
    return blob[-_ERROR_TAIL_CHARS:]


def _matches(blob: str, signs: tuple[str, ...]) -> bool:
    lowered = (blob or "").lower()
    return any(sign in lowered for sign in signs)


def _argv_meta(args: list[str]) -> dict:
    encoded = "\0".join(str(arg) for arg in args).encode("utf-8", errors="replace")
    return {
        "argv_count": len(args),
        "argv_chars": sum(len(str(arg)) for arg in args),
        "argv_sha256": hashlib.sha256(encoded).hexdigest()[:16],
    }


def _too_long_for_cmd(args: list[str]) -> int | None:
    """Total command-line length when it will not fit, else None. Windows only."""
    if not osutil.IS_WINDOWS:
        return None
    total = sum(len(str(arg)) + 3 for arg in args)  # +3: two quotes and a separator
    return total if total > (_CMD_LINE_LIMIT - _CMD_LINE_HEADROOM) else None


# Shared with every other adapter. The local copy this replaces dropped the redaction
# hits, so an agy call that scrubbed a secret still recorded `redactions: 0`.
from exoma.adapters.shared.redaction import (  # noqa: E402
    make_error,
    make_ok,
    sanitize_meta as _sanitize_meta,
)


class AgyAdapter:
    adapter = "agy"

    def __init__(
        self,
        command: str = AGY_COMMAND,
        timeout_seconds: int | None = DEFAULT_TIMEOUT_SECONDS,
        on_progress=None,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.no_timeout = timeout_seconds is None or timeout_seconds <= 0
        self.on_progress = on_progress
        self.poll_interval = DEFAULT_POLL_INTERVAL_SECONDS
        # No bootstrap call: the id is in the first line of the stream (fact 2 above).
        self.bootstrap_timeout_seconds = None
        # agy has personas (`--agent`), but `agy agents` on a stock install lists none, so
        # there is nothing to select and nothing to default to.
        self.agent = None
        self.effort: str | None = None
        self.last_call_meta: dict = {}
        self.on_session_created = None

    # ------------------------------------------------------------------ static

    @staticmethod
    def extract_session_id(text: str) -> str | None:
        for pattern in _CONVERSATION_ID_PATTERNS:
            found = re.search(pattern, text or "")
            if found:
                return found.group(1)
        return None

    @staticmethod
    def clean_output(text: str) -> str:
        """The final answer out of a `stream-json` transcript.

        Two sources, tried in order, because they fail differently. The `result` event is
        agy's own statement of the finished response and is the only one carrying the
        whole thing after a retry or a revision. It is also the LAST event, so a run that
        dies mid-answer never emits it — and that is the case the `agent_response` deltas
        cover, having carried the same text one event at a time on the way past.

        Non-JSON lines are kept. A build that banners in plain text before the stream
        starts would otherwise have its output silently dropped.
        """
        result_response: str | None = None
        deltas: list[str] = []
        plain: list[str] = []
        saw_json = False

        for line in (text or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("{"):
                plain.append(line)
                continue
            try:
                event = json.loads(stripped)
            except (ValueError, TypeError):
                plain.append(line)
                continue
            if not isinstance(event, dict):
                continue
            saw_json = True

            if event.get("event") == "result":
                payload = event.get("result")
                if isinstance(payload, dict) and payload.get("response"):
                    result_response = str(payload["response"])
                continue

            update = event.get("step_update")
            if isinstance(update, dict) and update.get("step_type") == "agent_response":
                delta = update.get("text_delta")
                if delta:
                    deltas.append(str(delta))

        if result_response:
            return result_response.strip()
        if deltas:
            return "".join(deltas).strip()
        if saw_json:
            # Structured output that named no answer. Returning the raw JSONL here would
            # hand the runtime a wall of events to parse as if it were evidence.
            return ""
        return "\n".join(plain).strip()

    # ------------------------------------------------------------------- argv

    def _effective_timeout(self) -> int | None:
        return None if self.no_timeout else self.timeout_seconds

    def _build_args(
        self,
        prompt: str,
        resume_id: str | None,
        model: str | None,
        timeout: int | None,
    ) -> list[str]:
        args = [
            self.command,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--disable-slash-commands",
            "--dangerously-skip-permissions",
        ]
        if timeout:
            # agy's own print-mode budget. Without this it is five minutes regardless of
            # what the runtime allotted, and the mismatch reads as a hang (fact 4).
            args.extend(["--print-timeout", f"{int(timeout)}s"])
        if resume_id:
            args.extend(["--conversation", str(resume_id)])
        if model:
            args.extend(["--model", model])
        args.extend(effort_args(self.adapter, self.effort))
        return args

    @staticmethod
    def _resolve_work_dir(work_dir: str | None) -> str | None:
        return str(Path(work_dir).resolve()) if work_dir else None

    # -------------------------------------------------------------------- run

    def run(
        self,
        prompt: str,
        session: dict,
        model: str | None = None,
        work_dir: str | None = None,
    ) -> dict:
        cwd = self._resolve_work_dir(work_dir)
        resume_id = (session or {}).get("provider_session_id")
        timeout = self._effective_timeout()
        args = self._build_args(prompt, resume_id, model, timeout)

        # Before spawning: the length is knowable now, and an OS-level refusal names
        # neither the prompt nor the limit.
        oversize = _too_long_for_cmd(args)
        if oversize is not None:
            return make_error(
                "prompt_too_long",
                f"command line is {oversize} chars; Windows caps it at {_CMD_LINE_LIMIT}",
                next_action=(
                    "Shorten the task text — split it into two narrower delegated calls. "
                    "The prompt scaffolding and the evidence sidecars are fixed cost; "
                    "only the task is yours to trim."
                ),
                meta={"argv_chars": oversize, "limit": _CMD_LINE_LIMIT},
            )

        # agy takes its prompt on argv. Launched through a .cmd/.bat shim, that argv is
        # re-parsed by cmd.exe, where `"`, `&`, `|`, `%` in the prompt become commands.
        # Same guard as opencode's, called through its module so its test seam applies.
        from exoma.adapters.providers import opencode_adapter

        hazards = opencode_adapter._cmd_shell_hazards(args)
        if hazards:
            return make_error(
                "unsafe_command_line",
                "refused to run agy: cmd.exe would interpret characters in its arguments",
                next_action=(
                    "Point AGY_COMMAND / provider_command at agy's executable rather than "
                    "its .cmd/.bat shim; the prompt travels on argv and cmd.exe re-parses it."
                ),
                meta={"hazards": hazards, "cwd": cwd, "checked": "pre_spawn"},
            )

        # The guard's first half. Taken before the process starts so that anything the
        # call leaves behind is attributable to the call (see core/agy_guard.py).
        guard_before = agy_guard.snapshot(cwd)

        captured: dict = {"session_id": resume_id}

        def _saw_session(conversation_id: str) -> None:
            if not conversation_id or conversation_id == captured["session_id"]:
                return
            captured["session_id"] = conversation_id
            if isinstance(session, dict):
                session["provider_session_id"] = conversation_id
            if self.on_session_created:
                try:
                    self.on_session_created(conversation_id)
                except Exception:
                    # The session is captured either way; a caller whose callback raises
                    # must not lose the run that produced it. The executor's callback does
                    # not raise: it keeps a failed write and retries it after the run, and
                    # names the loss on the result (`session_persisted: false`).
                    pass

        try:
            outcome = self._popen_capture(args, cwd, timeout, "agent", _saw_session)
        except FileNotFoundError:
            return make_error(
                "command_not_found",
                f"{self.command!r} is not on PATH",
                next_action=(
                    "Install agy, or set AGY_COMMAND / provider_command to its full path."
                ),
                meta=_argv_meta(args),
            )
        except OSError as exc:
            return make_error(
                "unknown",
                f"could not run {self.command!r}: {exc}",
                next_action="Check the command and the working directory, then retry.",
                meta=_argv_meta(args),
            )

        stdout = outcome["stdout"]
        stderr = outcome["stderr"]

        # Late recovery: the drain scans line by line, but a build that emits the id in a
        # shape split across reads would be missed there and present in the whole text.
        if not captured["session_id"]:
            recovered = self.extract_session_id(stdout + "\n" + stderr)
            if recovered:
                _saw_session(recovered)

        guard = agy_guard.verdict(guard_before, agy_guard.snapshot(cwd))

        meta = {
            "returncode": outcome["returncode"],
            **_argv_meta(args),
            "cwd": cwd,
            "duration_seconds": outcome["duration_seconds"],
            "idle_seconds": outcome["idle_seconds"],
            "provider_session_id": captured["session_id"],
            "resumed": bool(resume_id),
            "output_complete": outcome["output_complete"],
            # Never silent: a guard that could not run says so here, and a guard that
            # found a write says that. Both are louder than a missing key.
            "workspace_guard": guard,
            "stderr": stderr[-2000:],
        }
        if guard.get("mutated"):
            meta["workspace_mutated"] = True
        # Same as codex: a resumed call naming a different conversation lost its state, and
        # last_call_meta is what the usage row reads.
        # A call that named no thread (it failed before one was printed) changed nothing it can show.
        meta["thread_changed"] = (
            bool(resume_id) and bool(captured["session_id"]) and captured["session_id"] != resume_id
        )
        if isinstance(self.last_call_meta, dict):
            self.last_call_meta["resumed"] = meta["resumed"]
            self.last_call_meta["thread_changed"] = meta["thread_changed"]

        content = self.clean_output(stdout)
        tail = _error_tail(stderr, stdout)

        if outcome["timed_out"]:
            meta["kill"] = outcome["kill"]
            meta["timeout_seconds"] = timeout
            if _matches(tail, _RATE_LIMIT_SIGNS):
                return make_error(
                    "rate_limited",
                    "agy hit a provider rate limit before the call could finish",
                    next_action=(
                        "Wait for the window to reset, then retry the same task. "
                        "Splitting it will not help; the limit is per account."
                    ),
                    meta=meta,
                )
            resumable = (
                "The session was captured, so a retry resumes it."
                if captured["session_id"]
                else "No session was captured, so a retry starts a NEW conversation."
            )
            return make_error(
                "timeout",
                f"agy did not finish within {timeout}s",
                next_action=(
                    f"{resumable} Split the task into two narrower delegated calls, "
                    "or raise timeout_seconds in .workflow/second_agent.json."
                ),
                meta=meta,
            )

        if outcome["returncode"] != 0:
            if _matches(tail, _RATE_LIMIT_SIGNS):
                return make_error(
                    "rate_limited",
                    tail or "agy reported a rate limit",
                    next_action=(
                        "Wait for the window to reset, then retry the same task."
                    ),
                    meta=meta,
                )
            if _matches(tail, _STREAM_FAIL_SIGNS):
                return make_error(
                    "streaming_failed",
                    tail or "agy lost the provider stream mid-response",
                    next_action=(
                        "Transient — the stream dropped, the request itself was fine. "
                        "Retry once; if it dies again, split the task into two narrower "
                        "delegated calls."
                    ),
                    meta=meta,
                )
            if _matches(tail, _CMD_LINE_SIGNS):
                return make_error(
                    "prompt_too_long",
                    tail or "the shell refused the command line as too long",
                    next_action=(
                        "Shorten the task text — split it into two narrower delegated "
                        "calls."
                    ),
                    meta=meta,
                )
            return make_error(
                "unknown",
                tail or f"agy exited {outcome['returncode']}",
                next_action=(
                    "Read meta.stderr for the provider's own message, then retry."
                ),
                meta=meta,
                orphan_content=content[:4000],
            )

        if not content.strip():
            return make_error(
                "empty_output",
                "agy exited 0 and produced no answer",
                next_action=(
                    "Retry once. A run that ends with every tool refused looks exactly "
                    "like this — check meta.workspace_guard and meta.stderr."
                ),
                meta=meta,
            )

        if not captured["session_id"]:
            # The answer survives the failure. Losing a session costs the NEXT call its
            # context; discarding the text would cost this one its whole result.
            return make_error(
                "session_capture_failed",
                "agy produced an answer but no conversation id was found",
                next_action=(
                    "Rerun as a clean invocation. If it repeats, agy has changed where it "
                    "reports the id — widen _CONVERSATION_ID_PATTERNS in "
                    "adapters/agy_adapter.py."
                ),
                meta=meta,
                orphan_content=content[:4000],
            )

        return make_ok(content, meta)

    # ------------------------------------------------------------------ probe

    def probe(
        self,
        session_id: str | None = None,
        model: str | None = None,
        work_dir: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        """Is agy able to answer at all? Always on a fresh conversation.

        `session_id` is accepted and ignored, exactly as codex's probe does. Probing an
        existing conversation would report the health of that conversation rather than of
        the provider, and a broken session would then read as a broken install.
        """
        cwd = self._resolve_work_dir(work_dir)
        budget = timeout_seconds or 60
        args = self._build_args("PING. Reply PONG.", None, model, budget)

        try:
            outcome = self._popen_capture(args, cwd, budget, "probe", None)
        except FileNotFoundError:
            return {
                "alive": False,
                "reason": f"{self.command!r} is not on PATH",
                "rate_limited": False,
                "no_probe_output": False,
                "stream_failed": False,
                "returncode": None,
                "duration_seconds": 0.0,
                "timed_out": False,
                "stderr_tail": "",
            }
        except OSError as exc:
            return {
                "alive": False,
                "reason": str(exc),
                "rate_limited": False,
                "no_probe_output": False,
                "stream_failed": False,
                "returncode": None,
                "duration_seconds": 0.0,
                "timed_out": False,
                "stderr_tail": "",
            }

        tail = _error_tail(outcome["stderr"], outcome["stdout"])
        answered = bool(self.clean_output(outcome["stdout"]).strip())
        rate_limited = _matches(tail, _RATE_LIMIT_SIGNS)
        stream_failed = _matches(tail, _STREAM_FAIL_SIGNS)
        alive = (
            outcome["returncode"] == 0
            and not outcome["timed_out"]
            and answered
            and not rate_limited
        )

        if alive:
            reason = "ok"
        elif outcome["timed_out"]:
            reason = f"no answer within {budget}s"
        elif rate_limited:
            reason = "rate limited"
        elif stream_failed:
            reason = "provider stream dropped"
        elif not answered:
            reason = "exited 0 with no answer"
        else:
            reason = tail or f"exited {outcome['returncode']}"

        return {
            "alive": alive,
            "reason": reason,
            "rate_limited": rate_limited,
            "no_probe_output": not answered,
            "stream_failed": stream_failed,
            "returncode": outcome["returncode"],
            "duration_seconds": outcome["duration_seconds"],
            "timed_out": outcome["timed_out"],
            "stderr_tail": outcome["stderr"][-2000:],
        }

    # ----------------------------------------------------------------- popen

    def _popen_capture(
        self,
        args: list[str],
        cwd: str | None,
        timeout: int | None,
        phase: str,
        on_session,
    ) -> dict:
        """Run `args`, draining both streams on threads.

        Threaded draining is what makes the heartbeat and the timeout kill possible:
        `communicate()` blocks until the process ends, which is precisely the case that
        needs handling. It is also what lets the conversation id be reported the moment
        it appears rather than after the run — a call that dies mid-way still leaves a
        resumable session behind.

        No stdin is written. agy takes its prompt on argv, and closing the pipe
        immediately is what tells it there is nothing coming.
        """
        started = time.monotonic()
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        self.last_call_meta = _sanitize_meta(
            {"phase": phase, **_argv_meta(args), "cwd": cwd, "timeout_seconds": timeout}
        )

        proc = subprocess.Popen(
            args,
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
        seen_session = {"done": False}

        def _drain(stream, key: str) -> None:
            buf = chunks[key]
            try:
                for line in iter(stream.readline, ""):
                    buf.append(line)
                    sizes[key] += len(line)
                    last_output["at"] = time.monotonic()
                    # Both streams: the id is on stdout today, and a build that banners to
                    # stderr would otherwise lose the session with nothing to show for it.
                    if on_session and not seen_session["done"]:
                        conversation_id = AgyAdapter.extract_session_id(line)
                        if conversation_id:
                            seen_session["done"] = True
                            on_session(conversation_id)
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

        outcome = {
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
                "returncode": outcome["returncode"],
                "timed_out": timed_out,
                "duration_seconds": outcome["duration_seconds"],
                "timeout_seconds": timeout,
                "kill": kill_info or None,
                "output_complete": drained,
                "idle_seconds": outcome["idle_seconds"],
                "stderr_tail": outcome["stderr"][-2000:],
            }
        )
        return outcome

    def _tick(self, phase: str, elapsed: float, idle: float = 0.0) -> None:
        """One liveness beat. A broken callback must not kill a running call."""
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
