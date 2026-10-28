---
name: commit-changes
description: Review, clean up, and commit working-tree changes as a sequence of focused, conventionally typed commits (feat, fix, docs, style, refactor, perf, test, build, ci) that each build on their own. Use this whenever the user wants to commit or stage uncommitted work — "commit this", "commit my changes", "organize these changes into commits", "split this into meaningful commits", "clean up and commit", "prepare this for review/PR" — and also when they ask you to tidy a messy working tree before pushing. It derives the issue key from the branch and recent commits, flags secrets, config-file edits, and leftover debug or test code before anything is staged, reviews the diff for regressions outside the intended blast radius, and verifies every commit compiles.
---

# Commit changes

Turn a messy working tree into a sequence of commits a reviewer can actually read.

The value here is not "run `git commit`" — anyone can do that. It is everything that happens
before: understanding what the diff actually contains, catching the things that should never
reach history, and grouping changes so each commit is one idea that stands on its own.

Work through the phases in order. Do not stage anything until Phase 3 is done and the user has
answered.

---

## Phase 1 — Survey

Read the whole diff before deciding anything. Grouping by directory or file name produces
commits that look tidy and mean nothing; grouping by *intent* requires knowing what the code
does.

```bash
git status --porcelain
git diff --stat
git diff                     # tracked changes, in full
git log --oneline -20        # what this branch has been doing
```

For untracked files, read them — they are usually the centre of gravity of the change, and
they never show up in `git diff`.

If the working tree is large, read it in batches by area rather than skimming. Missing one
hunk means a commit that doesn't compile or a secret that ships.

**If the current branch is the default branch** (`main`/`master`/`develop`), stop and offer to
create a branch first. Committing feature work straight onto the default branch is almost never
what the user wants, and it is annoying to undo.

## Phase 2 — Learn the branch's conventions

Never invent a message format. Match what the repository already does.

```bash
git log -8 --format='===%n%B'    # full bodies, not just subjects — reveals trailers and style
git rev-parse --abbrev-ref HEAD  # branch name often carries the issue key
```

**Issue key.** Most teams prefix every subject with a tracker key like `TNOQPS-19582`,
`PROJ-4412`, `ABC-77`. Find it in this order:

1. The branch name (`feature/TNOQPS-19582/physical-card` → `TNOQPS-19582`)
2. The `git log` subjects on this branch — take the key that dominates recent commits
3. If neither yields one, ask the user rather than guessing or omitting it

Use the same key for every commit in the batch unless the user says otherwise.

**Type.** The type drives semantic-release: it decides the version bump, and a type the tool
doesn't know produces the wrong release or none at all. So use only these nine:

| Type | Use it when the commit… |
|---|---|
| `feat` | adds a capability a user could notice — a screen, an endpoint, a behavior |
| `fix` | corrects something that was wrong — a bug, a broken call, wrong copy, a model that didn't match its API |
| `docs` | changes documentation only — READMEs, API logs, doc comments, rulebooks |
| `style` | changes formatting only — whitespace, import order, lint autofixes; no behavior change |
| `refactor` | restructures code without changing behavior — renames, extractions, moves |
| `perf` | makes existing behavior faster or lighter, with nothing else changing |
| `test` | adds or changes tests only |
| `build` | changes dependencies, build scripts, or generated build config — `pubspec.yaml`, Gradle, Podfile, lockfiles |
| `ci` | changes CI pipelines and hooks — workflow files, `.githooks/`, gate scripts |

`chore`, `wip`, `update`, and anything else are not valid. Map them to the nearest type
above. Tidy-up with no behavior change is `refactor` or `style`, and a dependency bump is
`build`.

Pick the type by the commit's *intent*, not by which files it touches. A dependency added so a
new screen can exist belongs in that screen's `feat` commit. A parser rewritten because it never
matched the real payload is `fix`, not `refactor`. A commit that adds a feature together with its
tests is `feat`. `test` is only for commits whose sole purpose is tests. If a commit seems to
need two types, it is usually two commits (see Phase 4).

**Breaking changes.** Write `feat!` or `fix!` (a major version bump) when the commit removes or
changes behavior that something outside the repo depends on. Examples: a public API or endpoint
contract, a persisted data format, a deep link. Never add `!` on your own judgement alone. Name
the break in your plan and get the user's yes, because it decides a major release.

**The repo's own rule wins.** If the repository enforces a narrower or different list, follow
it and mention it in your report. That list may be in a `commit-msg` hook (`.githooks/`,
`.husky/`, `core.hooksPath`), a commitlint config, or a rules file such as `CLAUDE.md` or
`.claude/rules.md`. The same goes for allowed issue-key prefixes. If the branch's key is not on
the repo's list, ask the user before committing. The hook would reject the commit anyway.

