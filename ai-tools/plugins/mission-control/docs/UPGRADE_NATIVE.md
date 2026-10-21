# Upgrade to Mission Control 0.3.13 on main

Marketplace: `ai-tools`. Plugin: `mission-control@ai-tools`.
Native Claude Agent execution is included but remains opt-in; the existing CLI
runtime is still the default. Installation/update and project-config reconciliation
are separate operations.

## 1. Stop at a safe boundary and back up configuration

Do not update the executing plugin, switch runtime, or edit frozen configuration
while workers are active. Finish/stop and inspect them first. An older mission
stays tied to its frozen configuration and runtime: finish under its old plugin,
or retain it for inspected recovery rather than converting it to native execution.

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

Keep the printed path and a known-good plugin source/cache path from the old
installation if rollback may be needed. This copies configuration without replacing
or deleting it.

## 2. Update an existing ai-tools installation

When `ai-tools` already points at this repository's remote default branch:

```bash
claude plugin marketplace update ai-tools
claude plugin update mission-control@ai-tools
claude plugin list
```

Update uses the installed scope by default. If several scopes are installed, use
`--scope user`, `--scope project`, or `--scope local` to target the intended one.
Confirm version 0.3.13. A user-scope update affects other repos using that user
installation; use a local installation below to try this only in one repo.

A marketplace pinned to a branch/tag follows that ref, not main. A local-path
marketplace follows its local source. Inspect `marketplace list`: intentionally
retarget a pin to main or update the appropriate local checkout before refreshing.
Do not assume marketplace update changes its source.

For a first installation, add the normal marketplace once, then install locally:

```bash
claude plugin marketplace add mrgnedy/ai-mission-control --scope local
claude plugin install mission-control@ai-tools --scope local
```

If you only want a local installation of an already registered marketplace, skip
the add command and run install with `--scope local`.

### Migrating from ai-tools-native

If this repo enabled the separate native marketplace, avoid loading both versions:

```bash
claude plugin disable mission-control@ai-tools-native --scope local
claude plugin marketplace update ai-tools
claude plugin install mission-control@ai-tools --scope local
claude plugin update mission-control@ai-tools --scope local
claude plugin list
```

These commands assume `ai-tools` is already registered against main; otherwise add
the normal marketplace first. Installing at local scope enables that installation,
including when the native-branch procedure had locally disabled the older plugin.
Confirm ai-tools is enabled and ai-tools-native is disabled. Only if ai-tools is
still disabled, use `claude plugin enable mission-control@ai-tools --scope local`;
an unconditional enable reports an error when it is already enabled.
Do not remove either marketplace as a shortcut: other plugins or repos may still use it.

Restart Claude in this repo after updating; do not rely on `/reload-skills` alone
to reload Agent definitions and hooks. Confirm the native reader/writer in
`/agents` before choosing native execution. The native implementation was
live-tested with Claude Code 2.1.283; a CLI/auth check alone does not prove the
current session exposes native Agent dispatch.

## 3. Merge repository-owned configuration

In the new Claude session invoke:

```text
/mission-control:setup-orchestration
```

Setup now asks which available strategy to use, then prints compatible worker,
fallback, and control-plane profiles with their models for optional edits.
You can keep the current choices, name changes by profile ID, or review/edit the
proposed files before applying. If configuration diverges, choose Merge to retain
customizations. You can supply preferences up front:

> Merge this repo's .ai/orchestration with the installed 0.3.13 templates. Preserve
> my provider permissions, budgets, existing profiles/routing, methodology gates,
> and project-only files. Show changed files and validate using the bundled
> executable. Do not start execution.

Choose **Merge**, not Replace. Setup first reports changed, template-only, and
project-only files. Policy conflicts require a deliberate decision; merging is
agent-assisted semantic work, not an automatic blanket copy.

Native additions compared with stock 0.3.11:

- `strategies/claude-native.yaml` and `routing-claude-native.yaml`.
- `native-*` profiles, preserving existing profiles and budgets.
- Updated `schemas/execution-strategy.schema.json` and `schemas/result.schema.json`.

Older/customized repos may need additional changes. If a project-only run-manifest
schema exists, review its worker_runtime contract against the packaged schema.
Do not overwrite project policy simply because it differs from a template.

### Enable native execution only when desired

After the reviewed merge, ask to set these in
`.ai/orchestration/orchestration.yaml` for new missions:

```text
execution_strategy: claude-native
control_plane.review_profile: native-review
```

The selector is JSON-compatible YAML; the dotted key above describes the nested
field, not a literal new top-level property. To keep CLI mode, retain the existing
strategy and review profile.

The agent must validate with its plugin-root expansion:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/ai-orchestrate" doctor \
  --config .ai/orchestration/orchestration.yaml
```

`CLAUDE_PLUGIN_ROOT` is resolved in plugin execution, not normally in your shell.
Do not use a possibly stale `ai-orchestrate` from PATH. Native doctor status
`requires_live_session` is intentional; inspect actual Agent availability.

## 4. Start a new mission

Use `/mission-control:mission-control` for a new change. It prepares the same
spec/plan/tasks and visible task graph/delegation. Native mode uses
`session start/claim/finish/review`, actual Agent stop/results, independently
collected checks, fresh review, separate parent approval, and retrospective.
CLI `run/resume/recover` must not execute a native run.

Setup does not rewrite `specs/`, `.ai-runtime/`, `CLAUDE.md`, or Claude settings.
Do not edit an existing run snapshot to change its runtime.

## Rollback

First stop and reconcile workers; never discard an unresolved native claim.
A plugin update does not promise an in-place downgrade command. Keep a known-good
old plugin checkout, disable the updated plugin in this repo, and load the old
plugin for a fresh session:

```bash
claude plugin disable mission-control@ai-tools --scope local
claude --plugin-dir /absolute/path/to/known-good/ai-tools/plugins/mission-control
```

Leave `mission-control@ai-tools-native` locally disabled too if it was installed.
The old path must contain the actual earlier plugin version, not the updated
checkout. Restore the saved configuration only after inspecting later project
changes; preserve the current directory in another backup before replacing it.
Validate with the old bundled executable. The old CLI must not resume native runs.

## What changes where

| Action | Changes |
|---|---|
| Plugin update | Installed plugin cache; enablement remains under the same ai-tools ID |
| Local install/enable/disable | Repo-local plugin settings and installed cache |
| Setup merge | Only reviewed .ai/orchestration files |
| New mission | New change artifacts and runtime records |

Local scope uses `.claude/settings.local.json`; project scope uses the shared
`.claude/settings.json`. Review settings changes before committing. Updating this
plugin never automatically changes an existing project's delegation strategy.

References: [Claude plugin management](https://code.claude.com/docs/en/discover-plugins)
and [plugin commands](https://code.claude.com/docs/en/plugins-reference).
