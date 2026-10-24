# Research Methodology

`agent-workflow` is developed through an iterative engineering and research process.

The project does not assume that every implementation originates from a single research
paper. Design decisions may emerge from a combination of:

1. engineering observations,
2. literature review,
3. synthesis across multiple sources,
4. AI-assisted discussion,
5. implementation experiments,
6. real-world usage,
7. subsequent evaluation and revision.

## AI-assisted research

AI systems may be used during literature exploration, hypothesis generation, conceptual
synthesis, architecture discussion, implementation, debugging, and experiment
interpretation.

AI-generated reasoning is treated as **research input**, not authoritative evidence.
Claims produced during AI discussion must not automatically become project knowledge.

A claim should be promoted only after it has an identifiable basis such as literature,
observation, implementation evidence, experiment results, or explicit human reasoning.

A source read through an AI system is recorded as such. A literature record states in its
`limitations` who or what read the full text, and which parts were checked against the source
itself; its `relevant_claims` hold only claims checked against the text, with page or section
where the source has them. An AI summary of a paper is not the paper: a summary of a paper
behind a paywall may come from secondary material or the model's training rather than from
the text.

## Literature relationship

Literature may relate to the project in different ways:

- the project directly adapts a mechanism from the source,
- the source independently supports an existing design direction,
- the source challenges an existing assumption,
- the source inspires future work,
- the relationship is indirect or conceptual.

A paper discovered after implementation must not be presented as the original source of
that implementation unless historical evidence supports that claim.

## Real-world cases

Real-world usage may provide valuable evidence, but company-specific information may be
confidential. Public research records therefore use abstraction.

```text
Private:
"The ERP module X in company Y had file A with 2,000 lines."

Public:
"A mature production repository contained large modules
combining data access, business logic, and UI responsibilities."
```

The public record preserves the engineering phenomenon while removing confidential
identity and implementation details.

## Limitations of observations

Real-world observations may be uncontrolled, affected by operator behavior, specific to a
repository, affected by a particular model or provider, affected by prompt formulation, or
limited in sample size.

Such observations are labeled accordingly and are not automatically generalized.

## Evidence tiers

The project is built, used, and mostly evaluated by its maintainer. That invites
confirmation bias: *I built it, I understand it, I use it often, so I see its value.*
Every observation therefore declares its tier, and a lower tier is never presented as a
higher one:

| Tier | Example | What it can support |
| --- | --- | --- |
| `creator_observation` | the maintainer's own sessions | that something can happen |
| `team_observation` | colleagues' use and feedback | that it happens outside the creator's hands |
| `controlled_experiment` | a designed comparison (`EXP-XXX`) | an effect under stated conditions |
| `independent_evaluation` | assessment by people not involved in building it | that the effect holds without the builder's influence |

"The workflow feels better" is not equivalent to "the workflow improves task completion
under controlled conditions".

Tiers map onto a record's `validation_status` ([CONTRACT.md](CONTRACT.md) §14): creator and
team observations can make a record `observed`, never `validated`. `validated`,
`partially_validated`, and `rejected` need a designed evaluation — a `controlled_experiment`
or an `independent_evaluation` — measured against the record's success criteria.

One exception, defined in CONTRACT.md §14 and nowhere else: a decision (`DEC-XXX`) becomes
`validated` on a clean maintainer direct-use run of the decided behavior. That evidence stays
`creator_observation`: it shows the decision works in the maintainer's use, not that it
generalises. The exception does not extend to hypotheses, cases, or any other record type.

## Principles

1. AI discussion is not evidence.
2. A paper's relationship to a design is not the design's origin.
3. Implementation is not validation.
4. Real-world success is not universal proof.
5. Unknown provenance is better than fabricated provenance.
6. More orchestration is not better engineering.
7. The workflow must justify its own cost.
8. Human behavior is part of the agent system.

## Research integrity

The project prefers an incomplete but accurate history over a complete-looking history
built from uncertain reconstruction.

When historical provenance is unavailable:

```yaml
provenance:
  type: unknown
```

is preferred over assigning an unsupported literature source.
