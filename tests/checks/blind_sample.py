"""The blind-sample exporter: gate blocks found in transcripts, EXP-008's strata, the key kept apart."""

import json
import shutil
import tempfile
from pathlib import Path

from tests.checks.support import assert_true


def _line(rec: dict) -> str:
    return json.dumps(rec) + "\n"


def _test_blind_sample() -> None:
    from tools.maintain import export_blind_sample as blind

    root = Path(tempfile.mkdtemp(prefix="aw-blind-"))
    try:
        transcript = root / "t.jsonl"
        error = ('PreToolUse:Bash hook error: ["bash" "execution-gate.sh"]: [EXECUTION GATE] `git commit` '
                 "without the user asking for a commit this turn.\nA commit is approved by the user's own prompt")
        transcript.write_text(
            _line({"type": "user", "message": {"role": "user", "content": "please tidy up"}})
            + _line({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": "git commit -m x"}}]}})
            + _line({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "u1", "is_error": True, "content": error}]}})
            + _line({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "u0", "is_error": False, "content": "[EXECUTION GATE] quoted in a file"}]}})
            + _line({"type": "assistant", "message": {"content": [{"type": "text", "text": "Blocked; asking."}]}})
            + _line({"type": "user", "message": {"role": "user", "content": "yes commit it"}}),
            encoding="utf-8",
        )
        found = blind.gate_blocks(str(transcript))
        assert_true(len(found) == 1, f"only a hook error is a block, not the gate's text in a file: {found}")
        block = found[0]
        assert_true(
            block["rule"] == "commit_unapproved" and block["tool"] == "Bash"
            and "git commit" in block["blocked_input"] and block["prompt_before"] == "please tidy up"
            and block["after"] == [{"assistant": "Blocked; asking."}, {"user": "yes commit it"}],
            f"a block carries the call, the prompt before it and what followed: {block}",
        )

        tasks = ([{"project": "A", "grade": "solved_clean"}] * 10 + [{"project": "B", "grade": "solved_clean"}] * 10
                 + [{"project": f"C{i}", "grade": "solved_clean"} for i in range(8)]
                 + [{"project": "A", "grade": "solved_fixed"}] * 4 + [{"project": f"D{i}", "grade": "solved_fixed"} for i in range(6)]
                 + [{"project": "C1", "grade": "partial"}] * 3 + [{"project": "A", "grade": "not_in_main"}])
        tasks = [dict(t, n=i) for i, t in enumerate(tasks)]
        chosen = blind.sample_exp008(tasks, seed=1)
        clean = [t for t in chosen if t["grade"] == "solved_clean"]
        fixed = [t for t in chosen if t["grade"] == "solved_fixed"]
        assert_true(
            len(clean) == 12 and len(fixed) == 8
            and sum(1 for t in clean if t["project"] in ("A", "B")) <= 6
            and sum(1 for t in chosen if t["grade"] == "partial") == 3
            and not any(t["grade"] == "not_in_main" for t in chosen),
            f"12 clean and 8 fixed, the two largest projects at most half, every partial: {[t['project'] for t in clean]}",
        )
        assert_true(
            [t["n"] for t in blind.sample_exp008(tasks, seed=1)] == [t["n"] for t in chosen],
            "the same seed draws the same sample",
        )

        key: dict = {}
        items = blind._shuffle([{"x": 1, "_key": {"transcript": "a.jsonl"}}, {"x": 2, "_key": {"transcript": "b.jsonl"}}], "C", key, 7)
        assert_true(
            all("_key" not in i for i in items) and set(key) == {i["id"] for i in items},
            f"items carry an id and nothing of the key: {items} {key}",
        )
        out = root / "out"
        (out / "key").mkdir(parents=True)
        (out / "key" / "category.json").write_text(json.dumps(key), encoding="utf-8")
        labels, judged = root / "labels.json", root / "judged.json"
        labels.write_text(json.dumps({"old.jsonl": "ops"}), encoding="utf-8")
        judged.write_text(json.dumps({items[0]["id"]: "feature", items[1]["id"]: "nonsense"}), encoding="utf-8")
        import argparse

        code = blind.merge(argparse.Namespace(merge=str(judged), out=str(out), labels=str(labels)))
        merged = json.loads((out / "labels-merged.json").read_text(encoding="utf-8"))
        assert_true(
            code == 1 and merged.get(key[items[0]["id"]]["transcript"]) == "feature"
            and merged["old.jsonl"] == "ops" and len(merged) == 2,
            f"a known category merges by transcript, an unknown one is refused: {merged}",
        )
        assert_true(json.loads(labels.read_text(encoding="utf-8")) == {"old.jsonl": "ops"},
                    "the original labels file is left as it was")
    finally:
        shutil.rmtree(root, ignore_errors=True)
