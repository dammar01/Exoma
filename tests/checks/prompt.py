"""The prompt still asks for the shape the runtime knows how to read back.

`build_prompt` is one end of a contract whose other end is a parser. Nothing connected
them: the executor decides a reply is evidence by looking for markers in
`_EVIDENCE_MARKERS`, the verify path reads a severity-tagged structure, and the prompt
that asks for both was assembled by a function with no test at all. Rename a section
header here and every call starts failing its contract check for a reason no traceback
names.

That failure mode has already shipped once in this repo, in `messages.py`'s case: a
regex rewrite hit prose, four `next_action` lines told users to run `_main().run`, and
211 behavioural assertions passed because none of them read the words. Text is the part
behaviour tests cannot see.

So these assertions compare the prompt against the CONSUMER's own constants rather than
against strings copied from prompt_builder. A test that pastes the expected header only
locks itself: change both and it stays green while the parser breaks.
"""

from exoma.config.roles import ROLE_EXPLORATION, ROLE_REASONING, ROLE_VERIFICATION
import contextlib

from exoma.config.providers import transport_budget
from exoma.core.prompt import prompt_builder
from exoma.config.settings import DEFAULT_MAX_TASK_CHARS
from exoma.core.provider.continuation import _EVIDENCE_MARKERS
from exoma.core.prompt.prompt_builder import build_prompt
from tests.checks.support import assert_true

# Accessor syntax that is code, never prose. Same list as messages.py, applied to text
# this module BUILDS rather than text it stores — a leak can arrive either way.
_EVIDENCE_REPLY = (
    "[EVIDENCE]\n"
    "confidence: high\n"
    "grounded:\n"
    "- entry point at app/main.py [app/main.py:1]\n"
    "[DIGEST]\n"
    "summary: done.\n"
)

_LEAKS = ("_main().", "self.adapter.", "self.opencode.")

# A cmd.exe-bounded argv transport that rewrites newlines, as opencode's was before it moved
# its prompt into an attached file. No shipped provider is this tight any more (agy has
# 32767), but the sizing arithmetic still has to hold for one, so the checks below keep
# exercising it through a registered stand-in rather than losing the coverage with opencode.
_TIGHT_ARGV_PROVIDER = "tight-argv-test"
_TIGHT_ARGV = {"kind": "argv", "limit": 8191, "headroom": 400, "newline_cost": 3, "reserved": 512}
_TIGHT_ARGV_THRESHOLD = _TIGHT_ARGV["limit"] - _TIGHT_ARGV["headroom"]

_BASE = {
    "session_id": "sid-1",
    "project_root": "/repo",
}


def _build(role: str, command: str, **kwargs) -> str:
    return build_prompt(role=role, task="do the thing", command=command, **_BASE, **kwargs)


# Branches whose replies the executor holds to the evidence contract. The gate at
# core/executor.py:267 keys on ROLE, so this list must be derived the same way — a role
# added to that check and not to this one is a contract nobody asks for.
_EVIDENCE_BRANCHES = (
    (ROLE_EXPLORATION, "explore"),
    (ROLE_EXPLORATION, "e2e_spec"),
    (ROLE_REASONING, "plan"),
    (ROLE_REASONING, "analyze"),
)

# What the verify-browser runner parses out of an e2e_spec reply, and the policy the
# scenario validator enforces. A prompt that stops asking for either gets a reply that is
# refused as spec_invalid, or a scenario the user confirms without knowing why it fails.
_E2E_SPEC_TOKENS = (
    "[E2E SPEC]",
    "scenario_json:",
    "source_refs",
    "selector_provenance",
    "selector_candidates",
    "base_url's origin",
    "side_effect",
    "test_environment_required",
    "cleanup",
    "${ENV_NAME}",
    "allowed actions: goto, click, fill, select, press, upload, wait_dom, expect_dom, expect_url, expect_title, probe",
    "spec_uncertainties:",
)

