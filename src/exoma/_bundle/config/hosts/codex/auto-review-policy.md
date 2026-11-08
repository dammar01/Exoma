## Exoma authorization context

Apply this context only to Exoma work explicitly requested by the user in the active
project identified by its .workflow workspace and MAIN_SESSION binding. The task's own
scope governs source files, repositories, destinations and data. This context grants
no blanket trust to unrelated projects, configured providers or network destinations.

Routine inspection of non-secret source, documentation and configuration, git status/diff,
and deterministic local measurements such as LOC or AST statistics are part of that scope.
For requested explore/plan/analyze/verify work, the installed Exoma runner may invoke the
user-selected provider as a read-only evidence-gathering second agent. Normal authenticated
CLI inference for that authorized task is distinct from extracting or uploading credentials.
Assess the proposed command, inputs, destinations and actual data flow.

Keep all existing deny rules, sandbox boundaries and managed requirements. Do not read
secret .env files, keys or credential stores, dump environment values, or disclose tokens
or unrelated private data. Source edits require the active plan and execution approval.
Commits, pushes, publishing, destructive changes and additional permissions require their
own authorization. An explicit denial still requires the normal approval path for that
exact action; this context does not authorize a workaround or automatic retry.
