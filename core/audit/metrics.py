"""The calculation contract: every benchmark figure has an ID, a version and a stated method.

Three producers compute figures this project reports: the runtime's `--command report`
(`core/audit/telemetry.py`, `task_telemetry.py`) and the two offline tools
`tools/maintain/measure_real_use.py` and `measure_task_outcomes.py`. They used the same words
for different things — "task" meant a correlation id, a session run closed by a verify pass,
and a labelled transcript; "saved" had three formulas — and their statistics differ. This
registry does not unify them, because doing so would silently change figures already recorded
(CASE-011, CASE-012). It names each one apart, states exactly how it is computed, and versions
it: a figure is cited as `<id>@<version>`, and a change to how a metric is computed bumps its
version instead of quietly meaning something else.

Every producer also stamps its output with `provenance()`: the registry version, the tool,
the tool's commit, the parameters it ran with, and a hash over its inputs. The inputs are
private (Claude Code transcripts, usage streams, project git history), so a figure can be
recounted only on the machine that holds them: any user can measure their own use with the
same tools, and nobody can recount another user's figure. `RECOUNT` says so in every stamp.
`write_export` writes a figure set that may leave that machine: aggregates and the stamp,
refused when any value falls outside an allowlist of identifier shapes or holds a project name.

The registry is documented in docs/evaluation/metrics.md; a test keeps the two in step.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from utils import osutil

METRICS_VERSION = 1

RECOUNT = (
    "on the machine holding the inputs only: inputs are private transcripts, usage streams and "
    "git history; the method is public, the data is not"
)

# Statistic conventions, named once. Each metric says which one it uses.
STATISTICS = {
    "median_interpolated": "statistics-style median (mean of the two middle values), rounded to 2 decimals",
    "nearest_rank": "sorted values, index round(p * (n - 1)) with Python's half-to-even round; rounded to 1 decimal",
    "median_stdlib": "statistics.median, rounded to 1 decimal",
    "p90_floor": "sorted values, index floor(0.9 * (n - 1))",
    "count": "a count; no statistic",
    "rate": "numerator / denominator, rounded to 3 decimals; None when the denominator is 0",
    "sum": "a sum over the unit",
}

_RUNTIME = "core/audit/telemetry.py report()"
_TASKS = "core/audit/task_telemetry.py report()"
_REAL_USE = "tools/maintain/measure_real_use.py"
_OUTCOMES = "tools/maintain/measure_task_outcomes.py"

REGISTRY: dict[str, dict] = {
    # --- runtime: usage.jsonl ------------------------------------------------------------
    "runtime.command": {
        "version": 1, "producer": _RUNTIME, "unit": "command", "statistic": "count",
        "definition": "A delegated command: usage rows grouped by prompt_id (a continuation is one); a row without prompt_id is its own command. Report key `calls`/`commands`.",
        "denominator": "-",
        "missing": "Rows of a provider-less browser run (no prompt_id, provider or tokens; CASE-014) are dropped before counting.",
    },
    "runtime.provider_call": {
        "version": 1, "producer": _RUNTIME, "unit": "provider invocation", "statistic": "count",
        "definition": "One usage row. Report key `provider_calls`.",
        "denominator": "-", "missing": "As runtime.command.",
    },
    "runtime.cost_tokens": {
        "version": 1, "producer": _RUNTIME, "unit": "tokens", "statistic": "sum",
        "definition": "Input plus output per row: the provider's actual count when present, else characters // 4. Cached input and reasoning are inside these counts, never added. Report key `cost`.",
        "denominator": "-", "missing": "`token_source` says which rows were estimated.",
    },
    "runtime.accepted_task": {
        "version": 1, "producer": _RUNTIME, "unit": "correlation_id", "statistic": "count",
        "definition": "A correlation id (plan chain or derived task id) with at least one verify row whose derived verdict is pass. Report key `accepted_tasks`.",
        "denominator": "correlation ids with at least one judged verify",
        "missing": "Never-verified ids are outside the denominator, not counted as failures.",
    },
    "runtime.first_pass_correctness": {
        "version": 1, "producer": _RUNTIME, "unit": "correlation_id", "statistic": "rate",
        "definition": "Share of judged correlation ids whose first judged verify passed.",
        "denominator": "correlation ids with at least one judged verify", "missing": "As runtime.accepted_task.",
    },
    "runtime.rework": {
        "version": 1, "producer": _RUNTIME, "unit": "correlation_id", "statistic": "rate",
        "definition": "A judged correlation id whose judged verify rounds, ordered by recorded_at, hold a round that did not pass (fail or incomplete) followed by another round. extra_verification_rounds counts the rounds after that first failure. All-pass rounds (a plan verified phase by phase) are not rework (DEC-051). Report key `rework`.",
        "denominator": "correlation ids with at least one judged verify", "missing": "As runtime.accepted_task.",
    },
    "runtime.time_to_completion": {
        "version": 1, "producer": _RUNTIME, "unit": "seconds per command", "statistic": "median_interpolated",
        "definition": "Summed provider duration_seconds of one command's rows. Browser time (browser_seconds) and runtime test time (tests_seconds) are not included.",
        "denominator": "commands with at least one measured duration", "missing": "Unmeasured commands are counted apart, never averaged in.",
    },
    "runtime.evidence_reuse_rate": {
        "version": 1, "producer": _RUNTIME, "unit": "command", "statistic": "rate",
        "definition": "Exploration and reasoning commands served from a stored artifact. verify-browser drafts are not eligible.",
        "denominator": "exploration and reasoning commands", "missing": "Rows without a role are named, not placed.",
    },
    "runtime.context_avoided": {
        "version": 1, "producer": _RUNTIME, "unit": "tokens", "statistic": "sum",
        "definition": "(answer characters - digest characters) // 4 on the row carrying the digest. Report key `premium_context_avoided`.",
        "denominator": "-", "missing": "Rows without a digest contribute nothing.",
    },
    # --- runtime: tasks.jsonl -------------------------------------------------------------
    "runtime.workflow_task": {
        "version": 1, "producer": _TASKS, "unit": "event run in one MAIN_SESSION_ID", "statistic": "count",
        "definition": "Events from a session's first event to a verify whose derived verdict is pass after an edit (DEC-020). States completed, open, read_only, unknown.",
        "denominator": "-", "missing": "Events without a session are `unknown`; events before DEC-020 carry no derived verdict and stay open.",
    },
    # --- offline: measure_real_use ---------------------------------------------------------
    "real_use.session": {
        "version": 1, "producer": _REAL_USE, "unit": "Claude Code transcript", "statistic": "count",
        "definition": "One transcript file whose last bundle banner names the requested version, with at least one human prompt.",
        "denominator": "-", "missing": "Transcripts without a banner are excluded.",
    },
    "real_use.prompts_per_session": {
        "version": 1, "producer": _REAL_USE, "unit": "prompt", "statistic": "nearest_rank",
        "definition": "Human turns, excluding interrupts, compaction summaries, shell echoes and argument-less built-in slash commands.",
        "denominator": "sessions", "missing": "-",
    },
    "real_use.rebuttal": {
        "version": 1, "producer": _REAL_USE, "unit": "follow-up prompt", "statistic": "count",
        "definition": "A follow-up whose first 800 characters match the CORRECTION pattern (Indonesian and English). Recall measured 0.08 in CASE-011: a lower bound.",
        "denominator": "follow-up prompts", "missing": "-",
    },
    "real_use.active_minutes": {
        "version": 1, "producer": _REAL_USE, "unit": "minutes per session", "statistic": "nearest_rank",
        "definition": "Sum of gaps between consecutive transcript timestamps no longer than --idle-minutes (default 15).",
        "denominator": "sessions", "missing": "-",
    },
    "real_use.delegated_call": {
        "version": 2, "producer": _REAL_USE, "unit": "prompt answered by a provider", "statistic": "count",
        "definition": "Distinct prompt_id (else correlation_id) among a session's usage rows that name a provider. A usage row belongs to one session: the earliest-starting transcript that binds its MAIN_SESSION_ID (@1 joined it to every such transcript; identical on all data recorded up to 2026-10-04, where no id was bound twice). Its `delegated_runtime` block is per row, not per command.",
        "denominator": "-", "missing": "-",
    },
    # --- offline: measure_task_outcomes ----------------------------------------------------
    "outcome.coding_task": {
        "version": 1, "producer": _OUTCOMES, "unit": "labelled transcript", "statistic": "count",
        "definition": "A real_use.session labelled with a coding category (feature, bug, performance, migration, refactor) in the labels file.",
        "denominator": "-", "missing": "Unlabelled sessions are counted apart (`unlabelled_sessions`).",
    },
    "outcome.solved": {
        "version": 1, "producer": _OUTCOMES, "unit": "coding task", "statistic": "rate",
        "definition": "The first first-parent state of `main`, from session start to end + --search-days (30), holding at least --solved-share (0.8) of the lines the session left (Edit/Write/MultiEdit lines of 8+ characters it did not itself remove). 0.3 to 0.8 is partial; below is not in main.",
        "denominator": "coding tasks", "missing": "A repository without `main` grades every task not_in_main.",
    },
    "outcome.solved_fixed": {
        "version": 1, "producer": _OUTCOMES, "unit": "solved task", "statistic": "count",
        "definition": "A solved task whose lines changed on main within --fix-hours (24) of landing, in a commit by an author of the commits that brought them in (author window: -5 min before start, +1 min after landing).",
        "denominator": "-", "missing": "-",
    },
    "outcome.feature_segment": {
        "version": 1, "producer": _OUTCOMES, "unit": "correlation id within one session", "statistic": "count",
        "definition": "A coding task's tasks.jsonl events split by the correlation id of the verify that judged them: a verify's prompt_id joined to usage.jsonl (`direct`), through a verify-browser run's provider_prompt_ids in quality.jsonl (`browser`), or, without a prompt_id, the nearest verify usage row of the same session within 120 s (`inferred`); else `unlinked`. Edits join the next verify of their session; edits after the last one join the last segment, or `unverified`. Report keys `segments_per_task_median`, `segment_links`.",
        "denominator": "-", "missing": "Sessions before 3.8.0 wrote no task events and have no segments.",
    },
    "outcome.solved_fixed_outside_feature": {
        "version": 1, "producer": _OUTCOMES, "unit": "solved task", "statistic": "count",
        "definition": "An outcome.solved_fixed@1 task whose fix no continuation explains. A task continues an earlier one of the same project when it starts at most --link-hours (72) after that one ended and edits a file that one left lines in; with several such earlier tasks it is `ambiguous` and continues none. A fix is explained when a continuation of the task started within --fix-hours of landing and edited every file whose lines were lost. No author check: transcripts carry none.",
        "denominator": "-", "missing": "-",
    },
    "outcome.continuation_cost": {
        "version": 1, "producer": _OUTCOMES, "unit": "coding task", "statistic": "median_stdlib",
        "definition": "Delegated calls (distinct prompt_id with a provider) and their input tokens (actual, else estimated) recorded before the session's first edit in tasks.jsonl. Reported for continuations and for tasks that continue nothing. Report keys `ramp_up_*`.",
        "denominator": "tasks with a recorded edit", "missing": "Tasks without task events (before 3.8.0) are left out.",
    },
    "outcome.context_final": {
        "version": 1, "producer": _OUTCOMES, "unit": "tokens", "statistic": "median_stdlib",
        "definition": "input + cache_read + cache_creation of the task's last main-agent message. p90 uses p90_floor.",
        "denominator": "tasks with usage", "missing": "Tasks without usage are left out of the statistic.",
    },
    "outcome.context_saved": {
        "version": 2, "producer": _OUTCOMES, "unit": "tokens per task", "statistic": "median_stdlib",
        "definition": "Second agent's fresh input (input - cached) minus what returned to the main agent (response characters // 4), floored at 0; reasoning is reported apart, not counted. Usage rows belong to the earliest-starting transcript that binds their MAIN_SESSION_ID. p90 uses p90_floor. (@1 computed the same net, joined rows to every binding transcript, and its definition text wrongly added reasoning.)",
        "denominator": "coding tasks", "missing": "Rows without provider counts fall back to estimates; such tasks are counted in context_saved_partly_estimated_tasks.",
    },
}


def tool_commit(repo_root: Path | None = None) -> str | None:
    """The producing tool's commit, with `+dirty` when its tree has uncommitted changes."""
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    try:
        # Only when the install root is itself the top of a work tree: an install copied into
        # another repository (a project, a dotfiles repo) would stamp that repository's commit.
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=10, **osutil.hidden_run_kwargs())
        if top.returncode != 0 or not top.stdout.strip():
            return None
        if os.path.normcase(str(Path(top.stdout.strip()).resolve())) != os.path.normcase(str(root.resolve())):
            return None
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                              **osutil.hidden_run_kwargs())
        if head.returncode != 0:
            return None
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=10, **osutil.hidden_run_kwargs())
        # A status that failed says nothing about the tree: an empty stdout from it is not
        # a clean tree, and a bare hash would claim one.
        if dirty.returncode != 0:
            return None
        return head.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return None