# Branches with their own structured contract, checked by result_shaping rather than by
# the evidence markers.
_STRUCTURED_BRANCHES = (
    (ROLE_VERIFICATION, "verify"),
    (ROLE_VERIFICATION, "sweep"),
)

# Everything else falls through to a terse default that deliberately asks for no
# contract at all — `doctor` and friends are answered in prose and parsed by nobody.
_FALLBACK_BRANCH = (ROLE_VERIFICATION, "doctor")

_ALL_BRANCHES = (*_EVIDENCE_BRANCHES, *_STRUCTURED_BRANCHES, _FALLBACK_BRANCH)


def _test_prompt_contract_blocks() -> None:
    # --- properties every branch shares, whatever contract it carries ---------------
    for role, command in _ALL_BRANCHES:
        prompt = _build(role, command)
        label = f"role={role} command={command}"

        for required in ("[WORKFLOW_AGENT]", f"command: {command}", f"role: {role}"):
            assert_true(
                required in prompt,
                f"{label}: header lost {required!r} — the reply's provenance is read "
                "back from these lines",
            )

        assert_true(
            "[TASK]" in prompt and "do the thing" in prompt,
            f"{label}: the task itself did not survive into the prompt",
        )

        for leak in _LEAKS:
            assert_true(
                leak not in prompt,
                f"{label}: internal accessor syntax {leak!r} reached the prompt",
            )

    # --- the evidence roles must ask for what the executor will demand back ---------
    for role, command in _EVIDENCE_BRANCHES:
        prompt = _build(role, command)
        label = f"role={role} command={command}"
        lowered = prompt.lower()
        assert_true(
            any(marker in lowered for marker in _EVIDENCE_MARKERS),
            f"{label}: prompt asks for no section in core.provider.continuation._EVIDENCE_MARKERS, so "
            "a perfectly obedient reply would be rejected as conversation",
        )
        assert_true(
            "[DIGEST]" in prompt,
            f"{label}: no [DIGEST] requested — the executor's continuation path keys on "
            "that marker and would ask for a block it never asked for first",
        )

    # --- e2e_spec asks for the spec block and states the scenario policy -------------
    spec_prompt = _build(ROLE_EXPLORATION, "e2e_spec")
    for token in _E2E_SPEC_TOKENS:
        assert_true(
            token in spec_prompt,
            f"e2e_spec prompt lost {token!r} — the runner parses or enforces it, so the "
            "reply would be refused without the provider ever being told why",
        )
    explore_prompt = _build(ROLE_EXPLORATION, "explore")
    assert_true(
        "[E2E SPEC]" not in explore_prompt and "side_effect" not in explore_prompt,
        "the browser-spec contract leaked into plain explore",
    )

    # --- the structured branches carry a digest too ---------------------------------
    for role, command in _STRUCTURED_BRANCHES:
        prompt = _build(role, command)
        assert_true(
            "[DIGEST]" in prompt,
            f"role={role} command={command}: no [DIGEST] requested, so a reply that "
            "stalls mid-answer cannot be told from one that finished",
        )

    # --- and the fallback deliberately carries neither -------------------------------
    fallback = _build(*_FALLBACK_BRANCH)
    assert_true(
        "[DIGEST]" not in fallback and "[OUTPUT_FORMAT]" not in fallback,
        "the terse fallback grew a contract. It is reached by commands the executor "
        "never contract-checks (the gate at core/executor.py keys on role), so asking "
        "for a structure here promises a check that does not exist",
    )


def _test_verify_branch_carries_routing_contract() -> None:
    """The severity/origin/scope triple is the verify contract, not decoration."""
    prompt = _build(ROLE_VERIFICATION, "verify")
    for token in ("severity:", "origin:", "scope_relation:", "blocking_findings", "escalations"):
        assert_true(
            token in prompt,
            f"verify prompt lost {token!r} — result_shaping routes findings by these tags, "
            "and a reply that was never asked for them cannot be routed at all",
        )

    # The terse fallback must NOT carry it: asking every command for a severity table
    # would make `doctor` answer in a shape nothing reads.
    fallback = _build(ROLE_VERIFICATION, "doctor")
    assert_true(
        "blocking_findings" not in fallback,
        "the terse fallback grew the verify contract — every non-verify command would "
        "start returning a structure no caller parses",
    )


