# Learning Loop: Claude + Cursor coexistence

Status: implemented on local branch `codex/learning-loop-cursor-port`; automated tests and local CLI smoke tests pass. Cursor IDE/marketplace distribution and third-party-imports-off remain release checks. Baseline: `ai-mission-control` commit `e90cedb` (Learning Loop 0.2.0). No consuming-project files were changed; only disposable `/private/tmp` fixtures were used.

## Request and scope

Port the existing `learning-loop` Claude plugin to Cursor while preserving Claude behavior. Both installation flows must coexist, and invoking `setup` from either client must initialize only that client's missing wiring around one repository-owned rulebook and checks file. Break implementation into small verifiable tasks and do not publish a regression.

### Requirements

- **REQ-01 — Single implementation:** one `ai-tools/plugins/learning-loop` package and one shared rulebook/checker implementation; Claude and Cursor each have their own manifest and only the thin integration pieces they need.
- **REQ-02 — Separate installation:** the existing Claude marketplace remains valid; a Cursor marketplace can install the same package from `ai-mission-control` without copying the plugin.
- **REQ-03 — Safe setup:** either setup entry point first previews exact paths/content, requires explicit approval and its plan ID, rejects a changed preview, preserves existing files, and is idempotent.
- **REQ-04 — One knowledge source:** whichever client runs first, the second reuses the same rules and checks and adds only missing client-specific wiring. An existing nonstandard rulebook, such as `design_system/.Codex/rules.md`, must not be silently superseded. Both runtime hooks must resolve that same directory in later sessions, not only during setup.
- **REQ-05 — Claude compatibility:** current Claude install, `/learning-loop:setup`, lesson confirmation, prompt hook, and post-write advisory behavior remain valid. Its existing command-line setup syntax stays supported.
- **REQ-06 — Cursor behavior:** Cursor discovers the lesson and setup skills; its setup supplies durable project guidance; post-edit checks report violations to the agent without blocking writes; no rule is written without user approval.
- **REQ-07 — Coexistence:** when both clients are installed and Cursor third-party imports are enabled, a single edit does not produce duplicate checks or duplicate rulebook context. Do not require disabling unrelated third-party imports globally.
- **REQ-08 — Evidence before release:** automated tests plus a real Claude and Cursor smoke test cover each installation order, idempotence, stale plans, existing rulebooks, hooks, and dual install. Do not publish or install into `design_system` as part of this planning task.

## Decisions and assumptions for implementation

- **DEC-01 (technical proposal):** retain `.claude/rules.md` and `.claude/checks.tsv` as the default data location for new repositories, including Cursor-first setup. The name is historical, but moving the data now would create migration risk with no functional gain. An explicitly selected existing rulebook directory takes precedence.
- **DEC-02 (technical proposal):** keep the existing Claude `setup.sh plan|apply` interface. Add explicit Cursor actions rather than guessing the client from `CLAUDE_PROJECT_DIR`, which Cursor can also set.
- **DEC-03 (technical proposal):** Cursor gets a short project rule that points to the canonical rulebook; it does not copy its contents. Its exact inclusion syntax is subject to a live Cursor probe.
- **DEC-04 (technical proposal):** do not port Claude's keyword-based prompt hook. Cursor's documented `beforeSubmitPrompt` output does not inject an advisory message. The short project rule and discoverable lesson skill provide the trigger; test this in a real session.
- **DEC-05 (release gate):** if Cursor's documented `postToolUse.additional_context` does not actually reach the agent after edits, or both integrations cannot coexist without duplicates, stop the release and revise the adapter. Do not silently weaken the advertised behavior.
- **DEC-06 (second-review repair):** when a nondefault rulebook directory is approved, setup creates one optional repository-root pointer containing its validated relative path. Both setup entry points and both hooks read it. With no pointer, legacy `.claude/` behavior remains unchanged. A malformed pointer fails safely rather than reverting silently to another rulebook.

## Exclusions

- No migration of existing repository rules into a new format.
- No automatic edits to `design_system`, `physical-card`, or another consuming repository.
- No marketplace publication, plugin installation, or runtime behavior change in this planning phase.
- No new provider-agnostic plugin framework; this is one shared package with two small adapters.
- Initial runtime claim is local macOS Cursor IDE and CLI, matching the available live test environment. Windows, remote workers, and cloud agents are unverified until separately tested.

## Acceptance scenarios

1. **Claude only, fresh repo:** existing setup preview/apply and hooks behave as at 0.2.0.
2. **Cursor only, fresh repo:** Cursor setup creates the shared data once and only Cursor wiring; a violation reaches the Cursor agent, a compliant edit is quiet.
3. **Claude then Cursor / Cursor then Claude:** rules and checks retain their byte content; each second setup adds only its missing wiring; a third run makes no changes.
4. **Existing custom rulebook:** preview selects an explicit existing location or stops for clarification; it never creates a competing `.claude/rules.md` by default.
5. **Both installed, third-party imports on:** skills are unambiguous and each edit yields at most one advisory result.
6. **Safety:** plan changes after preview are rejected; symlink targets and paths outside the repository are rejected; preflight catches unwritable targets before changes, and any failure after writing begins has a documented recovery path without overwriting user content.

## Open validation facts, not user decisions

- **VAL-01:** confirm how the installed Cursor version resolves a plugin's explicit `skills` and `hooks` paths when a Claude manifest also exists in the same directory.
- **VAL-02:** confirm the actual Cursor edit tool name/payload and that `postToolUse.additional_context` is visible to the agent.
- **VAL-03:** confirm Cursor's project rule references the canonical rulebook reliably and does not duplicate its content through Claude imports.
- **VAL-04:** confirm what Cursor imports from the installed Claude plugin when third-party imports are enabled. The implementation must follow observed behavior, not assume a precedence rule.
- **VAL-05 (resolved for local Cursor CLI):** on 2026-10-06, the authenticated Cursor CLI used `Write` and repeated a marker present only in the `postToolUse.additional_context` hook output. A second run with the packaged plugin delivered `PROBE-001`; the event stream showed exactly one `hookAdditionalContexts` entry for the edit. Cursor IDE and marketplace installs remain unverified separately.
