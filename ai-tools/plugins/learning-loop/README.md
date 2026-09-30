# Learning Loop

`learning-loop` turns reusable corrections and hard-won implementation insights into durable,
repository-owned rules. It contributes:

- the user-invocable `lesson` skill;
- the user-invocable `setup` skill, with preview-before-apply consent;
- a prompt hook that reminds Claude to evaluate correction-like feedback;
- a post-write hook that checks only lines added against `HEAD`;
- starter assets for a repository rulebook, tier-2 checks, and optional cross-module settings.

The plugin owns the mechanism. The consuming repository owns the knowledge:

```text
<repository>/.claude/rules.md
<repository>/.claude/checks.tsv
<repository>/.claude/rulecheck.config  # optional
```

The hooks do nothing in repositories that have not initialized a rulebook. See
`skills/lesson/assets/wiring.md` for the component map and manual repository setup, or run:

```text
/learning-loop:setup
```

The setup command displays the exact files and `CLAUDE.md` block it would add. It does not write
until the user approves that plan. Re-running it is safe: existing rulebook files are preserved and
the `CLAUDE.md` import is never duplicated.
