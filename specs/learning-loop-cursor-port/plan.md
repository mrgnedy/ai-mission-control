# Technical implementation plan

Planning baseline: `e90cedb`. Requirements and acceptance cases are in [spec.md](spec.md); executable slices and evidence are in [tasks.md](tasks.md). This design is implemented on local branch `codex/learning-loop-cursor-port` but has not been published.

## Baseline and blast radius

Current Claude package: `ai-tools/plugins/learning-loop/`. Claude manifests live at repository root and plugin root under `.claude-plugin/`. `skills/setup/SKILL.md` invokes `scripts/setup.sh plan|apply`; that script targets `.claude/{rules.md,checks.tsv}` and `CLAUDE.md`, fingerprints those files, and protects symlinks. `skills/lesson/SKILL.md` governs user-confirmed lessons. `hooks/hooks.json` provides `UserPromptSubmit` and `PostToolUse` hooks; `hooks/rule_check.sh` reads added lines against `HEAD` and honors `LESSON_RULEBOOK_DIR`. Two shell tests cover setup and the checker.

| Surface | Classification | Risk |
|---|---|---|
| Claude manifests, hook config, legacy setup invocation | REGRESSION_SURFACE | Discovery, setup output, or hooks could change for current users. |
| Shared rulebook/checks templates and checker | REGRESSION_SURFACE | A new adapter could alter matching or output, or create two rulebooks. |
| New Cursor manifests, setup entry point, hook adapter | DIRECT | Incorrect discovery, duplicated components, or missing advisory context. |
| `CLAUDE.md`, `.cursor/rules`, `.claude` in consuming repos | INDIRECT | Setup may overwrite existing instructions or double-load rules. |
| Other `ai-tools` plugins and marketplace entries | INVESTIGATED_NOT_AFFECTED | Cursor manifest must list only Learning Loop; do not repackage other plugins. |
| `design_system` Flutter source | INVESTIGATED_NOT_AFFECTED | No Flutter code is in scope; use a disposable fixture resembling its rulebook layout. |

## Architecture: additive adapters, shared behavior

Proposed repository files (paths relative to `ai-mission-control`):

```text
.cursor-plugin/marketplace.json                  NEW — Cursor catalog, only learning-loop
ai-tools/plugins/learning-loop/
  .claude-plugin/plugin.json                    UNCHANGED
  .cursor-plugin/plugin.json                    NEW — explicit skills and hook paths
  skills/lesson/SKILL.md                        SMALL EDIT — platform-neutral cross-references
  skills/setup/SKILL.md                         UNCHANGED Claude entry point
  cursor/skills/setup/SKILL.md                  NEW Cursor entry point
  hooks/hooks.json                              UNCHANGED Claude config
  hooks/cursor.json                             NEW Cursor-native hook config
  hooks/cursor_adapter.py                       NEW thin JSON adapter; live CLI probe passed
  hooks/rule_check.sh                           SMALL EDIT — resolve optional shared pointer; matching core unchanged
  scripts/setup.sh                              SMALL EDIT — dispatch custom paths; legacy default unchanged
  scripts/setup_cursor.py                       NEW — Cursor preview/apply and shared custom-path setup
  tests/setup_test.sh                           EXTEND legacy contract and both-order fixtures
  tests/rule_check_test.sh                      KEEP; add custom-path cases if needed
  tests/cursor_adapter_test.py                  NEW isolated payload/output tests
  README.md; skills/lesson/assets/wiring.md    UPDATE both install/setup flows and real repo URL
```

The Cursor manifest must select the shared `skills/lesson` and Cursor-specific `cursor/skills/setup`, and explicitly select `hooks/cursor.json`; otherwise Cursor may auto-discover Claude's `hooks/hooks.json`. Verify this behavior in a local Cursor install. The root Cursor marketplace source points at `ai-tools/plugins/learning-loop`. Do not place a second copy of the plugin in the repository.

## Setup contract and state transitions

1. The Claude skill continues to call `setup.sh plan <repo>` and `setup.sh apply <repo> <PLAN_ID>`. Keep its current output and effects for ordinary repositories; capture a baseline fixture before touching the script.
2. The Cursor skill calls `setup_cursor.py plan <repo>` and `setup_cursor.py apply <repo> <PLAN_ID>`. Claude keeps `setup.sh plan|apply`; only custom-path Claude setup dispatches to the Python helper's `plan-claude|apply-claude`. Do not infer client from environment variables.
3. Both actions resolve the Git root, inspect applicable project instructions for an existing canonical rulebook, and resolve one rule/checks directory. Default to `.claude/` only when no alternate canonical rulebook is evident. Allow an explicit repository-relative `LESSON_RULEBOOK_DIR` for existing layouts; reject absolute paths, `..`, symlinks, and destinations outside the Git root. If evidence conflicts, preview reports a stop condition rather than creating a parallel rulebook.
4. For a nondefault location, preview an optional root `.learning-loop-rulebook-dir` file containing exactly the selected repository-relative directory. Apply it only after approval. Both setup modes and the checker resolve the path in precedence order: explicit environment override for that invocation, validated pointer, legacy `.claude/` default. The pointer is not shell-sourced. A malformed pointer causes an advisory miswiring result/no write, never a silent fallback to a different rulebook. Include its content in the plan fingerprint. Without a pointer, preserve existing Claude behavior byte-for-byte.
5. Cursor `plan` prints the exact shared files it would create or preserve and the exact `.cursor/rules/learning-loop.mdc` pointer. The local CLI probe confirmed this pointer causes the agent to read the canonical rulebook. Cursor setup does not modify `AGENTS.md` or `CLAUDE.md`.
6. Cursor `apply` accepts only the matching plan ID. Fingerprint every input that affects the plan: selected rulebook/checks, pointer, Cursor rule, relevant existing instruction files, and the selected relative path. Recheck containment and symlinks just before writing. Preflight all targets before the first write. Write only missing files; preserve existing content and file mode. If a later write fails, report exactly which managed files were created and how to retry; do not remove or roll back files that may already contain user edits.
7. If Claude runs second, only `CLAUDE.md` wiring is added; if Cursor runs second, only Cursor wiring is added. If either client already receives the rulebook through existing project instructions, preview must describe why it will not add redundant wiring. This behavior needs the dual-install live test before release.

