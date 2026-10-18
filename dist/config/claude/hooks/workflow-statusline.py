# workflow-statusline.py - Claude Code statusline
# Renders: <project> | Second Agent <tokens> (<cached>) / <calls> | Saved <tokens>
# Every figure is scoped to the CURRENT session.
#
# Second Agent = every token the provider processed for calls bound to this session's
#                MAIN_SESSION_ID, cache read included, falling back to the char-derived
#                estimate on rows no provider counted. The cached share is printed in
#                brackets: on an agentic run it is ~95% of the figure, and a number that
#                large with no explanation beside it reads as a bug rather than a fact.
# Saved        = what the second agent handled that NEVER reached this context: the files
#                and tool output it ingested (fresh input) plus the reasoning it spent
#                (inside output, never emitted as text). Both are facts about where the
#                tokens went, not estimates.
#                Excluded on purpose: cache read, which is the same context re-sent at
#                every internal step rather than new material, and the answer text itself,
#                which DOES arrive here. Claiming the answer as saved would credit the
#                main agent for not reading what it just read.
#                Rows no provider counted cannot answer this - the estimate there measures
#                the prompt, not what was read - so they fall back to
#                premium_context_avoided_tokens and the figure carries `~`, the same
#                measured-else-estimated rule as billable_input/billable_output.
#
# Both numbers carry `~` while any part of them fell back to a char-derived estimate. A
# session whose rows were all provider-counted carries no mark, because nothing in it is
# a guess.
#
# A call that failed before any provider counted it (ok false, no actual_* counts: a rate
# limit, a refused resume) is shown as "N failed" beside the calls and kept out of both
# numbers. Its row holds only the prompt's char estimate, which says nothing about what
# was processed - and folding it in used to put `~` on a session whose every completed
# call was measured.
#
# Input: statusline JSON on stdin. Output: single line on stdout.
# Never fails the prompt (always exit 0). JSON via python.
import json
import os
import sys


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


ESC = "\033"


def color(code, text):
    return "%s[38;5;%sm%s%s[0m" % (ESC, code, text, ESC)


def fmt_tok(n):
    if n >= 1000000:
        return "%.1fM" % (n / 1000000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(int(n))


def main():
    raw = os.environ.get("CLAUDE_STATUSLINE_RAW", "")
    if not raw.strip():
        return
    try:
        ctx = json.loads(raw)
    except Exception:
        ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}

    workspace = ctx.get("workspace") or {}
    proj_path = (
        (workspace.get("project_dir") if isinstance(workspace, dict) else None)
        or (workspace.get("current_dir") if isinstance(workspace, dict) else None)
        or ctx.get("cwd")
        or os.getcwd()
    )
    claude_sid = ctx.get("session_id")

    segments = []
    proj_name = os.path.basename(str(proj_path).rstrip("/\\"))
    if proj_name:
        segments.append(color("39", proj_name))

    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )

    # Claude session id -> MAIN_SESSION_ID mapping lives in session_registry.json,
    # written by hooks/session-bind.sh.
    main_id = None
    registry_path = os.path.join(home, "session_registry.json")
    if claude_sid and os.path.isfile(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as handle:
                entry = (json.load(handle) or {}).get(claude_sid) or {}
            main_id = entry.get("main_session_id") or None
        except Exception:
            main_id = None

    usage_path = os.path.join(workflow_data_dir(str(proj_path)), "usage.jsonl")
    if not os.path.isfile(usage_path):
        sys.stdout.write(color("240", " | ").join(segments))
        return

    # input/output kept apart so the headline can exclude cache read, which lives inside
    # the input count and never inside output.
    tokens = {"input": 0, "output": 0}
    cached = 0
    saved = 0
    measured = True
    saved_measured = True
    # A continuation writes one row per provider invocation, all sharing the command's
    # prompt_id. Counting rows would make this number jump whenever a retry happened,
    # which is not a second call from where the user is sitting.
    seen = set()
    calls = 0
    failed = 0
    try:
        with open(usage_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if len(line) < 2:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue

                if not main_id or row.get("session_id") != main_id:
                    continue

                if (
                    row.get("ok") is False
                    and not isinstance(row.get("actual_input_tokens"), int)
                    and not isinstance(row.get("actual_output_tokens"), int)
                ):
                    failed += 1
                    continue

                # This session only, like the token count beside it. A project-lifetime
                # figure next to a session one reads as a ratio that was never measured:
                # right after /clear the bar said "Second Agent 0 | Saved 129.5k", which
                # invites exactly the wrong conclusion and cost nothing to avoid.
                if isinstance(row.get("actual_input_tokens"), int):
                    cached_here = row.get("actual_cached_input_tokens")
                    if not isinstance(cached_here, int):
                        cached_here = 0
                    saved += max(0, row["actual_input_tokens"] - cached_here)
                    if isinstance(row.get("actual_reasoning_tokens"), int):
                        saved += row["actual_reasoning_tokens"]
                else:
                    avoided = row.get("premium_context_avoided_tokens")
                    if isinstance(avoided, int):
                        saved += avoided
                    saved_measured = False

                # Provider-reported first, char estimate only where nothing was reported.
                # Mirrors billable_input/billable_output: a measured count wins over the
                # estimate beside it, and the breakdowns are never added to the total.
                for slot, actual, estimate in (
                    ("input", "actual_input_tokens", "estimated_input_tokens"),
                    ("output", "actual_output_tokens", "estimated_output_tokens"),
                ):
                    value = row.get(actual)
                    if not isinstance(value, int):
                        value = row.get(estimate)
                    if isinstance(value, int):
                        tokens[slot] += value
                # Subtracted from the headline, never added to it: cache read is already
                # inside the input count above.
                if isinstance(row.get("actual_cached_input_tokens"), int):
                    cached += row["actual_cached_input_tokens"]
                if row.get("token_source") != "provider":
                    measured = False

                prompt_id = row.get("prompt_id")
                if prompt_id:
                    if prompt_id not in seen:
                        seen.add(prompt_id)
                        calls += 1
                else:
                    # A reuse hit never built a prompt, so it has no id to group by and
                    # is its own piece of work.
                    calls += 1
    except Exception:
        pass

    mark = "" if measured else "~"
    # Everything the provider processed for this session, cache read included. The cached
    # share follows in brackets because it is usually most of the figure and, unexplained,
    # a number this large reads as a mistake rather than as a fact.
    text = "Second Agent " + mark + fmt_tok(tokens["input"] + tokens["output"]) + " tok"
    if cached > 0:
        text += " (" + fmt_tok(cached) + " cached)"
    text += " / " + str(calls) + " calls"
    if failed > 0:
        text += ", " + str(failed) + " failed"
    segments.append(color("214", text))
    # Rendered at zero too, like the count beside it. A segment that disappears reads as
    # broken rather than as empty, and the bar changing shape between sessions is exactly
    # what makes a reader distrust the numbers that remain.
    # `~` only where a row fell back to the char-derived estimate. Provider-counted rows
    # make this a measurement, so marking it an estimate unconditionally - as an earlier
    # version did - would understate what is actually known.
    saved_mark = "" if saved_measured else "~"
    segments.append(color("77", "Saved " + saved_mark + fmt_tok(saved) + " tok"))

    sys.stdout.write(color("240", " | ").join(segments))


try:
    main()
except Exception:
    # A broken statusline must never break the prompt.
    pass
