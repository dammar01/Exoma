# Installation

Install Exoma on a machine, enable it per project, keep it up to date, and remove
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
> - **Agy, where it is installed** (a `~/.gemini` directory exists): `~/.gemini/GEMINI.md`
>   gains the `EXOMA-HOST` block and `~/.gemini/config/hooks.json` the `exoma` hook, so Agy
>   can be the primary agent too. Agy loads both on every run, its own sessions included
>   ([reference, "Hosts"](reference.md#hosts)).
> - **The secondary agent reads your source code**, and how strictly it is confined depends
>   on the provider ([Security](security.md)).
>
> These files can change again on every upgrade, as the workflow changes. Each install keeps
> backups and a receipt, and a rollback restores them, but only if you have not edited the
> files since. Always read the dry run first.

## Requirements

| Requirement | Required | Notes |
| --- | --- | --- |
| **Python 3.10+** | Yes | The runtime needs no third-party packages. An older Python is refused with a message naming the version found. Optional browser verification uses Playwright (the `e2e` extra) |
| **uv** or **pip** | Yes | To install the `exoma` package. `uv tool install` keeps it in its own environment |
| **A secondary-agent CLI** | Yes | `opencode` (recommended), `codex`, or `agy`, on `PATH`. Each workflow release is tested on one release of each CLI; `/.doctor` names it when yours differs ([provider releases](reference.md#provider-releases)) |
| **git** | Recommended | Used by `sweep`, `syntax` verify mode, and the workspace guard |
| **bash** | Yes | Hooks and the `.workflow/` runner are bash scripts on every OS. On Windows that is Git Bash, from Git for Windows; the installer finds it next to `git.exe` and refuses `--apply` without it |
| **Claude Code** or **Agy** | Recommended | The primary agent's host. Claude Code is the reference host; Agy is installed for when `~/.gemini` exists ([hosts](reference.md#hosts)) |

## Install on a machine (once)

### 1. Install the package

```bash
uv tool install exoma          # or: pip install exoma
exoma --version
```

`exoma` is the CLI; `python -m exoma` is the same entry point. For `/.verify-browser`, add the
browser extra: `uv tool install "exoma[e2e]"` (or `pip install "exoma[e2e]"`).

### 2. Read the dry run, then apply

```bash
exoma install                        # dry run: prints every change, writes nothing
```

Then pick how the workflow switches on and apply:

| You want | Install with |
| --- | --- |
| The workflow as the default way of working: plain requests are routed to commands | `exoma install --apply --auto-intent` |
| Your own setup untouched until you type a `/.<command>` | `exoma install --apply --only-command` |

On a terminal the installer asks which secondary agent to use; `--provider`/`--model` skip
the question. If unsure, choose opencode, the only provider whose read-only boundary is
enforced. The mode is
remembered for later upgrades; run the other flag to switch.

> **Do not skip this step.** It installs the permission block for the secondary agent as
> well as the skills and hooks; without it the secondary agent runs without write
> restrictions.

Pick command-only if you already rely on your own CLAUDE.md, or share the machine's Claude
Code with work unrelated to this workflow. Either way, the secondary agent's instructions
apply only to calls the workflow makes; using codex or opencode directly is unaffected.

## Enable it in a project (once per project, per person)

Open the primary agent (Claude Code or Agy) in the project and run:

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

Without a primary agent, or from a script, the skills run the same command:
`exoma init --work-dir <project>` (and `exoma upgrade`).

## Update

1. Update the package:

   ```bash
   uv tool upgrade exoma          # or: pip install -U exoma
   ```

2. Re-run the installer, dry run first. Run it from inside a project and it also upgrades
   that project's workspace:

   ```bash
   exoma install                    # what will change
   exoma install --apply            # keeps your mode and provider
   ```

   It refreshes the workflow's block, files and hooks, and removes files an older release
   installed that are no longer shipped, unless you edited them.

3. In every other project, open the primary agent and run `/.upgrade`. It refuses while a delegated
   command is running in that project; wait for it to finish. Do not skip it: the skills of a new
   release can call the runner in a form an older `.workflow/run.sh` does not read (0.3.0 moved
   it to named options), and every workflow command fails in that project until it is upgraded.

4. Run `/.doctor` in each project. `NEEDS_UPGRADE` or `run_script_drift` means step 3 was
   missed there; a provider warning names the CLI release this workflow version was tested
   on. `exoma install --update-providers --apply` brings an npm-installed codex or
   opencode to that release; anything else is reported, not changed
   ([provider releases](reference.md#provider-releases)).

Read the release notes ([CHANGELOG](../CHANGELOG.md)) before step 2: a release can change
behavior you rely on, and the notes say what to set afterwards.

## Undo

- **The last install went wrong:** `exoma install --rollback` (dry run), then
  `--rollback --apply`. It refuses if a file changed since the install, instead of
  overwriting your edits.
- **Remove the workflow:** `exoma install --uninstall` (dry run), then
  `--uninstall --apply`. It removes the workflow's block from each instruction file and keeps
  your text, removes only the files it installed and you did not edit, and takes its hooks
  and status line out of `settings.json`. The Agy host's two files are receipted like any
  other install write, so `--rollback` restores them. An uninstall can itself be rolled back. Project
  `.workflow/` folders and the per-user data directory (job records and the secondary-agent
  seed, see [workspace layout](reference.md#workspace-layout)) are left for you to remove,
  then `uv tool uninstall exoma` (or `pip uninstall exoma`).

## When something is wrong

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `/.doctor` reports `NOT_READY` / `run_script_drift`, or a command fails with `argument --prompt/-p: expected one argument` | Tool updated, workspace scripts not | `/.upgrade` in that project |
| `/.doctor` reports bundle drift | The package was updated, the global install was not | `exoma install --apply` |
| Provider not found | The agent CLI is not on `PATH` | Install it, then check `<cli> --version` |
| Delegated calls fail after a provider update | The provider release broke something | Pin it back to the release `/.doctor` names ([provider releases](reference.md#provider-releases)) |
| Commands fail after reinstalling Exoma or changing Python | Baked absolute paths are stale | `/.upgrade` in that project |
| `exoma install` refuses a CLAUDE.md or AGENTS.md | Its workflow markers do not pair up | Fix or remove the broken marker lines by hand, then re-run |

More: [troubleshooting](team-guide/troubleshooting.md).

## Good practice

- Read the dry run every time; the installer writes configuration every project reads.
- Run `exoma install --check` after an upgrade, and `/.doctor` in a project before
  relying on it.
- Keep your own rules outside the workflow's markers; anything inside is replaced on the
  next install.
- One install per machine. Exoma and agent-workflow install hooks and skills under the same
  names in `~/.claude`; whichever installed last owns them.
- Tell each project which test commands `/.verify` may run (`commands.verify_test_commands`
  in `.workflow/config.json`). Until you do, verify runs no test and cannot pass.
- Keep the secondary agent on the release this workflow version was tested with.
- To see what the workflow does for you over weeks of use:
  [measure your use](team-guide/measure-your-use.md).
