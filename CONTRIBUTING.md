# Contributing to Exoma

Welcome! Exoma is an open-source, Python standard-library runtime that supports
evidence-driven problem solving for AI coding agents. You do not need to know the project's
entire research history to make a meaningful contribution.

Before diving in, skim [README.md](README.md) and [docs/philosophy.md](docs/philosophy.md) so
your change aligns with how Exoma separates agent reasoning, deterministic runtime control,
and verifiable evidence.

---

## 1. Core development principles

- **All engineering contributions are valuable.** Bug fixes, reliability improvements, tests,
  documentation clarity, provider/host compatibility, and performance work matter just as much
  as new workflow capabilities.
- **Lead with the problem and evidence.** Explain what breaks, what is unclear, or what
  improves, backed by reproduction steps, tests, or source references where possible.
- **Preserve contracts and safety boundaries.** Keep deterministic guarantees intact: stdlib-only
  runtime dependencies, secret redaction, scope/approval gates, and honest reporting of
  limitations.
- **Justify new complexity.** Prefer simple, focused changes. New flags, persistent state, or
  orchestration steps should earn their maintenance cost.
- **Technical merit over initial preference.** Sound alternative implementations are evaluated
  on evidence, clarity, and trade-offs — not rejected simply because they differ from a
  maintainer's first sketch.
- **Proportionate expectations.** Small, focused PRs are welcome. Ordinary engineering fixes
  do not require research records or formal design papers.

---

## 2. Repository structure

```text
src/exoma/
  cli.py                  CLI entry point (`exoma`)
  config/                 Version (`settings.py`), host/provider registry (`providers.py`)
  core/
    audit/                Telemetry, metrics registry (`metrics.py`), session export
    evidence/             Fact store, evidence index, contracts, result shaping, test verify, browser `e2e/`
    graph/                Graphify candidate-file lead reader & staleness checks
    jobs/                 Async job manager, heartbeat, recovery
    knowledge/            Promoted project-knowledge schema, storage, retrieval
    prompt/               Router & prompt builder
    provider/             Executor, continuations, result preparation
    runtime/              Execution state (`execution.py`), config defaults, runner scripts
    workspace/            Sessions, locks, workspace paths
  adapters/providers/     Provider adapters (`opencode`, `codex`, `agy`, `claude`)
  installer/              Host & provider installer (`exoma install`)
  _bundle/                Shipped skills, hooks, and host/provider configs (manifest-checked)
tests/                    Test runner (`tests/run.py`) and check modules (`tests/checks/`)
tools/maintain/           Version stamp, manifest generator, offline measurement tools
docs/                     Public documentation (philosophy, architecture, reference, team guide)
```

Key repository constraints (see [CLAUDE.md](CLAUDE.md)):

- **Stdlib-only runtime:** `src/exoma/` has zero third-party runtime dependencies (enforced by
  `tests/checks/deps.py`). Optional extras (such as Playwright for `e2e`) are imported lazily
  under `ImportError`.
- **Single version source:** package version lives only in `src/exoma/config/settings.py`
  (`TOOL_VERSION`). Do not hand-edit stamped banners, `pyproject.toml` version, or
  `src/exoma/_bundle/manifest.json`.
- **Standalone hooks:** hooks shipped under `src/exoma/_bundle/` stay standalone stdlib scripts
  and must not import `exoma`.

---

## 3. Local setup

Requires **Python 3.11+** (and `uv` recommended, or a standard virtual environment).

```bash
git clone https://github.com/dammar01/Exoma.git
cd Exoma
uv run python tests/run.py --only cli
```

### Isolate development installs from your daily setup

Never run `exoma install` from a development checkout directly onto your real `~/.claude` or
`~/.gemini` — it overwrites your released hooks and skills. Redirect both homes to a temporary
directory when testing installation or workspace initialization:

```bash
export AGENT_HOME="$(mktemp -d)"
export EXOMA_HOME="$AGENT_HOME"
uv run exoma install
uv run exoma init --work-dir "$(mktemp -d)"
```

On Windows PowerShell:

```powershell
$tmp = Join-Path $env:TEMP ("exoma-dev-" + [guid]::NewGuid().ToString("N"))
$env:AGENT_HOME = $tmp
$env:EXOMA_HOME = $tmp
uv run exoma install
```

