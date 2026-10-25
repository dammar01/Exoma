"""The `[E2E SPEC]` contract: parse it out of stage-1 evidence, validate the scenario.

The section rides INSIDE standard exploration output — `[EVIDENCE]` … `[E2E SPEC]` …
`[DIGEST]` — so the executor's generic evidence guard and its one bounded continuation
apply to stage 1 unchanged. What this module adds is the part that guard cannot see: a
reply that is perfectly good evidence and still carries no scenario to run.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from exoma.core.evidence.e2e.preflight import safe_base_url, same_origin

SPEC_MARKER = "[E2E SPEC]"
_NEXT_MARKERS = ("[DIGEST]", "[VERIFICATION]", "[EVIDENCE]")

ALLOWED_ACTIONS = frozenset(
    {
        "goto",
        "click",
        "fill",
        "select",
        "press",
        "upload",
        "wait_dom",
        "expect_dom",
        "expect_url",
        "expect_title",
        "probe",
    }
)
ASSERTION_ACTIONS = frozenset({"expect_dom", "expect_url", "expect_title"})
# `e2e` targets the project's own `data-e2e` attribute, the same one tagging proposes
# (`tagging.TAG_ATTRIBUTE`). There is deliberately no `testid`: a second test attribute
# meant drafts recommending `data-testid` while the tags this runtime proposes say
# `data-e2e`, and the two never converged.
E2E_ATTRIBUTE = "data-e2e"
SELECTOR_KEYS = frozenset({"role", "name", "label", "e2e", "text", "css"})
# Keys a draft reaches for from habit, with the key to use instead. Named in the error so a
# repair continuation can fix the scenario rather than guess.
RETIRED_SELECTOR_KEYS = {"testid": "e2e", "data-testid": "e2e", "data-e2e": "e2e"}
# `proven`: a selector the browser knowledge store recorded as the one that matched in a
# clean earlier run against this origin (DEC-016), on the step's route under its `within`.
# Valid only when the store backs it there (`proven_here`).
PROVENANCE = frozenset({"source", "existing_test", "runtime_probe", "heuristic", "proven"})
SEVERITIES = frozenset({"blocking", "non_blocking"})
SIDE_EFFECTS = frozenset({"none", "creates_test_data", "modifies_test_data", "deletes_test_data"})
# Plan §10 preference order. A candidate list must not rank a weaker selector above a
# stronger one: the player tries candidates in order, so the order IS the fallback rule.
# Two kinds of candidate may lead the list ahead of it (DEC-016, `leads_rank`): an `e2e`
# selector the codebase names (provenance `source` with a path:line), and a `proven` one.
SELECTOR_RANK = ("role", "label", "e2e", "text", "css")
MAX_SELECTOR_CANDIDATES = 5
# Readiness a step waits for before its action (after navigation, for goto). Declared per
# step from the app's own indicators — a spinner gone, a button enabled, fetched rows shown
# — because `load` and `networkidle` say nothing on a page that polls.
READY_SELECTOR_KEYS = ("hidden", "visible", "enabled")
READY_TEXT_KEYS = ("text", "url")
READY_KEYS = (*READY_SELECTOR_KEYS, *READY_TEXT_KEYS)
MAX_READY_CONDITIONS = 5
# One ladder, three rungs, defined once. The browser guard used to keep its own
# `_SAFE_METHODS` beside this tuple, and the two answered different halves of the same
# question in different files — exactly the split that let the anchor regexes drift apart.
# A rung added here must not be a rung only one caller knows about.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
# Writes that remove something. They are separated from the rest not because deleting is
# technically different from updating, but because it is the one write whose mistake has
# no undo inside the run: a created row can be cleaned up, an edited field can be set
# back, a deleted record is gone. Existing data is what is at risk, and a test scenario
# cannot tell the runtime which records are its own.
DESTRUCTIVE_METHODS = frozenset({"DELETE"})

# Frameworks that route on a form field rather than the HTTP verb: Laravel and Rails both
# send `_method=DELETE` inside a POST, and some stacks read the header instead. The wire
# method is POST in every one of those cases, so a guard that only reads `request.method`
# waves the delete through the door that was opened for ordinary writes.
_METHOD_OVERRIDE_FIELD = "_method"
_METHOD_OVERRIDE_HEADERS = ("x-http-method-override", "x-method-override")
# The field name is the whole question. Not its value, not which method the value names,
# not whether that method outranks the one on the wire — only whether the request carries
# the field at all.
#
# Six rounds of verification were spent on the other design, the one that read the value
# and decided what it meant. Every round closed a real hole and every round left a deeper
# one: a demoted value, an escaped key, a late multipart part, a duplicated field, a
# doubly encoded name, a UTF-16 body. The holes were not carelessness. They came from the
# premise — that this guard can know how someone else's framework reads a request. It
# cannot, and each fix only moved the guess.
#
# So the guess is gone. A request carrying `_method` needs its endpoint approved, whatever
# the field says. That refuses some requests that were never deletes; a POST whose form
# happens to carry the field, or a body that merely spells it, now asks for a yes. That is
# the trade, taken deliberately: the cost is a refusal naming the endpoint, and the thing
# it buys is a rule with no interpretation left in it to get wrong.
_OVERRIDE_TOKEN = re.compile(rf"(?<!\w){_METHOD_OVERRIDE_FIELD}(?!\w)", re.IGNORECASE)
# `\\uXXXX` written out, as a JSON encoder may write any character including the ones that
# spell this field. `{"_method":"DELETE"}` is `_method` to every JSON decoder and is
# nothing at all to a pattern reading the raw text.
_JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def carries_method_override(
    body: str | None, headers: dict | None = None, query: str | None = None
) -> bool:
    """Whether this request carries a method-override field anywhere a backend would find it.

    True is not an accusation that the request is a delete. It says the request cannot be
    judged by its verb alone, which is enough to send it to the approval gate.
    """
    for name in (headers or {}):
        if str(name).lower() in _METHOD_OVERRIDE_HEADERS:
            return True
    for raw_source in (query, body):
        if not raw_source:
            continue
        for source in _decodings(raw_source):
            if _OVERRIDE_TOKEN.search(source):
                return True
    return False


def _decodings(text: str) -> list[str]:
    """`text` as sent, plus each way a backend might read it.

    Every spelling this guard was caught by came from one assumption: that the bytes on the
    wire are the bytes the application parses. They are not. A JSON decoder resolves
    `\\u005f`, a URL decoder resolves `%5F`, and a proxy in front of the app may resolve one
    layer before the framework resolves another. Each variant is produced and read rather
    than guessed between — and since only the NAME is looked for now, this list is the
    entire remaining surface.
    """
    out = [text]
    if "\\u" in text:
        out.append(_unescape_json(text))
    if "%" in text:
        once = unquote(text, errors="replace")
        out.append(once)
        # A second pass, for the layer that decodes what another layer already decoded:
        # `%255Fmethod` becomes `%5Fmethod` becomes `_method`.
        if "%" in once:
            out.append(unquote(once, errors="replace"))
    return out


def _unescape_json(text: str) -> str:
    """`\\uXXXX` sequences resolved, for matching only.

    Never fed back to the application and never parsed as JSON — one substitution over a
    string, so a malformed body costs nothing and yields nothing.
    """

    def one(match: re.Match) -> str:
        try:
            return chr(int(match.group(1), 16))
        except ValueError:  # unreachable for a 4-hex-digit match, cheap to keep honest
            return match.group(0)

    return _JSON_ESCAPE.sub(one, text)


_ACTIONS_WITH_SELECTOR = frozenset({"click", "fill", "select", "press", "upload", "wait_dom", "expect_dom"})
_ACTIONS_WITH_REQUEST = frozenset({"goto", "click", "fill", "select", "press", "upload"})
# `upload.file`: a fixture inside the project, named relative to its root with forward
# slashes. Never absolute, never `..` — the player may only hand the page a file the project
# itself carries, and upload_file_errors checks it still resolves inside the root.
_UPLOAD_FILE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[^\s\\:*?\"<>|]{1,200}$")
_SIDE_EFFECT_FIELDS = ("test_environment_required", "test_data", "no_cleanup_reason")
_STEP_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_REQUEST_PATH = re.compile(r"^/[^\s?#*\\]*$")
# `path[:line[-line]]` relative to the project, or `req:<id>` for a written requirement.
_SOURCE_REF = re.compile(r"^(?:req:\S.{0,198}|[^\s|,:\\]{1,200}(?::\d+(?:-\d+)?)?)$")
# Bounded digits: `int()` refuses a string past Python's digit limit with ValueError, and a
# malformed citation must read as "not cited", not raise during validation.
_SOURCE_LINE_REF = re.compile(r"^[^\s|,:\\]{1,200}:\d{1,7}(?:-\d{1,7})?$")
_REF_PARTS = re.compile(r"^(?P<path>[^:]+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$")
_UNSAFE_URL_CHARS = re.compile(r"[\\\s\x00-\x1f]")
_CLAIM_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
# Anything shaped like a placeholder, so `${e2e_user}` or `${E2E USER}` is named as a
# mistake instead of travelling to the player as literal text.
_ANY_ENV_REF = re.compile(r"\$\{([^}]*)\}")
_JSON_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL | re.IGNORECASE)
_NONE = re.compile(r"^(?:none|n/?a)\b", re.IGNORECASE)


def spec_block(content: str) -> str | None:
    """The text of the `[E2E SPEC]` section, or None when the marker is absent."""
    body = content or ""
    start = body.find(SPEC_MARKER)
    if start < 0:
        return None
    rest = body[start + len(SPEC_MARKER) :]
    end = len(rest)
    for marker in _NEXT_MARKERS:
        pos = rest.find(marker)
        if 0 <= pos < end:
            end = pos
    return rest[:end]


def _section_lines(block: str, name: str) -> list[str]:
    """Bullet items under `name:` up to the next `word:` heading or a code fence."""
    lines = block.splitlines()
    out: list[str] = []
    collecting = False
    for line in lines:
        stripped = line.strip()
        head = re.match(r"^([a-z_]+)\s*:\s*$", stripped)
        if head:
            if collecting:
                break
            collecting = head.group(1) == name
            continue
        if collecting:
            if stripped.startswith("```"):
                break
            if stripped.startswith("-"):
                out.append(stripped.lstrip("-").strip())
    return out


def _pipe_fields(item: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in item.split("|"):
        key, sep, value = part.partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()
    return fields


def parse_spec(content: str) -> dict:
    """Parse stage-1 output. Never raises: shape problems land in `errors`.

    Returns {"present", "claims", "existing_tests", "coverage_gap", "read_only_requests",
    "scenario", "uncertainties", "errors"}. `present` false means the section marker itself
    is missing — the case that earns a targeted continuation rather than a verdict.
    """
    block = spec_block(content)
    out = {
        "present": block is not None,
        "claims": [],
        "existing_tests": [],
        "coverage_gap": [],
        "read_only_requests": [],
        "scenario": None,
        "uncertainties": [],
        "errors": [],
    }
    if block is None:
        out["errors"].append("[E2E SPEC] section missing")
        return out

    for item in _section_lines(block, "claims"):
        if _NONE.match(item):
            continue
        fields = _pipe_fields(item)
        if "id" not in fields:
            out["errors"].append(f"claim without id: {item[:80]}")
            continue
        out["claims"].append(
            {
                "id": fields["id"],
                "severity": fields.get("severity", "blocking"),
                "description": fields.get("description", ""),
                "source_refs": [
                    ref.strip()
                    for ref in fields.get("source_refs", "").split(",")
                    if ref.strip()
                ],
            }
        )
    for item in _section_lines(block, "existing_tests"):
        if _NONE.match(item):
            continue
        fields = _pipe_fields(item)
        if "path" in fields:
            out["existing_tests"].append(
                {
                    "path": fields["path"],
                    "covers": [c.strip() for c in fields.get("covers", "").split(",") if c.strip()],
                    "confidence": fields.get("confidence", "low"),
                }
            )
    out["coverage_gap"] = [
        item for item in _section_lines(block, "coverage_gap") if not _NONE.match(item)
    ]
    for item in _section_lines(block, "read_only_requests"):
        if _NONE.match(item):
            continue
        fields = _pipe_fields(item)
        out["read_only_requests"].append(
            {
                "method": fields.get("method", ""),
                "endpoint": fields.get("endpoint", ""),
                "source_refs": [ref.strip() for ref in fields.get("source_refs", "").split(",") if ref.strip()],
                "reason": fields.get("reason", ""),
            }
        )
    out["uncertainties"] = [
        item for item in _section_lines(block, "spec_uncertainties") if not _NONE.match(item)
    ]

    fence = _JSON_FENCE.search(block)
    if not fence:
        out["errors"].append("scenario_json: no JSON code fence")
        return out
    try:
        scenario = json.loads(fence.group(1))
    except ValueError as exc:
        out["errors"].append(f"scenario_json: invalid JSON ({exc})")
        return out
    if not isinstance(scenario, dict):
        out["errors"].append("scenario_json: top level is not an object")
        return out
    # The prose claims list and the JSON one describe the same thing; the JSON wins
    # because the player reads it, but a claim named only in prose is still a claim.
    if not scenario.get("claims") and out["claims"]:
        scenario["claims"] = list(out["claims"])
    elif isinstance(scenario.get("claims"), list):
        # Models tend to put source_refs in the prose line only. Same id, same claim:
        # the reference is carried over rather than failing the spec on a formality.
        prose_refs = {claim["id"]: claim["source_refs"] for claim in out["claims"] if claim["source_refs"]}
        for claim in scenario["claims"]:
            if isinstance(claim, dict) and not claim.get("source_refs") and claim.get("id") in prose_refs:
                claim["source_refs"] = list(prose_refs[claim["id"]])
    out["scenario"] = scenario
    return out


def e2e_css(value: object) -> str:
    """The CSS attribute selector for an `e2e` selector value, quoted and escaped."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'[{E2E_ATTRIBUTE}="{escaped}"]'