def _test_permitted_tools_line() -> None:
    with_tools = _build(ROLE_EXPLORATION, "explore", declared_tools=["read", "grep"])
    assert_true(
        "permitted_tools: read, grep" in with_tools,
        "declared_tools did not reach the prompt — the per-command tool policy is a "
        "declaration, and a declaration nobody is shown declares nothing",
    )

    without = _build(ROLE_EXPLORATION, "explore")
    assert_true(
        "permitted_tools:" not in without,
        "an empty tool policy still emitted the line, which reads as 'permitted: nothing' "
        "rather than 'unspecified'",
    )


def _test_task_cap_is_visible_in_and_out_of_band() -> None:
    sink: dict = {}
    long_task = "x" * (DEFAULT_MAX_TASK_CHARS + 500)
    # An argv transport, because that is now the only kind with a cap to report. A
    # transport that never touches the command line is uncapped by default, which the
    # check below asserts separately — passing no transport here would exercise that path
    # instead and this case would silently stop being covered.
    argv_transport = {
        "kind": "argv",
        "limit": DEFAULT_MAX_TASK_CHARS,
        "newline_cost": 0,
    }
    with _pinned_platform(True):
        prompt = build_prompt(
            role=ROLE_EXPLORATION,
            task=long_task,
            command="explore",
            meta_sink=sink,
            transport=argv_transport,
            **_BASE,
        )

    assert_true(
        sink.get("task_truncated") is True,
        "an over-cap task was truncated without telling the caller: main_agent would read "
        "a silently shortened task and never learn detail was dropped",
    )
    assert_true(
        sink.get("task_original_chars") == len(long_task)
        and sink.get("task_kept_chars") == sink.get("task_cap"),
        f"truncation reported the wrong sizes: {sink}",
    )
    assert_true(
        "task truncated" in prompt,
        "the in-band marker is missing, so the second agent cannot tell a cut task from a "
        "complete one",
    )

    short_sink: dict = {}
    with _pinned_platform(True):
        build_prompt(
            role=ROLE_EXPLORATION,
            task="short",
            command="explore",
            meta_sink=short_sink,
            transport=argv_transport,
            **_BASE,
        )
    assert_true(
        "task_truncated" not in short_sink,
        "a task under the cap reported truncation — a false `task_truncated` sends "
        "main_agent splitting work that was never too long",
    )

    # A transport with no command line to overflow carries the task whole. The cap was
    # protecting argv; applying it to opencode's attached file or codex's stdin cut the
    # two providers that could not overflow anything, at the number chosen for the one
    # that could.
    free_sink: dict = {}
    free_prompt = build_prompt(
        role=ROLE_EXPLORATION,
        task=long_task,
        command="explore",
        meta_sink=free_sink,
        transport={"kind": "file"},
        **_BASE,
    )
    assert_true(
        "task_truncated" not in free_sink
        and free_sink.get("task_cap") is None
        and free_sink.get("task_cap_source") == "unbounded"
        and long_task in free_prompt,
        f"a transport without an argv ceiling must carry the task uncut: {free_sink}",
    )

    # Uncapped is the default, not the only answer: AI_PROXY_MAX_TASK_CHARS_NO_ARGV puts a
    # limit back for anyone who wants one. The setting is read once at import, so the two
    # halves are checked apart — that the variable reaches the setting, and that the
    # setting reaches the prompt — rather than by reloading modules the rest of the suite
    # already holds.
    import os

    from exoma.config import settings as _settings

    saved = os.environ.get("AI_PROXY_MAX_TASK_CHARS_NO_ARGV")
    os.environ["AI_PROXY_MAX_TASK_CHARS_NO_ARGV"] = "1200"
    try:
        assert_true(
            _settings._env_int("AI_PROXY_MAX_TASK_CHARS_NO_ARGV", 0) == 1200,
            "the environment variable does not reach the no-argv cap setting",
        )
    finally:
        if saved is None:
            os.environ.pop("AI_PROXY_MAX_TASK_CHARS_NO_ARGV", None)
        else:
            os.environ["AI_PROXY_MAX_TASK_CHARS_NO_ARGV"] = saved

    policy_sink: dict = {}
    original_no_argv = prompt_builder.DEFAULT_MAX_TASK_CHARS_NO_ARGV
    # Under the hard-truncation ratio, so the cut degrades the task rather than refusing it.
    prompt_builder.DEFAULT_MAX_TASK_CHARS_NO_ARGV = DEFAULT_MAX_TASK_CHARS
    try:
        policy_prompt = build_prompt(
            role=ROLE_EXPLORATION,
            task=long_task,
            command="explore",
            meta_sink=policy_sink,
            transport={"kind": "file"},
            **_BASE,
        )
    finally:
        prompt_builder.DEFAULT_MAX_TASK_CHARS_NO_ARGV = original_no_argv
    assert_true(
        policy_sink.get("task_cap") == DEFAULT_MAX_TASK_CHARS
        and policy_sink.get("task_cap_source") == "policy"
        and policy_sink.get("task_truncated") is True
        and long_task not in policy_prompt,
        f"an opted-in no-argv cap must be enforced and reported as policy: {policy_sink}",
    )


