# Learning Loop

`learning-loop` turns reusable corrections and hard-won implementation insights into durable,
repository-owned rules. It contributes:

- the user-invocable `lesson` skill;
- client-specific `setup` skills, each with preview-before-apply consent;
- a Claude prompt hook that reminds it to evaluate correction-like feedback;
- Claude and Cursor post-write hooks that share one checker for lines added against `HEAD`;
- starter assets for a repository rulebook, tier-2 checks, and optional cross-module settings.

The plugin owns the mechanism. The consuming repository owns the knowledge:

```text
<repository>/.claude/rules.md
<repository>/.claude/checks.tsv
<repository>/.claude/rulecheck.config  # optional
<repository>/.learning-loop-rulebook-dir  # only for an approved nondefault rulebook
```

The hooks do nothing in repositories that have not initialized a rulebook. See
`skills/lesson/assets/wiring.md` for the component map and manual repository setup.

In Claude Code, run:

```text
/learning-loop:setup
```

In Cursor, install the same package from this repository's Cursor marketplace, then invoke the
Learning Loop `setup` skill. It creates only missing shared files and a short
`.cursor/rules/learning-loop.mdc` pointer; it does not edit `CLAUDE.md` or `AGENTS.md`.

Both setup skills show exact changes and a `PLAN_ID`. Neither writes until the user approves that
preview. Existing rulebook knowledge is preserved. For an existing nondefault rulebook such as
`.Codex`, the approved setup adds a one-line repository pointer so both hooks continue finding
the same checks in later sessions. Ambiguous or unsafe paths stop setup rather than creating a
second rulebook. The Cursor CLI can load a local checkout with `--plugin-dir` for testing; this
branch has not been published to the marketplace.
