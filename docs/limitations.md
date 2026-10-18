# Known limitations

Moved from `docs/reference.md` ("Known limitations"), where the old anchor still
points here.

Provider-specific security boundaries (write prevention, secret-file reads, shell
restriction) are summarized in the [security page](security.md) and
detailed in [`reference.md`](reference.md).

Stated openly because staying silent about them would make the runtime look like it guarantees more than it does:

- **main_agent mutations are invisible to this runtime.** `/.execute` has no Python path at all. Scope auditing, a guard for destructive operations, and attribution of file changes therefore do not exist yet — they need a hook layer on the main_agent side.
- **Writes to other projects are not held back, and edits through Bash are not recorded.** The `intent-gate-check` hook only rejects writes to runner scripts; Edit/Write to paths outside the session's project root — including a neighboring repo — pass. Edits made through Bash or a patch script do not go through the Edit/Write hook, so `task-events` does not record them (DEC-021 deliberately installs no hook on Bash), and edits to other projects are discarded because they are outside the root. The prohibition on writing to another project without the user's explicit approval lives in the prompt contract (`CLAUDE.md`, DEC-046), not in a hook. Observed in real use (CASE-018).
- **Agy can write to the project.** Agy's boundary is `writes: unbounded`: its CLI has no read-only mode the runtime can enforce, so second_agent writes/edits are not prevented. Mutations are only detected after they happen, through the `git status` diff, so files that are `.gitignore`d (including `.env`) are invisible; `doctor` and every Agy result give a warning. If writes must be prevented, use OpenCode (boundary enforced); Codex is `sandboxed`, but the runtime effect of its flag is not yet proven. Per-provider details in the [security page](security.md).
- **The contract is still partly prompt-based.** The runtime validates the structure and routing of verify findings, but the semantic correctness of claims and of the contract output owned by main_agent still cannot be proven from markers alone.
- **Telemetry is still partial.** Duration, exit code, kill result, prompt/output size, and estimated tokens are recorded per call in `call.meta.json`; the number of tool calls and the provider's actual tokens are not always available.
- **Real OpenCode is only tested opt-in.** The default suite simulates the provider; the `Popen` path, persistence, installer, and process lifecycle are still run locally. Use `tools/e2e/e2e.py --full` for a quota-consuming smoke test.
- **The PING probe uses quota.** A job that stays stalled can be probed repeatedly according to the cadence; the default `await` is 120 seconds.
- **Liveness tuning is not yet uniform on the attach path.** `check.py --wait` uses the tool defaults for the stalled threshold and re-probe, not project-local values.
- **Upgrade detection can be masked by a partial stamp.** A delegated load updates the `config.json` version marker without regenerating scripts or `second_agent.json`, so the next warning can treat the workspace as current. Old layouts are still detected from their contents, not from the stamp.
- **Graph staleness detection only follows `.py` sources.** Changes in other languages do not enter the runtime fingerprint; the Graphify Stop hook is a separate Claude layer and is not available in every main agent.
- **Cross-OS graph paths are not fully normalized yet.** A snapshot built on Windows and then read from POSIX/WSL can still produce backslashed candidate paths; refresh the graph manually from the active environment if this happens.
- **Runner scripts are not portable across OSes.** Absolute paths are baked in at init/upgrade; moving the repo, path, or OS means running upgrade from the new environment.
