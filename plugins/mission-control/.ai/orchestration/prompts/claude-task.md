Execute exactly one approved task and return only a result matching the supplied JSON Schema.

Task: {{task_id}} — {{title}}
Objective: {{objective}}
Why: {{why}}
Dependencies complete: {{dependencies}}
Context: {{context}}
Allowed paths: {{allowed_scope}}
Forbidden paths: {{forbidden_scope}}
Acceptance criteria: {{acceptance_criteria}}
Verification: {{verification}}

Follow this code discipline:
{{code_discipline}}

Do not expand scope, edit forbidden paths, commit, push, create a pull request, install software,
or access credentials. If the task is ambiguous, return a configuration failure instead of
inventing product behavior.

Report the truth in the structured result. `outcome: completed` means the task's acceptance
criteria hold; a successful tool call or passing generic tests alone do not establish that.
For a writing task with no changed files, include `no_op_reason` identifying the existing
implementation and concrete evidence that the task was already satisfied.

If an essential Bash command is denied, do not retry it or claim completion. Return
`outcome: permission_required` with the exact denied simple command (not a shell compound), why
it is needed, and a concise checkpoint of completed and remaining work.
If a material decision is missing, return `outcome: decision_required` with `decision_request`:
`question`, `evidence`, `checkpoint`, and any known `options`; do not make the choice or ask in
prose while claiming completion. If implementation or required verification failed, return
`outcome: failed` and describe the failure. Omit request fields for other outcomes.
