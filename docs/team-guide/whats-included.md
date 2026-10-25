# What's included

Everything an install gives you, grouped by what you would use it for. Each entry says when to
reach for it; exact behavior is in the [reference](../reference.md#command) and the
[runtime contracts](../runtime-contracts.md).

## Commands that ask the secondary agent

These delegate the reading to the secondary agent and come back with evidence. They take
minutes and spend the secondary agent's quota.

| Command | Reach for it when | You get |
| --- | --- | --- |
| `/.explore` | you need to know where something lives or how it flows | a map: entry points, files, callers |
| `/.analyze` | you need to know why something happens, or whether a change is safe | a causal explanation with sources, no code changes |
| `/.plan` | you are about to build or change something | steps, risks, open questions, and the real alternatives |
| `/.verify` | something was just changed and you want proof it is right | a verdict (pass, fail, incomplete) with evidence |
| `/.verify-browser` | the proof has to come from a real browser | a browser run reviewed by the secondary agent |

## Commands Claude Code runs itself

| Command | Reach for it when |
| --- | --- |
| `/.execute -y` | a plan is agreed and you want it implemented (the `-y` is your approval) |
| `/.sweep` | you want to see what the current changes touch before verifying or committing |
| `/.commit` | you want your changes committed: it stages them when nothing is staged (secret-looking files skipped), proposes the message, and asks before committing |
| `/.local` | the secondary agent is unavailable, or you want Claude Code to gather evidence itself |
| `/.promote` | verified findings should become shared, Git-tracked project knowledge |
| `/.memory` | a lesson should be kept in your personal notes across projects |
| `/.provider` | you want a different secondary agent, model, or reasoning effort |
| `/.doctor` | a project's workflow setup seems broken, or before relying on a new one |
| `/.init`, `/.upgrade` | setting up a project, or refreshing it after updating Exoma |
| `/.caveman` | you want shorter answers |
| `/.help` | you want the command list in the terminal |

In auto-intent mode you can ask in plain language and Claude Code picks the command; in
command-only mode you type the command. A refactor is planned and executed like any other
change (`/.plan`, then `/.execute -y`); for a code review or a prose-file compression use
Claude Code's own commands. See [installation](installation.md#choose-a-mode-always-on-or-only-when-called).

## Hooks: what runs without being asked

| Hook | What it does for you |
| --- | --- |
| Session binding | gives each Claude Code session an identity, so two sessions in one project do not collide |
| Intent gate (auto-intent only) | once a request is recognised as needing evidence, stops Claude Code from reading the codebase itself until the secondary agent has been asked |
| Task events | records which skills ran, which files were edited and which commits followed, so the report can tell finished work from abandoned work |
| Graph refresh | after an implementing turn, rebuilds the code graph in the background so the next request finds the new code |
| Status line | shows the secondary agent's token use and what it kept out of your context in this session |

None of these blocks your answer: a hook that fails is skipped.

## Reports

`report` in a project summarises its recorded history: how often verification passes, what
delegated calls cost, how often stored evidence was reused, and how tasks ended. It reads
local files only and sends nothing anywhere. See [reference](../reference.md#cli-langsung).

## Not included

The workflow does not commit, push or deploy on its own, and never writes a project's
`.workflow/` into Git.
