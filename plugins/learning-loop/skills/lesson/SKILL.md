---
name: lesson
description: "Turn feedback or a repeated implementation insight into a durable project rule. Use when the user corrects a decision or states a lasting preference (\"always X\", \"use Y instead\", \"don't do Z\", \"next time\"), when finishing substantial work, or when you notice a pattern worth preventing. Writes to the project rulebook only after explicit confirmation."
user-invocable: true
disable-model-invocation: false
---

# Capturing a lesson

Turn only durable, non-obvious feedback into policy. Most work produces no new rule; adding noise
makes the rulebook less useful.

**Write nothing until the user explicitly approves the exact wording.** This applies equally to
user corrections and self-discovered patterns. A rule with the wrong scope is worse than no rule.

## Locate the project contract

Read the repository instructions, including `CLAUDE.md`, `AGENTS.md`, and their imports. Find the
canonical rulebook, the checks file, and the wiring that loads the rules. Never infer a rulebook
path from this installed skill.

The plugin convention is `.claude/rules.md`, `.claude/checks.tsv`, and optional
`.claude/rulecheck.config`. The installed plugin owns the hook and checker; the repository owns
the rules and checks. Do not copy `hooks/rule_check.sh` into the repository.

If the repository names different rulebook paths, follow its instructions. If it has no rulebook,
propose initialization as a separately approved step through `/learning-loop:setup`. Use
`assets/wiring.md` when the user wants to inspect or perform the setup manually. If a rulebook
exists without checks, do not invent tier-2 wiring during a lesson unless the user also approves
that setup.

## Route the lesson

- **Project rule** — code, architecture, testing, documentation, or repository workflow belongs
  in the version-controlled rulebook.
- **Personal preference** — communication and collaboration preferences belong in a supported
  personal-memory mechanism, still after confirmation.
- **One-off** — fix the instance and write no policy.

## Shape the candidate

1. Restate it as one imperative sentence in the user's terms.
2. Scope it to the narrowest boundary justified by the evidence. If two scopes are plausible,
   present both rather than guessing.
3. State why it exists. Ask instead of inventing a reason.
4. Search higher-priority instructions, specifications, and the rulebook for existing coverage.
   Treat a duplicate as an example under the existing rule, not a new rule.
5. Amend rather than append when a current rule is incomplete or overlapping.

## Choose the strongest practical tier

| Tier | Meaning | Example |
|---|---|---|
| **1 — structural** | The design makes the wrong action unavailable | a shared abstraction removes the invalid choice |
| **2 — automated** | A test, lint, or checks line catches it | a precise `checks.tsv` entry |
| **3 — prose** | Written policy applied by judgement | a contextual architecture decision |

Propose tier 1; never implement it silently. For tier 2, prefer precision over recall. The checker
inspects only added lines, so it need not tolerate legacy violations, but it must not flag correct
new code.

## Confirm one rule at a time

Draft the complete block before asking. Show the exact text that would be written:

- **Rule** — one imperative sentence;
- **Scope** — the narrowest applicable boundary;
- **Why** — the decision-making reason;
- **Don't/Do** — only when a contrast adds clarity;
- **Tier** — including deliberately partial automation coverage;
- **Added** — today's date and either `user feedback` or `self-proposed, confirmed`.

Offer at least: add as written, narrow the scope, reword, or skip. Confirm candidates separately.

## Write and verify

1. Append or amend the rulebook while preserving its format.
2. Add a check only when the approved tier calls for one and the repository already has the
   checks file. Test one violating and one compliant uncommitted change by making Claude perform
   a `Write` or `Edit`; the plugin's post-write hook must fire for the violation only.
3. Confirm every agent bootstrap still imports the rulebook.
4. Search for stale duplicates and old paths.
5. Keep within any rulebook size cap; at the cap, propose what the new rule replaces.

## Offer a separate back-sweep

Count existing violations and report the number, but offer remediation as a separate approved
change. Never fold it into the lesson capture.

## When not to capture

Write no rule for a typo, a misread requirement, a one-file exception, already-covered policy, or
a claim whose reason cannot be stated. Most sessions should produce no lesson.

## Subagents without confirmation

A subagent that cannot receive user approval must not write a rule. Return candidate rules to the
user-facing parent, with the imperative wording, reason, and narrowest scope. Omit the section when
there are no genuine candidates.
