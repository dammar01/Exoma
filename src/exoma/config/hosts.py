"""Host profiles: what differs between the main-agent hosts, held as data.

One contract source (hosts/claude/CLAUDE.md) is rendered for every host from these
profiles: the runner call, how a long runner call is waited for, how a question reaches
the user, where a skill is read, and which host-only passages apply. The hooks read the
same facts from `host-profile.json` beside them, written at install from `hook_profile`,
so the contract, the [INTENT HINT] and a gate refusal name one runner call per host
instead of three spellings that drift (docs/reference.md, "Host profiles").

Rendering, in `render_contract`:
- `<!-- HOST:a,b:START -->...<!-- HOST:a,b:END -->` keeps its text only for the named hosts;
  the markers never ship.
- `{{RUNNER}}`, `{{SHELL_TOOL}}`, `{{DELEGATED_RUN}}`, `{{ASK_USER}}`, `{{SKILL_PATH}}`
  take the profile's values.
- `<!-- rule:<id> -->` gains this host's enforcement level from config/rules.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# What every non-Claude host's hook script needs beside it: the shared policy it imports,
# the intent map it loads, the profile it reads. One list for install, check and doctor.
HOST_PROFILE_FILE = "host-profile.json"
HOOK_DATA_FILES = ("workflow_policy.py", "intent-map.json", HOST_PROFILE_FILE)

# Keys a host's read tool may carry a line range in. Agy's names are not calibrated against
# a live payload yet; an unknown key leaves the limit unknown (`None`), which is what every
# Agy read had before, so a wrong guess costs a refusal, never a pass.
_RANGE_KEYS = {
    "limit": ("limit", "Limit", "max_lines", "MaxLines"),
    "start": ("StartLine", "start_line", "startLine", "offset", "Offset"),
    "end": ("EndLine", "end_line", "endLine"),
}


@dataclass(frozen=True)
class HostProfile:
    name: str
    # "bare": `bash` as the host's shell resolves it (Claude Code's Bash tool is Git Bash on
    # Windows). "resolved": this machine's bash by absolute path, behind PowerShell's call
    # operator on Windows, because there the host's shell is PowerShell and a bare `bash`
    # can be WSL's, which cannot read E:/ paths.
    runner_form: str
    shell_tool: str
    # native_tool: the host has a background-task tool. auto_async: the host moves a long
    # call to the background by itself. blocking: the call blocks until it returns.
    background: str
    delegated_run: str
    ask_user: str
    skill_path: str
    read_range_keys: dict = field(default_factory=lambda: dict(_RANGE_KEYS))
    command_prefix: str = "/."


PROFILES: dict[str, HostProfile] = {
    "claude": HostProfile(
        name="claude",
        runner_form="bare",
        shell_tool="the Bash tool",
        background="native_tool",
        delegated_run=(
            "Run the delegated runner through Claude's background-task tool (`run_in_background: true`), "
            "NOT a blocking foreground call and NOT shell `&`/`Start-Process`. Keep the task ID and collect "
            "the result of that same task; a timeout while collecting output is not permission to start a "
            "new invocation."
        ),
        ask_user=(
            "Renderer = the AskUserQuestion tool. Hard limits (break one = the call is refused): MAX 4 "
            "questions per call, 2-4 options per question, `header` MAX 12 characters, `multiSelect: true` "
            "only for multi-choice. The ` :: ` part becomes the option's `description`. The user always gets "
            "\"Other\" — do not add an \"other\" option yourself. This tool is NOT available in subagents: "
            "render in the main thread; do NOT delegate asking the user to a Task/subagent."
        ),
        skill_path="~/.claude/skills/<name>.md",
    ),
    "agy": HostProfile(
        name="agy",
        runner_form="resolved",
        shell_tool="`run_command` with the sandbox bypassed (`BypassSandbox`)",
        background="auto_async",
        delegated_run=(
            "Agy moves a long `run_command` to the background by itself: when it does, wait for that task's "
            "completion notice and read its output; the runner still blocks until it prints its JSON. Never "
            "start a second runner call for the same task, and never read `.workflow/data` by hand while it "
            "runs. For live diagnosis use the bound `.workflow/inspect.sh --session <MAIN_SESSION_ID>` snapshot."
        ),
        ask_user=(
            "Renderer = `ask_question`, one call per open question, each with its options and their "
            "descriptions. At most 4 options per question; the user can always answer in their own words, so "
            "do not add an \"other\" option yourself. Ask in the main session, never from a delegated call."
        ),
        skill_path="~/.gemini/antigravity-cli/skills/.<name>/SKILL.md",
    ),
    "codex": HostProfile(
        name="codex",
        command_prefix="$exoma-",
        runner_form="resolved",
        shell_tool="`exec_command`",
        background="auto_async",
        delegated_run=(
            "Run it with `exec_command` using the parameters available in the current tool schema. "
            "When it returns a session_id, collect that same process through `write_stdin` until JSON arrives. "
            "In code mode, `functions.exec` may separately return a cell ID: resume that cell with "
            "`functions.wait`; a cell ID is not an exec_command session_id. Follow the CURRENT host "
            "permissions, not a sandbox assumed by this contract. "
            "If the runner is allowed normally, omit escalation arguments. With approval_policy=never "
            "or when the host forbids escalation, omit `sandbox_permissions`, `justification`, and "
            "`prefix_rule`; report a blocked boundary if normal execution is unavailable. Only when "
            "the runner needs broader permissions AND the host allows requesting them, use "
            "`sandbox_permissions: \"require_escalated\"` with a one-line `justification` and a narrow "
            "`prefix_rule` (the bash path and this project's `.workflow/run.sh`). Name the configured "
            "provider and the kind of repository context sent to its service, using available metadata "
            "and existing user authorization for this provider, project and context; never guess an "
            "endpoint or expose credentials. Ask only "
            "for authorization that is actually missing. Escalate only the delegated runner call. "
            "An auto-review denial BEFORE process creation is a host approval denial, not a run.sh or "
            "job failure: the runner has no JSON to return. Report its rationale; no retry via another "
            "shell, prefix, or weaker policy. Continue only through a materially safer authorized path "
            "or new explicit authorization, still subject to host policy. Distinguish this from a shell "
            "failure without JSON, `network_denied` after launch, or a JSON job error. Exoma cannot "
            "override host review, and a prefix rule does not guarantee approval. If the session yields "
            "before it returns, wait on that same `exec_command` "
            "session; never start a second runner call for the same task, and never read `.workflow/data` "
            "by hand while it runs. For live diagnosis use `.workflow/inspect.sh --session <MAIN_SESSION_ID>`; it only reads a bounded snapshot and does not start another provider."
        ),
        ask_user=(
            "Renderer = the user-input tool when Codex offers one, otherwise ask in the reply: one question "
            "per open question, its options numbered with their descriptions, then stop and wait for the "
            "answer. Do not add an \"other\" option yourself."
        ),
        skill_path="~/.codex/skills/exoma-<name>/SKILL.md",
    ),
    "opencode": HostProfile(
        name="opencode",
        runner_form="resolved",
        shell_tool="the `bash` tool",
        background="blocking",
        delegated_run=(
            "OpenCode cannot run a call in the background: run the runner in the foreground with a long timeout "
            "(600000 ms) and wait; it blocks until it prints its JSON. Never start a second runner call for "
            "the same task, and never read `.workflow/data` by hand while it runs. For live diagnosis use `.workflow/inspect.sh --session <MAIN_SESSION_ID>`; it only reads a bounded snapshot and does not start another provider. No output at all means the "
            "call did not reach Git Bash: check that it starts with the exact prefix above."
        ),
        ask_user=(
            "Renderer = your reply: one question per open question, its options numbered with their "
            "descriptions, then stop and wait for the answer. Do not add an \"other\" option yourself."
        ),
        skill_path="~/.config/opencode/skills/exoma-<name>/SKILL.md",
    ),
}

HOSTS = tuple(PROFILES)


def runner_prefix(host: str) -> str:
    """What the runner call starts with on `host`, for this machine.

    `bash` for a host whose shell resolves it; otherwise the absolute bash, behind `&` on
    Windows (where the host's shell is PowerShell and a quoted path runs only with the call
    operator), or plain `bash` when none is found.
    """
    if PROFILES[host].runner_form == "bare":
        return "bash"
    from pathlib import Path

    from exoma.utils import osutil

    found = osutil.find_bash()
    if not found:
        return "bash"
    path = Path(found).as_posix()
    return f"& '{path}'" if osutil.IS_WINDOWS else f"'{path}'"


def runner_call(host: str, root: str = "<work_dir>") -> str:
    """The runner call up to its command, as the contract, the hint and a refusal spell it."""
    return f"{runner_prefix(host)} '{root}/.workflow/run.sh'"


def hook_profile(host: str, auto_intent: bool = True) -> dict:
    """What a host's hooks read from host-profile.json."""
    profile = PROFILES[host]
    return {
        "host": host,
        "auto_intent": auto_intent,
        "runner_prefix": runner_prefix(host),
        "background": profile.background,
        "command_prefix": profile.command_prefix,
        "read_range_keys": {k: list(v) for k, v in profile.read_range_keys.items()},
    }