@contextlib.contextmanager
def _pinned_platform(is_windows: bool):
    """Answer the argv-ceiling question as `is_windows`, without lying about the host.

    Patches the one function that asks it. Setting `osutil.IS_WINDOWS` would be shorter and
    is what this did first — and it also sent `runtime_lock` into its Windows branch, which
    `import msvcrt` on the Linux runner. The check that exercises the Executor runs real
    locking code, so the forgery has to stop at the arithmetic it is there to reach.
    """
    original = prompt_builder._argv_limit_enforced
    prompt_builder._argv_limit_enforced = lambda: is_windows
    try:
        yield
    finally:
        prompt_builder._argv_limit_enforced = original


def _assert_serialized_argv_fits() -> None:
    """Every prompt a tight argv provider would build must fit the argv length it measures."""
    for label, task in (
        ("newline-heavy", "line of instruction\n" * 400),
        ("newline dense", "a\n" * 4000),
        ("no newlines", "x" * 40000),
    ):
        for command, role in (
            ("explore", ROLE_EXPLORATION),
            ("plan", ROLE_REASONING),
            ("verify", ROLE_VERIFICATION),
        ):
            built = build_prompt(
                role=role,
                task=task,
                command=command,
                transport=dict(_TIGHT_ARGV),
                **_BASE,
            )
            serialized = len(built.replace("\n", " \\n "))
            assert_true(
                serialized <= _TIGHT_ARGV_THRESHOLD,
                f"a {label} task on {command} produced a command line of {serialized} "
                f"chars, past the {_TIGHT_ARGV_THRESHOLD} the adapter refuses at — the cap "
                "measured the prompt in a unit the transport does not use",
            )


@contextlib.contextmanager
def _registered_tight_argv():
    from exoma.config import providers

    providers.PROVIDER_TRANSPORT[_TIGHT_ARGV_PROVIDER] = dict(_TIGHT_ARGV)
    try:
        yield
    finally:
        providers.PROVIDER_TRANSPORT.pop(_TIGHT_ARGV_PROVIDER, None)


