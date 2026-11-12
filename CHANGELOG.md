# Changelog

This file is the index only: one row per version, a one-line theme, and a link. Each
version's notes are their own file, `docs/releases/v<version>.md`. Behavior itself is
described in `docs/reference.md` and `docs/runtime-contracts.md` — the notes say what
changed and link there rather than repeating it.

Exoma is forked from agent-workflow v3.9.0; history before the fork stays in that project.

Release procedure: `RELEASE.md`.

| Version | Notes | Theme |
|---------|-------|-------|
| 1.0.2 (candidate) | [docs/releases/v1.0.2.md](docs/releases/v1.0.2.md) | Bounded updater waits, live manager output, elapsed heartbeat, cancellation recovery and Windows cleanup fallback; release gates pending |
| 1.0.1 (candidate) | [docs/releases/v1.0.1.md](docs/releases/v1.0.1.md) | Python 3.11 minimum, automatic Codex reviewer context, runtime and verification hardening, bounded research and metric interpretation; final-candidate release gates pending |
| 1.0.0 | [docs/releases/v1.0.0.md](docs/releases/v1.0.0.md) | Stable release: `exoma install <host>` writes without `--apply`, OpenCode as a main-agent host, Claude Code as a second agent, one second-agent question for every host from the live model catalog, `exoma session export` for studies on real work, OpenCode subagents renamed `exm-*`, `run.sh test` passes reused by verify, tree snapshots on plan and execution, Codex hooks that launch on Windows, pre-flight escape files guarded, install under pipx and uv |
| 1.0.0b1 | [docs/releases/v1.0.0b1.md](docs/releases/v1.0.0b1.md) | Pre-release of 1.0.0 for trying it on real work; installed only when asked for (`exoma==1.0.0b1`, `--pre`); the version stamp reads pre-release suffixes |
| 0.3.0 | [docs/releases/v0.3.0.md](docs/releases/v0.3.0.md) | Agy as a host chosen at install and kept under `~/.gemini`, a shared hook policy and pre-flight decision, named runner options with a wider session guard (run `/.upgrade`), bundle laid out by role (`hosts/`, `providers/`), the agy second agent as an Agy custom agent, macOS path drift and `AGENT_HOME` pin fixes |
| 0.2.0 | [docs/releases/v0.2.0.md](docs/releases/v0.2.0.md) | Provider choice confirmed at selection (no opt-in variable), secret files named by doctor and watched by the agy guard, threat model, `EXOMA_*` environment variables, English bundle, no console flash on Windows |
| 0.1.0 | [docs/releases/v0.1.0.md](docs/releases/v0.1.0.md) | First Exoma release: agent-workflow v3.9.0 repackaged as the pip-installable `exoma` library and CLI, with automatic migration of existing workspaces |
