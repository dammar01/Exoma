"""Read-only index over `graphify-out/graph.json`.

Deliberately parses graph.json directly rather than shelling out to `graphify query`
or running its MCP server, avoiding an extra process and a PATH dependency.

Edge confidence is honored: graphify tags every edge EXTRACTED (read from source),
INFERRED, or AMBIGUOUS. Treating an inferred edge as strong as an extracted one is
how a shortlist starts pointing at files that were never really related.
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

from exoma.core.workspace.workspace_paths import atomic_write_text, workflow_paths
from exoma.utils.osutil import process_alive

GRAPH_DIRNAME = "graphify-out"
GRAPH_FILENAME = "graph.json"
MAX_LEADS = 12
MAX_COMMUNITIES = 4

# Skipped when dating the sources: vendored trees can hold thousands of .py files that
# say nothing about whether THIS project's graph is out of date.
# The extensions _bundle/config/hosts/claude/hooks/graph-refresh.py scans before it refreshes.
# Restated rather than imported — the hook runs from ~/.claude without this package —
# and kept equal by tests/checks/graph_refresh.py, which parses both.
SOURCE_EXTENSIONS = (
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".php", ".go", ".rs", ".java", ".rb",
)
_SKIP_DIRS = {
    "__pycache__", ".git", GRAPH_DIRNAME, "storage", ".workflow",
    "node_modules", "venv", ".venv", "site-packages", "dist", "build", ".tox",
    "vendor", ".next", "coverage", "target",
}

# How much an edge counts toward a node's weight, by graphify's own confidence tag.
_CONFIDENCE_WEIGHT = {
    "extracted": 1.0,
    "inferred": 0.5,
    "ambiguous": 0.25,
}
_DEFAULT_CONFIDENCE_WEIGHT = 0.5

_WORD = re.compile(r"[A-Za-z0-9]+")
# job_manager / JobManager both have to yield {job, manager}, otherwise a hint like
# "job manager reaper" scores zero against the very file it describes.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "when", "what",
    "which", "yang", "untuk", "dari", "dengan", "pada", "atau", "adalah", "juga",
    "bagaimana", "kenapa", "apakah", "agar", "serta", "dalam", "akan", "bisa",
    "code", "file", "files", "kode", "project", "codebase",
}


def graph_path(project_root) -> Path:
    return Path(project_root) / GRAPH_DIRNAME / GRAPH_FILENAME


REFRESH_LOCK_FILENAME = ".refresh.lock"
# Equal to BEAT_STALE_S and LEGACY_MAX_AGE_S in the graph-refresh hook
# (_bundle/config/hosts/claude/hooks/graph-refresh.py; a test parses both). graphify runs without a time limit (DEC-042),
# so a lock is held while its worker keeps rewriting `beat`, every BEAT_INTERVAL_S there, not
# while it is young: a refresh of any length never reads as stale.
_REFRESH_BEAT_STALE_SECONDS = 120
# Locks written before `beat` existed keep the rule they were written under.
_REFRESH_LEGACY_MAX_AGE_SECONDS = 900


def refresh_lock_state(project_root) -> dict:
    """What `graphify-out/.refresh.lock` says, for readers, doctor and clean.

    `state` is `absent`, `active` (a worker is beating, or a legacy lock is young with a live
    pid), `stale` (no beat for _REFRESH_BEAT_STALE_SECONDS, or a legacy lock past its age or
    pid), or `unreadable` (present but not a lock; held while its mtime is younger than the
    beat window, since every writer replaces the file whole). `beat_age` / `age` are seconds
    when known; `pid` and `graphify_pid` are what the lock names.
    """
    path = Path(project_root) / GRAPH_DIRNAME / REFRESH_LOCK_FILENAME
    now = time.time()
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
        started = int(lock["started"])
        pid = int(lock["pid"])
        info = {"path": str(path), "pid": pid, "age": int(now - started),
                "graphify_pid": lock.get("graphify_pid")}
        if "beat" in lock:
            beat_age = now - int(lock["beat"])
            info["beat_age"] = int(beat_age)
            info["state"] = "active" if beat_age < _REFRESH_BEAT_STALE_SECONDS else "stale"
        else:
            young = now - started < _REFRESH_LEGACY_MAX_AGE_SECONDS
            info["state"] = "active" if young and process_alive(pid) else "stale"
        return info
    except FileNotFoundError:
        return {"path": str(path), "state": "absent"}
    except (OSError, ValueError, KeyError, TypeError):
        try:
            mtime_age = now - path.stat().st_mtime
        except OSError:
            return {"path": str(path), "state": "absent"}
        held = mtime_age < _REFRESH_BEAT_STALE_SECONDS
        return {"path": str(path), "state": "unreadable" if held else "stale", "age": int(mtime_age)}


def refresh_in_progress(project_root) -> bool:
    """True while the graph-refresh worker holds `graphify-out/.refresh.lock`.

    graphify rewrites graph.json in place, so a read during a refresh can see half a file
    (DEC-012). A lock that exists but cannot be read is held while it is young: failing open
    there would read the graph exactly when a writer is mid-way through the lock.
    """
    return refresh_lock_state(project_root)["state"] in ("active", "unreadable")


def release_stale_refresh_lock(project_root) -> dict:
    """Remove the refresh lock only when it is stale. Never touches a process.

    Re-read right before the delete: a lock that changed since it was judged belongs to a
    hook that just took it.
    """
    state = refresh_lock_state(project_root)
    if state["state"] != "stale":
        return {**state, "removed": False}
    path = Path(state["path"])
    try:
        seen = path.read_bytes()
        if refresh_lock_state(project_root)["state"] != "stale" or path.read_bytes() != seen:
            return {**state, "removed": False}
        path.unlink()
        return {**state, "removed": True}
    except OSError:
        return {**state, "removed": False}


def load_graph(project_root) -> dict | None:
    """Parsed graph.json, or None when absent/unreadable or being refreshed. Never raises."""
    if refresh_in_progress(project_root):
        return None
    path = graph_path(project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


_STALE_CACHE: dict[tuple[str, int, str], bool | None] = {}
_STALE_CACHE_FILE = "graph-stale.json"


def _stale_cache_path(project_root) -> Path:
    return workflow_paths(Path(project_root))["graph_stale"]


def _read_stale_cache(
    project_root, graph_mtime_ns: int, source_fingerprint: str
) -> bool | None:
    """Cached verdict, or stale when the same graph saw a different source tree."""
    try:
        data = json.loads(
            _stale_cache_path(project_root).read_text(encoding="utf-8")
        )
        if int(data.get("graph_mtime_ns", -1)) == graph_mtime_ns:
            if data.get("source_fingerprint") != source_fingerprint:
                return True
            verdict = data.get("verdict")
            return verdict if isinstance(verdict, bool) else None
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _write_stale_cache(
    project_root, graph_mtime_ns: int, source_fingerprint: str, verdict: bool
) -> None:
    """Best-effort: a cache that cannot be written must not fail the call."""
    try:
        atomic_write_text(
            _stale_cache_path(project_root),
            json.dumps(
                {
                    "graph_mtime_ns": graph_mtime_ns,
                    "source_fingerprint": source_fingerprint,
                    "verdict": verdict,
                }
            ),
        )
    except OSError:
        pass


def _source_state(project_root) -> tuple[str, int] | None:
    """Fingerprint source stat data and return the newest source mtime.

    Source means SOURCE_EXTENSIONS, the set the graph-refresh hook also scans. This used
    to read `*.py` only, so in a PHP or TypeScript project the hook would refresh a graph
    the reader had never called stale, and a reader in such a project reported a graph
    current that was a day behind every file it described. Walked with skip directories
    pruned before descent, the way the hook walks, rather than filtered after it.
    """
    root = Path(project_root)
    rows: list[str] = []
    newest_ns = 0
    try:
        for directory, dirnames, filenames in os.walk(root):
            # Case-insensitive, as the hook prunes: Vendor/ is vendor/ on Windows and macOS.
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
            for name in filenames:
                if not name.lower().endswith(SOURCE_EXTENSIONS):
                    continue
                candidate = Path(directory) / name
                try:
                    relative = candidate.relative_to(root)
                    stat = candidate.stat()
                except (OSError, ValueError):
                    continue
                newest_ns = max(newest_ns, stat.st_mtime_ns)
                rows.append(f"{relative.as_posix()}\0{stat.st_mtime_ns}\0{stat.st_size}")
    except OSError:
        return None
    if not rows:
        return None
    rows.sort()
    fingerprint = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:24]
    return fingerprint, newest_ns


def is_stale(project_root) -> bool | None:
    """True when the graph is older than the newest project Python source file.

    None when it cannot be determined. A stale graph is still useful as leads, but
    the caller must be able to say so rather than presenting it as current.
    """
    path = graph_path(project_root)
    if not path.exists():
        return None
    try:
        graph_mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return None

    source_state = _source_state(project_root)
    if source_state is None:
        return None
    source_fingerprint, newest_ns = source_state
    cache_key = (str(Path(project_root).resolve()), graph_mtime_ns, source_fingerprint)
    if cache_key in _STALE_CACHE:
        return _STALE_CACHE[cache_key]
    if any(
        key[0] == cache_key[0]
        and key[1] == graph_mtime_ns
        and key[2] != source_fingerprint
        for key in _STALE_CACHE
    ):
        _STALE_CACHE[cache_key] = True
        _write_stale_cache(project_root, graph_mtime_ns, source_fingerprint, True)
        return True
    cached = _read_stale_cache(project_root, graph_mtime_ns, source_fingerprint)
    if cached is not None:
        _STALE_CACHE[cache_key] = cached
        return cached

    verdict = newest_ns > graph_mtime_ns
    _STALE_CACHE[cache_key] = verdict
    _write_stale_cache(project_root, graph_mtime_ns, source_fingerprint, verdict)
    return verdict


def _relative_source(source_file: str | None, project_root) -> str | None:
    """graph.json stores absolute OS-native paths; emit repo-relative POSIX ones so the
    shortlist means the same thing on another machine (and matches how file:line
    citations are written everywhere else)."""
    if not source_file:
        return None
    try:
        rel = Path(source_file).resolve().relative_to(Path(project_root).resolve())
        return rel.as_posix()
    except (ValueError, OSError):
        return Path(source_file).name or None


def _nodes(graph: dict) -> list[dict]:
    nodes = graph.get("nodes")
    return [n for n in nodes if isinstance(n, dict)] if isinstance(nodes, list) else []


def _links(graph: dict) -> list[dict]:
    for key in ("links", "edges"):
        value = graph.get(key)
        if isinstance(value, list):
            return [e for e in value if isinstance(e, dict)]
    return []


def _edge_endpoints(edge: dict) -> tuple[str | None, str | None]:
    src = edge.get("source", edge.get("_src"))
    tgt = edge.get("target", edge.get("_tgt"))
    return (str(src) if src is not None else None, str(tgt) if tgt is not None else None)


def _edge_weight(edge: dict) -> float:
    tag = str(edge.get("confidence") or "").strip().lower()
    base = _CONFIDENCE_WEIGHT.get(tag, _DEFAULT_CONFIDENCE_WEIGHT)
    try:
        score = float(edge.get("confidence_score"))
    except (TypeError, ValueError):
        score = None
    return base * score if score is not None and 0 < score <= 1 else base


def _degrees(graph: dict) -> dict[str, float]:
    """Confidence-weighted degree per node id."""
    degrees: dict[str, float] = {}
    for edge in _links(graph):
        weight = _edge_weight(edge)
        for endpoint in _edge_endpoints(edge):
            if endpoint:
                degrees[endpoint] = degrees.get(endpoint, 0.0) + weight
    return degrees


def _keywords(text: str) -> set[str]:
    words: set[str] = set()
    for token in _WORD.findall(_CAMEL.sub(" ", text or "")):
        lowered = token.lower()
        if len(lowered) > 2 and lowered not in _STOPWORDS:
            words.add(lowered)
    return words


def entry_points(project_root, limit: int = MAX_LEADS) -> list[dict]:
    """Highest-connectivity nodes — the structural way into the codebase."""
    graph = load_graph(project_root)
    if not graph:
        return []
    degrees = _degrees(graph)
    ranked = sorted(
        _nodes(graph),
        key=lambda n: degrees.get(str(n.get("id")), 0.0),
        reverse=True,
    )
    return [_as_lead(n, project_root, degrees) for n in ranked[:limit]]


def files_in_community(project_root, community, graph: dict | None = None) -> list[str]:
    graph = graph if graph is not None else load_graph(project_root)
    if not graph:
        return []
    files: list[str] = []
    for node in _nodes(graph):
        if node.get("community") != community:
            continue
        rel = _relative_source(node.get("source_file"), project_root)
        if rel and rel not in files:
            files.append(rel)
    return files


def neighbors(project_root, node_id: str) -> list[str]:
    graph = load_graph(project_root)
    if not graph:
        return []
    found: list[str] = []
    for edge in _links(graph):
        src, tgt = _edge_endpoints(edge)
        other = tgt if src == str(node_id) else (src if tgt == str(node_id) else None)
        if other and other not in found:
            found.append(other)
    return found


def top_files(
    project_root, hint: str = "", limit: int = MAX_LEADS, graph: dict | None = None
) -> list[dict]:
    """Files ranked by keyword overlap with `hint`, then by weighted degree.

    Two ranking rules keep keyword relevance ahead of connectivity:

    1. Degree is normalised to the same 0-10 band as one keyword match, so
       connectivity remains a tiebreaker.
    2. Matches are aggregated ACROSS a file's nodes rather than taken from its single
       best one. graph.json carries no file contents; symbol labels (`JobManager`,
       `reap_stalled`) are its semantic signal.

    With no hint this degrades to pure connectivity ranking, which is still a far
    better starting point than an alphabetical directory listing.
    """
    graph = graph if graph is not None else load_graph(project_root)
    if not graph:
        return []
    degrees = _degrees(graph)
    wanted = _keywords(hint)
    max_degree = max(degrees.values(), default=0.0) or 1.0

    files: dict[str, dict] = {}
    for node in _nodes(graph):
        rel = _relative_source(node.get("source_file"), project_root)
        if not rel:
            continue
        label = str(node.get("label") or node.get("norm_label") or "")
        matched = (wanted & (_keywords(label) | _keywords(rel))) if wanted else set()
        degree = degrees.get(str(node.get("id")), 0.0)

        row = files.get(rel)
        if row is None:
            row = {
                "file": rel,
                "matched_terms": set(),
                "degree": 0.0,
                "community": node.get("community"),
                "label": label,
            }
            files[rel] = row
        row["matched_terms"] |= matched
        if degree > row["degree"]:
            # Keep the community and label of the file's most connected node: that is
            # the one whose cluster membership actually describes the file.
            row["degree"] = degree
            row["community"] = node.get("community")
            row["label"] = label

    ranked: list[dict] = []
    for row in files.values():
        overlap = len(row["matched_terms"])
        score = overlap * 10 + 10 * (row["degree"] / max_degree)
        ranked.append(
            {
                "file": row["file"],
                "score": round(score, 2),
                "matched": overlap,
                "matched_terms": sorted(row["matched_terms"])[:6],
                "community": row["community"],
                "label": row["label"],
            }
        )

    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked[:limit]


def _as_lead(node: dict, project_root, degrees: dict[str, float]) -> dict:
    return {
        "label": str(node.get("label") or node.get("norm_label") or node.get("id") or ""),
        "file": _relative_source(node.get("source_file"), project_root),
        "community": node.get("community"),
        "degree": round(degrees.get(str(node.get("id")), 0.0), 2),
    }


MAX_SUBGRAPH_HOPS = 2
MAX_SUBGRAPH_NODES = 120


def subgraph(
    project_root,
    hint: str = "",
    hops: int = MAX_SUBGRAPH_HOPS,
    limit: int = MAX_LEADS,
    graph: dict | None = None,
) -> dict | None:
    """The slice of the graph reachable from this task's best files, and nothing else.

    Every other reader here walks the whole graph for every call. That is correct for
    ranking — you cannot know which file scores highest without scoring all of them — but
    it is the wrong shape for the question "what is NEAR this work", where the answer is a
    neighbourhood and the rest of the graph is noise the caller has to re-filter.

    Expansion is confidence-ordered, not breadth-first-uniform. Following an AMBIGUOUS edge
    as eagerly as an EXTRACTED one is how a two-hop neighbourhood fills with files that were
    never really related — the exact failure the module docstring warns about, reintroduced
    one layer up. Edges are walked strongest first and the node budget runs out on the weak
    ones.

    Returns None when no graph exists, so callers can tell "no graph" from "graph, nothing
    near this task".
    """
    graph = graph if graph is not None else load_graph(project_root)
    if not graph:
        return None

    seeds_files = {row["file"] for row in top_files(project_root, hint, limit, graph=graph)}
    if not seeds_files:
        return None

    by_id: dict[str, dict] = {}
    seed_ids: list[str] = []
    for node in _nodes(graph):
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        by_id[node_id] = node
        if _relative_source(node.get("source_file"), project_root) in seeds_files:
            seed_ids.append(node_id)
    if not seed_ids:
        return None

    adjacency: dict[str, list[tuple[float, str]]] = {}
    for edge in _links(graph):
        src, tgt = _edge_endpoints(edge)
        if not src or not tgt:
            continue
        weight = _edge_weight(edge)
        adjacency.setdefault(src, []).append((weight, tgt))
        adjacency.setdefault(tgt, []).append((weight, src))

    selected: dict[str, int] = {node_id: 0 for node_id in seed_ids}
    frontier = list(seed_ids)
    for depth in range(1, max(0, hops) + 1):
        if len(selected) >= MAX_SUBGRAPH_NODES:
            break
        candidates: list[tuple[float, str]] = []
        for node_id in frontier:
            candidates.extend(adjacency.get(node_id, []))
        candidates.sort(key=lambda item: item[0], reverse=True)
        next_frontier: list[str] = []
        for weight, neighbour in candidates:
            if neighbour in selected or neighbour not in by_id:
                continue
            if len(selected) >= MAX_SUBGRAPH_NODES:
                break
            selected[neighbour] = depth
            next_frontier.append(neighbour)
        if not next_frontier:
            break
        frontier = next_frontier

    files: dict[str, dict] = {}
    for node_id, depth in selected.items():
        node = by_id[node_id]
        rel = _relative_source(node.get("source_file"), project_root)
        if not rel:
            continue
        row = files.get(rel)
        if row is None or depth < row["hops"]:
            files[rel] = {"file": rel, "hops": depth, "community": node.get("community")}

    return {
        "seed_files": sorted(seeds_files),
        "hops": hops,
        "nodes": len(selected),
        "files": sorted(files.values(), key=lambda row: (row["hops"], row["file"])),
        # Whether the walk stopped because it ran out of graph or ran out of budget. A
        # truncated neighbourhood presented as a complete one is the same lie as a silently
        # capped search result.
        "truncated": len(selected) >= MAX_SUBGRAPH_NODES,
        "node_budget": MAX_SUBGRAPH_NODES,
    }


def _edge_confidence_mix(graph: dict) -> dict[str, int]:
    """How many edges carry each confidence tag.

    graphify has always tagged edges EXTRACTED/INFERRED/AMBIGUOUS and this module has
    always used those tags to weight ranking — but only internally, so a shortlist built
    mostly from inferred edges looked exactly like one built from read-from-source facts.
    Surfacing the mix is what lets a reader discount the former.
    """
    mix: dict[str, int] = {}
    for edge in _links(graph):
        tag = str(edge.get("confidence") or "unknown").strip().lower() or "unknown"
        mix[tag] = mix.get(tag, 0) + 1
    return mix


def leads(project_root, hint: str = "", limit: int = MAX_LEADS) -> dict | None:
    """Shortlist for one delegated call, or None when no graph exists.

    Shape: {'files': [...], 'communities': [...], 'stale': bool|None, 'node_count': int}
    """
    # Parse once and pass the graph to helpers used by this delegated call.
    graph = load_graph(project_root)
    if not graph:
        return None
    files = top_files(project_root, hint, limit, graph=graph)
    if not files:
        return None

    communities: list[dict] = []
    seen: set = set()
    for row in files:
        community = row.get("community")
        if community is None or community in seen:
            continue
        seen.add(community)
        members = files_in_community(project_root, community, graph=graph)
        if members:
            communities.append({"community": community, "files": members[:8]})
        if len(communities) >= MAX_COMMUNITIES:
            break

    return {
        "files": files,
        "communities": communities,
        "stale": is_stale(project_root),
        "node_count": len(_nodes(graph)),
        "edge_count": len(_links(graph)),
        # The evidence quality behind the ranking, not just its output. Already used to
        # weight every score in this file; it was simply never said out loud.
        "edge_confidence": _edge_confidence_mix(graph),
    }