def selector_rank(selector: Mapping) -> int | None:
    """Position of the selector's strongest key in SELECTOR_RANK; None when it has none."""
    for rank, key in enumerate(SELECTOR_RANK):
        if key in selector:
            return rank
    return None


def proven_here(selector: object, policy: Mapping | None, scope: Mapping | None) -> bool:
    """Whether the knowledge store proved `selector` on this step's route under its `within`.

    `scope` is `knowledge.proven_scope(route, within)` for the step. Proven is scoped by
    page: the same selector on another route, or under another container, may name another
    element or none, and its absence there says nothing about the application.
    """
    if not isinstance(scope, Mapping):
        return False
    for item in (policy or {}).get("proven_selectors") or []:
        if (
            isinstance(item, Mapping)
            and item.get("selector") == selector
            and item.get("route") == scope.get("route")
            and item.get("within") == scope.get("within")
        ):
            return True
    return False


def leads_rank(selector: Mapping, provenance: object, policy: Mapping | None, scope: Mapping | None = None) -> bool:
    """Whether this candidate may sit ahead of the usual order (DEC-016).

    A `data-e2e` a developer placed and the draft cites, or a selector the knowledge store
    proved on this step's route and `within` (`scope`). A guess — heuristic, runtime
    probe — never jumps the order.
    """
    if not isinstance(provenance, Mapping):
        return False
    kind = provenance.get("type")
    if kind == "source":
        ref = provenance.get("ref")
        # A path AND a line: the tagging pass and the knowledge anchor both need the line,
        # and a bare path or a `req:` reference names no element.
        if "e2e" not in selector or not isinstance(ref, str) or not _SOURCE_LINE_REF.match(ref.strip()):
            return False
        # And the cited line must really carry that attribute: a well-formed reference to a
        # file that is not there, or a line without the value, is a guess wearing a citation.
        return _cites_e2e((policy or {}).get("project_root"), ref.strip(), str(selector["e2e"]))
    if kind == "proven":
        return proven_here(selector, policy, scope)
    return False