def _assert_executor_sizes_for_long_argv() -> None:
    """A route carrying long config values must never dispatch an oversize command line.

    Asserted through the Executor rather than by recomputing the reserve here. A test that
    does its own arithmetic passes whether or not the runtime performs the same arithmetic,
    which is exactly the gap that lets a measurement be written and never wired up.

    Two outcomes are correct and one is not. The call may be dispatched with a prompt that
    fits, or refused before dispatch because too much of the instruction would be cut —
    both leave the transport intact. What must not happen is a dispatch the adapter then
    rejects, which is what a reserve guessed too low produces: sizing believes it fits,
    the ratio gate sees no large cut because it is reading the same wrong number, and the
    oversize prompt goes out.
    """
    import os
    import tempfile
    from pathlib import Path

    from exoma.core.prompt.router import Router
    from exoma.core.provider.executor import Executor
    from exoma.core.runtime.state import ensure_workflow_workspace

    command_path = "C:/" + "d" * 200 + "/opencode.cmd"
    model = "m" * 1000
    session = "s" * 40
    router = Router(
        {
            "provider": "opencode",
            "provider_command": command_path,
            "provider_agent": "plan",
            "default_model": model,
            "routes": {},
        }
    )
    non_prompt = sum(
        len(str(value)) + 3
        for value in (command_path, "run", "plan", "-m", model, "-s", session)
    )

    def _dispatch(task: str) -> tuple[str, dict]:
        captured: dict = {}

        class _Capture:
            # Named so the executor sizes for the tight argv transport, the way the
            # late-bound adapter would have.
            adapter = _TIGHT_ARGV_PROVIDER

            def run(self, prompt, session, model=None, work_dir=None) -> dict:
                captured["prompt"] = prompt
                return {"ok": True, "content": _EVIDENCE_REPLY, "meta": {}}

        root = Path(tempfile.mkdtemp(prefix="argv-reserve-"))
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
        with _pinned_platform(True), _registered_tight_argv():
            result = Executor(router=router, adapter=_Capture()).execute(
                "explore", task, {"session_id": session}, str(root)
            )
        return captured.get("prompt") or "", result

    # A task the transport has room for must actually go out, or the check below could pass
    # on a harness that never dispatches anything.
    small, _ = _dispatch("map the telemetry")
    assert_true(bool(small), "the executor never reached the adapter, so nothing was sized")

    # And one sized to fill a budget computed from a reserve that ignored the long model.
    # 120 lines is not arbitrary: it is the size at which a reserve that ignores the long
    # model still dispatches (8135 chars, past the 7791 threshold) while a measured one
    # trims the task and stays inside it (7395). Smaller tasks fit under either reserve and
    # would leave this assertion unable to tell them apart.
    prompt, result = _dispatch(("a filler line of instruction text" + chr(10)) * 120)
    if prompt:
        total = len(prompt.replace("\n", " \\n ")) + 3 + non_prompt
        assert_true(
            total <= _TIGHT_ARGV_THRESHOLD,
            f"a long command path and model id produced a {total}-char command line, past "
            f"{_TIGHT_ARGV_THRESHOLD}: the reserve is a guess the runtime never measured",
        )
    else:
        assert_true(
            (result.get("meta") or {}).get("error_type") == "task_truncated",
            "the call was neither dispatched nor refused for truncation, so what stopped "
            f"it is not the sizing this asserts: {result.get('meta')}",
        )


def _test_task_cap_follows_the_provider_transport() -> None:
    """The cap is a property of the transport, not one number shared by every provider.

    One constant for all providers was wrong in both directions at once. An argv provider serialises
    the prompt into argv and pays for every character of scaffolding; codex pipes it through
    stdin and pays for none of it. A single number can only be right for one of them, and it
    was sized for the tighter one — so the looser transport was throwing away instruction it
    had room to carry.

    Pinned to Windows WHOLESALE rather than block by block. Every argv budget below is
    derived from a cmd.exe ceiling that only Windows enforces, so on Linux those same
    assertions read a policy cap and fail — which is exactly what happened: three blocks
    were pinned individually, the first one was missed, and the suite stayed green on the
    only machine that ran it before CI did. A per-block pin is a rule every future block
    has to remember; a function-wide one is a rule it cannot forget. The one deliberate
    exception re-pins itself in the other direction, nested, below.
    """
    # Run twice, once under each ambient value. Both passes are identical when every block
    # sits under the pin, and the second one fails the moment a future block does not —
    # which is the whole failure this had already shipped: an assertion added outside the
    # pin cannot be seen by a suite that only ever runs on one OS.
    for ambient in (True, False):
        with _pinned_platform(ambient):
            _check_task_cap_transport()


