"""Codex lifecycle hooks for the Exoma main-agent host. Standalone stdlib only."""

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

EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"}
_SKILL = re.compile(r"\$exoma-(explore|plan|analyze|verify-browser|verify|execute|commit)\b", re.I)
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", re.M)


def _home():
    return Path(os.environ.get("AGENT_HOME") or Path.home()) / ".codex"


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


def _bound(session_id):
    path = _entry_path(session_id)
    data = wp.read_json(str(path)) if path else None
    if not isinstance(data, dict) or not data.get("main_session_id") or not data.get("cwd"):
        return None
    if not (Path(data["cwd"]) / ".workflow").is_dir():
        return None
    return data


def _context(message, event):
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


def session_start(payload):
    sid = payload.get("session_id")
    root = _root(payload.get("cwd"))
    source = str(payload.get("source") or "startup")
    if not sid or root is None:
        return {}
    known = _bound(sid)
    reused = source in {"resume", "compact"} and known is not None and Path(known["cwd"]).resolve() == root
    if reused:
        main_id = known["main_session_id"]
    else:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S%f")[:17]
        main_id = "main_%s_%s_%04x" % (root.name or "root", stamp, random.getrandbits(16))
        path = _entry_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".%d.tmp" % os.getpid())
        temp.write_text(json.dumps({"main_session_id": main_id, "cwd": str(root), "host": "codex"}), encoding="utf-8")
        os.replace(temp, path)
    context = wp.session_binding(main_id, str(root), source, reused)
    context += ("\n[EXOMA CODEX HOST] Read ~/.codex/exoma/CONTRACT.md before workflow work. "
                "Use $exoma-<command> skills; the contract's /. names refer to those skills here.")
    return _context(context, "SessionStart")


def _normalize_skill_mentions(prompt):
    return _SKILL.sub(lambda m: "/." + m.group(1).lower(), wp.user_text(prompt))


def user_prompt_submit(payload):
    entry = _bound(payload.get("session_id"))
    if not entry:
        return {}
    root, main_id = entry["cwd"], entry["main_session_id"]
    raw = str(payload.get("prompt") or "")
    text = _normalize_skill_mentions(raw)
    rt = wp.runtime_dir(root, main_id)
    provenance = {"host": "codex", "session_id": payload.get("session_id"), "turn_id": payload.get("turn_id")}
    # Codex does not expose a prompt source. Only a command at the very start may approve.
    wp.record_approvals(text, rt, True, provenance)
    intent = wp.load_intent_map(str(HERE))
    if not isinstance(intent, dict):
        # A missing or broken map must not keep a previous turn's delegation armed.
        wp.set_delegated_marker(None, rt, provenance)
        return {}
    resolved = wp.explicit_command(text, intent.get("prefix_regex"))
    hinted = None if resolved else wp.nl_command(text, intent)
    wp.set_delegated_marker(resolved, rt, {**provenance, "prompt_excerpt": raw[:160]})
    hint = wp.intent_hint(hinted, root, main_id, wp.runner_command(root, _profile())) if hinted else None
    return _context(hint, "UserPromptSubmit") if hint else {}


def _profile():
    """host-profile.json beside this hook (written at install from exoma/config/hosts.py)."""
    return wp.load_host_profile(str(HERE))


def _is_bash(word):
    return wp.is_bash(word, _profile())


def _host_read(path):
    home = str(Path.home())
    return wp.skill_read(path, home, [str(_home() / "skills"), str(_home() / "exoma")])


def _patch_paths(command):
    return [line.strip() for line in _PATCH_PATH.findall(command or "")]


def _shell_precision_read(command):
    """One plain file read, optionally restricted to at most 200 output lines."""
    one = wp.shell_read_one(command)
    if one is not None:
        return one, None
    words = wp.plain_words(command)
    if not words:
        return None
    # ASCII digits only, as workflow_policy._int_arg reads them: `"²".isdigit()` is true and
    # int("²") raises, which this hook answers by failing open.
    if len(words) == 4 and words[:2] == ["Get-Content", "-TotalCount"] and wp._int_arg(words[2]) is not None:
        return words[3], wp._int_arg(words[2])
    if (len(words) == 4 and words[:2] == ["sed", "-n"]
            # At most 9 digits each, like _int_arg: int() on 4300+ digits raises (fail-open).
            and (span := re.fullmatch(r"([1-9][0-9]{0,8}),([1-9][0-9]{0,8})p", words[2]))):
        first, last = int(span.group(1)), int(span.group(2))
        if first <= last:
            return words[3], last - first + 1
    return None


