"""Project-local, evidence-derived fact store (.workflow/facts.jsonl).

Memory-like knowledge distilled from delegated runs. Ingests ONLY durable facts
(config / pattern / invariant) or claims that recur across >= RECURRENCE_THRESHOLD
distinct sessions. Every fact is anchored to a file:line content hash so staleness
is detectable — at INGEST (skip facts whose anchor already vanished, i.e. no
stale-at-birth) and at READ (drop/flag facts whose anchored line changed).

Raw per-run logs keep everything; this store keeps only the durable/recurring subset.
"""
import hashlib
import json
import re
from pathlib import Path

from core.runtime.config_defaults import default_policies
from core.workspace.workspace_paths import (
    _safe_component,
    atomic_write_text,
    now_iso,
    read_json_file,
    workflow_paths,
)
from utils.owned_lock import OwnedFileLock

FACTS_FILENAME = "facts.jsonl"
LOCK_FILENAME = "facts.jsonl.lock"
USAGE_FILENAME = "facts-usage.json"
# How long a writer waits for a live holder, and how old an ownerless lock file must be
# before it counts as a crashed writer's. ingest is sub-second, so 30s is generous.
LOCK_TTL_SECONDS = 30
# Distinct OTHER sessions a grounded claim must appear in to auto-promote. Read from the
# one default rather than restated: config.json holds overrides only, so this fallback IS
# the effective value for every project that never set the key, and a separate literal
# here (it was 3 while the documented default said 5) silently promoted claims early.
RECURRENCE_THRESHOLD = default_policies()["fact_recurrence_threshold"]
MAX_FACTS = default_policies()["fact_max_facts"]

# Provenance of a stored fact. Only `discovered` (a run read code and grounded the claim)
# may raise cross-session recurrence; a fact that merely got injected and echoed back must
# never re-promote itself. user_provided is reserved for facts a human asserts directly.
ORIGIN_DISCOVERED = "discovered"
ORIGIN_MEMORY_INJECTED = "memory_injected"
ORIGIN_USER_PROVIDED = "user_provided"
RELEVANT_LIMIT = default_policies()["fact_relevant_limit"]
# Per-role defaults for `fact_relevant_limits`. Reasoning (plan/analyze) gets one more
# slot than exploration because it weighs several subsystems at once; neither number is
# measured yet — the fact lifecycle telemetry exists so they can be.
RELEVANT_LIMITS = default_policies()["fact_relevant_limits"]
# Weighted overlap a fact must reach to be served. 2.0 = one identifier, or two ordinary
# words, or four path-only tokens. The old floor was ONE shared token of any kind, so a
# fact matched every English task through `the` and every task through `core`.
MIN_RELEVANCE_SCORE = default_policies()["fact_min_relevance"]
IDENTIFIER_WEIGHT = 2.0
WORD_WEIGHT = 1.0
PATH_WEIGHT = 0.5
# A claim long enough to be a paragraph is a summary, not a fact, and it is injected
# into every matching prompt. Refused at ingest rather than truncated: a cut claim can
# state the opposite of the whole one.
MAX_CLAIM_CHARS = default_policies()["fact_max_claim_chars"]
# Two claims anchored to the SAME file are treated as one when their word sets
# overlap this much — proxies rephrase the same fact every run, and the plain
# normalized-string key never catches that.
DUPLICATE_SIMILARITY = 0.5
# Short claims overlap by accident ("validates token" vs "handles token" is 0.5),
# so similarity is only trusted once both claims carry enough words to be specific.
# Below that, only an exact normalized match counts as a duplicate.
MIN_WORDS_FOR_SIMILARITY = 6
# Line proximity is a WEAK identity signal, so it is allowed only for read-time dedup
# (which merely trims what gets injected). A collapse that deletes a stored record
# always demands an identical anchor_hash — losing a real fact is unrecoverable.
LINE_PROXIMITY = 3

