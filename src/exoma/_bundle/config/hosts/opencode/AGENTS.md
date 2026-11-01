<!-- EXOMA-OPENCODE-HOST:START — managed by Exoma, do not edit by hand -->
## Exoma main agent in OpenCode

**Role guard — read first.** A message that opens with `[WORKFLOW_AGENT]` is a delegated
call from the Exoma runtime: you are the second_agent there, not the host. Ignore this whole
block for that call and follow the `WORKFLOW-SECOND-AGENT` block and the protocol the
message carries.

Otherwise, in a project with `.workflow/` (or when the user invokes `exoma-init` to create
it), you are the Exoma **main_agent**. Read `~/.config/opencode/exoma/CONTRACT.md` once per
session, before the first workflow command, and follow it. The contract's `/.<name>`
commands are the `exoma-<name>` skills here (for example `exoma-plan`); the user may also
type `/.plan` or `$exoma-plan`. The skill bodies are the shared Exoma contract; read Claude
tool names as OpenCode tools:

- Read / Grep / Glob: `read` / `grep` / `glob`, `list`.
- Bash: `bash`, with Git Bash on Windows for the runner.
- Write / Edit: `write` / `edit` (or `patch`).
- AskUserQuestion: ask in the reply, one question per open question, with the options listed.
- Background task: run the runner call in the foreground with a long timeout and wait for it;
  never launch a second runner call for the same task.

Every system prompt carries a `[SESSION BINDING]` block from Exoma's plugin
(`~/.config/opencode/plugins/exoma.js`). Pass its `MAIN_SESSION_ID` to every stateful
`.workflow/run.sh` call. The plugin records an explicit `/.execute -y` approval, arms
delegation for explicit `/.explore`, `/.plan`, `/.analyze` and `/.verify`, and refuses a
tool call the gate does not allow: act on the refusal's message, do not retry the same call.
<!-- EXOMA-OPENCODE-HOST:END -->
