"""Provider allowlist, budget ceiling, tool policy, and where metadata is allowed to live.

Each control is asserted at the layer that actually enforces it, not at the layer that
describes it. The allowlist is checked through `Router.route()` because refusing at the
route is the only place a refusal still means "no call was made"; the budget is checked
through `Executor.execute()` because a ceiling that only holds when called directly holds
nowhere.

The tool policy is asserted as a DECLARATION, which is what it is. Its assertions are
about the invariant this repo can actually keep — no write tool ever appears — and not
about sandboxing, which belongs to the provider's own permission config.
"""

import json
import shutil
import tempfile
from pathlib import Path

from exoma.core.evidence.contracts import UsageRecord
from exoma.core.policy.governance import (
    FORBIDDEN_TOOLS,
    allowed_providers,
    budget_limit,
    budget_state,
    check_provider,
    tools_for,
)
from exoma.core.prompt.router import Router
from exoma.core.evidence.runtime_io import write_usage_record
from tests.checks.support import assert_true


def _allowlist() -> None:
    shipped = allowed_providers({})
    assert_true(
        "opencode" in shipped,
        "the default allowlist must be the providers this build ships an adapter for",
    )
    assert_true(
        check_provider("opencode", {}) is None,
        "a shipped provider must be allowed by default",
    )
    denial = check_provider("some-unregistered-provider", {})
    assert_true(
        denial is not None and "allowlist" in denial,
        "an unregistered provider must be refused, and the reason must name the allowlist "
        "so the caller can act on it",
    )

    narrowed = allowed_providers({"provider_allowlist": ["codex"]})
    assert_true(
        "opencode" not in narrowed,
        "a project allowlist must be able to narrow the shipped set",
    )
    widened = allowed_providers({"provider_allowlist": ["a-provider-with-no-adapter"]})
    assert_true(
        "a-provider-with-no-adapter" not in widened,
        "an allowlist that can grant access to an unimplemented provider is not an allowlist",
    )

    # Through the router, which is the layer that has to refuse.
    router = Router({"provider": "not-a-real-provider", "provider_command": "x"})
    refused = False
    try:
        router.route("explore")
    except ValueError as exc:
        refused = "allowlist" in str(exc)
    assert_true(
        refused,
        "Router.route() must refuse a disallowed provider; past this point the prompt has "
        "already been handed over and refusing governs nothing",
    )


def _tool_policy() -> None:
    assert_true(
        "bash" in tools_for("verify") and "bash" not in tools_for("explore"),
        "verify needs to run things to check them; exploration does not",
    )
    for tool in FORBIDDEN_TOOLS:
        assert_true(
            tool not in tools_for("explore", {"command_tools": {"explore": [tool, "read"]}}),
            f"a config must not be able to grant '{tool}' — the second agent is read-only "
            "by design, and that is an invariant rather than a preference",
        )
    assert_true(
        tools_for("explore", {"command_tools": {"explore": ["read"]}}) == ["read"],
        "a project must still be able to narrow the default tool set",
    )
    assert_true(
        "declared_tools" in Router().route("explore"),
        "the route must carry the declared tools, or the prompt and audit trail cannot "
        "state what was permitted",
    )


def _budget() -> None:
    assert_true(
        budget_limit({}) is None and budget_limit({"session_token_budget": 0}) is None,
        "the ceiling must be off unless a number was chosen; an unasked-for budget blocks "
        "real work on the first long day",
    )
    rows = [
        UsageRecord(session_id="a", estimated_input_tokens=100, estimated_output_tokens=50),
        UsageRecord(session_id="b", estimated_input_tokens=9000, estimated_output_tokens=9000),
    ]
    state = budget_state(rows, "a", 1000)
    assert_true(
        state["spent"] == 150 and not state["exceeded"],
        "spend must be counted per session — a shared ceiling lets another session's work "
        "block this one",
    )
    assert_true(
        budget_state(rows, "b", 1000)["exceeded"],
        "a session past its ceiling must be reported as exceeded",
    )


def _budget_refuses_through_executor() -> None:
    """The ceiling has to hold on the real entry point, not just in the helper."""
    from exoma.core.provider.executor import Executor
    from exoma.core.runtime.state import ensure_workflow_workspace

    root = Path(tempfile.mkdtemp(prefix="aw-budget-"))
    try:
        ensure_workflow_workspace(root, str(Path("main.py").resolve()))
        config_path = root / ".workflow" / "second_agent.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["session_token_budget"] = 10
        config_path.write_text(json.dumps(config), encoding="utf-8")

        write_usage_record(
            root,
            UsageRecord(
                session_id="sid-budget",
                estimated_input_tokens=99,
                estimated_output_tokens=99,
            ).to_dict(),
        )
        result = Executor().execute(
            "explore",
            "anything",
            {"session_id": "sid-budget"},
            work_dir=str(root),
            workflow_session_id="sid-budget",
        )
        assert_true(
            not result["ok"] and result["meta"]["error_type"] == "budget_exceeded",
            "a session over its ceiling must be refused before dispatch, with its own "
            f"error type; got {result.get('meta', {}).get('error_type')}",
        )
        assert_true(
            "budget" in result["meta"] and result["meta"]["budget"]["token_source"] == "estimated",
            "a refusal computed from chars//4 must say so, or it reads as a billing fact",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _local_first_metadata() -> None:
    """Every stream this runtime writes stays inside the project it describes.

    Telemetry about a codebase is metadata about that codebase. The check is structural —
    resolve each stream path and assert containment — because the failure it guards against
    is a path built from a home directory or a temp dir, which reads perfectly well and is
    wrong in exactly the way nobody notices.
    """
    from exoma.core.evidence.contracts import AUDIT_STREAM_NAME, QUALITY_STREAM_NAME, USAGE_STREAM_NAME
    from exoma.core.evidence.runtime_io import write_audit_record, write_quality_record
    from exoma.core.workspace.workspace_paths import data_dir

    root = Path(tempfile.mkdtemp(prefix="aw-local-")).resolve()
    try:
        write_usage_record(root, UsageRecord(command="explore").to_dict())
        write_audit_record(root, {"command": "explore"})
        write_quality_record(root, {"kind": "tests", "ok": True})
        for name in (USAGE_STREAM_NAME, AUDIT_STREAM_NAME, QUALITY_STREAM_NAME):
            path = (data_dir(root) / name).resolve()
            assert_true(
                path.exists() and root in path.parents,
                f"{name} must be written inside the project it describes, not outside it",
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_governance_controls() -> None:
    _allowlist()
    _tool_policy()
    _budget()
    _budget_refuses_through_executor()
    _local_first_metadata()
