---
name: setup-orchestration
description: Initialize or reconcile a repository's project-owned Mission Control .ai/orchestration configuration. Use after installation, when setting up a workspace, or when reviewing template changes after a plugin update.
---

# Set up orchestration

Resolve the intended workspace root before writing. Prefer the current Git repository root; use a
different path only when the user names it explicitly.

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

## Missing or current configuration

For `status: missing`, run the initializer without a mode. It validates and copies the complete
template:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/init_orchestration.py" --workspace <workspace-root>
```

For `status: current`, do not prompt or rewrite anything. Any template-only, project-only, or
changed path makes the status divergent so removal and retention decisions stay visible.

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
