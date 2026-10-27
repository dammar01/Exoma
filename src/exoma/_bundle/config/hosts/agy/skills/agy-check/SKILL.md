---
name: .agy-check
description: "Check that Exoma's Agy host is set up end to end: skill registration, the runner without a permission prompt, and runtime writes from the sandbox."
---

# Skill: agy-check
Run from a project that has a `.workflow/` directory. Each step is one observation; do not
fix anything, report it.

1. **Skills.** This skill loaded, so Exoma's skills are registered. Record `skills: ok`.
2. **Hooks.** Look for the `[SESSION BINDING]` block injected at the start of this turn. Present
   → `hooks: ok (MAIN_SESSION_ID=<value>)`. Absent → `hooks: not running` and stop here: check
   `~/.gemini/config/hooks.json` holds the `exoma` hook and read
   `~/.gemini/exoma-hook-errors.log`.
3. **Runner.** Run exactly, once:
   `{{RUNNER}} '<root>/.workflow/run.sh' doctor '' '<MAIN_SESSION_ID>'`
   - it ran without asking for approval → `permission: ok`; it asked → `permission: prompted`
     (the opt-in rule is missing or did not match; `exoma install --check` shows which).
   - it printed doctor's JSON → `runner: ok`.
   - `No such file or directory` on the script path → `runner: wrong bash (WSL)`.
   - `NtCreateDirectoryObject` → `runner: sandbox refused Git Bash`.
   - `PermissionError` / `Access is denied` on `exoma\storage` → `runner: sandbox refused the data directory`.

Answer with one block and nothing else:

```
[AGY CHECK]
skills:     <ok>
hooks:      <ok (MAIN_SESSION_ID=...) | not running>
permission: <ok | prompted | not reached>
runner:     <ok | wrong bash (WSL) | sandbox refused Git Bash | sandbox refused the data directory | other: <first error line>>
```