def inputs_digest(paths) -> dict:
    """A hash over the inputs' contents, with no path in it: names would identify projects.

    Fail-open: an input that cannot be read is left out of the hash and counted in
    `unreadable` (present only when non-zero), so the stamp never breaks the report it is
    attached to, and a hash taken without that input does not pass for one over all of them.
    """
    digest = hashlib.sha256()
    count = 0
    total = 0
    unreadable = 0
    for path in sorted(str(p) for p in paths):
        file = Path(path)
        try:
            if not file.is_file():
                continue
            data = file.read_bytes()
        except OSError:
            unreadable += 1
            continue
        digest.update(hashlib.sha256(data).digest())
        count += 1
        total += len(data)
    out = {"files": count, "bytes": total, "sha256": digest.hexdigest()}
    if unreadable:
        out["unreadable"] = unreadable
    return out


def provenance(producer: str, params: dict, inputs, metric_ids=None) -> dict:
    """The stamp every benchmark output carries."""
    ids = list(metric_ids) if metric_ids is not None else [k for k, v in REGISTRY.items() if v["producer"] == producer]
    return {
        "metrics_version": METRICS_VERSION,
        "metrics": {mid: REGISTRY[mid]["version"] for mid in ids if mid in REGISTRY},
        "producer": producer,
        "tool_commit": tool_commit(),
        "params": params,
        "inputs": inputs_digest(inputs),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recount": RECOUNT,
    }


