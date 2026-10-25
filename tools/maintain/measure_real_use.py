#!/usr/bin/env python3
"""Measure real use of one agent-workflow version across local projects (RQ-02, RQ-13).

Joins two streams the maintainer already has, without reading them into the repository:

  1. each project's `.workflow/data/usage.jsonl` — one row per delegated call, written by
     the runtime (ok, verdict, duration, provider tokens);
  2. Claude Code's session transcripts, `~/.claude/projects/<slug>/*.jsonl` — what the user
     typed and what the main agent did.

A transcript belongs to a version when the last `Workflow Main Agent — v<x>` banner it
carries (the global bundle, injected into every session) is that version; a usage row
belongs to it when its `session_id` is a MAIN_SESSION_ID bound in such a transcript.

Per session it counts:

  prompts        human prompts: `core.audit.transcript.is_human_turn`, less interrupt
                 markers and built-in slash commands without arguments; a workflow skill
                 (`/.name`) or a slash command with arguments counts
  prompts_to_first_edit  prompts sent before the first successful edit
  follow_ups     prompts after the first
  rebuttals      follow-ups matching CORRECTION (a heuristic; --sample-rebuttals prints a
                 sample so its precision can be checked by hand, locally)
  interrupts     "[Request interrupted by user" markers
  task size      delegated calls (from usage rows), main-agent tool calls, Claude output
                 tokens
  changes        successful Edit/Write/MultiEdit/NotebookEdit results: distinct files and
                 lines added/removed, from `structuredPatch` or a created file's content
  duration       wall clock first..last record, and active time (gaps over --idle-minutes
                 dropped)

Read-only. Prints one JSON object of counts and distributions. Project names are replaced by
letters unless --show-names is given; prompt text is printed only with --sample-rebuttals,
which is for the local terminal and must never be committed or shared. Not part of the test
suite. Any user of the workflow can run it over their own projects; `--export <file>` writes
the same figures as a file that may be shared, refused if it would carry a path or a project
name (and never combined with --show-names or --sample-rebuttals).

  python tools/maintain/measure_real_use.py --projects-root <dir> --version 3.7.3
  python tools/maintain/measure_real_use.py --projects-root <dir> --version 3.8.0 --export real-use.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from exoma.core.audit import metrics  # noqa: E402
from exoma.core.audit.transcript import EDIT_TOOLS, is_human_turn, project_slug  # noqa: E402

BANNER = re.compile(r"Workflow Main Agent — v(\d+\.\d+\.\d+)")
SESSION_BINDING = re.compile(r"MAIN_SESSION_ID=(main_[A-Za-z0-9_.-]+)")
COMMAND_NAME = re.compile(r"<command-name>\s*(/[^<\s]+)\s*</command-name>")
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
# Beyond core.audit.transcript.is_human_turn: shell-mode echoes and stderr are not asks.
NOT_A_PROMPT = ("<local-command-stderr", "<bash-input", "<bash-stdout", "<bash-stderr", "Caveat:")
INTERRUPT = "[Request interrupted by user"
# A follow-up that pushes back on what the main agent did: says it is wrong or incomplete,
# asks for a retry, or pastes the error it left behind. Indonesian and English, the two
# languages of the maintainer's prompts. Heuristic: check with --sample-rebuttals.
CORRECTION = re.compile(
    r"\b(salah|keliru|bukan (?:itu|ini|begitu|gitu|maksud)|kok\b|(?:kenapa|mengapa) (?:malah|gak|nggak|ga|tidak|masih|jadi|bisa)"
    r"|harusnya|seharusnya|masih (?:error|salah|gagal|belum|sama|muncul|ada)"
    r"|belum (?:jalan|bisa|benar|bener|berhasil|sesuai|muncul|berubah|diberikan|ditambahkan|dibuat|ada|tampil|ke-?handle)"
    r"|(?:tidak|gak|ga|nggak) sesuai|(?:tidak|gak|ga|nggak) (?:jalan|berfungsi|muncul|berubah|tampil)"
    r"|coba (?:lagi|ulang|tes lagi|test lagi|cek lagi)|ulangi|revert|balikin|kembalikan|memang tidak ada"
    r"|sudah saya (?:jalankan|nyalakan|start|buat)|wrong|that's not|not what|still (?:fail|broken|wrong|error)"
    r"|doesn't work|didn't work)\b"
    r"|Traceback \(most recent|SQLSTATE|Exception\b|\bError:|\berror\b.{0,40}\bmuncul",
    re.I,
)

def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A stamp without an offset is read as UTC, so one transcript's stamps always sort together.
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def prompt_text(rec: dict) -> str | None:
    """The human prompt in a record, or None when it is not one.

    The runtime's `is_human_turn` decides first, so a prompt here is a turn there. On top of
    it, an interrupt marker, the summary Claude Code writes when it compacts a session
    (`isCompactSummary`), and a built-in slash command without arguments (`/compact`,
    `/model`, ...) are dropped: none of them asks the main agent for work.
    """
    if not is_human_turn(rec) or rec.get("isCompactSummary"):
        return None
    content = rec["message"]["content"]
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content)
    text = content.strip()
    if text.startswith(INTERRUPT) or text.startswith(NOT_A_PROMPT):
        return None
    if "<command-name>" in text:
        name = COMMAND_NAME.search(text)
        args = COMMAND_ARGS.search(text)
        args = args.group(1).strip() if args else ""
        if not name or not (name.group(1).startswith("/.") or args):
            return None
        return f"{name.group(1)} {args}".strip()
    return text


def patch_lines(result: dict) -> tuple[int, int]:
    """Lines added and removed by one successful edit-tool result."""
    hunks = result.get("structuredPatch")
    if isinstance(hunks, list) and hunks:
        added = removed = 0
        for hunk in hunks:
            for line in hunk.get("lines", []) if isinstance(hunk, dict) else []:
                if line.startswith("+"):
                    added += 1
                elif line.startswith("-"):
                    removed += 1
        return added, removed
    if result.get("type") == "create" and isinstance(result.get("content"), str):
        return len(result["content"].splitlines()), 0
    return 0, 0


def read_transcript(path: str, idle_seconds: float) -> dict:
    s = {"version": None, "sessions": set(), "prompts": [], "interrupts": 0,
         "delegated": Counter(), "tool_calls": 0, "output_tokens": 0, "files": set(),
         "added": 0, "removed": 0, "edits": 0, "commits": 0, "stamps": [],
         "first_edit_after": None}
    pending_edits: dict[str, str] = {}
    seen_messages: dict[str, int] = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            for v in BANNER.findall(line):
                s["version"] = v
            s["sessions"].update(SESSION_BINDING.findall(line))
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("isSidechain"):
                continue
            ts = parse_ts(rec.get("timestamp"))
            if ts and rec.get("type") in ("user", "assistant"):
                s["stamps"].append(ts)
            msg = rec.get("message") or {}
            content = msg.get("content")
            if rec.get("type") == "user":
                if isinstance(content, (str, list)) and INTERRUPT in json.dumps(content, ensure_ascii=False):
                    s["interrupts"] += 1
                text = prompt_text(rec)
                if text:
                    s["prompts"].append(text)
                result = rec.get("toolUseResult")
                if isinstance(content, list) and isinstance(result, dict):
                    for block in content:
                        if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                            continue
                        tool = pending_edits.pop(block.get("tool_use_id"), None)
                        if tool and not block.get("is_error") and result.get("filePath"):
                            added, removed = patch_lines(result)
                            s["files"].add(os.path.normcase(result["filePath"]))
                            s["added"] += added
                            s["removed"] += removed
                            s["edits"] += 1
                            if s["first_edit_after"] is None:
                                s["first_edit_after"] = len(s["prompts"])
            elif rec.get("type") == "assistant":
                mid = msg.get("id")
                out = (msg.get("usage") or {}).get("output_tokens")
                if mid and isinstance(out, int):
                    seen_messages[mid] = max(out, seen_messages.get(mid, 0))
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    s["tool_calls"] += 1
                    name = block.get("name")
                    if name in EDIT_TOOLS:
                        pending_edits[block.get("id")] = name
                    if name in ("Bash", "PowerShell"):
                        cmd = (block.get("input") or {}).get("command", "")
                        if re.search(r"\bgit\s+commit\b", cmd):
                            s["commits"] += 1
    s["output_tokens"] = sum(seen_messages.values())
    stamps = sorted(s.pop("stamps"))
    s["wall_minutes"] = (stamps[-1] - stamps[0]).total_seconds() / 60 if len(stamps) > 1 else 0.0
    active = sum(g for g in ((b - a).total_seconds() for a, b in zip(stamps, stamps[1:]))
                 if g <= idle_seconds)
    s["active_minutes"] = active / 60
    s["start"] = stamps[0].isoformat() if stamps else None
    return s


def dist(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    v = sorted(values)

    def q(p: float) -> float:
        return round(v[min(len(v) - 1, int(round(p * (len(v) - 1))))], 1)

    return {"n": len(v), "total": round(sum(v), 1), "mean": round(statistics.fmean(v), 1),
            "p25": q(.25), "median": q(.5), "p75": q(.75), "p90": q(.9), "max": round(v[-1], 1)}


def load_usage(path: str) -> list[dict]:
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects-root", required=True, help="directory holding the projects")
    ap.add_argument("--transcripts", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--version", required=True, help="workflow version, e.g. 3.7.3")
    ap.add_argument("--exclude", action="append", default=["exoma", "agent-workflow"],
                    help="project directory to leave out (default: the exoma and agent-workflow checkouts)")
    ap.add_argument("--idle-minutes", type=float, default=15.0)
    ap.add_argument("--show-names", action="store_true")
    ap.add_argument("--sample-rebuttals", type=int, default=0, metavar="N",
                    help="print N follow-ups with their classification for a hand check; local only")
    ap.add_argument("--sample-seed", type=int, default=0)
    ap.add_argument("--export", metavar="FILE",
                    help="also write the figures to FILE for sharing; refused if it would carry a path or project name")
    args = ap.parse_args()
    if args.export and (args.show_names or args.sample_rebuttals):
        ap.error("--export cannot be combined with --show-names or --sample-rebuttals")

    projects = sorted(p for p in glob.glob(os.path.join(args.projects_root, "*"))
                      if os.path.isdir(p) and os.path.basename(p) not in args.exclude)
    sessions, usage, labels = [], [], {}
    inputs: list[Path] = []
    for path in projects:
        name = os.path.basename(path)
        tdir = os.path.join(args.transcripts, project_slug(os.path.abspath(path)))
        found = []
        for f in sorted(glob.glob(os.path.join(tdir, "*.jsonl"))):
            s = read_transcript(f, args.idle_minutes * 60)
            if s["version"] == args.version and s["prompts"]:
                found.append(s)
                inputs.append(Path(f))
        if not found:
            continue
        labels[name] = name if args.show_names else f"P{metrics.project_letters(len(labels))}"
        bound = set().union(*(s["sessions"] for s in found))
        for s in found:
            s["project"] = labels[name]
        sessions += found
        usage_path = os.path.join(path, ".workflow", "data", "usage.jsonl")
        inputs.append(Path(usage_path))
        rows = [r for r in load_usage(usage_path) if r.get("session_id") in bound]
        for row in rows:
            row["_project"] = labels[name]
        usage += rows
        # Delegated calls come from the runtime's rows, not from the transcript: a skill runs
        # `.workflow/run` inside a subagent, whose records are the sidechain read_transcript
        # skips. A continuation writes several rows under one prompt id, so count prompt ids;
        # a row no provider answered (verify-browser's closing row, a quick verify) is not one.
        # A row belongs to one transcript only, the earliest that binds its session id.
        owners = metrics.session_owners(found)
        for i, s in enumerate(found):
            calls = {}
            for row in rows:
                if owners.get(row.get("session_id")) == i and row.get("provider"):
                    calls[row.get("prompt_id") or row.get("correlation_id")] = row.get("command")
            s["delegated"] = Counter(calls.values())

    if not sessions:
        print(json.dumps({"version": args.version, "sessions": 0}))
        return 1

    for s in sessions:
        follow = s["prompts"][1:]
        s["follow_ups"] = len(follow)
        s["rebuttal_list"] = [p for p in follow if CORRECTION.search(p[:800])]
        s["rebuttals"] = len(s["rebuttal_list"])

    by_cmd = defaultdict(list)
    for row in usage:
        by_cmd[row.get("command")].append(row)
    verdicts = Counter(r.get("verdict") for r in usage if r.get("command") in ("verify", "verify-browser"))
    changed = [s for s in sessions if s["files"]]
    prompts_total = sum(len(s["prompts"]) for s in sessions)
    follow_total = sum(s["follow_ups"] for s in sessions)
    starts = [s["start"] for s in sessions if s["start"]]
    report = {
        "provenance": metrics.provenance(
            "tools/maintain/measure_real_use.py",
            {"version": args.version, "idle_minutes": args.idle_minutes, "excluded": len(args.exclude)},
            inputs,
            metric_ids=[m for m in metrics.REGISTRY if m.startswith("real_use.")],
        ),
        "version": args.version,
        "window": [min(starts)[:10], max(starts)[:10]] if starts else None,
        "projects": len(labels),
        "sessions": len(sessions),
        "idle_cutoff_minutes": args.idle_minutes,
        "prompts": {
            "per_session": dist([len(s["prompts"]) for s in sessions]),
            "single_prompt_sessions": sum(1 for s in sessions if len(s["prompts"]) == 1),
            "workflow_command_prompts": sum(1 for s in sessions for p in s["prompts"] if p.startswith("/.")),
            "follow_ups_per_session": dist([s["follow_ups"] for s in sessions]),
            "rebuttals_per_session": dist([s["rebuttals"] for s in sessions]),
            "rebuttal_share_of_follow_ups": round(sum(s["rebuttals"] for s in sessions) / follow_total, 3) if follow_total else None,
            "sessions_with_rebuttal": sum(1 for s in sessions if s["rebuttals"]),
            "interrupts_per_session": dist([s["interrupts"] for s in sessions]),
            "total": prompts_total,
        },
        "task_size": {
            "delegated_calls_per_session": dist([sum(s["delegated"].values()) for s in sessions]),
            "delegated_by_command": dict(sum((s["delegated"] for s in sessions), Counter()).most_common()),
            "tool_calls_per_session": dist([s["tool_calls"] for s in sessions]),
            "claude_output_tokens_per_session": dist([s["output_tokens"] for s in sessions]),
        },
        "changes": {
            "sessions_with_changes": len(changed),
            "prompts_to_first_edit": dist([s["first_edit_after"] for s in changed]),
            "files_per_changing_session": dist([len(s["files"]) for s in changed]),
            "lines_added_per_changing_session": dist([s["added"] for s in changed]),
            "lines_removed_per_changing_session": dist([s["removed"] for s in changed]),
            "files_total": sum(len(s["files"]) for s in sessions),
            "lines_added_total": sum(s["added"] for s in sessions),
            "lines_removed_total": sum(s["removed"] for s in sessions),
            "successful_edits_total": sum(s["edits"] for s in sessions),
            "sessions_running_git_commit": sum(1 for s in sessions if s["commits"]),
        },
        "duration_minutes": {
            "wall": dist([s["wall_minutes"] for s in sessions]),
            "active": dist([s["active_minutes"] for s in sessions]),
        },
        "delegated_runtime": {
            "rows": len(usage),
            "ok_rate": round(sum(1 for r in usage if r.get("ok")) / len(usage), 3) if usage else None,
            "error_types": dict(Counter(r.get("error_type") for r in usage if not r.get("ok"))),
            "verify_verdicts": dict(verdicts),
            "duration_seconds": dist([r["duration_seconds"] for r in usage
                                      if isinstance(r.get("duration_seconds"), (int, float))]),
            "by_command": {c: {"rows": len(rs), "ok_rate": round(sum(1 for r in rs if r.get("ok")) / len(rs), 3),
                               "median_seconds": dist([r["duration_seconds"] for r in rs
                                                       if isinstance(r.get("duration_seconds"), (int, float))]).get("median")}
                           for c, rs in sorted(by_cmd.items(), key=lambda kv: -len(kv[1]))},
        },
        "per_project": {
            label: {"sessions": sum(1 for s in sessions if s["project"] == label),
                    "prompts": sum(len(s["prompts"]) for s in sessions if s["project"] == label),
                    "rebuttals": sum(s["rebuttals"] for s in sessions if s["project"] == label),
                    "delegated_rows": sum(1 for r in usage if r["_project"] == label)}
            for label in labels.values()
        },
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.export:
        try:
            # The projects that contributed sessions: a folder with no data cannot reach the
            # figures, and checking every folder name refused exports over unrelated ones.
            metrics.write_export(report, Path(args.export), forbidden=list(labels))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if args.sample_rebuttals:
        follow = [(p, bool(CORRECTION.search(p[:800]))) for s in sessions for p in s["prompts"][1:]]
        random.Random(args.sample_seed).shuffle(follow)
        print("\n# sample of follow-ups (local only; never commit)", file=sys.stderr)
        for text, flagged in follow[:args.sample_rebuttals]:
            one = " ".join(text.split())[:220]
            print(f"[{'R' if flagged else '-'}] {one}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
