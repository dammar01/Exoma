"""/.verify-browser — browser verification driven by a per-session request.

Draft: second_agent proposes claims and a declarative scenario (`[E2E SPEC]`, a section
inside ordinary exploration evidence), validated locally and handed back for the user to
confirm. Run: the confirmed scenario goes through a local player as a supervised child
process that reports JSONL, a delegated hybrid review reads a compact `[E2E EVIDENCE]`
block, and the result is normalised into the canonical `[VERIFICATION]` contract.
Settings come from the request (`request.py`), which layers the run's own overrides on
top of the project's pinned `e2e` section in `.workflow/config.json` — a configured
project runs without being interviewed again.

Nothing in this package imports Playwright at module load: the runtime stays
stdlib-only, and the one module that needs the browser (`player`) imports it inside the
function that launches one. Contract: docs/runtime-contracts.md, /.verify-browser.
"""
