# Byte-sized delivery tasks

Implementation is on local branch `codex/learning-loop-cursor-port`. The authenticated 2026-10-06 Cursor CLI probes passed; IDE and marketplace release checks remain. See [spec.md](spec.md) for REQs and [plan.md](plan.md) for risks. A task remains open when any part of its stated DoD is unverified.

| ID | Status | Depends on | Deliverable and tailored definition of done |
|---|---|---|---|
| TASK-01 | IN_PROGRESS | — | Capture baseline commit, Claude setup preview/apply output, and both current shell-test results in disposable repos. Preserve outputs as fixtures; no source edits. REQ-05/08, RSK-01. |
| TASK-02 | DONE | TASK-01 | Probe a local Cursor plugin with separate explicit `skills` and `hooks` paths while a Claude manifest exists alongside it. Record what Cursor actually discovers. `cursor-agent status` reported `Not logged in` on 2026-10-05. REQ-01/02, VAL-01. |
| TASK-03 | DONE | TASK-02 | Probe `postToolUse` for an agent edit: actual tool name, payload, and whether `additional_context` is seen by the agent. Also verify a no-output clean event. Stop hook work if unavailable. REQ-06, VAL-02. |
| TASK-04 | IN_PROGRESS | TASK-02 | Probe a short `.cursor/rules/*.mdc` reference to a project-owned rulebook in Cursor IDE/CLI; record whether full rules load once, including with Claude imports enabled. REQ-04/07, VAL-03. |
| TASK-05 | IN_PROGRESS | TASK-02 | Probe dual install with Cursor third-party imports on and off; list visible skills and fired hooks. Decide a per-plugin dedupe mechanism only from observed collisions. REQ-07, VAL-04. |
| TASK-06 | DONE | TASK-02 | Add root `.cursor-plugin/marketplace.json` containing only the `learning-loop` entry at `ai-tools/plugins/learning-loop`; validate path/name. REQ-02. |
| TASK-07 | DONE | TASK-02 | Add plugin `.cursor-plugin/plugin.json` with explicit shared lesson skill, Cursor setup skill, and Cursor hook paths; verify Claude manifest and root catalog are unchanged. REQ-01/02/05. |
| TASK-08 | DONE | TASK-04 | Add the minimal Cursor setup skill at `cursor/skills/setup/SKILL.md`; it names explicit Cursor plan/apply actions and repeats preview/approval rules. Confirm Cursor invokes this rather than Claude setup. REQ-03/06. |
| TASK-09 | IN_PROGRESS | TASK-01 | Add tests for shared-rulebook resolution, custom path and persistent pointer across new hook sessions, conflicting existing rulebooks, malformed pointer, symlink/path escape, unwritable target, injected write failure, and complete plan-ID fingerprint. Tests fail on the unimplemented cases. REQ-03/04, RSK-02/05. |
| TASK-10 | DONE | TASK-09 | Add explicit Cursor preview/apply in `scripts/setup_cursor.py`; route only custom-path Claude setup through it from `setup.sh`. Keep ordinary Claude actions and output contracts intact. REQ-03/04/05. |
| TASK-11 | DONE | TASK-10 | Add both-order and rerun tests: Claude→Cursor, Cursor→Claude, each repeated. Assert byte-identical shared rules/checks and only one client wiring change on second setup. REQ-03/04. |
| TASK-12 | IN_PROGRESS | TASK-04, TASK-10 | Add the short managed Cursor rule produced by setup. Test no overwrite of an existing different `.mdc` or `AGENTS.md`; test one canonical rulebook is loaded. REQ-04/06/07, RSK-02/06. |
| TASK-13 | IN_PROGRESS | TASK-08 | Make only the shared lesson skill's Claude-specific cross-references platform-neutral. Test it still routes to user confirmation and never writes an unapproved rule. REQ-05/06. |
| TASK-14 | IN_PROGRESS | TASK-03 | Add unit fixtures for Cursor edit payloads: write, edit, path with spaces, clean edit, violation, untracked file, invalid JSON, outside-repo file, default/custom/malformed pointer, no rulebook. REQ-06. |
| TASK-15 | DONE | TASK-14 | Implement the thin Cursor JSON adapter around the unchanged checker; emit `additional_context` only for violations. No file-content logging or blocking. REQ-01/06, RSK-03/06. |
| TASK-16 | DONE | TASK-15 | Add `hooks/cursor.json` targeting only the observed edit tool events. Verify plugin discovery and that Claude's hook config is unchanged. REQ-05/06. |
| TASK-17 | IN_PROGRESS | TASK-05, TASK-16 | If dual-install probe found duplication, implement and test the smallest per-plugin suppression; otherwise record why none is needed. Never disable all third-party imports. REQ-07, RSK-04. |
| TASK-18 | IN_PROGRESS | TASK-11–13, TASK-16–17 | Run all automated tests, strict Claude validation, Cursor manifest JSON/local-discovery checks, baseline comparison, and full diff review; repair findings before continuing. REQ-05/08. |
| TASK-19 | IN_PROGRESS | TASK-18 | In disposable repositories, run real Claude and Cursor setup/edit tests, both setup orders, third-party imports on/off, and verify exactly one advisory hit and no duplicate context. Record screenshots or logs without secrets. REQ-03–08. |
| TASK-20 | IN_PROGRESS | TASK-19 | Update README and wiring guide with verified install commands, setup behavior, custom-rulebook handling, and support limits. Correct the stale `mrgnedy/ai-tools` URL. REQ-02/04/08. |
| TASK-21 | TODO | TASK-20 | Final requirement/risk audit: every REQ has implementation plus evidence, no blocking VAL remains, Claude baseline passes, consuming repos unchanged. Only then propose version bump/publication. REQ-01–08. |

