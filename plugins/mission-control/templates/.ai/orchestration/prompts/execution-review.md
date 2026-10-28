Perform a read-only semantic review of the completed workflow. Return only JSON matching the
provided review schema. Do not edit files, run mutation commands, commit, or push.

Workflow objective:
{{workflow_objective}}

Task graph:
{{task_graph}}

Normalized execution results:
{{results}}

Review the actual workspace diff and repository rules in the current working directory. Check every
task's acceptance criteria, failure paths, regression surfaces, scope, architecture, security,
complexity, test quality, and cross-task integration.

Return `pass` only when the implementation and evidence cover the complete graph. Otherwise return
`remediation_required` with one independently actionable issue per finding. Each issue must name the
affected task, severity, precise description, acceptance criteria, and the narrowest safe scope.
