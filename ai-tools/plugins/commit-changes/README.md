# Commit Changes

`commit-changes` turns a messy working tree into a sequence of commits a reviewer can read. It
contributes one skill, `commit-changes`, which:

- derives the issue key from the branch name and recent commits;
- flags secrets, config-file edits, and leftover debug or test code before anything is staged;
- reviews the diff for regressions outside the intended blast radius;
- groups changes by intent, so each commit is one idea and builds on its own;
- types every subject as `<type>: <ISSUE-KEY> <summary>`, with `type` one of `feat` `fix` `docs`
  `style` `refactor` `perf` `test` `build` `ci` (`feat!`/`fix!` for breaking changes);
- verifies the final tree, and each commit when the split could have broken one.

A repository's own `commit-msg` hook, commitlint config, or rules file overrides the default
type list and allowed issue keys.

The skill triggers on requests such as "commit my changes" or "split this into meaningful
commits", or run it directly:

```text
/commit-changes:commit-changes
```