def _cites_e2e(project_root: object, ref: str, value: str) -> bool:
    """Whether `path:line[-line]` inside the project puts `data-e2e="value"` on an element.

    Held to the tagging pass's reading of a cited line (`tagging._OPEN_TAG`, comments,
    template suffixes): the attribute must sit in an opening tag, with exactly this value,
    in a template file, outside an HTML comment. A substring is not enough — `save` must not
    pass on `data-e2e="save-delete"`, on prose, or on a commented-out element.
    """
    from exoma.core.evidence.e2e.tagging import _COMMENTED, _OPEN_TAG, TEMPLATE_SUFFIXES, _inside_open_comment

    if not project_root or not value:
        return False
    parts = _REF_PARTS.match(ref)
    if not parts or not parts.group("start"):
        return False
    try:
        root = Path(str(project_root)).resolve()
        target = (root / parts.group("path")).resolve()
        if root != target and root not in target.parents:
            return False
        if target.suffix.lower() not in TEMPLATE_SUFFIXES:
            return False
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        # A ref with a NUL or another character the filesystem refuses is a malformed
        # citation, not a reason for validation to raise.
        return False
    start = int(parts.group("start"))
    end = int(parts.group("end") or start)
    attribute = re.compile(
        rf"(?<![\w:-]){re.escape(E2E_ATTRIBUTE)}\s*=\s*(?:\"{re.escape(value)}\"|'{re.escape(value)}'|{re.escape(value)}(?=[\s/>]|$))"
    )
    for number in range(max(start, 1), end + 1):
        if number > len(lines):
            break
        line = lines[number - 1]
        if _COMMENTED.search(line) or _inside_open_comment(lines[: number - 1]):
            continue
        if any(attribute.search(match.group("attrs")) for match in _OPEN_TAG.finditer(line)):
            return True
    return False


def _proven_errors(where: str, selector: object, provenance: object, policy: Mapping | None, scope: Mapping | None) -> list[str]:
    """`proven` is a claim about the knowledge store, checked against it on this step's
    route and `within`."""
    if not isinstance(provenance, Mapping) or provenance.get("type") != "proven":
        return []
    if proven_here(selector, policy, scope):
        return []
    route = (scope or {}).get("route")
    return [
        f"{where}: selector_provenance 'proven' names no selector the browser knowledge "
        f"store proved on route '{route}' with this step's `within`; use the provenance the "
        "selector actually has"
    ]


def _selector_errors(where: str, selector: object) -> list[str]:
    if not isinstance(selector, dict) or not selector:
        return [f"{where}: needs a selector object"]
    retired = sorted(key for key in selector if key in RETIRED_SELECTOR_KEYS)
    if retired:
        return [
            f"{where}: selector key '{key}' not allowed; use '{RETIRED_SELECTOR_KEYS[key]}' "
            f"(matches {E2E_ATTRIBUTE}), e.g. {{\"e2e\": \"<value>\"}}"
            for key in retired
        ]
    unknown = set(selector) - SELECTOR_KEYS
    if unknown:
        return [f"{where}: selector keys {sorted(unknown)} not allowed; allowed: {sorted(SELECTOR_KEYS)}"]
    if selector_rank(selector) is None:
        return [f"{where}: selector needs one of {'|'.join(SELECTOR_RANK)} (name only qualifies role)"]
    return []


