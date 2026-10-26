# Skill: memory
description: Propose a memory update to the personal knowledge files.
argument-hint: <note>

## Trigger
/.memory <note>

## Execution
1. Evaluate: does it affect future decisions? new ownership/architecture? a recurring landmine?
2. [MEMORY PROPOSAL] file | action (add|update|overwrite) | content. "Confirm? (yes/no/edit)"
3. yes → write + update the {AGENT_DIR}/memory/MEMORY.md index. no → discard. edit → wait for the correction.
4. Append to SESSION_LOG.md: [date] task | domain | memory written.
Do NOT write memory mid-session without confirmation.
