"""One contract source rendered per host, one runner call per host, rules held to the code.

The contract used to be Claude's text rewritten per host by regex; the Claude-only parts
(`run_in_background`, AskUserQuestion, Claude's skill path) leaked into the Codex, OpenCode
and Agy contracts, and the contract, the [INTENT HINT] and a gate refusal named three
different runner calls. These checks hold the render (config/hosts.py), the rule registry
(config/rules.py) and the hooks to one another.
"""

import importlib
import importlib.util
import re
import shutil
import sys
import tempfile
from pathlib import Path

from tests.checks.support import assert_true

REPO = Path(__file__).resolve().parents[2]
BUNDLE = REPO / "src" / "exoma" / "_bundle" / "config"
HOOKS = BUNDLE / "hosts" / "claude" / "hooks"

# Text that names a Claude Code mechanism and means nothing (or something wrong) elsewhere.
CLAUDE_ONLY = ("run_in_background", "AskUserQuestion", "~/.claude/skills", "background-task tool",
               "background task", "task ID", "intent-gate-set", "intent-gate-check", "status-guard")
_LABEL = re.compile(r"<!-- rule:(?P<id>[a-z0-9_.-]+) \| (?P<host>[a-z]+): (?P<level>[a-z]+) -->")


def _contract_keys():
    from exoma.installer import base

    return {
        "claude": base.CLAUDE_INSTRUCTIONS,
        "agy": base.AGY_CONTRACT,
        "codex": base.CODEX_CONTRACT,
        "opencode": base.OPENCODE_CONTRACT,
    }


def _render(host, only_command=False):
    from exoma.installer import base

    source = base.DIST_CONFIG / base.CLAUDE_INSTRUCTIONS
    return base._render_text(source, _contract_keys()[host], None, only_command)


