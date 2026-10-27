"""workflow_policy.py - host-neutral decisions shared by every host's hooks.

The Claude Code hooks (execution-gate.py, intent-gate-set.py) and the Agy host wrapper
(agy-host.py) decide the same things: may this write land here, may this command commit,
did the user approve an execution, which delegated command did they name. What differs per
host is how the question arrives (payload field names) and how the answer leaves (exit 2
and additionalContext for Claude Code, a JSON `decision` and injectSteps for Agy). This
module holds only the decisions; each host wrapper keeps its own parsing and output.

Standalone on purpose, like every hook beside it: installed into ~/.claude/hooks, it runs
with no repository on disk and imports nothing beyond the standard library. It is a library,
not a hook: no launcher, never run on its own.
"""
import datetime
import fnmatch
import json
import os
import re
import tempfile

# --- workspace layout -------------------------------------------------------------------


def workflow_data_dir(root):
    # Same rule as core/workspace/workspace_paths.data_dir: .workflow/data once it exists,
    # the .workflow root while a v3.5.x-layout workspace still keeps its data there, data/ otherwise.
    wf = os.path.join(root, ".workflow")
    data = os.path.join(wf, "data")
    if os.path.isdir(data):
        return data
    for name in ("sessions", "provider-sessions", "reports", "audit.jsonl", "usage.jsonl", "quality.jsonl", "facts.jsonl", "evidence.jsonl", "redactions.jsonl"):
        if os.path.exists(os.path.join(wf, name)):
            return wf
    return data


def runtime_dir(root, main_id):
    return os.path.join(workflow_data_dir(root), "sessions", main_id, "runtime")


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def policies(root):
    return (read_json(os.path.join(root, ".workflow", "config.json")) or {}).get("policies") or {}


# --- role -------------------------------------------------------------------------------

# Set by the runtime on the second agent's process; hooks inherit their host's environment.
# A signal between the runtime and the hooks, not a setting: WORKFLOW_, like
# WORKFLOW_LOCAL_MODE, rather than the EXOMA_ configuration prefix.
ROLE_ENV = "WORKFLOW_AGENT_ROLE"
SECOND_AGENT_ROLE = "second_agent"


def is_second_agent_turn(environ=None):
    """True when this process is a delegated call, not the user's host session.

    The same CLI can be the user's host and, one call later, the workflow's second agent:
    the host's hooks and instructions load in both. Only the environment the runtime set
    decides. The prompt's `[WORKFLOW_AGENT]` header (core/prompt/prompt_builder.py) is text
    the user can type or paste too, so a hook that trusted it would let any prompt opening
    with it switch the gates off; the header stays a prompt-level cue for the model.
    """
    env = os.environ if environ is None else environ
    return str(env.get(ROLE_ENV) or "") == SECOND_AGENT_ROLE


# --- paths ------------------------------------------------------------------------------


def norm(path):
    # realpath, not abspath: a symlink or junction inside the project that points elsewhere
    # must be judged by where the write lands, or `project/link/x.py` escapes the root
    # while reading as inside it.
    return os.path.normcase(os.path.realpath(path))


def under(path, base):
    """True when `path` is `base` or inside it (both already absolute)."""
    p, b = norm(path), norm(base)
    try:
        return os.path.commonpath([p, b]) == b
    except ValueError:
        return False  # different drives


def in_scope(relative, scope):
    for entry in scope or []:
        entry = str(entry).strip().replace("\\", "/")
        if not entry:
            continue
        stem = entry.rstrip("/")
        if relative == stem or relative.startswith(stem + "/") or fnmatch.fnmatch(relative, entry):
            return True
    return False


# --- execution gate ---------------------------------------------------------------------

# An option's value may be quoted and hold spaces: `git -C "repo with spaces" commit`. The
# executable may be named with its Windows suffix or by a quoted path (`git.exe commit`,
# `"C:/Program Files/Git/cmd/git.exe" commit`); without that, those spellings committed
# with no approval at all.
_ARG = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
GIT_COMMIT = re.compile(
    r"\bgit(?:\.exe)?\b[\"']?(?:\s+(?:-[cC]\s+" + _ARG + r"|--?[\w-]+(?:=" + _ARG + r")?))*\s+commit\b",
    re.IGNORECASE,
)

# The commit marker is cleared by the next user prompt that does not ask to commit; this
# bound covers the turn that never gets a next prompt. Not measured.
COMMIT_MARKER_TTL_S = 1800


