# Examples

## Same task, two modes

**Task:** add a notification preference feature — users choose which notifications they
receive.

### Mode A — direct, procedural

You decompose it, and send one instruction after another to the coding agent:

```text
create migration → create model → create API → create route
→ create component → connect API → add validation → test
```

This works. It is fast when you already know where each piece belongs in this codebase. The
risk sits in what you did not know: an existing settings module you should have extended,
a permission check every endpoint here goes through, a second client that also reads user
settings.

### Mode B — Exoma, objective-oriented

One request:

```text
Implement notification preferences end-to-end. First understand the existing
architecture and identify all affected areas, then propose a plan before implementation.
```

What happens:

| Stage | Who works | What you get | What you decide |
| --- | --- | --- | --- |
| `explore` | secondary agent reads the repository | where user settings, notifications, and the relevant API/UI live, with `file:line` anchors | nothing yet |
| `plan` | secondary agent gathers evidence; the primary agent reasons | ordered steps, affected files, risks, open questions, options | answer the open questions, pick an option |
| `/.execute` | the primary agent writes the code | the changes, reported as `implemented` | give the explicit approval ([how](../reference.md#kontrak-lapisan-prompt)) |
| `/.verify` | secondary agent checks the result | a verdict with evidence | whether it is done, or needs another pass |

The explore stage is often absorbed into `plan`; you can also ask for it explicitly.

Mode B is slower up front: every delegated stage takes from under a minute to several
minutes. What you get for it is that the decomposition is based on what the repository
actually contains, and you review it before any code is written.

## Root cause

```text
Why do some notification emails go out twice?
```

Runs `analyze`. The answer is a causal explanation with anchors, and no code is changed.
Follow up with "plan a fix" once you agree with the cause.

## Blast radius of your own changes

```text
What does my current working tree touch?
```

Runs `sweep`, which is **local**: it reads your git diff without calling the secondary
agent, so it returns quickly. Useful before a review or a commit.

## When not to use it

```text
Rename the `sendMail` helper to `sendEmail` in utils/mail.ts.
```

You know the file and the change. Ask the coding agent directly.
