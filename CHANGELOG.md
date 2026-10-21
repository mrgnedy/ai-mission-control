# Changelog

## Mission Control 0.3.12 — 2026-10-22

Native-Agent execution is merged into `main`, retaining the existing `ai-tools`
marketplace and `mission-control@ai-tools` plugin ID. The original native branch
keeps its separate `ai-tools-native` catalog. CLI execution remains the default;
updating the plugin does not change repository-owned configuration or migrate runs.

### Added

- Opt-in `claude-native` execution strategy. The main Claude session dispatches
  native Agents from the existing task graph and delegation plan.
- Two restricted plugin Agents: `native-writer` for scoped editing and
  `native-reader` for read-only tasks and fresh independent reviews.
- Durable `session start/claim/finish/review` operations, active concurrency/cost
  and attempt-budget checks, immutable receipts, and separate parent approval.
- Native profiles, routing matrix, packaged/template schema contracts, setup
  guidance, resume guidance, status ownership, and run-report metadata.

### Reliability

- Independently collect changed files, Git HEAD, scope, and declared verification;
  recheck after verification commands, which may themselves write files.
- Keep failed partial work exclusive through inspected reopen and verified
  recovery; block unrelated native tasks and shared CLI checkout overlap.
- Preserve unresolved claims across command/session exit. Do not guess liveness,
  automatically reset stale claims, or duplicate launches.
- Reconcile immutable receipts without repeating completed checks; interrupted
  verification requires inspected failure closure rather than silent replay.
- Require fresh reviewer identity, formal result/review schemas, receipt identity
  binding, current workspace fingerprints, and explicit final approval.
- Record failed reviewer launches with bounded retry instead of leaving an
  unresolvable review claim.
- Use the existing segment-aware path matcher in the editing hook.

### Compatibility and limits

- Existing CLI strategies/executors remain available. Shared-checkout CLI runs
  now serialize against native ownership; isolated CLI worktree concurrency stays.
- Native model aliases are Haiku/Sonnet/Opus. Effort inherits the parent session;
  unsupported per-Agent effort is rejected.
- Parent tool permissions govern native workers. CLI provider Bash allow rules
  do not grant native-Agent permissions.
- A requested model alias is not an observed resolved model. Unknown actual models
  remain unknown when the Agent handback does not expose a canonical ID.
- The parent supplies truthful agent-stop/return evidence. Automatic
  cross-session Agent continuation or independent liveness detection is not promised.
- Repository-owned `.ai/orchestration/` requires a reviewed merge after updating.
  Existing run snapshots are not migrated; runtime switching is for new missions.

### Validation

- Full suite: 104 tests, 102 passed, two opt-in live-provider tests skipped.
- Separate synthetic live mission: one native writer, one fresh native reviewer,
  seven acceptance tests passed, separate parent approval, completed run.
- Independent Astra focused recheck: no remaining material findings.
- Branch packaging recheck: the same 102 enabled tests passed; marketplace and
  plugin manifests validated. An isolated older-install upgrade test confirmed
  local branch enablement, old-version availability in another repo, successful
  rollback, and a read-only setup divergence report that leaves config intact.
- Main merge recheck: 102 tests passed, two opt-in provider tests skipped;
  marketplace/plugin manifests validated. Isolated 0.3.11-to-0.3.12 update kept
  project configuration unchanged, and ai-tools-native-to-ai-tools migration left
  only the main plugin enabled.

See [upgrade and rollback instructions](ai-tools/plugins/mission-control/docs/UPGRADE_NATIVE.md).
This changelog begins with this release; it does not reconstruct earlier history.
