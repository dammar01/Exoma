#!/usr/bin/env python3
"""Grade real coding tasks by what reached branch main (RQ-02, RQ-04; CASE-012).

A Senior-SWE-bench-style reading of real sessions, without replaying them. A task is one
Claude Code session whose purpose was a code change; the maintainer labels each session's
category once (feature, bug, performance, migration, refactor, or a non-coding kind) in a
local labels file, because a first prompt does not say reliably what the session became.

Per task, from the transcript, the project's Git history, and the runtime's usage rows:

  outcome      the substantive lines (8+ characters) the session's successful edits left
               behind -- lines it added and removed again itself are not its result -- are
               looked up on main's first-parent states from the session's start. The first
               state holding 80% or more is where the work entered main: `solved`. A lower
               best share is `partial` (30-80%) or `not_in_main`; no edit is `no_change`.
  fix within 24 h
               a solved task whose lines change on main within 24 hours of entering it, by a
               commit from an author of the commits that brought them in, counts as fixed
               after the fact: `solved` splits into clean and fixed. (The maintainer's rule.)
  segments     the session's work split by the correlation id of the verifies that judged it
               (tasks.jsonl joined to usage.jsonl by prompt id), so a plan verified phase by
               phase is one segment per id; a later session that edits a file this one left
               lines in, within --link-hours, continues it. A fix within 24 h that such a
               continuation explains is not counted in `solved_fixed_outside_feature`; what a
               session spent before its first edit is its ramp-up (`continuation_cost`)
  prompts      follow-up prompts classified as plan, execute, fix, question, verify,
               explore_analyze, change_request, or other, from the slash command or the
               intent map shipped in src/exoma/_bundle/config/claude/hooks/intent-map.json
  delegation   delegated calls: distinct prompt ids of the session's usage rows that a provider
               answered (a skill runs `.workflow/run` in a subagent, so the transcript's main
               thread does not show it); verify-browser's spec and review stages are each a call
  context      main-agent context of the last and the largest assistant message (input plus
               cache read and creation), per message id
  saved        tokens kept from entering the main agent: the second agent's fresh input less
               the answers that came back (see saved_net); the status line's figure, which
               adds reasoning, is reported beside it. The digest
               figure (`premium_context_avoided_tokens`, answer minus digest) is kept beside
               it, with the rows the runtime left empty on verify-browser's path recovered
               from archived replies (see saved_tokens)

Lines are matched by content, so a file moved to another directory is followed by its name.
Read-only. Prints one JSON object of figures; project names become letters unless
--show-names. Not part of the test suite. Any user can run it over their own projects with
their own labels file; `--export <file>` writes the figures as a file that may be shared,
refused if it would carry a path or a project name (never with --show-names).

  python tools/maintain/measure_task_outcomes.py --projects-root <dir> --labels <labels.json> --version 3.6.0 --version 3.7.3
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from exoma.core.audit import metrics, task_telemetry  # noqa: E402
from exoma.core.audit.telemetry import load_quality  # noqa: E402
from exoma.core.audit.transcript import project_slug  # noqa: E402
from tools.maintain.measure_real_use import CORRECTION, load_usage, read_transcript  # noqa: E402
from exoma.utils import osutil  # noqa: E402

CODING = ("feature", "bug", "performance", "migration", "refactor")
# Defaults of the outcome rule (metric outcome.solved / outcome.solved_fixed); each can be set
# on the command line, and the values used are stamped into the output's provenance.
SOLVED_SHARE, PARTIAL_SHARE, MIN_LINE = 0.8, 0.3, 8
FIX_WINDOW = timedelta(hours=24)
SEARCH_WINDOW = timedelta(days=30)
AUTHOR_BEFORE, AUTHOR_AFTER = timedelta(minutes=5), timedelta(minutes=1)
IDLE_SECONDS = 900
INTENT_MAP = Path(__file__).resolve().parents[2] / "src" / "exoma" / "_bundle" / "config" / "claude" / "hooks" / "intent-map.json"
TESTFILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|\.(test|spec)\.[jt]sx?$|Test\.php$", re.I)


GIT_TIMEOUT = 60


def git(project: str, *args: str) -> str:
    """Git's output, or "" when it fails or runs past GIT_TIMEOUT seconds (read as no history)."""
    try:
        r = subprocess.run(["git", "-C", project, *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT,
                           **osutil.hidden_run_kwargs())
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


# --- prompt kinds ------------------------------------------------------------------------

def _any(words):
    return re.compile("|".join(words), re.I)


_IM = json.loads(INTENT_MAP.read_text(encoding="utf-8"))["patterns"]
VERIFY = _any(_IM["verify"] + _IM["verify-browser"] + [r"\btes(t|ting)?\b", r"coba (cek|jalankan|run)",
                                                       r"sudah (jalan|benar|bener|aman)", r"lolos", r"\bverif"])
PLAN = _any(_IM["plan"] + [r"buat(kan)? rencana", r"\bplan\b", r"langkah", r"strategi"])
EXPLORE = _any(_IM["explore"] + _IM["analyze"] + [r"telusuri", r"pahami", r"jelaskan"])
EXECUTE = re.compile(r"^\s*(ya|y|ok|oke|okay|baik|gas|lanjut|lanjutkan|kerjakan|implement\w*|eksekusi|terapkan"
                     r"|jalankan|silahkan|silakan|continue|go|yes|setuju|boleh|sip|mantap)\b|\b(dilanjutkan|lanjutkan)\b", re.I)
QUESTION = re.compile(r"\?|^\s*(apa|apakah|bagaimana|bagiamana|gimana|kenapa|mengapa|berapa|di ?mana|siapa|kapan"
                      r"|bisa(kah)?|seharusnya|cara)\b|\b(apakah|bagaimana|bagiamana|gimana|mengapa|apa (yang|saja|itu)"
                      r"|jadi (masalah|maslaah|kesimpulan)\w*|atau rata-rata)\b", re.I)
FIX = re.compile(r"\b(perbaiki|benerin|fix|masih|error|gagal|bug|tidak (muncul|tampil|berubah|jalan|ada)"
                 r"|gak (muncul|jalan)|maksud saya|saya menanyakan|yang saya maksud|bukan itu|exited|failure"
                 r"|failed|exception|sqlstate)\b", re.I)
CHANGE = re.compile(r"\b(tambah\w*|ubah|ganti|hapus|buat\w*|hide|sembunyikan|pindah\w*|sesuaikan|rapikan"
                    r"|optimasi\w*|update|perbarui)\b", re.I)
KINDS = ("plan", "execute", "fix", "question", "verify", "explore_analyze", "change_request", "other")


def prompt_kind(prompt: str) -> str:
    """One follow-up prompt's kind. A heuristic: 85% agreed with a hand label on 80 samples."""
    text = prompt.strip()
    if text.startswith("/."):
        cmd = text[2:].split()[0] if len(text) > 2 else ""
        for prefix, kind in (("plan", "plan"), ("execute", "execute"), ("verify", "verify")):
            if cmd.startswith(prefix):
                return kind
        return "explore_analyze" if cmd in ("explore", "analyze") else "other"
    head = text[:600]
    if CORRECTION.search(head) or FIX.search(head):
        return "fix"
    if VERIFY.search(head) and not re.match(r"\s*(validasi \w+ (\w+ )?(juga )?agar|\W*revisi)", head, re.I):
        return "verify"
    for rx, kind in ((PLAN, "plan"), (EXECUTE, "execute"), (EXPLORE, "explore_analyze"),
                     (QUESTION, "question"), (CHANGE, "change_request")):
        if rx.search(head):
            return kind
    return "other"


# --- lines a session left behind ---------------------------------------------------------

def session_lines(path: str, root: str) -> dict[str, set[str]]:
    """Project-relative file (real case) -> substantive lines the session added and kept."""
    added, churned, pending = defaultdict(set), defaultdict(set), set()
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        if rec.get("isSidechain") or not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") in ("Edit", "Write", "MultiEdit"):
                pending.add(b.get("id"))
            if b.get("type") != "tool_result" or b.get("tool_use_id") not in pending or b.get("is_error"):
                continue
            result = rec.get("toolUseResult")
            if not isinstance(result, dict) or not result.get("filePath"):
                continue
            full = os.path.abspath(result["filePath"]).replace("\\", "/")
            if not full.lower().startswith(root.lower()):
                continue
            rel = full[len(root):]
            hunks = result.get("structuredPatch")
            if isinstance(hunks, list) and hunks:
                lines = [l for h in hunks if isinstance(h, dict) for l in h.get("lines", [])]
                new = [l[1:] for l in lines if l.startswith("+")]
                churned[rel] |= {l[1:].strip() for l in lines if l.startswith("-")} & added[rel]
            elif result.get("type") == "create" and isinstance(result.get("content"), str):
                new = result["content"].splitlines()
            else:
                continue
            added[rel].update(x.strip() for x in new if len(x.strip()) >= MIN_LINE)
    return {k: v - churned[k] for k, v in added.items() if v - churned[k]}


class Repo:
    """Cached file contents of one project at Git revisions."""

    def __init__(self, path: str):
        self.path, self._lines, self._names = path, {}, {}

    def lines(self, rel: str, rev: str) -> set[str]:
        key = (rel, rev)
        if key not in self._lines:
            text = git(self.path, "show", f"{rev}:{rel}")
            if not text:  # moved: follow the file by its name
                for moved in self.names(rev).get(rel.rsplit("/", 1)[-1].lower(), [])[:5]:
                    text += git(self.path, "show", f"{rev}:{moved}")
            self._lines[key] = {x.strip() for x in text.splitlines()}
        return self._lines[key]

    def names(self, rev: str) -> dict[str, list[str]]:
        if rev not in self._names:
            idx = defaultdict(list)
            for p in git(self.path, "ls-tree", "-r", "--name-only", rev).splitlines():
                idx[p.rsplit("/", 1)[-1].lower()].append(p)
            self._names[rev] = idx
        return self._names[rev]

    def share(self, per_file, rev) -> float:
        total = sum(len(v) for v in per_file.values())
        return sum(1 for k, v in per_file.items() for x in v if x in self.lines(k, rev)) / total if total else 0.0

    def touching(self, ref, since, until, files, authors=None) -> list[dict]:
        out = git(self.path, "log", ref, "--no-merges", "--format=@@%H\t%ae", "--name-only",
                  f"--since={since.isoformat()}", f"--until={until.isoformat()}")
        commits, cur = [], None
        for line in out.splitlines():
            if line.startswith("@@"):
                h, author = line[2:].split("\t", 1)
                cur = {"hash": h, "author": author.lower(), "files": set()}
                commits.append(cur)
            elif line.strip() and cur is not None:
                cur["files"].add(line.strip().replace("\\", "/").lower())
        return [c for c in commits if (authors is None or c["author"] in authors) and c["files"] & files]


def outcome(repo: Repo, per_file, start, end) -> dict:
    out = {"main_share": 0.0, "fixed_24h": False}
    if not per_file or not git(repo.path, "rev-parse", "--verify", "-q", "main").strip():
        return out
    best = None
    for row in git(repo.path, "log", "main", "--first-parent", "--reverse", "--format=%H\t%cI",
                   f"--since={start.isoformat()}", f"--until={(end + SEARCH_WINDOW).isoformat()}").splitlines():
        rev, ts = row.split("\t")
        share = repo.share(per_file, rev)
        if best is None or share > best[0] + 1e-9 or share >= SOLVED_SHARE:
            best = (share, rev, datetime.fromisoformat(ts))
        if share >= SOLVED_SHARE:
            break
    if not best or not best[0]:
        return out
    share, rev0, t0 = best
    out["main_share"] = round(share, 2)
    out["landed_at"] = t0
    files = {k.lower() for k in per_file}
    authors = {c["author"] for c in repo.touching(rev0, start - AUTHOR_BEFORE, t0 + AUTHOR_AFTER, files)}
    rev1 = git(repo.path, "rev-list", "-1", "--first-parent", f"--before={(t0 + FIX_WINDOW).isoformat()}", "main").strip()
    if rev1 and rev1 != rev0:
        lost = {k.lower() for k, v in per_file.items()
                if any(x in repo.lines(k, rev0) and x not in repo.lines(k, rev1) for x in v)}
        out["fixed_24h"] = bool(lost and repo.touching("main", t0 + timedelta(seconds=1), t0 + FIX_WINDOW, lost, authors))
        if out["fixed_24h"]:
            out["lost_files"] = lost
    return out


# --- feature segments: phases in a session, continuations across sessions ----------------
#
# A session is the unit above, so a feature built over several sessions, or a plan verified
# phase by phase, reads as separate tasks and its own next phase as a fix (CASE-012). The
# runtime's correlation id ties a plan to the verifies that follow it inside one session and,
# by design, never across sessions (core/evidence/contracts.py): a session that continues a
# feature starts clean and rebuilds its context, which is a cost worth seeing on its own.
# So a feature is read as segments -- one correlation id within one session -- joined across
# sessions by a heuristic link that says why it was made.

LINK_WINDOW = timedelta(hours=72)
INFER_SECONDS = 120


def _ts(value) -> datetime | None:
    """An ISO timestamp as an aware datetime (naive is read as UTC), or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def verify_correlations(usage: list[dict], quality: list[dict]) -> tuple[dict[str, str], set[str]]:
    """prompt_id -> correlation_id from the usage rows, and the ids known only through a
    verify-browser run: its quality row lists every stage's prompt id, and a stage without a
    correlation of its own takes the one another stage of the same run carries."""
    out: dict[str, str] = {}
    for r in usage:
        if r.get("prompt_id") and r.get("correlation_id"):
            out.setdefault(r["prompt_id"], r["correlation_id"])
    browser: set[str] = set()
    for q in quality:
        ids = [i for i in (q.get("provider_prompt_ids") or []) if isinstance(i, str)]
        corr = next((out[i] for i in ids if i in out), None)
        if corr:
            for i in ids:
                if i not in out:
                    out[i] = corr
                    browser.add(i)
    return out, browser


def segments(events: list[dict], usage: list[dict], correlations: dict[str, str], browser: set[str]) -> list[dict]:
    """One session's work split by the correlation id of the verify that judged it.

    Edits join the next verify of their session; edits after the last verify join the last
    segment, or an `unverified` one when the session never verified. A verify finds its id by
    its prompt id (`direct`, or `browser` through a verify-browser run's stage ids); without
    one, by the nearest verify usage row of the same session within INFER_SECONDS
    (`inferred`); else it is `unlinked` and makes a segment of its own.
    """
    by_session: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e.get("session_id") and e.get("kind") in ("edit", "skill"):
            by_session[e["session_id"]].append(e)
    verify_rows: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for r in usage:
        at = _ts(r.get("recorded_at"))
        if r.get("command") == "verify" and r.get("correlation_id") and r.get("session_id") and at:
            verify_rows[r["session_id"]].append((at, r["correlation_id"]))

    out: list[dict] = []
    for session, evs in by_session.items():
        evs.sort(key=lambda e: _ts(e.get("at")) or datetime.min.replace(tzinfo=timezone.utc))
        segs: dict[str, dict] = {}
        pending: list[tuple[datetime | None, str]] = []
        unlinked = 0

        def seg(key, corr, link):
            if key not in segs:
                segs[key] = {"session_id": session, "correlation_id": corr, "link": link,
                             "files": set(), "first_edit": None, "verifies": 0, "passes": 0}
            return segs[key]

        def take(target):
            for at, path in pending:
                target["files"].add(path)
                if at and (target["first_edit"] is None or at < target["first_edit"]):
                    target["first_edit"] = at
            pending.clear()

        for e in evs:
            at = _ts(e.get("at"))
            if e["kind"] == "edit":
                pending.append((at, str(e.get("path") or "").replace("\\", "/").lower()))
                continue
            if e.get("skill") != "verify":
                continue
            pid = e.get("prompt_id")
            corr, link = (correlations.get(pid), "browser" if pid in browser else "direct") if pid else (None, None)
            if corr is None and at:
                near = [(abs((t - at).total_seconds()), c) for t, c in verify_rows.get(session, [])]
                near = sorted(n for n in near if n[0] <= INFER_SECONDS)
                if near and (len(near) == 1 or near[0][0] < near[1][0]):
                    corr, link = near[0][1], "inferred"
            if corr is None:
                unlinked += 1
                target = seg(f"unlinked-{unlinked}", None, "unlinked")
            else:
                target = seg(corr, corr, link)
            take(target)
            target["verifies"] += 1
            if str(e.get("derived") or "").strip().lower() == "pass":
                target["passes"] += 1
        if pending:
            take(list(segs.values())[-1] if segs else seg("unverified", None, "unverified"))
        out.extend(segs.values())
    return out


def link_continuations(tasks: list[dict]) -> None:
    """Mark each task that continues an earlier task of the same project.

    A candidate is an earlier task (by start) whose session ended at most LINK_WINDOW before
    this one started and whose kept lines lie in a file this task also edits. One candidate:
    `continues` names it; several: `ambiguous`, linked to none. Transcripts carry no author,
    so unlike outcome.solved_fixed the link does not check one.
    """
    for t in tasks:
        t.setdefault("continues", None)
        t.setdefault("continuation", "none")
    for i, t in enumerate(tasks):
        cands = [j for j, e in enumerate(tasks)
                 if j != i and e["project"] == t["project"] and e["start"] < t["start"]
                 and timedelta(0) <= t["start"] - e["end"] <= LINK_WINDOW
                 and e["files"] & t["files"]]
        if len(cands) == 1:
            t["continues"], t["continuation"] = cands[0], "linked"
        elif cands:
            t["continuation"] = "ambiguous"


def fixed_outside_feature(tasks: list[dict]) -> None:
    """outcome.solved_fixed_outside_feature: a fix within the window that the feature's own
    next session does not explain. A solved_fixed task is explained when a task linked as its
    continuation started before the window closed and edited every file whose lines were lost."""
    for i, t in enumerate(tasks):
        t["fixed_outside_feature"] = t["grade"] == "solved_fixed"
        if not t["fixed_outside_feature"] or t.get("landed_at") is None:
            continue
        for c in tasks:
            if c.get("continues") == i and c["start"] <= t["landed_at"] + FIX_WINDOW \
                    and t.get("lost_files", set()) <= c["files"]:
                t["fixed_outside_feature"] = False
                break


def ramp_up(rows: list[dict], first_edit: datetime | None) -> dict | None:
    """outcome.continuation_cost: delegated calls and provider input tokens recorded before
    the session's first edit -- what it spent finding its way in. None without a recorded edit."""
    if first_edit is None:
        return None
    before = [r for r in rows if r.get("provider") and (_ts(r.get("recorded_at")) or first_edit) < first_edit]
    tokens = sum(int(r.get("actual_input_tokens") if r.get("actual_input_tokens") is not None
                     else r.get("estimated_input_tokens") or 0) for r in before)
    return {"calls": len({r.get("prompt_id") or r.get("correlation_id") for r in before}), "tokens": tokens}


def context(path: str) -> dict | None:
    per, order = {}, []
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        msg = rec.get("message") or {}
        usage, mid = msg.get("usage") or {}, msg.get("id")
        if not mid or not usage:
            continue
        if mid not in per:
            order.append(mid)
        per[mid] = sum(int(usage.get(k) or 0) for k in
                       ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    if not order:
        return None
    return {"final": per[order[-1]], "peak": max(per.values())}


def saved_net(rows: list[dict]) -> dict:
    """Tokens kept from entering the main agent: what the second agent read, less what came back.

    `fresh` is the second agent's fresh input (input minus cache read) on every row the
    provider counted -- the material it read so the main agent did not have to. `returned`
    is what the second agent answered (`response_chars` // 4 summed over every row): a
    continuation's answer is merged from its rows, and the closing row alone holds only the
    last fragment, so summing errs toward more returned and a lower net. The net is fresh
    minus returned. Reasoning is output, not input to
    the main agent, so it is reported apart and not counted. `badge` is the status line's
    figure (fresh plus reasoning, falling back to `premium_context_avoided_tokens` on rows
    the provider did not count), for comparison. A call that failed before any provider
    counted it is skipped. Rows without provider counts add nothing to `fresh`, so `partial`
    marks a task where part of the reading went unmeasured. The second agent's own fixed
    overhead per call (its system prompt and instructions) is inside `fresh` and is not
    separated by the runtime, so the net is a slight overstatement.
    """
    out = {"fresh": 0, "reasoning": 0, "returned": 0, "badge": 0, "partial": False}
    for r in rows:
        if r.get("ok") is False and r.get("actual_input_tokens") is None and r.get("actual_output_tokens") is None:
            continue
        if r.get("actual_input_tokens") is not None:
            fresh = max(0, int(r["actual_input_tokens"]) - int(r.get("actual_cached_input_tokens") or 0))
            reasoning = int(r.get("actual_reasoning_tokens") or 0)
            out["fresh"] += fresh
            out["reasoning"] += reasoning
            out["badge"] += fresh + reasoning
        else:
            out["badge"] += int(r.get("premium_context_avoided_tokens") or 0)
            out["partial"] = True
        out["returned"] += int(r.get("response_chars") or 0) // 4
    out["net"] = max(0, out["fresh"] - out["returned"])
    return out


def saved_tokens(project: str, rows: list[dict]) -> tuple[int, int]:
    """Context saved over a session's usage rows: (as recorded, with the runner's rows recovered).

    The runtime records `premium_context_avoided_tokens` only where the digest was parsed,
    which is the main delegated path. verify-browser's stages (the spec draft and the
    review, `core/evidence/e2e/runner.py`) call `_run_delegated` directly and never parse
    the digest their replies carry, so their rows hold None since commit cb98768
    (2026-09-15). For those rows the saving is recomputed with the runtime's own rule,
    `max(0, response_chars - digest_chars) // 4`, from the reply archived under the session's
    logs. A continuation's first row stays empty, as the runtime intends.
    """
    from exoma.core.evidence.contract import extract_digest
    from exoma.core.evidence.contracts import _digest_chars

    recorded = recovered = 0
    for r in rows:
        value = r.get("premium_context_avoided_tokens")
        if isinstance(value, int):
            recorded += value
            continue
        if not r.get("provider") or not r.get("ok") or r.get("provider_call_index") == 0 or not r.get("prompt_id"):
            continue
        for raw in glob.glob(os.path.join(project, ".workflow", "data", "sessions", "*", "logs",
                                          r["prompt_id"], "output.raw.md"))[:1]:
            text = Path(raw).read_text(encoding="utf-8", errors="replace")
            digest = extract_digest(text)
            if digest is not None:
                recovered += max(0, (r.get("response_chars") or len(text)) - (_digest_chars(digest) or 0)) // 4
    return recorded, recorded + recovered


def grade(share: float, changed: bool, fixed: bool) -> str:
    if not changed:
        return "no_change"
    if share >= SOLVED_SHARE:
        return "solved_fixed" if fixed else "solved_clean"
    return "partial" if share >= PARTIAL_SHARE else "not_in_main"


# --- report ------------------------------------------------------------------------------

def med(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


def p90(values):
    values = sorted(v for v in values if v is not None)
    return values[int(0.9 * (len(values) - 1))] if values else None


def summary(tasks: list[dict]) -> dict:
    n = len(tasks)
    g = Counter(t["grade"] for t in tasks)
    solved = g["solved_clean"] + g["solved_fixed"]
    kinds = Counter()
    for t in tasks:
        kinds.update(t["prompt_kinds"])
    ctx = [t["context"] for t in tasks if t["context"]]
    return {
        "tasks": n, "solved": solved, "solved_rate": round(solved / n, 3) if n else None,
        "solved_clean": g["solved_clean"], "solved_clean_rate": round(g["solved_clean"] / n, 3) if n else None,
        "solved_fixed_24h": g["solved_fixed"], "partial": g["partial"], "not_in_main": g["not_in_main"],
        "no_change": g["no_change"],
        "prompts_median": med([t["prompts"] for t in tasks]),
        "active_minutes_median": med([t["active_minutes"] for t in tasks]),
        "delegated_calls_median": med([t["delegated_calls"] for t in tasks]),
        "tasks_delegating": sum(1 for t in tasks if t["delegated_calls"]),
        "follow_up_kinds_per_task": {k: round(kinds[k] / n, 1) for k in KINDS} if n else {},
        "context_final_median": med([c["final"] for c in ctx]), "context_final_p90": p90([c["final"] for c in ctx]),
        "context_peak_max": max((c["peak"] for c in ctx), default=None),
        "context_saved_median": med([t["context_saved"] for t in tasks]),
        "context_saved_p90": p90([t["context_saved"] for t in tasks]),
        "context_saved_total": sum(t["context_saved"] for t in tasks),
        "context_saved_partly_estimated_tasks": sum(1 for t in tasks if t["context_saved_estimated"]),
        "context_saved_to_final_context_median": med([t["context_saved"] / t["context"]["final"]
                                                      for t in tasks if t["context"] and t["context"]["final"]]),
        "second_agent_fresh_median": med([t["second_agent_fresh"] for t in tasks]),
        "second_agent_reasoning_median": med([t["second_agent_reasoning"] for t in tasks]),
        "returned_to_main_median": med([t["returned_to_main"] for t in tasks]),
        "badge_saved_median": med([t["badge_saved"] for t in tasks]),
        "badge_saved_total": sum(t["badge_saved"] for t in tasks),
        "digest_saved_median": med([t["digest_saved"] for t in tasks]),
        "digest_saved_total": sum(t["digest_saved"] for t in tasks),
        "digest_saved_recorded_total": sum(t["digest_saved_recorded"] for t in tasks),
        "solved_touching_tests": sum(1 for t in tasks if t["grade"].startswith("solved") and t["tests_edited"]),
        "solved_fixed_outside_feature": sum(1 for t in tasks if t["fixed_outside_feature"]),
        "continuations": sum(1 for t in tasks if t["continuation"] == "linked"),
        "continuations_ambiguous": sum(1 for t in tasks if t["continuation"] == "ambiguous"),
        "segments_per_task_median": med([t["segments"] for t in tasks]),
        "segment_links": dict(sorted(sum((t["segment_links"] for t in tasks), Counter()).items())),
        "ramp_up_calls_median_continuation": med([t["ramp_up"]["calls"] for t in tasks
                                                  if t["ramp_up"] and t["continuation"] == "linked"]),
        "ramp_up_tokens_median_continuation": med([t["ramp_up"]["tokens"] for t in tasks
                                                   if t["ramp_up"] and t["continuation"] == "linked"]),
        "ramp_up_calls_median_first": med([t["ramp_up"]["calls"] for t in tasks
                                           if t["ramp_up"] and t["continuation"] == "none"]),
        "ramp_up_tokens_median_first": med([t["ramp_up"]["tokens"] for t in tasks
                                            if t["ramp_up"] and t["continuation"] == "none"]),
    }


def _apply_rule(args) -> None:
    """The outcome rule's parameters, from the command line, for this run."""
    global SOLVED_SHARE, PARTIAL_SHARE, FIX_WINDOW, SEARCH_WINDOW, IDLE_SECONDS, AUTHOR_BEFORE, AUTHOR_AFTER, LINK_WINDOW
    SOLVED_SHARE, PARTIAL_SHARE = args.solved_share, args.partial_share
    LINK_WINDOW = timedelta(hours=getattr(args, "link_hours", LINK_WINDOW.total_seconds() / 3600))
    FIX_WINDOW = timedelta(hours=args.fix_hours)
    SEARCH_WINDOW = timedelta(days=args.search_days)
    IDLE_SECONDS = args.idle_minutes * 60
    AUTHOR_BEFORE = timedelta(minutes=args.author_before_minutes)
    AUTHOR_AFTER = timedelta(minutes=args.author_after_minutes)


def collect_tasks(args) -> tuple[list[dict], int, list, list]:
    """The coding tasks of the labelled transcripts, linked; with the inputs and main heads read.

    Each task keeps its transcript path (`transcript`) for tools that sample tasks; summary()
    never reads it, so no report carries it.
    """
    labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))
    inputs = [Path(args.labels), INTENT_MAP]
    heads = []

    tasks, unlabelled = [], 0
    for path in sorted(p for p in glob.glob(os.path.join(args.projects_root, "*")) if os.path.isdir(p)):
        name = os.path.basename(path)
        if name in args.exclude:
            continue
        tdir = os.path.join(args.transcripts, project_slug(os.path.abspath(path)))
        usage_path = os.path.join(path, ".workflow", "data", "usage.jsonl")
        usage = load_usage(usage_path)
        inputs.append(Path(usage_path))
        data = Path(path) / ".workflow" / "data"
        inputs += [data / "tasks.jsonl", data / "quality.jsonl"]
        correlations, browser = verify_correlations(usage, load_quality(path))
        segs = segments(task_telemetry.load_events(path), usage, correlations, browser)
        heads.append(git(path, "rev-parse", "--verify", "-q", "main").strip())
        root = os.path.abspath(path).replace("\\", "/") + "/"
        repo = Repo(path)
        found = []
        for f in sorted(glob.glob(os.path.join(tdir, "*.jsonl"))):
            s = read_transcript(f, IDLE_SECONDS)
            if s["version"] in args.version and s["prompts"]:
                found.append((f, s))
        # A usage row belongs to one transcript only, the earliest that binds its session id,
        # whatever that transcript's label: a resumed copy must not count the row again.
        owners = metrics.session_owners([s for _, s in found])
        for i, (f, s) in enumerate(found):
            category = labels.get(os.path.basename(f))
            if category is None:
                unlabelled += 1
                continue
            if category not in CODING:
                continue
            inputs.append(Path(f))
            start = datetime.fromisoformat(s["start"])
            end = start + timedelta(minutes=s["wall_minutes"])
            per_file = session_lines(f, root)
            out = outcome(repo, per_file, start, end)
            rows = [r for r in usage if owners.get(r.get("session_id")) == i]
            saved_recorded, saved_digest = saved_tokens(path, rows)
            net = saved_net(rows)
            own = [g for g in segs if owners.get(g["session_id"]) == i]
            edits = [g["first_edit"] for g in own if g["first_edit"]]
            tasks.append({
                "start": _ts(start), "end": _ts(end), "files": {k.lower() for k in per_file},
                "landed_at": _ts(out.get("landed_at")), "lost_files": out.get("lost_files", set()),
                "segments": len(own), "segment_links": Counter(g["link"] for g in own),
                "ramp_up": ramp_up(rows, min(edits) if edits else None),
                "transcript": f,
                "project": name, "version": s["version"], "category": category,
                "grade": grade(out["main_share"], bool(per_file), out["fixed_24h"]),
                "prompts": len(s["prompts"]), "active_minutes": s["active_minutes"],
                "prompt_kinds": Counter(prompt_kind(p) for p in s["prompts"][1:]),
                # A call is one prompt a provider answered: rows without a provider (a
                # verify-browser run's closing row, a quick verify) are not second-agent work.
                # verify-browser's spec and review stages are each a call.
                "delegated_calls": len({r.get("prompt_id") or r.get("correlation_id")
                                        for r in rows if r.get("provider")}),
                "context": context(f),
                "context_saved": net["net"],
                "context_saved_estimated": net["partial"],
                "second_agent_fresh": net["fresh"], "second_agent_reasoning": net["reasoning"],
                "returned_to_main": net["returned"], "badge_saved": net["badge"],
                "digest_saved": saved_digest,
                "digest_saved_recorded": saved_recorded,
                "tests_edited": any(TESTFILE.search(k) for k in per_file),
            })
    link_continuations(tasks)
    fixed_outside_feature(tasks)
    return tasks, unlabelled, inputs, heads


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects-root", required=True)
    ap.add_argument("--labels", required=True, help="JSON: {transcript file name: category}")
    ap.add_argument("--version", action="append", required=True)
    ap.add_argument("--transcripts", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--exclude", action="append", default=["exoma", "agent-workflow"])
    ap.add_argument("--show-names", action="store_true")
    ap.add_argument("--solved-share", type=float, default=SOLVED_SHARE)
    ap.add_argument("--partial-share", type=float, default=PARTIAL_SHARE)
    ap.add_argument("--fix-hours", type=float, default=FIX_WINDOW.total_seconds() / 3600)
    ap.add_argument("--search-days", type=float, default=SEARCH_WINDOW.days)
    ap.add_argument("--idle-minutes", type=float, default=IDLE_SECONDS / 60)
    ap.add_argument("--author-before-minutes", type=float, default=AUTHOR_BEFORE.total_seconds() / 60)
    ap.add_argument("--author-after-minutes", type=float, default=AUTHOR_AFTER.total_seconds() / 60)
    ap.add_argument("--link-hours", type=float, default=LINK_WINDOW.total_seconds() / 3600,
                    help="a session starting this long after an earlier one ended can continue it")
    ap.add_argument("--export", metavar="FILE",
                    help="also write the figures to FILE for sharing; refused if it would carry a path or project name")
    args = ap.parse_args()
    if args.export and args.show_names:
        ap.error("--export cannot be combined with --show-names")
    _apply_rule(args)
    tasks, unlabelled, inputs, heads = collect_tasks(args)
    names = {}
    for t in tasks:
        names.setdefault(t["project"], None)
    order = sorted(names, key=lambda n: -sum(1 for t in tasks if t["project"] == n))
    letters = {n: (n if args.show_names else f"Project {metrics.project_letters(i)}") for i, n in enumerate(order)}

    def bucket(n):
        return "0" if n == 0 else "1-2" if n <= 2 else "3-5" if n <= 5 else "6+"

    params = {
        "versions": args.version,
        "solved_share": SOLVED_SHARE, "partial_share": PARTIAL_SHARE, "min_line_chars": MIN_LINE,
        "fix_hours": FIX_WINDOW.total_seconds() / 3600, "search_days": SEARCH_WINDOW.total_seconds() / 86400,
        "idle_minutes": IDLE_SECONDS / 60,
        "link_hours": LINK_WINDOW.total_seconds() / 3600, "infer_seconds": INFER_SECONDS,
        "author_window_minutes": [-AUTHOR_BEFORE.total_seconds() / 60, AUTHOR_AFTER.total_seconds() / 60],
        # git history cannot be hashed cheaply; the main heads it was read at can.
        "main_heads_sha256": hashlib.sha256("\n".join(sorted(heads)).encode()).hexdigest(),
    }
    report = {
        "provenance": metrics.provenance(
            "tools/maintain/measure_task_outcomes.py", params, inputs,
            metric_ids=[m for m in metrics.REGISTRY if m.startswith("outcome.")],
        ),
        "versions": args.version, "unlabelled_sessions": unlabelled,
        "all": summary(tasks),
        "by_version": {v: summary([t for t in tasks if t["version"] == v]) for v in args.version},
        "by_category": {c: summary([t for t in tasks if t["category"] == c]) for c in CODING
                        if any(t["category"] == c for t in tasks)},
        "by_delegated_calls": {b: summary([t for t in tasks if bucket(t["delegated_calls"]) == b])
                               for b in ("0", "1-2", "3-5", "6+")},
        "by_project": {letters[n]: summary([t for t in tasks if t["project"] == n]) for n in order},
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.export:
        try:
            # The projects that contributed a task: only their names can reach the figures.
            metrics.write_export(report, Path(args.export), forbidden=list(names))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
