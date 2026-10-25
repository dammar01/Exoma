"""The promoted-knowledge document shape, and the validator that refuses bad ones.

Hand-rolled on purpose. The project carries zero third-party dependencies, and a JSON
Schema library would be the first — for a document with eight fields. The JSON Schema
text below is still emitted alongside the knowledge files so external tooling has a
contract to read; it is documentation, not the enforcement path. `validate()` is.

The other reason it is hand-rolled: core/runtime/config_defaults.py's `validate_config`
reports warnings and never fails, which is right for a config the user can fix in place
and wrong for a document about to be committed to a shared repository. This validator
blocks. Anything it returns stops the write.
"""

import re

SCHEMA_VERSION = 1

CLAIM_TYPES = (
    "behavior",
    "structure",
    "dependency",
    "configuration",
    "invariant",
    "decision",
)

SOURCE_TYPES = ("code", "user")

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Field order for serialisation. A Git-tracked artifact whose key order follows dict
# insertion produces diffs that depend on which code path built the document, so the
# order is stated once, here, and store.py rebuilds every document against it.
DOC_KEYS = (
    "schema_version",
    "id",
    "title",
    "summary",
    "production",
    "anchors",
    "claims",
    "applicability",
)
PRODUCTION_KEYS = ("ref", "verified_commit")
ANCHORS_KEYS = ("paths",)
CLAIM_KEYS = ("id", "type", "statement", "sources")
CODE_SOURCE_KEYS = ("type", "path", "lines", "anchor_hash", "blob_oid", "evidence_ids")
USER_SOURCE_KEYS = ("type", "session_id", "text", "evidence_ids")
LINES_KEYS = ("start", "end")
APPLICABILITY_KEYS = ("excluded_branches",)
EXCLUSION_KEYS = ("pattern", "reason", "affected_claims")

_CODE_REQUIRED = ("type", "path", "lines", "anchor_hash")
_USER_REQUIRED = ("type", "text")


def _errors_for_string(value, where: str) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"{where}: expected a non-empty string"]
    return []


def _errors_for_lines(lines, where: str) -> list[str]:
    if not isinstance(lines, dict):
        return [f"{where}: expected an object with start and end"]
    errors = [f"{where}: unknown key {k!r}" for k in lines if k not in LINES_KEYS]
    for key in LINES_KEYS:
        if key not in lines:
            errors.append(f"{where}: missing {key}")
    start, end = lines.get("start"), lines.get("end")
    if isinstance(start, bool) or not isinstance(start, int) or start < 1:
        errors.append(f"{where}.start: expected an integer >= 1")
    if isinstance(end, bool) or not isinstance(end, int) or end < 1:
        errors.append(f"{where}.end: expected an integer >= 1")
    if isinstance(start, int) and isinstance(end, int) and not isinstance(start, bool):
        if end < start:
            errors.append(f"{where}: end ({end}) is before start ({start})")
    return errors


def _errors_for_id_list(values, where: str) -> list[str]:
    if not isinstance(values, list):
        return [f"{where}: expected an array"]
    errors = []
    for index, value in enumerate(values):
        errors += _errors_for_string(value, f"{where}[{index}]")
    if len(set(map(str, values))) != len(values):
        errors.append(f"{where}: contains duplicates")
    return errors


def _errors_for_source(source, where: str) -> list[str]:
    if not isinstance(source, dict):
        return [f"{where}: expected an object"]
    kind = source.get("type")
    if kind not in SOURCE_TYPES:
        return [f"{where}.type: expected one of {list(SOURCE_TYPES)}, got {kind!r}"]

    allowed = CODE_SOURCE_KEYS if kind == "code" else USER_SOURCE_KEYS
    required = _CODE_REQUIRED if kind == "code" else _USER_REQUIRED
    errors = [f"{where}: unknown key {k!r}" for k in source if k not in allowed]
    for key in required:
        if key not in source:
            errors.append(f"{where}: missing {key}")

    if kind == "code":
        if "path" in source:
            errors += _errors_for_string(source["path"], f"{where}.path")
        if "lines" in source:
            errors += _errors_for_lines(source["lines"], f"{where}.lines")
        if "anchor_hash" in source:
            errors += _errors_for_string(source["anchor_hash"], f"{where}.anchor_hash")
        if "blob_oid" in source:
            errors += _errors_for_string(source["blob_oid"], f"{where}.blob_oid")
    else:
        if "text" in source:
            errors += _errors_for_string(source["text"], f"{where}.text")
        if "session_id" in source:
            errors += _errors_for_string(source["session_id"], f"{where}.session_id")

    if "evidence_ids" in source:
        errors += _errors_for_id_list(source["evidence_ids"], f"{where}.evidence_ids")
    return errors


