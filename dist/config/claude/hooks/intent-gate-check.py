# intent-gate-check.py - PreToolUse hook (Pre-flight gate: CHECK side).
# Matcher (settings.json): mcp__.*|Read|Grep|Glob|Bash|PowerShell|Write|Edit|MultiEdit|
# NotebookEdit. If a DELEGATED marker is pending (set by intent-gate-set.py, not yet cleared
# by .workflow/run.sh) -> HARD-block: exit 2 with the reason on stderr. Bash and PowerShell
# tools share one allowlist and one tokeniser: one plain call `[bash] <root>/.workflow/
# {run,check,inspect}.sh ...` whose every word is bare or '...' (single quotes: literal in
# both shells), so `$`, backtick, ; | & < > and newline pass only inside single quotes.
# Writes pass except a Write/Edit/MultiEdit/NotebookEdit of the runner scripts themselves.
# A pending verify marker also lets through what skills/verify.md asks before the run: a
# clean `git [-C <root>] diff --name-only|--name-status|--stat ...`, a Read of
# .workflow/config.json or of this session's verify/tests.json, and a Write of that tests.json.
# A command may open with `cd <dir inside the root> && `; the rest is judged on its own.
# Under any pending marker a Read of a skill definition passes: an existing .md whose real
# path lies inside ~/.claude/skills. The skill says how to dispatch; it is not evidence.
# So does a Read of a background task's output (<temp>/claude.../tasks/<id>.output): that is
# where a delegation run in the background returns. `cat <file>` (Get-Content) of either
# passes too, and `~` or /c/... spellings of a path are read the way the shell reads them.
# So does a precision read (DEC-043): a project file outside .workflow, at most 200 lines
# (`limit`, or a file that short), two per marker, counted in runtime/delegated.reads.
# Escapes: WORKFLOW_LOCAL_MODE=1 / local_mode.flag. Marker/session absent -> allow (fail-open).
# exit 2 = block ; every other path exits 0. Exit code flows from python (no trailing exit).
import os, sys, json, re, datetime, tempfile


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


# Fail-open leaves no trace, and that is the problem: a hook that dies on a malformed
# registry exits 0 exactly like a hook that found nothing to block, so the enforcement
# layer can be dead for an entire session with nothing to show for it. Record the fault
# and still exit 0 — non-wedging, just no longer silent. Written ONLY on real faults,
# never on the normal allow/block paths, and overwritten rather than appended.
runtime_dir = None


