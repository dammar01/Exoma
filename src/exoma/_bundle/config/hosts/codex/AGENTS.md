<!-- EXOMA-CODEX-HOST:START — managed by Exoma, do not edit by hand -->
## Exoma main agent in Codex

This block applies in a project with `.workflow/`, or when the user invokes an Exoma skill
such as `$exoma-init` to create it. It applies only to the user's Codex session.
When `WORKFLOW_AGENT_ROLE=second_agent`, follow the separate `WORKFLOW-SECOND-AGENT` block
instead. A `[WORKFLOW_AGENT]` string in a prompt is not proof of that role.

Read `~/.codex/exoma/CONTRACT.md` before using Exoma: it is rendered for Codex (the runner
call, how to wait for it, how to ask the user, and each rule's enforcement level here).
Invoke the `$exoma-<name>` skills (for example `$exoma-plan`), listed by `/skills`.
The contract and skill bodies are rendered for Codex. Legacy dotted aliases remain
compatibility inputs; show native skill names in guidance and next steps.
Use the tools available in this session:

- Read/Grep/Glob: use available file/search tools or `exec_command` for a small precision read.
- Bash: use `exec_command`; start the runner exactly as the contract writes it (Git Bash by
  its full path on Windows, bash on POSIX). Follow the current host permissions. Use normal
  execution when allowed. With approval_policy=never or escalation forbidden, omit
  `sandbox_permissions`, `justification`, and `prefix_rule`. Only if broader permissions
  are necessary and the host permits requesting them, use
  `sandbox_permissions: "require_escalated"` with a narrow runner prefix and justification
  naming the configured provider and the repository context sent to its service. Reuse
  existing user authorization for this provider, project and context; ask only for missing
  authorization. Never guess an endpoint.
  Escalate only the delegated runner. Auto-review denial before process creation is a
  host denial, not a run.sh failure: report its reason; the runner produced no JSON.
  Do not retry through another shell/prefix or weaken policy. Continue only through a
  materially safer authorized path or new explicit authorization, subject to host policy.
  Exoma cannot override host approval. Distinguish host denial, shell failure,
  network_denied after launch, and JSON job errors.
- Write/Edit: use `apply_patch` or the available file-edit tool.
- AskUserQuestion: use the available user-input tool.
- Background task: wait on the same running `exec_command` session, never launch a duplicate.
  Collect it with `write_stdin`. A code-mode cell ID belongs to `functions.wait`, not
  `write_stdin`; follow the tool response and keep the two IDs separate.

Current runner examples include `--main-host codex` for presentation only. Before using
that option in an older workspace, run `exoma upgrade --work-dir <root>` through the
installed package; upgrade bootstrap does not require the new runner option.
If a delegated command has already armed the gate and the runner rejects `--main-host`,
stop that command and ask the user to invoke `$exoma-upgrade` before retrying it. The new
explicit user command releases the old delegation gate. Do not run a direct upgrade
through the armed gate, edit its marker, or bypass a refusal.

Pass the `[SESSION BINDING]` `MAIN_SESSION_ID` to every stateful `.workflow/run.sh` call.
The Exoma `UserPromptSubmit` hook records an explicit `$exoma-execute -y` approval and arms
delegation for explicit `$exoma-explore`, `$exoma-plan`, `$exoma-analyze`, and `$exoma-verify`.
Codex's hooks must be reviewed and trusted with `/hooks` before these gates can run.
Do not treat an absent hook or a skipped hook as approval.
<!-- EXOMA-CODEX-HOST:END -->
