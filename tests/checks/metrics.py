"""The calculation contract: registry, its documentation, and the provenance stamp."""

import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from exoma.core.audit import metrics, telemetry
from tests.checks.support import assert_true

DOC = Path(__file__).resolve().parents[2] / "docs" / "evaluation" / "metrics.md"
_REQUIRED = {"version", "producer", "unit", "statistic", "definition", "denominator", "missing"}


def _test_metrics_contract() -> None:
    # --- every metric is complete, named apart, and documented at its version ------------
    for mid, metric in metrics.REGISTRY.items():
        assert_true(re.fullmatch(r"(runtime|real_use|outcome|replay)\.[a-z_]+", mid), f"metric id shape: {mid}")
        assert_true(_REQUIRED <= set(metric), f"{mid} misses {_REQUIRED - set(metric)}")
        assert_true(metric["statistic"] in metrics.STATISTICS, f"{mid}: unknown statistic {metric['statistic']}")
    doc = DOC.read_text(encoding="utf-8")
    missing = [f"{mid}@{m['version']}" for mid, m in metrics.REGISTRY.items() if f"`{mid}@{m['version']}`" not in doc]
    assert_true(not missing, f"docs/evaluation/metrics.md is behind the registry: {missing}")
    assert_true(f"Registry version: **{metrics.METRICS_VERSION}**" in doc, "the page names the registry version")

    # --- the input hash depends on contents only, and on every byte of them --------------
    root = Path(tempfile.mkdtemp(prefix="metrics-"))
    try:
        a, b = root / "a.jsonl", root / "b.jsonl"
        a.write_text("one\n", encoding="utf-8")
        b.write_text("two\n", encoding="utf-8")
        first = metrics.inputs_digest([a, b])
        assert_true(first == metrics.inputs_digest([b, a]), "input order does not change the hash")
        assert_true(str(root) not in str(first), "the stamp carries no path")
        b.write_text("two!\n", encoding="utf-8")
        assert_true(metrics.inputs_digest([a, b])["sha256"] != first["sha256"], "a changed input changes the hash")

        # --- the runtime report stamps itself --------------------------------------------
        (root / ".workflow" / "data").mkdir(parents=True)
        stamp = telemetry.report(root)["provenance"]
        assert_true(
            stamp["metrics_version"] == metrics.METRICS_VERSION
            and set(stamp["metrics"]) == {m for m in metrics.REGISTRY if m.startswith("runtime.")}
            and "machine holding the inputs only" in stamp["recount"],
            f"report() carries its provenance: {stamp}",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- the offline outcome rule: defaults as documented, overridable, stamped ----------
    from tools.maintain import measure_task_outcomes as outcomes

    assert_true(
        (outcomes.SOLVED_SHARE, outcomes.PARTIAL_SHARE, outcomes.FIX_WINDOW.total_seconds())
        == (0.8, 0.3, 86400.0),
        "the outcome rule's defaults are the ones metrics.md documents",
    )
    assert_true(
        [outcomes.grade(s, True, f) for s, f in ((0.8, False), (0.8, True), (0.79, False), (0.3, False), (0.29, False))]
        == ["solved_clean", "solved_fixed", "partial", "partial", "not_in_main"]
        and outcomes.grade(1.0, False, False) == "no_change",
        "grade boundaries: 0.8 solved, 0.3 partial, no lines left is no_change",
    )
    saved = (outcomes.SOLVED_SHARE, outcomes.PARTIAL_SHARE, outcomes.FIX_WINDOW, outcomes.SEARCH_WINDOW,
             outcomes.IDLE_SECONDS, outcomes.AUTHOR_BEFORE, outcomes.AUTHOR_AFTER)
    try:
        outcomes._apply_rule(SimpleNamespace(
            solved_share=0.9, partial_share=0.5, fix_hours=48, search_days=7, idle_minutes=10,
            author_before_minutes=2, author_after_minutes=3,
        ))
        assert_true(
            outcomes.grade(0.85, True, False) == "partial" and outcomes.IDLE_SECONDS == 600,
            "a rule set on the command line is the rule the run applies",
        )
    finally:
        (outcomes.SOLVED_SHARE, outcomes.PARTIAL_SHARE, outcomes.FIX_WINDOW, outcomes.SEARCH_WINDOW,
         outcomes.IDLE_SECONDS, outcomes.AUTHOR_BEFORE, outcomes.AUTHOR_AFTER) = saved

    # --- the two percentile conventions stay what the registry says ---------------------
    from tools.maintain.measure_real_use import dist

    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert_true(dist(values)["median"] == 5 and dist(values)["p90"] == 9, "nearest_rank: round(p * (n - 1)), half to even (round(4.5) == 4)")
    assert_true(outcomes.p90(values) == 9 and outcomes.med(values) == 5.5, "p90_floor and median_stdlib")

    # --- the implementation computes what the registry defines ---------------------------
    # outcome.context_saved: fresh input (input - cached) minus returned (chars // 4), floored
    # at 0; reasoning is reported apart and never counted.
    net = outcomes.saved_net([
        {"actual_input_tokens": 1000, "actual_cached_input_tokens": 300, "actual_reasoning_tokens": 500,
         "response_chars": 400},
        {"actual_input_tokens": 200, "actual_cached_input_tokens": 0, "response_chars": 40},
    ])
    assert_true(
        (net["fresh"], net["returned"], net["reasoning"], net["net"]) == (900, 110, 500, 790),
        f"outcome.context_saved is fresh minus returned, reasoning apart: {net}",
    )
    assert_true("minus what returned" in metrics.REGISTRY["outcome.context_saved"]["definition"]
                and "plus reasoning" not in metrics.REGISTRY["outcome.context_saved"]["definition"],
                "the registry states the formula the tool runs")
    assert_true(outcomes.saved_net([{"actual_input_tokens": 10, "response_chars": 4000}])["net"] == 0,
                "the net is floored at 0")

    # --- a usage row belongs to one transcript: the earliest that binds its session -------
    owners = metrics.session_owners([
        {"start": "2026-10-02T00:00:00", "sessions": {"main_a"}},
        {"start": "2026-10-01T00:00:00", "sessions": {"main_a", "main_b"}},
        {"start": None, "sessions": {"main_c", "main_a"}},
    ])
    assert_true(owners == {"main_a": 1, "main_b": 1, "main_c": 2},
                f"a resumed copy binding the same id does not own its rows: {owners}")

    # --- an export carries only identifier-shaped values, and no project name ------------
    root = Path(tempfile.mkdtemp(prefix="metrics-export-"))
    try:
        dest = root / "out.json"
        metrics.write_export({"projects": 2, "per_project": {"PA": {"sessions": 3}}}, dest, forbidden=["shop-api"])
        assert_true('"schema": 1' in dest.read_text(encoding="utf-8"), "an export is written with its schema")
        # A real stamp (producer, params, input hash, dates, recount text) passes: the fixed
        # texts provenance() writes are accepted verbatim where it writes them.
        stamp = metrics.provenance("tools/maintain/measure_real_use.py", {"version": "3.8.0", "idle_minutes": 15.0},
                                   [dest], metric_ids=["real_use.session"])
        metrics.write_export({"provenance": stamp, "window": ["2026-09-01", "2026-10-04"],
                              "by_project": {"Project AB": {"tasks": 1}}, "error_types": {None: 2},
                              "buckets": {"1-2": 1, "6+": 0}, "commit": "0123abc+dirty",
                              "at": "2026-10-04T10:00:00+00:00"},
                             root / "stamped.json", forbidden=["shop-api"])
        # Project names are whole words, case-insensitive, over values only: a projects folder
        # named `data` or `main` no longer refuses a stamp whose producer path holds `maintain`
        # or whose keys hold `main_heads_sha256`.
        metrics.write_export({"provenance": stamp, "params": {"main_heads_sha256": "ab" * 32}, "note": "maintain-x"},
                             root / "names.json", forbidden=["data", "main"])
        bad_cases = (
            ({"note": "shop-api"}, "a project name"), ({"note": "SHOP-API-v2"}, "a project name in other case, as words"),
            ({"note": "main"}, "a project named main"),
            ({"note": r"E:\Work\x"}, "a Windows path"), ({"note": "/home/u/p"}, "a POSIX path"),
            ({"note": r"\\server\share\project"}, "a UNC path"), ({"note": "//server/share/project"}, "a forward-slash UNC path"),
            ({"note": "~/work/project"}, "a home-relative path"), ({"note": "see /etc/hosts"}, "a path inside free text"),
            ({"note": "/tmp"}, "a single-segment POSIX path"), ({"note": "//host"}, "a host-only UNC path"),
            ({"note": "cwd:/home/alice/proj"}, "a path behind a label"), ({"note": "[/home/alice]"}, "a bracketed path"),
            ({"note": "file:///home/a/x"}, "a file URL"), ({"note": "~alice/proj"}, "another user's home"),
            ({"note": "$HOME/x"}, "a variable path"), ({"note": r"C:\Users\x"}, "a drive path"),
            ({"note": r"abcC:\Users"}, "a drive path glued to a word"), ({"note": r"\\server\share"}, "a two-part UNC path"),
            ({"note": "a@b.com"}, "an email address"), ({"note": "3/4"}, "a slash in a value"),
            ({"note": "fix the login bug"}, "free text"), ({"note": "x" * 65}, "an over-long value"),
            ({"note": {"deep": ["ok", r"C:\x"]}}, "a nested path"), ({r"C:\Users\x": 1}, "a path in a key"),
            ({"provenance": {**stamp, "producer": "/home/alice/tool.py"}}, "a producer that is not a registry producer"),
            ({"provenance": {**stamp, "recount": "see /home/alice"}}, "a recount text that is not RECOUNT"),
            ({"note": b"bytes"}, "a value that is not JSON data"),
        )
        for bad, why in bad_cases:
            try:
                metrics.write_export(bad, root / "bad.json", forbidden=["shop-api", "main"])
            except ValueError as exc:
                assert_true("alice" not in str(exc) and "server" not in str(exc) and "Users" not in str(exc),
                            f"the refusal names the JSON path, not the value: {exc}")
            else:
                raise AssertionError(f"an export carrying {why} is refused")
        try:
            metrics.write_export({"a": {"b": ["ok", "cwd:/home/alice"]}}, root / "bad.json")
        except ValueError as exc:
            assert_true("$.a.b[1]" in str(exc), f"the refusal names the offending JSON path: {exc}")
        else:
            raise AssertionError("a nested path is refused")
        try:
            metrics.write_export({"a": "shop-api"}, root / "bad.json", forbidden=["x-y", "shop-api"])
        except ValueError as exc:
            assert_true("$.a" in str(exc) and "#2" in str(exc) and "shop" not in str(exc),
                        f"the refusal names which forbidden name collided, by position: {exc}")
        else:
            raise AssertionError("a project name is refused")
        assert_true(not (root / "bad.json").exists(), "a refused export writes nothing")

        # A short project name is checked like a long one, still as whole words: `ui` refuses
        # a value holding the word `ui`, not one holding `build` or `quick`.
        for short, value in (("ui", "ui"), ("ab", "x-AB-y"), ("a", "a")):
            try:
                metrics.write_export({"note": value}, root / "bad.json", forbidden=[short])
            except ValueError as exc:
                assert_true("#1" in str(exc), f"a short project name is refused by position: {exc}")
            else:
                raise AssertionError(f"a project named {short!r} is refused like any other")
        metrics.write_export({"note": "build-quick", "per_project": {"PA": {"sessions": 1}}},
                             root / "short.json", forbidden=["ui", "a"])
        # Under a per-project map every key is a label the tools write; a folder name there is
        # refused by its shape, at any length, and labels pass even when a project shares
        # a letter with them.
        for key in ("ui", "shop-api", "acme", "Project ab", "PA1"):
            for field in ("per_project", "by_project"):
                try:
                    metrics.write_export({field: {key: {"sessions": 1}}}, root / "bad.json", forbidden=[])
                except ValueError as exc:
                    assert_true(f"$.{field}" in str(exc) and key not in str(exc),
                                f"the refusal names the map, not the key: {exc}")
                else:
                    raise AssertionError(f"a {field} key {key!r} that is not a label is refused")
        metrics.write_export({"per_project": {"PA": {"sessions": 1}, "PAB": {"sessions": 2}},
                              "by_project": {"Project A": {"tasks": 1}}},
                             root / "labels.json", forbidden=["a", "ab"])
        assert_true(not (root / "bad.json").exists(), "a refused export writes nothing")

        # Real exports from both tools pass, from projects folders named `data` and `main`.
        _check_tool_exports(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- project labels go on past 26: A..Z, AA, AB, ... ----------------------------------
    assert_true([metrics.project_letters(i) for i in (0, 25, 26, 27, 51, 52, 701, 702)]
                == ["A", "Z", "AA", "AB", "AZ", "BA", "ZZ", "AAA"], "labels past 26 stay letters")

    # --- the input hash fails open on an unreadable input ---------------------------------
    root = Path(tempfile.mkdtemp(prefix="metrics-unreadable-"))
    try:
        a, b = root / "a.jsonl", root / "b.jsonl"
        a.write_text("one\n", encoding="utf-8")
        b.write_text("two\n", encoding="utf-8")
        real = Path.read_bytes

        def refuse_b(self):
            if self.name == "b.jsonl":
                raise PermissionError("locked")
            return real(self)

        with mock.patch.object(Path, "read_bytes", refuse_b):
            partial = metrics.inputs_digest([a, b])
        assert_true(partial["files"] == 1 and partial["unreadable"] == 1
                    and partial["sha256"] == metrics.inputs_digest([a])["sha256"],
                    f"an unreadable input is skipped and marked, not raised: {partial}")
        assert_true("unreadable" not in metrics.inputs_digest([a, b]), "a fully read set carries no unreadable mark")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # --- the graph-hook tool names no absolute path -----------------------------------------
    from tools.maintain import measure_graph_hook as graph_hook

    assert_true(graph_hook.hook_label(Path.home() / ".claude" / "hooks" / "graph-refresh.sh") == "~/.claude/hooks/graph-refresh.sh"
                and graph_hook.hook_label(graph_hook.REPO_ROOT / "dist" / "h.sh") == "dist/h.sh",
                "the measured hook is named by its repository path or under ~, never with the user's home")
    project = Path(tempfile.gettempdir()) / "some-project"
    line = graph_hook.sanitize_line(f"wrote {project / 'graphify-out' / 'graph.json'} and /home/alice/x in 2s", project)
    assert_true("alice" not in line and str(project) not in line and "in 2s" in line,
                f"graphify's last line keeps its summary, not its paths: {line}")
    _graph_hook_scan(graph_hook)

    _feature_segments()

    # --- the tool's commit comes only from the repository whose top is the install root ---
    repo = Path(__file__).resolve().parents[2]
    if shutil.which("git") and (repo / ".git").exists():
        assert_true(metrics.tool_commit(repo / "src") is None,
                    "a root inside some other work tree stamps no commit")
        assert_true(metrics.tool_commit(repo) is not None, "the repository's own top stamps its commit")
        # A status that fails is not a clean tree: no stamp, rather than a hash claiming one.
        real_run = subprocess.run

        def failing_status(argv, *a, **kw):
            if "status" in argv:
                return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal")
            return real_run(argv, *a, **kw)

        with mock.patch.object(metrics.subprocess, "run", failing_status):
            assert_true(metrics.tool_commit(repo) is None, "a failed git status stamps no commit")


def _hook_literal(source: str, name: str):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in graph-refresh.py")


def _graph_hook_scan(graph_hook) -> None:
    # The tool times the hook's scan, so its lists are the hook's lists.
    hook = (graph_hook.REPO_ROOT / "src" / "exoma" / "_bundle" / "config" / "claude" / "hooks" / "graph-refresh.py").read_text(encoding="utf-8")
    assert_true(set(_hook_literal(hook, "SKIP_DIRS")) == set(graph_hook.SKIP_DIRS)
                and tuple(_hook_literal(hook, "SOURCE_EXT")) == graph_hook.SOURCE_EXTENSIONS,
                "measure_graph_hook skips and matches exactly what graph-refresh.py does")
    tool = Path(graph_hook.__file__).read_text(encoding="utf-8")
    assert_true("Get-ChildItem" not in tool and "-NoProfile" not in tool and not hasattr(graph_hook, "_powershell"),
                "the tool measures the Bash/Python hook, not the retired PowerShell one")

    root = Path(tempfile.mkdtemp(prefix="graph-hook-"))
    try:
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (root / "src" / "notes.txt").write_text("n\n", encoding="utf-8")
        for skipped in ("Vendor", "target", "node_modules"):
            (root / skipped).mkdir()
            (root / skipped / "dep.py").write_text("y = 2\n", encoding="utf-8")
        scan = graph_hook.hook_scan(root)
        assert_true(scan["files_visited"] == 1 and scan["dirs_skipped"] == 3,
                    f"skip directories are pruned case-insensitively, target included: {scan}")
        size = graph_hook.project_size(root)
        assert_true(size["source_files_outside_skip"] == 1 and size["files_in_skip_dirs"] == 3,
                    f"project size uses the same case-insensitive skips: {size}")

        result = graph_hook.measure(root, hook=root / "missing.sh", repeat=2, run_hook=False, run_graphify=False)
        assert_true(len(result["scan_ms"]) == 2 and result["scan_files_visited"] == 1
                    and result["scan_dirs_skipped"] == 3 and "scan_current_ms" not in result,
                    f"measure reports the hook scan, no PowerShell fields: {result}")

        hook_file = root / "graph-refresh.sh"
        hook_file.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        with mock.patch("exoma.utils.osutil.find_bash", return_value=None):
            try:
                graph_hook.measure(root, hook=hook_file, repeat=1, run_hook=True, run_graphify=False)
                refused = None
            except SystemExit as exc:
                refused = str(exc)
        assert_true(refused is not None and "no bash" in refused,
                    f"without Bash the hook is refused, never run through a guessed shell: {refused}")

        from exoma.utils.osutil import find_bash
        if find_bash():
            # A space and a non-ASCII letter in the path: Windows Git Bash and POSIX alike.
            odd = root / "hook dir é"
            odd.mkdir()
            for code in (0, 3):
                failing = odd / f"graph-refresh-{code}.sh"
                failing.write_bytes(f"#!/usr/bin/env bash\ncat >/dev/null\nexit {code}\n".encode())
                ran = graph_hook.measure(root, hook=failing, repeat=1, run_hook=True, run_graphify=False)
                assert_true(ran["hook_exit_code"] == code and isinstance(ran["hook_total_ms"], int),
                            f"the hook's exit code is recorded beside its timing: {ran}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _feature_segments() -> None:
    """outcome.feature_segment, solved_fixed_outside_feature and continuation_cost compute
    what the registry defines."""
    from datetime import datetime, timedelta, timezone

    from tools.maintain import measure_task_outcomes as outcomes

    def at(minute):
        return f"2026-10-09T10:{minute:02d}:00+00:00"

    usage = [
        {"session_id": "s", "command": "plan", "prompt_id": "p0", "correlation_id": "c1",
         "provider": "codex", "recorded_at": at(0), "actual_input_tokens": 1000},
        {"session_id": "s", "command": "verify", "prompt_id": "p1", "correlation_id": "c1",
         "provider": "codex", "recorded_at": at(10)},
        {"session_id": "s", "command": "verify", "prompt_id": "p2", "correlation_id": "c1",
         "provider": "codex", "recorded_at": at(20)},
        # A verify-browser run: only its draft stage row carries the correlation.
        {"session_id": "s", "command": "verify-browser", "prompt_id": "b1", "correlation_id": "c2",
         "provider": "codex", "recorded_at": at(30)},
        # A verify whose task event lost its prompt id (reused evidence).
        {"session_id": "s", "command": "verify", "correlation_id": "c3", "recorded_at": at(41)},
    ]
    quality = [{"kind": "e2e", "provider_prompt_ids": ["b1", "b2"]}]
    correlations, browser = outcomes.verify_correlations(usage, quality)
    assert_true(correlations["b2"] == "c2" and browser == {"b2"},
                f"a verify-browser stage without its own id takes its run's: {correlations}")

    events = [
        {"session_id": "s", "kind": "skill", "skill": "plan", "at": at(0)},
        {"session_id": "s", "kind": "edit", "path": "a.py", "at": at(5)},
        {"session_id": "s", "kind": "skill", "skill": "verify", "prompt_id": "p1", "derived": "pass", "at": at(10)},
        {"session_id": "s", "kind": "edit", "path": "b.py", "at": at(15)},
        {"session_id": "s", "kind": "skill", "skill": "verify", "prompt_id": "p2", "derived": "pass", "at": at(20)},
        {"session_id": "s", "kind": "edit", "path": "c.py", "at": at(25)},
        {"session_id": "s", "kind": "skill", "skill": "verify", "prompt_id": "b2", "derived": "pass", "at": at(30)},
        {"session_id": "s", "kind": "skill", "skill": "verify", "derived": "fail", "at": at(40)},
        {"session_id": "s", "kind": "skill", "skill": "verify", "derived": "fail", "at": at(50)},
        {"session_id": "s", "kind": "edit", "path": "d.py", "at": at(55)},
    ]
    segs = {s["correlation_id"] or "none": s for s in outcomes.segments(events, usage, correlations, browser)}
    assert_true(set(segs) == {"c1", "c2", "c3", "none"},
                f"two phases of one plan are one segment; each other id its own: {sorted(segs)}")
    assert_true(segs["c1"]["verifies"] == 2 and segs["c1"]["passes"] == 2 and segs["c1"]["files"] == {"a.py", "b.py"}
                and segs["c1"]["link"] == "direct",
                f"phases verified under the plan's id join it: {segs['c1']}")
    assert_true(segs["c2"]["link"] == "browser" and segs["c3"]["link"] == "inferred"
                and segs["none"]["link"] == "unlinked" and segs["none"]["files"] == {"d.py"},
                "a browser stage id, a nearby verify row, or nothing: each link is named; "
                f"late edits join the last segment: {segs}")

    start = datetime(2026, 10, 9, 10, tzinfo=timezone.utc)
    rows = [u for u in usage if u.get("provider")]
    cost = outcomes.ramp_up(rows, start + timedelta(minutes=5))
    assert_true(cost == {"calls": 1, "tokens": 1000}, f"ramp-up counts what was spent before the first edit: {cost}")
    assert_true(outcomes.ramp_up(rows, None) is None, "no recorded edit is no ramp-up, not zero")

    def task(project, begin, hours, files, grade="solved_clean", **extra):
        return {"project": project, "start": start + timedelta(hours=begin),
                "end": start + timedelta(hours=begin + hours), "files": set(files), "grade": grade, **extra}

    tasks = [
        task("A", 0, 2, {"x.py"}, grade="solved_fixed", landed_at=start + timedelta(hours=2), lost_files={"x.py"}),
        task("A", 5, 2, {"x.py", "y.py"}),                     # continues task 0, explains its fix
        task("A", 100, 1, {"z.py"}, grade="solved_fixed", landed_at=start + timedelta(hours=101),
             lost_files={"z.py"}),                              # fixed, nothing continues it
        task("B", 6, 1, {"x.py"}),                             # another project: never a link
        task("A", 8, 1, {"y.py", "x.py"}),                     # two candidates: ambiguous
    ]
    outcomes.link_continuations(tasks)
    outcomes.fixed_outside_feature(tasks)
    assert_true([t["continuation"] for t in tasks] == ["none", "linked", "none", "none", "ambiguous"]
                and tasks[1]["continues"] == 0 and tasks[4]["continues"] is None,
                f"one candidate links, several are ambiguous, other projects never: {[t['continuation'] for t in tasks]}")
    assert_true([t["fixed_outside_feature"] for t in tasks] == [False, False, True, False, False],
                "a fix its own continuation explains is not a fix outside the feature")


def _check_tool_exports(root: Path) -> None:
    """Run both offline tools end to end on a fixture whose project folders are named
    `data` and `main`, and export: the real report must pass the allowlist."""
    from exoma.core.audit.transcript import project_slug
    from tools.maintain import measure_real_use, measure_task_outcomes

    projects, transcripts = root / "projects", root / "transcripts"
    for name in ("data", "main", "unused"):
        (projects / name / ".workflow" / "data").mkdir(parents=True)
    for i, name in enumerate(("data", "main")):
        sid = f"main_{name}_{i}"
        tdir = transcripts / project_slug(os.path.abspath(projects / name))
        tdir.mkdir(parents=True)
        records = [
            {"type": "user", "isMeta": True, "timestamp": "2026-10-01T10:00:00Z",
             "message": {"role": "user", "content": f"Workflow Main Agent \u2014 v9.9.9 [SESSION BINDING] MAIN_SESSION_ID={sid}"}},
            {"type": "user", "timestamp": "2026-10-01T10:00:05Z", "message": {"role": "user", "content": "please fix the parser"}},
            {"type": "assistant", "timestamp": "2026-10-01T10:01:00Z",
             "message": {"id": "m1", "content": [{"type": "text", "text": "done"}],
                         "usage": {"input_tokens": 10, "output_tokens": 5}}},
            {"type": "user", "timestamp": "2026-10-01T10:02:00",  # no offset: read as UTC
             "message": {"role": "user", "content": "that is not it, still wrong"}},
        ]
        (tdir / f"s{i}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        (projects / name / ".workflow" / "data" / "usage.jsonl").write_text(json.dumps({
            "session_id": sid, "provider": "opencode", "prompt_id": f"p{i}", "command": "explore",
            "ok": False, "error_type": "empty_output", "duration_seconds": 3.5,
        }) + "\n", encoding="utf-8")
    labels = root / "labels.json"
    labels.write_text(json.dumps({"s0.jsonl": "bug", "s1.jsonl": "feature"}), encoding="utf-8")

    def run(module, argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["tool", *argv]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            code = module.main()
        return code, out.getvalue()

    common = ["--projects-root", str(projects), "--transcripts", str(transcripts), "--version", "9.9.9"]
    code, out = run(measure_real_use, [*common, "--export", str(root / "real-use.json")])
    assert_true(code == 0 and (root / "real-use.json").is_file(), f"measure_real_use exports its real report:\n{out}")
    exported = json.loads((root / "real-use.json").read_text(encoding="utf-8"))
    assert_true(exported["sessions"] == 2 and exported["projects"] == 2, f"both projects counted: {exported}")
    code, out = run(measure_task_outcomes, [*common, "--labels", str(labels), "--export", str(root / "outcomes.json")])
    assert_true(code == 0 and (root / "outcomes.json").is_file(), f"measure_task_outcomes exports its real report:\n{out}")
    text = (root / "outcomes.json").read_text(encoding="utf-8")
    assert_true(str(root) not in text and str(root).replace("\\", "/") not in text, "the export carries no fixture path")