def hook_profile_text(host: str, auto_intent: bool = True) -> str:
    return json.dumps(hook_profile(host, auto_intent), indent=2, ensure_ascii=False) + "\n"


_HOST_SECTION = re.compile(
    r"(?P<lead>[ \t]*)<!--\s*HOST:(?P<names>[a-z,]+):START\s*-->(?P<nl>[ \t]*\n)?"
    r"(?P<body>.*?)(?:(?<=\n)[ \t]*)?<!--\s*HOST:(?P=names):END\s*-->(?P<trail>[ \t]*\n?)",
    re.DOTALL,
)
RULE_MARKER = re.compile(r"<!--\s*rule:(?P<id>[a-z0-9_.-]+)\s*-->")
PLACEHOLDERS = ("{{RUNNER}}", "{{SHELL_TOOL}}", "{{DELEGATED_RUN}}", "{{ASK_USER}}", "{{SKILL_PATH}}",
                "{{CMD_PREFIX}}", "{{HOST_CONFIG}}", "{{MAIN_HOST_OPTION}}")
COMMAND_IDS = frozenset(("analyze", "caveman", "commit", "doctor", "execute", "explore", "help", "init",
                         "local", "memory", "plan", "promote", "provider", "sweep", "upgrade", "verify", "verify-browser"))