## Evidence and remaining release checks (2026-10-06)

- `sh ai-tools/plugins/learning-loop/tests/setup_test.sh` — PASS; ordinary Claude setup contract unchanged.
- `sh ai-tools/plugins/learning-loop/tests/rule_check_test.sh` — PASS.
- `sh ai-tools/plugins/learning-loop/tests/cursor_setup_test.sh` — PASS; both installation orders, reruns, custom `.Codex` and spaced paths, stale plan, ambiguity, and symlink rejection.
- `PYTHONDONTWRITEBYTECODE=1 python3 ai-tools/plugins/learning-loop/tests/cursor_adapter_test.py` — 3 tests PASS; violation, quiet edit, missing path, custom/malformed pointer.
- `PYTHONDONTWRITEBYTECODE=1 python3 ai-tools/plugins/learning-loop/tests/cursor_manifest_test.py` — 2 tests PASS; one catalog entry and explicit component paths.
- `claude plugin validate --strict ai-tools/plugins/learning-loop` and `claude plugin validate --strict .` — both PASS.
- Authenticated Cursor CLI with `--plugin-dir`: explicit setup and lesson skills discovered; project `.mdc` led the agent to read the canonical rulebook; setup skill preview returned a real `PLAN_ID` without writes; three synthetic edit runs delivered `PROBE-001`. The `stream-json` completed edit event showed exactly one `hookAdditionalContexts` entry.
- Authenticated Claude CLI with local `--plugin-dir`: synthetic `Write` received `PROBE-001` through the Claude hook.
- `git diff --check` — PASS. All live edits were in `/private/tmp` disposable Git fixtures; `design_system` was not changed.

Release remains gated on Cursor IDE/marketplace installation, explicit third-party-imports-off coexistence, fuller failure-injection tests, and a final diff/requirement audit. No version bump, publication, or consuming-repository installation has occurred.

## Trace and stop conditions

| Requirement | Primary tasks | Verification |
|---|---|---|
| REQ-01 single implementation | 07, 13, 15 | one plugin root and one checker core |
| REQ-02 separate installation | 02, 06, 07, 20 | Cursor and Claude catalog validation/install |
| REQ-03 safe setup | 08–11 | preview/apply, stale, idempotence, symlink tests |
| REQ-04 one knowledge source | 04, 09–12 | both-order/custom-path byte comparison |
| REQ-05 Claude compatibility | 01, 10, 13, 18–19 | baseline and live Claude smoke |
| REQ-06 Cursor behavior | 03–04, 08, 12–16, 19 | model-visible hit; clean edit quiet |
| REQ-07 coexistence | 05, 12, 17, 19 | third-party on/off; one active checker |
| REQ-08 evidence before release | 18–21 | recorded automated/live checks and audit |

Stop rather than implement the behavior-dependent adapter or release if TASK-03 cannot demonstrate model-visible advisory context, TASK-04 cannot load the canonical rulebook, TASK-05/17 cannot avoid duplicate behavior without a broad setting change, or any Claude baseline test regresses. Such a stop is a capability finding, not permission to call the port complete.