# The optional single-letter prefix is a Windows drive. Without it the path group, which
# cannot contain `:`, stops at the drive colon: `E:\work\router.py:16` matched only
# `\work\router.py:16`, and that fragment resolves nowhere. An analysis whose evidence
# lives outside the repo — a transcript under ~/.claude, a sibling project — came back
# reporting zero anchors while every claim named its file. One letter is deliberate:
# `https:` and `note:` stay unmatched, so the shape this admits is still a path. The drive
# letter must also START a word: without that lookbehind a label before the path lent its
# last letter as a drive, so `proof:core/x.py:200` became `f:core/x.py:200` and
# `proxy:core/a.py:5` became `y:core/a.py:5` — real files reported as uncertified.
_FILELINE = re.compile(r"((?:(?<![A-Za-z0-9_])[A-Za-z]:)?[A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+):(\d+)")
_CATEGORY = re.compile(r"^\[(config|pattern|invariant)\]", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9_]+")
# "X is cached" and "X is not cached" share almost every word — high similarity, opposite
# meaning. A polarity mismatch vetoes a collapse outright.
_NEGATION = re.compile(
    r"\b(not|no|never|without|non|un|cannot|tidak|bukan|tanpa|belum|jangan)\b"
)


def _facts_path(project_root: Path) -> Path:
    return workflow_paths(project_root)["facts_store"]


class _FactLock(OwnedFileLock):
    """Cross-process advisory lock around the facts.jsonl read-modify-write.

    ingest/prune both load the whole store, mutate it, and rewrite it. Two sessions doing
    that at once (the norm for one project) would each save their own view, and the second
    write would silently drop the first's additions. An O_EXCL lock-file serialises them.

    Ownership lives in utils/owned_lock.py: a lock is taken over only when its recorded
    owner is no longer running, and release removes only a lock this holder still owns.
    A live holder past the wait raises TimeoutError; ingest reports that as
    `fact_ingest_error` rather than writing beside it.
    """

    def __init__(self, project_root: Path):
        super().__init__(_facts_path(project_root).with_name(LOCK_FILENAME), LOCK_TTL_SECONDS)


def _policies(project_root: Path) -> dict:
    """Project-local tuning knobs; falls back to module constants when absent."""
    try:
        config = read_json_file(workflow_paths(project_root)["config"])
    except (OSError, ValueError):
        return {}
    policies = config.get("policies")
    return policies if isinstance(policies, dict) else {}


def _policy_int(project_root: Path, key: str, fallback: int) -> int:
    value = _policies(project_root).get(key, fallback)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


# Function words of the two languages tasks arrive in. Matching on them is matching on
# grammar: an English task shared `the` with nearly every stored claim, and an Indonesian
# task shared `yang` with none of them, so the same intent was over-served in one
# language and starved in the other. Only words of three or more letters are listed —
# the tokenizer never produces shorter ones.
STOPWORDS = frozenset(
    """
    the and for are was were with that this these those from into onto over under than
    then them they their there here what when where which while who whom whose why how
    has have had not but its can could should would will shall may might must does did
    done any all each every some such only also very just more most less least other
    about after before again against between both during through until upon via per
    yang dan atau untuk dari dengan pada dalam ini itu tidak bukan ada akan sudah belum
    juga saja oleh karena agar supaya jika kalau maka bahwa apa apakah bagaimana gimana
    mana siapa kenapa mengapa lalu kemudian serta sebagai seperti para nya kan dong
    lakukan buat bikin coba tolong mohon harus bisa dapat lagi masih sangat lebih
    """.split()
)
# Path segments so common that sharing one says nothing about relevance.
GENERIC_PATH_TOKENS = frozenset(
    """
    core src lib app tests test checks dist config docs utils tools main index init
    module modules common shared base py md json yaml yml txt js ts
    """.split()
)
_TOKEN = re.compile(r"[a-z0-9_]{3,}")
# An identifier is anything the author would not have written as plain prose: a
# snake_case or dotted name, a camelCase word, or something in backticks.
_IDENTIFIER = re.compile(r"`([^`]+)`|\b([A-Za-z]+[A-Z_][A-Za-z0-9_]*|[a-z0-9]+(?:_[a-z0-9]+)+)\b")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in STOPWORDS}


def _identifier_tokens(text: str) -> set[str]:
    out: set[str] = set()
    for m in _IDENTIFIER.finditer(text or ""):
        span = m.group(1) or m.group(2) or ""
        out |= {t for t in _TOKEN.findall(span.lower()) if t not in STOPWORDS}
        if m.group(2) and "_" in m.group(2):
            out.add(m.group(2).lower())
    return out


