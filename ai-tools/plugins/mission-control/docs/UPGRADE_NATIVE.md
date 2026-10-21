# Upgrade an older installation to the native-Agent branch

Branch: `feat/claude-native-agents`. Plugin version: `0.3.12`.
Branch marketplace: `ai-tools-native`; original marketplace: `ai-tools`.

This procedure enables the branch in one repository for you only. It leaves the
older plugin installed, keeps other repositories on their existing setup, and
does not switch the shared `ai-tools` marketplace to a feature branch.
Use a fresh Claude session with native Agent support; the implementation was
live-tested with Claude Code 2.1.283.

## 1. Stop at a safe boundary and back up project configuration

Do not change runtime or configuration while workers are active. Finish/stop and
inspect them first. An older mission stays tied to its frozen configuration and
runtime: finish it under the old plugin, or keep it for inspected recovery rather
than silently converting it to native execution.

From the target repository root:

```bash
git status --short
claude plugin list
claude plugin marketplace list
backup_dir="$(mktemp -d /tmp/mission-control-config-backup.XXXXXX)"
if test -d .ai/orchestration; then
  cp -R .ai/orchestration "$backup_dir/orchestration"
fi
printf 'Configuration backup: %s\n' "$backup_dir"
```

Keep the printed backup path. This copies, not replaces or deletes, project files.
If the old plugin's marketplace has a different name, substitute the exact old
plugin ID shown by `claude plugin list` below.

## 2. Install the branch locally in this repository

```bash
claude plugin marketplace add \
  'https://github.com/mrgnedy/ai-mission-control.git#feat/claude-native-agents' \
  --scope local
claude plugin disable mission-control@ai-tools --scope local
claude plugin install mission-control@ai-tools-native --scope local
claude plugin list
```

The explicit local disable prevents two versions with the same Mission Control
skills/agents/hooks from being enabled together. Do not uninstall the old
marketplace: it may also supply learning-loop and commit-changes.
Confirm native version 0.3.12 is enabled and the old Mission Control version is
disabled in this repo. Existing user/project declarations may remain listed.

Shell plugin updates require a fresh session or the reload requested by Claude.
Restart Claude in this repo; do not rely on `/reload-skills` alone to reload new
Agent definitions and hooks. Confirm the two Mission Control native Agents in
`/agents` before choosing native execution.

## 3. Reconcile the repository-owned configuration

In the new Claude session invoke:

```text
/mission-control:setup-orchestration
```

Then say:

> Merge this repository's existing .ai/orchestration with the installed 0.3.12
> templates. Preserve my providers, allowed commands, costs, routing, methodology
> gates, and project-only files. Show the changed-file list. Do not start execution.
> After the reviewed merge, enable claude-native for new missions by setting
> execution_strategy to claude-native and control_plane.review_profile to
> native-review. Validate the result with the bundled executable.

Choose **Merge**, not Replace, when setup reports divergence. Setup must show
policy conflicts before changing them. A merge is deliberate agent-assisted work,
not an automatic three-way merge or blanket copy.

Expected native additions:

- `strategies/claude-native.yaml`.
- `routing-claude-native.yaml`.
- `native-*` entries in `profiles.yaml`, preserving existing profiles/budgets.
- Updated `schemas/execution-strategy.schema.json` and `schemas/result.schema.json`.
- Selector changes in `orchestration.yaml`:
  `execution_strategy: claude-native`,
  `control_plane.review_profile: native-review`.

For much older versions, setup may identify additional missing contracts; review
its exact report rather than limiting the merge to this list. If a project-only
run-manifest schema exists, review its worker_runtime contract against the
packaged schema instead of blindly replacing project policy.

To keep CLI mode, omit the enable-native instruction and retain the current
selector/review profile. Plugin installation itself does not activate native mode.

The agent must run, using its plugin-root expansion:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/ai-orchestrate" doctor \
  --config .ai/orchestration/orchestration.yaml
```

`CLAUDE_PLUGIN_ROOT` is resolved in plugin execution; it is not normally set in
your standalone shell. Do not use an older `ai-orchestrate` on PATH.
Doctor's `requires_live_session` native capability status is intentional: installed
CLI flags cannot prove the current session exposes the native Agent tool.

## 4. Start a new mission

Use `/mission-control:mission-control` for a new change. The orchestrator prepares
the same spec/plan/tasks and visible task graph/delegation. With the native selector,
it uses `session start`, claims ready tasks, calls native Agent, collects actual
stop/results and independent checks, obtains fresh review, and records approval.
It must not use CLI `run/resume/recover` to execute a native run.

No existing `specs/` or `.ai-runtime/` snapshots are rewritten by installation or
setup. Do not switch an active native mission back to CLI by editing its snapshot.

## Later updates from this branch

From the repo root:

```bash
claude plugin marketplace update ai-tools-native
claude plugin update mission-control@ai-tools-native --scope local
```

Restart/reload the plugin, inspect the changelog, and invoke setup again for a
reviewed project-config merge. The pinned marketplace follows this branch rather
than `main`.

## Roll back

First stop and reconcile native workers; never discard an unresolved claim.
Then, from the same repo:

```bash
claude plugin disable mission-control@ai-tools-native --scope local
claude plugin enable mission-control@ai-tools --scope local
```

Inspect and restore the saved configuration contents, preserving any unrelated
project changes made since backup. Do not blindly copy over the live directory:
that leaves branch-only files behind and may destroy new customization. A clean
rollback can move the current configuration to a new backup and copy the saved
directory into place after you explicitly choose restoration.

Restart Claude and validate the restored config using the old bundled executable.
Keep native run records for evidence; the old CLI must not be asked to resume them.
You can leave the native marketplace installed and disabled; no uninstall is needed.

## What changes where

| Action | Changes |
|---|---|
| Branch marketplace add | Local repo marketplace declaration and Claude marketplace cache |
| Disable old / install branch at local scope | Local repo plugin enablement settings and installed plugin cache; no source code changes |
| Setup merge | Only reviewed repository-owned .ai/orchestration files |
| New mission | New change artifacts and runtime records as usual |

Local scope uses `.claude/settings.local.json`, not the shared project settings.
If that file is tracked in your repo, review/exclude the change yourself before
committing. This guide does not install or update anything in design_system.

References: [Claude plugin management](https://code.claude.com/docs/en/discover-plugins)
and [plugin commands](https://code.claude.com/docs/en/plugins-reference).
