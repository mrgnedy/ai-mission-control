# Native Claude session execution

Select `.ai/orchestration/orchestration.yaml` `execution_strategy: claude-native`
and `control_plane.review_profile: native-review`. Ensure the matching strategy,
matrix and native profiles are present through a reviewed setup merge. Existing
CLI strategies remain available. Native execution supports Claude model aliases
haiku/sonnet/opus; effort inherits the parent. Do not invent a per-Agent effort
setting, enforce a worker timeout by guessing, or turn on Agent teams.

The main Claude session makes decisions and dispatches Agents; the ledger records
facts and guards transitions. Use the bundled executable for every command.

1. Complete intake, spec, plan, tasks and visible graph/delegation; run `validate
   --change specs/<change>`. Start with `session start --change specs/<change>
   --workspace .`. This reuses required-artifact/decision/readiness gates and
   freezes run inputs. Record the returned exact run ID.
2. Inspect `status RUN_ID --workspace .`. For each ready task, run `session claim
   RUN_ID TASK_ID --workspace .`. Only a ticket with `launch_allowed: true`
   authorizes one launch. Call native Agent with its `agent_type`, `model`, and
   `prompt`; keep its returned ID and resolved model. Do not create another task
   board or launch subprocess workers. Run only a stable reader batch or one
   writer, including that writer's checks. The ledger enforces active budgets.
3. Wait for actual completion or confirmed stop. Use Write to save a compact
   submission under `/tmp` or parent-owned `.ai-runtime/submissions/`, so it does
   not become an unrelated checkout edit: `claim_id`, actual `agent_id`, `status`, `summary`, optional
   `observed_model` (full resolved ID only when exposed by the tool, never the
   requested alias), `no_op_reason`, `failure`, `permission_request` or
   `decision_request`. Omit task identity, routes, changed paths and check results;
   the ledger collects those independently. Run `session finish RUN_ID --workspace
   . --result <submission.json> --worker-stopped --stop-evidence "<actual return/
   stop evidence>"`. It executes declared checks and rechecks scope and HEAD.
   Identical finish is idempotent. Inspect its actual result before proceeding.
4. Inspect partial failed edits and the reason; use existing `reopen RUN_ID TASK_ID
   --workspace . --reason "..." --checkpoint "..."`, adding `--partial-reviewed`
   only after inspecting edits. Claim that recovery task next. Unrelated tasks
   remain blocked until retained edits validate. Decision failures use existing
   resolution/authority/evidence fields. Frozen-scope changes require replanning.
5. When tasks complete, `session review RUN_ID --workspace .` reserves a fresh
   read-only review ticket. Dispatch it through Agent; never reuse any prior task
   or review Agent ID for this fresh review.
   Return `{claim_id, agent_id, review: {schema_version: "1.0", status: "pass" or
   "remediation_required", summary, issues}}` via `session review RUN_ID --result
   <review-submission.json> --workspace . --worker-stopped --stop-evidence "..."`.
   Recorded findings add bounded remediation tasks through the existing review
   mechanism; process them, then obtain a fresh review. On pass, inspect evidence
   in the parent and call `session review RUN_ID --workspace . --approval
   "<parent's acceptance reasoning>"`. No completion before independent review
   and this separate parent approval; workspace drift invalidates approval.
   If the reviewer fails or cannot launch, submit `{claim_id, agent_id: <actual
   ID or null>, status: "failed", summary}` with explicit stop evidence through
   the same review operation. It records a failed review and permits a fresh
   reviewer within the per-cycle attempt budget. Restore/reconcile any observed
   reviewer changes before retrying; a failed reviewer cannot approve work.
6. Run the mandatory retrospective for this exact run ID. `report RUN_ID
   --workspace .` regenerates the factual report; propose only supported lessons.

Native tools inherit parent permissions. A permission request does not grant them.
Review the exact command and partial edits against existing user authority, obtain
real parent permission when required, then reopen with `--native-recovery-evidence
"<actual permission evidence>"`. A Claude provider limit requires successful
availability evidence before that same option can reopen its failed task. These
records do not modify Claude settings or call CLI `grant-permission`/`recover`.

After interruption, inspect status and the actual tool/session state. Claims remain
owned even if the Python command or orchestrator process exited. A repeated claim
returns `launch_allowed: false`; it must not cause a second Agent launch. Do not
clear claims from a timer or PID. Reconcile returned/stopped Agents through finish.
If verification was interrupted, the claim remains `validating`; inspect its
commands and workspace, then close it as failed with stop evidence beginning
`reconciled:`. That closure collects current diff without rerunning commands; an
explicit verified reopen is required for another attempt. If liveness or command
outcome cannot be established, retain the claim and report the uncertainty.
Cross-session Agent continuation is not promised; disk evidence preserves progress
and stops duplicate dispatch. Ordinary native transcripts follow Claude's own
retention; the ledger does not create or manage a separate transcript store.

The writer's Edit/Write hook checks the active claim scope. Bash still uses parent
permission rules and can write; final independent diff/check validation remains
mandatory. External editor changes are detected, not prevented.
Concurrent shared-checkout CLI sessions use the same lock and refuse overlap;
CLI worktree execution keeps its existing concurrency. Claim responses carry one
bounded prompt; status summarizes ownership without repeating previous prompts.
