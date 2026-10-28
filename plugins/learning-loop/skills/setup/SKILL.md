---
name: setup
description: "Initialize the current git repository for the learning-loop plugin by previewing and, only after explicit user approval, creating repository-owned rulebook files and wiring CLAUDE.md. Invoke manually after installing the plugin or when repairing missing learning-loop repository wiring."
user-invocable: true
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/setup.sh *)
---

# Set up Learning Loop

Initialize the current git repository without silently changing it.

## Preview

Run this command exactly once before asking for approval:

```sh
${CLAUDE_PLUGIN_ROOT}/scripts/setup.sh plan "${CLAUDE_PROJECT_DIR}"
```

Show the complete output to the user. It names every file that would be created or preserved and
prints the exact block that would be prepended to `CLAUDE.md`. Keep the reported `PLAN_ID`; it
binds approval to the repository state that was previewed.

Ask whether to apply that exact plan. A request to run this skill is not itself approval to write.
Accept an unambiguous affirmative response; otherwise stop without changing files.

## Apply after approval

After approval, run:

```sh
${CLAUDE_PLUGIN_ROOT}/scripts/setup.sh apply "${CLAUDE_PROJECT_DIR}" "<PLAN_ID>"
```

Use the identifier from the preview verbatim. If the repository changed after the preview, the
script refuses to apply; run `plan` again and request approval for the new output.

Report what was created and what was already present. The setup script:

- targets the git repository root;
- creates `.claude/rules.md` and `.claude/checks.tsv` only when absent;
- prepends the shared-rulebook block only when `@.claude/rules.md` is absent;
- never overwrites existing rulebook knowledge;
- leaves optional cross-module configuration to the manual wiring guide;
- refuses symbolic-link targets because their write destination may be outside the repository.