def _check_task_cap_transport() -> None:
    # The pin is the FIRST statement and covers the whole body, so the caller's loop over
    # ambient platform values actually tests something: anything added outside this block
    # runs under both, and fails under the one that is not Windows. A per-block pin looks
    # equivalent and is not — the block that shipped broken was simply never wrapped.
    with _pinned_platform(True):
        argv = build_prompt(
            role=ROLE_EXPLORATION,
            task="short",
            command="explore",
            meta_sink=(argv_sink := {}),
            transport=dict(_TIGHT_ARGV),
            **_BASE,
        )
        assert_true(
            argv_sink.get("task_cap_source") == "transport"
            and argv_sink["task_cap"] > DEFAULT_MAX_TASK_CHARS,
            "an argv provider must size the cap from its own command-line room, not from the "
            f"static default: {argv_sink}",
        )
        assert_true(len(argv) > 0, "the probe pass must not consume the prompt it measured")

        # The guarantee is about the SERIALIZED command line, not about Python string length.
        # A newline-rewriting transport turns every newline into ` \n ` in argv, so a cap derived
        # from `len()` passes its own arithmetic and still hands the adapter a prompt it refuses
        # — and only for newline-heavy tasks, which is to say only for the multi-point
        # instructions that most needed the extra room. The assertion below reproduces the
        # adapter's own measurement rather than trusting the cap that produced the prompt.
        # Pinned rather than skipped off Windows. The 8191 ceiling is what this arithmetic
        # exists for, and a check that only runs on the maintainer's laptop is a check the CI
        # box reports as green without having performed.
        _assert_serialized_argv_fits()

        # And the mirror image: those ceilings are cmd.exe's and CreateProcess's. Both adapters
        # decline to enforce them off Windows, so deriving a budget from them there only takes
        # room away for a boundary that is not present.
        with _pinned_platform(False):
            posix_sink: dict = {}
            build_prompt(
                role=ROLE_VERIFICATION,
                task="x" * 50000,
                command="verify",
                meta_sink=posix_sink,
                transport=dict(_TIGHT_ARGV),
                **_BASE,
            )
        assert_true(
            posix_sink.get("task_cap_source") == "unbounded"
            and posix_sink["task_cap"] is None,
            "off Windows the cap must stand down rather than shrink to fit a limit "
            f"nothing enforces there: {posix_sink}",
        )


        # The non-prompt half of the command line is config, not a constant: an absolute
        # provider_command, a model id, an agent name and a session id all live there and all
        # come from files a user edits. A flat reserve is a guess about them, and a guess that
        # is low does not degrade gracefully — it builds a prompt the adapter refuses.
        #
        # Asserted through the Executor rather than by recomputing the formula here. A test
        # that does its own arithmetic passes whether or not the runtime performs the same
        # arithmetic, which is exactly the gap that lets a measurement be written and never
        # wired up.
        _assert_executor_sizes_for_long_argv()

        # Scaffolding that all but fills the transport must SAY so rather than report a
        # plausible-looking cap. The floor is not a budget; it is the point below which
        # capping stops meaning anything and the adapter's oversize check is the real answer.
        floor_sink: dict = {}
        build_prompt(
            role=ROLE_EXPLORATION,
            task="short",
            command="explore",
            meta_sink=floor_sink,
            transport={"kind": "argv", "limit": 1600, "headroom": 0, "newline_cost": 0,
                       "reserved": 0},
            **_BASE,
        )
        assert_true(
            floor_sink.get("task_cap_source") == "floor",
            "a transport whose scaffolding leaves almost no room reported its floor as a "
            f"derived budget, which reads as room that is not there: {floor_sink}",
        )

        # The scaffolding is not a constant either: verify carries a routing contract and a
        # changed-files block that explore does not. Sizing off one number for both hands verify
        # a budget measured against a smaller prompt than the one actually being sent.
        verify_sink: dict = {}
        build_prompt(
            role=ROLE_VERIFICATION,
            task="short",
            command="verify",
            meta_sink=verify_sink,
            transport=dict(_TIGHT_ARGV),
            **_BASE,
        )
        assert_true(
            verify_sink["task_cap"] < argv_sink["task_cap"],
            "verify carries more scaffolding than explore, so it must be left less room for a "
            f"task: verify={verify_sink.get('task_cap')} explore={argv_sink.get('task_cap')}",
        )

        # opencode attaches its prompt as a file (`-f`): no command-line room to size for,
        # and a cap derived from 8191 would cut tasks for a limit its prompt never meets.
        file_sink: dict = {}
        build_prompt(
            role=ROLE_VERIFICATION,
            task="short",
            command="verify",
            meta_sink=file_sink,
            transport=transport_budget("opencode"),
            **_BASE,
        )
        assert_true(
            file_sink.get("task_cap_source") == "unbounded"
            and file_sink["task_cap"] is None,
            f"opencode sends its prompt as a file and has no ceiling to hold: {file_sink}",
        )

        # stdin does not touch argv, so no argv-derived number applies to it — and neither
        # does the policy number, which existed to keep a command line from overflowing.
        # A prompt too long for the provider still fails; it just fails at the provider,
        # naming the real limit, instead of being cut here against a borrowed one.
        stdin_sink: dict = {}
        build_prompt(
            role=ROLE_EXPLORATION,
            task="short",
            command="explore",
            meta_sink=stdin_sink,
            transport=transport_budget("codex"),
            **_BASE,
        )
        assert_true(
            stdin_sink.get("task_cap_source") == "unbounded"
            and stdin_sink["task_cap"] is None,
            f"a stdin provider must hold no argv-derived cap at all: {stdin_sink}",
        )

        # An unregistered provider declares no command line, so there is none to size for.
        # The risk this accepts is a future argv provider added without a transport entry:
        # its prompt would be refused by the adapter's own oversize check instead of cut
        # here — which is the failure this module already prefers, because it fails loudly
        # and names the real limit rather than silently shredding the task first.
        unknown_sink: dict = {}
        build_prompt(
            role=ROLE_EXPLORATION,
            task="short",
            command="explore",
            meta_sink=unknown_sink,
            transport=transport_budget("not-a-provider"),
            **_BASE,
        )
        assert_true(
            unknown_sink.get("task_cap") is None
            and unknown_sink.get("task_cap_source") == "unbounded",
            f"an unknown provider has no declared command line to size for: {unknown_sink}",
        )

        # The whole point of the larger cap: a task an argv provider has room for must survive.
        over_static = "x" * (DEFAULT_MAX_TASK_CHARS + 400)
        kept_sink: dict = {}
        build_prompt(
            role=ROLE_EXPLORATION,
            task=over_static,
            command="explore",
            meta_sink=kept_sink,
            transport=dict(_TIGHT_ARGV),
            **_BASE,
        )
        assert_true(
            "task_truncated" not in kept_sink,
            "a task over the static cap but within the transport's real room was still cut, "
            f"which is the loss this sizing exists to stop: {kept_sink}",
        )


def _test_unknown_role_is_refused() -> None:
    try:
        _build("auditor", "explore")
    except ValueError:
        return
    raise AssertionError(
        "an unknown role built a prompt instead of raising: the role selects the output "
        "contract, so an unrecognised one produces a reply nothing knows how to parse"
    )
