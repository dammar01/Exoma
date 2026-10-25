"""Promoted project knowledge: the durable layer above facts and evidence.

facts.jsonl and evidence.jsonl are runtime memory — capped, pruned, session-scoped,
gitignored. This package owns what survives them: reviewed, production-backed claims
written to Git-tracked JSON in the consumer project.

The split of responsibility with the skill is deliberate. This package holds the parts
that must be deterministic and testable — validation, the staleness ladder, the
reconciliation rules, and the single write path with its gates. Judgement stays in the
skill: what is worth promoting, how many evidence records collapse into one claim, and
which conflict a user has to resolve.
"""
