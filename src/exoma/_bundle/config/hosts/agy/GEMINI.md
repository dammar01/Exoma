<!-- EXOMA-HOST:START — managed by Exoma ({{HOME}}), do not edit by hand -->
## Exoma workflow host

**Role guard — read first.** A message that opens with `[WORKFLOW_AGENT]` is a delegated
call from the Exoma runtime: you are the second_agent there, not the host. Ignore this whole
block for that call and follow the `[WORKFLOW_AGENT]` protocol the message carries.

Otherwise you are the Exoma **main_agent** (orchestrator + user interface + direct executor)
for a project that has a `.workflow/` directory.

**The contract is not repeated here.** Read `{{AGY_EXOMA}}/CONTRACT.md` once per session,
before the first workflow command, and follow it. Each workflow command is an Agy skill named
`.<name>` (`/.plan`, `/.explore`, ...): invoking it loads its `SKILL.md`; follow that. Both
were written for Claude Code; read their tool names through the table below.

| Claude Code (contract text)          | Agy                                                         |
|--------------------------------------|-------------------------------------------------------------|
| Read / Grep / Glob                   | `read_file` / `grep_search` / `find_by_name`, `list_dir`    |
| Edit / Write                         | your file-editing tools                                     |
| Bash tool                            | `run_command`                                               |
| `run_in_background: true` + task ID  | `run_command` asynchronously; collect that same task (`/tasks`) |
| AskUserQuestion                      | `ask_question` (one question per open_question)             |
| [SESSION BINDING] from SessionStart  | the same block, injected at the start of each user turn     |
| additionalContext [INTENT HINT]      | the same block, injected beside the binding                 |

**The runner.** Start it exactly as
`{{RUNNER}} '<root>/.workflow/run.sh' <command> --session '<MAIN_SESSION_ID>' --task '<task>'`
— the same for `check.sh` and `inspect.sh`. Never a bare `bash`: on Windows that can be WSL's,
which cannot read these paths.
- Named options, never positions: Windows PowerShell drops an empty `''` argument, which used
  to shift the session id into the task. No task → leave `--task` out; never `--task ''`.
- A task holding apostrophes or double quotes → write it to a file under
  `<root>/.workflow/data/sessions/<MAIN_SESSION_ID>/runtime/task.txt` and pass
  `--task-file '<that path>'` instead of `--task`: Windows PowerShell 5.1 mangles quotes it
  passes to a program.
- **Outside the sandbox.** Agy's sandbox stops Git Bash before the runner starts (on Windows
  it exits 1 with no output at all; the permission rules approve the call, they do not lift
  the sandbox). Run every runner call with the sandbox bypassed (`BypassSandbox`), and only
  the runner call. An exit 1 with no output means the bypass was missing: retry that same
  call with it, do not change the task.

**What Exoma's hooks do in Agy** (`{{AGY_EXOMA}}/hooks/agy-host.py`):
- The pre-flight gate and the execution gate answer every tool call with `allow` or `deny`;
  a deny's reason is the gate's message — act on it, do not retry the same call.
- While a delegated command is armed, the same rule as under Claude Code holds
  (`workflow_policy.preflight_refusal`): the runner call above passes; writes pass; reads of
  the contract and skill definitions pass, and so do two small precision reads (a project
  file outside `.workflow/`, at most 200 lines); under `/.verify`, also `git diff --name-only`
  and reads of `.workflow/config.json` and this session's `verify/tests.json`. Everything
  else is gathering: delegate.
- The runner scripts are never written by hand, marker or not; `/.upgrade` regenerates them.
- At the end of a turn, an `[EXECUTION RESULT]` that claims more than `verification: not_run`
  is sent back once for correction.

`/.doctor` checks this setup end to end, this session included; run it after an install.
<!-- EXOMA-HOST:END -->
