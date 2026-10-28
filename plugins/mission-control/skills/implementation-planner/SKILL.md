---
name: implementation-planner
description: Convert an approved, repository-grounded implementation strategy into an atomic dependency graph with explicit scope, acceptance criteria, capabilities, risk, and verification. Do not use for provider selection or execution.
---

# Implementation planner

Produce `specs/<change>/orchestration/task-graph.yaml` matching
`.ai/orchestration/schemas/task-graph.schema.json`. Keep this file beside the change's governance
records so a person can inspect and edit the execution plan before a run. Validate it after every
manual edit; once a run starts, its runtime snapshot is immutable execution evidence, so source
edits require a new run rather than silently changing the active one.

Before creating the graph, read the spec's readiness record and its evidence. Challenge whether
the requested outcome, negative/regression behavior, affected callers, baseline failures,
pinned APIs versus project rules, and decision authority are sufficiently known for the tasks.
Resolve any material in-scope user choice first; if the user defers an outcome, exclude its tasks
from this mission. A document's presence is not proof of completeness. When material in-scope
decisions exist, index all of them in optional `orchestration/decisions.yaml`, including ones not
yet referenced by a task. Do not create an empty index as a readiness certificate.

- Create one task per independently verifiable outcome, not one per architectural layer.
- Carry the requirement, rationale, relevant context, allowed/forbidden paths, acceptance criteria,
  dependencies, capabilities, complexity, risk, and argument-array verification commands.
- Dependencies represent actual data or sequencing constraints. Keep independent tasks independent.
- Make scope narrow enough for changed-file validation. When two tasks share material write scope,
  encode a dependency rather than assuming parallel safety. Compare each task's scope with the
  files its own verification commands inspect or force it to change.
- Use a requested profile only when the approved strategy truly requires it; otherwise leave
  provider choice to the router.
- Cover failure paths and regression surfaces. A task is not complete merely because code exists.
- For substituted components or changed public contracts, name the in-scope call sites and the
  behavior to preserve or update; accepting a new argument is not proof that behavior is correct.
- Give each task a concrete postcondition and verification suited to its change. Review vague
  acceptance criteria semantically rather than relying on keyword checks.
- If a known decision is prerequisite to a task, record it in optional
  `specs/<change>/orchestration/decisions.yaml` and reference its ID from `task.decisions`. Resolve
  it before the task is runnable. Do not use this file as a speculative question queue.

Before returning, verify unique IDs, dependency existence, acyclicity, complete requirement
coverage, decision-reference readiness, and that no task asks an executor to make an unresolved
product decision.
