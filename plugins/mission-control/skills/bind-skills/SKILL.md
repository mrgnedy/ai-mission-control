---
name: bind-skills
description: Inspect or configure project-owned Mission Control skills for parent gates, task workers, and independent review. Use when customizing which procedure runs at a workflow step; does not execute the configured mission or change models, routing, or permissions.
---

# Bind workflow skills

Read `.ai/orchestration/orchestration.yaml` and its selected methodology. If either is missing,
direct the user to `mission-control:setup-orchestration`; do not create another initializer.
Use only `"${CLAUDE_PLUGIN_ROOT}/bin/ai-orchestrate"`, never a potentially stale PATH copy.
Inspect unfinished runs first. Leave configuration changes unapplied while workers own a run;
show a proposed diff for the next run. Never modify frozen runtime inputs.

## Inspect and configure

Show a compact table of step, executor, bound skills, condition, actual availability and expanded
file-payload size. Read only the skills involved in the requested change. Names in another agent's
catalog or an arbitrary cached plugin version are not proof of availability in this session.

Apply explicit unambiguous edit requests without another approval round. Inspection/proposal requests
remain read-only. Ask only about unresolved material choices. Change only the selected methodology
and any explicitly needed compatible project skill/adapter; preserve all unrelated settings.
Identical settings are a no-op. If the project schema predates bindings, use the existing setup merge
procedure rather than replacing configuration or editing packaged schemas/defaults.

The existing methodology `skills` mapping remains unchanged. Configure these optional fields:

```yaml
gate_skills:
  investigation:
    - skill: figma-intake
      when: "the supplied sources include a Figma link"
  closure:
    - skill: commit-changes:commit-changes
      when: "the user asked to commit"
file_skills:
  ui: .claude/skills/ui-implementation
reviewer_skills: []
```

- `gate_skills`: ordered parent procedures at selected intake, investigation, planning, closure or
  learning_capture gates. Use exact names the parent can invoke. `when` is evidence-based judgment,
  not code. Applicable skills are required; record non-applicability rather than silently skipping.
- `file_skills`: aliases for repository-relative directories containing `SKILL.md`. This plugin's
  skills may use `mission-control:<name>`. Workers cannot invoke the parent's Skill tool. The runtime
  freezes and inlines the procedure and its reachable in-root Markdown references, including an
  outside-skill-directory repository rulebook. Symlink/path escapes are rejected.
- Tasks explicitly select aliases with optional `skills: [ui]`; do not infer assignments at dispatch.
  Reviewer aliases apply to the existing independent reviewer, not a second agent. Repair tasks
  inherit the affected task's assignments. No binding creates an agent or selects a model.

## Assess compatibility before enabling

Check artifact/result format, references, tools, writes, delegation, interaction and stopping point.
Intake/investigation/planning procedures may mutate only authorized local planning artifacts. Review
must remain independent/read-only and return the existing review JSON. Worker scope, checks,
continuation, decisions and discipline still apply. A binding never grants unavailable tools or MCP.

Do not bind Mission Control recursively or another full orchestration chain. If a useful subprocedure
can be isolated, propose an explicit adapter with input, output, allowed actions and stopping point.
Do not invoke a full foreign workflow and hope a final "stop" cancels its chaining instructions.
Verify an actual adapter's behavior in isolation; delivering text is not proof of compliance.

Plugin/user skills intended for workers need a compatible repository copy or self-contained adapter.
Preserve required resources/provenance; a wrapper invoking an unavailable Skill tool is insufficient.
V1 captures local Markdown instructions, not arbitrary script/asset dependencies. `doctor` reports
detected structural issues; the parent decides whether the procedure actually depends on them.

Closeout can use `commit-changes` when requested, respecting its actual user interaction and narrow
documented comment-cleanup rule. Other proposed edits still need existing user authority. Permit
only demonstrably non-behavioral cleanup in accepted scope; removing a logging call is not harmless
if it changes side effects or required observability. Rerun affected tasks' declared checks on the
final tree and record post-acceptance edits. Behavioral fixes require tracked work/independent review.
The skill's own instructions cannot grant authority the user has not supplied.

## Validate and hand off

Run `"${CLAUDE_PLUGIN_ROOT}/bin/ai-orchestrate" doctor --config .ai/orchestration/orchestration.yaml`.
If a mission graph exists, also run the bundled `validate --change specs/<change>`.
Configuration inspection does not require creating a graph. Report exact changed files and that
bindings affect the next run; do not change profiles/providers, install plugins, commit or publish.

Expanded extra guidance warns above 32 KiB and refuses above 128 KiB per task/review. Preflight groups
identical warnings with affected uses and largest contributors. No truncation or automatic model
upgrade. An optional smaller project procedure must preserve canonical rules: a full-read rulebook
cannot silently become a selected subset. Valid full bindings remain usable within the hard limit.
Do not require an adapter or additional confirmation merely because the payload warns.
File guidance uses `run --change` (or native `session start`) to bind resources to an immutable run;
legacy positional runs remain supported without configurable file guidance.

Selecting another methodology uses its actual bindings; do not assume it has none. Offer a copy only
when a requested binding is absent and remains compatible with that methodology.
