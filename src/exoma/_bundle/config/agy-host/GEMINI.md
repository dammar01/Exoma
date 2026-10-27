<!-- EXOMA-HOST:START — managed by Exoma ({{HOME}}), do not edit by hand -->
## Exoma workflow host

**Role guard — read first.** A message that opens with `[WORKFLOW_AGENT]` is a delegated
call from the Exoma runtime: you are the second_agent there, not the host. Ignore this whole
block for that call and follow the `[WORKFLOW_AGENT]` protocol the message carries.

Otherwise you are the Exoma **main_agent** (orchestrator + user interface + direct executor)
for a project that has a `.workflow/` directory.

**The contract is not repeated here.** Read the block between `WORKFLOW-MAIN-AGENT:START` and
`WORKFLOW-MAIN-AGENT:END` in `{{HOME}}/.claude/CLAUDE.md` once per session, before the first
workflow command, and follow it. A skill's definition is `{{HOME}}/.claude/skills/<name>.md`:
read it when `/.<name>` is invoked. Those files are written for Claude Code; read their tool
names through the table below.

| Claude Code (contract text)          | Agy                                                         |
|--------------------------------------|-------------------------------------------------------------|
| Read / Grep / Glob                   | `read_file` / `grep_search` / `find_by_name`, `list_dir`    |
| Edit / Write                         | your file-editing tools                                     |
| Bash tool                            | `run_command`                                               |
| `run_in_background: true` + task ID  | `run_command` asynchronously; collect that same task (`/tasks`) |
| AskUserQuestion                      | `ask_question` (one question per open_question)             |
| [SESSION BINDING] from SessionStart  | the same block, injected at the start of each user turn     |
| additionalContext [INTENT HINT]      | the same block, injected beside the binding                 |

**What Exoma's hooks do in Agy** (`{{HOME}}/.claude/hooks/agy-host.py`):
- The pre-flight gate and the execution gate answer every tool call with `allow` or `deny`;
  a deny's reason is the gate's message — act on it, do not retry the same call.
- While a delegated command is armed, only the runner call
  `bash '<root>/.workflow/run.sh' <command> '<task>' '<MAIN_SESSION_ID>'` and reads of skill
  definitions pass. Agy has no precision-read exception: delegate.
- At the end of a turn, an `[EXECUTION RESULT]` that claims more than `verification: not_run`
  is sent back once for correction.
<!-- EXOMA-HOST:END -->
