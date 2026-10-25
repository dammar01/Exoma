"""Per-node graph verification, on a graph built by hand.

A real `graphify-out/graph.json` is not available to a test — graphify is an external CLI
— so the fixture is a minimal graph in the shape the real one uses: absolute
`source_file`, `source_location` as `L<n>`, `community`, and edges tagged with graphify's
own confidence vocabulary.

What matters here is the distinction whole-graph staleness could never draw. Editing one
file must not condemn the nodes in every other file, and a line that MOVED must not be
reported as a line that changed — the first is an index that needs updating, the second is
a claim that may have stopped being true.
"""

import json
import shutil
import tempfile
from pathlib import Path

from exoma.core.graph import graph_index, graph_meta
from tests.checks.support import assert_true


def _build_fixture(root: Path) -> None:
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "alpha.py").write_text(
        "def alpha():\n    return 'alpha value'\n", encoding="utf-8"
    )
    (root / "pkg" / "beta.py").write_text(
        "def beta():\n    return 'beta value'\n", encoding="utf-8"
    )
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {
                "id": "n_alpha",
                "label": "alpha",
                "file_type": "code",
                "source_file": str((root / "pkg" / "alpha.py").resolve()),
                "source_location": "L1",
                "community": 0,
            },
            {
                "id": "n_beta",
                "label": "beta",
                "file_type": "code",
                "source_file": str((root / "pkg" / "beta.py").resolve()),
                "source_location": "L1",
                "community": 0,
            },
            {
                "id": "n_rationale",
                "label": "why alpha exists",
                "file_type": "rationale",
                "community": 1,
            },
        ],
        "edges": [
            {
                "source": "n_alpha",
                "target": "n_beta",
                "relation": "calls",
                "confidence": "EXTRACTED",
                "confidence_score": 0.9,
            },
            {
                "source": "n_beta",
                "target": "n_rationale",
                "relation": "explains",
                "confidence": "AMBIGUOUS",
                "confidence_score": 0.2,
            },
        ],
        "hyperedges": [],
    }
    out = root / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")


def _provenance_and_verification() -> None:
    root = Path(tempfile.mkdtemp(prefix="aw-graphmeta-")).resolve()
    try:
        _build_fixture(root)
        built = graph_meta.build(root)
        assert_true(
            built["ok"] and built["node_count"] == 3 and built["anchored_count"] == 2,
            "two code nodes are anchorable and the rationale node is not; reporting the gap "
            f"is what makes a verification rate readable. got {built}",
        )

        clean = graph_meta.verify(root)
        assert_true(
            clean["fresh"] == 2 and clean["verified_rate"] == 1.0,
            f"an untouched tree must verify clean; got {clean}",
        )
        assert_true(
            clean["unverifiable"] == 1 and clean["verifiable"] == 2,
            "the rationale node has no source line and was never checkable — scoring it as "
            "drift would make a healthy graph look rotten",
        )

        # A line MOVES: insert above it, leave the text alone.
        (root / "pkg" / "alpha.py").write_text(
            "# a new comment\ndef alpha():\n    return 'alpha value'\n", encoding="utf-8"
        )
        moved = graph_meta.verify(root)
        assert_true(
            [row["node"] for row in moved["moved"]] == ["n_alpha"]
            and moved["moved"][0]["to"] == 2,
            f"a line that moved is an index to update, not a claim to drop; got {moved}",
        )
        assert_true(
            moved["fresh"] == 1 and not moved["drifted"],
            "editing one file must not condemn the nodes in every other file — this is the "
            "answer whole-graph staleness cannot give",
        )

        # A line CHANGES: same position, different text.
        (root / "pkg" / "beta.py").write_text(
            "def beta_renamed():\n    return 'beta value'\n", encoding="utf-8"
        )
        drifted = graph_meta.verify(root)
        assert_true(
            drifted["drifted"] == ["n_beta"],
            f"changed text is drift, and must be reported apart from a move; got {drifted}",
        )

        rows = graph_meta.provenance(root, ["n_alpha", "n_missing"])
        assert_true(
            rows[0]["known"] and rows[0]["source_file"] == "pkg/alpha.py",
            "provenance must come back repo-relative, matching how file:line is written "
            "everywhere else",
        )
        assert_true(
            rows[0]["verified"] is False and rows[1]["known"] is False,
            "a node whose line moved is not verified at its recorded position, and an "
            "unknown node must say it is unknown rather than defaulting to fine",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _subgraph_and_confidence() -> None:
    root = Path(tempfile.mkdtemp(prefix="aw-subgraph-")).resolve()
    try:
        _build_fixture(root)
        slice_ = graph_index.subgraph(root, "alpha", hops=1)
        assert_true(
            slice_ is not None and "pkg/alpha.py" in slice_["seed_files"],
            f"the slice must start from the files the hint actually ranks; got {slice_}",
        )
        assert_true(
            any(row["file"] == "pkg/beta.py" for row in slice_["files"]),
            "one hop from alpha must reach beta across the EXTRACTED edge",
        )
        assert_true(
            slice_["truncated"] is False and slice_["node_budget"] > 0,
            "a slice must say whether it stopped for want of graph or want of budget — a "
            "truncated neighbourhood shown as a complete one is a silent cap",
        )

        found = graph_index.leads(root, "alpha")
        assert_true(
            found["edge_confidence"].get("extracted") == 1
            and found["edge_confidence"].get("ambiguous") == 1,
            "the confidence mix has always driven ranking; surfacing it is what lets a "
            f"reader discount an inferred shortlist. got {found.get('edge_confidence')}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_graph_verification() -> None:
    _provenance_and_verification()
    _subgraph_and_confidence()
