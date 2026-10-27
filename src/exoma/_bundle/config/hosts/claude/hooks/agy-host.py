"""agy-host.py - the Agy (Antigravity CLI) host wrapper, one entry point for every event.

    <python> agy-host.py <Event>     (stdin: Agy's hook JSON; stdout: Agy's hook JSON)

Registered by the installer in ~/.gemini/config/hooks.json under the hook name `exoma`, and
installed with workflow_policy.py and intent-map.json into ~/.gemini/antigravity-cli/exoma/
hooks, so an Agy session never reads ~/.claude. It
gives an Agy session what the Claude Code hooks give a Claude session, through Agy's own
contract as calibrated (CASE-029; docs/reference.md, "Hosts"):

  PreInvocation  invocationNum 0 is the start of a user turn: bind the conversation to a
                 MAIN_SESSION_ID, record the user's approvals (execute.token, commit.marker),
                 arm or clear the pre-flight marker, and inject [SESSION BINDING] (plus an
                 [INTENT HINT]) as an ephemeral message. SessionStart cannot do this: its
                 injectSteps never reach the model, and a resumed conversation skips it.
  PreToolUse     the pre-flight gate and the execution gate, answered with an explicit
                 decision. Agy treats `{}` as deny, so every path here says `allow` or `deny`.
  Stop           the status guard. Agy has no warning channel at Stop, so a completion
                 claim against verification: not_run asks the agent to continue once and
                 correct it.

Fail-open, deliberately against Agy's grain: Agy fails the tool call when a hook crashes or
exits non-zero, so a bug here would block every tool. Every error is logged to
~/.gemini/exoma-hook-errors.log and answered with `allow`.

A process with WORKFLOW_AGENT_ROLE=second_agent (set by the agy adapter) is the runtime's
delegated call, not the user's session: allowed through untouched. The prompt's
[WORKFLOW_AGENT] header is not trusted for that: the user can type or paste it.

Standalone stdlib, like every hook beside it; the decisions come from workflow_policy.py.
"""
import datetime
import json
import os
import random
import re
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

EVENTS = ("SessionStart", "PreInvocation", "PostInvocation", "PreToolUse", "PostToolUse", "Stop")
ERROR_LOG = "exoma-hook-errors.log"
REGISTRY = "exoma_session_registry.json"
_ERROR_LOG_MAX_BYTES = 256 * 1024

# Agy's tool names and argument keys (as calibrated: read_file {"Path": ...}, run_command; the
# write tools as listed in adapters/install/agy_install.py). They are matched by name shape
# as well as by list, so a renamed edit tool is still gated rather than waved through.
WRITE_TOOLS = ("write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file",
               "notebook_edit", "edit_file", "create_file")
_WRITE_SHAPE = re.compile(r"(?:^|_)(?:write|replace|edit|create)(?:_|$)")
SHELL_TOOLS = ("run_command",)
GATHER_TOOLS = ("read_file", "view_file", "view_file_outline", "view_code_item", "list_dir",
                "grep_search", "find_by_name", "codebase_search", "read_url_content", "search_web")
PATH_KEYS = ("TargetFile", "AbsolutePath", "Path", "path", "file_path", "FilePath")
COMMAND_KEYS = ("CommandLine", "Command", "command")


def gemini_dir():
    return os.path.join(os.path.expanduser("~"), ".gemini")


def log_error(event):
    try:
        path = os.path.join(gemini_dir(), ERROR_LOG)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > _ERROR_LOG_MAX_BYTES:
            with open(path, "rb") as f:
                f.seek(-_ERROR_LOG_MAX_BYTES // 2, os.SEEK_END)
                tail = f.read()
            with open(path, "wb") as f:
                f.write(tail)
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(path, "a", encoding="utf-8") as f:
            f.write("--- %s %s\n%s\n" % (stamp, event, traceback.format_exc()))
    except Exception:
        pass


def allow(event):
    return {"decision": "allow"} if event == "PreToolUse" else {}


def first_arg(args, keys):
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


# --- session binding --------------------------------------------------------------------


def _read_registry():
    import workflow_policy as wp
    return wp.read_json(os.path.join(gemini_dir(), REGISTRY)) or {}


def _write_registry(registry):
    path = os.path.join(gemini_dir(), REGISTRY)
    items = sorted(registry.items(), key=lambda kv: (kv[1] or {}).get("bound_at", ""), reverse=True)[:50]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Replaced, not rewritten in place: a half-written registry reads as empty to every hook.
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(items), f, indent=2)
    os.replace(tmp, path)


def workflow_root(paths):
    """The first workspace path that holds a .workflow/, resolved; None without one."""
    for path in paths or []:
        try:
            root = os.path.realpath(str(path))
        except Exception:
            continue
        if os.path.isdir(os.path.join(root, ".workflow")):
            return root
    return None


def bound(cid):
    """(main_id, root) already bound to this conversation, or None."""
    entry = _read_registry().get(cid) if cid else None
    if not entry or not entry.get("main_session_id") or not entry.get("cwd"):
        return None
    return str(entry["main_session_id"]), str(entry["cwd"])


