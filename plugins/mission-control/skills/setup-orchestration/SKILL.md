---
name: setup-orchestration
description: Initialize or reconcile project-owned Mission Control configuration, choose an execution strategy, and review or customize its profile models. Use after installation, when setting up a workspace, or when changing strategy/model assignments.
---

# Set up orchestration

Resolve the intended workspace root before writing. Prefer the current Git repository root; use a
different path only when the user names it explicitly.
Inspect pending runs before edits. Never change a running mission's configuration or frozen
snapshots; finish/reconcile its workers first or leave the proposed setup unapplied.

Inspect first:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/init_orchestration.py" \
  --workspace <workspace-root> --status --json
```

The report compares every packaged and project file by relative path and content. Treat these as
repository-owned files after creation: the selector, methodology, strategies, profiles, provider
manifests, routing rules, prompts, and schemas under `.ai/orchestration/`. Setup does not own
`CLAUDE.md`, `.claude/settings.json`, `specs/`, or `.ai-runtime/`.
The current template's disciplined methodology opts into pre-execution artifact, known-decision,
and required-review gates. A plugin update does not opt an existing project in; treat that
methodology difference as a project-policy choice during a semantic merge.
Execution strategy and methodology are different: choosing worker dispatch must not silently
change the project's methodology gates. Stage reconciliation, strategy selection, and model
edits as one proposal before applying them.

## Missing or current configuration

For `status: missing`, use the packaged template as the proposed starting point. Complete the
strategy/profile choices below, then run the initializer and apply only the approved overrides:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/init_orchestration.py" --workspace <workspace-root>
```

For `status: current`, skip the merge/replace question, but still offer strategy and model review.
Keeping the selected strategy and all models is a no-op; do not rewrite identical files.
Any template-only, project-only, or changed path makes the status divergent.

## Divergent configuration

Before writing, summarize the `template_only`, `project_only`, and `changed` paths and ask the user
to choose one of these outcomes:

- **Merge (recommended):** review each changed file, preserve intentional project policy, add
  missing current contracts, and retain project-only files unless the user explicitly removes one.
- **Keep unchanged:** make no configuration edits.
- **Replace:** discard the live project configuration in favor of the packaged template after a
  complete recoverable backup.

Do not infer a choice from a plugin update or a request to inspect. A merge is semantic work, not a
blind copy: distinguish project decisions from obsolete contracts, patch only what the selected
plugin version requires, and show the exact proposed/actual file list. If a conflict changes
provider authority, cost policy, methodology gates, or task behavior and the user's preference is
not known, stop that part of the merge and ask.
Keep unchanged ends the write workflow; do not interpret later inspection as permission to edit.

## Choose strategy, then review profile models

1. **Discover the actual choices.** Read the project selector and strategy manifests under
   `.ai/orchestration/strategies/`, plus packaged choices under
   `${CLAUDE_PLUGIN_ROOT}/templates/.ai/orchestration/strategies/`. Project definitions win for
   duplicate IDs; show packaged-only strategies as requiring an approved setup merge.
   Do not replace a customized strategy with its stock namesake or invent options from memory.
   Print a compact strategy table: ID, worker runtime (`cli` when omitted), allowed worker
   providers, isolation/parallelism, current selection, and whether required assets are present.
   Infer the explanation from each manifest and its routing matrix. The stock choices currently
   cover hybrid CLI, Claude-only CLI, and native Claude Agents; custom valid strategies belong
   in the same list. Native writers are serial in the shared workspace; only readers may overlap.

2. **Ask which strategy to use.** Offer retaining the current strategy as the default for an
   existing repo, or explicitly choosing another available ID. Missing configuration needs an
   explicit choice rather than silently accepting the template's default. If the user already
   selected a strategy in this invocation, acknowledge it instead of asking again.
   A strategy choice is not permission to replace project policy or change methodologies.
   Native workers use plugin `agents/` and parent permissions, not provider CLI Bash allow rules.
   Keep Claude as the control plane unless the user separately requests a supported change.