**Subject.** `<type>: <ISSUE-KEY> <lowercase imperative summary>` — matching whatever
separator and casing the branch already uses. Around 50–72 characters.

**Body.** If the branch's recent commits carry bodies, write bodies. A good body explains what
was wrong or missing and why this approach, not what the diff already shows. If the branch's
commits are subject-only, stay subject-only — do not invent multi-paragraph explanations for a
three-line change just because you have things to say.

**Trailers** (`Co-Authored-By`, `Signed-off-by`, `Reviewed-by`) follow a different rule from
body style, and the two can look like they conflict:

- The branch uses a trailer → use it, formatted identically.
- The branch uses none, but your environment or the project's instructions require one → add
  it, and note in your report that you did and why. A standing instruction is policy; a
  branch's habit is a local norm, and policy wins. This is the one place where departing from
  the branch is right.
- Neither → add none. Never introduce a trailer on your own initiative.

A trailer is not a body. A subject plus a required trailer is still subject-only style, and it
does not license adding prose alongside it.

## Phase 3 — Review, and stop for the user

This is the phase that earns the skill. Go through the diff hunting for four categories of
problem, then present everything at once and wait.

Read `references/review-checklist.md` for the full catalogue of what to look for in each
category and how to judge severity. The summary:

1. **Secrets and leaks** — API keys, tokens, passwords, private URLs, real customer data,
   credentials in test fixtures. These are the reason this phase exists. A secret committed is
   a secret leaked, even if the next commit removes it, because the object stays in history.
2. **Config files touched** — dependency manifests and lockfiles, CI pipelines, `.env*`,
   editor and IDE settings, build files, `.gitignore`, container and deploy manifests. Config
   edits ride along invisibly and change behavior for everyone. Surface every one, with what
   changed, even when it looks intentional.
3. **Leftovers and accidents** — debug logging, unreachable code, mocked or stubbed data paths,
   `TODO: REVERT`, commented-out code, disabled tests, hardcoded values pointing at a dev
   environment, whitespace-only edits to files the change had no reason to touch.
4. **Regression risk** — read `references/review-checklist.md` for how to trace blast radius.
   The core question: what does this diff change that is used by code it did not touch? Renamed
   or removed parameters, changed function signatures, edits to shared utilities and widgets,
   removed defaults or fallbacks, changed lifecycle or state ownership.

If the project has its own code-review skill or command available, use it here for the
regression pass rather than duplicating the work — then fold its findings into the same summary.

**Present findings once, grouped, most severe first.** Do not interrupt four separate times.
For each finding give the file:line, what it is, why it matters, and a concrete proposal.

**Scale the report to what you found.** The four categories are a checklist for *you*, not an
output template. A clean diff deserves one line — "no secrets, config changes, or leftovers;
one regression risk below" — not four headings, three of which say "none". Writing a paragraph
to report the absence of something buries the one finding that matters, and it is the fastest
way to train the reader to skim past your findings entirely. Say what you checked in a clause,
then spend the words on what you found.

Keep the user's vocabulary, not the skill's. Phrases like "Phase 3 findings" or "the ordering
constraint from Phase 4" leak internal scaffolding into the user's report; they name their work,
not yours. Say "before I stage anything" and "committing these separately would break the build".

Then wait for the user's decision. Use `AskUserQuestion` when the choices are discrete
(commit as-is / fix first / leave uncommitted), plain prose when you are handing them a list to
react to. Their answer governs — if they say commit the mock as-is, commit the mock as-is and
say so plainly in the commit body.

### Cleanup you can apply without asking

Comments that carry no information are noise, and removing them changes nothing at runtime, so
clean them up as part of the relevant commit and mention it in your report:

- Comments restating the code (`// increment counter` above `counter++`)
- Empty section banners, `// ---- helpers ----` with nothing to disambiguate
- Stale comments describing behavior the code no longer has
- Scaffolding left by a generator or template that was never filled in

**Keep** anything that explains *why*: non-obvious constraints, API quirks, workarounds with a
reason, doc comments on public API, design references (`Figma 13780-13883`), and TODOs that name
a ticket or owner. Deleting those is a real loss and much harder to notice than leaving noise in.
When a comment's value is unclear, keep it and mention it — do not silently delete.

Everything else — dead code, mocks, debug statements, hardcoded values, config edits — needs the
user's word before you touch it.

## Phase 4 — Group into commits

One commit is one intent. A reviewer should be able to read the subject, then the diff, and find
nothing in the diff that the subject didn't promise.

Good seams to split along:

- A model/schema change vs. the UI that consumes it
- New shared capability vs. the feature that first uses it
- Each unrelated bug fix — these are almost always their own commit, however small
- Assets and generated files vs. the code that references them

