# Security

The secondary agent reads your source code. How strictly it is confined depends on the
provider you select.

| | `opencode` | `codex` | `agy` |
| --- | --- | --- | --- |
| Write/edit denied by config | **Yes** | Not enforceable | No |
| Secret-file reads denied | **Yes** | Declared, not enforced | No |
| Shell commands restricted | **Yes**, read-only git allowlist | No | No |
| Workspace mutation handling | Prevented | Prevented for writes | Detected after the fact |

1. **The write boundary lives in the global configuration** installed by `install.py`
   ([installation](installation.md)). The project-local `opencode.json` covers secret-file
   *reads* only.
2. **`codex` passes filesystem permission flags on every call, but their runtime effect is
   unverified** against the current CLI. Treat the boundary as unproven. Its Windows sandbox
   also depends on the codex release ([provider releases](reference.md#provider-releases)).
3. **`agy` runs with permissions skipped**, guarded by detection rather than prevention: it
   diffs `git status` around each call, so `.gitignore`d files — including `.env` — are
   invisible to it.

For projects holding secrets the secondary agent must not read, use `opencode`.

The installer changes a provider CLI only when asked (`--update-providers`), and then only an
npm-global install, only to the release the workflow was tested on, and never with `sudo`
([provider releases](reference.md#provider-releases)).

What each provider enforces, in detail, and how to switch: [reference, "Choosing a provider"](reference.md#memilih-provider--dan-konsekuensi-keamanannya).
