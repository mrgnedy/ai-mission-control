# AI Tools for Claude Code

Marketplace for the Noqodi mobile team's Claude Code plugins. Publish this directory as the root
of the `ai-tools` GitHub repository.

| Plugin | What it does |
|---|---|
| `learning-loop` | Captures durable project lessons as confirmed rules and advisory checks. |
| `commit-changes` | Reviews, cleans up, and commits working-tree changes as focused, conventionally typed commits. |

Install with:

```sh
claude plugin marketplace add mrgnedy/ai-tools
claude plugin install learning-loop@ai-tools
claude plugin install commit-changes@ai-tools
```

For local development, register `~/.claude/local-marketplaces/ai-tools` instead of the GitHub
repository.
