"""Output contract. Every result is `ok:true` or a structured error.

Shape stays `{ok, content, meta}` (backward compatible). Errors carry
`error_type` + mandatory `next_action` inside `meta`. Optional `digest`
rides at the top level for main_agent to relay.
"""
import re

from core.evidence.fact_store import _FILELINE

ERROR_TYPES = {
    "permission_denied",
    "empty_output",
    "job_already_running",
    "session_capture_failed",
    "invalid_evidence",
    "timeout",
    "command_not_found",
    "routing_error",
    "worker_died",
    # The worker process lived but its own body raised. Distinct from worker_died: nothing
    # needs reaping and no recovery claim applies, the job is simply failed and the stack
    # that ended it is in the worker log. Left as a plain dict it carried no error_type at
    # all, so callers branching on the type read a crash as an unrecognised shape.
    "worker_crashed",
    "worker_stalled",  # PID alive, no progress — probe before judging
    "rate_limited",  # provider refused on quota: waiting fixes it, retrying does not
    "prompt_too_long",  # the shell rejected the command line before opencode ran
    # An argument would have been parsed as shell syntax by cmd.exe (opencode's .cmd shim).
    # Refused before spawning: past that point it is command injection, not an error.
    "unsafe_command_line",
    # The project has no .workflow/second_agent.json, or has one that does not parse. Both
    # used to run on the tool-level file instead, which is how a model nobody chose for
    # the project kept being called. Refused so the fix is named, not absorbed.
    "provider_config_missing",
    "provider_config_invalid",
    # No model resolved for a provider whose CLI then picks one itself (opencode: its last
    # used model). Refused rather than left to whatever that happened to be.
    "model_unset",
    # The provider stream died mid-answer. The opposite advice to rate_limited: this one
    # IS worth retrying, and waiting does nothing for it. Left as `unknown` it collected
    # the useless "inspect the logs and rerun" next_action.
    "streaming_failed",
    # The provider's own OS sandbox refused to start the session (codex on Windows,
    # CASE-015). Nothing ran; the remedy is on the provider's side, and `unknown` hid it
    # behind "inspect the logs and rerun".
    "sandbox_unavailable",
    "second_agent_unavailable",  # probe in a FRESH session could not get an answer either
    "job_expired",  # ran past the hard runtime ceiling (OOM backstop)
    "task_truncated",  # the instruction lost too much to trust the answer to it
    "fact_ingest_failed",
    # A provider/model/effort combination the runtime refused to write. Its own type
    # rather than `unknown`: every case carries an exact remedy (the known providers,
    # the values that model accepts) and `unknown` would bury that under generic advice.
    "invalid_provider_selection",
    "workflow_init_error",
    "workflow_upgrade_error",
    "job_submit_error",
    "worker_capacity",
    "sweep_git_error",
    "runtime_lock",
    # The project-local provider session file would not load (unreadable, or not a session).
    # Raised from main before any job exists; unregistered, it raised out of make_error and
    # a corrupt file produced a traceback instead of the repair instruction.
    "session_state_error",
    # The session hit its configured token ceiling. Its own type rather than `unknown`
    # because the remedy is exact and unlike every other refusal here: nothing is broken,
    # nothing will fix itself by waiting or retrying, and the only next steps are raising
    # the ceiling or starting a fresh session.
    "budget_exceeded",
    # The promote pipeline's refusals. Every one of them is a refusal the user has to read
    # and act on — a candidate that will not parse, a HEAD that is not on the production
    # branch, a statement shaped like a credential — so each carries an exact next_action
    # and none of them belongs under `unknown`. Unregistered, they raised out of
    # make_error and the caller got a traceback where the refusal should have been.
    "promote_input_missing",
    "promote_input_unreadable",
    "promote_existing_unreadable",
    "promote_not_on_branch",  # detached HEAD, mid-rebase, or a repo with no commits
    "promote_not_production_branch",
    "promote_write_rejected",  # store.write refused without naming a type of its own
    # store.write's own refusals, surfaced through promote-write. Separate types rather
    # than one rejection: the remedies differ (fix the document, remove the credential,
    # stop citing an ignored path, point knowledge_dir somewhere Git tracks).
    "invalid_knowledge",
    "secret_shaped_content",
    "ignored_source",
    "ignored_destination",
    # knowledge_dir or a document id that resolves outside the project root.
    "knowledge_path_escape",
    # The plan -> execute chain (core/runtime/execution.py). One type per missing
    # precondition, because each has its own remedy: fix the plan document, answer its
    # questions, ask the user for `-y`, or there is simply no execution to close.
    "plan_input_missing",
    "plan_input_unreadable",
    "plan_invalid",
    "execute_no_plan",
    "execute_plan_not_proceed",
    "execute_not_approved",
    "execution_not_active",
    "unknown",
}

# Required fields per command, checked by validate_fields.
REQUIRED_FIELDS = {
    "explore": ("entry_points", "uncertainties"),
    "analyze": ("grounded", "uncertainties"),
    "plan": ("grounded", "uncertainties"),
    "verify": (
        "verdict",
        "blocking_findings",
        "escalations",
        "notes",
        "checks_run",
        "not_verified",
        "confidence",
    ),
}


def make_ok(content: str, meta: dict | None = None, digest: dict | None = None) -> dict:
    payload = {"ok": True, "content": content or "", "meta": meta or {}}
    if digest is not None:
        payload["digest"] = digest
    return payload


def make_error(error_type: str, message: str, next_action: str, meta: dict | None = None, **fields) -> dict:
    if error_type not in ERROR_TYPES:
        raise ValueError(f"unknown error_type: {error_type}")
    if not next_action or not str(next_action).strip():
        raise ValueError(f"next_action is required for error_type {error_type}")
    merged = dict(meta or {})
    merged["error_type"] = error_type
    merged["next_action"] = next_action
    merged.update(fields)
    return {"ok": False, "content": message or error_type, "meta": merged}


