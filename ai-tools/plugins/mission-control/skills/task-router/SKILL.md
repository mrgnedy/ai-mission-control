---
name: task-router
description: Map a validated implementation task graph to named execution profiles using task characteristics, risk, capabilities, budgets, and isolation needs. Produces delegation only; never executes or rewrites tasks.
---

# Task router

Read `.ai/orchestration/orchestration.yaml`, resolve its selected execution strategy, then read
`.ai/orchestration/profiles.yaml`, every referenced provider manifest, and only the routing matrix
named by that strategy. Produce `specs/<change>/orchestration/delegation.yaml` matching the
packaged schema. This is the visible provider-routing half of the orchestration plan. A person may
inspect or edit it before execution, but rerunning the router overwrites it; always validate after a
manual edit. Never edit a run's runtime copy.

For every task:

1. Honor a valid approved requested profile.
2. Otherwise apply routing rules by descending priority over type, capabilities, complexity, and
   risk.
3. Reject any profile whose provider is outside the strategy or whose model, effort, isolation, or
   editing requirements exceed the provider's declared capabilities.
4. Resolve profile defaults into executor, isolation, model, effort, timeout, and attempt limit.
5. Record the matched rule and a human-readable reason.
6. For an unpinned task, apply the first matching `fallback_rules` entry from the selected routing
   matrix. Keep its candidates in cost-first order in `fallbacks`; verify each candidate uses the
   same isolation and an allowed provider. A requested profile is pinned and gets no implicit
   fallback. A person may explicitly add one to delegation before the run freezes.
7. Check expensive-task and parallel-agent budgets; only providers listed in
   `parallel_worktree_providers` may form a parallel worktree batch.

Do not change task scope, acceptance criteria, dependencies, or verification. Do not execute
anything. If no valid profile exists or the graph/delegation coverage differs, fail validation.