def _policy(hook_dir):
    """workflow_policy loaded from `hook_dir`, as a hook loads it beside itself."""
    spec = importlib.util.spec_from_file_location(f"wp_{abs(hash(str(hook_dir)))}", hook_dir / "workflow_policy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _test_contract_renders_per_host():
    from exoma.config import hosts

    for only_command in (False, True):
        for host in hosts.HOSTS:
            text = _render(host, only_command)
            for leftover in ("{{RUNNER}}", "{{SHELL_TOOL}}", "{{DELEGATED_RUN}}", "{{ASK_USER}}", "{{SKILL_PATH}}"):
                assert_true(leftover not in text, f"{host}: {leftover} left unrendered")
            assert_true(not re.search(r"<!--\s*HOST:", text), f"{host}: a HOST section marker shipped")
            assert_true(hosts.runner_call(host) in text,
                        f"{host}: the contract names the profile's runner call {hosts.runner_call(host)!r}")
            assert_true("not returned yet)" in text and "the call killed)" in text
                        and "orthe" not in text and ",the call" not in text,
                        f"{host}: an inline HOST section keeps the space before the text after it")
            if host == "claude":
                assert_true("still registered, or the call not returned yet" in text
                            and "its background task gone, the call killed" in text,
                            "the Claude inline sections render with their spaces")
                assert_true("run_in_background: true" in text and "AskUserQuestion" in text
                            and "~/.claude/skills/<name>.md" in text and "intent-gate-set" in text,
                            "the Claude contract keeps Claude's mechanics")
            else:
                leaks = [word for word in CLAUDE_ONLY if word in text]
                assert_true(not leaks, f"{host}: Claude-only text in its contract: {leaks}")
                assert_true(hosts.PROFILES[host].skill_path in text, f"{host}: the contract names its skill path")
                assert_true(hosts.PROFILES[host].delegated_run in text, f"{host}: the contract says how to wait here")
            if host == "codex":
                assert_true("If the runner is allowed normally, omit escalation arguments" in text
                            and "approval_policy=never" in text and "Exoma cannot override host review" in text,
                            "Codex guidance respects current permissions and cannot promise approval")


def _test_host_sections_whitespace():
    """HOST sections, block and inline, render without joined words, stray blank lines or
    leftover indentation, kept or dropped."""
    from exoma.config.hosts import select_host_sections

    cases = (
        # (text, host, expected)
        ("a <!-- HOST:claude:START -->x<!-- HOST:claude:END --> b", "agy", "a b"),
        ("a <!-- HOST:claude:START -->x<!-- HOST:claude:END --> b", "claude", "a x b"),
        ("x <!-- HOST:claude:START -->y<!-- HOST:claude:END -->, z", "agy", "x, z"),
        ("(<!-- HOST:claude:START -->its task, or <!-- HOST:claude:END -->the call)", "claude", "(its task, or the call)"),
        ("(<!-- HOST:claude:START -->its task, or <!-- HOST:claude:END -->the call)", "agy", "(the call)"),
        ("one\n<!-- HOST:claude:START -->\nbody\n<!-- HOST:claude:END -->\ntwo\n", "claude", "one\nbody\ntwo\n"),
        ("one\n<!-- HOST:claude:START -->\nbody\n<!-- HOST:claude:END -->\ntwo\n", "agy", "one\ntwo\n"),
        ("one\n<!-- HOST:claude:START -->\nbody\n    <!-- HOST:claude:END -->\ntwo\n", "claude", "one\nbody\ntwo\n"),
    )
    for text, host, want in cases:
        got = select_host_sections(text, host)
        assert_true(got == want, f"{host}: {text!r} -> {got!r}, want {want!r}")


def _test_rule_registry_parity():
    from exoma.config import hosts, rules

    source = (BUNDLE / "hosts" / "claude" / "CLAUDE.md").read_text(encoding="utf-8")
    marked = set(hosts.RULE_MARKER.findall(source))
    assert_true(marked == set(rules.BY_ID),
                f"every contract rule is registered and every registered rule is in the contract: "
                f"unregistered {sorted(marked - set(rules.BY_ID))}, unmarked {sorted(set(rules.BY_ID) - marked)}")
    for rule in rules.RULES:
        assert_true(set(rule.levels) == set(hosts.HOSTS) and set(rule.levels.values()) <= set(rules.LEVELS),
                    f"{rule.id}: one known level per host: {rule.levels}")
    for only_command in (False, True):
        for host in hosts.HOSTS:
            labels = [(m.group("id"), m.group("host"), m.group("level")) for m in _LABEL.finditer(_render(host, only_command))]
            ids = [label[0] for label in labels]
            assert_true(sorted(ids) == sorted(set(ids)) and set(ids) == set(rules.BY_ID),
                        f"{host}: each rule labelled exactly once: {sorted(ids)}")
            for rule_id, label_host, level in labels:
                assert_true(label_host == host and level == rules.level_of(rule_id, host),
                            f"{host}: {rule_id} labelled {label_host}: {level}")

    # A level above `advised` is backed by the code; `advised` is not claimed where the
    # host's hooks do call the check (that would understate what the host does).
    for rule in rules.RULES:
        if rule.runtime:
            module, attribute = rule.runtime
            assert_true(hasattr(importlib.import_module(module), attribute),
                        f"{rule.id}: runtime check {module}.{attribute} exists")
        for host, level in rule.levels.items():
            calls = rule.hook_symbol and any(
                re.search(r"\b%s\(" % re.escape(rule.hook_symbol), (BUNDLE / path).read_text(encoding="utf-8"))
                for path in rules.HOOK_FILES[host])
            if level == "advised":
                assert_true(not calls, f"{rule.id} is advised on {host}, yet its hooks call {rule.hook_symbol}")
            else:
                assert_true(calls or rule.runtime,
                            f"{rule.id} is {level} on {host}: nothing backs it (hook {rule.hook_symbol}, runtime {rule.runtime})")


def _test_runner_call_single_source():
    """Contract, [INTENT HINT] and refusal name the same runner call on every host."""
    from exoma.config import hosts
    from exoma.installer import base

    root = "E:/work/proj"
    with tempfile.TemporaryDirectory(prefix="exoma-host-profile-") as tmp:
        for host in hosts.HOSTS:
            hook_dir = Path(tmp) / host
            hook_dir.mkdir()
            shutil.copyfile(HOOKS / "workflow_policy.py", hook_dir / "workflow_policy.py")
            key = f"hosts/{host}/exoma/hooks/host-profile.json" if host != "claude" else "hosts/claude/hooks/host-profile.json"
            rendered = base._render_text(HOOKS / "host-profile.json", key, None)
            (hook_dir / "host-profile.json").write_text(rendered, encoding="utf-8")
            wp = _policy(hook_dir)
            profile = wp.load_host_profile(str(hook_dir))
            assert_true(profile.get("host") == host and profile.get("runner_prefix") == hosts.runner_prefix(host),
                        f"{host}: host-profile.json is the profile: {profile}")
            call = wp.runner_command(root, profile)
            assert_true(call == hosts.runner_call(host, root), f"{host}: hook runner call {call!r}")
            hint = wp.intent_hint("plan", root, "main_x", call if host != "claude" else None)
            assert_true(f"{hosts.runner_call(host, root)} plan --session 'main_x'" in hint,
                        f"{host}: the hint names the contract's runner call: {hint}")
            prefix = hosts.runner_prefix(host)
            quoted = re.fullmatch(r"(?:&\s*)?'([^']+)'", prefix)
            if quoted:
                assert_true(wp.is_bash(quoted.group(1), profile), f"{host}: the profile's bash may start the runner")
        # The adapters build their refusals and hints from the profile, not from their own
        # bash discovery (the three copies of find_bash are gone).
        for script in (HOOKS / "agy-host.py", BUNDLE / "hosts" / "codex" / "codex-host.py",
                       BUNDLE / "hosts" / "opencode" / "opencode-host.py"):
            text = script.read_text(encoding="utf-8")
            assert_true("runner_command(" in text and "load_host_profile(" in text
                        and "def find_bash" not in text and "def _bash_path" not in text,
                        f"{script.name}: runner call and bash come from the host profile")
    for host in ("agy", "codex", "opencode"):
        assert_true(any(name == "host-profile.json" for name in hosts.HOOK_DATA_FILES)
                    and any(key.endswith(f"/hooks/{hosts.HOST_PROFILE_FILE}") and key.startswith(f"hosts/{host}/")
                            for _src, _dest, key in base._targets((host,))),
                    f"{host}: the install writes host-profile.json beside its hooks")


def _test_read_limit_from_host_keys():
    from exoma.config import hosts

    wp = _policy(HOOKS)
    agy = hosts.hook_profile("agy")
    cases = (
        ({"Path": "a.py", "StartLine": 10, "EndLine": 60}, agy, 51),
        ({"AbsolutePath": "a.py", "StartLine": "1", "EndLine": "200"}, agy, 200),
        ({"Path": "a.py", "StartLine": 5}, agy, None),
        ({"Path": "a.py"}, agy, None),
        ({"file_path": "a.py", "limit": 150}, None, 150),
        ({"file_path": "a.py", "limit": True}, None, None),
        ({"file_path": "a.py", "offset": 10}, None, None),
        # The larger span counts: a small limit beside a long range is a long read.
        ({"Path": "a.py", "limit": 5, "StartLine": 1, "EndLine": 5000}, agy, 5000),
        ({"Path": "a.py", "StartLine": 60, "EndLine": 10}, agy, None),
        ({"Path": "a.py", "EndLine": 10}, agy, None),
        # Non-ASCII digits and absurd lengths are not numbers (int("²") raises: fail-open).
        ({"file_path": "a.py", "limit": "²"}, None, None),
        ({"file_path": "a.py", "limit": "9" * 5000}, None, None),
        ({"file_path": "a.py", "limit": 50.0}, None, 50),
        ({"file_path": "a.py", "limit": 50.5}, None, None),
    )
    for args, profile, want in cases:
        got = wp.read_limit(args, profile)
        assert_true(got == want, f"read_limit({args}) = {got}, want {want}")


def _test_agy_precision_read_slice():
    """A 60-line slice of a long file passes the Agy gate as a precision read; before, Agy's
    reads carried no limit at all, so only a whole file of at most 200 lines could pass."""
    import json
    import os

    from exoma.config import hosts

    with tempfile.TemporaryDirectory(prefix="exoma-agy-slice-") as tmp:
        root = Path(tmp) / "proj"
        rt = root / ".workflow" / "data" / "sessions" / "main_x" / "runtime"
        rt.mkdir(parents=True)
        big = root / "big.py"
        big.write_text("x = 1\n" * 900, encoding="utf-8")
        wp = _policy(HOOKS)
        profile = hosts.hook_profile("agy")
        marker = {"command": "verify", "set_at": "2026-11-02T00:00:00+00:00"}
        ctx = {"root": str(root), "home": tmp, "rt_dir": str(rt), "is_bash": lambda w: False,
               "host_read": lambda p: False}
        sliced = {**ctx, "limit": wp.read_limit({"Path": str(big), "StartLine": 100, "EndLine": 159}, profile)}
        whole = {**ctx, "limit": wp.read_limit({"Path": str(big)}, profile)}
        assert_true(wp.preflight_refusal("read", str(big), "", marker, whole) == "preflight",
                    "a whole 900-line file is still gathering")
        assert_true(wp.preflight_refusal("read", str(big), "", marker, sliced) is None,
                    "a 60-line Agy slice is a precision read")
        counter = json.loads((rt / "delegated.reads").read_text(encoding="utf-8"))
        assert_true(counter.get("count") == 1 and os.path.isfile(rt / "delegated.reads"),
                    "the slice used one of the precision reads")


def _test_codex_grant():
    """The install writes the job store grant into ~/.codex/config.toml, leaves a table of
    the user's own alone, and uninstall takes only its block out."""
    from exoma.installer import base, uninstall

    block_start = base.CODEX_GRANT_MARKERS[0]
    user_text = 'model = "gpt-6"\n\n[profiles.fast]\nmodel = "mini"\n'
    merged = base.codex_grant_text(user_text)
    assert_true(merged.startswith(user_text) and block_start in merged and "[sandbox_workspace_write]" in merged
                and base.codex_grant_state(merged) == "managed", f"the grant is appended: {merged!r}")
    assert_true(base.codex_grant_text(merged) == merged, "writing the grant again changes nothing")
    stale = merged.replace(base.codex_grant_fix()["writable_root"], "C:/old/storage")
    assert_true(base.codex_grant_text(stale) == merged, "a stale grant is replaced in place")
    assert_true(base.strip_codex_grant(merged) == user_text, "uninstall restores the user's text exactly")
    own = user_text + "\n[sandbox_workspace_write]\nwritable_roots = ['D:/x']\n"
    assert_true(base.codex_grant_state(own) == "user_table" and base.codex_grant_text(own) is None,
                "a sandbox table of the user's own is never touched")
    dotted = 'sandbox_workspace_write.network_access = true\n'
    assert_true(base.codex_grant_text(dotted) is None, "a dotted key for that table counts as the user's own")
    # The user adds their own table after Exoma's block: two tables of one name are invalid
    # TOML; the block goes and theirs governs, and --check reports it until an install runs.
    both = merged + "\n[sandbox_workspace_write]\nwritable_roots = ['D:/x']\n"
    assert_true(base.codex_grant_state(both) == "conflict", "Exoma's block beside the user's table is a conflict")
    cleaned = base.codex_grant_text(both)
    assert_true(block_start not in cleaned and "writable_roots = ['D:/x']" in cleaned
                and cleaned.count("[sandbox_workspace_write]") == 1,
                f"the conflict resolves to the user's table alone: {cleaned!r}")
    half = user_text + block_start + " — managed by Exoma\n[sandbox_workspace_write]\n"
    assert_true(base.codex_grant_state(half) == "broken" and base.codex_grant_text(half) is None,
                "a START without its END is not appended to or rewritten")
    import tomllib

    parsed = tomllib.loads(merged)
    assert_true(parsed["sandbox_workspace_write"]["writable_roots"] == [base.codex_grant_fix()["writable_root"]]
                and parsed["model"] == "gpt-6", f"the result is valid TOML: {parsed}")

    with tempfile.TemporaryDirectory(prefix="exoma-codex-grant-") as tmp:
        home = Path(tmp)
        saved = base.HOME, uninstall.HOME
        base.HOME = uninstall.HOME = home
        try:
            config = home / ".codex" / "config.toml"
            config.parent.mkdir(parents=True)
            config.write_text(user_text, encoding="utf-8")
            plan = base.Plan()
            base._install_codex_grant(plan, True, home / "backup")
            assert_true(base.codex_grant_state(config.read_text(encoding="utf-8")) == "managed"
                        and any(verb == "merge" for verb, _t, _d in plan.actions), f"install merges: {plan.actions}")
            again = base.Plan()
            base._install_codex_grant(again, True, home / "backup2")
            assert_true([a[0] for a in again.actions] == ["unchanged"], f"reinstall is idempotent: {again.actions}")
            uninstall._uninstall_codex_grant(base.Plan(), True, home / "backup3")
            assert_true(config.read_text(encoding="utf-8") == user_text, "uninstall keeps the user's settings")
            config.write_text(own, encoding="utf-8")
            refused = base.Plan()
            base._install_codex_grant(refused, True, home / "backup4")
            assert_true(config.read_text(encoding="utf-8") == own
                        and any("writable_roots" in w for w in refused.warnings),
                        f"the user's table is kept and the snippet printed: {refused.warnings}")
            config.write_text(both, encoding="utf-8")
            resolved = base.Plan()
            base._install_codex_grant(resolved, True, home / "backup4b")
            after = config.read_text(encoding="utf-8")
            assert_true(block_start not in after and after.count("[sandbox_workspace_write]") == 1
                        and any("invalid TOML" in w for w in resolved.warnings),
                        f"install takes Exoma's block out of a conflict and says why: {resolved.warnings}")
            config.write_text(half, encoding="utf-8")
            broken = base.Plan()
            base._install_codex_grant(broken, True, home / "backup4c")
            assert_true(config.read_text(encoding="utf-8") == half
                        and any(verb == "refused" for verb, _t, _d in broken.actions),
                        f"unpaired grant markers: the file is left alone: {broken.actions}")
            config.unlink()
            fresh = base.Plan()
            base._install_codex_grant(fresh, True, home / "backup5")
            assert_true(config.is_file() and any(verb == "create" for verb, _t, _d in fresh.actions),
                        "no config.toml yet: the install creates one holding the grant")
            uninstall._uninstall_codex_grant(base.Plan(), True, home / "backup6")
            assert_true(not config.exists(), "a config.toml holding only the grant is removed")
        finally:
            base.HOME, uninstall.HOME = saved
