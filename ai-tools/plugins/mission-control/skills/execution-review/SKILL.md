---
name: execution-review
description: Semantically audit completed orchestrated work against tasks, acceptance criteria, diffs, verification, architecture, and cross-task behavior. Return a structured pass or remediation plan; do not silently repair findings.
---

# Execution review

Review completed task contracts, normalized results, actual diffs, test/static-analysis evidence,
repository rules, and the approved strategy. Check correctness, edge and failure behavior,
regressions, security, architecture fit, scope, complexity, test quality, and cross-task conflicts.
Treat explicit worker outcomes as evidence, not authority: `failed` and `decision_required` cannot
be reviewed as completed, and an explained no-op is valid only when the actual diff and task
criteria support it. For a substituted component or exceptional change, inspect the target API and
every affected in-scope call site; compilation or compatible arguments alone do not prove the
intended behavior.

Return only `.ai/orchestration/schemas/review.schema.json`:

- `pass` when all acceptance criteria and affected risks have evidence.
- `remediation_required` with one independently actionable issue per finding. Include severity,
  affected task, precise description, acceptance criteria, and the narrowest safe scope.

Do not edit code or ask the original worker to “fix everything.” The orchestrator converts review
issues into new routed tasks. Do not call work complete if required verification did not run.
