"""Claude Code CLI as a second_agent provider.

Built on CodexAdapter because the transport is the same one: the prompt goes in on STDIN
(`claude -p` with no prompt argument), the session id arrives in the first event of the
stream, and continuing is a flag on the same command. What differs is the argv and the
shape of the stream, and that is all this module overrides.

The read-only boundary is argv, asserted on every call, resumed ones included:

    --tools Read,Grep,Glob     the ONLY tools the session has. Not a deny list: a tool that
                               is not named does not exist in the session, so a write tool
                               added by a later Claude Code release stays unavailable.
    --setting-sources ""       no user, project or local settings file is loaded: the
                               user's own hooks (Exoma's host hooks among them), permission
                               rules and CLAUDE.md never reach the delegated process.
    --strict-mcp-config        no MCP server from any config; none is passed.

Probed on Claude Code 2.1.316 under OAuth login (`apiKeySource: none`): the init event
listed exactly Glob, Grep and Read, a request to create a file was refused for lack of a
write tool and no file appeared, and nothing from ~/.claude/CLAUDE.md or the SessionStart
hook was in context. `--bare` would isolate further but accepts only ANTHROPIC_API_KEY,
which would lock out every subscription user, so it is not used.

Reads are NOT bounded: Read can open any file the process can, `.env` included, which is
what `boundary` in config/providers.py records. WORKFLOW_AGENT_ROLE=second_agent rides the
environment (CodexAdapter._popen_capture) for any hook that still loads.

The final answer is the `result` field of the stream's closing `{"type":"result"}` event;
`stream-json` (which needs `--verbose` under `-p`) is used rather than `json` so the
session id is captured from the opening `init` event while the call is still running.
"""

import json
import os
from pathlib import Path

from exoma.adapters.providers.codex_adapter import CodexAdapter, make_error
from exoma.adapters.shared.usage import merge_usage, normalize_usage
from exoma.config.settings import DEFAULT_TIMEOUT_SECONDS
from exoma.utils import osutil
from exoma.utils.parser import ensure_text

CLAUDE_COMMAND = os.getenv("CLAUDE_COMMAND", "claude")
# The whole tool set of a delegated call. Read-only by construction: see the docstring.
READ_ONLY_TOOLS = "Read,Grep,Glob"


def claude_version(command: str | None) -> str | None:
    from exoma.core.provider.versions import read_version

    return read_version(command or "claude")


def _events(text: str):
    for line in ensure_text(text).splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


