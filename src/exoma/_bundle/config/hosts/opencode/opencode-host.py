"""OpenCode lifecycle hooks for the Exoma main-agent host. Standalone stdlib only.

Called by the plugin shim `~/.config/opencode/plugins/exoma.js` once per event, with the
payload as JSON on stdin; prints one JSON object. The policy itself is `workflow_policy`,
shared with every other host, so a gate behaves the same whichever host runs it.

Events (OpenCode has no session-start hook a plugin can answer, so the first prompt of a
session binds it):
    UserPromptSubmit  {session_id, cwd, prompt}            -> {context?}
    System            {session_id, cwd}                    -> {context?}  (every model call)
    PreToolUse        {session_id, cwd, tool_name, tool_input} -> {deny?|warn?}
"""

import datetime
import json
import os
import random
import re
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import workflow_policy as wp  # noqa: E402

EVENTS = {"UserPromptSubmit", "System", "PreToolUse"}
_SKILL = re.compile(r"\$exoma-(explore|plan|analyze|verify-browser|verify|execute|commit)\b", re.I)
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.M)
SHELL_TOOLS = {"bash"}
READ_TOOLS = {"read"}
WRITE_TOOLS = {"write", "edit", "multiedit"}
PATCH_TOOLS = {"patch", "apply_patch"}


def _home():
    return Path(os.environ.get("AGENT_HOME") or Path.home()) / ".config" / "opencode"


def _root(cwd):
    if not cwd:
        return None
    path = Path(cwd).resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".workflow").is_dir():
            return candidate
    return None


def _entry_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id or ""))
    return _home() / "exoma" / "sessions" / (safe + ".json") if safe else None


def _write_entry(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    temp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temp, path)


def _bound(session_id):
    path = _entry_path(session_id)
    data = wp.read_json(str(path)) if path else None
    if not isinstance(data, dict) or not data.get("main_session_id") or not data.get("cwd"):
        return None
    if not (Path(data["cwd"]) / ".workflow").is_dir():
        return None
    return data


def _bind(session_id, cwd):
    """The session's binding, created on its first prompt in a project with .workflow/."""
    known = _bound(session_id)
    if known:
        return known, True
    root = _root(cwd)
    if not session_id or root is None:
        return None, False
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S%f")[:17]
    main_id = "main_%s_%s_%04x" % (root.name or "root", stamp, random.getrandbits(16))
    entry = {"main_session_id": main_id, "cwd": str(root), "host": "opencode", "hint": None}
    _write_entry(_entry_path(session_id), entry)
    return entry, False


