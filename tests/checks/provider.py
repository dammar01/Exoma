"""The v3.4.3 provider seam: registry selection and key migration."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import check
import main
from adapters.providers.opencode_adapter import OpenCodeAdapter
from core.provider.executor import Executor
from core.jobs.job_manager import JobManager
from core.prompt.prompt_builder import build_prompt
from core.policy.secret_patterns import (
    CODEX_PERMISSION_PROFILE,
    SECRET_READ_ALLOWLIST,
    SECRET_READ_PATTERNS,
    codex_secret_globs,
)
from core.runtime.state import ensure_workflow_workspace

from tests.checks.support import (
    FakeJobProcess,
    FakeOpenCodeAdapter,
    RecordingOpenCodeAdapter,
    assert_true,
    clean_output,
    extract_session_id,
)


def _test_provider_seam() -> None:
    """The v3.4.3 seam: registry selection plus the hard-rename migration.

    Both are new load-bearing paths with no coverage anywhere else. The migration in
    particular is the only thing between a v3.4.2 workspace and silent loss of the
    user's tuning, so it is asserted on values, not just on the absence of a crash.
    """
    from adapters.contract.registry import (
        UnknownProviderError,
        available_providers,
        hint_conflict,
        register,
        resolve_adapter,
        selected_provider,
    )
    from core.workspace.provider_migration import migrate_provider_keys
    from core.workspace.workspace_paths import atomic_write_json

    assert_true(
        "opencode" in available_providers(),
        f"opencode must stay registered: {available_providers()}",
    )
    assert_true(
        resolve_adapter().adapter == "opencode",
        "default resolution must yield the opencode adapter",
    )

    raised = False
    try:
        resolve_adapter("gemini-that-does-not-exist")
    except UnknownProviderError:
        raised = True
    assert_true(
        raised,
        "an unregistered provider must raise, not silently fall back to the default — "
        "a typo that ran OpenCode anyway would look like the config took effect",
    )

    temp_root = Path(tempfile.mkdtemp(prefix="provider-migration-"))
    workflow_dir = temp_root / ".workflow"
    workflow_dir.mkdir(parents=True)
    sessions = temp_root / "sessions"
    sessions.mkdir()

    # A v3.4.2 workspace: legacy file, legacy keys, and a value the user tuned.
    atomic_write_json(
        workflow_dir / "opencode.json",
        {"opencode_command": "opencode-custom", "timeout_seconds": 999},
    )
    atomic_write_json(
        workflow_dir / "config.json",
        {"runtime": {"opencode_config": ".workflow/opencode.json"}},
    )
    atomic_write_json(
        sessions / "s1.json",
        {"session_id": "s1", "opencode_session_id": "ses_keepme"},
    )
    # The store the runtime actually reads is project-local. The first version of the
    # migration only walked the fallback dir, so a real workspace kept its legacy key,
    # every call re-bootstrapped, and bootstrap is what times out.
    local_sessions = workflow_dir / "provider-sessions"
    local_sessions.mkdir()
    atomic_write_json(
        local_sessions / "s2.json",
        {"session_id": "s2", "opencode_session_id": "ses_local"},
    )

    report = migrate_provider_keys(temp_root, sessions)

    migrated = json.loads(
        (workflow_dir / "second_agent.json").read_text(encoding="utf-8")
    )
    assert_true(
        migrated.get("provider_command") == "opencode-custom",
        f"tuned value must survive the rename, got {migrated!r}",
    )
    assert_true(
        "opencode_command" not in migrated,
        "hard rename means the legacy key is gone, not shadowed",
    )
    assert_true(
        migrated.get("timeout_seconds") == 999,
        "untouched keys must carry over verbatim",
    )
    assert_true(
        not (workflow_dir / "opencode.json").exists(),
        "the legacy file must be retired, or the next upgrade migrates it again",
    )

    config = json.loads((workflow_dir / "config.json").read_text(encoding="utf-8"))
    assert_true(
        config["runtime"].get("provider_config") == ".workflow/second_agent.json",
        f"runtime pointer must follow the rename: {config['runtime']!r}",
    )

    session = json.loads((sessions / "s1.json").read_text(encoding="utf-8"))
    assert_true(
        session.get("provider_session_id") == "ses_keepme",
        "a stored provider session must survive, or every session re-bootstraps on quota",
    )
    local = json.loads((local_sessions / "s2.json").read_text(encoding="utf-8"))
    assert_true(
        local.get("provider_session_id") == "ses_local"
        and "opencode_session_id" not in local,
        f"the PROJECT-LOCAL session store must migrate too: {local!r}",
    )

    # Second line of defence: nothing forces a user to run upgrade before their next
    # delegated call, so SessionManager repairs a legacy record on read as well.
    from core.workspace.session_manager import SessionManager

    stray = workflow_dir / "stray-sessions"
    stray.mkdir()
    atomic_write_json(
        stray / "s3.json", {"session_id": "s3", "opencode_session_id": "ses_onread"}
    )
    loaded = SessionManager(stray).load_or_create("s3")
    assert_true(
        loaded.get("provider_session_id") == "ses_onread",
        f"a legacy record must be readable without an upgrade first: {loaded!r}",
    )
    assert_true(
        "opencode_session_id"
        not in json.loads((stray / "s3.json").read_text(encoding="utf-8")),
        "the repair must be persisted, not re-done on every load",
    )

    # Idempotence: upgrade runs this on every invocation, including already-migrated ones.
    again = migrate_provider_keys(temp_root, sessions)
    assert_true(
        again["provider_config"]["status"] in {"nothing_to_do", "keys_migrated"},
        f"second run must be a no-op, got {again['provider_config']}",
    )
    assert_true(
        json.loads((workflow_dir / "second_agent.json").read_text(encoding="utf-8"))
        == migrated,
        "a second migration must not alter an already-migrated config",
    )
    assert_true(
        selected_provider(temp_root) in available_providers(),
        "a migrated workspace must still resolve to a registered provider",
    )

    # Read the migrated config back THROUGH the resolver the runtime actually uses.
    # Everything above asserts the write side, and the write side was never the bug: the
    # v3.4.3 rename produced a correct second_agent.json that no reader opened, because
    # the resolver still named the v3.4.2 file. Every project silently ran on tool
    # defaults — wrong model, wrong timeouts, wrong quota — and this suite stayed green.
    from config.settings import (
        load_provider_config_for,
        resolve_provider_config_for,
    )

    resolved = resolve_provider_config_for(temp_root)
    assert_true(
        resolved["source"] == "project",
        f"the project's own config must win over the tool default: {resolved!r}",
    )
    assert_true(
        Path(resolved["path"]) == workflow_dir / "second_agent.json",
        f"the resolver must open the CURRENT filename: {resolved['path']}",
    )
    assert_true(
        resolved["error"] is None,
        f"a well-formed config must resolve without error: {resolved['error']!r}",
    )
    assert_true(
        load_provider_config_for(temp_root).get("timeout_seconds") == 999,
        "the user's tuned value must reach the reader, not just the file on disk",
    )
    assert_true(
        load_provider_config_for(temp_root).get("provider_command")
        == "opencode-custom",
        "a migrated key must be readable through the resolver too",
    )

    # A v3.4.2 workspace that never ran upgrade still has the legacy filename. It must
    # keep working: the fallback is what makes fixing the resolver safe to ship.
    legacy_root = Path(tempfile.mkdtemp(prefix="provider-legacy-"))
    legacy_dir = legacy_root / ".workflow"
    legacy_dir.mkdir(parents=True)
    atomic_write_json(legacy_dir / "opencode.json", {"timeout_seconds": 777})
    legacy_resolved = resolve_provider_config_for(legacy_root)
    assert_true(
        legacy_resolved["source"] == "project_legacy"
        and legacy_resolved["config"].get("timeout_seconds") == 777,
        f"an un-upgraded workspace must still be read: {legacy_resolved!r}",
    )

    # No project config is reported as `missing`, pointing at the file to create. It
    # used to resolve to the tool-level config/second_agent.json, and that silent
    # substitution is how one machine-wide model reached projects nobody chose it for.
    bare_root = Path(tempfile.mkdtemp(prefix="provider-bare-"))
    (bare_root / ".workflow").mkdir(parents=True)
    bare = resolve_provider_config_for(bare_root)
    assert_true(
        bare["source"] == "missing"
        and bare["error"] is None
        and Path(bare["path"]) == bare_root / ".workflow" / "second_agent.json",
        f"absence must be named, and name the project file to create: {bare!r}",
    )

    # Malformed is NOT absence. The runtime stays up on defaults, but the substitution
    # has to be recorded — an unreported swap is the failure mode this whole check exists
    # to catch, just arriving through a stray comma instead of a rename.
    broken_root = Path(tempfile.mkdtemp(prefix="provider-broken-"))
    broken_dir = broken_root / ".workflow"
    broken_dir.mkdir(parents=True)
    (broken_dir / "second_agent.json").write_text(
        '{"timeout_seconds": }', encoding="utf-8"
    )
    broken = resolve_provider_config_for(broken_root)
    assert_true(
        broken["error"] is not None and broken["source"] == "invalid",
        f"an unreadable config must be reported as invalid, not substituted: {broken!r}",
    )
    assert_true(
        broken["config"].get("timeout_seconds") is not None,
        "readers such as doctor still get a config shape to report on",
    )

    # And the executor refuses both before any provider is called.
    from core.provider.executor import Executor
    from tests.checks.support import FakeOpenCodeAdapter

    for root, expected in ((bare_root, "provider_config_missing"), (broken_root, "provider_config_invalid")):
        fake = FakeOpenCodeAdapter()
        refused = Executor(adapter=fake).execute(
            "explore", "map it", {"session_id": "sid-cfg", "provider_session_id": None}, str(root)
        )
        assert_true(
            refused["meta"].get("error_type") == expected and not fake.calls,
            f"{expected}: the call must be refused before reaching the adapter: {refused}",
        )
    # A key the runtime does not know is a THIRD outcome, distinct from both of the above:
    # the file parses, its valid keys apply, and only the misspelled one is inert. Reported
    # as warnings rather than an error precisely so it does not discard the working keys —
    # asserted here because "unknown key" silently succeeding is the failure this closes.
    typo_root = Path(tempfile.mkdtemp(prefix="provider-typo-"))
    typo_dir = typo_root / ".workflow"
    typo_dir.mkdir(parents=True)
    atomic_write_json(
        typo_dir / "second_agent.json",
        {"timeout_second": 3600, "max_probes": "five", "idle_stall_seconds": 111},
    )
    typo = resolve_provider_config_for(typo_root)
    assert_true(
        typo["error"] is None and typo["source"] == "project",
        f"a typo must not be treated as an unreadable file: {typo!r}",
    )
    assert_true(
        any("timeout_second" in w for w in typo["warnings"]),
        f"an unknown key must be reported, not silently accepted: {typo['warnings']!r}",
    )
    assert_true(
        any("max_probes" in w for w in typo["warnings"]),
        f"a wrong-typed knob must be reported too: {typo['warnings']!r}",
    )
    assert_true(
        typo["config"].get("idle_stall_seconds") == 111,
        "the keys that ARE correct must still apply alongside the warnings",
    )

    # Every knob in config/settings.py is parsed at import time, so a mistyped env var used
    # to raise before argparse ran — taking `doctor` down with it, the one command whose job
    # is to say what broke. Asserted through a real subprocess import: the guarantee is about
    # module import, and calling the helper directly would not prove it.
    env = dict(os.environ, AI_PROXY_TIMEOUT_SECONDS="45m", AI_PROXY_MAX_PROBES="-3")
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from config.settings import DEFAULT_TIMEOUT_SECONDS, DEFAULT_MAX_PROBES;"
            "print(DEFAULT_TIMEOUT_SECONDS, DEFAULT_MAX_PROBES)",
        ],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        capture_output=True,
        text=True,
    )
    assert_true(
        probe.returncode == 0,
        f"a mistyped env var must not crash the import: {probe.stderr.strip()}",
    )
    assert_true(
        probe.stdout.split() == ["1800", "3"],
        f"a rejected env value must fall back to the built-in default: {probe.stdout!r}",
    )
    assert_true(
        "AI_PROXY_TIMEOUT_SECONDS" in probe.stderr
        and "AI_PROXY_MAX_PROBES" in probe.stderr,
        f"the warning must name the offending env var: {probe.stderr!r}",
    )

    for stray in (legacy_root, bare_root, broken_root, typo_root):
        shutil.rmtree(stray, ignore_errors=True)

    # The acceptance criterion for the whole v3.4.3 refactor: a provider with its OWN
    # session-id shape and log format can be added without touching core/. Asserted
    # rather than argued, because "the code looks general enough" is how it regresses.
    class _ProbeProvider:
        adapter = "test-provider"

        def __init__(self, command="probe-cli", timeout_seconds=0, on_progress=None):
            self.command = command
            self.timeout_seconds = timeout_seconds
            self.no_timeout = True
            self.on_progress = on_progress
            self.poll_interval = 1
            self.bootstrap_timeout_seconds = 30
            self.agent = None
            self.last_call_meta = {}
            self.on_session_created = None

        @staticmethod
        def extract_session_id(text):
            for line in (text or "").splitlines():
                if line.startswith("sid="):
                    return line[4:].strip()
            return None

        @staticmethod
        def clean_output(text):
            return "\n".join(
                l for l in (text or "").splitlines() if not l.startswith("sid=")
            ).strip()

        def probe(
            self, session_id=None, model=None, work_dir=None, timeout_seconds=None
        ):
            return {"ok": True, "content": "alive", "meta": {}}

        def run(self, prompt, session, model=None, work_dir=None):
            return {"ok": True, "content": "[EVIDENCE]", "meta": {}}

    from adapters.contract.base import SecondAgentAdapter

    register(_ProbeProvider)
    assert_true(
        isinstance(_ProbeProvider(), SecondAgentAdapter),
        "a provider implementing run/probe/parsers must satisfy the contract without "
        "inheriting anything — the Protocol must not demand OpenCode's internal shape",
    )
    # v3.4.3 made `runtime.second_agent` SELECT the provider. v3.4.4 takes that back, and
    # the reason is the pair of keys, not the key: `provider_command` is only ever read
    # from second_agent.json, so a config.json naming another provider built that adapter
    # and handed it the first one's binary — a combination codex's `_command_guard` refuses
    # on every call. One file owns the decision now, and the ignored hint is REPORTED so
    # the key does not go back to being silently inert.
    atomic_write_json(
        workflow_dir / "config.json", {"runtime": {"second_agent": "test-provider"}}
    )
    assert_true(
        selected_provider(temp_root) != "test-provider",
        "config.json `second_agent` must NOT select the provider — second_agent.json does",
    )
    conflict = hint_conflict(temp_root, selected=selected_provider(temp_root))
    assert_true(
        conflict is not None
        and conflict["provider_hint_ignored"] == "test-provider",
        f"an ignored config.json hint must be reported, not swallowed: {conflict}",
    )
    atomic_write_json(
        workflow_dir / "second_agent.json",
        {**migrated, "provider": "test-provider", "provider_command": "test-provider"},
    )
    assert_true(
        selected_provider(temp_root) == "test-provider",
        "second_agent.json `provider` must select the provider",
    )
    assert_true(
        resolve_adapter(selected_provider(temp_root)).adapter == "test-provider",
        "resolution must follow the file that also carries provider_command",
    )
    assert_true(
        hint_conflict(temp_root, selected=selected_provider(temp_root)) is None,
        "a hint that AGREES with the selection is not a conflict and must stay quiet",
    )
    atomic_write_json(workflow_dir / "second_agent.json", migrated)
    assert_true(
        _ProbeProvider.extract_session_id("sid=abc123") == "abc123",
        "a provider's own session-id shape must be honoured, not OpenCode's ses_ prefix",
    )
    _assert_codex_provider()
    shutil.rmtree(temp_root, ignore_errors=True)


def _test_provider_selection() -> None:
    """The `provider` command: the catalog it reports and the write it refuses.

    The picker that drives this runs in the main agent, so the only thing standing
    between a mistyped answer and a workspace that cannot run is the validation here.
    Asserted on the FILE, not just the return value: a refusal that still wrote is the
    failure mode worth catching, and it looks identical from the outside.
    """
    from config.providers import effort_args, model_efforts, provider_models
    from config.settings import validate_provider_config
    from core.provider import provider_select
    from core.evidence.result_shaping import _verify_exit_code
    from core.workspace.workspace_paths import atomic_write_json, read_json_file

    temp_root = Path(tempfile.mkdtemp(prefix="agent-workflow-provider-"))
    workflow_dir = temp_root / ".workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    config_file = workflow_dir / "second_agent.json"

    catalog = provider_select.run(temp_root, "")
    listed = {entry["name"]: entry for entry in catalog["meta"]["providers"]}
    assert_true(
        {"opencode", "codex"} <= set(listed),
        f"both shipped providers must be offered: {sorted(listed)}",
    )
    assert_true(
        all(entry["models"] for entry in listed.values()),
        "every provider must offer at least one model, or the picker has nothing to show",
    )
    assert_true(
        all(
            isinstance(model.get("efforts"), list)
            for entry in listed.values()
            for model in entry["models"]
        ),
        "every shortlisted model must state its efforts — an empty list is a valid "
        "answer (this model takes none), a missing key is not",
    )

    applied = provider_select.run(temp_root, "codex|gpt-5.6-luna|high")
    assert_true(applied["ok"], f"a valid selection must apply: {applied}")
    written = read_json_file(config_file)
    assert_true(
        written.get("provider") == "codex"
        and written.get("provider_command") == "codex"
        and written.get("provider_agent") is None
        and written.get("default_model") == "gpt-5.6-luna"
        and written.get("effort") == "high",
        # Five keys together or not at all: `provider` alone leaves opencode's binary and
        # persona on disk under a codex selection, which is the exact state
        # foreign_provider_values exists to report.
        f"an apply must write all five selection keys: {written}",
    )
    assert_true(
        not applied["meta"]["foreign_values"],
        f"a fresh apply must leave nothing foreign behind: {applied['meta']}",
    )

    refused = provider_select.run(temp_root, "codex|gpt-5.5|ultra")
    assert_true(
        not refused["ok"]
        and refused["meta"]["error_type"] == "invalid_provider_selection",
        f"an effort the model does not accept must be refused: {refused}",
    )
    assert_true(
        read_json_file(config_file) == written,
        "a refused selection must leave the file byte-identical — a half-applied "
        "selection is worse than no change at all",
    )
    assert_true(
        _verify_exit_code("provider", refused) == 2,
        "a refusal must exit nonzero, or a caller checking only the status reads it as "
        "an applied selection",
    )

    unknown = provider_select.run(temp_root, "gemini|x|high")
    assert_true(
        not unknown["ok"] and read_json_file(config_file) == written,
        f"an unknown provider must be refused without writing: {unknown}",
    )

    # A model outside the shortlist is a supported pin, not an error: the shortlist is a
    # menu (opencode alone lists 137 models), so refusing here would refuse a working
    # config. It warns instead, because the effort can no longer be checked.
    # A deliberately absent id, not a real one: the shortlist is hand-tuned, and a test
    # naming a model someone might later add would start passing for the wrong reason.
    off_menu = provider_select.run(temp_root, "opencode|opencode/not-shipped-here|high")
    assert_true(
        off_menu["ok"] and off_menu["meta"]["warnings"],
        f"an off-shortlist model must apply WITH a warning: {off_menu}",
    )

    cleared = provider_select.run(temp_root, "opencode")
    assert_true(
        cleared["ok"]
        and read_json_file(config_file).get("effort") is None
        and read_json_file(config_file).get("default_model") is None,
        "omitting model and effort must clear them, not keep the previous provider's",
    )

    # config.json is a hint that selects nothing, but doctor prints it. Left stale it is a
    # second answer to "which provider is this workspace on".
    atomic_write_json(workflow_dir / "config.json", {"runtime": {"second_agent": "x"}})
    synced = provider_select.run(temp_root, "codex|gpt-5.6-sol|max")
    assert_true(
        synced["meta"]["config_hint"]["updated"]
        and read_json_file(workflow_dir / "config.json")["runtime"]["second_agent"]
        == "codex",
        f"an apply must bring the config.json hint along: {synced['meta']}",
    )

    # A workspace written before this key existed must keep loading unchanged.
    assert_true(
        not validate_provider_config(
            {"provider": "codex", "default_model": "gpt-5.5", "timeout_seconds": 1800}
        ),
        "a config with no effort key must produce no warnings",
    )
    assert_true(
        any(
            "not accepted" in warning
            for warning in validate_provider_config(
                {"provider": "codex", "default_model": "gpt-5.5", "effort": "ultra"}
            )
        ),
        "validation must catch an effort the pinned model rejects",
    )
    assert_true(
        not validate_provider_config(
            {
                "provider": "codex",
                "default_model": "some-unlisted-model",
                "effort": "ultra",
            }
        ),
        "an unlisted model constrains nothing, so its effort must not be second-guessed",
    )

    # A model that takes NO effort. Empty means "none" only for a model the shortlist
    # actually lists; for anything else it means "unknown", and the two must not be
    # collapsed — one has to refuse, the other has to allow.
    from config import providers as providers_module
    from core.prompt.router import Router

    original = providers_module.PROVIDER_BUNDLES["codex"]["models"]
    providers_module.PROVIDER_BUNDLES["codex"]["models"] = (
        {"id": "no-effort-model", "efforts": ()},
    )
    try:
        assert_true(
            not model_efforts("codex", "no-effort-model")
            and providers_module.model_is_listed("codex", "no-effort-model"),
            "a listed model with no efforts must still read as listed",
        )
        refused_none = provider_select.run(temp_root, "codex|no-effort-model|high")
        assert_true(
            not refused_none["ok"] and "takes no reasoning effort" in refused_none["content"],
            f"an effort on a model that takes none must be refused: {refused_none}",
        )
        accepted = provider_select.run(temp_root, "codex|no-effort-model")
        assert_true(
            accepted["ok"] and read_json_file(config_file)["effort"] is None,
            f"the same model with no effort must apply cleanly: {accepted}",
        )
        # The refusal above only guards writes through this command. A hand-edited file
        # never passes through it, so the route drops the flag as well.
        routed = Router(
            {
                "provider": "codex",
                "provider_command": "codex",
                "default_model": "no-effort-model",
                "effort": "high",
                "routes": {},
            }
        ).route("analyze")
        assert_true(
            routed["effort"] is None,
            f"a hand-written effort must be dropped for a model that takes none: {routed}",
        )
        kept = Router(
            {
                "provider": "codex",
                "provider_command": "codex",
                "default_model": "some-unlisted-model",
                "effort": "high",
                "routes": {},
            }
        ).route("analyze")
        assert_true(
            kept["effort"] == "high",
            f"an unlisted model declares nothing, so its effort must survive: {kept}",
        )
    finally:
        providers_module.PROVIDER_BUNDLES["codex"]["models"] = original

    assert_true(
        effort_args("opencode", "high") == ["--variant", "high"]
        and effort_args("codex", "high") == ["-c", 'model_reasoning_effort="high"']
        and effort_args("opencode", None) == [],
        "each provider must spell effort its own way, and say nothing when unset",
    )
    assert_true(
        model_efforts("codex", "gpt-5.5") == ("low", "medium", "high", "xhigh")
        and model_efforts("codex", "not-a-model") == (),
        "efforts must come from the model entry, not from the provider",
    )
    assert_true(
        all(
            isinstance(entry.get("id"), str) for entry in provider_models("opencode")
        ),
        "every shortlist entry must carry an id the picker can send back",
    )

    # A workspace initialized before `effort` existed gets the key on the next install
    # pass, at its default of None — the backfill is what carries new keys into projects
    # that were set up once and left alone.
    from adapters.install.opencode_install import _merge_provider_config

    atomic_write_json(config_file, {"provider": "codex", "timeout_seconds": 1800})
    added = _merge_provider_config(temp_root, "")
    assert_true(
        "effort" in added and read_json_file(config_file)["effort"] is None,
        f"backfill must add `effort` to a pre-existing workspace, unset: {added}",
    )
    assert_true(
        read_json_file(config_file)["timeout_seconds"] == 1800,
        "backfill must not touch a value the user already tuned",
    )

    shutil.rmtree(temp_root, ignore_errors=True)


def _test_doctor_read_boundary_warning() -> None:
    """codex and agy cannot bound what the second agent reads and get the full environment;
    agy cannot bound what it writes either.

    Choosing one is trusting it, which is the user's call — so doctor warns on every run
    instead of failing readiness. Reads and writes are warned apart: codex's sandbox stops
    writes, so it gets the read warning only; agy gets both; opencode neither."""
    from core.audit.diagnostics import run_doctor
    from core.runtime.state import ensure_workflow_workspace
    from core.workspace.workspace_paths import PROVIDER_CONFIG_NAME, read_json_file, workflow_paths

    root = Path(tempfile.mkdtemp(prefix="doctor-read-boundary-"))
    try:
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
        config_path = workflow_paths(root)["workflow_dir"] / PROVIDER_CONFIG_NAME
        for provider, expected, writes in (
            ("codex", "not_enforceable", "sandboxed"),
            ("agy", "not_enforceable", "unbounded"),
            ("opencode", "enforceable", "denied"),
        ):
            config = read_json_file(config_path)
            config["provider"] = provider
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            meta = run_doctor(root, "does-not-exist", "doctor-read-boundary")["meta"]
            checks = read_json_file(Path(meta["doctor_report"]))["checks"]
            boundary = checks.get("second_agent_read_boundary") or {}
            warnings = [fix for fix in meta["recommended_fixes"] if fix.startswith("WARNING: second_agent provider")]
            read_warnings = [fix for fix in warnings if "can read every file" in fix]
            write_warnings = [fix for fix in warnings if "can WRITE" in fix]
            assert_true(boundary.get("status") == expected, f"{provider}: read boundary reported as {expected}: {boundary}")
            assert_true(
                (checks.get("second_agent_write_boundary") or {}).get("writes") == writes,
                f"{provider}: write boundary reported as {writes}: {checks.get('second_agent_write_boundary')}",
            )
            if expected == "not_enforceable":
                assert_true(
                    len(read_warnings) == 1 and f"'{provider}'" in read_warnings[0] and "`.env`" in read_warnings[0] and "/.provider" in read_warnings[0],
                    f"{provider}: doctor warns that every file and env var is readable: {warnings}",
                )
                assert_true(
                    not any("second_agent provider" in issue for issue in meta["issues"]),
                    f"{provider}: a trusted provider is a warning, never an issue: {meta['issues']}",
                )
            else:
                assert_true(read_warnings == [], f"{provider}: no read warning where the boundary holds: {warnings}")
            if writes == "unbounded":
                assert_true(
                    len(write_warnings) == 1 and f"'{provider}'" in write_warnings[0] and "/.provider" in write_warnings[0],
                    f"{provider}: doctor warns that the second agent can write: {warnings}",
                )
            else:
                assert_true(write_warnings == [], f"{provider}: no write warning where writes are stopped: {warnings}")
            assert_true(
                len(warnings) == len(read_warnings) + len(write_warnings),
                f"{provider}: every provider warning is a read or a write warning: {warnings}",
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_agy_provider() -> None:
    """agy: parsing, argv, and the guard that stands in for a boundary it does not have.

    Offline on purpose. Every assertion runs against transcripts captured from the real
    binary rather than the binary itself, so the suite stays deterministic and costs no
    tokens — the one thing a live call would prove that these do not is that agy still
    speaks this dialect, and that is what `probe` is for.
    """
    from adapters.providers.agy_adapter import AgyAdapter
    from adapters.contract.registry import adapter_class, available_providers
    from config.providers import (
        PROVIDER_BUNDLES,
        effort_args,
        provider_opt_in_env,
    )
    from core.policy import agy_guard
    from core.provider import provider_select
    from core.workspace.workspace_paths import read_json_file

    assert_true(
        "agy" in available_providers(),
        f"agy must be registered as a provider: {available_providers()}",
    )
    adapter = adapter_class("agy")()
    assert_true(
        adapter.adapter == "agy" and adapter.agent is None,
        "agy selects no persona — `agy agents` lists none, so there is none to default to",
    )
    assert_true(
        adapter.bootstrap_timeout_seconds is None,
        "agy needs no bootstrap call: the id is in the first line of the stream",
    )

    # --- session id -------------------------------------------------------------
    # Line 1 of a real `--output-format stream-json` run, trimmed.
    init_line = (
        '{"event":"init","conversation_id":"91e90a3a-9883-4595-80e1-b5560e0ed474",'
        '"init":{"cwd":"C:\\\\tmp","permission_mode":"always-proceed"}}'
    )
    assert_true(
        AgyAdapter.extract_session_id(init_line)
        == "91e90a3a-9883-4595-80e1-b5560e0ed474",
        "the conversation id must come off the init event",
    )
    assert_true(
        AgyAdapter.extract_session_id(
            "I0814 16:35:08.340741 35188 server.go:1007] "
            "Created conversation 91e90a3a-9883-4595-80e1-b5560e0ed474"
        )
        == "91e90a3a-9883-4595-80e1-b5560e0ed474",
        "the --log-file spelling must still parse: it is the fallback if the stream moves",
    )
    assert_true(
        AgyAdapter.extract_session_id("no identifier anywhere in this line") is None,
        "a line with no id must yield None rather than a partial match",
    )

    # --- clean_output -----------------------------------------------------------
    transcript = "\n".join(
        [
            init_line,
            '{"event":"step_update","step_update":{"step_index":3,"state":"ACTIVE",'
            '"step_type":"agent_response","text_delta":"pong."}}',
            '{"event":"step_update","step_update":{"step_index":3,"state":"DONE",'
            '"step_type":"agent_response","text_delta":"\\n"}}',
            '{"event":"result","result":{"status":"SUCCESS","response":"pong.\\n"}}',
        ]
    )
    assert_true(
        AgyAdapter.clean_output(transcript) == "pong.",
        f"the result event is the answer: {AgyAdapter.clean_output(transcript)!r}",
    )
    without_result = "\n".join(
        line for line in transcript.splitlines() if '"event":"result"' not in line
    )
    assert_true(
        AgyAdapter.clean_output(without_result) == "pong.",
        # A run killed mid-answer never emits `result`; the deltas carried the same text
        # on the way past, and dropping them would throw away a recoverable answer.
        "the agent_response deltas must cover a run that never reached its result event",
    )
    assert_true(
        AgyAdapter.clean_output("plain banner text") == "plain banner text",
        "non-JSON output must survive: a build that banners in plain text still answered",
    )
    assert_true(
        AgyAdapter.clean_output(
            '{"event":"step_update","step_update":{"step_type":"tool","state":"ERROR"}}'
        )
        == "",
        # Structured output naming no answer. Returning the raw JSONL would hand the
        # runtime a wall of events to read as though it were evidence.
        "a stream with no answer in it must produce empty content, not the events",
    )

    # --- argv -------------------------------------------------------------------
    fresh = AgyAdapter(timeout_seconds=1800)._build_args("TASK", None, "claude-sonnet-4-6", 1800)
    assert_true(
        "--conversation" not in fresh and fresh[:3] == ["agy", "-p", "TASK"],
        f"a fresh call passes the prompt on argv and resumes nothing: {fresh}",
    )
    assert_true(
        "--dangerously-skip-permissions" in fresh and "--disable-slash-commands" in fresh,
        # The first is the only way agy enables any tool at all (see the adapter
        # docstring); the second stops agy expanding the `/.`-shaped text in our prompts.
        f"both boundary-and-parsing flags must ride every call: {fresh}",
    )
    assert_true(
        fresh[fresh.index("--print-timeout") + 1] == "1800s",
        # agy's own print budget defaults to five minutes. Unset, it kills the call long
        # before the runtime's timeout and the runtime then blames the wrong thing.
        f"--print-timeout must follow the runtime budget: {fresh}",
    )
    resumed = AgyAdapter(timeout_seconds=900)._build_args("TASK", "abc-123", None, 900)
    assert_true(
        resumed[resumed.index("--conversation") + 1] == "abc-123"
        and "--model" not in resumed,
        f"a resumed call adds --conversation and nothing else: {resumed}",
    )
    with_effort = AgyAdapter(timeout_seconds=60)
    with_effort.effort = "high"
    assert_true(
        with_effort._build_args("T", None, None, 60)[-2:] == ["--effort", "high"],
        "effort must render through the bundle, not a literal in the adapter",
    )

    # --- workspace guard --------------------------------------------------------
    guard_root = Path(tempfile.mkdtemp(prefix="agent-workflow-agy-guard-"))
    try:
        init = subprocess.run(
            ["git", "init", "-q", str(guard_root)], capture_output=True, text=True
        )
        assert_true(init.returncode == 0, f"guard fixture git init failed: {init.stderr}")

        before = agy_guard.snapshot(guard_root)
        assert_true(
            before["available"],
            f"a git repository must be snapshottable: {before['reason']}",
        )
        (guard_root / "written-by-agy.txt").write_text("BREACH", encoding="utf-8")
        verdict = agy_guard.verdict(before, agy_guard.snapshot(guard_root))
        assert_true(
            verdict["checked"] and verdict["mutated"],
            f"a file written during a call must be detected: {verdict}",
        )
        assert_true(
            any("written-by-agy.txt" in entry for entry in verdict.get("appeared", ())),
            f"the detection must name the file, not just report a change: {verdict}",
        )

        (guard_root / "written-by-agy.txt").unlink()
        clean = agy_guard.verdict(before, agy_guard.snapshot(guard_root))
        assert_true(
            clean["checked"] and not clean["mutated"],
            f"an unchanged tree must read as clean, not as unknown: {clean}",
        )
    finally:
        shutil.rmtree(guard_root, ignore_errors=True)

    # Outside git the guard cannot see anything, and saying so is the point: `mutated:
    # False` from a guard that never ran would read as proof that nothing was written.
    outside = Path(tempfile.mkdtemp(prefix="agent-workflow-agy-nogit-"))
    try:
        unavailable = agy_guard.snapshot(outside)
        blind = agy_guard.verdict(unavailable, unavailable)
        assert_true(
            not blind["checked"] and not blind["mutated"] and blind["reason"],
            f"a guard that could not run must say so with a reason: {blind}",
        )
    finally:
        shutil.rmtree(outside, ignore_errors=True)

    # --- bundle + selection -----------------------------------------------------
    assert_true(
        effort_args("agy", "high") == ["--effort", "high"]
        and effort_args("agy", None) == [],
        "agy spells effort with --effort, and says nothing when unset",
    )
    # An install module that reports the absence of a boundary, rather than no module at
    # all. `core/runtime/upgrade.py` and `installer/settings.py` both dispatch through
    # `provider_install_module`, which RAISES on a bundle that declares none — so a
    # provider without one crashes the install path instead of being skipped by it.
    from config.providers import provider_install_module

    assert_true(
        PROVIDER_BUNDLES["agy"]["install_module"] == "adapters.install.agy_install",
        "agy must declare an install module, or the boundary install path raises on it",
    )
    boundary = provider_install_module("agy").install_project_config(
        Path(tempfile.gettempdir()), ""
    )
    assert_true(
        boundary["status"] == "not_enforceable"
        and boundary["permissions_enforced"] == 0
        and boundary["permissions_declared"] == 0
        and boundary["path"] is None,
        # Zero DECLARED as well is what separates agy from codex: codex still sends
        # permission flags that would start working the day it gates shell reads; agy has
        # no such flag to send, and claiming a path would send someone looking for a
        # boundary file that does not exist.
        f"agy must report no boundary rather than a zero-strength one: {boundary}",
    )
    assert_true(
        any("enforces NO boundary" in warning for warning in boundary["warnings"]),
        f"the missing boundary must be stated where install output is read: {boundary}",
    )

    select_root = Path(tempfile.mkdtemp(prefix="agent-workflow-agy-select-"))
    opt_in_env = provider_opt_in_env("agy")
    previous_opt_in = os.environ.get(opt_in_env)
    try:
        (select_root / ".workflow").mkdir(parents=True, exist_ok=True)
        config_path = select_root / ".workflow" / "second_agent.json"

        # Without the acknowledgement, agy is a provider the picker names and refuses to
        # write. Asserted on the FILE as well as the verdict: a refusal that still wrote
        # would leave a workspace pointed at an unbounded second_agent while reporting
        # that it had declined to do exactly that.
        os.environ.pop(opt_in_env, None)
        blocked = provider_select.run(select_root, "agy|claude-sonnet-4-6")
        assert_true(
            not blocked["ok"]
            and blocked["meta"]["error_type"] == "invalid_provider_selection"
            and "read-only boundary" in blocked["content"],
            f"agy must be refused without an explicit opt-in: {blocked}",
        )
        assert_true(
            not config_path.exists(),
            "a refused agy selection must write nothing at all",
        )
        assert_true(
            opt_in_env in (blocked["meta"].get("next_action") or ""),
            f"the refusal must name the variable that lifts it: {blocked['meta']}",
        )

        catalog = provider_select.run(select_root, "")
        entries = {entry["name"]: entry for entry in catalog["meta"]["providers"]}
        assert_true(
            entries["agy"]["requires_opt_in"]
            and not entries["agy"]["opt_in_granted"]
            and not entries["opencode"]["requires_opt_in"],
            # The picker offering agy as though it were interchangeable, only for the
            # write to fail a second later, is the menu lying about itself.
            f"the catalog must mark agy as gated and the others as not: {entries['agy']}",
        )

        os.environ[opt_in_env] = "1"
        applied = provider_select.run(select_root, "agy|claude-sonnet-4-6")
        assert_true(
            applied["ok"], f"agy must be selectable once acknowledged: {applied}"
        )
        written = read_json_file(config_path)
        assert_true(
            written.get("provider") == "agy"
            and written.get("provider_command") == "agy"
            and written.get("provider_agent") is None,
            f"selecting agy must write agy's own command and persona: {written}",
        )
        assert_true(
            all(
                (written.get("routes") or {}).get(name, {}).get("model")
                == "claude-sonnet-4-6"
                for name in ("explore", "plan", "analyze", "verify")
            ),
            f"the model must reach every route, not just default_model: {written}",
        )
        # Every agy model declares an empty effort set — a statement that nothing has been
        # verified, and the picker must hold the line rather than build a command line the
        # provider may refuse.
        refused = provider_select.run(select_root, "agy|claude-sonnet-4-6|high")
        assert_true(
            not refused["ok"] and "takes no reasoning effort" in refused["content"],
            f"an unverified effort must be refused for agy: {refused}",
        )
    finally:
        if previous_opt_in is None:
            os.environ.pop(opt_in_env, None)
        else:
            os.environ[opt_in_env] = previous_opt_in
        shutil.rmtree(select_root, ignore_errors=True)

    # Resume health reaches last_call_meta, which is what the usage row reads.
    class _ConversationAgy(AgyAdapter):
        def _popen_capture(self, args, cwd, timeout, phase, on_session):
            self.last_call_meta = {"phase": phase}
            if on_session:
                on_session("conv-new")
            return {
                "output_complete": True,
                "returncode": 0,
                "stdout": "[EVIDENCE]",
                "stderr": "",
                "timed_out": False,
                "duration_seconds": 0.1,
                "idle_seconds": 0.0,
            }

    agy_root = Path(tempfile.mkdtemp(prefix="agy-resume-"))
    try:
        kept = _ConversationAgy(timeout_seconds=60)
        kept.run("task", {"provider_session_id": "conv-new"}, None, str(agy_root))
        lost = _ConversationAgy(timeout_seconds=60)
        lost.run("task", {"provider_session_id": "conv-old"}, None, str(agy_root))
        assert_true(
            (kept.last_call_meta.get("resumed"), kept.last_call_meta.get("thread_changed")) == (True, False)
            and (lost.last_call_meta.get("resumed"), lost.last_call_meta.get("thread_changed")) == (True, True),
            "agy reports resume and a lost conversation on last_call_meta: "
            f"kept={kept.last_call_meta} lost={lost.last_call_meta}",
        )
    finally:
        shutil.rmtree(agy_root, ignore_errors=True)


def _check_version_cache(versions_module) -> None:
    """`<cli> --version` costs a spawn per delegated run (about 1.2 s for opencode's shim),
    so a reading is kept on disk, keyed by the executable's path, mtime and size."""
    work = Path(tempfile.mkdtemp(prefix="agent-workflow-versions-"))
    exe = work / "fakecli.exe"
    exe.write_bytes(b"v1")
    saved_path, saved_run = versions_module.CACHE_PATH, versions_module.osutil.run_bounded
    saved_cache = dict(versions_module._CACHE)
    answers: list = []
    calls: list = []

    def fake_run(args, timeout, **_kwargs):
        calls.append(args)
        return answers.pop(0)

    def read() -> str | None:
        versions_module._CACHE.clear()  # a new process: only the disk remembers
        return versions_module.read_version(str(exe))

    versions_module.CACHE_PATH = work / "cache" / "provider-versions.json"
    versions_module.osutil.run_bounded = fake_run
    try:
        # A banner before the version line: the first line WITH a version is the reading.
        answers.append((0, "warning: update available\nfakecli version 1.2.3\n", ""))
        assert_true(read() == "fakecli version 1.2.3" and len(calls) == 1, f"first read spawns: {calls}")
        assert_true(read() == "fakecli version 1.2.3" and len(calls) == 1, "a cache hit spawns nothing")

        exe.write_bytes(b"v2, a different size")
        answers.append((0, "", "fakecli 2.0.0\n"))  # and a CLI that answers on stderr
        assert_true(read() == "fakecli 2.0.0" and len(calls) == 2, "a rewritten binary is read again")

        exe.write_bytes(b"v3: no --version flag")
        answers.append((2, "", "error: unknown option --version"))
        assert_true(read() is None and len(calls) == 3, "no readable version is None")
        assert_true(read() is None and len(calls) == 3, "and that None is cached, not retried every run")

        # Exit 0 with only a banner: no release in it, so the reading is None, cached like any
        # completed answer. The banner itself is never kept as the CLI's version.
        exe.write_bytes(b"v3b: banner only")
        answers.append((0, "Welcome to fakecli!\nRun fakecli --help\n", "no version here"))
        assert_true(read() is None and len(calls) == 4, "a banner without a version is None")
        assert_true(read() is None and len(calls) == 4, "and that None is cached")

        exe.write_bytes(b"v4 that hangs")
        answers.extend([(None, "", ""), (0, "fakecli 4.0.0", "")])
        assert_true(read() is None and len(calls) == 5, "a timeout reads as None")
        assert_true(read() == "fakecli 4.0.0" and len(calls) == 6, "but is not remembered across runs")

        stale = versions_module._cache_key(str(exe))
        assert_true(stale is not None, "an existing executable has a cache key")
        os.utime(exe, ns=(0, 0))
        answers.append((0, "fakecli 4.0.1", ""))
        assert_true(read() == "fakecli 4.0.1" and len(calls) == 7, "an mtime change alone invalidates")

        versions_module.CACHE_PATH = work / "cache-file-is-a-dir"
        versions_module.CACHE_PATH.mkdir()
        answers.append((0, "fakecli 4.0.1", ""))
        assert_true(read() == "fakecli 4.0.1", "an unusable cache only means the version is read again")
    finally:
        versions_module.CACHE_PATH = saved_path
        versions_module.osutil.run_bounded = saved_run
        versions_module._CACHE.clear()
        versions_module._CACHE.update(saved_cache)
        shutil.rmtree(work, ignore_errors=True)


def _assert_codex_provider() -> None:
    """Codex: the second real provider, and the proof the seam holds for shipped code.

    `_ProbeProvider` above shows a provider CAN be added without touching core/. Codex is
    the same claim tested against a provider that actually runs: a different session-id
    shape (a UUID in a JSON event, not a prefixed token), a different output format (JSONL
    events, not log lines), and a resume subcommand instead of a session flag.

    Everything here is argv- and parser-level. Nothing spawns codex — the CLI may not be
    installed, and a test that needs it would be skipped exactly where it matters.
    """
    from adapters.contract.base import SecondAgentAdapter
    from adapters.providers.codex_adapter import CodexAdapter
    from adapters.contract.registry import (
        available_providers,
        hint_conflict,
        resolve_adapter,
        selected_provider,
    )
    from config.providers import bundle_for, provider_install_module
    from core.audit.mcp_scan import _mcp_config_candidates

    assert_true(
        "codex" in available_providers(),
        f"codex must be registered: {available_providers()}",
    )
    assert_true(
        resolve_adapter("codex").adapter == "codex",
        "an explicit provider name must resolve to that provider's adapter",
    )
    assert_true(
        isinstance(CodexAdapter(), SecondAgentAdapter),
        "CodexAdapter must satisfy the Protocol without inheriting from it",
    )

    started = '{"type":"thread.started","thread_id":"019fe9cb-5a19-7303-b571-38d04f2d395a"}'
    assert_true(
        CodexAdapter.extract_session_id(started)
        == "019fe9cb-5a19-7303-b571-38d04f2d395a",
        "the thread id must be read out of the first JSONL event",
    )
    stream = "\n".join(
        [
            started,
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"[EVIDENCE]\\nfinal"}}',
            '{"type":"turn.completed","usage":{"output_tokens":5}}',
        ]
    )
    assert_true(
        CodexAdapter.clean_output(stream) == "[EVIDENCE]\nfinal",
        "clean_output must return the LAST agent_message, not the first or the raw stream",
    )
    assert_true(
        CodexAdapter.clean_output("plain text, no events") == "plain text, no events",
        "a non-JSONL stream must degrade to its text rather than to an empty answer",
    )
    # The banner `codex exec` prints WITHOUT --json. Not a shape this adapter can produce,
    # but the one the reader sees when they run codex by hand, and the difference between
    # "capture is broken" and "you were looking at a different mode" is worth a pattern.
    assert_true(
        CodexAdapter.extract_session_id(
            "session id: 019fe9cb-5a19-7303-b571-38d04f2d395a\n"
        )
        == "019fe9cb-5a19-7303-b571-38d04f2d395a",
        "the plain-text session banner must be read too, not only the JSONL event",
    )
    assert_true(
        CodexAdapter.extract_session_id("see the session id: field in the docs") is None,
        "the plain-text pattern must not match prose that merely mentions a session id",
    )

    adapter = CodexAdapter(command="codex")
    fresh = adapter._build_args(
        resume_id=None, model=None, cwd="/tmp/project", last_message=None
    )
    assert_true(
        "-" in fresh and "--sandbox" in fresh and "read-only" in fresh,
        f"a fresh call must read the prompt from stdin under read-only: {fresh}",
    )
    assert_true("-C" in fresh, f"a fresh call must set the working root: {fresh}")

    resumed = adapter._build_args(
        resume_id="019fe9cb-5a19-7303-b571-38d04f2d395a",
        model=None,
        cwd="/tmp/project",
        last_message=None,
    )
    assert_true(
        "resume" in resumed and "019fe9cb-5a19-7303-b571-38d04f2d395a" in resumed,
        f"a session must be continued via `exec resume <id>`: {resumed}",
    )
    # Every flag `exec resume` rejects, asserted by name. These are PARSER refusals: codex
    # answers `error: unexpected argument '--color' found` and exits before the model is
    # reached, so one stray flag is a total outage of continuation rather than a degraded
    # call. `--color never` shipped in v3.4.3 and made every resumed codex call fail; the
    # test that existed then only checked -C and --sandbox, so the list is now explicit.
    # The refusal is of flags AFTER `resume`; `exec` options before it are parsed by `exec`.
    after_resume = resumed[resumed.index("resume") + 1 :]
    for rejected in ("-C", "--cd", "--sandbox", "-s", "--color"):
        assert_true(
            rejected not in after_resume,
            f"`exec resume` rejects {rejected} — passing it fails the call: {resumed}",
        )
    assert_true(
        "--json" in resumed and "--skip-git-repo-check" in resumed,
        f"a resumed call must still stream JSONL events: {resumed}",
    )
    # CASE-021: the sandbox rides `exec --sandbox` before `resume`. The `-c sandbox_mode`
    # form it replaced makes codex 0.155+ refuse every resumed thread on Windows.
    before_resume = resumed[: resumed.index("resume")]
    assert_true(
        before_resume[-3:] == ["exec", "--sandbox", "read-only"],
        f"a resumed call must re-assert the sandbox as `exec --sandbox read-only resume`: {resumed}",
    )
    assert_true(
        not any(arg.startswith("sandbox_mode=") for arg in resumed),
        f"a resumed call must not carry the sandbox through -c sandbox_mode: {resumed}",
    )

    # The seam that has not been fixed yet: config/settings.py defaults provider_command to
    # opencode's binary for every provider. Pointed at the wrong CLI, this adapter must
    # refuse rather than run it — a silent substitution would return evidence from a
    # provider nobody selected.
    misconfigured = CodexAdapter(command="opencode")
    refused = misconfigured.run("anything", {}, None, None)
    assert_true(
        not refused["ok"] and refused["meta"]["error_type"] == "command_not_found",
        f"codex must refuse a non-codex provider_command: {refused}",
    )
    assert_true(
        "provider_command" in refused["meta"]["next_action"]
        and '"provider"' in refused["meta"]["next_action"],
        "the refusal must name BOTH keys that fix it — selecting a provider without its "
        "command is what produced this state",
    )

    # Session capture, without spawning codex. `_popen_capture` is the only seam that
    # touches a process, so overriding it exercises the whole of run() against a stream
    # whose shape the test controls.
    class _NoSessionCodex(CodexAdapter):
        """A codex that answers but never names its thread."""

        _stdout = (
            '{"type":"item.completed","item":'
            '{"type":"agent_message","text":"[EVIDENCE]"}}'
        )

        def _popen_capture(self, args, prompt, cwd, timeout, phase, on_session):
            # The real seam rebinds last_call_meta per spawn; so does this one.
            self.last_call_meta = {"phase": phase}
            return {
                "output_complete": True,
                "returncode": 0,
                "stdout": self._stdout,
                "stderr": "",
                "timed_out": False,
                "duration_seconds": 0.1,
                "idle_seconds": 0.0,
                "pid": 0,
                "kill": None,
            }

    class _LateEventCodex(_NoSessionCodex):
        """The id arrives only when the buffered stream is handed over at exit."""

        _stdout = started + "\n" + _NoSessionCodex._stdout

    class _LiveEventCodex(_LateEventCodex):
        """The id arrives live, as `_drain` reports it from stdout line 1."""

        def _popen_capture(self, args, prompt, cwd, timeout, phase, on_session):
            outcome = super()._popen_capture(args, prompt, cwd, timeout, phase, on_session)
            if on_session:
                on_session("019fe9cb-5a19-7303-b571-38d04f2d395a")
            return outcome

    orphan = _NoSessionCodex(command="codex").run("task", {}, None, None)
    assert_true(
        not orphan["ok"] and orphan["meta"]["error_type"] == "session_capture_failed",
        f"an answer with no thread id cannot be continued and must fail loudly: {orphan}",
    )
    assert_true(
        "[EVIDENCE]" in orphan["meta"].get("orphan_content", ""),
        "failing the call must not also destroy the text the call produced",
    )
    late = _LateEventCodex(command="codex").run("task", {}, None, None)
    assert_true(
        late["ok"]
        and late["meta"]["provider_session_id"]
        == "019fe9cb-5a19-7303-b571-38d04f2d395a",
        f"an event seen only at exit must still yield the session, not fail: {late}",
    )
    carried = _NoSessionCodex(command="codex").run(
        "task",
        {"provider_session_id": "019fe9cb-5a19-7303-b571-38d04f2d395a"},
        None,
        None,
    )
    assert_true(
        carried["ok"],
        f"a resumed call already knows its thread id and must not fail: {carried}",
    )

    # Resume health reaches the usage row through last_call_meta, not the result meta.
    kept = _NoSessionCodex(command="codex")
    kept.run("task", {"provider_session_id": "019fe9cb-5a19-7303-b571-38d04f2d395a"}, None, None)
    fresh = _LiveEventCodex(command="codex")
    fresh.run("task", {}, None, None)
    lost = _LiveEventCodex(command="codex")
    lost_result = lost.run("task", {"provider_session_id": "thread-that-codex-dropped"}, None, None)
    assert_true(
        (kept.last_call_meta.get("resumed"), kept.last_call_meta.get("thread_changed")) == (True, False)
        and (fresh.last_call_meta.get("resumed"), fresh.last_call_meta.get("thread_changed")) == (False, False),
        "a resumed call on its own thread and a fresh call both reach last_call_meta: "
        f"kept={kept.last_call_meta} fresh={fresh.last_call_meta}",
    )
    assert_true(
        lost.last_call_meta.get("thread_changed") is True
        and lost_result["meta"].get("thread_changed") is True,
        "a resumed call answered on a different thread is flagged: the session lost its "
        f"state without an error; got {lost.last_call_meta}",
    )

    # codex's Windows sandbox refusing a resumed thread (CASE-015): its own error type and
    # the steps that fix it, not `unknown` and "inspect the logs".
    class _SandboxCodex(_NoSessionCodex):
        def _popen_capture(self, args, prompt, cwd, timeout, phase, on_session):
            outcome = super()._popen_capture(args, prompt, cwd, timeout, phase, on_session)
            return {
                **outcome,
                "returncode": 1,
                "stdout": "",
                "stderr": (
                    "ERROR codex_core::session: Failed to create session: failed to load "
                    "AGENTS.md instructions for environment `local`: fs sandbox helper failed "
                    "with status exit code: 1: windows sandbox failed: elevated Windows sandbox "
                    "requires effective `:root` read access"
                ),
            }

    import adapters.providers.codex_adapter as codex_module
    from core.provider import versions as versions_module

    versions_module._CACHE[codex_module.osutil.resolve_exe("codex")] = "codex-cli 9.9.9"
    sandboxed = _SandboxCodex(command="codex")
    refused = sandboxed.run("task", {"provider_session_id": "019fe9cb-5a19-7303-b571-38d04f2d395a"}, None, None)
    assert_true(
        not refused["ok"]
        and refused["meta"]["error_type"] == "sandbox_unavailable"
        and "/clear" in refused["meta"]["next_action"]
        and "/.provider" in refused["meta"]["next_action"],
        f"a sandbox refusal names itself and its fix: {refused['meta'].get('error_type')} / {refused['meta'].get('next_action')}",
    )
    assert_true(
        refused["meta"].get("provider_version") == "codex-cli 9.9.9"
        and sandboxed.last_call_meta.get("provider_version") == "codex-cli 9.9.9",
        "the codex release travels with the failure, to the usage row too",
    )
    # A resumed thread refused by the sandbox is told apart from a fresh one (CASE-016): its
    # own reason, the failing release and the pin named in the advice, the type unchanged.
    from config.providers import provider_stable_version

    assert_true(
        refused["meta"].get("reason") == "resume_sandbox_failed"
        and "Resuming the codex thread failed" in refused["meta"]["next_action"]
        and "codex-cli 9.9.9" in refused["meta"]["next_action"]
        and provider_stable_version("codex") in refused["meta"]["next_action"],
        f"a resume refusal names itself: {refused['meta'].get('reason')} / {refused['meta'].get('next_action')}",
    )
    fresh_refused = _SandboxCodex(command="codex").run("task", {}, None, None)
    assert_true(
        not fresh_refused["ok"]
        and fresh_refused["meta"]["error_type"] == "sandbox_unavailable"
        and "reason" not in fresh_refused["meta"]
        and fresh_refused["meta"]["next_action"] == codex_module._SANDBOX_NEXT_ACTION,
        f"a fresh thread refused keeps the general advice: {fresh_refused['meta'].get('reason')} / {fresh_refused['meta'].get('next_action')}",
    )

    # stdout is the agent's JSONL, tool output included: a file it read that mentions a
    # sandbox failure must not turn an unrelated exit into `sandbox_unavailable`.
    class _SandboxWordsInStdoutCodex(_NoSessionCodex):
        def _popen_capture(self, args, prompt, cwd, timeout, phase, on_session):
            outcome = super()._popen_capture(args, prompt, cwd, timeout, phase, on_session)
            return {
                **outcome,
                "returncode": 1,
                "stdout": (
                    '{"type":"item.completed","item":{"type":"command_execution",'
                    '"aggregated_output":"docs: fs sandbox helper failed; windows sandbox failed"}}'
                ),
                "stderr": "ERROR codex_core: turn aborted",
            }

    unrelated = _SandboxWordsInStdoutCodex(command="codex").run(
        "task", {"provider_session_id": "019fe9cb-5a19-7303-b571-38d04f2d395a"}, None, None
    )
    assert_true(
        not unrelated["ok"] and unrelated["meta"]["error_type"] != "sandbox_unavailable",
        f"sandbox words only in the agent's stdout are not a sandbox refusal: {unrelated['meta'].get('error_type')}",
    )

    _check_version_cache(versions_module)

    # Provider releases against the bundle's stable_version (core/provider/versions.py):
    # advisory statuses, the same rule for every provider.
    from config.providers import PROVIDER_BUNDLES, provider_stable_version

    for name in PROVIDER_BUNDLES:
        stable = provider_stable_version(name)
        assert_true(bool(versions_module.release(stable)), f"{name} names a stable release: {stable}")
        assert_true(versions_module.status(name, f"{name}-cli {stable}") == "stable", f"{name}: its stable release is stable")
        assert_true(versions_module.status(name, "99.0.0") == "untested", f"{name}: another release is untested")
        assert_true(versions_module.status(name, None) == "unreadable", f"{name}: no answer is unreadable")
    assert_true(versions_module.release("codex-cli 0.162.0-alpha.9") == "0.162.0-alpha.9",
                "a pre-release keeps its suffix, so it never reads as the stable release")
    # Every provider's invocation is stamped, not only codex's: the usage row is the trail.
    from types import SimpleNamespace

    from core.provider.executor import Executor

    versions_module._CACHE[codex_module.osutil.resolve_exe("opencode-fake-cli")] = "1.0.0"
    stamped = {}
    Executor._stamp_provider_version(
        SimpleNamespace(adapter=SimpleNamespace(adapter="opencode", command="opencode-fake-cli")), stamped
    )
    assert_true(
        stamped == {"provider_version": "1.0.0", "provider_version_status": "untested"},
        f"an opencode call records its release and that it was not the tested one: {stamped}",
    )
    kept = {"provider_version": "codex-cli 9.9.9"}
    Executor._stamp_provider_version(SimpleNamespace(adapter=SimpleNamespace(adapter="codex", command="codex")), kept)
    assert_true(kept["provider_version_status"] == "untested" and kept["provider_version"] == "codex-cli 9.9.9",
                "an adapter's own reading is kept; only the status is added")

    # The seam itself: defaults must follow the SELECTED provider. Before this, a config
    # naming codex came back holding opencode's binary and opencode's `plan` persona, and
    # the workspace backfill then wrote those wrong values into the file permanently.
    from config.settings import default_provider_config

    codex_defaults = default_provider_config("codex")
    assert_true(
        codex_defaults["provider_command"] == "codex"
        and codex_defaults["provider_agent"] is None,
        f"codex defaults must be codex's, not opencode's: {codex_defaults}",
    )
    opencode_defaults = default_provider_config("opencode")
    assert_true(
        opencode_defaults["provider_command"] == "opencode"
        and opencode_defaults["provider_agent"] == "plan",
        f"opencode defaults must be unchanged: {opencode_defaults}",
    )
    assert_true(
        set(codex_defaults) == set(opencode_defaults),
        "the key SET must stay provider-independent — validate_provider_config uses it "
        "to decide which keys are known",
    )
    assert_true(
        default_provider_config("provider-that-is-not-registered")["provider_command"]
        == "provider-that-is-not-registered",
        "an unregistered provider must not silently inherit another provider's binary",
    )

    # A project that selects codex must resolve to codex through the same path the
    # executor uses, without any env var set.
    selected = Path(tempfile.mkdtemp(prefix="agent-workflow-select-"))
    try:
        (selected / ".workflow").mkdir(parents=True, exist_ok=True)
        (selected / ".workflow" / "second_agent.json").write_text(
            json.dumps({"provider": "codex"}), encoding="utf-8"
        )
        from config.settings import (
            load_provider_config_for,
            resolve_provider_config_for,
        )
        from core.provider.executor import Executor

        effective = load_provider_config_for(selected)
        assert_true(
            effective["provider_command"] == "codex",
            f"a file naming codex must not carry opencode's command: {effective['provider_command']}",
        )
        assert_true(
            resolve_provider_config_for(selected)["provider_explicit"],
            "a file that names a provider must be distinguishable from one that defaults",
        )
        assert_true(
            Executor()._adapter_for(selected).adapter == "codex",
            "the executor must resolve the provider the project selected, not the "
            "import-time default",
        )
        assert_true(
            Executor(adapter=OpenCodeAdapter())._adapter_for(selected).adapter
            == "opencode",
            "an injected adapter must still win outright — late resolution must not "
            "override what a caller handed in",
        )
        assert_true(
            Executor(provider="opencode")._adapter_for(selected).adapter == "opencode",
            "an explicitly pinned provider must win over the project's own selection",
        )
    finally:
        shutil.rmtree(selected, ignore_errors=True)

    # config.json alone must NOT select. It names the adapter while `provider_command`
    # keeps coming from second_agent.json, so honouring it built the codex adapter around
    # opencode's binary — the exact pair `_command_guard` refuses. The key is reported
    # instead of obeyed, which is what keeps it from going back to silently inert.
    via_config_json = Path(tempfile.mkdtemp(prefix="agent-workflow-cfg-"))
    try:
        (via_config_json / ".workflow").mkdir(parents=True, exist_ok=True)
        (via_config_json / ".workflow" / "config.json").write_text(
            json.dumps({"runtime": {"second_agent": "codex"}}), encoding="utf-8"
        )
        assert_true(
            Executor()._adapter_for(via_config_json).adapter != "codex",
            "`runtime.second_agent` in .workflow/config.json must NOT select the "
            "provider — second_agent.json carries the command that has to match it",
        )
        hint = hint_conflict(
            via_config_json, selected=selected_provider(via_config_json)
        )
        assert_true(
            hint is not None and hint["provider_hint_ignored"] == "codex",
            f"the ignored hint must be reported on the call's meta: {hint}",
        )
        assert_true(
            _mcp_config_candidates(via_config_json)
            == _mcp_config_candidates(via_config_json, selected_provider(via_config_json)),
            "the MCP scan must read the config of the provider that actually runs, not "
            "the one the hint names",
        )
    finally:
        shutil.rmtree(via_config_json, ignore_errors=True)

    bundle = bundle_for("codex")
    module = provider_install_module("codex")
    for function in ("load_config", "merge_policy", "install_project_config"):
        assert_true(
            callable(getattr(module, function, None)),
            f"codex install module must answer {function}()",
        )
    boundary = module.install_project_config(Path("."), ".")
    assert_true(
        boundary["status"] == "not_enforceable" and boundary["path"] is None,
        "codex installs no boundary FILE and enforces no read boundary either — it must say "
        f"so, and claim no path for a file that was never written: {boundary}",
    )
    globs = codex_secret_globs()
    # These two numbers must never be equal again. `permissions_enforced` was `len(denies)`
    # while codex enforced none of them, which is how a workspace that reads `.env` freely
    # came to print a healthy-looking count in doctor. Probed on codex-cli 0.147.0: denying
    # `**` for `:workspace_roots` still returns file contents at exit 0. Declared is what
    # rides on argv; enforced is what is known to stop something.
    assert_true(
        boundary["permissions_declared"] == len(globs),
        "every canonical secret pattern must still reach codex's argv; a declaration that "
        f"quietly shrinks is the failure this count exists to catch: {boundary}",
    )
    assert_true(
        boundary["permissions_enforced"] == 0,
        "codex enforces no read boundary in exec mode — reporting a nonzero count would "
        f"restate the overstatement this field was corrected to end: {boundary}",
    )
    assert_true(
        any("does NOT enforce" in warning for warning in boundary["warnings"]),
        "the non-enforcement must be stated in the warnings a user actually reads, not only "
        f"encoded in a status string: {boundary['warnings']}",
    )
    # The count above can no longer be `len(SECRET_READ_PATTERNS)`: a suffix pattern ships as
    # two globs so the dotfile spelling is covered even if codex's `*` will not cross a
    # leading dot. Assert the expansion itself, or a regression that dropped the second
    # spelling would still satisfy an equality check written against the glob list.
    for canonical, dotted in (
        ("**/*.env", "**/.env"),
        ("**/*.env.*", "**/.env.*"),
        ("**/*.npmrc", "**/.npmrc"),
        ("**/*.git-credentials", "**/.git-credentials"),
        ("**/*credentials.json", "**/.credentials.json"),
    ):
        assert_true(
            canonical in globs and dotted in globs,
            f"codex needs both spellings of the same secret: {canonical} and {dotted} "
            f"— missing from {globs}",
        )
    assert_true(
        "**/.ssh/**" in globs and "**/.kube/**" in globs and "**/.docker/**" in globs,
        f"directory secrets must translate to a dot-anchored recursive deny: {globs}",
    )

    # The secret list lives in core/secret_patterns.py, but opencode's copy of it ships as
    # a static JSON artifact. Nothing at runtime reconciles the two, so a pattern added in
    # one place and not the other would halve the boundary in silence — for whichever
    # provider the author happened not to be thinking about.
    project_json = (
        Path(__file__).resolve().parents[2]
        / "dist"
        / "config"
        / "opencode"
        / "opencode.project.json"
    )
    # Every block that names a path pattern is a way to reach file contents — `read` opens
    # a file, `grep` prints its matching lines — so every such block carries the whole list.
    # Checking `read` alone let `grep` fall eight patterns behind: a keystore `read` refused,
    # `grep` would print.
    permission = json.loads(project_json.read_text(encoding="utf-8"))["permission"]
    path_blocks = {name: block for name, block in permission.items() if isinstance(block, dict)}
    assert_true(
        {"read", "grep"} <= set(path_blocks),
        f"opencode.project.json must bound both read and grep: {sorted(path_blocks)}",
    )
    for name, shipped in path_blocks.items():
        denied = {pattern for pattern, verdict in shipped.items() if verdict == "deny"}
        allowed = {pattern for pattern, verdict in shipped.items() if verdict == "allow"}
        assert_true(
            denied == set(SECRET_READ_PATTERNS),
            f"core/secret_patterns.py and opencode.project.json permission.{name} must deny the same set; "
            f"only in code: {sorted(set(SECRET_READ_PATTERNS) - denied)}, "
            f"only in JSON: {sorted(denied - set(SECRET_READ_PATTERNS))}",
        )
        assert_true(
            allowed == set(SECRET_READ_ALLOWLIST),
            f"the permission.{name} allowlist must agree too: {sorted(allowed)} vs {sorted(SECRET_READ_ALLOWLIST)}",
        )

    # The boundary is only real if it is on the argv of BOTH call shapes. A resumed thread
    # that dropped it would leave a hole that opens on the second call of a session.
    from adapters.providers.codex_adapter import CodexAdapter

    adapter = CodexAdapter()
    for resume_id in (None, "01999999-0000-7000-8000-000000000000"):
        argv = adapter._build_args(
            resume_id=resume_id, model=None, cwd=".", last_message=None
        )
        joined = " ".join(argv)
        assert_true(
            f"default_permissions=\"{CODEX_PERMISSION_PROFILE}\"" in argv
            and f"permissions.{CODEX_PERMISSION_PROFILE}.filesystem=" in joined,
            "codex argv must carry the read boundary on "
            f"{'resume' if resume_id else 'fresh'} calls: {joined}",
        )
        assert_true(
            '"**/*.env"="none"' in joined,
            f"the .env deny must survive into argv verbatim: {joined}",
        )
    assert_true(
        bundle["instructions"] == ("AGENTS.md", "AGENTS.md") and not bundle["agents_dir"],
        f"codex ships instructions and no agent roster: {bundle}",
    )
