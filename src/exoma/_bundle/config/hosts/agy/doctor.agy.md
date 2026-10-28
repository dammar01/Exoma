
## STEP 3 — This Agy session (Agy only)
The runtime checks that the Agy host's files are in place (`checks.hosts.agy`); only this
session can see whether Agy loads them. One observation each; do not fix anything, report it.
1. **Skills.** This skill loaded as `/.doctor`, so Exoma's skills are registered → `skills: ok`.
2. **Hooks.** The `[SESSION BINDING]` block injected at the start of this turn: present →
   `hooks: ok (MAIN_SESSION_ID=<value>)`; absent → `hooks: not running` (check
   `~/.gemini/config/hooks.json` holds the `exoma` hook and read `~/.gemini/exoma-hook-errors.log`).
3. **Runner.** The STEP 1 runner call is the probe:
   - it ran without asking for approval → `permission: ok`; it asked → `permission: prompted`
     (`checks.hosts.agy.permissions` says whether the rules are on; `--no-agy-permissions` turns them off);
   - it printed doctor's JSON → `runner: ok`;
   - `No such file or directory` on the script path → `runner: wrong bash (WSL)`;
   - `NtCreateDirectoryObject` → `runner: sandbox refused Git Bash`;
   - `PermissionError` / `Access is denied` on `exoma\storage` → `runner: sandbox refused the data directory`.

Add to the report:
```
agy_session:
  skills:     ok
  hooks:      <ok (MAIN_SESSION_ID=...) | not running>
  permission: <ok | prompted | not reached>
  runner:     <ok | wrong bash (WSL) | sandbox refused Git Bash | sandbox refused the data directory | other: <first error line>>
```