Bad seams: "all the files in `data/`", "everything I edited on Tuesday", one commit per file.

**Ordering is a hard constraint, not a preference.** Each commit must build on its own, which
means a commit that renames or removes an API must land *together with every caller of it*. This
is the single most common way a well-intentioned split produces a broken history:

> Commit A renames `showVirtualBadge` → `cardTypeBadeLabel` in the widget and updates one call
> site. Commit B updates the second call site. Commit A does not compile.

Before finalising the grouping, for every symbol the diff renames or removes, list its callers
and confirm they are all in the same commit or earlier. If two changes are mutually dependent —
A needs B's new parameter, B needs A's new widget — they are one commit. Merge them and write an
honest subject covering both rather than shipping a broken intermediate.

Sometimes a single file's changes belong to two commits. See
`references/git-mechanics.md` for how to split a file's hunks across commits, and for when not
to bother: if the file's changes are one contiguous rewrite, splitting means rewriting code,
which is out of scope — merge the commits instead.

Tell the user the plan — the ordered list of subjects and what goes in each — before you start
committing, unless they have already approved a plan. It is much cheaper to re-group on paper
than to unpick six commits.

## Phase 5 — Commit

Stage by explicit path, never `git add -A` or `git add .`. Blanket staging is how the file the
user asked you to leave alone ends up in a commit.

```bash
git add <explicit paths>
git diff --cached --stat        # confirm exactly what is staged
git commit -F - <<'EOF'
feat: PROJ-123 subject line here

Body explaining what was wrong or missing and why this approach.

Co-Authored-By: ...
EOF
```

Use `-F -` with a heredoc rather than chained `-m` flags: it keeps blank lines and wrapping
intact and stops shell quoting from mangling the body.

Do not push and do not open a PR unless the user asks.

## Phase 6 — Verify

Two checks, both cheap relative to what they catch.

**The final tree.** Run the project's own check — read `references/git-mechanics.md` for
detecting the right command per ecosystem (`flutter analyze`, `tsc --noEmit`, `cargo check`,
`go build ./...`). Compare against the pre-existing baseline: the goal is *no new* errors, since
most real repositories already have warnings.

**Every commit, individually** — but only when the split could actually have broken one. The
sweep costs real time, so spend it where the risk is:

| Situation | Check |
|---|---|
| A symbol was renamed or removed, or a signature changed | Full sweep — this is the case that breaks |
| A file's hunks were split across commits | Full sweep |
| A new module landed in a different commit from its first importer | Full sweep |
| More than ~5 commits | Full sweep |
| A few commits, no renames, no split hunks, each self-contained | Final tree only — say that you scoped it and why |

`references/git-mechanics.md` has the throwaway-worktree recipe, including the two traps that
make this check silently useless: gitignored generated files missing from a fresh worktree, and
an error-matching pattern that matches nothing.

If a commit fails, fix the grouping (usually by merging two commits), not the code. Read
`references/git-mechanics.md` for how to redo commits safely — tag first, mixed-reset, replay —
and how to prove the final tree is unchanged afterwards.

## Phase 7 — Report

Lead with what the user needs to know, not a recap of the process:

- The commit list, oldest last, as it will appear in `git log --oneline`
- Any deviation from the plan and the reason — a merged pair, a dropped split
- What was verified and how, stated plainly: "all 11 commits analyze with zero new errors"
- What remains uncommitted and why
- Findings the user chose not to act on, so they are not forgotten
- Improvement proposals, kept separate from the commits — see below

If a verification step was skipped or inconclusive, say so. A confident "all good" that wasn't
checked is worse than an honest gap.

Length should track what happened, not how much work you did. A three-file change split into two
clean commits is a short report — the commit list, one line on verification, done. Reserve the
detail for what the user has to act on or decide. Recapping your own process at length reads as
padding and buries the one line they needed.

### Improvement proposals

Committing is a good moment to notice things, and a bad moment to act on them. Where you spotted
something worth changing beyond the scope of this work — a performance problem, a readability or
architecture improvement, a pattern the codebase would benefit from — describe it briefly at the
end of the report with a concrete suggestion, and leave it out of the commits. Rank by impact and
keep it short; three sharp observations beat a checklist of generic advice. If a task-spawning
tool is available, offering to spin one off is better than a paragraph the user has to re-explain
later.

---

## Reference files

- `references/review-checklist.md` — what to look for in Phase 3: secrets, config files,
  leftovers, regression blast radius, and comment triage rules
- `references/git-mechanics.md` — splitting a file's hunks across commits, per-commit build
  verification, per-ecosystem check commands, and safely redoing commits
