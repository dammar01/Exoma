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
            "runs."
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
        runner_form="resolved",
        shell_tool="`exec_command`",
        background="blocking",
        delegated_run=(
            "Run it with `exec_command` and a long timeout (10 minutes): the runner blocks until it prints "
            "its JSON. Request it with `sandbox_permissions: \"require_escalated\"`, a one-line "
            "`justification`, and a `prefix_rule` matching the runner call exactly as you run it (its bash "
            "path, then this project's `.workflow/run.sh`) so the user approves the runner once: the second "
            "agent it starts needs the network, which your sandbox "
            "denies (os error 10013, `network_denied`), and it keeps its own read-only sandbox. Escalate "
            "only the runner call. If the session yields before it returns, wait on that same `exec_command` "
            "session; never start a second runner call for the same task, and never read `.workflow/data` "
            "by hand while it runs."
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
            "the same task, and never read `.workflow/data` by hand while it runs. No output at all means the "
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


def hook_profile(host: str) -> dict:
    """What a host's hooks read from host-profile.json."""
    profile = PROFILES[host]
    return {
        "host": host,
        "runner_prefix": runner_prefix(host),
        "background": profile.background,
        "read_range_keys": {k: list(v) for k, v in profile.read_range_keys.items()},
    }


def hook_profile_text(host: str) -> str:
    return json.dumps(hook_profile(host), indent=2, ensure_ascii=False) + "\n"


_HOST_SECTION = re.compile(
    r"(?P<lead>[ \t]*)<!--\s*HOST:(?P<names>[a-z,]+):START\s*-->(?P<nl>[ \t]*\n)?"
    r"(?P<body>.*?)(?:(?<=\n)[ \t]*)?<!--\s*HOST:(?P=names):END\s*-->(?P<trail>[ \t]*\n?)",
    re.DOTALL,
)
RULE_MARKER = re.compile(r"<!--\s*rule:(?P<id>[a-z0-9_.-]+)\s*-->")
PLACEHOLDERS = ("{{RUNNER}}", "{{SHELL_TOOL}}", "{{DELEGATED_RUN}}", "{{ASK_USER}}", "{{SKILL_PATH}}")


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
    }
    for name, value in values.items():
        text = text.replace(name, value)
    return RULE_MARKER.sub(lambda m: f"<!-- rule:{m.group('id')} | {host}: {level_of(m.group('id'), host)} -->", text)