_SUBAGENT_LINE = re.compile(r"^\s*subagents\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_CLUSTER_TAG = re.compile(r"\[c(\d+)\]")
# The honest-fallback line the fan-out instruction asks for when opencode has no spawn tool:
# `subagents: none (no spawn tool; tools: ...)`.
_NO_SPAWN_RE = re.compile(
    r"^\s*subagents\s*:\s*none\b.*no\s+spawn\s+tool", re.IGNORECASE | re.MULTILINE
)
# A refusal is not an absence. `task` can be present and still be denied — opencode
# blocks a read-only primary agent from spawning a write-capable subagent. Left
# undistinguished, that permission wall reads as "opencode cannot fan out" and latches
# the capability off forever, so the one fix that would work is never attempted.
_DENIED_RE = re.compile(
    r"^\s*subagents\s*:\s*none\b.*\b(denied|refus|not\s+permitted|permission)",
    re.IGNORECASE | re.MULTILINE,
)
_DECLINED_RE = re.compile(
    r"^\s*subagents\s*:\s*none\b", re.IGNORECASE | re.MULTILINE
)
# The no-spawn fallback must list the agent's own tools, which makes the claim checkable
# against itself. Observed live: `subagents: none (no spawn tool available; tools: read,
# grep, glob, task, ...)` — `task` named in the same breath as its absence. The agent had
# decided read-only mode forbade spawning and dressed a choice up as a limitation. Taken
# at face value that sentence permanently disables fan-out, so it is checked, not trusted.
_TOOLS_INVENTORY_RE = re.compile(r"tools?\s*:\s*(.+)$", re.IGNORECASE)
_SPAWN_TOOL_NAMES = ("task", "spawn", "subagent", "dispatch_agent")


def _no_spawn_report_is_false(content: str) -> bool:
    """True when a 'no spawn tool' claim lists a spawn tool in its own inventory.

    Reads the WHOLE `subagents:` line, not the no-spawn match: that match ends at the
    words "no spawn tool", and the inventory that contradicts it comes after.
    """
    line = _SUBAGENT_LINE.search(content or "")
    if not line:
        return False
    inventory = _TOOLS_INVENTORY_RE.search(line.group(0))
    if not inventory:
        return False
    names = {
        token.strip().strip("`'\"*").lower()
        for token in re.split(r"[,;/]| and ", inventory.group(1))
    }
    return any(name in names for name in _SPAWN_TOOL_NAMES)

# What actually happened to the fan-out instruction, for meta.fanout_mode.
FANOUT_PARALLEL = "parallel"
FANOUT_DENIED = "denied_by_permission"
FANOUT_INCAPABLE = "incapable"
FANOUT_DECLINED = "declined"
FANOUT_UNREPORTED = "unreported"
FANOUT_MISMATCH = "claimed_unconfirmed"


def reported_no_spawn_tool(content: str) -> bool:
    """True when the second agent explicitly reported it has no sub-agent/spawn tool.

    A capability signal, not a contract miss: it means opencode here cannot fan out, so the
    runtime can stop paying prompt space for a plan it will never run. A report that names
    a spawn tool while denying one does not count — that is a decision, not a limitation.
    """
    return bool(_NO_SPAWN_RE.search(content or "")) and not _no_spawn_report_is_false(content)


def detect_subagent_usage(content: str) -> dict:
    """Did the second agent actually fan out, or just say it did?

    Two independent signals: the declared `subagents:` line and the [cN] tags that
    should appear on merged claims. Agreement is what makes the answer trustworthy —
    a declaration with no tagged claims is a claim of work, not evidence of it.

    `fanout_clusters` and `covered_clusters` are deliberately separate. Claims can be
    cluster-tagged whether or not any sub-agent ran, so collapsing the two produced a
    populated "clusters" field on runs that never fanned out — accurate content under a
    name that read like proof of fan-out.

    Returns {'used', 'fanout_clusters', 'covered_clusters', 'mismatch'}.
    """
    declared: list[str] = []
    match = _SUBAGENT_LINE.search(content or "")
    raw = (match.group(1).strip() if match else "")
    if raw and not raw.lower().startswith("none"):
        declared = sorted({f"c{n}" for n in re.findall(r"c(\d+)", raw)})

    tagged = sorted({f"c{n}" for n in _CLUSTER_TAG.findall(content or "")})
    # A dispatched slice may legitimately return no claim, so declarations may be a
    # strict superset. Every observed claim tag must still belong to a declared slice.
    signals_match = bool(declared) and bool(tagged) and set(tagged) <= set(declared)
    used = signals_match

    # Why fan-out did not happen decides what to do about it, and the three reasons need
    # opposite responses: a permission wall is fixable config, no tool is permanent, a
    # decline is the agent's judgement call. Collapsing them into "not used" threw that
    # away — and an omitted line was indistinguishable from a decline.
    no_spawn = _NO_SPAWN_RE.search(content or "")
    false_report = bool(no_spawn) and _no_spawn_report_is_false(content)
    if used:
        mode = FANOUT_PARALLEL
    elif declared:
        mode = FANOUT_MISMATCH
    elif no_spawn and not false_report:
        mode = FANOUT_INCAPABLE
    elif false_report:
        # It has the tool and did not use it. That is a decline, and it must stay one:
        # only a real absence may switch fan-out off for the whole project.
        mode = FANOUT_DECLINED
    elif _DENIED_RE.search(content or ""):
        mode = FANOUT_DENIED
    elif _DECLINED_RE.search(content or ""):
        mode = FANOUT_DECLINED
    else:
        mode = FANOUT_UNREPORTED

    return {
        "used": used,
        "mode": mode,
        # Clusters a sub-agent was actually dispatched to — empty unless BOTH signals agree.
        "fanout_clusters": declared if used else [],
        # What the agent SAID it dispatched, corroborated or not. Kept separate from
        # fanout_clusters and never blanked: a run that dispatched real sub-agents and then
        # forgot the [cN] tags used to report an empty cluster list beside
        # subagent_used=false, which reads as "no fan-out was attempted" — the one thing
        # that was not true. The reader can now see the claim and the verdict at once.
        "declared_clusters": declared,
        # Clusters the answer draws on, fan-out or not.
        "covered_clusters": tagged,
        "mismatch": bool(declared) and not signals_match,
        # The agent contradicted itself about having a spawn tool. Surfaced rather than
        # silently downgraded: the prompt contract forbids this line, and a run that
        # produced it is worth seeing.
        "false_incapable_report": false_report,
    }


def normalize_output(*, ok: bool, content: str, meta: dict | None = None) -> dict:
    """Legacy shim — kept for callers still passing raw ok/content/meta."""
    return {"ok": ok, "content": content, "meta": meta or {}}


def validate_fields(command: str, content: str) -> list[str]:
    """Return names of required fields missing from an evidence payload."""
    required = REQUIRED_FIELDS.get(command, ())
    lowered = (content or "").lower()
    return [field for field in required if field not in lowered]


_SECTION_HEAD = re.compile(r"^\s*([a-z_]+)\s*:\s*$", re.MULTILINE)


def _section(content: str, name: str) -> list[str]:
    """Bullet lines under a `name:` heading, up to the next heading."""
    lines = (content or "").splitlines()
    out: list[str] = []
    collecting = False
    for line in lines:
        head = _SECTION_HEAD.match(line)
        if head:
            if collecting:
                break
            collecting = head.group(1).lower() == name
            continue
        if collecting and line.strip().startswith("-"):
            out.append(line.strip().lstrip("-").strip())
    return out


_BRACKET_REF = re.compile(r"\[([^\[\]]+)\]")

# Not every bracket is an anchor. Treating them all as one deleted the marker out of
# "replaces the value with [REDACTED:api key]", leaving a sentence that says the opposite
# of what was written and a `refs` list holding the word "REDACTED:api key". A bracket is
# bookkeeping only when its contents READ like a reference.
_REF_SOURCE = r"(?:proof|proxy|ref|source|src|evidence|external)"
_REF_ATOM = re.compile(
    rf"""^(?:{_REF_SOURCE}\s*:\s*)?              # optional source prefix
        [^\s\[\]<>:]+(?:[\\/][^\s\[\]<>:]+)*     # path segments
        \.[A-Za-z0-9_]{{1,12}}                   # extension — a bare word is not a path
        (?:[:#]L?\d+(?:[-:]L?\d+)?)?             # optional :line, :start-end, #Lnn
        $""",
    re.VERBOSE | re.IGNORECASE,
)
# Attribution labels the plan/analysis contract requires. They carry no path but are
# bookkeeping all the same, so they belong beside the claim rather than inside it.
_ATTRIBUTION_REFS = {
    "main_agent-inference",
    "main-agent-inference",
    "user-provided",
    "placeholder",
    "asumsi",
    "assumption",
}


def _is_anchor(inner: str) -> bool:
    """True when bracketed text is bookkeeping, not part of the sentence."""
    body = (inner or "").strip()
    if not body:
        return False
    # A redaction marker is content: it is what the reader is meant to see in place of
    # the secret. Pulling it out of the prose destroys the only trace the value existed.
    if body.upper().startswith("REDACTED"):
        return False
    parts = [part.strip() for part in body.split(",") if part.strip()]
    return bool(parts) and all(
        part.lower() in _ATTRIBUTION_REFS or _REF_ATOM.match(part) for part in parts
    )


def split_claim(claim: str) -> dict:
    """Separate what a claim SAYS from the identifiers that back it.

    The two are written on one line because that is how the agent must emit them — an
    anchor detached from its claim cannot be checked. But a reader drowning in
    `[core/router.py:16]` mid-sentence is reading machine bookkeeping, so hand callers a
    prose `text` and a separate `refs` list rather than making each of them re-derive it.

    Only reference-shaped brackets move. Anything else — a redaction marker, a status
    tag, a markdown link label — stays in `text`, because removing it changes what the
    sentence means.

    Returns {'text', 'refs'}. A claim with no anchors comes back unchanged with refs=[].
    """
    body = claim or ""
    refs: list[str] = []

    def take(match: "re.Match") -> str:
        # `[label](url)` is a markdown link, not an anchor, however path-like the label.
        if body[match.end():match.end() + 1] == "(":
            return match.group(0)
        if not _is_anchor(match.group(1)):
            return match.group(0)
        refs.append(match.group(1).strip())
        return ""

    text = _BRACKET_REF.sub(take, body)
    # Collapse the whitespace and dangling punctuation the removal leaves behind.
    text = re.sub(r"\s{2,}", " ", text).strip().rstrip(" ,;")

    # Not every anchor arrives bracketed, and the prompt is the reason: its `grounded:`
    # line asks for a bare `claim + file:line` while the `durable_facts:` line directly
    # below asks for `[file:line]`. Only the bracketed shape was ever collected, so a run
    # whose every claim named its file could still come back with refs=[] on all of them —
    # the anchors were in the prose, just never lifted out of it. `contract_warnings` has
    # always read the bare shape; this is now literally the same regex, applied where the
    # refs are actually built. It was not: this module kept its own `_FILE_LINE` while the
    # anchor counter used `fact_store._FILELINE`, and the comment claiming they matched was
    # the only thing holding them together. They drifted, and the drift was silent —
    # `evidence_ref.anchors` could read 0 while `grounded_without_evidence` stayed quiet,
    # because each half was asking a different question of a different pattern.
    #
    # Bare anchors are COPIED, not moved. A trailing `[core/router.py:16]` is bookkeeping
    # appended to a finished sentence and can be lifted out cleanly; a bare one is usually
    # load-bearing grammar ("dispatch happens in main.py:48"), and removing it leaves prose
    # that reads like it was truncated. Fallback only: a claim that brackets its anchors
    # has already said which identifiers are bookkeeping, and that answer is better than
    # this one.
    if not refs:
        # `group(0)`, not `findall`: the shared pattern captures path and line separately,
        # so `findall` would hand back tuples where every caller expects `file.py:16`.
        refs = list(dict.fromkeys(m.group(0) for m in _FILELINE.finditer(text)))
    return {"text": text, "refs": refs}


def readable_claims(content: str, section: str = "grounded") -> list[dict]:
    """Every claim in `section` as {'text', 'refs'} — prose first, anchors beside it.

    Detail stays available for audit; it just stops being the thing the eye lands on.
    """
    return [split_claim(claim) for claim in _section(content, section)]


# Commands whose prompt ships a [DIGEST] template. Absent here means absent by design
# (verify carries its own contract, checked by validate_verification_contract).
_DIGEST_COMMANDS = {"explore", "analyze", "plan"}
# `confidence:` is the LAST field of the digest template, so its absence from a digest that
# started is the cheapest available proof that the block never finished.
_DIGEST_TAIL = re.compile(r"^\s*confidence\s*:", re.IGNORECASE | re.MULTILINE)
_CODE_FENCE = re.compile(r"^\s*```", re.MULTILINE)
# `[DIGEST]` opens a section on a line of its own. The same characters also appear INSIDE
# an evidence body whenever the agent quotes the contract it was asked to follow — a task
# ABOUT this workflow names the marker in its findings as a matter of course. A substring
# search cannot tell a section header from a sentence mentioning one, and reading the first
# match let a quoted mention shadow the real block behind it: the reply was judged
# truncated, its merge rejected, and the body it had already earned thrown away.
# `\r` belongs in the trailing class, not just spaces and tabs. In MULTILINE mode `$`
# matches before `\n` and nowhere else, so on a CRLF reply the `\r` sits between the
# marker and the anchor and the header never matches — a valid digest read as a missing
# one, which is exactly the false truncation this helper replaced a substring search to
# avoid. `_SECTION_HEAD` already tolerates it through `\s`; this is the sibling that did
# not.
_DIGEST_HEADER = re.compile(r"^[ \t]*\[DIGEST\][ \t\r]*$", re.MULTILINE)


def digest_split(body: str) -> tuple[str, str] | None:
    """(before, after) around the LAST standalone [DIGEST] header, or None if absent.

    Last rather than first: a body may name the marker several times, and the block that
    closes the reply is the final one. Taking the first match is what made a mention in
    the middle of the evidence stand in for the section at the end of it.
    """
    matches = list(_DIGEST_HEADER.finditer(body or ""))
    if not matches:
        return None
    last = matches[-1]
    return body[: last.start()], body[last.end() :]


# The fields a [DIGEST] block is made of. They are what separates a real digest tail from
# a body that merely names the marker: anything else heading a section after the header
# means the reply went on writing evidence, so the header was a quotation.
_DIGEST_FIELDS = frozenset(
    {
        "summary",
        "key_findings",
        "evidence_basis",
        "risk_level",
        "recommended_next_action",
        "confidence",
    }
)


def digest_trim_point(body: str) -> int | None:
    """Where a truncated [DIGEST] begins, so a continuation can replace it. None if none.

    A digest the reply stopped inside is the LAST thing in that reply. Sections following
    the header mean the reply kept going, and the header was something it quoted — cutting
    there takes every section behind it with it, which is exactly how whole evidence
    bodies were being thrown away.
    """
    parts = digest_split(body or "")
    if parts is None:
        return None
    before, after = parts
    if {name.lower() for name in _SECTION_HEAD.findall(after)} - _DIGEST_FIELDS:
        return None
    return len(before)
# Closers that address the USER. The output is evidence material handed to another program;
# a question at the end of it is a conversational turn that nothing will ever answer. Kept
# to explicit phrases rather than "ends with ?" — a grounded claim may legitimately quote
# one, and a false positive here would cap confidence on a clean run.
_TRAILING_NOISE = re.compile(
    r"(what would you like|how can i help|would you like me to|shall i |let me know"
    r"|specify (a |the )?command|apa yang ingin|mau saya|silakan pilih)",
    re.IGNORECASE,
)
_TRAILING_WINDOW = 400
# Warnings that mean the payload itself is damaged, not merely off-template. The reader has
# to know the difference: an off-template answer is still an answer, a truncated one is a
# fragment wearing ok:true. Membership carries a real penalty — executor caps confidence to
# `low` on any of these — so a signal only belongs here if being wrong about it is rarer
# than being right.
#
# `unbalanced_code_fence` deliberately does NOT qualify, though it is still reported. An odd
# fence count usually does mean a block was left open, but "usually" is the problem: prose
# that quotes a lone ``` , or evidence quoting a document whose fences it only partly
# reproduces, lands here too. It is also redundant — a truncated payload has already lost
# its digest, and the two digest checks say so far more reliably. Paying a cap-to-low on a
# duplicate signal with a false-positive tail buys nothing and misprices clean runs.
STRUCTURAL_KINDS = ("digest_missing", "digest_incomplete")


def _structural_warnings(command: str, body: str) -> list[dict]:
    """Signals that the output stopped early, read off its own template.

    Deliberately structural rather than phrase-matching. A run was observed returning
    ok:true with content cut mid-word — `...hooks not tested in e` — and no digest at all.
    No amount of "does it end with 'still reading'" catches that; a missing terminal
    section does, and costs one substring search.
    """
    warnings: list[dict] = []
    if command in _DIGEST_COMMANDS:
        digest_parts = digest_split(body)
        if digest_parts is None:
            warnings.append(
                {
                    "kind": "digest_missing",
                    "detail": (
                        "no [DIGEST] block — the contract's terminal section never arrived"
                    ),
                }
            )
        else:
            tail = digest_parts[1]
            if not _DIGEST_TAIL.search(tail) or extract_digest(body) is None:
                warnings.append(
                    {
                        "kind": "digest_incomplete",
                        "detail": (
                            "[DIGEST] block is missing its summary/confidence tail — "
                            "output likely truncated mid-block"
                        ),
                    }
                )
    if len(_CODE_FENCE.findall(body)) % 2:
        warnings.append(
            {
                "kind": "unbalanced_code_fence",
                "detail": "odd number of ``` fences — a code block was left open",
            }
        )
    if _TRAILING_NOISE.search(body[-_TRAILING_WINDOW:]):
        warnings.append(
            {
                "kind": "trailing_non_evidence",
                "detail": (
                    "output ends by addressing the user (menu/offer/question) instead of "
                    "closing on evidence"
                ),
            }
        )
    return warnings


def contract_warnings(command: str, content: str) -> list[dict]:
    """Where the second agent's output does not match the contract it was given.

    Warnings only. Nothing here fails a call: the runtime can see the shape of this
    output, but it cannot see whether the CLAIMS are right, and rejecting a usable
    result over a formatting miss trades a real answer for a clean one.

    Scope is honest about what is checkable here. The contracts main_agent owns —
    [OPTIONS], per-claim attribution, the confidence triple, intent detection — are
    written in ITS output, which this process never sees. They stay prompt-only, and
    listing them here would only make enforcement look broader than it is.
    """
    warnings: list[dict] = []
    body = content or ""

    missing = validate_fields(command, body)
    if missing:
        warnings.append(
            {
                "kind": "missing_fields",
                "detail": f"required section(s) absent: {', '.join(missing)}",
            }
        )

    grounded = _section(body, "grounded")
    unbacked = [
        claim
        for claim in grounded
        if claim.lower() not in {"none", "(none)"} and not _FILELINE.search(claim)
    ]
    if unbacked:
        # `grounded` is the one section the prompt defines by its evidence, not by its
        # topic: a claim there without a file:line is an assumption wearing the label
        # that makes main_agent trust it.
        warnings.append(
            {
                "kind": "grounded_without_evidence",
                "detail": f"{len(unbacked)} grounded claim(s) carry no file:line",
                "samples": unbacked[:3],
            }
        )

    warnings.extend(_structural_warnings(command, body))
    return warnings


_VERIFY_VERDICT = re.compile(
    r"^\s*verdict\s*:\s*(DONE|NEEDS\s+FIX|INCOMPLETE)\b", re.IGNORECASE | re.MULTILINE
)
_VERIFY_SECTION_NAMES = (
    "blocking_findings",
    "escalations",
    "notes",
    "checks_run",
    "not_verified",
)
# Whole phrases only. `tidak ada` or `no` followed by any word would also take "tidak ada
# validasi untuk X" and "no check on X", which are findings.
_NONE_WORD = re.compile(
    r"(?:none|\(none\)|n/?a|not applicable|nothing(?: (?:found|to report))?"
    r"|no (?:blocking )?(?:findings?|issues?|escalations?|notes?)"
    r"|tidak ada(?: (?:temuan|masalah|isu|catatan|eskalasi)(?: blocking)?)?)"
    r"(?P<rest>.*)",
    re.IGNORECASE,
)
# The old pattern, word for word: what a first line may say when its item goes on. The
# wider spellings (`none.`, `nothing found`, `no findings`) would read `- none.` followed by
# a finding on the next line as empty.
_NONE_PLAIN = re.compile(
    r"(?:none|\(none\)|n/?a|not applicable)(?:\s*(?P<note>\([^\r\n]*\)))?",
    re.IGNORECASE,
)
_NONE_SEPARATOR = re.compile(r"\s*[;:,—–-]\s*(?P<note>.*)")
_TAG_FIELD = re.compile(r"\b(?:severity|origin|scope_relation)\s*[:=]", re.IGNORECASE)
# A note after a separator is the whole note, from a closed list of neutral phrases. A
# blocklist of problem words was tried and lost: every round of review found another word
# ("issue", "defect", "concern") that let a real finding read as empty. Free explanation
# belongs in parentheses, which the verify prompt asks for; anything else after a separator
# stays a finding, and the continuation asks for `- none`.
_NEUTRAL_NOTE = re.compile(
    r"(?:all\s+)?(?:clean|bersih|ok|okay|fine|passed|lulus|aman"
    r"|nothing (?:found|blocking|to report)|no (?:issues?|findings?|problems?)"
    r"|semua bersih|tidak ada (?:temuan|masalah))[.!]*",
    re.IGNORECASE,
)


class _NoneSentinel:
    """An entry that says "nothing to report" — in the spellings agents actually write.

    The old pattern took only `none` or `none (why)`. Agents also write `none.`,
    `none; all clean`, `none - clean`, `tidak ada`, and every one of those was
    read as an untagged finding: in `blocking_findings` that is a blocking finding, so a
    clean DONE became `fail`, and the continuation then asked for tags on it (CASE-009).

    Still a finding, fail-closed: a sentinel word running straight into text with no
    separator (`none of the callers handle X`), any note carrying tag fields, and a note
    after a separator that is anything but a neutral phrase (`none - caller X fails`,
    `none; all inspected paths are clean`). A parenthetical note is free, as it always was:
    the verify prompt asks for `- none (say why: what you checked)`. Multi-line text is
    never a sentinel.

    Exposes `fullmatch` so callers written against the old compiled pattern keep working.
    """

    def fullmatch_plain(self, text: str) -> bool:
        """The forms the old pattern took: a sentinel alone, or with a parenthetical note.

        For a first line whose item continues on further lines. A separator note there
        would let `none - clean` stand in front of a finding written on the next line.
        """
        match = _NONE_PLAIN.fullmatch((text or "").strip())
        return bool(match) and not _TAG_FIELD.search(match.group("note") or "")

    def fullmatch(self, text: str) -> bool:
        text = (text or "").strip()
        if "\n" in text:
            return False
        match = _NONE_WORD.fullmatch(text)
        if not match:
            return False
        rest = match.group("rest").strip()
        if not rest or re.fullmatch(r"[.!]+", rest):
            return True
        if rest.startswith("(") and rest.endswith(")"):
            return not _TAG_FIELD.search(rest)
        separated = _NONE_SEPARATOR.fullmatch(rest)
        if not separated:
            return False
        return bool(_NEUTRAL_NOTE.fullmatch(separated.group("note").strip()))


_NONE_ITEM = _NoneSentinel()

# Lines the runtime itself writes into a [VERIFICATION] block (core/evidence/verify_tests.py),
# spelled here because this validator is what has to tell them from the agent's own.
# The "no test requested" line stays visible in `checks_run` but is not a check anyone ran:
# counting it turned an agent's `checks_run: - none` into a pass with nothing executed.
# A `not_verified` line with the `tests:` prefix is a gap the runtime found in the test
# request, not one the agent declared, and it never earns the gap-only exit 0.
RUNTIME_NO_TEST_ITEM = "runtime: no test requested"
RUNTIME_GAP_PREFIX = "tests:"

_VERIFY_TAG_VALUES = {
    "severity": {"critical", "high", "medium", "low"},
    "origin": {"introduced", "regression", "pre_existing", "unknown"},
    "scope_relation": {"in_scope", "out_of_scope"},
}


# Horizontal whitespace only. `\s` includes the newline, and under re.MULTILINE a `\s*`
# sitting after the colon walks off the end of the heading line: for an EMPTY section it
# consumed the blank line and `(.*)$` then captured the NEXT heading, so
# `blocking_findings:` with nothing under it came back holding one item — the literal
# string "escalations:". That is the shape a clean verify has, so every honest pass was
# reported as a verdict conflict, and the only spelling of "nothing to report" the parser
# accepted was a literal `- none` line (`_NONE_ITEM`).
_H_SPACE = r"[^\S\n]"


def _section_items(content: str, name: str) -> list[str]:
    pattern = re.compile(
        rf"^{_H_SPACE}*{re.escape(name)}{_H_SPACE}*:{_H_SPACE}*(.*)$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(content or "")
    if not match:
        return []

    items: list[str] = []
    inline = match.group(1).strip()
    if inline and not inline.startswith("#"):
        items.append(inline.lstrip("-").strip())
    section_heads = {*_VERIFY_SECTION_NAMES, "confidence", "verdict"}
    for line in (content or "")[match.end() :].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        heading = re.match(r"^([a-z_]+)\s*:", stripped, re.IGNORECASE)
        if heading and heading.group(1).lower() in section_heads:
            break
        if stripped.startswith("[") and stripped.endswith("]"):
            break
        if stripped.startswith("-"):
            items.append(stripped.lstrip("-").strip())
    return items


def _meaningful_items(items: list[str]) -> list[str]:
    return [item for item in items if item and not _NONE_ITEM.fullmatch(item.strip())]


def _verification_tags(finding: str) -> tuple[dict[str, str], list[str]]:
    tags: dict[str, str] = {}
    invalid: list[str] = []
    lowered = finding.lower()
    for tag, allowed in _VERIFY_TAG_VALUES.items():
        match = re.search(rf"\b{tag}\s*:\s*([a-z_]+)", lowered)
        if not match:
            invalid.append(f"{tag} missing")
            continue
        value = match.group(1)
        tags[tag] = value
        if value not in allowed:
            invalid.append(f"{tag}={value}")
    return tags, invalid


def _expected_verification_section(tags: dict[str, str]) -> str:
    severity = tags.get("severity")
    origin = tags.get("origin")
    scope = tags.get("scope_relation")
    severe = severity in {"critical", "high"}
    if severe and origin in {"introduced", "regression", "unknown"}:
        return "blocking_findings"
    if (severe and origin == "pre_existing") or (
        not severe and origin in {"introduced", "regression"} and scope == "out_of_scope"
    ):
        return "escalations"
    return "notes"


def validate_verification_contract(content: str) -> dict:
    """Derive a fail-closed verdict from delegated verification output."""
    body = content or ""
    verification_body = body
    if "[VERIFICATION]" in body:
        verification_body = body.split("[VERIFICATION]", 1)[1]
    verification_parts = digest_split(verification_body)
    if verification_parts is not None:
        verification_body = verification_parts[0]
    warnings: list[dict] = []
    missing = validate_fields("verify", verification_body)
    if "[VERIFICATION]" not in body:
        missing.insert(0, "[VERIFICATION]")
    for name in _VERIFY_SECTION_NAMES:
        if not re.search(
            rf"^\s*{name}\s*:", verification_body, re.IGNORECASE | re.MULTILINE
        ):
            missing.append(name)
    if missing:
        warnings.append(
            {
                "kind": "missing_fields",
                "detail": (
                    "required verification field(s) absent: "
                    + ", ".join(dict.fromkeys(missing))
                ),
            }
        )

    declared_match = _VERIFY_VERDICT.search(verification_body)
    declared = (
        re.sub(r"\s+", " ", declared_match.group(1).upper())
        if declared_match
        else None
    )
    sections = {
        name: _section_items(verification_body, name)
        for name in _VERIFY_SECTION_NAMES
    }
    checks = [
        item
        for item in _meaningful_items(sections["checks_run"])
        if not item.lower().startswith(RUNTIME_NO_TEST_ITEM)
    ]
    gaps = _meaningful_items(sections["not_verified"])
    runtime_gaps = [item for item in gaps if item.lower().startswith(RUNTIME_GAP_PREFIX)]
    declared_gaps = [item for item in gaps if item not in runtime_gaps]

    for section_name, items in sections.items():
        if section_name != "checks_run" and not items:
            warnings.append(
                {
                    "kind": "empty_section",
                    "detail": f"{section_name} must contain findings or an explicit none",
                }
            )

    # What blocks is decided by the routing table, not by the heading the agent filed the
    # finding under. Reading membership straight off `blocking_findings` made the table
    # one-way: a note-class finding written into that section was flagged `finding_misrouted`
    # and then obeyed anyway, so a run whose findings were all notes still came back a
    # failure. Promotion out of escalations/notes is unchanged — that direction was already
    # right, and is what keeps this fail-closed.
    #
    # The one thing membership is NOT read from the table: a finding already sitting in
    # `blocking_findings` whose tags do not parse. The table cannot route what it cannot
    # read, and an unreadable finding in that section is the last place to start guessing.
    effective_blocking: list[str] = []
    for section_name in ("blocking_findings", "escalations", "notes"):
        for finding in _meaningful_items(sections[section_name]):
            tags, invalid = _verification_tags(finding)
            if invalid:
                warnings.append(
                    {
                        "kind": "invalid_finding_tags",
                        "detail": (
                            f"{section_name} finding has invalid tag(s): {', '.join(invalid)}"
                        ),
                        "sample": finding[:240],
                    }
                )
                if section_name == "blocking_findings":
                    effective_blocking.append(finding)
                continue
            expected_section = _expected_verification_section(tags)
            if expected_section != section_name:
                warnings.append(
                    {
                        "kind": "finding_misrouted",
                        "detail": (
                            f"{section_name} finding belongs in {expected_section} "
                            "under the severity/origin/scope routing table"
                        ),
                        "sample": finding[:240],
                    }
                )
            if expected_section == "blocking_findings":
                effective_blocking.append(finding)

    if not re.search(
        r"^\s*confidence\s*:\s*(low|medium|high)\b",
        verification_body,
        re.IGNORECASE | re.MULTILINE,
    ):
        warnings.append(
            {
                "kind": "invalid_confidence",
                "detail": "verification confidence must be low, medium, or high",
            }
        )

    if declared == "DONE" and effective_blocking:
        warnings.append(
            {
                "kind": "verdict_mismatch",
                "detail": "verdict DONE conflicts with non-empty blocking_findings",
            }
        )
    elif declared == "NEEDS FIX" and not effective_blocking:
        warnings.append(
            {
                "kind": "verdict_mismatch",
                "detail": "verdict NEEDS FIX has no blocking_findings",
            }
        )
    if not checks:
        warnings.append(
            {"kind": "checks_missing", "detail": "checks_run contains no executed check"}
        )
    if declared_gaps:
        warnings.append(
            {
                "kind": "verification_gap",
                "detail": f"not_verified contains {len(declared_gaps)} unchecked area(s)",
            }
        )
    if runtime_gaps:
        warnings.append(
            {
                "kind": "runtime_gap",
                "detail": (
                    f"not_verified contains {len(runtime_gaps)} gap(s) the runtime found in "
                    "the test request (missing, unusable, or a command it refused)"
                ),
            }
        )

    if effective_blocking or declared == "NEEDS FIX":
        verdict = "fail"
    elif declared != "DONE" or warnings:
        verdict = "incomplete"
    else:
        verdict = "pass"

    return {
        "verdict": verdict,
        "declared_verdict": declared,
        "blocking_findings": len(dict.fromkeys(effective_blocking)),
        "checks_run": len(checks),
        "not_verified": len(gaps),
        "warnings": warnings,
    }


# The only warning an agent earns by being honest: `not_verified` is a field the prompt
# asks it to fill, and filling it fires `verification_gap`. The verdict still refuses to
# call that a pass — nothing here relaxes what `pass` means — but the exit status stops
# reading a declared gap as a failed run. `runtime_gap` is deliberately not here: a test
# request that was missing, unusable or refused is the runtime's finding, not an agent's
# honesty, and a verify whose tests never ran is not a clean exit.
_GAP_ONLY_KINDS = frozenset({"verification_gap"})


def verify_exit_status(verdict: str | None, assessment: dict | None = None) -> int:
    """0 for a clean run, or one whose only blemish is a gap the agent declared itself.

    `verdict` wins outright when a caller already has one (quick verify writes its own,
    and it never carries an assessment). The relaxation needs the warning list, so a
    caller without one gets today's behaviour: anything short of `pass` is nonzero.
    """
    if verdict == "pass":
        return 0
    if verdict != "incomplete" or not isinstance(assessment, dict):
        return 2
    if assessment.get("declared_verdict") != "DONE":
        return 2
    warnings = assessment.get("warnings") or []
    if not warnings:
        return 2
    return 0 if all(item.get("kind") in _GAP_ONLY_KINDS for item in warnings) else 2


_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
_RANK_CONFIDENCE = {0: "low", 1: "medium", 2: "high"}


def anchor_cap(
    certified: int, total: int, scanned_all: bool = True
) -> tuple[str, str] | None:
    """What a run's certified-anchor ratio is worth as a confidence ceiling, or None.

    An anchor this project cannot open is a citation, not evidence. The grounded-claims
    warning cannot see the difference: it asks only whether a claim names a file:line, and
    an unopenable one does.

    Priced in rather than only reported. Reporting the ratio and leaving the number alone
    assumed the reader opens the artifact to see which run this was — but the contract
    tells them to read the digest FIRST and open the evidence only on a gap, so a digest
    reading `confidence: high` over two verifiable anchors out of forty is the gap failing
    to announce itself.

    Coarse on purpose: there is no measurement behind a finer ladder, and its shape IS the
    claim it makes. Mostly verified is worth a `medium`; mostly unverified is worth no
    more than `low`; nothing verified keeps the `medium` it always had, because a
    cross-project analysis whose anchors all point outside this root is the ordinary case
    and not a weak analysis. The ratio stays in the digest either way, so a reader can
    disagree with the grade without opening anything.

    `scanned_all=False` means the certifier stopped before the content ended, so `total`
    counts a prefix. A clean prefix is not a clean run — the citation that names a file
    which does not exist can sit past the limit — so it is worth `medium` at most, with
    its own reason. The ratio rungs still apply to the prefix that WAS read, and the
    lower of the two wins.
    """
    if total <= 0:
        return None
    if certified <= 0:
        return "medium", "no cited anchor could be verified in this project"
    if certified >= total:
        if not scanned_all:
            return "medium", f"anchor scan stopped after {total} citations; the rest were not checked"
        return None
    if certified / total < 0.5:
        return "low", f"only {certified} of {total} cited anchors could be verified in this project"
    return "medium", f"{total - certified} of {total} cited anchors could not be verified in this project"


def cap_confidence(digest: dict | None, reasons: list[tuple[str, str]]) -> dict | None:
    """Lower a reported confidence to what the run's own conditions support.

    The second agent grades its own confidence, and it grades on the analysis it did —
    not on whether the inputs to that analysis were sound. A stale dependency graph, a
    truncated instruction, or a `grounded` section with no file:line all leave the prose
    just as assured as a clean run. Every one of those signals is already computed
    elsewhere in this pipeline; the only missing step was letting them reach the number
    main_agent actually reads.

    `reasons` is a list of (cap_level, why). The lowest cap wins. The original value is
    kept as `confidence_reported` so the downgrade is auditable, never silent.

    Callable more than once on the same digest. Some signals are only known after the
    result has been archived — how many of its anchors could actually be certified, for
    one — and those arrive too late to join the first pass. So `confidence_reported` is
    written once and never overwritten by an already-capped value, and each pass appends
    its reasons instead of replacing them. Without both, a later cap would report the
    earlier cap as the second agent's own number and erase why the first one fired.
    """
    if not digest:
        return digest
    reported = (digest.get("confidence") or "unknown").lower()
    if reported not in _CONFIDENCE_RANK or not reasons:
        return digest

    ceiling = min(_CONFIDENCE_RANK[level] for level, _ in reasons if level in _CONFIDENCE_RANK)
    if _CONFIDENCE_RANK[reported] <= ceiling:
        # Already at or below this ceiling, but the reason still belongs in the record:
        # main_agent reads `confidence_capped_by` to know what was wrong with the run,
        # not only to explain the number. A run that graded itself `low` and then hit a
        # stale graph used to record neither — the number needed no change, so the reason
        # was dropped with it, and the one signal saying WHY the run was weak never
        # arrived. `confidence_reported` is written here too, so the field means the same
        # thing on both paths: what the second agent said before anything was applied.
        digest.setdefault("confidence_reported", reported)
        _append_cap_reasons(digest, reasons)
        return digest

    digest.setdefault("confidence_reported", reported)
    digest["confidence"] = _RANK_CONFIDENCE[ceiling]
    _append_cap_reasons(digest, reasons)
    return digest


def _append_cap_reasons(digest: dict, reasons: list[tuple[str, str]]) -> None:
    """Add each new reason to `confidence_capped_by`, once.

    `existing` is re-read as the list grows rather than snapshotted: two callers passing
    the same reason in one `reasons` list both cleared a membership test taken before
    either was added, and the record said the same thing twice.
    """
    existing = list(digest.get("confidence_capped_by") or [])
    for _, why in reasons:
        if why not in existing:
            existing.append(why)
    digest["confidence_capped_by"] = existing


def extract_digest(content: str) -> dict | None:
    """Parse a [DIGEST] block from second_agent output. None if absent/unusable.

    Graceful: main_agent falls back to the full content (contract_detail) when
    this returns None.
    """
    import re

    parts = digest_split(content or "")
    if parts is None:
        return None
    block = parts[1]
    # Stop at the next bracketed section, if any.
    block = re.split(r"\n\[[A-Z_]+\]", block, 1)[0]

    def _field(name: str) -> str:
        match = re.search(rf"{name}\s*:\s*(.+)", block, re.IGNORECASE)
        return match.group(1).strip() if match else ""

    summary = _field("summary")
    if not summary:
        return None  # a digest without a summary is not worth relaying

    findings = []
    fm = re.search(r"key_findings\s*:\s*(.*?)(?:\nrisk_level|\nrecommended_next_action|\nconfidence|$)", block, re.IGNORECASE | re.DOTALL)
    if fm:
        for line in fm.group(1).splitlines():
            line = line.strip().lstrip("-").strip()
            if line:
                findings.append(line)

    risk = _field("risk_level").lower()
    return {
        "summary": summary,
        "key_findings": findings[:3],
        "risk_level": risk if risk in {"low", "medium", "high"} else "unknown",
        "recommended_next_action": _field("recommended_next_action"),
        "confidence": _field("confidence").lower() or "unknown",
    }