def project_letters(index: int) -> str:
    """The letters standing in for the index-th project (0-based): A..Z, then AA, AB, ..."""
    out = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        out = chr(ord("A") + rest) + out
    return out


def session_owners(transcripts: list[dict]) -> dict[str, int]:
    """Which transcript owns each MAIN_SESSION_ID: the earliest-starting one that binds it.

    A usage row is joined to a transcript by the session id it carries; a resumed or copied
    transcript can bind the same id again, and joining the row to both would count it twice.
    Index into `transcripts`; ties keep the earlier index.
    """
    order = sorted(range(len(transcripts)), key=lambda i: (transcripts[i].get("start") or "~", i))
    owners: dict[str, int] = {}
    for i in order:
        for sid in transcripts[i].get("sessions") or ():
            owners.setdefault(sid, i)
    return owners


# What a figure set that leaves the machine may carry: an allowlist of shapes, not a list of
# what to refuse, because a blacklist of path forms always misses one (`cwd:/home/u`,
# `file:///x`, `~u/x`, `$HOME/x`, an email address). Numbers, booleans and null pass; a string
# passes only in an identifier shape — a metric id, a version, a commit hash with `+dirty`, an
# ISO date or timestamp, a command, verdict or error_type name — with no separator and no
# space. A key may also hold single spaces between such words (a label like `Project A`).
_VALUE_SHAPE = re.compile(r"[A-Za-z0-9_.:+-]{1,64}")
_KEY_SHAPE = re.compile(r"[A-Za-z0-9_.:+-](?:[A-Za-z0-9_.:+ -]{0,62}[A-Za-z0-9_.:+-])?")
# The fixed texts a stamp carries outside that shape, accepted only verbatim and only where
# provenance() puts them: they are this module's constants, not data.
_PRODUCERS = frozenset(m["producer"] for m in REGISTRY.values())
_FIXED = {("provenance", "recount"): frozenset({RECOUNT}), ("provenance", "producer"): _PRODUCERS}
# The maps a producer keys by project. Their keys are data, not field names: each must be a
# label the tools write (`PA`, `Project AB`), so a folder name there is refused by shape,
# whatever its length, without matching labels against names they may share a letter with.
_PROJECT_MAPS = frozenset({"per_project", "by_project"})
_LABEL_SHAPE = re.compile(r"(?:P|Project )[A-Z]+")


