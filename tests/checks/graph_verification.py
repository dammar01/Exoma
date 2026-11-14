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
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import Mock, patch

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


def _path_flavours() -> None:
    posix_root = PurePosixPath("/work/repo")
    windows_root = PureWindowsPath(r"E:\Work\repo")
    expected = ("pkg", "alpha.py")
    cases = (
        ("pkg/alpha.py", posix_root, expected),
        (r"pkg\alpha.py", posix_root, expected),
        (r"pkg\\alpha.py", windows_root, expected),
        ("/work/repo/pkg/alpha.py", posix_root, expected),
        (r"e:\WORK\repo\pkg\alpha.py", windows_root, expected),
        (r"E:\\Work\\repo\\pkg\\alpha.py", PurePosixPath("/mnt/e/Work/repo"), expected),
        (r"\\server\share\repo\pkg\alpha.py", PureWindowsPath(r"\\server\share\repo"), expected),
        (r"E:\Work\repo\pkg\alpha.py", PurePosixPath("/custom/e/Work/repo"), None),
        (r"F:\Work\repo\pkg\alpha.py", PurePosixPath("/mnt/e/Work/repo"), None),
        (r"E:\other\repo\pkg\alpha.py", PurePosixPath("/mnt/e/Work/repo"), None),
        (r"E:\WORK\repo\pkg\alpha.py", PurePosixPath("/mnt/e/Work/repo"), None),
        (r"\\server\share\repo\pkg\alpha.py", posix_root, None),
        ("/other/repo/pkg/alpha.py", posix_root, None),
        ("/work/repo/pkg/alpha.py", windows_root, None),
    )
    for source, root, result in cases:
        assert_true(graph_index._source_relative_parts(source, root) == result,
                    f"explicit path flavour/root mapping: {source!r} under {root}")
    alias_source = r"E:\WORK\repo\pkg\alpha.py"
    mounted_root = PurePosixPath("/mnt/e/Work/repo")
    for proof in (True, False, PermissionError("identity denied"), FileNotFoundError("alias missing")):
        identity = Mock(side_effect=proof) if isinstance(proof, OSError) else Mock(return_value=proof)
        result = graph_index._source_relative_parts(alias_source, mounted_root, same_root=identity)
        assert_true(result == (expected if proof is True else None),
                    "case aliases require positive filesystem identity; denial and missing roots fail closed")
        identity.assert_called_once_with(PurePosixPath("/mnt/e/WORK/repo"))
    identity = Mock(return_value=True)
    assert_true(graph_index._source_relative_parts(r"E:\other\repo\pkg\alpha.py", mounted_root,
                                                  same_root=identity) is None and not identity.called,
                "filesystem identity cannot bypass the lexical project-root boundary")
    for source in (None, 42, "", "\0", "../pkg/alpha.py", r"pkg\..\alpha.py",
                   r"E:pkg\alpha.py", r"\pkg\alpha.py", r"\\?\E:\Work\repo\pkg\alpha.py",
                   r"\\.\E:\Work\repo\pkg\alpha.py", "file:///work/repo/pkg/alpha.py",
                   "https://example.test/alpha.py"):
        for root in (posix_root, windows_root):
            assert_true(graph_index._source_relative_parts(source, root) is None,
                        f"ambiguous, traversing or unsupported source is unmapped: {source!r}")