def _provenance_errors(where: str, provenance: object) -> list[str]:
    if provenance is None:
        return []
    kind = provenance.get("type") if isinstance(provenance, dict) else None
    if kind not in PROVENANCE:
        return [f"{where}: selector_provenance '{kind}' not in {sorted(PROVENANCE)}"]
    ref = provenance.get("ref")
    if ref is not None and (not isinstance(ref, str) or not _SOURCE_REF.match(ref.strip())):
        return [f"{where}: selector_provenance.ref must be `path[:line]`, the source or test the selector comes from"]
    return []


def _ready_errors(where: str, ready: object) -> list[str]:
    if ready is None:
        return []
    if not isinstance(ready, list) or not 1 <= len(ready) <= MAX_READY_CONDITIONS:
        return [f"{where}: ready must list 1..{MAX_READY_CONDITIONS} conditions"]
    errors: list[str] = []
    for index, condition in enumerate(ready):
        at = f"{where}.ready[{index}]"
        if not isinstance(condition, dict) or len(condition) != 1 or next(iter(condition)) not in READY_KEYS:
            errors.append(f"{at}: an object with exactly one of {'|'.join(READY_KEYS)}")
            continue
        key, value = next(iter(condition.items()))
        if key in READY_SELECTOR_KEYS:
            errors.extend(_selector_errors(f"{at}.{key}", value))
        elif not isinstance(value, str) or not value.strip():
            errors.append(f"{at}.{key}: a non-empty string")
    return errors


def _request_errors(where: str, spec: object) -> list[str]:
    """`request`: the write a step is expected to send, matched against what it actually sent."""
    if spec is None:
        return []
    if not isinstance(spec, dict) or set(spec) - {"method", "path"}:
        return [f"{where}: request is an object with method and path only"]
    errors: list[str] = []
    if spec.get("method") not in WRITE_METHODS:
        errors.append(f"{where}: request.method one of {'|'.join(WRITE_METHODS)}")
    path = spec.get("path")
    if not isinstance(path, str) or not _REQUEST_PATH.match(path):
        errors.append(f"{where}: request.path starts with '/', no query, wildcard or whitespace (':name' matches one segment)")
    return errors


def request_path_matches(template: str, path: str) -> bool:
    """`/items/:id` matches `/items/7`; every other segment matches exactly."""
    want = (template or "/").rstrip("/").split("/")
    got = (path or "/").rstrip("/").split("/")
    return len(want) == len(got) and all((w.startswith(":") and bool(g)) or w == g for w, g in zip(want, got))


def _id_errors(where: str, step: Mapping, seen: set[str]) -> list[str]:
    sid = step.get("id")
    if not isinstance(sid, str) or not _STEP_ID.match(sid):
        return [f"{where}: id '{sid}' must be a kebab identifier, stable across edits of the draft"]
    if sid in seen:
        return [f"{where}: duplicate step id '{sid}'"]
    seen.add(sid)
    return []


def step_selectors(step: Mapping) -> list[dict]:
    """The selectors a step may use, in the order the player must try them.

    One shape for both spellings, so the player never re-implements the choice:
    `[{"selector": {...}, "provenance": "source" | ... | None, "ref": "path:line" | None}, ...]`.

    `ref` travels with the candidate rather than being looked up later: which candidate won
    is only known at runtime, and the tagging pass needs the address of THAT one.
    """
    candidates = step.get("selector_candidates")
    if isinstance(candidates, list):
        pairs = [
            (item.get("selector"), item.get("selector_provenance"))
            for item in candidates
            if isinstance(item, dict)
        ]
    else:
        pairs = [(step.get("selector"), step.get("selector_provenance"))]
    return [
        {
            "selector": selector,
            "provenance": (provenance or {}).get("type") if isinstance(provenance, dict) else None,
            "ref": (provenance or {}).get("ref") if isinstance(provenance, dict) else None,
        }
        for selector, provenance in pairs
        if isinstance(selector, dict) and selector
    ]


def navigation_error(url: str, policy: Mapping | None) -> str | None:
    """Why a `goto` target is refused, or None when it may be opened.

    Relative paths resolve against `base_url`, which preflight has already vetted. An
    absolute URL must share that origin, or be listed in `allowed_origins` AND still pass
    the base-URL policy (so a remote origin also needs `allow_remote`). Backslashes and
    whitespace are refused outright: a browser reads `/\\host` as another host even though
    `urlsplit` sees a path.
    """
    policy = policy or {}
    if _UNSAFE_URL_CHARS.search(url):
        return f"url '{url[:80]}' contains a backslash, whitespace, or control character"
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"url '{url[:80]}' does not parse"
    if not parts.scheme and not url.startswith("//"):
        return None
    base = str(policy.get("base_url") or "")
    absolute = url if parts.scheme else f"{urlsplit(base).scheme or 'http'}:{url}"
    target = urlsplit(absolute)
    if target.scheme.lower() not in ("http", "https") or not target.hostname:
        return f"url '{url[:80]}': only http(s) navigation is allowed"
    if base and same_origin(absolute, base):
        return None
    origin = f"{target.scheme}://{target.netloc}".lower()
    allowed = {str(o).lower().rstrip("/") for o in policy.get("allowed_origins") or []}
    if origin not in allowed:
        return f"cross-origin navigation to '{origin}' refused: not base_url's origin and not in config.json e2e.allowed_origins"
    ok, detail = safe_base_url(absolute, policy)
    return None if ok else f"cross-origin navigation to '{origin}' refused: {detail}"