def _key_text(key) -> str:
    """A key as JSON writes it (None -> null, True -> true)."""
    return key if isinstance(key, str) else json.dumps(key)


def _where(path: tuple) -> str:
    out = "$"
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def _check_shape(value, path: tuple, values: list) -> None:
    """Refuse anything outside the allowlist, naming its JSON path and never its content.

    Collects every data string (with its path) into `values` for the project-name check.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if path in _FIXED and value in _FIXED[path]:
            return
        if not _VALUE_SHAPE.fullmatch(value):
            raise ValueError(f"export refused: the value at {_where(path)} is not an identifier-shaped string")
        values.append((path, value))
        return
    if isinstance(value, dict):
        labelled = bool(path) and path[-1] in _PROJECT_MAPS
        for key, item in value.items():
            text = _key_text(key)
            if not _KEY_SHAPE.fullmatch(text):
                raise ValueError(f"export refused: a key under {_where(path)} is not identifier-shaped")
            if labelled and not _LABEL_SHAPE.fullmatch(text):
                raise ValueError(f"export refused: a key under {_where(path)} is not a project label")
            _check_shape(item, path + (text,), values)
        return
    if isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            _check_shape(item, path + (i,), values)
        return
    raise ValueError(f"export refused: the value at {_where(path)} is a {type(value).__name__}, not JSON data")


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-z]+", text.lower()) if t]


def _holds(tokens: list[str], name: list[str]) -> bool:
    """`name`'s tokens appear as a contiguous run in `tokens`: whole words, never a substring."""
    n = len(name)
    return any(tokens[i:i + n] == name for i in range(len(tokens) - n + 1))


