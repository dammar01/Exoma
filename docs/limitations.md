# Known limitations

Moved from `docs/reference.md` ("Known limitations"), where the old anchor still
points here.

Provider-specific security boundaries (write prevention, secret-file reads, shell
restriction) are summarized in the [security page](security.md) and
detailed in [`reference.md`](reference.md).

Stated openly because staying silent about them would make the runtime look like it guarantees more than it does:

- **main_agent mutations are only partly visible to this runtime.** Since 3.9.0 `/.execute` opens and closes a runtime execution (`execute-begin`/`execute-end`, `core/runtime/execution.py`) and the `execution-gate` hook holds Edit/Write to its scope and root (DEC-067). The runtime still does not attribute individual file changes to an execution, and the main agent's output contract (`[OPTIONS]`, confidence, attribution) is not validated.
- **Edits through Bash are not held back or recorded.** Since 3.9.0 the `execution-gate` hook blocks Edit/Write outside the session's project root (except the temp dir, `~/.claude` and `policies.write_allowed_outside_root`) and outside an active execution's scope (DEC-067). Edits made through Bash or a patch script do not go through that hook or the Edit/Write `task-events` hook (DEC-021 deliberately records no Bash edits), so a script can still write into another project. The prohibition on that remains a prompt contract (`CLAUDE.md`). Observed in real use (CASE-018).
- **Agy can write to the project.** Agy's boundary is `writes: unbounded`: its CLI has no read-only mode the runtime can enforce, so second_agent writes/edits are not prevented. Mutations are only detected after they happen, through the `git status` diff, so files that are `.gitignore`d (including `.env`) are invisible; `doctor` and every Agy result give a warning. If writes must be prevented, use OpenCode (boundary enforced); Codex is `sandboxed`, but the runtime effect of its flag is not yet proven. Per-provider details in the [security page](security.md).
- **The contract is still partly prompt-based.** The runtime validates the structure and routing of verify findings, but the semantic correctness of claims and of the contract output owned by main_agent still cannot be proven from markers alone.
- **Telemetry is still partial.** Duration, exit code, kill result, prompt/output size, and estimated tokens are recorded per call in `call.meta.json`; the number of tool calls and the provider's actual tokens are not always available.
- **Real OpenCode is only tested opt-in.** The default suite simulates the provider; the `Popen` path, persistence, installer, and process lifecycle are still run locally. Use `tools/e2e/e2e.py --full` for a quota-consuming smoke test.
- **The PING probe uses quota.** A job that stays stalled can be probed repeatedly according to the cadence; the default `await` is 120 seconds.
- **Liveness tuning is not yet uniform on the attach path.** `check.py --wait` uses the tool defaults for the stalled threshold and re-probe, not project-local values.
- **Upgrade detection can be masked by a partial stamp.** A delegated load updates the `config.json` version marker without regenerating scripts or `second_agent.json`, so the next warning can treat the workspace as current. Old layouts are still detected from their contents, not from the stamp.
- **Graph staleness detection follows a fixed source list.** The runtime fingerprint and the Graphify Stop hook scan the same twelve extensions and skip directories; a language outside that list does not mark the graph stale. The Stop hook is a separate Claude layer and is not available in every main agent.
- **Cross-OS graph paths are not fully normalized yet.** A snapshot built on Windows and then read from POSIX/WSL can still produce backslashed candidate paths; refresh the graph manually from the active environment if this happens.
- **Runner scripts are not portable across OSes.** Absolute paths are baked in at init/upgrade; moving the repo, path, or OS means running upgrade from the new environment.