def bind(cid, paths):
    """(main_id, root, reused) for this conversation, binding it on first sight.

    Agy keeps the conversationId across `--conversation` resumes and never says which kind
    of start it is, so the registry decides: a known id is a resume, an unknown one is new.
    """
    known = bound(cid)
    if known:
        return known[0], known[1], True
    root = workflow_root(paths)
    if not cid or not root:
        return None
    now = datetime.datetime.now()
    ts = now.strftime("%Y%m%d_%H%M%S") + "%03d" % (now.microsecond // 1000)
    rand = "".join(random.choice("0123456789abcdef") for _ in range(4))
    slug = os.path.basename(root.rstrip("/\\")) or "root"
    main_id = "main_%s_%s_%s" % (slug, ts, rand)
    registry = _read_registry()
    registry[cid] = {
        "main_session_id": main_id,
        "cwd": root,
        "bound_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": "agy",
    }
    _write_registry(registry)
    return main_id, root, False


# --- events -----------------------------------------------------------------------------


def pre_invocation(payload, cid, prompt):
    import workflow_policy as wp
    if int(payload.get("invocationNum") or 0) != 0:
        return {}  # the turn already started; its binding and hint were injected then
    binding = bind(cid, payload.get("workspacePaths"))
    if not binding:
        return {}
    main_id, root, reused = binding
    rt = wp.runtime_dir(root, main_id)
    text = wp.user_text(prompt)
    provenance = {"host": "agy", "conversation_id": cid}
    wp.record_approvals(text, rt, False, provenance)
    lines = [wp.session_binding(main_id, root, "resume" if reused else "startup", reused)]
    mp = wp.load_intent_map(HERE)
    if mp is not None:
        resolved = wp.explicit_command(text, mp.get("prefix_regex"))
        hinted = None if resolved else wp.nl_command(text, mp)
        if hinted:
            lines.append(wp.intent_hint(hinted, root, main_id))
        wp.set_delegated_marker(resolved, rt, {**provenance, "prompt_excerpt": prompt[:160]})
    return {"injectSteps": [{"ephemeralMessage": "\n\n".join(lines)}]}


_SHELL_META = set(";|&<>`$\n")
_RUNNER = re.compile(r"^(?:'([^']+)'|(\S+))(?:\s|$)")
_BASH_WORD = re.compile(r"^(?:'([^']+)'|(\S+))\s+")


def find_bash():
    """The bash the runner must start under, restated from exoma.utils.osutil.find_bash (this
    hook runs without the package): `bash` on PATH, or on Windows Git Bash beside `git` —
    there a bare `bash` can be WSL's, which cannot read the Windows paths the runner takes."""
    import shutil
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    if not git:
        return None
    parent = os.path.dirname(os.path.abspath(git))
    while True:
        for candidate in (os.path.join(parent, "bin", "bash.exe"),
                          os.path.join(parent, "usr", "bin", "bash.exe")):
            if os.path.isfile(candidate):
                return candidate
        up = os.path.dirname(parent)
        if up == parent:
            return None
        parent = up


def runner_prefix():
    """The runner's bash as the host instructions spell it (installer.base.runner_prefix)."""
    found = find_bash()
    if not found:
        return "bash"
    path = found.replace("\\", "/")
    return "& '%s'" % path if os.name == "nt" else "'%s'" % path


def _is_bash(word):
    """A bash this runner may start under: the one find_bash names, or `bash` off Windows."""
    import workflow_policy as wp
    if os.name != "nt" and word == "bash":
        return True
    found = find_bash()
    return bool(found) and wp.norm(word.replace("\\", "/")) == wp.norm(found.replace("\\", "/"))


def runner_call(command, root):
    """True for one plain call of <root>/.workflow/{run,check,inspect}.sh through the bash
    find_bash names (PowerShell's `& '<bash>'` included): every shell metacharacter inside
    single quotes, the script path inside the root. A bare `bash` on Windows is refused —
    it can be WSL's, which fails on these paths."""
    text = (command or "").strip()
    if text.startswith("& "):
        text = text[2:].lstrip()
    bash = _BASH_WORD.match(text)
    if not bash or not _is_bash(bash.group(1) or bash.group(2)):
        return False
    text = text[bash.end():]
    quoted = False
    for ch in text:
        if ch == "'":
            quoted = not quoted
        elif not quoted and (ch in _SHELL_META or ch == '"'):
            return False
    if quoted:
        return False
    m = _RUNNER.match(text)
    if not m:
        return False
    script = (m.group(1) or m.group(2)).replace("\\", "/")
    import workflow_policy as wp
    for name in ("run.sh", "check.sh", "inspect.sh"):
        if wp.norm(script) == wp.norm(os.path.join(root, ".workflow", name)):
            return True
    return False


def skill_read(target):
    """A skill definition or the contract: an .md under Agy's skills or Exoma's own Agy
    directory, where the host instructions point."""
    import workflow_policy as wp
    cli = os.path.join(gemini_dir(), "antigravity-cli")
    return target.lower().endswith(".md") and (
        wp.under(target, os.path.join(cli, "skills")) or wp.under(target, os.path.join(cli, "exoma")))


def pre_tool_use(payload, cid):
    import workflow_policy as wp
    known = bound(cid)
    if not known:
        return allow("PreToolUse")
    main_id, root = known
    pol = wp.policies(root)
    mode = str(pol.get("enforcement") or "hybrid")
    if mode == "off":
        return allow("PreToolUse")
    rt = wp.runtime_dir(root, main_id)
    call = payload.get("toolCall") or {}
    tool = str(call.get("name") or "")
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    is_shell = tool in SHELL_TOOLS
    is_write = tool in WRITE_TOOLS or bool(_WRITE_SHAPE.search(tool))
    target = first_arg(args, PATH_KEYS)
    command = first_arg(args, COMMAND_KEYS)

    refusal = None
    marker = wp.read_json(os.path.join(rt, "delegated.marker"))
    if marker and os.environ.get("WORKFLOW_LOCAL_MODE") != "1" and not os.path.isfile(
            os.path.join(root, ".workflow", "local_mode.flag")):
        cmd = marker.get("command") or "?"
        gather = tool in GATHER_TOOLS or tool.startswith("mcp_") or is_write or is_shell
        if is_shell and runner_call(command, root):
            gather = False
        if tool in GATHER_TOOLS and target and skill_read(target):
            gather = False
        if gather:
            refusal = ("preflight", (
                "/.%s is a delegated command: the next step is the runner, not %s.\n"
                "Run: %s '%s/.workflow/run.sh' %s '<task>' '%s'\n"
                "Escape: WORKFLOW_LOCAL_MODE=1, .workflow/local_mode.flag, or delete %s."
            ) % (cmd, tool, runner_prefix(), root.replace("\\", "/"), cmd, main_id,
                 os.path.join(rt, "delegated.marker")))
    if refusal is None and is_shell:
        refusal = wp.commit_refusal(command, rt)
    if refusal is None and is_write and target:
        home = os.path.expanduser("~")
        refusal = wp.write_refusal(tool, target, root, root, pol, rt,
                                   host_dirs=(gemini_dir(), os.path.join(home, ".claude")))
    if refusal is None:
        return allow("PreToolUse")
    rule, message = refusal
    blocked = mode != "warn"
    plan_id = (wp.read_json(os.path.join(rt, "execution.json")) or {}).get("plan_id")
    wp.log_refusal(rt, "agy-host", rule, tool, mode, blocked, plan_id)
    if blocked:
        return {"decision": "deny", "reason": "[EXECUTION GATE] " + message}
    return {"decision": "allow", "reason": "[EXECUTION GATE: warn] " + message}


def last_model_text(payload):
    """The last model response in Agy's transcript (`source: MODEL`, PLANNER_RESPONSE)."""
    path = payload.get("transcriptPath")
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-200:]
    except Exception:
        return ""
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("source") == "MODEL" and row.get("type") == "PLANNER_RESPONSE":
            content = row.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def stop(payload, cid):
    import workflow_policy as wp
    known = bound(cid)
    if not known:
        return {}
    main_id, root = known
    if str(wp.policies(root).get("enforcement") or "hybrid") == "off":
        return {}
    rt = wp.runtime_dir(root, main_id)
    execution = wp.read_json(os.path.join(rt, "execution.json"))
    if not execution or execution.get("verification") != "not_run":
        return {}
    word = wp.claims_completion(last_model_text(payload))
    if not word:
        return {}
    # One correction per turn: `continue` keeps the agent running, and asking twice for the
    # same fix would loop on a reply that keeps its wording.
    flag = os.path.join(rt, "status-guard.agy")
    turn = "%s:%s" % (cid, payload.get("executionNum"))
    try:
        with open(flag, "r", encoding="utf-8") as f:
            if f.read().strip() == turn:
                return {}
    except Exception:
        pass
    os.makedirs(rt, exist_ok=True)
    with open(flag, "w", encoding="utf-8") as f:
        f.write(turn)
    return {
        "decision": "continue",
        "reason": (
            "[STATUS GUARD] the [EXECUTION RESULT] says %r, but plan %s has verification: not_run. "
            "Implemented is not verified: restate the result as implemented, not yet verified, "
            "and offer /.verify."
        ) % (word, execution.get("plan_id")),
    }


def handle(event, payload, environ):
    import workflow_policy as wp
    if wp.is_second_agent_turn(environ):
        return allow(event)
    prompt = str(payload.get("lastUserInput") or "")
    cid = str(payload.get("conversationId") or environ.get("ANTIGRAVITY_CONVERSATION_ID") or "")
    if event == "PreInvocation":
        return pre_invocation(payload, cid, prompt)
    if event == "PreToolUse":
        return pre_tool_use(payload, cid)
    if event == "Stop":
        return stop(payload, cid)
    return allow(event)


def main(argv):
    event = argv[1] if len(argv) > 1 else ""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace").lstrip("﻿")
        payload = json.loads(raw) if raw.strip() else {}
        out = handle(event, payload if isinstance(payload, dict) else {}, os.environ)
    except Exception:
        log_error(event)
        out = allow(event)
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
