"""Codex CLI as a second_agent provider.

Shaped by four facts about `codex exec` that make it a different animal from opencode,
not a rename of it:

1. The prompt goes in on STDIN (`codex exec -`), never as an argument. OpenCode's adapter
   has to measure its own argv against the Windows 8191-char command-line cap and can
   fail a call before the provider ever runs; here that limit is simply unreachable, so
   there is no `prompt_too_long` path to write.
2. The session id arrives in the FIRST event of the stream —
   `{"type":"thread.started","thread_id":"<uuid>"}` — so there is no bootstrap call to
   pay for. OpenCode needs `init_session` because its id only appears once a session has
   been opened; codex hands it over as the run begins. The id is reported through
   `on_session_created` the moment it is seen rather than after the run, so a call that
   dies mid-way still leaves a resumable session behind.
3. Continuing work is a different subcommand, not a flag: `codex exec resume <id> -`.
   It accepts a NARROWER option set than `exec`: no `-C/--cd`, no `--sandbox`, and no
   `--color`. Those are parser-level refusals — the call dies with `unexpected argument`
   before codex runs, which is why passing one is a total outage of continuation rather
   than a degraded call. The process cwd is what actually roots a call — `_popen_capture`
   spawns with `cwd=` set either way — so the missing `-C` costs a resumed call nothing.
   Sandbox and the declared read boundary both re-assert through `-c`, which resume DOES
   accept, so a resumed call cannot end up looser than the call that opened it.
4. Policy is per-invocation, not a config file. Codex reads `~/.codex/config.toml`, but
   every value in it can be overridden with `-c key=value` on the command line, and the
   sandbox has its own `-s` flag. So this provider ships no global config to merge: the
   read-only boundary rides on every single call and there is no file for a user to drift
   out from under it. `config/providers.py` records that choice.

   That boundary covers writes and NOT reads, which is the opposite of what this paragraph
   used to say. `--sandbox read-only` stops SHELL writes only; codex's own `--help` scopes
   it to "model-generated shell commands", and that is literal. Its file-edit tool
   (`apply_patch`) is gated by the approval policy, not the sandbox: probed on codex-cli
   0.154.0, Windows, a read-only call had its shell write denied and then created the file
   through that tool. `approval_policy="never"` makes codex reject such a patch ("writing
   is blocked by read-only sandbox"), so it rides every call, resumed ones included
   (`_APPROVAL_ARGS`). `tests/checks/codex_write_probe.py` re-proves both writes on both
   branches against the real CLI, opt-in. Reads were supposed to be stopped by
   `[permissions.<profile>.filesystem]`, passed on every call from `core/secret_patterns.py`
   — the same secret list opencode denies, in codex's dialect. It parses, and it stops
   nothing: probed against codex-cli 0.147.0, denying `**` and `**/*` for `:workspace_roots`
   and then asking `codex exec` for a file in that root returns the file at exit 0. Codex
   reads by spawning a shell, and no shell read is routed through the permission map.

   The flags still ship. They cost four argv elements, they are already correct for the day
   codex gates shell reads, and codex has no project-scoped config layer to install them
   into anyway (a `.codex/config.toml` in a project root is not loaded at all), so argv is
   not merely the tidier route here, it is the only one. What must not happen again is
   anyone reading their presence as protection: `adapters/install/codex_install.py` reports
   `not_enforceable` with `permissions_enforced: 0`, and `dist/config/codex/AGENTS.md` tells
   the agent that avoiding secret files is its own obligation. A codex second_agent can read
   every file in the project it is pointed at.

`--output-schema` is deliberately unused. It would force the final message into JSON, and
the workflow's contract is the text blocks `[EVIDENCE]`/`[DIGEST]` — schema-forcing here
would break the very output the runtime parses.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from config.providers import provider_stable_version
from config.settings import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
)
from core.evidence.contract import make_error as _contract_make_error
from core.evidence.contract import make_ok as _contract_make_ok
from core.policy.secret_patterns import codex_permission_args
from utils import osutil
from utils.parser import ensure_text
from utils.redact import redact, redact_value

from adapters.shared.usage import merge_usage, normalize_usage

CODEX_COMMAND = os.getenv("CODEX_COMMAND", "codex")
# read-only is the whole point of a second agent: it gathers evidence and writes nothing.
# Overridable for the rare caller that knows better, but never widened by this adapter.
CODEX_SANDBOX = os.getenv("CODEX_SANDBOX", "read-only")

# Upper bound on captured stdout/stderr per stream (~4MB). A healthy evidence run is a few
# hundred KB; this only clamps a runaway process. The FRONT is dropped — the evidence
# contract is emitted at the end, and the tail is what the runtime consumes.
MAX_CAPTURE_CHARS = 4_000_000

# `thread.started` is the first event `--json` emits, verified against codex-cli 0.147.0:
# `{"type":"thread.started","thread_id":"019febbb-012d-7710-83c8-5a59d3c6d7ed"}` on stdout
# line 1, stderr empty. The id also appears in the persisted rollout header as
# `session_id`, and the third pattern reads the human banner (`session id: <uuid>`) that
# `codex exec` prints WITHOUT `--json`. That last shape is not one this adapter can
# produce — every call passes `--json` — but a build that ever fell back to plain output
# would otherwise cost the session silently, and the pattern is anchored to the start of
# a line so it cannot match an id quoted inside the evidence text.
_THREAD_ID_PATTERNS = (
    r'"thread_id"\s*:\s*"([0-9a-fA-F-]{16,})"',
    r'"session_id"\s*:\s*"([0-9a-fA-F-]{16,})"',
    r"(?im)^\s*session[ _-]?id\s*:\s*([0-9a-fA-F][0-9a-fA-F-]{15,})",
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

_PERMISSION_SIGNS = (
    "permission denied",
    "eacces",
    "not permitted",
    "access is denied",
    "operation not permitted",
    "outside",
    "forbidden",
)

# Codex's own Windows sandbox refusing to start a session (CASE-015): "fs sandbox helper
# failed ... windows sandbox failed: elevated Windows sandbox requires effective `:root` read
# access". Checked before the permission signs: the read access it names is the sandbox's
# setup, not a path the agent asked for, and "grant read access to the path" fixes nothing.
_SANDBOX_SIGNS = (
    "windows sandbox failed",
    "sandbox helper failed",
    "elevated windows sandbox",
    "unelevated restricted-token sandbox",
)

# The steps, in the order they are worth trying. The runtime never lowers the sandbox itself:
# the read-deny profile it sends needs the elevated backend, and choosing a weaker one is the
# user's call. Upstream reports: openai/codex#46312 (a resumed thread fails while a new one
# works), openai/codex#46114 (every thread fails after an update).
_SANDBOX_NEXT_ACTION = (
    "codex's Windows sandbox refused to start this session; nothing ran. Fix in order: "
    "(1) a RESUMED thread failing where a new one worked is a known codex regression — start "
    "a new main session (/clear in Claude Code, which issues a new MAIN_SESSION_ID and a new "
    "codex thread) and rerun the same command; "
    "(2) if new threads fail too, the elevated sandbox setup is broken — update codex, run "
    "`codex` once interactively and approve the sandbox setup prompts (UAC), see "
    "openai/codex#46114; "
    "(3) until then, switch the second agent with /.provider (opencode). "
    "The workflow does not lower the sandbox for you; meta.provider_version names the codex "
    "release that failed."
)

# The same refusal on a RESUMED thread is a different failure for the reader: a new thread is
# known to work there (CASE-015, CASE-016 bisect: 0.155.0-0.160.0 fail on resume only), so the
# first step is certain rather than one of three guesses. `meta.reason` carries the split; the
# error type stays `sandbox_unavailable` so DEC-024's contract is unchanged.
_RESUME_SANDBOX_REASON = "resume_sandbox_failed"


def _resume_sandbox_next_action(provider_version: str | None) -> str:
    pinned = provider_stable_version("codex")
    release = provider_version or "this codex release"
    return (
        f"Resuming the codex thread failed in the Windows sandbox ({release}); nothing ran. "
        "New threads are known to work where a resume fails (a codex regression on resume, "
        "CASE-016). Fix in order: "
        "(1) start a new main session (/clear in Claude Code, which issues a new "
        "MAIN_SESSION_ID and a new codex thread) and rerun the same command — the previous "
        "thread's context is not carried over; "
        f"(2) if every resume keeps failing, pin codex back to {pinned}, the release this "
        "workflow was tested against; "
        "(3) if a new thread fails too, this is not the resume regression — follow the "
        "sandbox setup steps (run `codex` once interactively, approve UAC) or switch with "
        "/.provider. The workflow does not lower the sandbox for you."
    )

_ERROR_TAIL_CHARS = 1600

def codex_version(command: str | None) -> str | None:
    """`codex --version`, read once per binary per process; None when it cannot be read.

    Recorded on every call so a failure can be tied to the release that produced it: codex
    behavior on Windows changes between releases (CASE-015), and the trail held no version.
    The reader is shared with the other providers (core/provider/versions.py).
    """
    from core.provider.versions import read_version

    return read_version(command or "codex")


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


# Shared with every other adapter. The local copy this replaces dropped the redaction
# hits, so a codex call that scrubbed a secret still recorded `redactions: 0`.
from adapters.shared.redaction import (  # noqa: E402
    make_error,
    make_ok,
    sanitize_meta as _sanitize_meta,
)


# `--sandbox read-only` confines shell commands only. codex's own file-edit tool
# (`apply_patch`) is gated by the approval policy instead: a patch outside the writable
# roots is REJECTED only under `never`, and otherwise falls to whatever the user's
# config.toml or exec's default decides. Probed on codex-cli 0.154.0, Windows: without
# this pair a read-only call created a file through that tool while its shell write was
# denied. Passed on every call, resumed ones too, so no config file can loosen it.
_APPROVAL_ARGS = ("-c", 'approval_policy="never"')


class CodexAdapter:
    adapter = "codex"

    def __init__(
        self,
        command: str = CODEX_COMMAND,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        on_progress=None,
        sandbox: str = CODEX_SANDBOX,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.no_timeout = timeout_seconds is None or timeout_seconds <= 0
        self.on_progress = on_progress
        self.poll_interval = DEFAULT_POLL_INTERVAL_SECONDS
        self.bootstrap_timeout_seconds = None
        self.agent = None
        self.effort: str | None = None
        self.sandbox = sandbox
        self.last_call_meta: dict = {}
        self.on_session_created = None

    # ---------------------------------------------------------------- public

    @staticmethod
    def extract_session_id(text: str) -> str | None:
        """The codex thread id, read out of its own JSONL stream."""
        body = ensure_text(text)
        for pattern in _THREAD_ID_PATTERNS:
            match = re.search(pattern, body)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def clean_output(text: str) -> str:
        """The agent's final message, pulled out of the JSONL event stream.

        `codex exec --json` emits one JSON object per line; the answer lives in the last
        `{"type":"item.completed","item":{"type":"agent_message","text":...}}`. Anything
        that is not parseable JSON is passed through untouched — a codex build that prints
        plain text should degrade to "the text", not to "".
        """
        body = ensure_text(text)
        found = ""
        saw_json = False
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                event = json.loads(stripped)
            except ValueError:
                continue
            saw_json = True
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                message = item.get("text")
                if isinstance(message, str) and message.strip():
                    found = message.strip()
        if found:
            return found
        return "" if saw_json else body.strip()

    @staticmethod
    def extract_usage(text: str) -> dict | None:
        """Token counts codex reported, summed across every turn in the stream.

        Read from the same raw JSONL `clean_output` reads, and read for a different reason:
        that one keeps the last agent message, because only the final answer is wanted.
        This one ADDS UP every `turn.completed`, because a run that took three turns spent
        the tokens of all three, and keeping only the last would report a fraction of the
        bill as the whole of it.

        Returns None when the stream carries no usage, which is what a non-JSONL codex
        build produces. The caller keeps its chars//4 estimate in that case rather than
        gaining a zero, and the row says `token_source: estimated` — true, and visibly so.
        """
        total: dict | None = None
        for line in ensure_text(text).splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                event = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "turn.completed":
                continue
            total = merge_usage(total, normalize_usage(event.get("usage")))
        return total

    def run(
        self,
        prompt: str,
        session: dict,
        model: str | None = None,
        work_dir: str | None = None,
    ) -> dict:
        """One delegated call. Resumes the session in `session` when there is one."""
        guard = self._command_guard(work_dir)
        if guard is not None:
            return guard

        session = session if isinstance(session, dict) else {}
        resume_id = session.get("provider_session_id")
        cwd = self._resolve_work_dir(work_dir)
        handle, last_message_path = tempfile.mkstemp(
            prefix="codex-last-", suffix=".txt"
        )
        os.close(handle)
        last_message = Path(last_message_path)

        args = self._build_args(
            resume_id=resume_id, model=model, cwd=cwd, last_message=last_message
        )

        captured: dict = {"session_id": resume_id}

        def _saw_session(thread_id: str) -> None:
            if captured["session_id"] == thread_id:
                return
            captured["session_id"] = thread_id
            session["provider_session_id"] = thread_id
            if not self.on_session_created:
                return
            try:
                self.on_session_created(thread_id)
            except Exception:
                # The id is captured either way; a caller whose callback raises must not
                # lose the run that produced it. The executor's callback does not raise: it
                # keeps a failed write and retries it after the run, and names the loss on
                # the result (`session_persisted: false`).
                pass

        try:
            outcome = self._popen_capture(
                args, prompt, cwd, self._effective_timeout(), "agent", _saw_session
            )
        except FileNotFoundError as exc:
            last_message.unlink(missing_ok=True)
            return make_error(
                "command_not_found",
                f"command not found: {args[0]}",
                next_action=(
                    "Install the codex CLI, or set provider_command in "
                    ".workflow/second_agent.json to its absolute path."
                ),
                meta={"error": str(exc), **_argv_meta(args), "cwd": cwd},
            )
        except OSError as exc:
            last_message.unlink(missing_ok=True)
            return make_error(
                "unknown",
                str(exc),
                next_action="Inspect .workflow/data/sessions/<session>/logs and rerun.",
                meta={"error": type(exc).__name__, **_argv_meta(args), "cwd": cwd},
            )

        # Live capture in `_drain` is the normal path — the id arrives on stdout line 1, long
        # before the process ends. This is the net under it: a build that block-buffers its
        # stream hands the whole thing over at once on exit, and a line-by-line scan that
        # already finished would never have seen it.
        if not captured["session_id"]:
            recovered = self.extract_session_id(
                f"{ensure_text(outcome['stdout'])}\n{ensure_text(outcome['stderr'])}"
            )
            if recovered:
                _saw_session(recovered)

        meta = {
            "returncode": outcome["returncode"],
            **_argv_meta(args),
            "cwd": cwd,
            "duration_seconds": outcome["duration_seconds"],
            "idle_seconds": outcome["idle_seconds"],
            "provider_session_id": captured["session_id"],
            "resumed": bool(resume_id),
            "sandbox": self.sandbox,
            "stderr": ensure_text(outcome["stderr"]).strip(),
            "provider_version": codex_version(self.command),
        }
        # A resumed call that reports a different thread id opened a new thread instead:
        # the session silently lost its state. Copied onto last_call_meta as well, which is
        # what the usage row reads; `meta` alone never reaches telemetry. A call that named
        # no thread at all (it failed before codex printed one) changed nothing it can show.
        meta["thread_changed"] = (
            bool(resume_id) and bool(captured["session_id"]) and captured["session_id"] != resume_id
        )
        if isinstance(self.last_call_meta, dict):
            self.last_call_meta["resumed"] = meta["resumed"]
            self.last_call_meta["thread_changed"] = meta["thread_changed"]
            self.last_call_meta["provider_version"] = meta["provider_version"]
        tail = _error_tail(outcome["stderr"], outcome["stdout"])
        content = self._read_last_message(last_message, outcome["stdout"])
        last_message.unlink(missing_ok=True)

        if outcome["timed_out"]:
            meta["kill"] = outcome["kill"]
            meta["timeout_seconds"] = self.timeout_seconds
            if _matches(tail, _RATE_LIMIT_SIGNS):
                return make_error(
                    "rate_limited",
                    content or "codex ran out of provider quota mid-call",
                    next_action=(
                        "Second agent is out of quota. Wait for the limit to reset or "
                        "switch default_model in .workflow/second_agent.json — do NOT "
                        "resubmit immediately."
                    ),
                    meta=meta,
                )
            # What the retry will actually do depends on whether the id was captured, and
            # saying "the retry resumes" unconditionally was advice that could be false at
            # exactly the moment it mattered: a call killed before its first event has no
            # session, and the reader would be told the work is being continued when it is
            # being redone.
            resumable = (
                "The session was captured, so the retry resumes rather than starting over."
                if captured["session_id"]
                else "No session id was captured, so the retry starts a NEW thread and "
                "repeats the work — narrow the task rather than raising the timeout alone."
            )
            return make_error(
                "timeout",
                content or f"codex exceeded {self.timeout_seconds}s",
                next_action=(
                    "Increase timeout_seconds (0 = no limit) or narrow the task, then "
                    f"retry. {resumable}"
                ),
                meta=meta,
            )

        if outcome["returncode"] != 0:
            if _matches(tail, _RATE_LIMIT_SIGNS):
                return make_error(
                    "rate_limited",
                    content or "codex refused: provider quota exhausted",
                    next_action=(
                        "Second agent is out of quota. Wait for the limit to reset or "
                        "switch default_model in .workflow/second_agent.json."
                    ),
                    meta=meta,
                )
            if _matches(tail, _STREAM_FAIL_SIGNS):
                return make_error(
                    "streaming_failed",
                    content or "codex lost the provider stream mid-answer",
                    next_action=(
                        "Transient — the request itself was fine. Retry once; if it dies "
                        "again, split the task into two narrower delegated calls. Do NOT "
                        "wait for a quota reset, this is not a limit."
                    ),
                    meta=meta,
                )
            # stderr only: stdout is the agent's JSONL, tool output included, so a file it
            # read that mentions a sandbox failure turned an unrelated exit into this one.
            if _matches(_error_tail(outcome["stderr"]), _SANDBOX_SIGNS):
                if resume_id:
                    meta["reason"] = _RESUME_SANDBOX_REASON
                    return make_error(
                        "sandbox_unavailable",
                        content or "codex's Windows sandbox could not resume the thread",
                        next_action=_resume_sandbox_next_action(meta["provider_version"]),
                        meta=meta,
                    )
                return make_error(
                    "sandbox_unavailable",
                    content or "codex's Windows sandbox could not start the session",
                    next_action=_SANDBOX_NEXT_ACTION,
                    meta=meta,
                )
            if _matches(tail, _PERMISSION_SIGNS):
                return make_error(
                    "permission_denied",
                    content or "codex was refused access while reading the target",
                    next_action=(
                        "Grant read access to the path or move the context inside the "
                        "project, then retry."
                    ),
                    meta=meta,
                )
            return make_error(
                "unknown",
                content or f"codex exited {outcome['returncode']}",
                next_action=(
                    "Inspect .workflow/data/sessions/<session>/logs for the raw JSONL events "
                    "and rerun."
                ),
                meta=meta,
            )

        if not content.strip():
            return make_error(
                "empty_output",
                "codex returned no final message",
                next_action=(
                    "Rephrase the task, or check the raw JSONL events in "
                    ".workflow/data/sessions/<session>/logs — the run succeeded but produced "
                    "nothing."
                ),
                meta=meta,
                raw_tail=ensure_text(outcome["stdout"])[-500:],
            )

        # A run that answered but never named its thread cannot be continued: the follow-up
        # would open a fresh session holding none of the work. OpenCode fails the call in
        # the same situation rather than returning an orphan, and the parity matters —
        # `core/provider/executor.py` reads `provider_session_id` to decide whether its one bounded
        # continuation is even possible, and `main.py` refuses job recovery without it.
        #
        # Deliberately LAST, after timeout and returncode: those are different failures with
        # their own next_action, and labelling a rate-limited call `session_capture_failed`
        # would send the reader to fix the wrong thing. A resumed call cannot reach here —
        # `captured` starts holding `resume_id`. The answer is carried on `orphan_content`
        # so failing the call does not also destroy the text it produced.
        if not captured["session_id"]:
            return make_error(
                "session_capture_failed",
                "codex answered but never reported a thread id",
                next_action=(
                    "Rerun the task as a clean invocation. If it repeats, codex changed "
                    "the shape of its session event — check the raw JSONL in "
                    ".workflow/data/sessions/<session>/logs for the first line and widen "
                    "_THREAD_ID_PATTERNS in adapters/codex_adapter.py to match it."
                ),
                meta=meta,
                orphan_content=content[:4000],
            )

        return make_ok(content, meta)

    def probe(
        self,
        session_id: str | None = None,
        model: str | None = None,
        work_dir: str | None = None,
        timeout_seconds: int | None = 45,
    ) -> dict:
        """Liveness probe: a trivial prompt in a BRAND NEW codex thread.

        `session_id` is accepted for contract symmetry and deliberately ignored — the
        suspect session is the thing under test, so resuming it would prove nothing about
        whether codex itself still answers.

        Returns {'alive', 'reason', ...}. Its own short budget matters: a watchdog that
        can hang is not a watchdog.
        """
        guard = self._command_guard(work_dir)
        if guard is not None:
            return _sanitize_meta(
                {
                    "alive": False,
                    "reason": "command_not_found",
                    "error": guard["content"],
                    "returncode": None,
                    "duration_seconds": None,
                    "timed_out": False,
                }
            )

        cwd = self._resolve_work_dir(work_dir)
        args = self._build_args(
            resume_id=None, model=model, cwd=cwd, last_message=None
        )
        budget = max(5, int(timeout_seconds or 45))

        try:
            outcome = self._popen_capture(
                args, "PING. Reply PONG.", cwd, budget, "probe", None
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

        tail = _error_tail(outcome["stderr"], outcome["stdout"])
        rate_limited = _matches(tail, _RATE_LIMIT_SIGNS)
        stream_failed = _matches(tail, _STREAM_FAIL_SIGNS)
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

        if not tail and outcome["timed_out"]:
            rate_limited = None

        return _sanitize_meta(
            {
                "alive": reason == "probe_ok",
                "reason": reason,
                "rate_limited": rate_limited,
                "no_probe_output": not tail,
                "stream_failed": stream_failed,
                "returncode": outcome["returncode"],
                "duration_seconds": outcome["duration_seconds"],
                "timed_out": outcome["timed_out"],
                "stderr_tail": ensure_text(outcome["stderr"]).strip()[-500:],
            }
        )

    # --------------------------------------------------------------- private

    def _command_guard(self, work_dir: str | None) -> dict | None:
        """Refuse loudly when the configured command is not a codex binary.

        `config/settings.py` defaults `provider_command` to opencode's binary for EVERY
        provider, so a project that selects codex without also setting `provider_command`
        arrives here pointed at the wrong CLI. Running it would half-work and produce
        evidence from a provider nobody asked for; that silent substitution is exactly
        what `UnknownProviderError` refuses to do one layer up.
        """
        name = Path(str(self.command or "")).name.lower()
        stem = name.split(".")[0]
        if stem and stem != "codex" and "codex" not in stem:
            return make_error(
                "command_not_found",
                f"provider is codex but provider_command is '{self.command}'",
                next_action=(
                    'Set BOTH "provider": "codex" and "provider_command": "codex" in '
                    ".workflow/second_agent.json — that file is the only place a provider "
                    "is selected. `runtime.second_agent` in .workflow/config.json is a "
                    "hint and does not choose one."
                ),
                meta={
                    "provider": self.adapter,
                    "provider_command": str(self.command),
                    "cwd": self._resolve_work_dir(work_dir),
                },
            )
        return None

    def _build_args(
        self,
        *,
        resume_id: str | None,
        model: str | None,
        cwd: str | None,
        last_message: Path | None,
    ) -> list[str]:
        """argv for one call. `-` puts the prompt on stdin, so argv stays short.

        Both branches carry the declared read boundary — declared, not enforced; see the
        module docstring. Kept symmetric anyway: the flags are what turn into a real boundary
        if codex ever gates shell reads, and a resumed thread that skipped them would then be
        a hole opening only on the second call of a session, the hardest kind to notice.
        """
        command = osutil.resolve_exe(self.command)
        boundary = codex_permission_args()
        if resume_id:
            # `resume` accepts a NARROWER option set than `exec` itself, and the ones it
            # rejects are rejected by the argument parser — before the model is reached, so
            # the call dies with `unexpected argument` and exit 2 rather than anything the
            # error classifier below can read. Verified against codex-cli 0.147.0 through
            # 0.160.0: AFTER `resume` there is no -C, no --sandbox, and no --color. Only
            # --json, -c, -o, -m and --skip-git-repo-check survive there.
            #
            # `--sandbox` is an `exec` option, so it goes BEFORE the subcommand, where the
            # parser takes it. It used to ride `-c sandbox_mode=...` instead, and from codex
            # 0.155.0 that form, together with the `default_permissions` profile below, makes
            # the Windows elevated sandbox refuse every resumed thread ("requires effective
            # `:root` read access") while new threads work (CASE-016, CASE-021). The flag
            # form resumes on 0.154.0 through 0.160.0, and on 0.163.0 (DEC-058), with the
            # same boundary.
            #
            # The call is rooted by the process cwd, which `_popen_capture` sets on both
            # branches, so dropping -C here changes nothing about where codex looks; -C
            # before `resume` was tried and changes nothing about the refusal either. The
            # filesystem read denies ride -c, so a resumed call cannot end up looser than
            # the one that opened the thread.
            args = [
                command,
                "exec",
                "--sandbox",
                self.sandbox,
                "resume",
                resume_id,
                "-",
                "--json",
                "--skip-git-repo-check",
                *_APPROVAL_ARGS,
                *boundary,
            ]
        else:
            args = [
                command,
                "exec",
                "-",
                "--json",
                "--color",
                "never",
                "--skip-git-repo-check",
                "--sandbox",
                self.sandbox,
                *_APPROVAL_ARGS,
                *boundary,
            ]
            if cwd:
                args.extend(["-C", cwd])
        if last_message is not None:
            args.extend(["-o", str(last_message)])
        if model:
            args.extend(["-m", model])
        # `-c` is declared global across subcommands, so this rides both branches —
        # including `resume`, whose narrower option set still accepts -c. A thread
        # resumed without it would silently drop back to the config-file effort.
        from config.providers import effort_args

        args.extend(effort_args("codex", self.effort))
        return args

    def _popen_capture(
        self,
        args: list[str],
        prompt: str,
        cwd: str | None,
        timeout: int | None,
        phase: str,
        on_session,
    ) -> dict:
        """Run `args`, feeding `prompt` on stdin and draining both streams on threads.

        Threaded draining is what makes the heartbeat and the timeout kill possible:
        `communicate()` blocks until the process ends, which is precisely the case that
        needs handling.
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
            stdin=subprocess.PIPE,
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
                    # Both streams. `--json` puts the id on stdout and leaves stderr empty,
                    # but scanning only stdout meant a build that banners to stderr lost the
                    # session with nothing to show for it — and the cost of looking is one
                    # regex per line on a stream that is normally empty.
                    if on_session and not seen_session["done"]:
                        thread_id = CodexAdapter.extract_session_id(line)
                        if thread_id:
                            seen_session["done"] = True
                            on_session(thread_id)
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

        # Recorded, not swallowed. A prompt this adapter could not hand over is the one
        # failure mode argv providers cannot have and this one can: the process is already
        # running, so the write fails against a pipe the child closed — typically because
        # it rejected the input — and the call then proceeds to wait on a process that was
        # never given its question. Discarding the exception made that indistinguishable
        # from an empty answer, which is why "prompt too long" has been guesswork rather
        # than something the run meta could state.
        stdin_error: str | None = None
        try:
            proc.stdin.write(prompt or "")
        except Exception as exc:  # noqa: BLE001 - the reason is data here, not control flow
            stdin_error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                proc.stdin.close()
            except Exception as exc:  # noqa: BLE001
                # A close that fails after a successful write is the same broken pipe seen
                # one step later; only report it when the write itself looked fine.
                if stdin_error is None:
                    stdin_error = f"{type(exc).__name__}: {exc} (on close)"
        prompt_chars = len(prompt or "")

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
                # Carried into THIS dict rather than written when they are learned, because
                # this assignment replaces `last_call_meta` wholesale — anything set on the
                # old object between the Popen above and here is discarded. The stdin write
                # happens in that window, so recording it earlier looked correct and
                # silently reached nothing.
                "prompt_chars": prompt_chars,
                "stdin_write_failed": stdin_error,
                # Parsed from the stdout captured just above, while it is still the raw
                # event stream. After this point the only survivor is the cleaned final
                # message, and the usage events are not in it.
                "provider_usage": self.extract_usage(outcome["stdout"]),
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

    @staticmethod
    def _read_last_message(last_message: Path | None, stdout: str) -> str:
        """The final answer: the `-o` file first, the event stream as the fallback.

        Two sources because they fail differently. The file is codex's own statement of
        what the final message was, but it is not written when the run dies early; the
        stream survives that, and carries the same text one event deeper.
        """
        raw = ""
        if last_message is not None:
            try:
                raw = last_message.read_text(encoding="utf-8").strip()
            except OSError:
                raw = ""
        return raw or CodexAdapter.clean_output(stdout)

    def _effective_timeout(self) -> int | None:
        return None if self.no_timeout else self.timeout_seconds

    @staticmethod
    def _resolve_work_dir(work_dir: str | None) -> str | None:
        return str(Path(work_dir).resolve()) if work_dir else None
