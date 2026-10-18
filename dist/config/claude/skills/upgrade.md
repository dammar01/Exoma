# Skill: upgrade
description: Refresh .workflow in place while preserving project state and sessions.

## Trigger
/.upgrade

## Run (local)
Resolve the workflow entry point in this order:
1. `$AGENT_PATH` / `$env:AGENT_PATH` — the build the user points at.
2. Existing `.workflow/config.json` → `runtime.main_py_path`, only when AGENT_PATH is unset.
   It names the build that WROTE the workspace, which may be an old clone.
3. If neither points to a file, stop and ask for the agent-workflow repository path.
The runtime then prefers the build actually running the command, so an upgrade always
repoints the workspace at the code doing the upgrade.

Semua OS (tool Bash): <python> "<main.py>" --command upgrade --work-dir "<work_dir>" --pretty
<python> = yang pertama JALAN dan ≥ 3.10 dari `python3`, `python`, `py -3`. Upgrade juga menghapus
`run.ps1`/`inspect.ps1`/`check.ps1` lama: runner sekarang `.sh` saja, di semua OS.

Upgrade refuses while delegated jobs are active. On a workspace still on the v3.5.x layout it
first MIGRATES: backup to `.workflow/data/backups/<stamp>/`, internal files moved into
`.workflow/data/`, old leftovers removed, `config.json` stripped to overrides (values equal to
a default and retired keys removed), a pre-list `e2e/secrets.json` converted. Entries are
staged in `.workflow/data.migrating/` and renamed to `data/` in one step. A leftover staging
directory that holds names also present at the `.workflow` root stops upgrade with a conflict
and moves nothing — the user keeps one copy of each (compare first), removes the other, reruns.
Past that, a failure is one of three outcomes — relay the runtime message as is, never soften
it to "rolled back":
- "failed and was rolled back" → everything is back, copy kept as `.workflow/migration-backup-<stamp>/`.
- "failed and the rollback is INCOMPLETE" → entries that could not move back are still in
  `.workflow/data.migrating/`; move them to `.workflow/` by hand, or rerun upgrade (it recovers
  a leftover staging directory first). A copy may be under `.workflow/migration-backup-*`.
- "migration incomplete" (after the rename) → nothing moved back, workspace already on
  `data/`; fix the named cause and rerun upgrade, it resumes from that step.
Then it regenerates
runner scripts, repoints tool paths and restamps config. Sessions and history are preserved.
It does not call second_agent or run verification.

## Output
[UPGRADE]
from: <installed tool/config versions>
to: <current tool/config versions>
scripts: regenerated | unchanged
migration: <moved N items into .workflow/data/, backup <path> | not needed>
config: <overrides kept; removed: <keys> | unchanged>
secrets: <converted | unchanged>
stray: <unrecognised files left at .workflow root | none>
sessions: preserved
status: READY | BLOCKED (<active job/error>)
