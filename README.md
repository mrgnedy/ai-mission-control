# AI Tools for Claude Code

Marketplace for the Noqodi mobile team's Claude Code plugins. The repository root is the
marketplace for both GitHub installation and local development; plugins live in `plugins/`.

| Plugin | What it does |
|---|---|
| `learning-loop` | Captures durable project lessons as confirmed rules and advisory checks. |
| `commit-changes` | Reviews, cleans up, and commits working-tree changes as focused, conventionally typed commits. |
| `mission-control` | Coordinates planning, delegation, implementation, review, and recovery. |
| `design-workflows` | Captures Figma evidence and prepares an app-aware brief before implementation. |

Install with:

```sh
claude plugin marketplace add mrgnedy/ai-mission-control
claude plugin install learning-loop@ai-tools
claude plugin install commit-changes@ai-tools
claude plugin install mission-control@ai-tools
claude plugin install design-workflows@ai-tools
```

For local development, register this repository root instead of the GitHub repository.
If a local marketplace was registered from the old `ai-tools/` subdirectory,
inspect its source with `claude plugin marketplace list` and re-register that
local entry from the repository root. GitHub-backed registrations keep the same
`ai-tools` marketplace name and plugin IDs.

`design-workflows` needs a separately connected Figma MCP server for live
capture; it does not install one or modify project code.
It can be invoked on its own before UI planning. Mission Control 0.3.13 does
not automatically bind it to an investigation gate; use the skill explicitly
there until that separate binding change is published.

## Native-Agent execution

Mission Control 0.3.12 includes opt-in native Claude Agent execution on `main`.
The marketplace remains `ai-tools`; existing installations update under the same ID.
CLI execution remains the default until project configuration explicitly selects
`claude-native`. For older installations or migration from `ai-tools-native`,
follow the [upgrade guide](plugins/mission-control/docs/UPGRADE_NATIVE.md).
See the repository [changelog](CHANGELOG.md) for capabilities, compatibility,
and validation.