def _portable_sources() -> None:
    with tempfile.TemporaryDirectory(prefix="graph-portable-") as tmp:
        base = Path(tmp).resolve()
        root, foreign = base / "repo", base / "foreign"
        _build_fixture(root)
        foreign.mkdir()
        outside = foreign / "alpha.py"
        outside.write_text("def foreign_alpha(): pass\n", encoding="utf-8")
        (root / "alpha.py").write_text("def local_impostor(): pass\n", encoding="utf-8")
        with patch("os.getcwd", return_value=str(foreign)):
            for source in ("pkg/alpha.py", r"pkg\alpha.py", str(root / "pkg" / "alpha.py")):
                assert_true(graph_index._relative_source(source, root) == "pkg/alpha.py",
                            "relative and native sources resolve against the project, never cwd")
        for source in (str(outside), "../foreign/alpha.py", "pkg/missing.py", "pkg"):
            assert_true(graph_index._relative_source(source, root) is None,
                        "foreign, missing and non-file sources never fall back to a local basename")
        link = root / "pkg" / "escape.py"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            # Exercise the resolved-containment boundary even where creating a native
            # symlink is unavailable (for example Windows without symlink privileges).
            original_resolve = Path.resolve
            def escape(path, *args, **kwargs):
                return outside if path == link else original_resolve(path, *args, **kwargs)
            with patch.object(Path, "resolve", escape):
                assert_true(graph_index._relative_source("pkg/escape.py", root) is None,
                            "a resolved escape cannot become a source location")
        else:
            assert_true(graph_index._relative_source("pkg/escape.py", root) is None,
                        "a native symlink escaping the project cannot become a source location")

        graph_file = root / "graphify-out" / "graph.json"
        graph = json.loads(graph_file.read_text(encoding="utf-8"))
        graph["nodes"][0]["source_file"] = r"pkg\alpha.py"
        foreign_node = {"id": "n_foreign", "label": "alpha", "community": 0,
                        "source_file": str(outside), "source_location": "L1"}
        missing_node = {"id": "n_missing", "label": "missing", "community": 0,
                        "source_file": str(root / "pkg" / "missing.py"), "source_location": "L1"}
        graph["nodes"].insert(0, foreign_node)
        graph["nodes"].append(missing_node)
        graph["edges"].extend({"source": "n_foreign", "target": target, "confidence": "EXTRACTED"}
                              for target in ("n_alpha", "n_beta", "n_rationale"))
        graph_file.write_text(json.dumps(graph), encoding="utf-8")
        expected = {"pkg/alpha.py", "pkg/beta.py"}
        found = graph_index.leads(root, "alpha")
        assert_true(found is not None and {row["file"] for row in found["files"]} == expected,
                    "synthetic Windows-relative graph emits only real, contained POSIX sources")
        assert_true(set(graph_index.files_in_community(root, 0)) == expected
                    and set(found["communities"][0]["files"]) == expected,
                    "community locations obey the same mapping as ranked leads")
        with patch.object(graph_index, "_relative_source", wraps=graph_index._relative_source) as normalize:
            entry = graph_index.entry_points(root, limit=1)
            assert_true(normalize.call_count < len(graph["nodes"]),
                        "entry point resolution stops once enough valid sources are found")
            calls = normalize.call_count
            assert_true(graph_index.entry_points(root, limit=0) == [] and normalize.call_count == calls,
                        "a zero entry point budget performs no source resolution")
        assert_true(len(entry) == 1 and entry[0]["file"] in expected,
                    "unresolved high-degree entry points are filtered before applying the limit")
        slice_ = graph_index.subgraph(root, "alpha")
        assert_true(slice_ is not None and {row["file"] for row in slice_["files"]} == expected,
                    "subgraphs cannot reintroduce foreign or missing source locations")
        built = graph_meta.build(root)
        assert_true(built["anchored_count"] == 2
                    and graph_meta.provenance(root, ["n_foreign"])[0]["source_file"] is None,
                    "foreign basename collisions remain unanchored even when a local namesake exists")
        assert_true(graph_meta.verify(root)["unverifiable"] == 3,
                    "missing, foreign and sourceless nodes retain explicit unverifiable provenance")
        graph["nodes"], graph["edges"] = [foreign_node, missing_node], []
        graph_file.write_text(json.dumps(graph), encoding="utf-8")
        assert_true(graph_index.leads(root) is None and graph_index.entry_points(root) == []
                    and graph_index.subgraph(root) is None,
                    "an existing graph without usable source locations has a valid empty fallback")


def _test_graph_verification() -> None:
    _path_flavours()
    _portable_sources()
    _provenance_and_verification()
    _subgraph_and_confidence()