COMMAND_TOKEN = re.compile(r"\{\{CMD:([a-z][a-z0-9_-]*)\}\}")


def command_invocation(host: str, command_id: str) -> str:
    """User-facing invocation; runner/protocol command IDs remain bare."""
    if command_id not in COMMAND_IDS:
        raise ValueError(f"unknown workflow skill: {command_id}")
    return PROFILES[host].command_prefix + command_id


def render_commands(text: str, host: str) -> str:
    text = COMMAND_TOKEN.sub(lambda m: command_invocation(host, m.group(1)), text)
    return text.replace("{{CMD_PREFIX}}", PROFILES[host].command_prefix)


def format_guidance(text: str, host: str) -> str:
    """Format authored guidance only; callers must never pass delegated evidence."""
    # Template expansion belongs to source rendering, never runtime strings. Paths and
    # diagnostics may legitimately contain literal {{CMD:...}} text, including unknown IDs.
    return re.sub(r"(?<![\w/~])/\.([a-z][a-z0-9_-]*)\b",
                  lambda m: command_invocation(host, m.group(1)) if m.group(1) in COMMAND_IDS else m.group(0), text)


def present_result(result: dict, host: str | None, command: str) -> dict:
    """Copy only first-party guidance fields. Evidence, IDs and verdicts are opaque."""
    if host is None:
        return result
    def guidance(value):
        if isinstance(value, str):
            return format_guidance(value, host)
        if isinstance(value, list):
            return [guidance(item) if isinstance(item, (str, list)) else item for item in value]
        return value
    shown = dict(result)
    for key in ("next_action",):
        if key in shown:
            shown[key] = guidance(shown[key])
    # All content is opaque, including local-command content: it may embed subprocess
    # output, command lines, paths or user diagnostics even when there is no digest.
    if isinstance(shown.get("meta"), dict):
        shown["meta"] = dict(shown["meta"])
        for key in ("next_action",):
            if key in shown["meta"]:
                shown["meta"][key] = guidance(shown["meta"][key])
    return shown


def _host_section(match: "re.Match", host: str) -> str:
    """One HOST section as `host` reads it.

    A block section (START ending its line) is whole lines: kept, its body stands as written
    (the marker lines and an indented END go); dropped, nothing is left. An inline section
    sits inside a sentence: kept, the text around the markers keeps its spaces ("or <END>the
    call" reads "or the call"); dropped, the words on either side stay one space apart."""
    kept = host in match.group("names").split(",")
    if match.group("nl") is not None:
        return match.group("body") if kept else ""
    lead, trail = match.group("lead"), match.group("trail")
    if kept:
        return lead + match.group("body") + trail
    if "\n" in trail:
        return trail.lstrip(" \t")
    following = match.string[match.end():match.end() + 1]
    if following and following in ",.;:!?)]}":
        return ""  # "x <S>y<E>, z" reads "x, z"
    return lead or trail


def select_host_sections(text: str, host: str) -> str:
    """Keep each HOST section naming `host`, drop the others; the markers never ship."""
    return _HOST_SECTION.sub(lambda m: _host_section(m, host), text)


def render_contract(text: str, host: str) -> str:
    """The shared contract text as `host` reads it."""
    from exoma.config.rules import level_of

    profile = PROFILES[host]
    text = select_host_sections(text, host)
    values = {
        "{{RUNNER}}": runner_prefix(host),
        "{{SHELL_TOOL}}": profile.shell_tool,
        "{{DELEGATED_RUN}}": profile.delegated_run,
        "{{ASK_USER}}": profile.ask_user,
        "{{SKILL_PATH}}": profile.skill_path,
        "{{CMD_PREFIX}}": profile.command_prefix,
        "{{HOST_CONFIG}}": {"claude": "~/.claude", "codex": "~/.codex",
                            "agy": "~/.gemini/antigravity-cli", "opencode": "~/.config/opencode"}[host],
        "{{MAIN_HOST_OPTION}}": " --main-host codex" if host == "codex" else "",
        "{{QUESTION_TOOL}}": "AskUserQuestion" if host == "claude" else "the available user-input tool",
        "{{MULTI_CHOICE}}": "AskUserQuestion multiSelect" if host == "claude" else
                            "the available user-input tool (ask per endpoint or accept an explicit list)",
    }
    for name, value in values.items():
        text = text.replace(name, value)
    text = render_commands(text, host)
    return RULE_MARKER.sub(lambda m: f"<!-- rule:{m.group('id')} | {host}: {level_of(m.group('id'), host)} -->", text)