def _side_effect_errors(where: str, step: Mapping, policy: Mapping) -> list[str]:
    effect = step.get("side_effect")
    if "cleanup" in step:
        return [
            f"{where}: cleanup is no longer a description on the step; declare executable steps in "
            "scenario.cleanup, each with cleans: '<this step id>'"
        ]
    if effect is None:
        stray = [field for field in _SIDE_EFFECT_FIELDS if field in step]
        return [f"{where}: {', '.join(stray)} declared without side_effect"] if stray else []
    if effect not in SIDE_EFFECTS:
        return [f"{where}: side_effect '{effect}' not in {sorted(SIDE_EFFECTS)}"]
    if effect == "none":
        return []
    errors = []
    if step.get("test_environment_required") is not True:
        errors.append(f"{where}: side_effect '{effect}' needs test_environment_required: true")
    test_data = step.get("test_data")
    if not isinstance(test_data, dict) or not isinstance(test_data.get("marker"), str) or not test_data["marker"].strip():
        errors.append(f"{where}: side_effect '{effect}' needs test_data.marker, the value that tells this run's data apart from existing data")
    reason = step.get("no_cleanup_reason")
    keeps_created = bool(policy.get("keep_created_data"))
    if reason is not None and ((effect == "creates_test_data" and not keeps_created) or not isinstance(reason, str) or not reason.strip()):
        errors.append(
            f"{where}: no_cleanup_reason is a non-empty string, for modifies/deletes only "
            "(created data is always cleaned up unless settings.keep_created_data is true)"
        )
    if not policy.get("allow_side_effects"):
        errors.append(f"{where}: side_effect '{effect}' refused: settings.allow_side_effects is false")
    return errors


def _step_errors(
    where: str, step: Mapping, policy: Mapping, claim_ids: set[str], *, cleanup: bool, route: str = "/"
) -> tuple[list[str], str | None]:
    """One step's rules. Returns (errors, the claim id it asserts or None).

    A cleanup step shares every rule a test step has, except that it proves the cleanup, not
    a claim, and its write is covered by the side effect of the step it cleans. `route` is
    the path of the last goto before the step, where a `proven` selector must have been
    proven.
    """
    from exoma.core.evidence.e2e.knowledge import proven_scope

    scope = proven_scope(route, step.get("within"))
    errors: list[str] = []
    asserted = None
    action = step.get("action")
    if action not in ALLOWED_ACTIONS:
        return [f"{where}: action '{action}' not allowed"], None
    if action in ASSERTION_ACTIONS:
        cid = step.get("claim_id")
        if cleanup:
            if cid is not None:
                errors.append(f"{where}: a cleanup assertion proves the cleanup, not a claim; drop claim_id")
        elif not cid:
            errors.append(f"{where}: {action} needs a claim_id")
        elif cid not in claim_ids:
            errors.append(f"{where}: claim_id '{cid}' names no claim")
        else:
            asserted = cid
    if action == "goto":
        url = step.get("url")
        if not isinstance(url, str) or not url:
            errors.append(f"{where}: goto needs a url")
        else:
            refused = navigation_error(url, policy)
            if refused:
                errors.append(f"{where}: {refused}")
    if cleanup:
        declared = [field for field in ("side_effect", "cleanup", *_SIDE_EFFECT_FIELDS) if field in step]
        if declared:
            errors.append(f"{where}: a cleanup step declares no {', '.join(declared)} of its own")
    else:
        errors.extend(_side_effect_errors(where, step, policy))
    if action in _ACTIONS_WITH_SELECTOR:
        candidates = step.get("selector_candidates")
        if candidates is not None and "selector" in step:
            errors.append(f"{where}: use selector or selector_candidates, not both")
        elif candidates is not None:
            if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_SELECTOR_CANDIDATES:
                errors.append(f"{where}: selector_candidates must list 1..{MAX_SELECTOR_CANDIDATES} entries")
            else:
                ranks: list[int] = []
                leading = True
                seen: list[dict] = []
                for position, item in enumerate(candidates):
                    at = f"{where}.selector_candidates[{position}]"
                    if not isinstance(item, dict):
                        errors.append(f"{at}: not an object")
                        continue
                    found = _selector_errors(at, item.get("selector"))
                    found += _provenance_errors(at, item.get("selector_provenance"))
                    if not found:
                        found += _proven_errors(at, item["selector"], item.get("selector_provenance"), policy, scope)
                    errors.extend(found)
                    if found:
                        continue
                    if item["selector"] in seen:
                        errors.append(f"{at}: duplicate selector")
                    seen.append(item["selector"])
                    # Candidates that may lead are exempt only while they lead; once the
                    # usual order has started, every later candidate obeys it.
                    if leading and leads_rank(item["selector"], item.get("selector_provenance"), policy, scope):
                        continue
                    leading = False
                    ranks.append(selector_rank(item["selector"]))
                if ranks != sorted(ranks):
                    errors.append(
                        f"{where}: selector_candidates must run strongest first ({' > '.join(SELECTOR_RANK)}); "
                        "only a source-cited e2e or a proven selector may lead ahead of that order"
                    )
        else:
            single = _selector_errors(where, step.get("selector"))
            single += _provenance_errors(where, step.get("selector_provenance"))
            if not single:
                single += _proven_errors(where, step["selector"], step.get("selector_provenance"), policy, scope)
            errors.extend(single)
    if "within" in step:
        if action not in _ACTIONS_WITH_SELECTOR:
            errors.append(f"{where}: within scopes a selector, and {action} has none")
        else:
            errors.extend(_selector_errors(f"{where}.within", step["within"]))
    errors.extend(_ready_errors(where, step.get("ready")))
    if "request" in step:
        if action not in _ACTIONS_WITH_REQUEST:
            errors.append(f"{where}: request names the write an action sends; {action} sends none")
        else:
            errors.extend(_request_errors(where, step.get("request")))
    if action == "fill" and not isinstance(step.get("value"), str):
        errors.append(f"{where}: fill needs a string value")
    elif action == "fill" and not _ENV_REF.search(step["value"]) and _credential_target(step):
        # The prompt forbids literal credentials; this is where that rule stops being a
        # request. A password typed out in the draft is a password written to draft.json,
        # copied into request.json, and read by every stage after it.
        errors.append(
            f"{where}: fill into a password field needs a placeholder ({', '.join('${' + k + '}' for k in _credential_keys())}), "
            "never a literal value"
        )
    if action == "upload" and not (isinstance(step.get("file"), str) and _UPLOAD_FILE.match(step["file"])):
        errors.append(f"{where}: upload needs `file`, a project-relative path with forward slashes and no `..`")
    if action == "expect_url" and not any(k in step for k in ("contains", "equals", "matches")):
        errors.append(f"{where}: expect_url needs contains|equals|matches")
    return errors, asserted


