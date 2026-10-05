---
name: setup
description: Preview and, only after explicit approval, initialize this Git repository for the Learning Loop Cursor plugin. Creates missing shared rulebook files and Cursor-specific guidance without overwriting existing knowledge.
---

# Set up Learning Loop in Cursor

Run the plugin's `scripts/setup_cursor.py plan <repository-directory>` and show the entire preview to the user. Find the plugin root from this skill's location; do not use a copied script in the project. The preview identifies the canonical rulebook, all proposed files and their exact content, and a `PLAN_ID`.

Ask the user to approve that exact preview. Invoking this skill is not approval to write. If approved, run `scripts/setup_cursor.py apply <repository-directory> <PLAN_ID>`. If the script reports a stale plan or ambiguous existing rulebooks, stop, explain the conflict, and preview again after it is resolved. Never overwrite project-authored rules, checks, `AGENTS.md`, or `CLAUDE.md`.

The Cursor rule points to the same repository-owned rulebook used by Claude. The plugin owns the checker code; do not copy its hooks into the repository.