def relevance(task: str, claim: str, path: str | None = None) -> tuple[float, int]:
    """(weighted score, identifier hits) of one claim for one task.

    Weights: an identifier both sides name is worth 2, an ordinary word 1, a token found
    only in the fact's path 0.5 — the path says where a fact lives, not what it is about.
    Generic path segments (`core`, `tests`, `py`) and function words score nothing.
    Shared with core/knowledge/retrieve.py so facts and knowledge in one prompt agree on
    what the task is.
    """
    task_tokens = _tokens(task) - GENERIC_PATH_TOKENS
    if not task_tokens:
        return 0.0, 0
    task_ids = _identifier_tokens(task)
    claim_tokens = _tokens(claim) - GENERIC_PATH_TOKENS
    claim_ids = _identifier_tokens(claim)
    path_tokens = _tokens((path or "").replace("/", " ").replace("\\", " ").replace(".", " "))
    path_tokens -= GENERIC_PATH_TOKENS
    path_tokens -= claim_tokens

    shared = task_tokens & claim_tokens
    id_hits = len(shared & (task_ids | claim_ids))
    words = len(shared) - id_hits
    path_hits = len(task_tokens & path_tokens)
    score = id_hits * IDENTIFIER_WEIGHT + words * WORD_WEIGHT + path_hits * PATH_WEIGHT
    return score, id_hits


def _policy_limit(project_root: Path, role: str | None) -> int:
    """The fact limit for one role: `fact_relevant_limits[role]`, else the scalar key."""
    scalar = _policy_int(project_root, "fact_relevant_limit", RELEVANT_LIMIT)
    per_role = _policies(project_root).get("fact_relevant_limits")
    if role and isinstance(per_role, dict) and role in per_role:
        try:
            value = int(per_role[role])
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    # An explicit scalar override is the user's number and wins over the role default.
    if "fact_relevant_limit" in _policies(project_root):
        return scalar
    return RELEVANT_LIMITS.get(role or "", scalar)


def _policy_float(project_root: Path, key: str, fallback: float) -> float:
    value = _policies(project_root).get(key, fallback)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _normalize(claim: str) -> str:
    text = _FILELINE.sub("", claim)          # drop file:line
    text = re.sub(r"\[[^\]]*\]", "", text)   # drop [tags]
    text = text.replace("*", "").replace("`", "")  # drop markdown emphasis/code ticks
    return re.sub(r"\s+", " ", text).strip().lower()


def _words(normalized: str) -> set[str]:
    return set(_WORD.findall(normalized))