def _cleanup_errors(
    scenario: Mapping, step_ids: dict[str, tuple[int, Mapping]], policy: Mapping, claim_ids: set[str], seen_ids: set[str],
    route: str = "/",
) -> list[str]:
    """scenario.cleanup: executable steps, grouped by the side-effect step each one cleans.
    `route` is where the test steps left the page; a cleanup goto moves it on."""
    errors: list[str] = []
    cleanup = scenario.get("cleanup")
    cleaned: dict[str, list[Mapping]] = {}
    if cleanup is not None and not isinstance(cleanup, list):
        errors.append("cleanup: a list of steps")
        cleanup = []
    for index, step in enumerate(cleanup or []):
        where = f"cleanup[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{where}: not an object")
            continue
        errors.extend(_id_errors(where, step, seen_ids))
        route = _next_route(route, step, policy)
        found, _ = _step_errors(where, step, policy, claim_ids, cleanup=True, route=route)
        errors.extend(found)
        target = step.get("cleans")
        if target not in step_ids:
            errors.append(f"{where}: cleans '{target}' names no step")
        elif step_ids[target][1].get("side_effect") in (None, "none"):
            errors.append(f"{where}: cleans '{target}', a step that declares no side_effect")
        else:
            cleaned.setdefault(str(target), []).append(step)
    for target, group in cleaned.items():
        if not any(s.get("action") in ASSERTION_ACTIONS for s in group):
            errors.append(
                f"cleanup: the steps cleaning '{target}' need an assertion (expect_dom|expect_url|expect_title) "
                "showing the data is gone or restored"
            )
    for sid, (index, step) in step_ids.items():
        effect = step.get("side_effect")
        if effect not in SIDE_EFFECTS or effect == "none":
            continue
        if sid in cleaned:
            if step.get("no_cleanup_reason") is not None:
                errors.append(f"steps[{index}]: has cleanup steps and a no_cleanup_reason; keep one")
            continue
        if effect == "creates_test_data" and not (policy.get("keep_created_data") and step.get("no_cleanup_reason") is not None):
            errors.append(f"steps[{index}]: side_effect '{effect}' needs a cleanup step (scenario.cleanup with cleans: '{sid}')")
        elif step.get("no_cleanup_reason") is None:
            errors.append(f"steps[{index}]: side_effect '{effect}' needs a cleanup step (scenario.cleanup with cleans: '{sid}') or no_cleanup_reason")
    return errors


_CREDENTIAL_FIELD = re.compile(r"password|passwd|passcode|\bpwd\b", re.IGNORECASE)


def _credential_keys() -> tuple[str, ...]:
    from exoma.core.evidence.e2e.request import CREDENTIAL_KEYS

    return CREDENTIAL_KEYS


def _credential_target(step: Mapping) -> bool:
    """Whether a fill step's selector names a password field.

    A heuristic over the selector text only — a user-name field is not detectable this
    way, and does not need to be: the runner scrubs every value the secrets lookup knows,
    whatever field it was typed into. This rule exists for the value the lookup does not
    know yet, a password the draft invented or copied before secrets.json was filled.
    """
    selectors = [step.get("selector")]
    for item in step.get("selector_candidates") or []:
        if isinstance(item, dict):
            selectors.append(item.get("selector"))
    for selector in selectors:
        if isinstance(selector, dict) and any(
            isinstance(value, str) and _CREDENTIAL_FIELD.search(value) for value in selector.values()
        ):
            return True
    return False


def placeholder_errors(scenario: object) -> list[str]:
    """Every placeholder must be a registered credential name, spelled exactly."""
    from exoma.core.evidence.e2e.request import CREDENTIAL_KEYS

    errors: list[str] = []
    seen: set[str] = set()

    def visit(item):
        if isinstance(item, str):
            for name in _ANY_ENV_REF.findall(item):
                if name in CREDENTIAL_KEYS or name in seen:
                    continue
                seen.add(name)
                shown = name[:40]
                if name.upper() in CREDENTIAL_KEYS:
                    errors.append(f"placeholder '${{{shown}}}' must be written '${{{name.upper()}}}': names are case-sensitive")
                else:
                    errors.append(f"placeholder '${{{shown}}}' is not a registered credential ({', '.join(CREDENTIAL_KEYS)})")
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(scenario)
    return errors


def _next_route(route: str, step: Mapping, policy: Mapping) -> str:
    """The route a step runs on: a goto's own path, otherwise the one before it — read the
    way the knowledge store reads a run, so `proven` is checked where it was recorded."""
    if step.get("action") != "goto":
        return route
    from exoma.core.evidence.e2e.knowledge import route_of

    return route_of(step.get("url"), str(policy.get("base_url") or ""))


def validate_scenario(scenario: object, policy: Mapping | None = None, *, covered: set[str] | frozenset = frozenset()) -> list[str]:
    """Structural and safety rules the player relies on. Empty list means runnable.

    `policy` is the request's settings (base_url, allowed_origins, allow_remote,
    allow_side_effects). Without it the strictest reading applies: no absolute
    navigation, no side effects. `covered` names the claims an existing test the project
    will actually run stands in for: those need no assertion, and when every claim is
    covered the scenario may have no steps at all.
    """
    policy = policy or {}
    errors: list[str] = []
    if not isinstance(scenario, dict):
        return ["scenario is not an object"]
    if scenario.get("version") != 1:
        errors.append("version must be 1")

    claims = scenario.get("claims")
    claim_ids: set[str] = set()
    if not isinstance(claims, list) or not claims:
        errors.append("claims: at least one claim is required")
    else:
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                errors.append(f"claims[{index}]: not an object")
                continue
            cid = str(claim.get("id") or "")
            if not _CLAIM_ID.match(cid):
                errors.append(f"claims[{index}]: id '{cid}' is not a kebab identifier")
            elif cid in claim_ids:
                errors.append(f"claims[{index}]: duplicate id '{cid}'")
            claim_ids.add(cid)
            severity = claim.get("severity", "blocking")
            if severity not in SEVERITIES:
                errors.append(f"claims[{index}]: severity '{severity}' not in {sorted(SEVERITIES)}")
            refs = claim.get("source_refs")
            if (
                not isinstance(refs, list)
                or not refs
                or not all(isinstance(ref, str) and _SOURCE_REF.match(ref.strip()) for ref in refs)
            ):
                errors.append(
                    f"claims[{index}]: source_refs must list at least one `path[:line]` or `req:<id>` reference"
                )

    steps = scenario.get("steps")
    asserted: set[str] = set()
    if not isinstance(steps, list):
        errors.append("steps: at least one step is required")
        steps = []
    elif not steps and not (claim_ids and claim_ids <= set(covered)):
        errors.append("steps: at least one step is required (only claims covered by a runnable existing test may go without)")
    seen_ids: set[str] = set()
    step_ids: dict[str, tuple[int, Mapping]] = {}
    route = "/"
    for index, step in enumerate(steps):
        where = f"steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{where}: not an object")
            continue
        id_problems = _id_errors(where, step, seen_ids)
        errors.extend(id_problems)
        if not id_problems:
            step_ids[step["id"]] = (index, step)
        route = _next_route(route, step, policy)
        found, cid = _step_errors(where, step, policy, claim_ids, cleanup=False, route=route)
        errors.extend(found)
        if cid:
            asserted.add(cid)
    errors.extend(_cleanup_errors(scenario, step_ids, policy, claim_ids, seen_ids, route))
    errors.extend(placeholder_errors(scenario))
    unasserted = claim_ids - asserted - set(covered)
    if unasserted and not errors:
        # A claim no assertion points at can never be proven, so a run that ends clean
        # would report pass on a claim nobody tested. Refused up front, not at verdict.
        errors.append(
            f"claims never asserted: {', '.join(sorted(unasserted))} (assert them in steps, or cover them with an "
            "existing test the project runs through config.json e2e.existing_test_command)"
        )
    return errors


