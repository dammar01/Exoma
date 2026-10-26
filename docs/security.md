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

The primary agent (Claude Code) is in scope only as a consumer of evidence. Its own file
access is governed by Claude Code's permissions, not by Exoma.

### Trust boundaries

- **Your machine and your Claude Code session** are trusted.
- **The provider CLI and its vendor** (`opencode`, `codex`, `agy`) are outside the boundary.
  What each one can reach depends on what that CLI lets Exoma restrict (table below).
- **Delegated output** is untrusted until checked: the runtime verifies every cited
  `file:line` against the project and caps the reported confidence when citations fail
  ([reference, "Evidence reuse and boundary"](reference.md#evidence-reuse-and-boundary)).

### What each provider enforces

| | `opencode` | `codex` | `agy` |
| --- | --- | --- | --- |
| Secret-file reads denied | **Yes** (`opencode.json`) | No — declared, not enforced | No |
| Environment passed to the CLI | Full | Full | Full |
| Writes and edits denied | **Yes** | **Yes** (`--sandbox read-only` + `approval_policy="never"`) | No |
| Shell commands restricted | **Yes**, read-only git allowlist | No (the sandbox blocks writes only) | No |
| Working-tree changes | Prevented | Prevented | Detected after the call |
| Selection confirmed in `/.provider` | No | **Yes** | **Yes** |

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

### Accepted risks (decisions)

These are choices, not gaps waiting for a fix:

- **`codex` and `agy` are trusted providers.** Selecting one means accepting that the
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

- **Reads are not confined for `codex` or `agy`.** The confirmation records that the risk was
  accepted; it does not reduce it. Running the second agent on a filtered copy of the
  project or inside an operating-system sandbox would, and is not implemented: a filtered
  copy alone does not stop reads by absolute path, and an OS sandbox needs per-platform
  mechanisms outside the standard library.
- **Secret detection is by file name.** A secret in a file whose name matches no pattern
  (a credential in `config.py`) is not found, and a harmless file with a matching name is
  reported.
- **The agy guard has gaps:** projects outside git, ignored files that match no secret
  pattern, and a file written and restored within one call.
- **Anchor certification proves that a cited line exists, not that the claim about it is
  true.** Reading the anchor a decision rests on is still the primary agent's job.

For projects holding secrets the second agent must not read, use `opencode`.

## Provider CLI updates

The installer changes a provider CLI only when asked (`--update-providers`), and then only an
npm-global install, only to the release the workflow was tested on, and never with `sudo`
([provider releases](reference.md#provider-releases)).

What each provider enforces, in detail, and how to switch:
[reference, "Choosing a provider"](reference.md#choosing-a-provider--and-its-security-consequences).