3. **Print that strategy's profile inventory before asking for edits.** Resolve its routing
   matrix, project profiles/provider manifests, and control-plane profile references from the
   proposed merged configuration. Show every compatible worker profile, not just profiles
   selected by static rules: `requested_profile` may pin other eligible profiles. Compatibility
   requires an allowed provider and the runtime's supported isolation, model, and effort.
   For native workers this means Claude, workspace/read_only, haiku/sonnet/opus, and no explicit
   effort. Do not silently coerce an incompatible customized profile.
   Use a table with profile ID, provider, current model, effort, isolation, and usage
   (routing rule/task role, fallback, control-plane role, or available but currently unused).
   Include direct rule references and fallback profiles. Show referenced incompatible/missing
   profiles separately as conflicts, not as usable choices.
   Also show planning/review/final-approval profile references. Control-plane-only profiles
   need not satisfy native worker constraints; label them separately. Their configuration does
   not switch the already-running parent Claude session's model/effort. Native worker effort
   inherits the parent. Local command-only profiles have no model to edit.

4. **Ask for model changes by profile ID, or keep all unchanged.** For example:
   `native-standard -> haiku` or `cursor-implementation -> <provider-model-id>`.
   Accept changes to any displayed model-bearing profile, including fallback and control-plane
   profiles, within that provider/runtime's supported choices. Do not limit editing to the
   example or preferred models. Keep profile IDs/routing references stable; explain that changing
   a shared profile changes every use of it, including uses in other strategies.
   If the user wants strategy-specific values, propose a separately named profile and updated
   references for approval rather than silently cloning profiles.
   Flag model/effort incompatibility and cost-class implications; do not silently change expense
   labels, budgets, provider permissions, or isolation. Validate supplied model IDs using
   available provider metadata when possible. Unknown model availability remains unverified;
   a schema pass is not a successful provider probe. Do not make paid probes by default.
   Users may edit the proposed files themselves; reread their edits before validation/applying.

5. **Preview, confirm, and apply the minimal diff.** Show the selected strategy, affected
   control-plane references, each old -> new model assignment, and exact file paths. An explicit
   choice/edit request authorizes those changes, not unrelated template conflicts; ask only for
   unresolved choices. Native mode needs a compatible read-only review profile with inherited
   effort: retain an existing compatible choice, otherwise propose `native-review` and show
   the `control_plane.review_profile` change. Do not carry that native-only restriction into
   CLI mode. Initialize/merge/replace only as approved, then patch the project selector/profiles;
   never edit packaged defaults, provider accounts, visible mission plans, or runtime snapshots
   as part of setup. If the user keeps strategy/models unchanged, preserve their bytes.

## Skill bindings

Preserve project `gate_skills`, `file_skills`, and `reviewer_skills` when merging configuration;
strategy/model changes do not change these settings or mission task assignments. Use the actual
destination methodology's bindings, not an assumption that it is empty. For binding-only changes,
use `mission-control:bind-skills` instead of repeating this setup wizard.
After first-time setup, offer once to configure skills per step; if the user already requested
that, hand off directly. Do not repeat the offer on reconciliation/merge runs.

## Apply and validate

For an explicitly chosen replacement, run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/init_orchestration.py" \
  --workspace <workspace-root> --replace --yes
```

Replacement validates and stages the packaged template, then moves the former directory to
`.ai/mission-control-backups/<timestamp>/orchestration/` before swapping in the template. Report
the backup path. Never pass `--yes` before the user chooses replacement.

After initialization, merge, or replacement, run Mission Control's configuration check from the
workspace. Use the executable from this plugin, not a possibly older `ai-orchestrate` on `PATH`:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/ai-orchestrate" doctor --config .ai/orchestration/orchestration.yaml
```

Also validate any affected schemas or routing behavior appropriate to the change. Report the
created, merged, retained, removed, and backed-up files distinctly. Plugin installation/update and
repository configuration reconciliation are separate operations; completing one never implies the
other.
Do the same validation for strategy/model-only edits. Check all referenced primary, fallback,
and control-plane profiles against the selected provider/runtime; verify route model/effort
compatibility, not just selector syntax. Do not claim setup success with unresolved conflicts.
Finish by printing the saved strategy and effective profile/model table, with any unverified
provider availability clearly marked. Setup never starts implementation.