def resolve_upload(project_root: Path | str, rel: str) -> Path | None:
    """The fixture an `upload` step names, or None when it is not a file inside the project.

    Resolved (symlinks followed) and compared against the resolved root, so a link that
    points outside the project is refused like a `..` would be.
    """
    if not isinstance(rel, str) or not _UPLOAD_FILE.match(rel):
        return None
    root = Path(project_root).resolve()
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        return None
    return target if target.is_file() else None


def upload_file_errors(scenario: object, project_root: Path) -> list[str]:
    """Every `upload.file` must be an existing file inside the project."""
    errors: list[str] = []
    if not isinstance(scenario, dict):
        return errors
    for section in ("steps", "cleanup"):
        for index, step in enumerate(scenario.get(section) or []):
            if isinstance(step, dict) and step.get("action") == "upload" and resolve_upload(project_root, step.get("file")) is None:
                errors.append(f"{section}[{index}]: upload file '{step.get('file')}' is not a file inside the project")
    return errors


def ground_claims(scenario: object, project_root: Path) -> list[str]:
    """Every file reference in a claim must name a file — and line — that exists (G8).

    `req:<id>` references are requirements, not files, and pass. Validation already fixed
    the reference format; this is the half that needs the project on disk: a claim the
    spec cannot tie to real code is a claim the spec invented.
    """
    errors: list[str] = []
    root = Path(project_root).resolve()
    claims = scenario.get("claims") if isinstance(scenario, dict) else None
    for index, claim in enumerate(claims if isinstance(claims, list) else []):
        if not isinstance(claim, dict):
            continue
        for ref in claim.get("source_refs") or []:
            ref = str(ref).strip()
            if ref.startswith("req:"):
                continue
            problem = _file_ref_problem(ref, root)
            if problem:
                errors.append(f"claims[{index}]: source_ref '{ref[:80]}' {problem}")
    return errors


def _file_ref_problem(ref: str, root: Path) -> str | None:
    """Why `path[:line[-line]]` names no real place in the project, or None."""
    match = _REF_PARTS.match(ref)
    if not match:
        return "is not `path[:line]`"
    target = (root / match.group("path").replace("\\", "/")).resolve()
    if not target.is_relative_to(root):
        return "points outside the project"
    if not target.is_file():
        return "names no file in the project"
    if match.group("start"):
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        try:
            count = len(target.read_bytes().splitlines())
        except OSError:
            count = 0
        if start < 1 or end < start or end > count:
            return f"is outside the file ({count} lines)"
    return None


def validate_read_only_requests(entries: object, project_root: Path) -> list[str]:
    """A proposed read-only POST must name an exact endpoint AND the handler that proves it.

    The proposal is second_agent's word about someone else's code, and admitting it lets
    a request past the write guard. So the reference has to be a real file and line — a
    `req:` requirement is not accepted here, because a requirement says what a request
    should do, not what its handler does.
    """
    from exoma.core.evidence.e2e.request import read_only_request_errors

    if not isinstance(entries, list):
        return ["read_only_requests: not a list"]
    root = Path(project_root).resolve()
    errors: list[str] = []
    for index, item in enumerate(entries):
        where = f"read_only_requests[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: not an object")
            continue
        entry = f"{str(item.get('method') or '').strip()} {str(item.get('endpoint') or '').strip()}"
        errors += [error.replace("settings.allowed_read_only_requests[0]", where) for error in read_only_request_errors([entry])]
        refs = [str(ref).strip() for ref in item.get("source_refs") or [] if str(ref).strip()]
        if not refs:
            errors.append(f"{where}: source_refs must name the handler (`path:line`) that shows the request writes nothing")
        for ref in refs:
            problem = "is a requirement, not the handler's file" if ref.startswith("req:") else _file_ref_problem(ref, root)
            if problem:
                errors.append(f"{where}: source_ref '{ref[:80]}' {problem}")
    return errors


def validate_existing_tests(existing_tests: object, claim_ids: set[str]) -> list[str]:
    """`existing_tests` from the spec: project-relative paths covering known claims."""
    errors: list[str] = []
    if not isinstance(existing_tests, list):
        return ["existing_tests: not a list"]
    for index, test in enumerate(existing_tests):
        where = f"existing_tests[{index}]"
        if not isinstance(test, dict):
            errors.append(f"{where}: not an object")
            continue
        path = str(test.get("path") or "")
        normalized = path.replace("\\", "/")
        if not path or normalized.startswith("/") or re.match(r"^[A-Za-z]:", path) or ".." in normalized.split("/"):
            errors.append(f"{where}: path '{path}' must be project-relative")
        unknown = [cid for cid in test.get("covers") or [] if cid not in claim_ids]
        if unknown:
            errors.append(f"{where}: covers unknown claim(s) {', '.join(unknown)}")
    return errors


def env_references(scenario: object) -> list[str]:
    """Every `${NAME}` placeholder in the scenario, in first-seen order."""
    found: list[str] = []

    def visit(item):
        if isinstance(item, str):
            for name in _ENV_REF.findall(item):
                if name not in found:
                    found.append(name)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(scenario)
    return found


