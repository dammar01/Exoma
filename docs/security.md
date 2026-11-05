# Security

The second agent reads your source code. How strictly it is confined depends on the
provider you select. This page states what Exoma protects, what it deliberately accepts,
and what remains open, so a reader can tell a decision from a limitation.

## Threat model

### What is protected

| Asset | Threat | Why it matters |
| --- | --- | --- |
| Secret files in the project (`.env`, private keys, cloud credentials) | The second agent reads them and sends their contents to its provider | The provider is a third party; a secret it has seen cannot be taken back |
| Credentials in the process environment | Passed to the provider CLI with the rest of the environment | Same exposure as a secret file, without a file to notice |
| The working tree | The second agent edits, creates or reverts files during a read-only call | A delegated call is meant to gather evidence, not change code; an unattributed change ends up in a later commit |
| The primary agent's decisions | Evidence that cites files or lines that do not exist | A plan built on a fabricated anchor looks grounded and is not |

The primary agent (Claude Code, Agy, or Codex) is in scope only as a consumer of evidence. Its own file
access is governed by its host's permissions, not by Exoma; Exoma's hooks add the workflow's
own rules (execution scope, project root, commit approval) on top, as far as each host lets them
([reference, Hosts](reference.md#hosts)).

### Trust boundaries

- **Your machine and your primary agent's session** (Claude Code, Agy, or Codex) are trusted.
- **The provider CLI and its vendor** (`opencode`, `codex`, `agy`, `claude`) are outside the boundary.
  What each one can reach depends on what that CLI lets Exoma restrict (table below).
- **Delegated output** is untrusted until checked: the runtime verifies every cited
  `file:line` against the project and caps the reported confidence when citations fail
  ([reference, "Evidence reuse and boundary"](reference.md#evidence-reuse-and-boundary)).

### What each provider enforces

| | `opencode` | `codex` | `agy` | `claude` |
| --- | --- | --- | --- | --- |
| Secret-file reads denied | **Yes** (`opencode.json`) | No — declared, not enforced | No | No |
| Environment passed to the CLI | Full | Full | Full | Full |
| Writes and edits denied | **Yes** | **Yes** (`--sandbox read-only` + `approval_policy="never"`) | No | **Yes** (only `Read`, `Grep`, `Glob` exist in the session) |
| Shell commands restricted | **Yes**, read-only git allowlist | No (the sandbox blocks writes only) | No | **Yes** (no shell tool) |
| Working-tree changes | Prevented | Prevented | Detected after the call | Prevented |
| User hooks, settings and MCP servers loaded | — | — | — | No (`--setting-sources ''`, `--strict-mcp-config`) |
| Selection confirmed in `/.provider` | No | **Yes** | **Yes** | **Yes** |

1. **The opencode write boundary lives in the global configuration** installed by
   `exoma install` ([installation](installation.md)). The project-local `opencode.json`
   covers secret-file *reads*.
2. **`codex` reads through a shell its permission flags never see.** Its sandbox stops
   writes; nothing stops reads. Its Windows sandbox also depends on the codex release
   ([provider releases](reference.md#provider-releases)).
3. **`agy` runs with permissions skipped.** A guard compares `git status` before and after
   each call, and also the size and modification time of every file that matches the secret
   patterns, so a change to a `.gitignore`d `.env` is reported too. It detects; it does not
   prevent.
4. **Recursion is refused, not prevented.** Every adapter marks its child
   `WORKFLOW_AGENT_ROLE=second_agent`, and the runtime refuses a marked process that asks to
   delegate again (`recursion_refused`). That covers a child that inherits the marker, which is
   every OpenCode, Codex and Claude call. An `agy` child can run shell commands and so can
   unset the variable before calling the runtime; for agy the refusal is a guard against
   accidents, not a boundary.

### Accepted risks (decisions)

These are choices, not gaps waiting for a fix:

- **`codex`, `agy` and `claude` are trusted providers.** Selecting one means accepting that the
  second agent can read every project file and the full environment (agy can also write).
  The acknowledgement happens where the choice is made: the `/.provider` picker shows the
  provider's boundary and asks for confirmation before it writes the selection. No variable or
  config key stands behind it, so it is not enforced by the runtime: `/.provider <provider> ...`
  with arguments and `exoma install --provider` select without asking, and their result
  carries the same warning instead.
- **The full environment is passed through.** An allowlist could break the provider CLI's
  authentication, and a provider that can already read `.env` gains nothing from a trimmed
  environment.
- **Warnings, not refusals, after selection.** `doctor` reports the read boundary on every
  run and lists the files that match the secret patterns. It never turns this into an
  issue, because the provider choice is the user's.

### Residual risks (limitations)

These remain open, and nothing on this page should be read as covering them:

- **Host edits are not fully attributable on any host.** Claude Code, Agy, and Codex host
  hooks check supported write tools before they run. Changed-file and scope reports use the
  current diff against Git `HEAD`, without a content baseline taken at the start of each
  execution. A further edit to a file already modified before the turn can therefore be
  visible in the final diff without being attributable to that turn. Shell and script writes
  also bypass the host hooks' file-path checks.
- **Reads are not confined for `codex`, `agy` or `claude`.** The confirmation records that the risk was
  accepted; it does not reduce it. Running the second agent on a filtered copy of the
  project or inside an operating-system sandbox would, and is not implemented: a filtered
  copy alone does not stop reads by absolute path, and an OS sandbox needs per-platform
  mechanisms outside the standard library.
- **Secret detection is by file name.** A secret in a file whose name matches no pattern
  (a credential in `config.py`) is not found, and a harmless file with a matching name is
  reported.
- **The agy second-agent guard has gaps:** a tracked file already modified before a delegated call can be
  modified again without changing its `git status` entry. The guard then reports
  `checked: true, mutated: false` despite the write. It also misses ignored files that
  match no secret pattern, files written and restored within one call, and projects outside
  git (where it reports `checked: false`). The guard detects after the fact; it does not
  prevent writes.
- **Anchor certification proves that a cited line exists, not that the claim about it is
  true.** Reading the anchor a decision rests on is still the primary agent's job. A short
  citation (`cli.py:54`) certifies against the one project file whose path ends with it; a
  name two files share stays uncertified.
- **The pre-flight gate keeps a cooperating agent from drifting; it is not a boundary
  against an agent that works around it.** While a delegation is armed in a bound session,
  the hooks refuse gathering tools, and in any bound session they refuse a Write of the
  runner scripts or of an escape file (`local_mode.flag`, `delegated.marker`,
  `delegated.reads`). They do not parse shell commands for writes, so a shell command run
  while the gate is not armed can still create `local_mode.flag`; that, and every escape
  used while armed, is only recorded in `gate-skips.jsonl` and reported by `doctor`. A
  session the hooks cannot resolve gets no gate at all (fail-open). See
  [runtime-contracts.md](runtime-contracts.md) for the exact rules.

For projects holding secrets the second agent must not read, use `opencode`.

## Provider CLI updates

The installer changes a provider CLI only when asked (`--update-providers`), and then only an
npm-global install, only to the release the workflow was tested on, and never with `sudo`
([provider releases](reference.md#provider-releases)).

What each provider enforces, in detail, and how to switch:
[reference, "Choosing a provider"](reference.md#choosing-a-provider--and-its-security-consequences).