def marker_fresh(path, ttl=COMMIT_MARKER_TTL_S):
    marker = read_json(path)
    if marker is None:
        return False
    try:
        set_at = datetime.datetime.fromisoformat(str(marker.get("set_at")))
    except ValueError:
        return False
    if set_at.tzinfo is None:
        set_at = set_at.replace(tzinfo=datetime.timezone.utc)
    age = (datetime.datetime.now(datetime.timezone.utc) - set_at).total_seconds()
    return 0 <= age <= ttl


def commit_refusal(command, rt_dir):
    """(rule, message) when `command` commits without the user's approval this turn."""
    if GIT_COMMIT.search(command or "") and not marker_fresh(os.path.join(rt_dir, "commit.marker")):
        return "commit_unapproved", (
            "`git commit` without the user asking for a commit this turn.\n"
            "A commit is approved by the user's own prompt (e.g. /.commit, or asking to commit); "
            "show the proposed message and wait for that prompt.\n"
            "Turn the gate off for this project: policies.enforcement = \"off\" in .workflow/config.json."
        )
    return None


def write_refusal(tool, target, cwd, root, pol, rt_dir, host_dirs=()):
    """(rule, message) when a write to `target` breaks the root or scope rule, else None.

    `host_dirs` are the host's own config directories, writable from any project (~/.claude
    for Claude Code). The system temp dir and policies.write_allowed_outside_root always are.
    """
    target = target if os.path.isabs(target) else os.path.join(str(cwd or root), target)
    if not under(target, root):
        allowed = [tempfile.gettempdir(), os.environ.get("TEMP") or "", os.environ.get("TMP") or ""]
        allowed += [d for d in host_dirs if d]
        extra = pol.get("write_allowed_outside_root")
        if isinstance(extra, list):
            allowed += [os.path.expanduser(str(p)) for p in extra if str(p).strip()]
        if any(base and under(target, base) for base in allowed):
            return None
        return "outside_root", (
            "'%s' writes outside the session's project root (%s): %s\n"
            "Writing into another project needs the user's explicit approval for that path.\n"
            "Once given, the user adds it to policies.write_allowed_outside_root in .workflow/config.json."
        ) % (tool, root, target)

    relative = os.path.relpath(norm(target), norm(root)).replace("\\", "/")
    if relative == ".workflow" or relative.startswith(".workflow/"):
        return None
    execution = read_json(os.path.join(rt_dir, "execution.json"))
    if not execution or execution.get("status") != "active":
        return None
    scope = execution.get("scope") or []
    scope_keys = [s.lower() if os.name == "nt" else s for s in scope]
    # The plan names paths as the user sees them. Containment above is decided on the
    # resolved path; scope accepts either spelling, so `linked/x.py` in a plan still
    # matches after `linked/` resolves to another in-root directory.
    lexical = os.path.relpath(
        os.path.normcase(os.path.abspath(target)), os.path.normcase(os.path.abspath(root))
    ).replace("\\", "/")
    if in_scope(relative, scope_keys) or in_scope(lexical, scope_keys):
        return None
    return "outside_scope", (
        "'%s' targets %s, which is outside the active execution's scope (plan %s).\n"
        "Scope: %s\n"
        "Needing it means the plan was incomplete: stop, report the conflict, and ask the user; "
        "a revised plan recorded with plan-record and a new /.execute -y opens the wider scope."
    ) % (tool, relative, execution.get("plan_id"), ", ".join(scope) or "(empty)")