class ClaudeAdapter(CodexAdapter):
    adapter = "claude"

    def __init__(
        self,
        command: str = CLAUDE_COMMAND,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        on_progress=None,
    ) -> None:
        super().__init__(command=command, timeout_seconds=timeout_seconds, on_progress=on_progress)
        # Reported in call meta where codex reports its sandbox mode.
        self.sandbox = "tools:" + READ_ONLY_TOOLS

    @staticmethod
    def clean_output(text: str) -> str:
        """The `result` of the closing result event; the last assistant text otherwise."""
        result = ""
        assistant = ""
        saw_json = False
        for event in _events(text):
            saw_json = True
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                result = event["result"].strip()
            message = event.get("message") if event.get("type") == "assistant" else None
            if isinstance(message, dict):
                parts = [
                    part.get("text", "")
                    for part in message.get("content") or ()
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                joined = "".join(parts).strip()
                if joined:
                    assistant = joined
        if result or assistant:
            return result or assistant
        return "" if saw_json else ensure_text(text).strip()

    @staticmethod
    def extract_usage(text: str) -> dict | None:
        """The result event's usage: Claude reports the whole call there, once."""
        total = None
        for event in _events(text):
            if event.get("type") == "result":
                total = merge_usage(None, normalize_usage(event.get("usage")))
        return total

    @staticmethod
    def _read_last_message(last_message: Path | None, stdout: str) -> str:
        return ClaudeAdapter.clean_output(stdout)

    def run(self, prompt, session, model=None, work_dir=None):
        result = super().run(prompt, session, model=model, work_dir=work_dir)
        # The stream marks a failed turn with `is_error` while the process may exit 0.
        if isinstance(result, dict) and result.get("ok"):
            meta = result.get("meta") or {}
            if meta.get("returncode") == 0 and self._last_is_error:
                return make_error(
                    "unknown",
                    result.get("content") or "claude reported an error result",
                    next_action="Inspect .workflow/data/sessions/<session>/logs and rerun.",
                    meta=meta,
                )
        return result

    _last_is_error = False

    def _popen_capture(self, args, prompt, cwd, timeout, phase, on_session):
        outcome = super()._popen_capture(args, prompt, cwd, timeout, phase, on_session)
        self._last_is_error = any(
            event.get("type") == "result" and event.get("is_error") is True
            for event in _events(outcome.get("stdout") or "")
        )
        if isinstance(self.last_call_meta, dict):
            self.last_call_meta["provider_version"] = claude_version(self.command)
        return outcome

    def _command_guard(self, work_dir):
        name = Path(str(self.command or "")).name.lower()
        stem = name.split(".")[0]
        if stem and "claude" not in stem:
            return make_error(
                "command_not_found",
                f"provider is claude but provider_command is '{self.command}'",
                next_action=(
                    'Set BOTH "provider": "claude" and "provider_command": "claude" in '
                    ".workflow/second_agent.json."
                ),
                meta={"provider": self.adapter, "provider_command": str(self.command),
                      "cwd": self._resolve_work_dir(work_dir)},
            )
        return None

    def _build_args(self, *, resume_id, model, cwd, last_message):
        """argv for one call; the prompt rides stdin. Both branches carry the boundary."""
        args = [
            osutil.resolve_exe(self.command),
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--tools",
            READ_ONLY_TOOLS,
            "--setting-sources",
            "",
            "--strict-mcp-config",
        ]
        # The second-agent contract, installed whole by `exoma install` (config/providers.py
        # `instructions`). Passed per call because no CLAUDE.md is loaded; absent before an
        # install, and the prompt's own [WORKFLOW_AGENT] protocol still carries the call.
        contract = second_agent_contract()
        if contract is not None:
            args.extend(["--append-system-prompt-file", str(contract)])
        if resume_id:
            args.extend(["--resume", resume_id])
        if model:
            args.extend(["--model", model])
        from exoma.config.providers import effort_args

        args.extend(effort_args("claude", self.effort))
        return args


def second_agent_contract() -> Path | None:
    path = osutil.agent_home() / ".claude" / "exoma" / "second-agent.md"
    return path if path.is_file() else None


# --- install module (config/providers.py `install_module`) ------------------------------
#
# Here rather than in adapters/install/: claude has no config file for the installer to
# merge and no project file to install, so all three answers are "nothing to do", and the
# boundary is the argv above. Kept beside that argv so the report cannot drift from it.


def load_config(path):
    from exoma.core.workspace.workspace_paths import read_json_file

    return read_json_file(path)


def merge_policy(current: dict, incoming: dict, warn) -> tuple[dict, int, int]:
    """Additive, like codex: nothing claude enforces lives in a file."""
    merged = dict(current or {})
    added = 0
    for key, value in (incoming or {}).items():
        if key not in merged:
            merged[key] = value
            added += 1
    return merged, added, 0


def install_project_config(project_root) -> dict:
    """No project file: writes are bounded per call by the tool list, reads are not."""
    return {
        "path": None,
        "status": "not_enforceable",
        "keys_added": 0,
        "permissions_enforced": 0,
        "permissions_declared": 0,
        "warnings": [
            "claude's second_agent runs with only Read, Grep and Glob (`--tools`), so it "
            "cannot write; it CAN read every file the process can, `.env` included",
            "opencode is the provider whose read boundary is enforced; switch to it for a "
            "project whose secrets must stay unreadable by the second agent",
        ],
    }
