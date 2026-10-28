"""workflow_policy.py - host-neutral decisions shared by every host's hooks.

The Claude Code hooks (execution-gate.py, intent-gate-set.py, intent-gate-check.py) and the
Agy host wrapper (agy-host.py) decide the same things: may this call pass while a delegated
command waits for the runner, may this write land here, may this command commit, did the
user approve an execution, which delegated command did they name. What differs per
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


# --- pre-flight gate ----------------------------------------------------------------------
#
# While a delegated marker is armed, the next step is the runner, not gathering. Each host
# names its tools differently; the adapter reduces a call to one `kind` and the decision is
# made here, once, for every host:
#   write  passes, except a write that lands on a runner script (runner_file);
#   shell  passes only as one plain runner call; under verify, also a file-name `git diff`;
#          a `cat <file>` of something the host may always read passes like that Read;
#   read   passes for what the host may always read (its skills, its task output), under
#          verify for .workflow/config.json and this session's verify/tests.json, and as a
#          precision read: a project file outside .workflow, small, two per marker;
#   other  (search, listing, MCP) never passes.
# Writes are not gathering: they are judged by the execution gate (write_refusal), as for a
# turn with no marker at all.

RUNNERS = ("run.sh", "check.sh", "inspect.sh")


def same_path(candidate, expected, base):
    # True when candidate names expected. A relative path resolves against the project root.
    # realpath as well as abspath: the root comes from session-bind's realpath(cwd), so an
    # 8.3 short name in the command (`C:\Users\RUNNER~1\...`) only matches once expanded.
    if not candidate.strip():
        return False
    try:
        if not os.path.isabs(candidate):
            candidate = os.path.join(base, candidate)
        a = os.path.normcase(os.path.abspath(candidate))
        b = os.path.normcase(os.path.abspath(expected))
        if a == b:
            return True
        return os.path.normcase(os.path.realpath(candidate)) == os.path.normcase(os.path.realpath(expected))
    except Exception:
        return False


def runner_target(candidate, root):
    # True when candidate names exactly <root>/.workflow/{run,check,inspect}.sh AND the file
    # it reaches, every link followed, is still inside this project's .workflow: a run.sh
    # that is a symlink to a script elsewhere is not the runner. A runner that does
    # not exist yet passes like before: running it fails, and runner_file refuses writes to
    # it, so nothing can be put there between this check and the call.
    # Unresolvable -> False. The .ps1 runners an earlier release generated are retired: a call to one is
    # not a runner call.
    if not any(same_path(candidate, os.path.join(root, ".workflow", name), root) for name in RUNNERS):
        return False
    try:
        full = candidate if os.path.isabs(candidate) else os.path.join(root, candidate)
        real = os.path.normcase(os.path.realpath(full))
        wf = os.path.normcase(os.path.realpath(os.path.join(root, ".workflow"))).rstrip("\\/")
        return os.path.dirname(real) == wf
    except Exception:
        return False


def runner_file(candidate, root):
    # True when a file write lands on a runner script: <root>/.workflow/{run,check,inspect}
    # with any extension. On Windows an NTFS stream suffix (`run.ps1::$DATA` writes run.ps1)
    # is cut off first, and a path carrying an 8.3 short name (`WORKFL~1`) counts when the
    # filesystem cannot resolve it. Unsure -> True.
    if not candidate.strip():
        return False
    try:
        if os.name == "nt" and len(candidate) > 2 and ":" in candidate[2:]:
            candidate = candidate[: candidate.index(":", 2)]
        if not os.path.isabs(candidate):
            candidate = os.path.join(root, candidate)
        full = os.path.realpath(candidate).rstrip("\\/")
        if not re.fullmatch(r"(?i)(run|check|inspect)(\..*)?", os.path.basename(full)):
            return False
        parent = os.path.dirname(full)
        wf = os.path.realpath(os.path.join(root, ".workflow"))
        if os.path.normcase(parent) == os.path.normcase(wf) or same_path(parent, wf, root):
            return True
        leaf = os.path.basename(parent)
        return "~" in parent and (leaf == ".workflow" or "~" in leaf)
    except Exception:
        return True


def msys_path(path):
    # Git Bash spells E:\x as /e/x (or /cygdrive/e/x); read it the way that shell does.
    m = re.fullmatch(r"/(?:cygdrive/)?([A-Za-z])(/.*)?", path) if os.name == "nt" else None
    return (m.group(1) + ":" + (m.group(2) or "/")) if m else path


def local_path(path, home):
    # A path the way the shell that wrote it reads it: `~` is the home directory in bash and
    # PowerShell alike, and Git Bash spells E:\x as /e/x. Anything else is returned as is.
    if path == "~" or path.startswith(("~/", "~\\")):
        path = os.path.join(home, path[2:])
    return msys_path(path)


def inside_root(path, root, home):
    # True when path (as the shell reads it) is the project root or lies inside it.
    try:
        full = os.path.normcase(os.path.realpath(local_path(path, home))).rstrip("\\/")
        base = os.path.normcase(os.path.realpath(root)).rstrip("\\/")
        return full == base or full.startswith(base + os.sep)
    except Exception:
        return False


def skill_read(candidate, home, roots):
    # True when a Read names a skill definition: an existing .md file whose real path (every
    # link followed) lies inside one of the host's real skill directories (`roots`). A skill
    # is the instruction the agent must read to know how to dispatch, not evidence, so
    # blocking it before the run locked the two together. `~` and /c/... spellings are read
    # as the shell reads them; after that an absolute path only, no `..` in it; a link may
    # point anywhere inside a skills folder, never out of it.
    candidate = local_path(candidate, home)
    if not candidate.strip() or not os.path.isabs(candidate):
        return False
    if ".." in re.split(r"[\\/]", candidate) or not candidate.lower().endswith(".md"):
        return False
    try:
        full = os.path.realpath(candidate)
        if not full.lower().endswith(".md") or not os.path.isfile(full):
            return False
        for root in roots:
            base = os.path.realpath(root).rstrip("\\/")
            if os.path.normcase(full).startswith(os.path.normcase(base) + os.sep):
                return True
        return False
    except Exception:
        return False


# A precision read: the small slice the main agent may read itself to attribute a
# claim to file:line, as CLAUDE.md allows. Bounded by span and by count so it cannot become
# the bulk gather the gate exists to route to second_agent.
PRECISION_READ_LINES = 200
PRECISION_READS_PER_MARKER = 2
PRECISION_FILE_BYTES = 1_000_000


def precision_span_ok(full, limit):
    # A `limit` of 1..PRECISION_READ_LINES, or no limit on a file that short. A file too
    # large to count cheaply is not small.
    if limit is not None:
        return isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit <= PRECISION_READ_LINES
    if os.path.getsize(full) > PRECISION_FILE_BYTES:
        return False
    with open(full, "rb") as fh:
        data = fh.read()
    lines = data.count(b"\n") + (0 if not data or data.endswith(b"\n") else 1)
    return lines <= PRECISION_READ_LINES


def precision_read(target, limit, root, rt_dir, marker_key):
    # (allowed, quota_used). Allowed when the Read names an existing file inside the project
    # (never .workflow), its span is small, and fewer than PRECISION_READS_PER_MARKER such
    # reads went through under this marker. The count lives beside the marker, keyed to its
    # set_at, so a new marker starts from zero without anyone deleting the count.
    if not target.strip():
        return False, False
    try:
        if not os.path.isabs(target):
            target = os.path.join(root, target)
        base = os.path.normcase(os.path.realpath(root)).rstrip("\\/")
        full = os.path.realpath(target)
        norm_full = os.path.normcase(full)
        if not norm_full.startswith(base + os.sep) or not os.path.isfile(full):
            return False, False
        if norm_full == os.path.join(base, ".workflow") or norm_full.startswith(os.path.join(base, ".workflow") + os.sep):
            return False, False
        if not precision_span_ok(full, limit):
            return False, False
        counter = os.path.join(rt_dir, "delegated.reads")
        count = 0
        try:
            with open(counter, encoding="utf-8") as fh:
                state = json.load(fh)
            if state.get("marker") == marker_key:
                count = int(state.get("count") or 0)
        except Exception:
            count = 0
        if count >= PRECISION_READS_PER_MARKER:
            return False, True
        with open(counter, "w", encoding="utf-8") as fh:
            json.dump({"marker": marker_key, "count": count + 1}, fh)
        return True, False
    except Exception:
        return False, False


# `cat <one file>` (Get-Content/type in PowerShell): the shell spelling of a Read, allowed
# only where a Read is allowed without a quota — what the host may always read. One path,
# bare or single-quoted, nothing else on the line.
_READ_ONE = re.compile(r"\s*(?:cat|type|Get-Content|gc)\s+(?:'([^'\r\n]*)'|([^\s'\"$`;|&<>(){}*?\[\]]+))\s*")


def shell_read_one(command):
    m = _READ_ONE.fullmatch(command)
    return (m.group(1) if m.group(1) is not None else m.group(2)) if m else None


# `cd <dir> && <rest>`: a shell tool may reset the working directory between calls, so a
# model names the project before the command. The directory is checked against the root;
# the rest goes through plain_words like any other command.
_CD_THEN = re.compile(r"\s*cd\s+(?:'([^'\r\n]*)'|([^\s'\"$`;|&<>(){}*?\[\]]+))\s*&&\s*(.+)", re.DOTALL)


# A bare word: nothing either shell treats as special. Letters of any script (a project root
# may be `proyék`), digits and _ . / : = + % - ~ ^. No quote, no $ ` ; | & < > ( ) { } (bash),
# no @ , # (PowerShell splatting, arrays, comments), no * ? [ ] (globs). `~` passes inside a
# word (HEAD~1) but not at its start, where bash expands it to the home directory.
_BARE_WORD = re.compile(r"(?:[^\W]|[./:=+%^-])(?:[^\W]|[./:=+%^~-])*")


def plain_words(command):
    # The words of a command written so bash and PowerShell read it the same way, or None.
    # A word is bare (see _BARE_WORD) or '...': single quotes are literal in both shells, so
    # whatever sits inside — $(...), ; | & < >, brackets — is text, not code. Words are
    # separated by spaces or tabs; a quote glued to anything (a'b', 'a'b) refuses, as does a
    # quote inside a quote, a newline outside a quoted word (it ends a command in both
    # shells), and a dash word carrying `:` (a PowerShell switch with an argument). A newline
    # inside '...' is text, like everything else there: a multi-line delegated task. Double
    # quotes refuse outright: inside them both shells expand `$`. It refuses some quoting a
    # shell accepts, never the other way round.
    words = []
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if c in " \t":
            i += 1
            continue
        if c == "'":
            j = command.find("'", i + 1)
            if j < 0:
                return None
            words.append(command[i + 1 : j])
            i = j + 1
        else:
            m = _BARE_WORD.match(command, i)
            if not m:
                return None
            word = m.group(0)
            if word.startswith("-") and ":" in word:
                return None
            words.append(word)
            i = m.end()
        if i < n and command[i] not in " \t":
            return None
    return words or None


def runner_call(words, root, home, is_bash):
    # The runner is the command itself — the first word, or the word after a bash the host
    # accepts (`is_bash`) — anchored to this project's .workflow. `bash -c ...`, `bash -x ...`,
    # an interpreter in front, or the runner as a later word (`python x .workflow/run.sh`) is
    # not a runner call.
    i = 1 if words and is_bash(words[0]) else 0
    return i < len(words) and runner_target(local_path(words[i], home), root)


DIFF_SUMMARY = {"--name-only", "--name-status"}
DIFF_ALLOWED = {"--cached", "--staged", "--relative", "--no-renames", "--no-color", "--"}


def diff_words(tokens, root=None, home=None):
    # `git diff` that names files only: --name-only, --name-status or --stat, every other
    # option from a short list that can neither print a patch nor write a file; refs and
    # paths are free. `git -C <dir> diff` counts when <dir> lies inside the project root.
    # plain_words has already rejected shell metacharacters.
    if len(tokens) >= 3 and tokens[:2] == ["git", "-C"] and root and inside_root(tokens[2], root, home):
        tokens = ["git"] + tokens[3:]
    if len(tokens) < 3 or tokens[0] != "git" or tokens[1] != "diff":
        return False
    summary = False
    for tok in tokens[2:]:
        if tok in DIFF_SUMMARY or re.fullmatch(r"--stat(=\d+(,\d+)*)?", tok):
            summary = True
        elif tok in DIFF_ALLOWED:
            continue
        elif tok.startswith("-"):
            return False
    return summary


def preflight_refusal(kind, target, command, marker, ctx):
    """None when a call may pass while `marker` is armed, else the rule that refuses it.

    `kind` is write | shell | read | other, as the host adapter classified the tool;
    `target` is the path a write or read names, `command` the shell command line. `ctx`:
      root, home, rt_dir   the session's project root, home directory, runtime directory;
      is_bash(word)        a bash the host's runner call may start with;
      host_read(path)      what the host may always read (skills, contract, task output);
      limit                a read's line limit, None when the host gives none.
    Rules: "runner_write", "preflight", "preflight_quota" (the precision reads are used).
    """
    root, home, rt_dir = ctx["root"], ctx["home"], ctx["rt_dir"]
    cmd = str((marker or {}).get("command") or "?")
    if kind == "write":
        return "runner_write" if runner_file(target or "", root) else None
    if kind == "shell":
        command = command or ""
        cd = _CD_THEN.fullmatch(command)
        if cd and inside_root(cd.group(1) if cd.group(1) is not None else cd.group(2), root, home):
            command = cd.group(3)
        words = plain_words(command)
        if words is not None:
            if runner_call(words, root, home, ctx["is_bash"]):
                return None
            if cmd == "verify" and diff_words(words, root, home):
                return None
        one = shell_read_one(command)
        if one is not None and ctx["host_read"](one):
            return None
        return "preflight"
    if kind == "read":
        target = target or ""
        if ctx["host_read"](target):
            return None
        if cmd == "verify":
            tests_json = os.path.join(os.path.dirname(rt_dir), "verify", "tests.json")
            if same_path(target, tests_json, root):
                return None
            if same_path(target, os.path.join(root, ".workflow", "config.json"), root):
                return None
        marker_key = (marker or {}).get("set_at")
        if marker_key:
            allowed, quota_used = precision_read(target, ctx.get("limit"), root, rt_dir, str(marker_key))
            if allowed:
                return None
            if quota_used:
                return "preflight_quota"
        return "preflight"
    return "preflight"


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
        "is that request, delegate first: bash '%s/.workflow/run.sh' %s --session '%s' --task '<task>'. "
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
