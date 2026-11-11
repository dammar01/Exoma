# Developing Exoma

For maintainers working on this repository. Using Exoma in a project is covered by the
[installation guide](installation.md) and the [team guide](team-guide/README.md).

## Two repositories, two jobs

| Repository | Role |
| --- | --- |
| **Exoma** (this one) | The core: the runtime, CLI, installer, bundled skills and hooks, and the package published to PyPI. Every runtime change lands here. |
| [agent-workflow](https://github.com/dammar01/agent-workflow) | Research and staging: hypotheses, experiments, decision records, simulation and measurement harnesses, and the place a release candidate is exercised before it is tagged. It depends on Exoma; it does not carry its own copy of the runtime. |

Exoma began as a fork of agent-workflow 3.9.0 and shares its history up to that point.
Runtime fixes are made in Exoma, and agent-workflow picks them up by depending on a newer
Exoma. Runtime code is not merged back from agent-workflow into this repository. This
ownership and dependency direction allows ideas, findings, and research questions to flow
in both directions.

## Development and Research Relationship

Exoma should evolve continuously based on practical needs, engineering discoveries, and
opportunities for improvement. Its development must not be constrained by the research
agenda or require formal research validation before new ideas can be implemented.

Relevant findings from research papers, emerging concepts, or engineering observations may
follow either of two paths:

1. **Direct Implementation:** When a finding has clear relevance and sufficient technical
   feasibility, it may be explored, prototyped, or implemented directly in Exoma. Its
   effectiveness and limitations can be evaluated afterward through usage, testing, and
   further experimentation.

2. **Research and Evaluation:** When the underlying concept, applicability, expected
   benefits, or potential risks are still unclear, it may first be investigated through
   agent-workflow. This can involve theoretical analysis, hypothesis formulation,
   experiments, and comparative evaluation before deciding whether and how it should be
   adopted.

These paths are not mutually exclusive or strictly sequential. Implementation may generate
new research questions, while research may inspire new implementations. Findings from both
environments should continuously inform each other.

**The purpose of agent-workflow is not to control what Exoma is allowed to implement, but
to help understand, evaluate, and learn from what Exoma implements.**

## Daily use versus development

The `~/.claude` you work in every day runs a **released** Exoma, installed from PyPI:

```bash
python -m pip install --upgrade exoma
exoma install
```

A development checkout never installs onto that `~/.claude`, nor onto `~/.gemini`, where the
installer also writes the Agy host block and hook when that directory exists. The hooks and skills it would
write replace the released ones under the same names, so an unfinished change would run in
every session on the machine, including the one you are using to fix it. If it happens
anyway, `exoma install --rollback` previews and `exoma install --rollback --apply` restores
the previous install, but it refuses when an
installed file has changed since or its receipt is missing; then reinstall the release with
`python -m pip install --force-reinstall exoma` and `exoma install`.

## Working in the checkout

The checkout needs Python 3.11+, the floor in `pyproject.toml` that `uv run` follows.

```bash
uv run python tests/run.py                   # the suite; a temporary EXOMA_HOME unless one is already set
uv run python tools/maintain/stamp_version.py --check
uv run python tools/maintain/gen_manifest.py --check
uv run python tools/e2e/e2e.py               # local end-to-end, no provider quota
uv run python tools/e2e/e2e.py --full --provider codex --model <id>   # plus real delegated calls (quota)
```

To try the installer or a workspace from the checkout, point every home at a temporary
directory so nothing reaches the real `~/.claude`, `~/.gemini` or user data directory:

```bash
export AGENT_HOME="$(mktemp -d)"
export EXOMA_HOME="$AGENT_HOME"   # separate line: in one export it would expand while still empty
uv run exoma install
uv run exoma init --work-dir "$(mktemp -d)"
```

`AGENT_HOME` redirects the install target and `EXOMA_HOME` the per-user data; their exact
behavior is in the [reference](reference.md#detail-installer).

## Staging a release in agent-workflow

Before a version is tagged, agent-workflow runs its experiments and harnesses against the
release candidate rather than against the last published version:

1. Push the candidate to Exoma `main`, where CI (including the wheel install in the `dist`
   job) must be green.
2. In agent-workflow, point its Exoma dependency at that commit and run its staging suite,
   including the delegated end-to-end run that spends real provider quota.
3. Only then tag the release here ([RELEASE.md](../RELEASE.md), step 6b).

This staging requirement is an engineering release gate under the
[Development and Research Relationship](#development-and-research-relationship).
Candidate compatibility, regression checks, delegated end-to-end coverage, and integrity
checks for research records maintained in agent-workflow remain required by the staging
procedure. Successful scientific validation or an answered research question is not a
prerequisite for implementing a change or accepting a release candidate. An Exoma feature
does not require its own research record; record-integrity checks apply to the records
maintained there. Direct implementation still follows applicable engineering planning,
user authorization, and verification requirements.

A failure found in staging is fixed in Exoma, never patched inside agent-workflow.

## Research boundary

Research records stay in agent-workflow. This repository carries no `docs/research/`; local
drafts may live in the git-ignored `docs/research-drafts/`, and tracked files never link
there.
