"""Export unlabelled, shuffled samples for a blind judge (EXP-008, EXP-010, EXP-014).

The judge is a fresh session that never saw the key: it reads `items/*.jsonl`, writes one label
per item id into a file shaped like `label-schema.json`, and nothing else. The key, which ties
an item id to its transcript, project and arm, sits apart in `key/` and is read only when
the labels are merged back. Three item sets:

  exp014    every execution-gate block found in a transcript: the blocked tool call, the gate's
            message, the prompt before it and what followed. Label: legitimate | false_block |
            unclear. Blocks the hook logged (runtime/gate-blocks.jsonl) are counted beside them.
  category  transcripts of --category-version not yet in the labels file, as stage 1 of
            EXP-010: first prompt and follow-ups. Label: one of CATEGORIES. `--merge` writes the
            judge's labels into a copy of the labels file.
  exp008    EXP-008's sample of labelled 3.6.0/3.7.3 coding tasks: 12 solved clean, 8 solved
            and fixed within 24 h (the arm is hidden from the judge), every partial task; the
            first prompt and the lines the session left. Label: EXP-008's rubric.

Items carry private text (prompts, code, commands), redacted for secrets but not anonymised:
write them under docs/research-drafts/_data/ (git-ignored), never into a tracked file.

  python tools/maintain/export_blind_sample.py --projects-root <dir> --labels <labels.json> --out <dir>
  python tools/maintain/export_blind_sample.py --merge <judge-labels.json> --out <dir> --labels <labels.json>
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core.audit.transcript import is_human_turn, project_slug  # noqa: E402
from core.evidence.e2e.redact import redact_text  # noqa: E402
from tools.maintain import measure_task_outcomes as outcomes  # noqa: E402
from tools.maintain.measure_real_use import read_transcript  # noqa: E402

CATEGORIES = ("feature", "bug", "performance", "migration", "refactor", "ops", "investigation", "review")
GATE = "[EXECUTION GATE]"
GATE_RULES = (
    ("commit_unapproved", "git commit` without"),
    ("outside_root", "outside the session's project root"),
    ("outside_scope", "outside the active execution's scope"),
)
EXP008_ARMS = {"solved_clean": 12, "solved_fixed": 8}

SCHEMA = {
    "exp014": {"label": ["legitimate", "false_block", "unclear"],
               "note": "legitimate: the gate stopped work the user had not approved; false_block: the user "
                       "wanted that action and had to work around the gate; unclear: the record cannot tell."},
    "category": {"label": list(CATEGORIES),
                 "note": "the session's main kind of work, judged from its prompts; coding = feature, bug, "
                         "performance, migration, refactor."},
    "exp008": {"root_cause_correct": ["yes", "no"], "scope": ["tight", "over_change"],
               "unnecessary_files": ["yes", "no"], "consistent_with_codebase": ["yes", "no"],
               "regression_risk": ["low", "medium", "high"], "tests_adequate": ["yes", "no", "not_applicable"],
               "verdict": ["correct_and_acceptable", "correct_but_poor", "incorrect"],
               "partial_cause": ["rewritten_for_taste", "wrong_approach", "superseded", "other", "not_partial"],
               "note": "EXP-008 rubric; partial_cause only for items marked partial: true."},
}


def clip(text, limit: int) -> str:
    text = str(text or "")
    clean, _ = redact_text(text if len(text) <= limit else text[:limit] + " [...]")
    return clean


def _records(path: str):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and not rec.get("isSidechain"):
                yield rec


def _blocks(content):
    return content if isinstance(content, list) else []


def _text_of(rec) -> str:
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in _blocks(content) if b.get("type") == "text")


def gate_blocks(path: str) -> list[dict]:
    """Each execution-gate block in one transcript, with the turn around it."""
    recs = list(_records(path))
    uses, found = {}, []
    for i, rec in enumerate(recs):
        for b in _blocks((rec.get("message") or {}).get("content")):
            if b.get("type") == "tool_use":
                uses[b.get("id")] = b
            if b.get("type") != "tool_result" or not b.get("is_error"):
                continue
            body = b.get("content")
            body = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            if "hook error" not in body or GATE not in body:
                continue
            use = uses.get(b.get("tool_use_id")) or {}
            before = next((_text_of(r) for r in reversed(recs[:i]) if is_human_turn(r)), "")
            after = []
            for r in recs[i + 1:]:
                text = _text_of(r)
                if is_human_turn(r):
                    after.append({"user": clip(text, 600)})
                elif r.get("type") == "assistant" and text.strip():
                    after.append({"assistant": clip(text, 600)})
                if len(after) >= 4:
                    break
            found.append({
                "rule": next((name for name, mark in GATE_RULES if mark in body), "unknown"),
                "tool": use.get("name"),
                "blocked_input": clip(json.dumps(use.get("input") or {}, ensure_ascii=False), 800),
                "gate_message": clip(body[body.index(GATE):], 500),
                "prompt_before": clip(before, 800),
                "after": after,
                "_at": rec.get("timestamp"),
            })
    return found


def _projects(root: str, exclude) -> list[str]:
    return sorted(p for p in glob.glob(os.path.join(root, "*"))
                  if os.path.isdir(p) and os.path.basename(p) not in exclude)


def _transcripts(project: str, transcripts_dir: str) -> list[str]:
    return sorted(glob.glob(os.path.join(transcripts_dir, project_slug(os.path.abspath(project)), "*.jsonl")))


def export_exp014(args) -> tuple[list, dict, dict]:
    items, key, logged = [], {}, Counter()
    for project in _projects(args.projects_root, args.exclude):
        for f in _transcripts(project, args.transcripts):
            for block in gate_blocks(f):
                items.append(block)
                key_entry = {"project": os.path.basename(project), "transcript": os.path.basename(f),
                             "at": block.pop("_at")}
                block["_key"] = key_entry
        for log in glob.glob(os.path.join(project, ".workflow", "data", "sessions", "*", "runtime", "gate-blocks.jsonl")):
            for line in Path(log).read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    logged[json.loads(line).get("rule")] += 1
                except ValueError:
                    logged["unreadable"] += 1
    return _shuffle(items, "B", key, args.seed), key, dict(logged)


def export_category(args, labels: dict) -> tuple[list, dict]:
    items, key = [], {}
    # measure_task_outcomes leaves the clone out by default; labels for it would go unused.
    for project in _projects(args.projects_root, list(args.exclude) + ["agent-workflow"]):
        for f in _transcripts(project, args.transcripts):
            if os.path.basename(f) in labels:
                continue
            s = read_transcript(f, outcomes.IDLE_SECONDS)
            if s["version"] not in args.category_version or not s["prompts"]:
                continue
            items.append({
                "first_prompt": clip(s["prompts"][0], 1500),
                "follow_ups": [clip(p, 300) for p in s["prompts"][1:6]],
                "follow_up_count": max(0, len(s["prompts"]) - 1),
                "files_edited": len(s["files"]),
                "_key": {"project": os.path.basename(project), "transcript": os.path.basename(f),
                         "version": s["version"]},
            })
    return _shuffle(items, "C", key, args.seed), key


def _outcome_args(args) -> argparse.Namespace:
    return argparse.Namespace(
        projects_root=args.projects_root, labels=args.labels, version=list(args.exp008_version),
        transcripts=args.transcripts, exclude=list(args.exclude) + ["agent-workflow"],
        solved_share=outcomes.SOLVED_SHARE, partial_share=outcomes.PARTIAL_SHARE,
        fix_hours=outcomes.FIX_WINDOW.total_seconds() / 3600, search_days=outcomes.SEARCH_WINDOW.days,
        idle_minutes=outcomes.IDLE_SECONDS / 60,
        author_before_minutes=outcomes.AUTHOR_BEFORE.total_seconds() / 60,
        author_after_minutes=outcomes.AUTHOR_AFTER.total_seconds() / 60,
        link_hours=outcomes.LINK_WINDOW.total_seconds() / 3600,
    )


def sample_exp008(tasks: list[dict], seed: int) -> list[dict]:
    """EXP-008 step 1: 12 clean and 8 fixed solved tasks, the two largest projects holding at
    most half of each arm; every partial task."""
    rng = random.Random(seed)
    size = Counter(t["project"] for t in tasks)
    big = {p for p, _ in size.most_common(2)}
    chosen = []
    for arm, want in EXP008_ARMS.items():
        pool = [t for t in tasks if t["grade"] == arm]
        rng.shuffle(pool)
        picked, from_big = [], 0
        for t in pool:
            if len(picked) == want:
                break
            if t["project"] in big:
                if from_big >= want // 2:
                    continue
                from_big += 1
            picked.append(t)
        for t in pool:  # not enough elsewhere: fill from the large projects after all
            if len(picked) == want:
                break
            if t not in picked:
                picked.append(t)
        chosen += picked
    return chosen + [t for t in tasks if t["grade"] == "partial"]


def export_exp008(args) -> tuple[list, dict]:
    o_args = _outcome_args(args)
    outcomes._apply_rule(o_args)
    tasks, _, _, _ = outcomes.collect_tasks(o_args)
    items, key = [], {}
    for t in sample_exp008(tasks, args.seed):
        root = os.path.abspath(os.path.join(args.projects_root, t["project"])).replace("\\", "/") + "/"
        lines = outcomes.session_lines(t["transcript"], root)
        shown, budget = {}, 400
        for file in sorted(lines):
            kept = sorted(lines[file])[: min(80, budget)]
            if not kept:
                break
            shown[file] = [clip(line, 200) for line in kept]
            budget -= len(kept)
        s = read_transcript(t["transcript"], outcomes.IDLE_SECONDS)
        items.append({
            "partial": t["grade"] == "partial",
            "first_prompt": clip(s["prompts"][0] if s["prompts"] else "", 1500),
            "lines_left_by_session": shown,
            "files_left": len(lines),
            "_key": {"project": t["project"], "transcript": os.path.basename(t["transcript"]),
                     "arm": t["grade"], "category": t["category"], "version": t["version"]},
        })
    return _shuffle(items, "Q", key, args.seed), key


def _shuffle(items: list, prefix: str, key: dict, seed: int) -> list:
    random.Random(f"{seed}:{prefix}").shuffle(items)
    for n, item in enumerate(items, 1):
        item_id = f"{prefix}{n:03d}"
        key[item_id] = item.pop("_key")
        item["id"] = item_id
    return items


def _write_jsonl(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def merge(args) -> int:
    """Turn the judge's category labels into a labels file measure_task_outcomes can read."""
    judged = json.loads(Path(args.merge).read_text(encoding="utf-8"))
    key = json.loads((Path(args.out) / "key" / "category.json").read_text(encoding="utf-8"))
    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    added, refused = 0, []
    for item_id, label in judged.items():
        if item_id not in key or label not in CATEGORIES:
            refused.append(item_id)
            continue
        labels.setdefault(key[item_id]["transcript"], label)
        added += 1
    dest = Path(args.out) / "labels-merged.json"
    dest.write_text(json.dumps(labels, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"merged": added, "refused": refused, "written": str(dest)}))
    return 0 if not refused else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects-root")
    ap.add_argument("--labels", required=True, help="JSON: {transcript file name: category}")
    ap.add_argument("--out", required=True, help="output folder; keep it under docs/research-drafts/_data/")
    ap.add_argument("--transcripts", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--category-version", action="append", default=None,
                    help="versions whose unlabelled transcripts go to the category set (default 3.8.0-3.9.0)")
    ap.add_argument("--exp008-version", action="append", default=None, help="default 3.6.0 and 3.7.3")
    ap.add_argument("--seed", type=int, default=20261023)
    ap.add_argument("--merge", metavar="JUDGE_JSON", help="merge judged category labels instead of exporting")
    args = ap.parse_args()
    if args.merge:
        return merge(args)
    if not args.projects_root:
        ap.error("--projects-root is required to export")
    args.category_version = args.category_version or ["3.8.0", "3.8.1", "3.8.2", "3.8.3", "3.8.4", "3.9.0"]
    args.exp008_version = args.exp008_version or ["3.6.0", "3.7.3"]
    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))

    out = Path(args.out)
    (out / "items").mkdir(parents=True, exist_ok=True)
    (out / "key").mkdir(parents=True, exist_ok=True)
    gate, gate_key, logged = export_exp014(args)
    cat, cat_key = export_category(args, labels)
    q, q_key = export_exp008(args)
    summary = {
        "seed": args.seed,
        "exp014": {"items": len(gate), "by_rule": dict(Counter(i["rule"] for i in gate)), "hook_logged": logged},
        "category": {"items": len(cat), "versions": args.category_version},
        "exp008": {"items": len(q), "partial": sum(1 for i in q if i["partial"]),
                   "arms_in_key": dict(Counter(v["arm"] for v in q_key.values()))},
    }
    for name, rows, key in (("exp014", gate, gate_key), ("category", cat, cat_key), ("exp008", q, q_key)):
        _write_jsonl(out / "items" / f"{name}.jsonl", rows)
        (out / "key" / f"{name}.json").write_text(json.dumps(key, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "label-schema.json").write_text(json.dumps(SCHEMA, indent=1) + "\n", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
