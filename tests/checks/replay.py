"""EXP-013's replay runner: both arms as specified, the store untouched, the future kept out."""

import json
import shutil
import tempfile
from pathlib import Path

from exoma.core.evidence import fact_store
from exoma.core.workspace.workspace_paths import workflow_paths
from tests.checks.support import assert_true


def _archive(root: Path, session: str, prompt_id: str, role: str, task: str, answer: str,
             recorded_at: str, served=None) -> None:
    folder = workflow_paths(root, session)["logs_dir"] / prompt_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "prompt.md").write_text(f"[WORKFLOW_AGENT]\nrole: {role}\n\n[TASK]\n{task}\n\n[OUTPUT_FORMAT]\nx\n", encoding="utf-8")
    (folder / "output.raw.md").write_text(answer, encoding="utf-8")
    meta = {"role": role, "recorded_at": recorded_at}
    if served is not None:
        meta["served_facts"] = served
    (folder / "call.meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _test_fact_replay() -> None:
    from tools.maintain import replay_facts as replay

    assert_true(
        replay.citations("see [a/b.py:12-20] and E:\\x\\c.py:5")
        == [("a/b.py", 12, 20), ("e:/x/c.py", 5, 5)],
        "a range and a Windows path are both read as citations",
    )
    assert_true(
        replay.is_cited({"file": "a/b.py", "line": 15}, replay.citations("[a/b.py:12-20]"))
        and not replay.is_cited({"file": "a/b.py", "line": 21}, replay.citations("[a/b.py:12-20]"))
        and replay.is_cited({"file": "core/x.py", "line": 3}, replay.citations("[E:/proj/core/x.py:3]")),
        "a fact is used when its line sits inside a cited range, under any path prefix",
    )
    assert_true(
        replay.task_text("[TASK]\nfind `RETRY_LIMIT`\n\n[OUTPUT_FORMAT]\n") == "find `RETRY_LIMIT`",
        "the task is the [TASK] block and nothing after it",
    )

    root = Path(tempfile.mkdtemp(prefix="aw-replay-"))
    try:
        (root / "store.py").write_text("RETRY_LIMIT = 3\nTIMEOUT = 9\nother = 1\n", encoding="utf-8")
        fact_store.ingest(root, "durable_facts:\n- [config] `RETRY_LIMIT` is three [store.py:1]\n", "s0")
        fact_store.ingest(root, "durable_facts:\n- [config] `TIMEOUT` is nine for retry [store.py:2]\n", "s0")
        store = fact_store._facts_path(root)
        stored = fact_store._load_facts(root)
        before_bytes = store.read_bytes()
        usage = store.with_name(fact_store.USAGE_FILENAME)

        facts = replay.placeable_facts(root)
        # One shared word (`retry`) is enough for the one-token arm, below the weighted floor.
        task = "why is `RETRY_LIMIT` three on retry"
        live = fact_store.load_relevant(root, task, role="reasoning")
        assert_true(
            [f["claim"] for f in replay.weighted(root, task, "reasoning", facts)]
            == [f["claim"] for f in live],
            "the weighted arm serves what load_relevant serves on the same store",
        )
        assert_true(
            len(replay.weighted(root, "`RETRY_LIMIT` `TIMEOUT` retry", "reasoning", facts)) == 2
            and len(replay.weighted(root, "`RETRY_LIMIT` `TIMEOUT` retry", "reasoning", facts, limit=1)) == 1,
            "--weighted-limit caps the weighted arm whatever the role's own limit",
        )
        usage.unlink(missing_ok=True)
        assert_true(
            len(replay.one_token("retry three", facts)) == 2,
            "the one-token arm serves any fact sharing one word",
        )

        late = max(f["captured_at"] for f in stored)
        early = "2000-01-01T00:00:00+00:00"
        _archive(root, "s1", "p1", "reasoning", task, "it is [store.py:1]", late + "z")
        _archive(root, "s1", "p2", "exploration", task, "cites nothing", early)
        _archive(root, "s1", "p3", "verification", task, "[store.py:1]", late + "z")
        _archive(root, "s1", "p4", "reasoning", task, "[store.py:1]", late + "z",
                 served=[{"key": "k", "file": "store.py", "line": 1}])
        arms = {"one_token": replay.Arm(), "weighted": replay.Arm(), "recorded": replay.Arm()}
        from collections import Counter

        skipped: Counter = Counter()
        count = replay.replay_project(root, arms, skipped, [])
        report = {name: arm.report() for name, arm in arms.items()}
        assert_true(
            count == 3,
            f"exploration and reasoning calls replay, verification does not: {count}",
        )
        assert_true(
            report["weighted"]["candidates"] == 4 and report["weighted"]["served"] == 2
            and report["weighted"]["used"] == 2,
            f"a call recorded before every fact was learned is offered none: {report['weighted']}",
        )
        assert_true(
            report["one_token"]["served"] == 4 and report["one_token"]["used"] == 2
            and report["one_token"]["served_fact_use_rate"] == 0.5,
            f"the broader arm serves more and is cited less: {report['one_token']}",
        )
        assert_true(
            report["recorded"]["calls"] == 1 and report["recorded"]["used"] == 1,
            f"a call that archived its served facts is also scored exactly: {report['recorded']}",
        )
        assert_true(
            store.read_bytes() == before_bytes and not usage.exists(),
            "the replay never writes the store or its usage sidecar",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