def pre_tool_use(payload):
    entry = _bound(payload.get("session_id"))
    if not entry:
        return {}
    root, main_id = entry["cwd"], entry["main_session_id"]
    policy = wp.policies(root)
    mode = str(policy.get("enforcement") or "hybrid")
    if mode == "off":
        return {}
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    rt = wp.runtime_dir(root, main_id)
    command = str(args.get("command") or args.get("cmd") or args.get("patch") or "")
    paths = _patch_paths(command) if tool in {"apply_patch", "Edit", "Write"} else []
    if tool in {"Edit", "Write"} and not paths:
        target = args.get("file_path") or args.get("path")
        paths = [str(target)] if target else []
    read_tools = {"Read", "read_file", "view_file", "view_file_outline", "view_code_item"}
    read_target = str(args.get("file_path") or args.get("path") or args.get("Path") or "")
    kind = ("shell" if tool in {"Bash", "exec_command", "functions.exec_command"}
            else "write" if paths else "read" if tool in read_tools else "other")
    # Code mode's outer exec delegates to nested tools, whose hooks inspect the real call.
    if tool in {"functions.exec", "exec"}:
        return {}
    marker = wp.read_json(os.path.join(rt, "delegated.marker"))
    local = os.environ.get("WORKFLOW_LOCAL_MODE") == "1" or os.path.isfile(os.path.join(root, ".workflow", "local_mode.flag"))
    refusal = None
    for target in paths:
        resolved = target if os.path.isabs(target) else os.path.join(str(payload.get("cwd") or root), target)
        if wp.runner_file(resolved, root):
            refusal = ("runner_write", "Runner scripts must be regenerated by /.upgrade, not edited directly: " + target)
            break
    if refusal is None and marker and not local:
        limit = wp.read_limit(args, _profile())
        ctx = {"root": root, "home": str(Path.home()), "rt_dir": rt, "is_bash": _is_bash,
               "host_read": _host_read, "limit": limit}
        line = command.strip()
        if line.startswith("& "):
            line = line[2:].lstrip()
        if kind == "shell":
            precision = _shell_precision_read(line)
            if precision is not None and not _host_read(precision[0]):
                kind, read_target = "read", precision[0]
                ctx["limit"] = precision[1]
        if kind == "read" and read_target and not os.path.isabs(read_target):
            read_target = os.path.join(str(payload.get("cwd") or root), read_target)
        refusal_key = wp.preflight_refusal(kind, read_target if kind == "read" else paths[0] if paths else "",
                                           line, marker, ctx)
        if refusal_key:
            refusal = (refusal_key, "Delegate /.%s first: %s %s --session '%s' --task '<task>'"
                       % (marker.get("command"), wp.runner_command(root, _profile()), marker.get("command"), main_id))
    if refusal is None and tool in {"Bash", "exec_command", "functions.exec_command"}:
        refusal = wp.commit_refusal(command, rt)
    if refusal is None:
        for target in paths:
            refusal = wp.write_refusal(tool, target, str(payload.get("cwd") or root), root, policy, rt,
                                       host_dirs=(str(_home()),))
            if refusal:
                break
    if refusal is None:
        return {}
    rule, message = refusal
    blocked = mode != "warn"
    execution = wp.read_json(os.path.join(rt, "execution.json")) or {}
    wp.log_refusal(rt, "codex-host", rule, tool, mode, blocked, execution.get("plan_id"))
    if not blocked:
        return {"systemMessage": "[EXECUTION GATE: warn] " + message}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "[EXECUTION GATE] " + message}}


def stop(payload):
    entry = _bound(payload.get("session_id"))
    if not entry or payload.get("stop_hook_active"):
        return {}
    rt = wp.runtime_dir(entry["cwd"], entry["main_session_id"])
    execution = wp.read_json(os.path.join(rt, "execution.json")) or {}
    if execution.get("verification") != "not_run":
        return {}
    word = wp.claims_completion(str(payload.get("last_assistant_message") or ""))
    if not word:
        return {}
    return {"systemMessage": "[STATUS GUARD] Verification has not run; describe this as implemented, not verified."}


def handle(event, payload):
    if wp.is_second_agent_turn():
        return {}
    if event == "SessionStart":
        return session_start(payload)
    if event == "UserPromptSubmit":
        return user_prompt_submit(payload)
    if event == "PreToolUse":
        return pre_tool_use(payload)
    if event == "Stop":
        return stop(payload)
    return {}


def main(argv=None):
    argv = sys.argv if argv is None else argv
    event = argv[1] if len(argv) > 1 else ""
    if event not in EVENTS:
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