def substitute_env(scenario: object, env: Mapping[str, str]) -> tuple[object, list[str]]:
    """Resolve placeholders from `env`. Returns (resolved, missing names).

    The resolved copy is what the PLAYER gets; it must never be written to a log,
    a prompt, or an artifact — those keep the placeholder form.
    """
    missing: list[str] = []

    def visit(item):
        if isinstance(item, str):
            def sub(match):
                name = match.group(1)
                if name in env:
                    return env[name]
                if name not in missing:
                    missing.append(name)
                return match.group(0)
            return _ENV_REF.sub(sub, item)
        if isinstance(item, dict):
            return {key: visit(value) for key, value in item.items()}
        if isinstance(item, list):
            return [visit(value) for value in item]
        return item

    return visit(scenario), missing


# Why a draft was rejected, as an enum a quality row can count. Ordered: the first rule
# whose pattern the message holds decides, so the specific ones (a fence, a source_ref, a
# password) come before the shapes that also mention a selector or a step. The patterns
# read the validator's own messages above, and a message none of them names is `other` —
# counted, never guessed into a neighbour.
DRAFT_ERROR_CATEGORIES = (
    ("section_missing", ("[e2e spec] section missing",)),
    ("fence_missing", ("no json code fence",)),
    ("json_invalid", ("invalid json", "top level is not an object", "scenario is not an object")),
    ("credential_literal", ("password field needs a placeholder", "placeholder '")),
    ("source_refs", ("source_ref",)),
    ("provenance", ("selector_provenance",)),
    ("selector_shape", ("selector", "within scopes")),
    ("readiness", ("ready",)),
    ("cleanup", ("cleanup", "cleans '", "no_cleanup_reason")),
    ("side_effect", ("side_effect", "request.", "request is", "request names", "allow_side_effects")),
    ("enum", (" not in [", "not allowed", " one of ")),
    ("policy", ("refused", "origin", "allow_remote")),
    ("claims", ("claim",)),
    ("secrets", ("secrets.json", "secrets_profile")),
)
DRAFT_ERROR_CATEGORY_NAMES = (*(name for name, _ in DRAFT_ERROR_CATEGORIES), "structure", "other")
_STRUCTURE_HINTS = ("id '", "duplicate", "needs", "at least one", "must", "not an object", "not a list", "version")


def error_category(message: object) -> str:
    """The DRAFT_ERROR_CATEGORY_NAMES entry one validator or parser message belongs to."""
    text = str(message or "").lower()
    for name, needles in DRAFT_ERROR_CATEGORIES:
        if any(needle in text for needle in needles):
            return name
    if any(hint in text for hint in _STRUCTURE_HINTS):
        return "structure"
    return "other"


def spec_gap(content: str) -> dict | None:
    """Why the stage-1 reply cannot proceed, or None when a scenario is present.

    Two shapes, both answered in the same provider thread. `recoverable`: evidence arrived
    and the section did not, so a continuation asks for the section. `repairable`: the
    section is there but its scenario did not parse (no fence, invalid JSON), so a repair
    asks for the whole section again with the parser's own words. Both used to end the
    draft as `spec_invalid` on the first attempt only for the second shape, which is the
    shape a model most often produces.
    """
    parsed = parse_spec(content)
    if parsed["scenario"] is not None and not parsed["errors"]:
        return None
    if not parsed["present"]:
        return {"reason": "e2e spec section missing", "missing": [SPEC_MARKER], "recoverable": True}
    return {
        "reason": "e2e spec present but unusable",
        "missing": list(parsed["errors"]),
        "recoverable": False,
        "repairable": True,
    }


def spec_continuation_prompt(gap: dict) -> str:
    """The follow-up sent in the same provider thread when only the section is missing.

    Closes with a short `[DIGEST]` on purpose: the reply travels through the same
    evidence guard as any exploration output, and that guard wants one of the evidence
    markers present. A digest is the cheapest one that is also useful.
    """
    return "\n".join(
        [
            "[CONTINUATION]",
            "Your previous reply carried the evidence but no [E2E SPEC] section.",
            "Return ONLY the missing section, in this exact shape, grounded in the evidence you already gathered:",
            *_SPEC_SHAPE,
        ]
    )


def spec_repair_prompt(errors: list[str] | tuple) -> str:
    """The one repair sent in the same provider thread when the section failed validation.

    Quotes the validator's own messages rather than paraphrasing them: they name the field,
    the bad value, and the allowed ones, which is what the model needs to fix the scenario
    instead of rewriting it from scratch. Asks for the WHOLE section, because the runtime
    parses the repair on its own — a partial answer would lose the claims it left out.
    """
    shown = [f"- {error}" for error in list(errors)[:MAX_REPAIR_ERRORS]]
    if len(errors) > MAX_REPAIR_ERRORS:
        shown.append(f"- … {len(errors) - MAX_REPAIR_ERRORS} more of the same kind")
    return "\n".join(
        [
            "[CONTINUATION]",
            "Your [E2E SPEC] section was rejected by the runtime validator:",
            *shown,
            "Fix exactly these problems and return the COMPLETE corrected [E2E SPEC] section, grounded in the evidence you already gathered.",
            "scenario_json must be ONE fenced ```json block holding ONE valid JSON object. Keep every claim and step that was not wrong.",
            *_SPEC_SHAPE,
        ]
    )


MAX_REPAIR_ERRORS = 12
_SPEC_SHAPE = (
            "",
            "[E2E SPEC]",
            "claims:",
            "- id: <kebab-id> | severity: <blocking|non_blocking> | description: <behaviour to prove> | source_refs: <file:line, ...>",
            "",
            "existing_tests:",
            "- path: <test file> | covers: <claim ids> | confidence: <low|medium|high> | none",
            "",
            "coverage_gap:",
            "- <claim ids not covered by existing tests> | none",
            "",
            "read_only_requests:",
            "- method: POST | endpoint: </path or http(s)://host/path> | source_refs: <handler file:line> | reason: <why it writes nothing> | none",
            "",
            "scenario_json:",
            "```json",
            '{"version": 1, "feature": "<name>", "claims": [...], "steps": [{"id": "<kebab-id>", "action": "...", ...}], "cleanup": [...]}',
            "```",
            "",
            "spec_uncertainties:",
            "- <selector or flow you could not ground> | none",
            "",
            "[DIGEST]",
            "summary: <one line: how many claims, what existing tests cover>",
            "confidence: low | medium | high",
)