def log_refusal(rt_dir, hook, rule, tool, mode, blocked, plan_id):
    """One line per refusal in runtime/gate-blocks.jsonl; never the path or command."""
    if not rt_dir:
        return
    row = {
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "hook": hook,
        "rule": rule,
        "tool": tool,
        "mode": mode,
        "exit": 2 if blocked else 0,
        "plan_id": plan_id,
    }
    try:
        os.makedirs(rt_dir, exist_ok=True)
        with open(os.path.join(rt_dir, "gate-blocks.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass  # measuring the gate must never change what it decides


# --- the user's turn: approvals and intent ----------------------------------------------

# Both tags may carry attributes (`<system-reminder id="...">`, and the same on the close).
_REMINDER = re.compile(r"\A\s*<system-reminder\b[^>]*>.*?</system-reminder\b[^>]*>", re.DOTALL)

# Pasted text arrives inside the prompt. It is material the user brought, not their words:
# a pasted log that says "commit" or "analisa" asks for nothing.
PASTED = re.compile(r"<pasted_content\b[^>]*>.*?</pasted_content\b[^>]*>", re.DOTALL)


def without_reminders(prompt):
    """The prompt with its leading <system-reminder> blocks removed, left-stripped."""
    text = prompt
    while True:
        stripped = _REMINDER.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return text.lstrip()


def user_text(prompt):
    """What the user wrote: pasted material out, leading reminders out."""
    return without_reminders(PASTED.sub(" ", prompt or ""))


# `/.execute -y` typed as text, or as the expanded slash command Claude Code submits.
_EXECUTE_YES = re.compile(
    r"(?:^|\s)/\.execute\s+-y\b|<command-name>/?\.execute</command-name>\s*<command-args>\s*-y\b",
    re.IGNORECASE,
)
_COMMIT = re.compile(r"(?:^|\s)/\.commit\b|\bcommit\w*\b", re.IGNORECASE)
# "jangan commit", "don't commit", "tanpa commit": naming the act is not asking for it.
_COMMIT_NEGATED = re.compile(
    r"\b(?:jangan|tanpa|belum|tidak|bukan|don'?t|do\s+not|no|never|without)\b[^.\n]{0,20}\bcommit",
    re.IGNORECASE,
)

# The skill asks "Jalankan? (yes/no)" after proposing the message, so the commit itself
# happens a turn after the request. A bare yes keeps the approval that turn already holds;
# it never creates one.
AFFIRM = re.compile(
    r"^\s*(?:yes|ya|y|iya|ok|oke|okay|gas|lanjut|boleh|setuju|jalankan|commit)\b[\s.!]*$",
    re.IGNORECASE,
)

# The same two approvals, held to the start of the prompt: what a turn from an unknown
# `source` must look like to approve anything. The slash-command payload counts wherever it
# sits, since the host, not the text, puts the tag there.
_EXECUTE_AT_START = re.compile(r"/\.execute\s+-y\b", re.IGNORECASE)
_COMMIT_AT_START = re.compile(r"(?:/\.commit\b|commit\w*\b)", re.IGNORECASE)
_EXECUTE_TAG = re.compile(r"<command-name>/?\.execute</command-name>\s*<command-args>\s*-y\b", re.IGNORECASE)
_COMMIT_TAG = re.compile(r"<command-name>/?\.commit</command-name>", re.IGNORECASE)

# A delegated command the user named: typed with its prefix, or the slash-command payload.
# Only these arm the gate. A sentence that merely matches the NL map gets a hint.
_DELEGATED = "explore|plan|analyze|verify-browser|verify"
_COMMAND_TAG = re.compile(r"<command-name>/?\.(%s)</command-name>" % _DELEGATED, re.IGNORECASE)


def execute_approved(prompt, anchored=False):
    if anchored:
        return bool(_EXECUTE_AT_START.match(prompt or "") or _EXECUTE_TAG.search(prompt or ""))
    return bool(_EXECUTE_YES.search(prompt or ""))


def commit_requested(prompt, anchored=False):
    text = prompt or ""
    asked = (_COMMIT_AT_START.match(text) or _COMMIT_TAG.search(text)) if anchored else _COMMIT.search(text)
    return bool(asked) and not _COMMIT_NEGATED.search(text)


def explicit_command(text, prefix_regex):
    """The delegated command the user named explicitly, or None."""
    m = re.search(prefix_regex, text) if prefix_regex else None
    if m:
        return m.group(1)
    m = _COMMAND_TAG.search(text)
    return m.group(1).lower() if m else None


def nl_command(text, mp):
    """The delegated command a sentence resembles under the NL map, or None."""
    for cmd in mp.get("delegated", []):
        for pat in mp.get("patterns", {}).get(cmd, []):
            if re.search(pat, text, re.IGNORECASE):
                return cmd
    return None


def load_intent_map(hook_dir):
    return read_json(os.path.join(hook_dir or "", "intent-map.json"))


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def record_approvals(text, rt_dir, anchored, provenance):
    """Write or clear execute.token and commit.marker for the user's own turn.

    The files the runtime and the execution gate check (core/runtime/execution.py). Written
    before the NL map is read: approval does not depend on intent classification, and a
    missing map must not drop a `-y`.
    """
    approvals = (
        ("execute.token", execute_approved(text, anchored=anchored)),
        ("commit.marker", commit_requested(text, anchored=anchored)),
    )
    for name, given in approvals:
        path = os.path.join(rt_dir, name)
        if name == "commit.marker" and not given and os.path.isfile(path) and AFFIRM.match(text):
            continue  # the yes to a commit this session just proposed
        if given:
            os.makedirs(rt_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"set_at": _now(), **provenance}))
        elif name == "commit.marker" and os.path.isfile(path):
            # A commit is approved for the turn the user asked for it, not for the rest of
            # the session. The execute token is consumed by execute-begin instead, and
            # expires on its own.
            try:
                os.remove(path)
            except Exception:
                pass


