# AI Tools for Claude Code

Marketplace for the Noqodi mobile team's Claude Code plugins. The plugins live in
`ai-tools/plugins/` inside the `ai-mission-control` repository. The repository root has the
marketplace catalog for remote installation; this directory also has one for local development.

| Plugin | What it does |
|---|---|
| `learning-loop` | Captures durable project lessons as confirmed rules and advisory checks. |
| `commit-changes` | Reviews, cleans up, and commits working-tree changes as focused, conventionally typed commits. |
| `mission-control` | Coordinates planning, delegation, implementation, review, and recovery. |

Install with:

```sh
claude plugin marketplace add mrgnedy/ai-mission-control
claude plugin install learning-loop@ai-tools
claude plugin install commit-changes@ai-tools
claude plugin install mission-control@ai-tools
```

For local development, register this `ai-tools` directory instead of the GitHub repository.

## Native-Agent feature branch

On `feat/claude-native-agents`, both marketplace catalogs are named
`ai-tools-native` to avoid replacing the normal `ai-tools` source. The general
install commands above apply to `main`, not this feature branch.
For upgrading a repo with an older installation, follow the
[native branch upgrade guide](plugins/mission-control/docs/UPGRADE_NATIVE.md).
See the repository [changelog](../CHANGELOG.md) for capabilities, compatibility,
and validation.