## Cursor lesson and hooks

- Reuse the lesson workflow's rule wording, scope, confirmation and no-write-before-approval gates. Change only Claude-specific references that would misdirect a Cursor agent. Cursor's setup entry point is explicit; do not teach Cursor to invoke Claude's setup.
- Keep Claude's prompt hook unchanged. Cursor receives the correction-capture reminder through the short project rule and the skill description; no `beforeSubmitPrompt` hook is added because its documented output cannot inject context.
- Probe Cursor's actual edit events. If `postToolUse` fires for agent writes/edits and `additional_context` reaches the model, `hooks/cursor.json` filters those events and invokes `cursor_adapter.py`. The adapter parses JSON structurally, extracts one changed path, invokes the existing checker with a normalized payload, converts a checker hit to Cursor's `additional_context`, and emits nothing for a clean edit. Do not store transcripts or changed file contents. Test paths with spaces, untracked files, malformed input, and missing rulebooks.
- Do not use `afterFileEdit` as the reporting hook unless Cursor adds a supported output contract: current docs list no output fields for it. Do not block the write for an advisory check.
- Test Cursor with third-party imports both on and off. If Claude's installed plugin is also imported, identify actual duplicate skill/hook behavior. Use the smallest per-plugin guard or manifest selection proved by the live test. Do not disable all third-party imports as a workaround, and do not release a duplicated checker.

## Risk register and release gates

| Risk | Mitigation | Evidence required |
|---|---|---|
| RSK-01 Claude setup regression | Preserve legacy syntax/branch; snapshot current preview/apply; run existing shell tests and real Claude smoke test. | Identical ordinary Claude outputs/effects; strict Claude validation. |
| RSK-02 Two rulebooks or unexpected project edits | Explicit canonical path, persistent validated pointer for custom layouts, conservative conflict stop, no-overwrite preview/apply, both-order tests. | Fresh, existing `.claude`, and existing `.Codex` fixture diffs plus hook lookup after a new session. |
| RSK-03 Cursor hook silently ignored | Probe documented `postToolUse` feedback before building adapter; live violating/compliant edits. | Agent visibly receives one violation; no output for compliant edit. |
| RSK-04 Double skill/hook loading | Live both-installed matrix with third-party imports on/off; resolve per-plugin, not globally. | One visible setup/lesson choice and one checker report per edit. |
| RSK-05 Unsafe stale or symlink apply | Fingerprint complete read set, preflight/recheck targets, fail closed for setup, report any partial creation precisely. | Stale-plan, symlink, path traversal, unwritable-target, and injected write-failure tests. |
| RSK-06 Excess token overhead | Short pointer rule; no per-prompt Cursor keyword hook; report only on hits. | Quiet healthy runs and no duplicate rulebook content. |

**Do not publish until all release gates pass.** If Cursor cannot deliver the advisory context reliably, stop and report the limitation; a manual-only Cursor skill can be a separately labeled partial release only with explicit user approval. If a dual-install collision cannot be resolved without broad settings changes, stop rather than weakening Claude or unrelated plugins.

The second critical review found a known uncertainty stronger than a documentation caveat: Cursor community reports state that `postToolUse.additional_context` may be accepted by the hook runner but dropped before the model. The 2026-10-06 local CLI probe resolved this for the installed CLI version: the agent repeated a hook-only marker, then a packaged-plugin violation; the event stream contained one advisory context on the edit. This does not establish Cursor IDE or future-version behavior.

## Verification and rollback

- Automated: `sh tests/setup_test.sh`, `sh tests/rule_check_test.sh`, new Cursor adapter tests, strict Claude plugin/marketplace validation, JSON parsing and local discovery for Cursor manifests, and `git diff --check`.
- Disposable fixture matrix: Claude-only, Cursor-only, each setup order, reruns, custom rulebook, ambiguous existing rules, stale plan, symlinks, no Git root, added-line violation, compliant edit, and missing checks.
- Live matrix: Claude install/setup/edit; Cursor local plugin install/setup/edit in IDE and CLI if both are claimed supported; both installed with Cursor third-party imports on/off; verify hook output channel and model-visible result. Cloud-agent support must be tested separately before claiming it.
- Review: inspect full diff for secrets, changes to existing Claude contracts, unplanned files, duplicate prompts, and installation instructions; compare pre/post Claude outputs.
- Rollout: publish a new plugin version only after evidence is recorded in [tasks.md](tasks.md). Existing project-owned rules/checks must never be rewritten during installation or rollback. The code rollback is a Git revert of the new plugin changes; client-specific setup wiring is removed manually only after inspecting whether the user has edited it.

Cursor documentation used for the interface assumptions: [plugin manifests](https://prod.cursor.com/docs/reference/plugins), [skills](https://cursor.com/docs/skills), [rules](https://cursor.com/docs/rules), [hooks](https://prod.cursor.com/docs/hooks), and [third-party imports](https://prod.cursor.com/docs/reference/third-party-hooks). These are inputs to the live probes, not substitutes for them.
