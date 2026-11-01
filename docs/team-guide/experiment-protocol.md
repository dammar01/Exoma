# Experiment protocol: collecting real sessions

For a team running a light-touch study of how Exoma is used: participants (interns, new team
members) work on real problems with it, and each finished session is collected with its
second-agent results and the participant's own review. The control is light on purpose. The
point is to see how the workflow behaves in ordinary work, not in a staged test.

The command is described in the [reference, Session export](../reference.md#session-export).
For your own aggregate figures across weeks of use, see [measure-your-use.md](measure-your-use.md).

## What counts as a session

**One problem.** One session is one real problem from start to finish: a bug from the
tracker, a feature from the backlog, a question about the codebase you actually need
answered. If a second problem comes up partway, finish or park the first and start a new
session (`/clear` or a new terminal) for the second.

**Real work only.** No made-up tasks, no "let's see what it does with X", and no re-running
the same task to compare outputs. A problem nobody needed solved tells us nothing about
whether the workflow helps.

**Natural use.** Phrase requests the way you normally would, and judge the result by its
evidence. Do not reshape your work to make the workflow look good or bad. If you would
normally skip the second agent for a task, skip it, and say so in the review.

## The study setup

The team lead decides these once, before anyone starts:

1. **Hosts and providers.** Who uses which main agent and second agent. Install with
   `exoma install <host>` and pick the provider when the installer asks
   ([installation](installation.md)).
2. **A comparison group (optional).** Some problems can be worked with the second agent
   switched off (`/.local`, or `WORKFLOW_LOCAL_MODE=1`). Decide this **before** a problem is
   picked up, by a simple rule (for example, alternate weeks), never after seeing how hard
   it is.
3. **Where bundles go.** A shared folder or a ticket per session. The bundle is redacted,
   but it still contains your prompts and the code the agents read: keep it inside the team.

## For each session

1. Start the session in the project and note the `MAIN_SESSION_ID` from the session binding
   (`/.doctor` also shows it).
2. Work the problem until it is solved, abandoned, or parked.
3. Export it from the project directory:

   ```bash
   exoma session export <MAIN_SESSION_ID>
   ```

   The first run writes `<MAIN_SESSION_ID>.review.md` beside the bundle and reports the review
   as not filled.
4. Fill in the review **the same day**: what the problem was, the outcome, which delegated
   calls helped or misled, and where time went. Leave a line blank rather than guessing.
5. Run the same export again. The bundle now carries the filled review, and the command says
   `review: filled`.
6. Hand over the newest `.zip`.

## What the bundle holds

The session's prompts, answers and per-call logs (with the second agent's archived evidence),
the rows of the usage and audit streams for that session, the host's transcript where the
host records it (Claude Code and Codex today; for OpenCode and Agy the bundle says
`transcript: unavailable`), and your review. Credential-shaped text is replaced with
`[REDACTED:<kind>]` before anything is written. Read the bundle before handing it over if the
session touched anything sensitive; the export can leave the transcript out
([reference, Session export](../reference.md#session-export)).

## Reading the results

Look across many sessions, not at one. Useful questions:

- How often did the second agent's evidence change what was done, and how often was it
  ignored or wrong?
- How much did delegated calls cost (time and tokens, from the usage rows) against the
  outcome?
- Where did participants correct the main agent's routing or confidence?
- Do the comparison-group sessions differ in outcome or time?

Treat a single session as an anecdote. Patterns that repeat across participants and problems
are the findings.
