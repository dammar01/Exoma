# Installation

Install agent-workflow on a machine, enable it per project, keep it up to date, and remove
it again. Every installer flag, target file and hook is listed in the
[reference](reference.md#detail-installer); this page is the path through them.

> **Read this before installing.** The installer changes configuration that **every**
> Claude Code session and every project on the machine reads, not just one repository:
>
> - **`~/.claude/CLAUDE.md`** gains the workflow's marked block. Your own text outside the
>   markers is never changed, but in auto-intent mode the block changes how Claude Code
>   answers ordinary requests (routing, output format, style). Command-only mode limits it to
>   `/.<command>` runs.
> - **`~/.claude/settings.json`** gains the workflow's hooks and status line. Hooks run on
>   every prompt, on several tool calls and at the end of each turn, and in auto-intent mode
>   one of them **blocks** reading tools until a delegated command has run.
> - **`~/.claude/skills`, `commands`, `hooks`** receive the workflow's files.
> - **The secondary agent's own configuration** gains a marked block in its `AGENTS.md`
>   (opencode, codex, agy) and, for opencode, a merged permission policy in `opencode.json`.
> - **`AGENT_PATH`** is set in your user environment when you pass `--set-env`.
> - **The secondary agent reads your source code**, and how strictly it is confined depends
>   on the provider ([Security](security.md)).
>
> These files can change again on every upgrade, as the workflow changes. Each install keeps
> backups and a receipt, and a rollback restores them, but only if you have not edited the
> files since. Always read the dry run first.

## Requirements

| Requirement | Required | Notes |
| --- | --- | --- |
| **Python 3.10+** | Yes | The runtime needs no third-party packages. An older Python is refused with a message naming the version found. Optional browser verification uses Playwright (`requirements-e2e.txt`) |
| **A secondary-agent CLI** | Yes | `opencode` (recommended), `codex`, or `agy`, on `PATH`. Each workflow release is tested on one release of each CLI; `/.doctor` names it when yours differs ([provider releases](reference.md#provider-releases)) |
| **git** | Recommended | Used by `sweep`, `syntax` verify mode, and the workspace guard |
| **bash** | Yes | Hooks and the `.workflow/` runner are bash scripts on every OS. On Windows that is Git Bash, from Git for Windows; the installer finds it next to `git.exe` and refuses `--apply` without it |
| **Claude Code** | Recommended | The primary integration target |

## Install on a machine (once)

### 1. Clone

```bash
git clone https://github.com/dammar01/agent-workflow.git
cd agent-workflow
```

Keep this directory somewhere permanent: every project you enable points back at it.

### 2. Read the dry run, then apply

```bash
python install.py                    # dry run: prints every change, writes nothing
```

Then pick how the workflow switches on and apply:

| You want | Install with |
| --- | --- |
| The workflow as the default way of working: plain requests are routed to commands | `python install.py --apply --set-env --auto-intent` |
| Your own setup untouched until you type a `/.<command>` | `python install.py --apply --set-env --only-command` |

On a terminal the installer asks which secondary agent to use; `--provider`/`--model` skip
the question. If unsure, choose opencode, the only provider whose read-only boundary is
enforced. `--set-env` persists `AGENT_PATH`; reopen the terminal afterwards. The mode is
remembered for later upgrades; run the other flag to switch.

> **Do not skip this step.** It installs the permission block for the secondary agent as
> well as the skills and hooks; without it the secondary agent runs without write
> restrictions.

Pick command-only if you already rely on your own CLAUDE.md, or share the machine's Claude
Code with work unrelated to this workflow. Either way, the secondary agent's instructions
apply only to calls the workflow makes; using codex or opencode directly is unaffected.

## Enable it in a project (once per project, per person)

Open Claude Code in the project and run:

```text
/.init
```

It creates `.workflow/` in the project: config overrides, the provider selection, the entry
scripts (`run`, `inspect`, `check`), live progress in `current/` and internal data in
`data/`, plus an `opencode.json` that denies secret-file reads. `.workflow/` is added to
`.gitignore`.

If the project already has a `.workflow/` from an older release, run `/.upgrade` instead:
it refreshes the workspace and keeps its config and sessions.

Then check it:

```text
/.doctor
```

`/.doctor` must report **`READY`**. `NOT_READY` lists what to fix in `recommended_fixes`.
Warnings (an empty test allowlist for `/.verify`, an untested provider release, a leftover
header in your CLAUDE.md) do not block it, but each tells you what to change.

> **Do not commit `.workflow/`.** The generated scripts hold absolute paths from your
> machine. Each team member runs `/.init` themselves.

Without Claude Code, or from a script, the skills run the same command:
`python "$AGENT_PATH" --command init --work-dir <project>` (and `--command upgrade`).

## Update

1. Pull the new release in the clone:

   ```bash
   cd agent-workflow
   git pull
   ```

2. Re-run the installer, dry run first. Run it from inside a project and it also upgrades
   that project's workspace:

   ```bash
   python install.py                # what will change
   python install.py --apply        # keeps your mode and provider
   ```

   It refreshes the workflow's block, files and hooks, and removes files an older release
   installed that are no longer shipped, unless you edited them.

3. In every other project, open Claude Code and run `/.upgrade`. It refuses while a delegated
   command is running in that project; wait for it to finish.

4. Run `/.doctor` in each project. `NEEDS_UPGRADE` or `run_script_drift` means step 3 was
   missed there; a provider warning names the CLI release this workflow version was tested
   on.

Read the release notes ([CHANGELOG](../CHANGELOG.md)) before step 2: a release can change
behavior you rely on, and the notes say what to set afterwards.

## Undo

- **The last install went wrong:** `python install.py --rollback` (dry run), then
  `--rollback --apply`. It refuses if a file changed since the install, instead of
  overwriting your edits.
- **Remove the workflow:** `python install.py --uninstall` (dry run), then
  `--uninstall --apply`. It removes the workflow's block from each instruction file and keeps
  your text, removes only the files it installed and you did not edit, and takes its hooks
  and status line out of `settings.json`. An uninstall can itself be rolled back. Project
  `.workflow/` folders, the secondary-agent seed (`config/second_agent.seed.json`) and a
  persisted `AGENT_PATH` are left for you to remove.

## When something is wrong

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `/.doctor` reports `NOT_READY` / `run_script_drift` | Tool updated, workspace scripts not | `/.upgrade` in that project |
| `/.doctor` reports bundle drift | The clone changed, the global install did not | `python install.py --apply` |
| Provider not found | The agent CLI is not on `PATH` | Install it, then check `<cli> --version` |
| Delegated calls fail after a provider update | The provider release broke something | Pin it back to the release `/.doctor` names ([provider releases](reference.md#provider-releases)) |
| Commands fail after moving the clone | Baked absolute paths are stale | Point `AGENT_PATH` at the new location, then `/.upgrade` |
| `install.py` refuses a CLAUDE.md or AGENTS.md | Its workflow markers do not pair up | Fix or remove the broken marker lines by hand, then re-run |

More: [troubleshooting](team-guide/troubleshooting.md).

## Good practice

- Read the dry run every time; the installer writes configuration every project reads.
- Run `python install.py --check` after an upgrade, and `/.doctor` in a project before
  relying on it.
- Keep your own rules outside the workflow's markers; anything inside is replaced on the
  next install.
- One clone per machine. Two clones installing over each other leave hooks pointing at
  whichever ran last.
- Tell each project which test commands `/.verify` may run (`commands.verify_test_commands`
  in `.workflow/config.json`). Until you do, verify runs no test and cannot pass.
- Keep the secondary agent on the release this workflow version was tested with.
- To see what the workflow does for you over weeks of use:
  [measure your use](team-guide/measure-your-use.md).
