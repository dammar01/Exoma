# Skill: upgrade
description: Refresh .workflow in place while preserving project state and sessions.

## Trigger
/.upgrade

## Run (local)
The entry point is the `exoma` CLI (`exoma --version`; fallback `<python> -m exoma`). If neither
runs, stop and ask the user to install it (`uv tool install exoma` or `pip install exoma`).
The workspace is always repointed at the build running the command. A workspace written by
agent-workflow (its `runtime` names a checkout's `main.py`) is taken over: those paths are
removed and the scripts call `<python> -m exoma`.

Semua OS (tool Bash): exoma upgrade --work-dir "<work_dir>" --pretty
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