def _similar(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _signature(
    claim: str, category: str | None, anchor_hash: str | None, line: int | None
) -> dict:
    normalized = _normalize(claim)
    return {
        "words": _words(normalized),
        "negated": bool(_NEGATION.search(normalized)),
        "category": category,
        "anchor_hash": anchor_hash,
        "line": line,
    }


def _fact_signature(fact: dict) -> dict:
    return _signature(
        fact.get("claim", ""),
        fact.get("category"),
        fact.get("anchor_hash"),
        fact.get("line"),
    )


def _is_duplicate(
    sig: dict, others: list[dict], *, allow_line_proximity: bool = False
) -> bool:
    """Two same-file claims are the same fact only when EVERY guard agrees.

    Word similarity alone is not identity: it happily merges a claim with its own
    negation, or two unrelated facts that happen to share vocabulary. Each guard below
    exists to veto one of those false merges, and a veto always means "keep both".
    """
    for other in others:
        if sig["category"] != other["category"]:
            continue  # a [config] value and an [invariant] are not interchangeable
        if sig["negated"] != other["negated"]:
            continue  # opposite polarity — never the same fact
        same_anchor = (
            sig["anchor_hash"] is not None
            and sig["anchor_hash"] == other["anchor_hash"]
        )
        near_line = (
            allow_line_proximity
            and sig["line"] is not None
            and other["line"] is not None
            and abs(sig["line"] - other["line"]) <= LINE_PROXIMITY
        )
        if not (same_anchor or near_line):
            continue  # different anchor → treat as different facts
        if sig["words"] == other["words"]:
            return True
        if min(len(sig["words"]), len(other["words"])) < MIN_WORDS_FOR_SIMILARITY:
            continue
        if _similar(sig["words"], other["words"]) >= DUPLICATE_SIMILARITY:
            return True
    return False


# A sibling section starts at column 0 with a snake_case key and a colon — the shape
# every block in the agent's output format uses (`grounded:`, `external:`, `confidence:`).
# Trailing text after the colon still counts: `external: none (...)` ends the previous
# block, it is NOT a continuation of the last bullet.
_SECTION_HEADER = re.compile(r"^[a-z][a-z0-9_]{0,30}:(\s|$)")
_BULLET = re.compile(r"^[-*•]\s*")


def _parse_block(content: str, header: str) -> list[str]:
    """Bullet lines under `header:`, up to the next section header.

    Blank lines, sub-bullets, fenced blocks, and alternate bullet markers do not end
    the section; only a genuine new section does.
    """
    out: list[str] = []
    collecting = False
    in_fence = False
    header_prefix = header.lower() + ":"

    for line in content.splitlines():
        stripped = line.strip()

        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        if not collecting:
            if stripped.lower().startswith(header_prefix):
                collecting = True
                inline = stripped[len(header_prefix):].strip()
                if inline and inline.lower() != "none":
                    out.append(_BULLET.sub("", inline).strip())
            continue

        if not stripped:
            continue  # blank lines separate bullets, they do not end the section
        # Unindented on purpose: an indented `key:` is sub-structure of the current
        # bullet, only a column-0 key opens the next section.
        if _SECTION_HEADER.match(line) and not _BULLET.match(stripped):
            break
        if stripped.startswith("[") and stripped.endswith("]"):
            break  # a bracketed block marker, e.g. [DIGEST]
        if _BULLET.match(stripped):
            out.append(_BULLET.sub("", stripped).strip())
            continue
        if out:
            # Continuation of the previous bullet (wrapped line).
            out[-1] = f"{out[-1]} {stripped}".strip()

    return [o for o in out if o and o.lower() != "none"]


def _extract_fileline(text: str) -> tuple[str | None, int | None]:
    m = _FILELINE.search(text)
    return (m.group(1), int(m.group(2))) if m else (None, None)


def _extract_filelines(text: str) -> list[tuple[str, int]]:
    """Every distinct `file:line` in a claim, in order. The first is the primary anchor."""
    out: list[tuple[str, int]] = []
    for m in _FILELINE.finditer(text or ""):
        key = (m.group(1), int(m.group(2)))
        if key not in out:
            out.append(key)
    return out


def _extract_category(text: str) -> str | None:
    m = _CATEGORY.match(text.strip())
    return m.group(1).lower() if m else None


def _line_hash(text: str) -> str:
    """The anchor value for one line of source. Whitespace is stripped first, so
    re-indenting a block does not invalidate every fact anchored inside it."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def _read_lines(project_root: Path, file: str | None) -> list[str] | None:
    """Lines of a project-relative file, or None if it cannot be read as one.

    The containment check is the reason this is not just `read_text`: `file` arrives from a
    stored fact, and a fact claiming `../../etc/passwd` must not turn into a read outside
    the project root.
    """
    if not file:
        return None
    try:
        root = Path(project_root).resolve()
        path = (root / file).resolve()
    except (OSError, ValueError):
        return None
    if path != root and root not in path.parents:
        return None
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _anchor_hash(project_root: Path, file: str | None, line: int | None) -> str | None:
    """Hash the referenced line's content. None if file/line is missing or out of range
    (used as the light ingest-time verify AND the read-time staleness check)."""
    if not line:
        return None
    lines = _read_lines(project_root, file)
    if lines is None or line < 1 or line > len(lines):
        return None
    return _line_hash(lines[line - 1])


def _hash_index(
    project_root: Path, file: str | None, cache: dict | None = None
) -> dict[str, list[int]]:
    """Every line hash in a file mapped to the line numbers carrying it.

    Built once per file per call via `cache`: without it, a store where many facts point at
    the same edited file re-reads and re-hashes that file once per fact.
    """
    if cache is not None and file in cache:
        return cache[file]
    lines = _read_lines(project_root, file)
    index: dict[str, list[int]] = {}
    for number, text in enumerate(lines or [], 1):
        index.setdefault(_line_hash(text), []).append(number)
    if cache is not None:
        cache[file] = index
    return index


def current_anchor_line(
    project_root: Path,
    file: str | None,
    line: int | None,
    anchor_hash: str | None,
    cache: dict | None = None,
) -> int | None:
    """Where this fact's anchor text lives NOW, or None if the fact can no longer be placed.

    Staleness used to be decided by one question — does line N still hash to the recorded
    value? — which conflates two very different events. A line whose CONTENT changed is a
    fact that may well have stopped being true. A line that merely MOVED, because something
    was inserted above it, is the same source text at a new index: the fact is intact and
    only the lookup is wrong. The first question was answering both, so inserting a block at
    the top of a file silently invalidated every fact anchored below it.

    So: try the recorded line first, and if that fails, look for the recorded hash elsewhere
    in the file. A UNIQUE match is a relocation — same text, new position, nothing about the
    claim weakened. Anything else is still a drop.

    The uniqueness rule is what keeps this honest. Anchor text like a lone `)`, or a bare
    docstring delimiter, hashes identically on dozens of lines, and picking the first would
    hand back a fact pointing at unrelated code with full confidence. An anchor that cannot
    single out a line never identified anything in the first place; it is dropped exactly as
    before.
    """
    if not anchor_hash:
        return None
    if line and _anchor_hash(project_root, file, line) == anchor_hash:
        return line
    matches = _hash_index(project_root, file, cache).get(anchor_hash) or []
    return matches[0] if len(matches) == 1 else None


def _load_facts(project_root: Path) -> list[dict]:
    path = _facts_path(project_root)
    if not path.exists():
        return []
    facts: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            facts.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return facts


def _dedupe(facts: list[dict], *, allow_line_proximity: bool = False) -> list[dict]:
    """Collapse duplicate claims anchored to the same file, keeping the first.

    The store is append-only, so a rephrased-but-identical fact would otherwise be
    stored (and later injected) once per phrasing.

    `allow_line_proximity` is for READ-time trimming only, where dropping a near-duplicate
    costs nothing. Persistent collapse (save/ingest/prune) leaves it off, so a stored
    record is deleted only when another record carries the very same anchor.
    """
    kept: list[dict] = []
    per_file: dict[str | None, list[dict]] = {}
    for fact in facts:
        sig = _fact_signature(fact)
        if not sig["words"]:
            continue
        file = fact.get("file")
        if _is_duplicate(
            sig, per_file.get(file, []), allow_line_proximity=allow_line_proximity
        ):
            continue
        per_file.setdefault(file, []).append(sig)
        kept.append(fact)
    return kept


def _evict(project_root: Path, facts: list[dict], cap: int) -> list[dict]:
    """Keep `cap` facts, chosen by value; the store's order is preserved among them.

    The store used to keep its newest tail, which evicted a fact served in every session
    for a month the moment enough new ones arrived. Value here is use first (`hits`), then
    how recently it was used or, never used, captured, then position. A store whose facts
    were never served ranks by capture time and position — the old newest-tail rule.
    """
    if len(facts) <= cap:
        return facts
    usage = _load_usage(project_root)

    def value(item: tuple[int, dict]) -> tuple:
        index, fact = item
        entry = usage.get(_fact_key(fact))
        entry = entry if isinstance(entry, dict) else {}
        last = entry.get("last_used") or fact.get("captured_at") or ""
        return (int(entry.get("hits") or 0), str(last), index)

    keep = sorted(enumerate(facts), key=value, reverse=True)[:cap]
    return [fact for _, fact in sorted(keep, key=lambda item: item[0])]


def _save_facts(project_root: Path, facts: list[dict]) -> None:
    path = _facts_path(project_root)
    facts = _evict(
        project_root, _dedupe(facts), _policy_int(project_root, "fact_max_facts", MAX_FACTS)
    )
    atomic_write_text(
        path,
        "\n".join(json.dumps(f, ensure_ascii=False) for f in facts) + ("\n" if facts else ""),
    )


def _recurrence_counts(
    project_root: Path,
    exclude_session_id: str | None = None,
    injected: set[str] | None = None,
) -> dict[str, int]:
    """normalized grounded claim -> number of DISTINCT sessions whose logs contain it.

    The CURRENT session is excluded: its output.raw.md is written before ingest runs,
    so counting it lets a fact that was injected as a KNOWN_FACT (and merely echoed
    back by second_agent) raise its own recurrence — evidence laundering, not support.

    `injected` extends that guard ACROSS sessions: any claim already in the store gets
    injected as a KNOWN_FACT into every later run, so its reappearance in those runs'
    `grounded` blocks is an echo, not independent support. Such claims are skipped in all
    sessions, not just the current one.
    """
    paths = workflow_paths(project_root)
    sessions = paths["sessions_dir"]
    cache_path = paths["recurrence_cache"]
    if not sessions.exists():
        return {}
    excluded = _safe_component(exclude_session_id) if exclude_session_id else None
    injected_norms = injected or set()
    cache = _load_claim_cache(cache_path)
    dirty = False
    per_claim: dict[str, set[str]] = {}
    for sdir in sessions.iterdir():
        logs = sdir / "logs"
        if not (sdir.is_dir() and logs.exists()):
            continue
        if excluded and sdir.name == excluded:
            continue
        runs = sorted((p for p in logs.iterdir() if p.is_dir()), key=lambda p: p.name)
        fingerprint_parts: list[str] = []
        for run in runs:
            out = run / "output.raw.md"
            try:
                stat = out.stat()
                fingerprint_parts.append(
                    f"{run.name}:{stat.st_mtime_ns}:{stat.st_size}"
                )
            except OSError:
                fingerprint_parts.append(f"{run.name}:missing")
        fingerprint = hashlib.sha256(
            "\n".join(fingerprint_parts).encode("utf-8")
        ).hexdigest()[:24]
        entry = cache.get(sdir.name)
        if entry and entry.get("fingerprint") == fingerprint:
            seen = set(entry.get("claims") or [])
        else:
            seen = set()
            for run in runs:
                out = run / "output.raw.md"
                if not out.exists():
                    continue
                try:
                    content = out.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for claim in _parse_block(content, "grounded"):
                    norm = _normalize(claim)
                    if norm:
                        seen.add(norm)
            cache[sdir.name] = {"fingerprint": fingerprint, "claims": sorted(seen)}
            dirty = True
        # The injected filter is applied AFTER the cache, never baked into it: it varies
        # per call, and a cache that folded it in would answer the wrong question later.
        for norm in seen - injected_norms:
            per_claim.setdefault(norm, set()).add(sdir.name)

    if dirty:
        _save_claim_cache(cache_path, cache, {p.name for p in sessions.iterdir()})
    return {claim: len(dirs) for claim, dirs in per_claim.items()}


_CLAIM_CACHE_FILE = "recurrence-cache.json"


def _load_claim_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_claim_cache(path: Path, cache: dict, live: set[str]) -> None:
    """Best-effort write, dropping sessions that no longer exist.

    Pruning here rather than never: `prune_jobs` deletes old session dirs, and a cache
    that only grew would outlive the data it describes.
    """
    try:
        pruned = {k: v for k, v in cache.items() if k in live}
        atomic_write_text(path, json.dumps(pruned))
    except OSError:
        pass


def ingest(
    project_root: Path, content: str, session_id: str, stats: dict | None = None
) -> int:
    """Promote qualifying claims from one run's output into the fact store. Returns count added.

    The whole read-modify-write runs under _FactLock so a concurrent ingest in another
    session cannot overwrite this one's additions.

    Every `file:line` a claim cites must resolve, not only the first. The store used to
    hash the first anchor and ignore the rest, so a claim citing one real line and one
    file that does not exist was stored whole — and served later with the false half
    attached. `stats`, when given, receives the counts the telemetry reports:
    `rejected_anchor`, `rejected_length`, `duplicates`.
    """
    counts = stats if stats is not None else {}
    for key in ("rejected_anchor", "rejected_length", "duplicates"):
        counts.setdefault(key, 0)
    max_chars = _policy_int(project_root, "fact_max_claim_chars", MAX_CLAIM_CHARS)
    with _FactLock(project_root):
        loaded = _load_facts(project_root)
        existing = _dedupe(loaded)
        collapsed = len(loaded) - len(existing)
        seen: dict[str | None, list[dict]] = {}
        for fact in existing:
            sig = _fact_signature(fact)
            if sig["words"]:
                seen.setdefault(fact.get("file"), []).append(sig)
        added = 0

        def _try_add(
            raw_claim: str, category: str, origin: str = ORIGIN_DISCOVERED
        ) -> None:
            nonlocal added
            refs = _extract_filelines(raw_claim)
            if not refs:
                counts["rejected_anchor"] += 1
                return
            hashes = [_anchor_hash(project_root, f, n) for f, n in refs]
            if any(h is None for h in hashes):
                # ingest-time verify: any anchor gone/invalid → skip stale-at-birth
                counts["rejected_anchor"] += 1
                return
            claim = _CATEGORY.sub("", raw_claim).strip()
            if len(claim) > max_chars:
                counts["rejected_length"] += 1
                return
            (file, line), anchor = refs[0], hashes[0]
            sig = _signature(claim, category, anchor, line)
            if not sig["words"]:
                return
            if _is_duplicate(sig, seen.get(file, [])):
                counts["duplicates"] += 1
                return
            seen.setdefault(file, []).append(sig)
            fact = {
                "claim": claim,
                "category": category,
                "file": file,
                "line": line,
                "anchor_hash": anchor,
                "origin": origin,
                "captured_at": now_iso(),
                "session": session_id,
            }
            if len(refs) > 1:
                # The primary anchor stays in file/line/anchor_hash, where every reader
                # written before this field already looks; the rest ride beside it.
                fact["anchors"] = [
                    {"file": f, "line": n, "anchor_hash": h}
                    for (f, n), h in zip(refs[1:], hashes[1:])
                ]
            existing.append(fact)
            added += 1

        # Everything already stored is exactly what load_relevant injects as a KNOWN_FACT
        # into later runs, so its reappearance in their `grounded` is an echo, not support.
        # Feed that set to recurrence so an injected fact can never re-promote itself.
        injected = {_normalize(f.get("claim", "")) for f in existing}

        # 1) explicit durable facts (config/pattern/invariant) — read from THIS run's output
        for item in _parse_block(content, "durable_facts"):
            _try_add(item, _extract_category(item) or "invariant")

        # 2) grounded claims recurring across >= threshold distinct OTHER sessions
        threshold = _policy_int(
            project_root, "fact_recurrence_threshold", RECURRENCE_THRESHOLD
        )
        counts = _recurrence_counts(
            project_root, exclude_session_id=session_id, injected=injected
        )
        for claim in _parse_block(content, "grounded"):
            if counts.get(_normalize(claim), 0) >= threshold:
                _try_add(claim, "recurring")

        if added or collapsed:
            _save_facts(project_root, existing)
        return added


def _fact_key(fact: dict) -> str:
    """Stable identity of one stored fact for the usage sidecar."""
    raw = f"{fact.get('file')}|{fact.get('anchor_hash')}|{_normalize(fact.get('claim', ''))}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def served_fact_refs(facts: list[dict]) -> list[dict]:
    """Identity of each served fact, for the call's archived meta (EXP-013).

    `key` is the usage-sidecar key, so a replay can join a call to the store; file and line
    are the anchor as served, which is what an answer would cite.
    """
    return [
        {"key": _fact_key(f), "file": f.get("file"), "line": f.get("line")}
        for f in facts
    ]


def _usage_path(project_root: Path) -> Path:
    return _facts_path(project_root).with_name(USAGE_FILENAME)


def _load_usage(project_root: Path) -> dict:
    try:
        data = json.loads(_usage_path(project_root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_usage(project_root: Path, served: list[dict]) -> None:
    """Note that these facts were served: `hits` and `last_used`, in a sidecar.

    Not in facts.jsonl. Rewriting the store on every read would put every delegated call
    behind `_FactLock`, which ingest and prune hold for a whole read-modify-write. The
    sidecar is written without the lock and best-effort: two calls racing lose one hit,
    which eviction can afford and a fact cannot.
    """
    if not served:
        return
    usage = _load_usage(project_root)
    stamp = now_iso()
    for fact in served:
        key = _fact_key(fact)
        entry = usage.get(key)
        entry = entry if isinstance(entry, dict) else {}
        usage[key] = {"hits": int(entry.get("hits") or 0) + 1, "last_used": stamp}
    try:
        atomic_write_text(_usage_path(project_root), json.dumps(usage, sort_keys=True))
    except OSError:
        pass


def _anchors_placeable(project_root: Path, fact: dict, cache: dict) -> dict | None:
    """The fact with every anchor placed where its text sits today, or None if any is gone.

    The primary anchor is file/line/anchor_hash; any extra ones ride in `anchors`. A fact
    is only as fresh as its weakest citation, so one unplaceable extra drops the fact.
    Returns the SAME dict when nothing moved, a copy when something did.
    """
    placed = current_anchor_line(
        project_root, fact.get("file"), fact.get("line"), fact.get("anchor_hash"), cache
    )
    if placed is None:
        return None
    out = fact if placed == fact.get("line") else {**fact, "line": placed}
    extras = fact.get("anchors")
    if isinstance(extras, list) and extras:
        moved = []
        for extra in extras:
            if not isinstance(extra, dict):
                return None
            where = current_anchor_line(
                project_root, extra.get("file"), extra.get("line"), extra.get("anchor_hash"), cache
            )
            if where is None:
                return None
            moved.append(extra if where == extra.get("line") else {**extra, "line": where})
        if moved != extras:
            out = {**out, "anchors": moved}
    return out


def _newest_first(stamp) -> str:
    """Sort key that puts the NEWER ISO timestamp first in an ascending sort."""
    text = str(stamp or "")
    return "".join(chr(0x10FFFF - ord(ch)) for ch in text) if text else chr(0x10FFFF)


def load_relevant(
    project_root: Path,
    task: str,
    limit: int | None = None,
    *,
    role: str | None = None,
    stats: dict | None = None,
) -> list[dict]:
    """FRESH facts relevant to `task` (stale ones dropped — never served as fresh).

    Scored by `relevance` and kept only at or above `fact_min_relevance` (default 2.0).
    Ties break on identifier hits, then recency, then the claim text, so the same store
    and task always serve the same facts. `stats`, when given, receives `candidates`,
    `rejected_stale`, `rejected_irrelevant` and `injected`.
    """
    counts = stats if stats is not None else {}
    for key in ("candidates", "rejected_stale", "rejected_irrelevant", "injected"):
        counts.setdefault(key, 0)
    if limit is None:
        limit = _policy_limit(project_root, role)
    floor = _policy_float(project_root, "fact_min_relevance", MIN_RELEVANCE_SCORE)
    # read-time trimming: nothing is deleted here, so the weaker proximity signal is safe
    facts = _dedupe(_load_facts(project_root), allow_line_proximity=True)
    counts["candidates"] += len(facts)
    if not facts:
        return []
    scored: list[tuple[tuple, dict]] = []
    index_cache: dict = {}
    for f in facts:
        # A moved line comes back as a copy: the dicts come from _load_facts and writing
        # through them here would edit the store from a read path. Persisting the move is
        # `prune`'s job, which holds the lock.
        placed = _anchors_placeable(project_root, f, index_cache)
        if placed is None:
            counts["rejected_stale"] += 1
            continue  # stale/invalid/unplaceable → drop, do not inject
        score, id_hits = relevance(task, placed.get("claim", ""), placed.get("file"))
        if score < floor:
            counts["rejected_irrelevant"] += 1
            continue  # below the floor == irrelevant; do not pad the top-N with noise
        rank = (-score, -id_hits, _newest_first(placed.get("captured_at")), placed.get("claim", ""))
        scored.append((rank, placed))
    scored.sort(key=lambda pair: pair[0])
    served = [f for _, f in scored[:limit]]
    counts["injected"] += len(served)
    _record_usage(project_root, served)
    return served


def prune(project_root: Path) -> dict:
    """Drop unplaceable facts, RELOCATE the ones that only moved, and collapse duplicates.

    This is the only path that deletes, so it is also the only sensible place to write a
    relocation back. `load_relevant` recomputes the move on every read and throws it away —
    correct for a read path, but it means a shifted fact pays the file scan forever. Here the
    lock is already held and the file is already being rewritten, so the new line number
    persists and later reads hit the fast path again. Every anchor a fact carries is placed,
    not only the first.
    """
    with _FactLock(project_root):
        facts = _load_facts(project_root)
        index_cache: dict = {}
        fresh: list[dict] = []
        relocated = 0
        for f in facts:
            placed = _anchors_placeable(project_root, f, index_cache)
            if placed is None:
                continue
            if placed is not f:
                relocated += 1
            fresh.append(placed)
        deduped = _dedupe(fresh)
        if len(deduped) != len(facts) or relocated:
            _save_facts(project_root, deduped)
        return {
            "kept": len(deduped),
            "removed": len(facts) - len(fresh),
            "relocated": relocated,
            "duplicates_collapsed": len(fresh) - len(deduped),
        }
