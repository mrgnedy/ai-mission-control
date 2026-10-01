# Learning Loop structure and manual wiring

## Where each part lives

```text
learning-loop plugin                    consuming repository
├── skills/setup/SKILL.md               ├── CLAUDE.md
│   consented setup workflow            │   imports the rulebook
├── skills/lesson/SKILL.md              └── .claude/
│   lesson capture workflow                 ├── rules.md
├── skills/lesson/assets/                   ├── checks.tsv
│   repository starter templates            └── rulecheck.config  optional
├── scripts/setup.sh
│   deterministic preview/apply helper
└── hooks/
    ├── hooks.json
    └── rule_check.sh
```

The plugin owns behavior and executable code. Each repository owns its accumulated knowledge, so
rules and checks travel with the repository and remain reviewable in its history.

## Normal installation

Register the marketplace once from GitHub:

```sh
claude plugin marketplace add mrgnedy/ai-tools
```

A Git URL or an absolute local checkout path also works when using another source.

Install the plugin:

```sh
claude plugin install learning-loop@ai-tools
```

Start Claude Code in the git repository that should own the rulebook:

```sh
cd <repository>
claude
```

If Claude Code asks for a plugin reload, run `/reload-plugins`. Then invoke:

```text
/learning-loop:setup
```

The setup skill previews the exact changes, asks for approval, and applies only the approved plan.
Marketplace registration and plugin installation are user-level operations; repository setup is
performed separately in every git repository that should use the learning loop.

For local development of this marketplace, the first command looks like:

```sh
claude plugin marketplace add /absolute/path/to/ai-tools
```

## Manual repository installation

If automatic setup is unsuitable—for example, `CLAUDE.md` is a symbolic link—wire it manually:

1. Copy `rules.template.md` to `<repository>/.claude/rules.md` unless that file already exists.
2. Copy `checks.template.tsv` to `<repository>/.claude/checks.tsv` unless that file already exists.
3. If cross-module checks are needed, copy `rulecheck.config.example` to
   `<repository>/.claude/rulecheck.config` and adapt its values.
4. Prepend this block to the repository's `CLAUDE.md` unless the import already exists:

   ```md
   ## Shared rulebook

   @.claude/rules.md

   When you discover a durable, non-obvious project insight that would help future agents avoid
   mistakes or repeated investigation, consider invoking the `learning-loop:lesson` skill and
   propose capturing it.
   ```

The prompt hook covers correction-like user messages. The short `CLAUDE.md` policy covers insights
Claude discovers without a user correction. Do not duplicate the lesson workflow or hook commands
in `CLAUDE.md`, and do not copy the plugin's checker into the repository.

To validate a tier-2 check, make one uncommitted violating edit and one compliant edit through
Claude. The violating edit must produce `RULEBOOK:` context; the compliant edit must remain quiet.
