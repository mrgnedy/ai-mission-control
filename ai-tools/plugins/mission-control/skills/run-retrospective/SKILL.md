---
name: run-retrospective
description: Review a Mission Control run's worker attempts, failures, verification, reviews, recoveries, and routing efficiency. Use when the user asks for a retrospective, run report, workflow weaknesses, autonomy improvements, or to inspect a run at any point; also use at Mission Control closeout after run or resume stops.
---

# Run retrospective

The deterministic report is evidence; your diagnosis is a hypothesis. Keep them distinct so a
workflow improvement can be judged by whether later runs actually improve.
Run CLI commands through `"${CLAUDE_PLUGIN_ROOT}/bin/ai-orchestrate"` so this skill uses its own
plugin version instead of an unrelated executable on `PATH`.

1. Identify the exact run. Use `"${CLAUDE_PLUGIN_ROOT}/bin/ai-orchestrate" runs --all --workspace .` if the ID is unknown; if
   several runs could match, ask the user to select one. Before a run exists, explain that there is
   no retrospective yet; offer a planning review only if requested.
2. Run `"${CLAUDE_PLUGIN_ROOT}/bin/ai-orchestrate" report RUN_ID --workspace .`. Read the generated `run-report.md` and, only
   where needed, its linked result, review, recovery, or permission evidence. Do not quote raw
   stdout/stderr, prompts, credentials, or permission commands into a shareable report.
3. If the run is active, interrupted, failed, blocked, or otherwise unfinished, label the review
   **provisional**. Do not call an unfinished attempt a failure without a recorded result, and do
   not infer a final outcome from an intermediate snapshot.
4. Check completion, first-attempt success, retries, failed-attempt time, verification coverage,
   review issues/remediation, route changes, and permission grants. Read the decision-incident log
   in `tasks.md` where present. Classify recorded questions as planned, avoidable, emergent, or
   unclassified; separately count false-completion and manual-drift incidents. Do not make zero
   questions a success metric: a necessary, timely question can prevent expensive rework. Treat
   token cost, true human intervention, and time-to-unblock as unknown unless independently
   recorded. A reroute or grant is not proof of autonomous success; check the later result and
   verification.
5. For each material incident, state: task and worker, observed failure and evidence, immediate
   action proposed or taken, reason, verified outcome (or `pending`), and likely cause clearly
   labeled as a hypothesis. Separate task-specific repair from a repeatable workflow weakness.
6. Recommend only changes supported by evidence. Prioritize recurring preventable pauses, weak
   cheap routes, missing verification, unnecessary retries, and review escapes. State expected
   benefit, risk/overhead, and how a later run would validate the improvement. Do not change
   provider policy, routing, or project permissions merely because a retrospective recommends it.

Use a short, repeatable authored-review structure:

- Snapshot and outcome: run ID, final or provisional, completed/total tasks, verification and
  review status, and the generated-report path.
- Incidents: observed failure, worker/attempt, evidence, proposed or taken action, and verified
  outcome. Mark unrecorded proposals as `not recorded`; do not reconstruct them as facts.
- Improvements: prioritize only supported, actionable proposals, each with expected benefit,
  overhead/risk, and a later-run validation signal. Write `none justified yet` when appropriate.
- Unknowns: unavailable cost, human-intervention, or timing data that affects interpretation.
  If decision incidents were not recorded, say so rather than reporting zero.

Write or update `specs/<change>/orchestration/run-reviews/<run-id>.md` if the run has a change
directory. Preserve user edits and earlier snapshots; append a dated review when revisiting a run.
Keep this authored review separate from generated `.ai-runtime/<workflow>/<run-id>/run-report.md`,
which is replaced on regeneration. If the run has no change directory, deliver the review in the
conversation and link the generated report. In either case, give the user the concise outcome and
the exact report location.