def hook_warning(kind, message):
    try:
        # Session dir when it is known. The fault most worth recording — an unparseable
        # registry — happens BEFORE that dir can be resolved, so a session-only location
        # would miss exactly the case this exists for; ~/.claude is the fallback.
        target = runtime_dir or os.path.join(os.path.expanduser("~"), ".claude")
        if not target:
            return
        os.makedirs(target, exist_ok=True)
        payload = {
            "hook": "intent-gate-check",
            "kind": kind,
            "message": str(message),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with open(
            os.path.join(target, "hook-warning.json"), "w", encoding="utf-8"
        ) as fh:
            json.dump(payload, fh, indent=2)
    except Exception:
        pass


def same_path(candidate, expected, base):
    # True when candidate names expected. A relative path resolves against the project root.
    if not candidate.strip():
        return False
    try:
        if not os.path.isabs(candidate):
            candidate = os.path.join(base, candidate)
        a = os.path.normcase(os.path.abspath(candidate))
        b = os.path.normcase(os.path.abspath(expected))
        return a == b
    except Exception:
        return False


RUNNERS = ("run.sh", "check.sh", "inspect.sh")


def runner_target(candidate, root):
    # True when candidate names exactly <root>/.workflow/{run,check,inspect}.sh AND the file
    # it reaches, every link followed, is still inside this project's .workflow: a run.sh
    # that is a symlink to a script elsewhere is not the runner (DEC-068). A runner that does
    # not exist yet passes like before: running it fails, and the gate refuses writes to it
    # while armed, so nothing can be put there between this check and the call.
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


def local_path(path, home):
    # A path the way the shell that wrote it reads it: `~` is the home directory in bash and
    # PowerShell alike, and Git Bash spells E:\x as /e/x. Anything else is returned as is.
    if path == "~" or path.startswith(("~/", "~\\")):
        path = os.path.join(home, path[2:])
    return msys_path(path)


def skill_read(candidate, home):
    # True when a Read names a skill definition: an existing .md file whose real path (every
    # link followed) lies inside the real ~/.claude/skills. A skill is the instruction the
    # agent must read to know how to dispatch, not evidence, so blocking it before the run
    # locked the two together. `~` and /c/... spellings are read as the shell reads them;
    # after that an absolute path only, no `..` in it; a link may point anywhere inside the
    # skills folder, never out of it.
    candidate = local_path(candidate, home)
    if not candidate.strip() or not os.path.isabs(candidate):
        return False
    if ".." in re.split(r"[\\/]", candidate) or not candidate.lower().endswith(".md"):
        return False
    try:
        skills = os.path.realpath(os.path.join(home, ".claude", "skills")).rstrip("\\/")
        full = os.path.realpath(candidate)
        if not full.lower().endswith(".md") or not os.path.isfile(full):
            return False
        return os.path.normcase(full).startswith(os.path.normcase(skills) + os.sep)
    except Exception:
        return False


# A precision read (DEC-043): the small slice the main agent may read itself to attribute a
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


def precision_read(ti, root, runtime_dir, marker_key):
    # (allowed, quota_used). Allowed when the Read names an existing file inside the project
    # (never .workflow), its span is small, and fewer than PRECISION_READS_PER_MARKER such
    # reads went through under this marker. The count lives beside the marker, keyed to its
    # set_at, so a new marker starts from zero without anyone deleting the count.
    target = str(ti.get("file_path") or "")
    if not target.strip():
        return False, False
    try:
        if not os.path.isabs(target):
            target = os.path.join(root, target)
        base = os.path.normcase(os.path.realpath(root)).rstrip("\\/")
        full = os.path.realpath(target)
        norm = os.path.normcase(full)
        if not norm.startswith(base + os.sep) or not os.path.isfile(full):
            return False, False
        if norm == os.path.join(base, ".workflow") or norm.startswith(os.path.join(base, ".workflow") + os.sep):
            return False, False
        if not precision_span_ok(full, ti.get("limit")):
            return False, False
        counter = os.path.join(runtime_dir, "delegated.reads")
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


def task_output(candidate, home):
    # True when a Read names the output file of one of Claude Code's own background tasks:
    # <temp>/claude.../<...>/tasks/<id>.output. That is how a delegated run started in the
    # background hands back its result, so blocking it blocked reading the delegation itself.
    # Undocumented layout: if Claude Code moves it, this stops matching and the read is gated
    # again, as before.
    candidate = local_path(candidate, home)
    if not candidate.strip() or not os.path.isabs(candidate):
        return False
    try:
        full = os.path.realpath(candidate)
        if not os.path.isfile(full) or not re.fullmatch(r"[\w.-]+\.output", os.path.basename(full)):
            return False
        parts = re.split(r"[\\/]", os.path.normcase(full))
        if len(parts) < 3 or parts[-2] != "tasks" or not any(p.startswith("claude") for p in parts[:-2]):
            return False
        roots = {os.path.normcase(os.path.realpath(tempfile.gettempdir()))}
        if os.name != "nt":
            roots.add(os.path.normcase(os.path.realpath("/tmp")))
        return any(os.path.normcase(full).startswith(r.rstrip("\\/") + os.sep) for r in roots)
    except Exception:
        return False


def msys_path(path):
    # Git Bash spells E:\x as /e/x (or /cygdrive/e/x); read it the way that shell does.
    m = re.fullmatch(r"/(?:cygdrive/)?([A-Za-z])(/.*)?", path) if os.name == "nt" else None
    return (m.group(1) + ":" + (m.group(2) or "/")) if m else path


def inside_root(path, root, home):
    # True when path (as the shell reads it) is the project root or lies inside it.
    try:
        full = os.path.normcase(os.path.realpath(local_path(path, home))).rstrip("\\/")
        base = os.path.normcase(os.path.realpath(root)).rstrip("\\/")
        return full == base or full.startswith(base + os.sep)
    except Exception:
        return False


# `cat <one file>` (Get-Content/type in PowerShell): the shell spelling of a Read, allowed
# only where a Read is allowed without a quota — a skill definition or a background task's
# output. One path, bare or single-quoted, nothing else on the line.
_READ_ONE = re.compile(r"\s*(?:cat|type|Get-Content|gc)\s+(?:'([^'\r\n]*)'|([^\s'\"$`;|&<>(){}*?\[\]]+))\s*")


def shell_read_one(command):
    m = _READ_ONE.fullmatch(command)
    return (m.group(1) if m.group(1) is not None else m.group(2)) if m else None


# `cd <dir> && <rest>`: Claude's Bash tool resets the working directory between calls, so a
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


def runner_call(words, root):
    # The runner is the command itself — the first word, or the word after a bare `bash` —
    # anchored to this project's .workflow. `bash -c ...`, `bash -x ...`, an interpreter in
    # front, or the runner as a later word (`python x .workflow/run.sh`) is not a runner call.
    i = 1 if words and words[0] in ("bash", "bash.exe") else 0
    return i < len(words) and runner_target(local_path(words[i], os.path.expanduser("~")), root)


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


try:
    raw = globals().get("HOOK_RAW") or os.environ.get("CLAUDE_HOOK_RAW", "")
    if not raw.strip():
        sys.exit(0)
    payload = json.loads(raw)
    claude_sid = payload.get("session_id")
    tool_name = str(payload.get("tool_name") or "")
    cwd = payload.get("cwd")
    if not claude_sid:
        sys.exit(0)

    # global env escape
    if os.environ.get("WORKFLOW_LOCAL_MODE") == "1":
        sys.exit(0)

    home = os.path.expanduser("~")
    registry_path = os.path.join(home, ".claude", "session_registry.json")
    if not os.path.isfile(registry_path):
        sys.exit(0)
    with open(registry_path, "r", encoding="utf-8") as f:
        reg = json.load(f) or {}
    entry = reg.get(claude_sid)
    if not entry:
        sys.exit(0)
    main_id = str(entry.get("main_session_id") or "")
    root = str(entry.get("cwd") or "") or str(cwd or "")
    if not main_id or not root:
        sys.exit(0)

    runtime_dir = os.path.join(workflow_data_dir(root), "sessions", main_id, "runtime")
    marker = os.path.join(runtime_dir, "delegated.marker")
    local_flag = os.path.join(runtime_dir, "local_mode.flag")

    if os.path.isfile(local_flag):
        sys.exit(0)
    if not os.path.isfile(marker):
        sys.exit(0)

    cmd = "?"
    marker_key = None
    try:
        with open(marker, "r", encoding="utf-8") as f:
            marker_obj = json.load(f)
        cmd = str(marker_obj.get("command") or "?")
        marker_key = marker_obj.get("set_at")
    except Exception as exc:
        cmd = "?"
        hook_warning("marker_unreadable", exc)

    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        ti = {}

    # File writes: only the runner scripts are refused. Rewriting run.sh and then running it
    # would make the runner allowlist below an arbitrary command.
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        target = str(ti.get("file_path") or ti.get("notebook_path") or "")
        if runner_file(target, root):
            sys.stderr.write((
                "[PRE-FLIGHT GATE] intent=DELEGATED (%s) is pending and '%s' targets a runner script: %s\n"
                "The runner is the one command this gate lets through, so it cannot be rewritten while the gate is armed.\n"
                "Do this instead: run bash .workflow/run.sh %s '<task>' '%s' as shipped.\n"
                "False positive? Escapes: export WORKFLOW_LOCAL_MODE=1, create %s, or delete %s.\n"
            ) % (cmd, tool_name, target, cmd, main_id, local_flag, marker))
            sys.exit(2)
        sys.exit(0)

    # Bash and PowerShell tools: one tokeniser, one rule. Only a plain runner call passes;
    # under verify, so does the file-name diff that tells main_agent which tests to pick
    # (skills/verify.md).
    if tool_name in ("Bash", "PowerShell"):
        command = str(ti.get("command") or "")
        cd = _CD_THEN.fullmatch(command)
        if cd and inside_root(cd.group(1) if cd.group(1) is not None else cd.group(2), root, home):
            command = cd.group(3)
        words = plain_words(command)
        if words is not None:
            if runner_call(words, root):
                sys.exit(0)
            if cmd == "verify" and diff_words(words, root, home):
                sys.exit(0)
        one = shell_read_one(command)
        if one is not None and (skill_read(one, home) or task_output(one, home)):
            sys.exit(0)

    # Any command: a Read of a skill definition, the instruction that says how to dispatch,
    # or of a background task's output, the result of a delegation started in the background.
    if tool_name == "Read" and (skill_read(str(ti.get("file_path") or ""), home)
                                or task_output(str(ti.get("file_path") or ""), home)):
        sys.exit(0)

    # verify: the test allowlist and this session's test request, nothing else.
    if cmd == "verify" and tool_name in ("Read", "Write"):
        target = str(ti.get("file_path") or "")
        tests_json = os.path.join(os.path.dirname(runtime_dir), "verify", "tests.json")
        if same_path(target, tests_json, root):
            sys.exit(0)
        if tool_name == "Read" and same_path(target, os.path.join(root, ".workflow", "config.json"), root):
            sys.exit(0)

    # Any command: a precision read, small and counted. A marker without set_at cannot key a
    # count, so it gets none.
    quota_used = False
    if tool_name == "Read" and marker_key:
        allowed, quota_used = precision_read(ti, root, runtime_dir, str(marker_key))
        if allowed:
            sys.exit(0)

    what = (
        "a shell read (cat/rg/grep/git show) -- reading the codebase is second_agent's job"
        if tool_name in ("Bash", "PowerShell")
        else "a bulk-gather tool"
    )
    reason = (
        "[PRE-FLIGHT GATE] intent=DELEGATED (%s) but .workflow/run has NOT run this turn.\n"
        "Tool '%s' is %s -- FORBIDDEN before delegation (Division of Labor: gather = second_agent).\n"
        "%s"
        "Do this instead: bash .workflow/run.sh %s '<task>' '%s'\n"
        "Single quotes, so $ ; | & < > and brackets in the task are text; no apostrophe inside.\n"
        "That routes evidence to second_agent AND clears this gate.\n"
        "False positive? Escapes: export WORKFLOW_LOCAL_MODE=1, create %s, or delete %s."
    ) % (
        cmd, tool_name, what,
        ("The %d precision reads this delegation allows are used.\n" % PRECISION_READS_PER_MARKER) if quota_used
        else ("A precision read passes: a project file, at most %d lines (`limit`, or a file that short), %d per delegation.\n"
              % (PRECISION_READ_LINES, PRECISION_READS_PER_MARKER)) if tool_name == "Read" else "",
        cmd, main_id, local_flag, marker,
    )
    sys.stderr.write(reason + "\n")
    sys.exit(2)
except SystemExit:
    raise
except Exception as exc:
    # on any hook error, fail-open (never wedge the agent) — but leave the reason behind
    hook_warning("hook_error", exc)
    sys.exit(0)