def _errors_for_claim(claim, where: str) -> list[str]:
    if not isinstance(claim, dict):
        return [f"{where}: expected an object"]
    errors = [f"{where}: unknown key {k!r}" for k in claim if k not in CLAIM_KEYS]
    for key in CLAIM_KEYS:
        if key not in claim:
            errors.append(f"{where}: missing {key}")

    claim_id = claim.get("id")
    if isinstance(claim_id, str) and not SLUG.match(claim_id):
        errors.append(f"{where}.id: {claim_id!r} is not a lowercase-hyphen slug")
    elif not isinstance(claim_id, str):
        errors.append(f"{where}.id: expected a string")

    if claim.get("type") not in CLAIM_TYPES:
        errors.append(
            f"{where}.type: expected one of {list(CLAIM_TYPES)}, got {claim.get('type')!r}"
        )
    if "statement" in claim:
        errors += _errors_for_string(claim["statement"], f"{where}.statement")

    sources = claim.get("sources")
    if not isinstance(sources, list) or not sources:
        # A claim with no source is the exact thing promotion exists to prevent: an
        # assertion in a shared repository that nothing can be traced back to.
        errors.append(f"{where}.sources: expected a non-empty array")
    else:
        for index, source in enumerate(sources):
            errors += _errors_for_source(source, f"{where}.sources[{index}]")
    return errors


def _errors_for_applicability(applicability, claim_ids: set[str]) -> list[str]:
    where = "applicability"
    if not isinstance(applicability, dict):
        return [f"{where}: expected an object"]
    errors = [f"{where}: unknown key {k!r}" for k in applicability if k not in APPLICABILITY_KEYS]
    exclusions = applicability.get("excluded_branches")
    if not isinstance(exclusions, list):
        return errors + [f"{where}.excluded_branches: expected an array"]

    for index, exclusion in enumerate(exclusions):
        spot = f"{where}.excluded_branches[{index}]"
        if not isinstance(exclusion, dict):
            errors.append(f"{spot}: expected an object")
            continue
        errors += [f"{spot}: unknown key {k!r}" for k in exclusion if k not in EXCLUSION_KEYS]
        for key in ("pattern", "reason"):
            if key not in exclusion:
                errors.append(f"{spot}: missing {key}")
            else:
                errors += _errors_for_string(exclusion[key], f"{spot}.{key}")
        pattern = exclusion.get("pattern")
        if isinstance(pattern, str):
            try:
                re.compile(pattern)
            except re.error as exc:
                errors.append(f"{spot}.pattern: not a valid regex ({exc})")
        affected = exclusion.get("affected_claims")
        if affected is not None:
            errors += _errors_for_id_list(affected, f"{spot}.affected_claims")
            if isinstance(affected, list):
                # An exclusion naming a claim that does not exist silently protects
                # nothing, and reads at review time as though it does.
                for claim_id in affected:
                    if isinstance(claim_id, str) and claim_id not in claim_ids:
                        errors.append(f"{spot}.affected_claims: no claim {claim_id!r}")
    return errors


