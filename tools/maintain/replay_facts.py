"""Replay fact retrieval over archived delegated calls, one-token rule against weighted (EXP-013).

For every archived exploration/reasoning call (`.workflow/data/sessions/*/logs/<prompt_id>/`)
the tool asks: of the facts in the store that existed before the call, which would each arm
have served, and does the call's answer cite them? Two arms:

  one-token  the 3.8.4 rule: any shared [a-z0-9_]{3,} token between the task and the fact's
             claim + file, ranked by overlap, limit 3
  weighted   the current rule: `fact_store.relevance` (DEC-066), its floor and per-role limit

Both arms see the same candidates: stored facts captured before the call's `recorded_at`
whose every anchor still places today. That makes the replay counterfactual: facts evicted or
pruned since the call are absent from both, and the answer was written with whatever was
served then, not with either arm's set. Calls archived after 3.9.0 carry `served_facts` in
call.meta.json; for those the exact served set is reported as the `recorded` arm.

A fact counts as used when the answer cites its file at its line, or inside a cited range.
Read-only: the fact store, its usage sidecar and the projects are never written.

  python tools/maintain/replay_facts.py --projects-root <dir>
  python tools/maintain/replay_facts.py --projects-root <dir> --export replay.json
  python tools/maintain/replay_facts.py --projects-root <dir> --weighted-limit 3   (EXP-016)
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from exoma.core.audit import metrics  # noqa: E402
from exoma.core.evidence import fact_store  # noqa: E402

ROLES = ("exploration", "reasoning")
ONE_TOKEN_LIMIT = 3  # 3.8.4 RELEVANT_LIMIT
ONE_TOKEN_WORD = re.compile(r"[a-z0-9_]{3,}")
TASK_BLOCK = re.compile(r"\[TASK\]\r?\n(.*?)(?:\r?\n\[[A-Z_ ]+\]|\Z)", re.S)
# path:line or path:start-end; the path may carry a drive letter and either slash.
CITATION = re.compile(r"((?:[A-Za-z]:)?[\w./\\-]+\.[\w]+):(\d+)(?:-(\d+))?")


def _norm(path: str | None) -> str:
    return (path or "").replace("\\", "/").lstrip("./").lower()


def citations(text: str) -> list[tuple[str, int, int]]:
    """Every file:line or file:start-end in `text`, as (normalized path, start, end)."""
    out = []
    for m in CITATION.finditer(text or ""):
        start = int(m.group(2))
        end = int(m.group(3)) if m.group(3) else start
        out.append((_norm(m.group(1)), start, max(start, end)))
    return out


def is_cited(fact: dict, cited: list[tuple[str, int, int]]) -> bool:
    file, line = _norm(fact.get("file")), fact.get("line")
    if not file or not isinstance(line, int):
        return False
    for path, start, end in cited:
        # An answer may cite a path relative to a deeper cwd or with the project prefix.
        if (path == file or path.endswith("/" + file) or file.endswith("/" + path)) and start <= line <= end:
            return True
    return False


def task_text(prompt: str) -> str | None:
    m = TASK_BLOCK.search(prompt or "")
    return m.group(1).strip() if m and m.group(1).strip() else None


def one_token(task: str, facts: list[dict]) -> list[dict]:
    """The 3.8.4 `load_relevant` ranking, minus its freshness check (done once, upstream)."""
    words = set(ONE_TOKEN_WORD.findall((task or "").lower()))
    scored = []
    for f in facts:
        blob = f"{f.get('claim', '')} {f.get('file') or ''}".lower()
        overlap = len(words & set(ONE_TOKEN_WORD.findall(blob)))
        if overlap >= 1:
            scored.append((overlap, f))
    scored.sort(key=lambda x: -x[0])  # stable, as in 3.8.4
    return [f for _, f in scored[:ONE_TOKEN_LIMIT]]


def weighted(project_root: Path, task: str, role: str, facts: list[dict], limit: int | None = None) -> list[dict]:
    """`fact_store.load_relevant`'s ranking over a given candidate list, without its side effects.

    `limit` overrides the project's per-role limit for every role (EXP-016); None keeps it.
    """
    floor = fact_store._policy_float(project_root, "fact_min_relevance", fact_store.MIN_RELEVANCE_SCORE)
    if limit is None:
        limit = fact_store._policy_limit(project_root, role)
    scored = []
    for f in facts:
        score, id_hits = fact_store.relevance(task, f.get("claim", ""), f.get("file"))
        if score < floor:
            continue
        rank = (-score, -id_hits, fact_store._newest_first(f.get("captured_at")), f.get("claim", ""))
        scored.append((rank, f))
    scored.sort(key=lambda pair: pair[0])
    return [f for _, f in scored[:limit]]


def placeable_facts(project_root: Path) -> list[dict]:
    """Stored facts, read-time deduplicated, each placed where its anchors sit today."""
    facts = fact_store._dedupe(fact_store._load_facts(project_root), allow_line_proximity=True)
    cache: dict = {}
    placed = []
    for f in facts:
        p = fact_store._anchors_placeable(project_root, f, cache)
        if p is not None:
            placed.append(p)
    return placed


def archived_calls(project_root: Path):
    """(call folder, meta) for every archived call of this project, oldest first."""
    pattern = os.path.join(str(project_root), ".workflow", "data", "sessions", "*", "logs", "*", "call.meta.json")
    for meta_path in sorted(glob.glob(pattern)):
        try:
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            yield Path(meta_path).parent, None
            continue
        yield Path(meta_path).parent, meta if isinstance(meta, dict) else None


def _rate(num: int, den: int):
    return round(num / den, 3) if den else None


class Arm:
    def __init__(self) -> None:
        self.calls = self.candidates = self.served = self.used = 0
        self.calls_served = 0

    def add(self, candidates: int, served: list[dict], cited) -> None:
        self.calls += 1
        self.candidates += candidates
        self.served += len(served)
        self.calls_served += 1 if served else 0
        self.used += sum(1 for f in served if is_cited(f, cited))

    def report(self) -> dict:
        return {
            "calls": self.calls,
            "calls_served_any": self.calls_served,
            "candidates": self.candidates,
            "served": self.served,
            "used": self.used,
            "injection_rate": _rate(self.served, self.candidates),
            "served_fact_use_rate": _rate(self.used, self.served),
        }


def replay_project(project_root: Path, arms: dict, skipped: Counter, inputs: list,
                   weighted_limit: int | None = None) -> int:
    store = fact_store._facts_path(project_root)
    if not store.is_file():
        skipped["no_fact_store"] += 1
        return 0
    inputs.append(store)
    facts = placeable_facts(project_root)
    replayed = 0
    for folder, meta in archived_calls(project_root):
        if meta is None:
            skipped["unreadable_meta"] += 1
            continue
        role = meta.get("role")
        if role not in ROLES:
            continue
        try:
            prompt = (folder / "prompt.md").read_text(encoding="utf-8")
            answer = (folder / "output.raw.md").read_text(encoding="utf-8")
        except OSError:
            skipped["missing_prompt_or_output"] += 1
            continue
        task = task_text(prompt)
        if not task or not answer.strip():
            skipped["no_task_or_empty_output"] += 1
            continue
        when = str(meta.get("recorded_at") or "")
        if not when:
            skipped["no_recorded_at"] += 1
            continue
        before = [f for f in facts if str(f.get("captured_at") or "") < when]
        cited = citations(answer)
        arms["one_token"].add(len(before), one_token(task, before), cited)
        arms["weighted"].add(len(before), weighted(project_root, task, role, before, weighted_limit), cited)
        recorded = meta.get("served_facts")
        if isinstance(recorded, list):
            arms["recorded"].add(len(before), recorded, cited)
        inputs.extend([folder / "call.meta.json", folder / "prompt.md", folder / "output.raw.md"])
        replayed += 1
    return replayed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects-root", required=True, help="directory holding the projects")
    ap.add_argument("--exclude", action="append", default=[], help="project directory to leave out")
    ap.add_argument("--export", metavar="FILE", help="also write the figures to FILE for sharing")
    ap.add_argument("--weighted-limit", type=int, metavar="N",
                    help="cap the weighted arm at N facts for every role (default: each project's per-role limit)")
    args = ap.parse_args()

    projects = sorted(
        p for p in glob.glob(os.path.join(args.projects_root, "*"))
        if os.path.isdir(os.path.join(p, ".workflow")) and os.path.basename(p) not in args.exclude
    )
    arms = {"one_token": Arm(), "weighted": Arm(), "recorded": Arm()}
    skipped: Counter = Counter()
    inputs: list = []
    per_project = {}
    names = []
    for path in projects:
        count = replay_project(Path(path), arms, skipped, inputs, args.weighted_limit)
        if count:
            per_project[f"P{metrics.project_letters(len(per_project))}"] = count
            names.append(os.path.basename(path))
    report = {
        "provenance": metrics.provenance(
            "tools/maintain/replay_facts.py",
            {"one_token_limit": ONE_TOKEN_LIMIT, "weighted_limit": args.weighted_limit, "excluded": len(args.exclude)},
            inputs,
            metric_ids=[m for m in metrics.REGISTRY if m.startswith("replay.")],
        ),
        "counterfactual": True,
        "projects": len(per_project),
        "calls": sum(per_project.values()),
        "per_project": per_project,
        "skipped": dict(skipped),
        "arms": {name: arm.report() for name, arm in arms.items()},
    }
    print(json.dumps(report, indent=2))
    if args.export:
        metrics.write_export(report, Path(args.export), forbidden=names)
    return 0 if report["calls"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
