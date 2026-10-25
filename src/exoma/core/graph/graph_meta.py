"""Provenance, commit awareness, and per-node verification for a graph this repo cannot build.

Graphify is an external CLI (PyPI `graphifyy`); `graphify-out/graph.json` arrives already
written and nothing here can change how it is produced. That ruled out the obvious design
— add fields to the nodes — and for a while it looked like it ruled out the feature.

It does not, because the graph already carries the hard part. Every node states its
`source_file` and `source_location`, which is provenance down to the line. What was
missing is everything ABOUT that provenance: which commit it was true at, and whether the
line it points to still says what it said. Both are computable here, from the repo, and
both go in a sidecar keyed by node id rather than into a file this repo does not own.

Verification reuses the mechanism the fact store already trusts: hash the referenced
line's text, compare later. That gives per-node invalidation, which is strictly better
than what `graph_index.is_stale()` can do — a whole-graph mtime verdict condemns every
node in the graph the moment one file is touched, so the honest answer ("these nine nodes
moved, the other twelve hundred are fine") was not expressible.
"""

import json
import subprocess
from pathlib import Path

from exoma.core.graph import graph_index
from exoma.core.evidence.fact_store import _anchor_hash, _hash_index
from exoma.core.workspace.workspace_paths import atomic_write_json, workflow_paths
from exoma.utils import osutil

GRAPH_META_FILENAME = "graph-meta.json"
META_VERSION = 1

# Provenance fields a node can carry once the sidecar is built. `extractor` is absent on
# purpose and stays absent: only graphify knows which rule produced a node, and inventing
# a value here would be provenance that provenance cannot check.
PROVENANCE_FIELDS = ("source_file", "source_location", "commit_sha", "anchor_hash")


def meta_path(project_root) -> Path:
    return workflow_paths(Path(project_root))["graph_meta"]


def head_commit(project_root) -> str | None:
    """The commit the working tree is on, or None outside a repo.

    Best-effort and short-fused. This is metadata about an index; a slow or missing git
    must degrade the answer, never delay the call that wanted it.
    """
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
            # No console flash if this is ever reached from a process without a console.
            # It is called from the parent today, where the flags change nothing — the
            # cost of carrying them is one line, the cost of the class of bug is a window
            # blinking on the user's screen from a path nobody re-audits.
            **osutil.hidden_run_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    sha = (done.stdout or "").strip()
    return sha or None


def _node_line(node: dict) -> int | None:
    """graph.json writes the line as `L<n>`; return it as a number."""
    raw = str(node.get("source_location") or "").strip()
    digits = raw[1:] if raw[:1].lower() == "l" else raw
    try:
        value = int(digits)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _relative(project_root, source_file: str | None) -> str | None:
    return graph_index._relative_source(source_file, project_root)


def build(project_root) -> dict:
    """Write the sidecar for the current graph. Returns a summary of what it recorded.

    Anchored per node, so the cost is one read per distinct source file rather than one
    per node — the hash index the fact store already builds is reused for exactly that.
    """
    graph = graph_index.load_graph(project_root)
    if not graph:
        return {"ok": False, "reason": "no graph.json to index"}

    root = Path(project_root)
    commit = head_commit(root)
    cache: dict = {}
    nodes: dict[str, dict] = {}
    anchored = 0
    for node in graph_index._nodes(graph):
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        rel = _relative(root, node.get("source_file"))
        line = _node_line(node)
        digest = _anchor_hash(root, rel, line) if rel and line else None
        if digest:
            anchored += 1
            _hash_index(root, rel, cache)
        nodes[node_id] = {
            "source_file": rel,
            "source_location": line,
            "commit_sha": commit,
            "anchor_hash": digest,
        }

    path = graph_path_state(root, graph)
    payload = {
        "meta_version": META_VERSION,
        "commit_sha": commit,
        "graph_mtime_ns": path,
        "node_count": len(nodes),
        # Nodes whose line could actually be hashed. The gap between this and node_count
        # is the part of the graph that can never be verified — rationale nodes with no
        # source, files that have since been deleted — and it is reported rather than
        # rounded away, because a verification rate is meaningless without it.
        "anchored_count": anchored,
        "nodes": nodes,
    }
    atomic_write_json(meta_path(root), payload)
    return {
        "ok": True,
        "path": str(meta_path(root)),
        "node_count": len(nodes),
        "anchored_count": anchored,
        "commit_sha": commit,
    }