def validate(doc) -> list[str]:
    """Every reason this document must not be written. Empty list means it may be.

    Returns all errors rather than the first: a promote plan is reviewed by a human once,
    and handing back one problem per round trip wastes the review.
    """
    if not isinstance(doc, dict):
        return ["document: expected a JSON object"]

    errors = [f"document: unknown key {k!r}" for k in doc if k not in DOC_KEYS]
    for key in DOC_KEYS:
        if key not in doc:
            errors.append(f"document: missing {key}")

    if doc.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version: this build writes and reads version {SCHEMA_VERSION}, "
            f"got {doc.get('schema_version')!r}"
        )

    doc_id = doc.get("id")
    if not isinstance(doc_id, str):
        errors.append("id: expected a string")
    elif not SLUG.match(doc_id):
        errors.append(f"id: {doc_id!r} is not a lowercase-hyphen slug")

    for key in ("title", "summary"):
        if key in doc:
            errors += _errors_for_string(doc[key], key)

    production = doc.get("production")
    if not isinstance(production, dict):
        errors.append("production: expected an object")
    else:
        errors += [f"production: unknown key {k!r}" for k in production if k not in PRODUCTION_KEYS]
        for key in PRODUCTION_KEYS:
            if key not in production:
                errors.append(f"production: missing {key}")
            else:
                errors += _errors_for_string(production[key], f"production.{key}")
        commit = production.get("verified_commit")
        if isinstance(commit, str) and len(commit.strip()) < 7:
            errors.append("production.verified_commit: too short to identify a commit")

    anchors = doc.get("anchors")
    if not isinstance(anchors, dict):
        errors.append("anchors: expected an object")
    else:
        errors += [f"anchors: unknown key {k!r}" for k in anchors if k not in ANCHORS_KEYS]
        if "paths" not in anchors:
            errors.append("anchors: missing paths")
        else:
            errors += _errors_for_id_list(anchors["paths"], "anchors.paths")

    claims = doc.get("claims")
    claim_ids: set[str] = set()
    if not isinstance(claims, list) or not claims:
        errors.append("claims: expected a non-empty array")
    else:
        seen: list[str] = []
        for index, claim in enumerate(claims):
            errors += _errors_for_claim(claim, f"claims[{index}]")
            if isinstance(claim, dict) and isinstance(claim.get("id"), str):
                seen.append(claim["id"])
        claim_ids = set(seen)
        duplicates = sorted({cid for cid in seen if seen.count(cid) > 1})
        for duplicate in duplicates:
            # Two claims under one id makes in-place revision ambiguous: the next
            # promotion cannot tell which of them it is updating.
            errors.append(f"claims: duplicate claim id {duplicate!r}")

    if "applicability" in doc:
        errors += _errors_for_applicability(doc["applicability"], claim_ids)

    return errors


# Emitted next to the knowledge files for external tooling. Kept in sync by hand with
# `validate()` above — which is the authority; this is the description of it.
JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://agent-workflow.local/schema/promoted-knowledge-v1.json",
    "title": "PromotedKnowledge",
    "type": "object",
    "additionalProperties": False,
    "required": list(DOC_KEYS),
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "id": {"type": "string", "pattern": SLUG.pattern},
        "title": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "minLength": 1},
        "production": {
            "type": "object",
            "additionalProperties": False,
            "required": list(PRODUCTION_KEYS),
            "properties": {
                "ref": {"type": "string", "minLength": 1},
                "verified_commit": {"type": "string", "minLength": 7},
            },
        },
        "anchors": {
            "type": "object",
            "additionalProperties": False,
            "required": list(ANCHORS_KEYS),
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "uniqueItems": True,
                }
            },
        },
        "claims": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/claim"}},
        "applicability": {
            "type": "object",
            "additionalProperties": False,
            "required": list(APPLICABILITY_KEYS),
            "properties": {
                "excluded_branches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["pattern", "reason"],
                        "properties": {
                            "pattern": {"type": "string", "minLength": 1},
                            "reason": {"type": "string", "minLength": 1},
                            "affected_claims": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                                "uniqueItems": True,
                            },
                        },
                    },
                }
            },
        },
    },
    "$defs": {
        "claim": {
            "type": "object",
            "additionalProperties": False,
            "required": list(CLAIM_KEYS),
            "properties": {
                "id": {"type": "string", "pattern": SLUG.pattern},
                "type": {"enum": list(CLAIM_TYPES)},
                "statement": {"type": "string", "minLength": 1},
                "sources": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "oneOf": [
                            {"$ref": "#/$defs/codeSource"},
                            {"$ref": "#/$defs/userSource"},
                        ]
                    },
                },
            },
        },
        "codeSource": {
            "type": "object",
            "additionalProperties": False,
            "required": list(_CODE_REQUIRED),
            "properties": {
                "type": {"const": "code"},
                "path": {"type": "string", "minLength": 1},
                "lines": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(LINES_KEYS),
                    "properties": {
                        "start": {"type": "integer", "minimum": 1},
                        "end": {"type": "integer", "minimum": 1},
                    },
                },
                "anchor_hash": {"type": "string", "minLength": 1},
                "blob_oid": {"type": "string", "minLength": 7},
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "uniqueItems": True,
                },
            },
        },
        "userSource": {
            "type": "object",
            "additionalProperties": False,
            "required": list(_USER_REQUIRED),
            "properties": {
                "type": {"const": "user"},
                "session_id": {"type": "string", "minLength": 1},
                "text": {"type": "string", "minLength": 1},
                "evidence_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "uniqueItems": True,
                },
            },
        },
    },
}
