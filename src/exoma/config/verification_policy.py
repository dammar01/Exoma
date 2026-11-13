"""Verification routing shared by the runtime and rendered agent guidance.

Tag validation and verdict derivation belong to the evidence contract. This module
only routes findings and renders that same ordered policy; it has no runtime I/O.
"""

from dataclasses import dataclass
from types import MappingProxyType


VERIFICATION_TAG_VALUES = MappingProxyType({
    "severity": frozenset(("critical", "high", "medium", "low")),
    "origin": frozenset(("introduced", "regression", "pre_existing", "unknown")),
    "scope_relation": frozenset(("in_scope", "out_of_scope")),
})


@dataclass(frozen=True)
class VerificationRoute:
    section: str
    severity: tuple[str, ...] = ()
    origin: tuple[str, ...] = ()
    scope_relation: tuple[str, ...] = ()
    exclude_severity: bool = False
    note: str = ""

    def matches(self, tags: dict[str, str]) -> bool:
        severity_matches = not self.severity or tags.get("severity") in self.severity
        if self.exclude_severity:
            severity_matches = not severity_matches
        return (severity_matches
                and (not self.origin or tags.get("origin") in self.origin)
                and (not self.scope_relation or tags.get("scope_relation") in self.scope_relation))

    def guidance(self) -> str:
        if not (self.severity or self.origin or self.scope_relation):
            return f"- anything else → {self.section}"
        severity = self.severity
        if self.exclude_severity:
            severity = tuple(sorted(VERIFICATION_TAG_VALUES["severity"].difference(severity)))
        condition = " + ".join((
            "/".join(self.origin) or "anything",
            "/".join(self.scope_relation) or "anything",
            "|".join(severity) or "anything",
        ))
        suffix = f" ({self.note})" if self.note else ""
        return f"- {condition} → {self.section}{suffix}"


VERIFICATION_ROUTING_RULES = (
    VerificationRoute("blocking_findings", ("critical", "high"), ("introduced", "regression"),
                      note="out_of_scope also reports a scope violation"),
    VerificationRoute("blocking_findings", ("critical", "high"), ("unknown",), note="fail closed"),
    VerificationRoute("escalations", ("critical", "high"), ("pre_existing",)),
    VerificationRoute("escalations", ("critical", "high"), ("introduced", "regression"),
                      ("out_of_scope",), exclude_severity=True),
    VerificationRoute("notes"),
)


def expected_verification_section(tags: dict[str, str]) -> str:
    """Keep routing separate from the caller's fail-closed tag validation."""
    return next(rule.section for rule in VERIFICATION_ROUTING_RULES if rule.matches(tags))


def render_verification_routing() -> str:
    return "\n".join(rule.guidance() for rule in VERIFICATION_ROUTING_RULES)


def render_verification_guidance(text: str) -> str:
    return text.replace("{{VERIFICATION_ROUTING}}", render_verification_routing())