def graph_path_state(project_root, graph: dict | None = None) -> int | None:
    """The graph file's mtime, so a sidecar can tell which graph it describes."""
    try:
        return graph_index.graph_path(project_root).stat().st_mtime_ns
    except OSError:
        return None


def load(project_root) -> dict | None:
    """The sidecar as written, or None when absent/unreadable."""
    try:
        data = json.loads(meta_path(project_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def verify(project_root) -> dict:
    """Which recorded nodes still point at the text they were recorded against.

    This is the per-node answer `is_stale()` cannot give. A node is `moved` when its hash
    is found elsewhere in the same file — the source is intact and only the index is wrong,
    exactly the distinction the fact store draws for anchors — and `drifted` when the hash
    is gone from the file entirely.

    `commit_changed` is reported separately from any of that. A different HEAD does not by
    itself invalidate a single node, and treating it as invalidation would throw away a
    whole index every time a branch is switched.
    """
    sidecar = load(project_root)
    if not sidecar:
        return {"ok": False, "reason": "no graph-meta.json; run graph_meta.build first"}

    root = Path(project_root)
    cache: dict = {}
    fresh: list[str] = []
    moved: list[dict] = []
    drifted: list[str] = []
    unverifiable: list[str] = []

    for node_id, entry in (sidecar.get("nodes") or {}).items():
        digest = entry.get("anchor_hash")
        rel = entry.get("source_file")
        line = entry.get("source_location")
        if not digest or not rel:
            unverifiable.append(node_id)
            continue
        if _anchor_hash(root, rel, line) == digest:
            fresh.append(node_id)
            continue
        matches = _hash_index(root, rel, cache).get(digest) or []
        if len(matches) == 1:
            moved.append({"node": node_id, "from": line, "to": matches[0]})
        else:
            drifted.append(node_id)

    recorded_commit = sidecar.get("commit_sha")
    current_commit = head_commit(root)
    verifiable = len(fresh) + len(moved) + len(drifted)
    return {
        "ok": True,
        "fresh": len(fresh),
        "moved": moved,
        "drifted": drifted,
        "unverifiable": len(unverifiable),
        "verifiable": verifiable,
        # The share of CHECKABLE nodes still standing. Denominator excludes unverifiable
        # nodes rather than counting them as failures: a rationale node with no source line
        # was never going to be checkable, and scoring it as drift would make a healthy
        # graph look rotten.
        "verified_rate": round((len(fresh) + len(moved)) / verifiable, 3) if verifiable else None,
        "commit_recorded": recorded_commit,
        "commit_current": current_commit,
        "commit_changed": bool(
            recorded_commit and current_commit and recorded_commit != current_commit
        ),
        "graph_changed": sidecar.get("graph_mtime_ns") != graph_path_state(root),
    }


def provenance(project_root, node_ids) -> list[dict]:
    """Recorded provenance for specific nodes, verified against the file as it is now."""
    sidecar = load(project_root) or {}
    recorded = sidecar.get("nodes") or {}
    root = Path(project_root)
    out: list[dict] = []
    for node_id in node_ids:
        entry = recorded.get(str(node_id))
        if not entry:
            out.append({"node": str(node_id), "known": False})
            continue
        digest = entry.get("anchor_hash")
        still = (
            _anchor_hash(root, entry.get("source_file"), entry.get("source_location"))
            == digest
            if digest
            else None
        )
        out.append(
            {
                "node": str(node_id),
                "known": True,
                **{field: entry.get(field) for field in PROVENANCE_FIELDS},
                "verified": still,
            }
        )
    return out