More maintainer and release-staging details live in [docs/development.md](docs/development.md).

---

## 4. Running tests and checks

Run the targeted checks that cover your change, plus the full local suite before opening a PR
(no provider quota required):

```bash
uv run python tests/run.py                          # full deterministic test suite
uv run python tests/run.py --only metrics-contract  # run one check suite (use --list to view all)
uv run python tools/maintain/stamp_version.py --check
uv run python tools/maintain/gen_manifest.py --check
uv run python tools/e2e/e2e.py                      # local end-to-end check (no quota)
```

If you modify files under `src/exoma/_bundle/`, regenerate the manifest and verify:

```bash
uv run python tools/maintain/gen_manifest.py
uv run python tools/maintain/gen_manifest.py --check
```

---

## 5. Reporting reproducible bugs

Good bug reports help fixes land quickly. Please include:

- **Environment:** OS, Python version, Exoma version (`exoma --version`), primary host
  (Claude Code / Agy / Codex / OpenCode), and secondary provider.
- **Minimal reproduction:** exact command or workflow step, expected behavior, and observed
  output/error (`error_type` or `meta.reason` when available).
- **Redacted diagnostics:** attach relevant `exoma doctor` or sanitized log snippets — **never**
  paste secrets, tokens, or private repository credentials.

---

## 6. Proposing improvements

Before starting a large change, open an issue or short discussion note covering:

1. **Problem:** what concrete friction, gap, or failure mode exists today.
2. **Proposal:** the bounded change you have in mind and why its complexity is justified.
3. **Boundaries & trade-offs:** compatibility, security/privacy, or host/provider limitations.

Small bug fixes, test additions, and documentation improvements can go straight to a pull
request.

---

## 7. Pull request expectations

Keep PRs focused and easy to review:

- Explain **what** changed, **why**, and **how it was tested** (exact commands run).
- Include regression tests for bug fixes and behavior changes.
- Keep documentation aligned with behavior: update [docs/reference.md](docs/reference.md) or
  [docs/runtime-contracts.md](docs/runtime-contracts.md) when runtime behavior or contracts
  change.
- Avoid bundling unrelated refactors or unrequested formatting churn.
- Ensure CI checks pass (`tests/run.py`, `stamp_version.py --check`, `gen_manifest.py --check`).

---

## 8. Documentation contributions

Documentation follows a layered single-source-of-truth convention (see [docs/README.md](docs/README.md)):

- **Philosophy & onboarding:** `README.md`, `docs/philosophy.md`, `CONTRIBUTING.md`.
- **Day-to-day usage:** `docs/team-guide/` (links to reference; does not copy config keys or
  command flags).
- **Authoritative behavior:** `docs/architecture/README.md`, `docs/reference.md`,
  `docs/runtime-contracts.md`, `docs/limitations.md`, `docs/evaluation/metrics.md`.

Documentation guidelines:

- Document **implemented behavior**, grounded in source code and tests — not future plans.
- Keep public repository docs in clear, approachable English.
- Do not add version strings in `README.md` prose (only the shields.io badge carries the
  version; enforced by `stamp_version.py --check`).
- Preserve existing `<id>@<version>` identifiers and `Registry version` in
  `docs/evaluation/metrics.md` (enforced by `tests/checks/metrics.py`).

---

## 9. Security, privacy, and compatibility

- **Secrets:** never commit credentials, `.env` files, or private transcripts. Changes touching
  prompts, logs, exports, or browser runs must preserve redaction (`utils/redact.py`,
  `core/evidence/e2e/redact.py`).
- **Cross-platform:** Exoma runs on Linux, macOS, and Windows (including PowerShell quoting and
  path normalization). Avoid POSIX-only assumptions in shared core code.
- **Host/provider honesty:** if a host or provider cannot enforce a boundary (for example,
  read-only delegation or shell write blocking), document the actual limit rather than masking
  it. See [docs/security.md](docs/security.md).

---

## 10. Review process

Reviews focus on correctness, contract safety, test coverage, clarity, and proportionality of
complexity. Expect direct, constructive feedback grounded in code and evidence — and feel free
to push back with technical reasoning or alternative approaches when the evidence supports them.