def _contract_text():
    """The rendered contract, read here rather than by the agent.

    A `read` tool call on ~/.config/opencode/exoma/CONTRACT.md sits outside the project, so
    OpenCode asks for it under `external_directory` (and refuses it outright where the
    project's opencode.json denies that). Carried in the system prompt, it costs no tool call
    and no prompt; the second agent never gets it (the plugin and `handle` skip that role)."""
    try:
        text = (_home() / "exoma" / "CONTRACT.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text.strip() or None


def _binding_text(entry, reused, contract=None):
    text = wp.session_binding(entry["main_session_id"], entry["cwd"], "opencode", reused)
    if contract:
        text += ("\n[EXOMA OPENCODE HOST] The Exoma contract follows; do not read "
                 "~/.config/opencode/exoma/CONTRACT.md again. Use the /exoma-<command> commands "
                 "or exoma-<command> skills; the contract's /. names refer to those here.")
    else:
        text += ("\n[EXOMA OPENCODE HOST] Read ~/.config/opencode/exoma/CONTRACT.md before workflow work. "
                 "Use the /exoma-<command> commands or exoma-<command> skills; the contract's /. names "
                 "refer to those here.")
    if entry.get("hint"):
        text += "\n" + entry["hint"]
    if contract:
        text += "\n\n" + contract
    return text


def _unwrap(prompt):
    """The prompt without one pair of quotes wrapping all of it.

    `opencode run '<message>'` on Windows hands the message over with its quotes still on
    (seen on OpenCode 1.19), so a command at the very start would sit behind a `"`. Only a pair
    that encloses the whole text is taken off; quotes inside it are the user's.
    """
    text = str(prompt or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _normalize_skill_mentions(prompt):
    return _SKILL.sub(lambda m: "/." + m.group(1).lower(), wp.user_text(_unwrap(prompt)))


def user_prompt_submit(payload):
    sid = payload.get("session_id")
    entry, reused = _bind(sid, payload.get("cwd"))
    if not entry:
        return {}
    root, main_id = entry["cwd"], entry["main_session_id"]
    raw = str(payload.get("prompt") or "")
    text = _normalize_skill_mentions(raw)
    rt = wp.runtime_dir(root, main_id)
    provenance = {"host": "opencode", "session_id": sid}
    # Without a prompt source, execute stays anchored; commit accepts direct requests too.
    wp.record_approvals(text, rt, True, provenance)
    intent = wp.load_intent_map(str(HERE))
    hinted = None
    if not isinstance(intent, dict):
        # A missing or broken map must not keep a previous turn's delegation armed.
        wp.set_delegated_marker(None, rt, provenance)
    else:
        resolved = wp.explicit_command(text, intent.get("prefix_regex"))
        hinted = None if resolved else wp.nl_command(text, intent)
        wp.set_delegated_marker(resolved, rt, {**provenance, "prompt_excerpt": raw[:160]})
    # The hint belongs to this turn only; the system hook re-reads it on every model call.
    entry["hint"] = wp.intent_hint(hinted, root, main_id, wp.runner_command(root, _profile())) if hinted else None
    _write_entry(_entry_path(sid), entry)
    return {"context": _binding_text(entry, reused)}


def system(payload):
    entry = _bound(payload.get("session_id"))
    return {"context": _binding_text(entry, True, _contract_text())} if entry else {}


def _profile():
    """host-profile.json beside this hook (written at install from exoma/config/hosts.py)."""
    return wp.load_host_profile(str(HERE))


def _is_bash(word):
    return wp.is_bash(word, _profile())


def _host_read(path):
    home = str(Path.home())
    return wp.skill_read(path, home, [str(_home() / "skills"), str(_home() / "exoma")])


def pre_tool_use(payload):
    entry = _bound(payload.get("session_id"))
    if not entry:
        return {}
    root, main_id = entry["cwd"], entry["main_session_id"]
    policy = wp.policies(root)
    mode = str(policy.get("enforcement") or "hybrid")
    if mode == "off":
        return {}
    tool = str(payload.get("tool_name") or "").lower()
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    cwd = str(payload.get("cwd") or root)
    rt = wp.runtime_dir(root, main_id)
    command = str(args.get("command") or "")
    if tool in PATCH_TOOLS:
        paths = [p.strip() for p in _PATCH_PATH.findall(str(args.get("patchText") or args.get("patch") or ""))]
    elif tool in WRITE_TOOLS:
        target = args.get("filePath") or args.get("file_path") or args.get("path")
        paths = [str(target)] if target else []
    else:
        paths = []
    read_target = str(args.get("filePath") or args.get("file_path") or args.get("path") or "")
    kind = ("shell" if tool in SHELL_TOOLS else "write" if paths
            else "read" if tool in READ_TOOLS else "other")
    marker = wp.read_json(os.path.join(rt, "delegated.marker"))
    local = os.environ.get("WORKFLOW_LOCAL_MODE") == "1" or os.path.isfile(os.path.join(root, ".workflow", "local_mode.flag"))
    refusal = None
    if kind == "shell":
        session_error = wp.runner_session_refusal(command, {
            "root": root, "home": str(Path.home()), "is_bash": _is_bash, "expected_session": main_id})
        if session_error:
            refusal = (session_error, "Use exactly --session '%s' from the authoritative binding; "
                       "missing, default, duplicate or different session arguments are refused." % main_id)
    for target in paths:
        resolved = target if os.path.isabs(target) else os.path.join(cwd, target)
        if wp.runner_file(resolved, root):
            refusal = ("runner_write", "Runner scripts must be regenerated by /.upgrade, not edited directly: " + target)
            break
        # The escape files are the user's switch: an agent writing one turns its own gate off.
        if wp.escape_file(resolved, root):
            refusal = ("escape_write", wp.escape_write_message(tool, target))
            break
    if marker and local:
        wp.log_gate_skip(rt, "opencode-host", "env_local_mode" if os.environ.get("WORKFLOW_LOCAL_MODE") == "1"
                         else "local_mode_flag", root=root, key=str(marker.get("set_at") or ""))
    if refusal is None and marker and not local:
        profile = _profile()
        ctx = {"root": root, "home": str(Path.home()), "rt_dir": rt, "is_bash": _is_bash,
               "host_read": _host_read, "limit": wp.read_limit(args, profile), "expected_session": main_id}
        if kind == "read" and read_target and not os.path.isabs(read_target):
            read_target = os.path.join(cwd, read_target)
        line = command.strip()
        if line.startswith("& "):
            line = line[2:].lstrip()  # PowerShell's call operator in front of the quoted bash
        refusal_key = wp.preflight_refusal(kind, read_target if kind == "read" else paths[0] if paths else "",
                                           line, marker, ctx)
        if refusal_key:
            refusal = (refusal_key, "Delegate /.%s first, in the foreground with a long timeout: %s %s --session '%s' --task '<task>'"
                       % (marker.get("command"), wp.runner_command(root, profile), marker.get("command"), main_id))
    if refusal is None and tool in SHELL_TOOLS:
        refusal = wp.commit_refusal(command, rt, {"root": root, "home": str(Path.home()), "is_bash": _is_bash})
    if refusal is None:
        for target in paths:
            refusal = wp.write_refusal(tool, target, cwd, root, policy, rt, host_dirs=(str(_home()),))
            if refusal:
                break
    if refusal is None:
        return {}
    rule, message = refusal
    blocked = mode != "warn"
    execution = wp.read_json(os.path.join(rt, "execution.json")) or {}
    wp.log_refusal(rt, "opencode-host", rule, tool, mode, blocked, execution.get("plan_id"))
    if not blocked:
        return {"warn": "[EXECUTION GATE: warn] " + message}
    return {"deny": "[EXECUTION GATE] " + message}


def handle(event, payload):
    if wp.is_second_agent_turn():
        return {}
    if event == "UserPromptSubmit":
        return user_prompt_submit(payload)
    if event == "System":
        return system(payload)
    if event == "PreToolUse":
        return pre_tool_use(payload)
    return {}


def main(argv=None):
    argv = sys.argv if argv is None else argv
    event = argv[1] if len(argv) > 1 else ""
    if event not in EVENTS:
        sys.stdout.write("{}")
        return 0
    try:
        payload = json.load(sys.stdin)
        out = handle(event, payload if isinstance(payload, dict) else {})
    except Exception:
        try:
            path = _home() / "exoma" / "hook-errors.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(event + "\n" + traceback.format_exc() + "\n")
        except OSError:
            pass
        out = {}
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