def set_delegated_marker(resolved, rt_dir, provenance):
    """Arm the pre-flight gate for the command the user named, or clear a stale marker."""
    marker = os.path.join(rt_dir, "delegated.marker")
    if resolved:
        os.makedirs(rt_dir, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(json.dumps({"command": resolved, "set_at": _now(), **provenance}))
    elif os.path.isfile(marker):
        try:
            os.remove(marker)
        except Exception:
            pass


def intent_hint(hinted, root, main_id):
    return (
        "[INTENT HINT] This prompt resembles /.%s under the workflow NL map. If it "
        "is that request, delegate first: bash '%s/.workflow/run.sh' %s '<task>' '%s'. "
        "If it is a question or a remark, answer it. Nothing is blocked: the gate "
        "arms only on an explicit /.%s."
    ) % (hinted, root.replace("\\", "/"), hinted, main_id, hinted)


def session_binding(main_id, root, source, reused):
    return (
        "[SESSION BINDING - authoritative]\n"
        "MAIN_SESSION_ID=%s\n"
        "MAIN_SESSION_PROJECT_ROOT=%s\n"
        "source=%s\n"
        "second_agent_thread=%s\n"
        "Use this MAIN_SESSION_ID for all /.explore /.plan /.analyze /.verify /.sweep "
        "invocations. Overrides .workflow/state.json session.id."
    ) % (main_id, root, source, "REUSE (continue)" if reused else "NEW")


# --- end of turn: the [EXECUTION RESULT] claim --------------------------------------

# The values skills/execute.md allows while verification is not_run. Anything else in the
# status field ("done") claims what only /.verify can establish.
UNVERIFIED_STATUSES = {"implemented", "partial", "blocked"}
# The reply's own block: the header opens a line (heading or emphasis marks allowed). One
# quoted (`> [EXECUTION RESULT]`), mentioned mid-sentence, or inside a code fence is someone
# else's result being relayed, not this reply's claim.
_RESULT_HEADER = re.compile(r"^[ \t#*_-]*\[EXECUTION RESULT\]", re.MULTILINE)
# A fence runs to its closing fence, or to the end of the reply when it is never closed.
_FENCE = re.compile(r"^[ \t]*(```|~~~).*?(?:^[ \t]*\1[^\n]*$|\Z)", re.DOTALL | re.MULTILINE)
# A block ends at the next [SECTION] header. A field line may carry list or emphasis marks
# around the key (`- **status:** implemented`); a quoted line is not a field.
_NEXT_HEADER = re.compile(r"^\s*\[[A-Z][A-Z _-]+\]", re.MULTILINE)
_FIELD = re.compile(r"^[ \t*_`|-]*(status|verification)[*_`\s]*:[*_`\s]*([A-Za-z_-]+)", re.IGNORECASE | re.MULTILINE)


def result_fields(text):
    """{field: value} from the last [EXECUTION RESULT] block in `text`, lower-cased."""
    text = _FENCE.sub("", text or "")
    headers = list(_RESULT_HEADER.finditer(text))
    if not headers:
        return {}
    body = text[headers[-1].end():]
    nxt = _NEXT_HEADER.search(body)
    body = body[: nxt.start()] if nxt else body
    return {m.group(1).lower(): m.group(2).lower() for m in _FIELD.finditer(body)}


def claims_completion(text):
    """`field: value` where the reply's own [EXECUTION RESULT] claims more than not_run
    allows, or None."""
    fields = result_fields(text)
    status, verification = fields.get("status"), fields.get("verification")
    if status and status not in UNVERIFIED_STATUSES:
        return "status: " + status
    if verification and verification != "not_run":
        return "verification: " + verification
    return None
