# Task framing

How you phrase a request decides how much Exoma can do for you.

## Two ways to ask

**Procedural** — you decompose the task and hand over one step at a time:

```text
Create the migration.
Create the model.
Create the API.
Create the frontend.
Add delete.
Add pagination.
```

**Objective-oriented** — you state the outcome and the constraints, and let the workflow
investigate and decompose:

```text
Implement TODO management end-to-end, consistent with the existing architecture,
including persistence, API, UI, and validation.
First understand how similar features are built here and identify every affected area,
then propose a plan before implementing.
```

## What actually changes

The difference is not the command. It is **who does the decomposition and the repository
discovery**.

- Procedural: you do both, in your head, before you type. The agent only executes.
  Exoma has little to add, because the exploring has already been done — by you,
  possibly with gaps.
- Objective-oriented: the workflow explores the repository, finds the conventions and the
  affected areas, and proposes the decomposition as a plan. You review and decide.

A procedural request with a slightly bigger scope ("create the table, then the CRUD") is
still procedural: you have still decided the steps.

## Neither is always better

For small, local changes, procedural requests are often **more efficient**: you already know
the steps, and exploration would only add latency. Use objective-oriented framing when you
want the agent to find out what you do not know yet. See [when-to-use.md](when-to-use.md).

## Writing a good objective

Include:

1. **The outcome** — what should exist or work when it is done.
2. **The boundary** — which part of the system, and what must not change.
3. **Consistency** — "follow the existing patterns for X".
4. **The order** — "understand first, then propose a plan", so nothing is written before
   you have seen the plan.

Avoid dictating file names or steps you are not sure about. If you already know them, say
so as a constraint ("the endpoint must live next to the existing orders API"), not as a
step list.

## Phrases the router recognizes

A few examples; the shipped Claude Code configuration holds the full map.

| Intent | Example phrasing |
| --- | --- |
| explore | "where is …", "which files …", "how does the flow for … work" |
| analyze | "why does …", "is it safe to …", "what is the impact if …" |
| plan | "I want to add …", "how should we build …", "design …" |
| verify | "check what was just done", "is it correct now" |

A mismatch is harmless: the primary agent prints `[INTENT] …` before it starts, so you can stop it
and rephrase, or use the explicit `/.plan`, `/.explore`, and so on.
