# Exoma — repository development contract

Applies only when developing this repository. It is not shipped: the global bundle
installed into user projects is `src/exoma/_bundle/config/claude/CLAUDE.md`, and nothing
here belongs in it.

## Scope

Exoma is the user-facing library and CLI for the agent-workflow runtime. Research on
delegation (hypotheses, experiments, decision records) stays in agent-workflow; this
repository does not carry `docs/research/` or research records. Local drafts may live in
the git-ignored `docs/research-drafts/`; tracked files never link there.

## Package layout

- Runtime code lives under `src/exoma/` and is imported as `exoma.<package>`; never add a
  top-level package beside it.
- Shipped resources (skills, hooks, provider bundles, manifest) live in
  `src/exoma/_bundle/` and are read with `importlib.resources`, never through a path
  relative to the repository.
- The runtime stays stdlib-only (`tests/checks/deps.py`). An optional extra is imported
  lazily under `ImportError`.
- Hooks copied to `~/.claude/hooks` stay standalone stdlib scripts and do not import
  `exoma`.

## Documentation layers

Put content in the right layer (index: `docs/README.md`):

- `docs/team-guide/` — how and when to use it, for developers. Links to the reference;
  never copies command flags, config keys, provider security details, or storage layouts.
- `docs/reference.md`, `docs/runtime-contracts.md`, `docs/architecture/` — exact behavior.
  Authoritative: when another layer disagrees, fix the other layer.

A behavior change updates the reference first, then any team-guide page that describes its
use. Never write into `docs/project-knowledge/`: the runtime injects that directory into
exploration and reasoning prompts, and only `promote-write` may write there.

## Release surface

- Version lives only in `src/exoma/config/settings.py` `TOOL_VERSION`. Follow
  `RELEASE.md`; never hand-edit a stamped banner, `pyproject.toml` `version`, or the
  bundle manifest.
- `README.md` carries the version only in its shields.io badge. Any other version string in
  its prose, or in `docs/reference.md`, fails `tools/maintain/stamp_version.py --check`.
- Release notes: `docs/releases/v<version>.md` plus one index row in `CHANGELOG.md` (the
  index stays one line per version).
- Do not run `exoma install --apply` from a development checkout onto a machine that also
  uses agent-workflow: both install hooks and skills under the same names in `~/.claude`.