def write_export(report: dict, dest: Path, forbidden=()) -> Path:
    """Write a figure set meant to leave this machine, or refuse with ValueError.

    Every key and value of `report` must fit the allowlist above; the export header
    (`schema`, `recount`) is added after the check, by construction. `forbidden` holds the
    names of the projects whose data went into the report; a data value holding one as whole
    words (case-insensitive: `data` does not match `metadata`, `main` not `maintain`) is
    refused, at any length: a project named `ui` is as identifying as one named `shop-api`.
    Keys are not read for names: they are the tool's own field names, except under a
    per-project map (`per_project`, `by_project`), where every key must be a project label.
    The error names the JSON path and which forbidden name, by position, collided — never
    the value. The caller decides what goes in: aggregates and the provenance stamp, never
    prompt text.
    """
    if not isinstance(report, dict):
        raise ValueError("export refused: the report is not a JSON object")
    values: list = []
    _check_shape(report, (), values)
    names = [(i, _tokens(w)) for i, w in enumerate(forbidden) if w]
    for path, value in values:
        tokens = _tokens(value)
        for i, name in names:
            if name and _holds(tokens, name):
                raise ValueError(f"export refused: the value at {_where(path)} holds forbidden project name #{i + 1}")
    payload = {"export": {"schema": 1, "recount": RECOUNT}, **report}
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    out = Path(dest)
    out.write_text(text + "\n", encoding="utf-8")
    return out
