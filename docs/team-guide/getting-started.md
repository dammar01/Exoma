# Getting started

## One-time setup (each team member)

Follow the [installation guide](../installation.md); it also covers upgrading and
uninstalling, and [installation.md](installation.md) helps you choose a mode. In short:

1. Clone the repository to a permanent location.
2. Run the global install once per machine. Do not skip it: it installs the Claude Code
   skills and hooks (and, where Agy is installed, its host block and hook) **and** the permission block for the secondary agent. How much that
   block actually enforces depends on the provider: with `opencode` writes are prevented;
   with `codex` the boundary is unproven; with `agy` changes are only detected afterwards.
3. Run `/.init` once per project, on your own machine (`/.upgrade` when the project already
   has a `.workflow/`). The generated `.workflow/` contains
   absolute paths from your machine, so it is never committed or shared.
4. Run `/.doctor` and check that it reports `READY`.

Which secondary agent to choose, and what each one enforces: [security](../security.md). If unsure, use `opencode`.

## Day to day

You do not type runtime commands. You talk to the primary agent (Claude Code or Agy) normally,
and it recognizes what kind of request you are making:

| You ask… | The primary agent runs | What comes back |
| --- | --- | --- |
| "Where is …", "which files handle …" | `explore` | A map: entry points, files, ownership |
| "Why does …", "is it safe to …" | `analyze` | A causal explanation, no code changes |
| "I want to add …", "plan how to …" | `plan` | Steps, risks, open questions, options |
| "Implement it", "go ahead" | `/.execute`, with your explicit approval | Code changes, made by the primary agent itself |
| "Is it correct now?", "check what was just done" | `verify` | A verdict with evidence |
| "Test it in the browser", "click through the login flow" | `verify-browser` | A verdict from a real browser run, reviewed by the secondary agent; or, when the drafted test is blocked or invalid, the reason it could not run ([reference](../runtime-contracts.md)) |

Before running a delegated command, the primary agent prints one line such as
`[INTENT] explore — location question`. If it guessed wrong, stop it (Esc in Claude Code) and rephrase, or use
the explicit form (`/.explore`, `/.analyze`, `/.plan`, `/.verify`, `/.verify-browser`). The full list of trigger
phrases lives in the shipped Claude Code configuration (`src/exoma/_bundle/config/hosts/claude/CLAUDE.md`,
"Command registry").

## Three things that surprise new users

1. **Delegated requests take time.** The secondary agent actually reads your code. Expect
   anything from under a minute to several minutes for broad analysis or planning. See
   [when-to-use.md](when-to-use.md) for when that is worth it.
2. **Nothing is implemented without your go-ahead.** `/.execute` needs an explicit approval
   from you; without it, it only shows what it would change. A plan with open questions
   stops and asks you first. Exact form: [prompt-layer contracts in the
   reference](../reference.md#kontrak-lapisan-prompt).
3. **Implemented is not verified.** By default `/.execute` does not run verification by
   itself; it reports `implemented` and offers `/.verify`. A project setting can make it
   chain automatically ([reference](../reference.md#kontrak-lapisan-prompt)).
