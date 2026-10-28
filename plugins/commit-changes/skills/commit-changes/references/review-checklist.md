# Review checklist

What to hunt for in Phase 3, and how to judge it. Work through the four categories against the
full diff, then present everything in one grouped summary.

## Contents

- [1. Secrets and leaks](#1-secrets-and-leaks)
- [2. Config files](#2-config-files)
- [3. Leftovers and accidents](#3-leftovers-and-accidents)
- [4. Regression blast radius](#4-regression-blast-radius)
- [Comment triage](#comment-triage)
- [Presenting findings](#presenting-findings)

---

## 1. Secrets and leaks

Highest severity, and the reason the review happens before anything is staged. Git keeps the
object even after a later commit removes the line, so "I'll take it out in the next commit"
does not work — undoing it means rewriting history or rotating the secret.

Scan added lines for:

- API keys and tokens — long opaque strings, and recognisable prefixes: `AIza` (Google),
  `sk-` / `sk_live_` (OpenAI, Stripe), `ghp_` / `github_pat_` (GitHub), `xox` (Slack),
  `AKIA` (AWS), `eyJ` (JWT), `-----BEGIN ... PRIVATE KEY-----`
- Passwords, connection strings, `Authorization:` headers with a literal value
- Internal hostnames, staging URLs, VPN-only endpoints, database DSNs
- Real customer data in fixtures or tests — names, emails, phone numbers, account numbers
- `.env` files and anything mirroring one

A useful cross-check: if some entries in a file read a value from the environment and others
hardcode it, the hardcoded ones are almost certainly a mistake.

```bash
git diff | grep -nE '(AIza|sk-|sk_live_|ghp_|github_pat_|xox[abpr]-|AKIA|eyJ[A-Za-z0-9_-]{10,})'
git diff | grep -niE '(api[_-]?key|secret|passwd|password|token|credential)[[:space:]]*[:=]'
```

Treat these as a starting point, not a guarantee — read the diff too. Custom key formats and
base64 blobs match nothing.

**Proposal to offer:** replace with an environment variable or the project's existing secrets
mechanism, and mirror however the file's other entries already do it. If the secret is already
in the remote's history, say so explicitly — the fix is rotation, not deletion.

**Leave the file unstaged; do not rewrite the line yourself.** The working tree may hold the
only copy of that value. Replacing it with `os.environ.get(...)` before the user has put the
real value somewhere safe destroys it, turning a contained problem into a lost credential and a
forced rotation. Keeping the file out of the commit already achieves the goal — nothing enters
history — while leaving the user in a position to move the value themselves. Say precisely that:
the key is still only in the working tree, so no rotation is needed *provided it is never
staged*. This is the rare case where doing less is strictly safer than doing more.

## 2. Config files

Config changes alter behavior for everyone who pulls, often invisibly, and they are easy to
make by accident while debugging. Surface every one with what changed, even when it is
obviously intentional — the point is that the user sees it, not that it is wrong.

Watch for:

- Dependency manifests and lockfiles — `package.json`, `pubspec.yaml`, `Cargo.toml`,
  `go.mod`, `requirements.txt`, `Gemfile`, `pom.xml`, `build.gradle`, and their lockfiles
- CI and automation — `.github/workflows/`, `.gitlab-ci.yml`, `Jenkinsfile`, `.circleci/`
- Environment and runtime — `.env*`, `Dockerfile`, `docker-compose.yml`, k8s manifests,
  `Procfile`, Terraform
- Editor and tooling — `.vscode/`, `.idea/`, `.editorconfig`, formatter and linter configs
- Platform build files — `Info.plist`, `AndroidManifest.xml`, `*.entitlements`, signing config
- `.gitignore` — especially entries that would start hiding files from the team

Things worth calling out specifically: a dependency version loosened or pinned, a lockfile
changed without a manifest change (or the reverse), a lint rule disabled, a timeout or retry
count raised, a feature flag flipped, a test excluded from CI.

## 3. Leftovers and accidents

Code written to get something working that was never meant to ship. It is easy to spot in a
diff and nearly invisible once merged.

**Debug and instrumentation**
`print` / `console.log` / `debugPrint` / `dump` / `NSLog`, verbose logging added to trace a bug,
a breakpoint helper, timing instrumentation.

**Dead and unreachable code**
Statements after a `return`/`throw`, a branch whose condition cannot hold, a function no longer
called by anything, a file left orphaned when its last import was removed. Most linters catch
these — running the project's checker before the review is a cheap way to find them.

**Mocks and stubs on a live path**
Hardcoded response objects, `Future.delayed` / `sleep` simulating latency, a real API call
swapped for a fixture, a feature flag forced to a constant, an auth check short-circuited.
`TODO: REVERT`, `HACK`, `XXX`, `DO NOT MERGE`, `TEMP` are the usual markers — grep for them, but
also read for the unmarked ones.

**Commented-out code**
Especially a commented line sitting next to the live line that replaced it, which usually means
someone was toggling between two behaviors and stopped mid-experiment.

**Disabled tests**
`skip`, `xit`, `it.only`, `@Ignore`, `#[ignore]`, `@pytest.mark.skip`. `.only` is worse than
`skip` — it silently stops the rest of the suite from running.

**Environment-specific values**
`localhost`, a dev or staging hostname, a personal device id, a test account, a hardcoded date
or user id that made one scenario reproduce.

**Accidental edits**
Whitespace-only or reformat-only changes to files the work had no reason to touch — usually a
format-on-save in a file that was opened to read. Worth flagging because they inflate the diff
and bury real changes. `git diff -w --stat` next to `git diff --stat` shows which files are
purely cosmetic.

```bash
git diff | grep -nE '^\+' | grep -niE '(TODO:? ?REVERT|DO NOT MERGE|HACK|XXX|FIXME|TEMP)'
git diff | grep -nE '^\+' | grep -nE '(console\.log|debugPrint|print\(|NSLog|dbg!)'
git diff -w --stat            # compare with git diff --stat to find whitespace-only churn
```

## 4. Regression blast radius

The question to answer: **what does this diff change that is used by code it did not touch?**

Everything inside the feature being built is the intended target. The risk lives at the edges.

**Shared code.** Any file outside the feature's own directory — utilities, design-system
widgets, base classes, interceptors, formatters, extension methods. A change here reaches
callers nobody was thinking about. List them explicitly and check each caller.

**Signature changes.** A parameter renamed, removed, reordered, or made required; a return type
narrowed or widened; a nullable made non-nullable. Find every call site — do not assume the
compiler will catch it, because dynamically typed languages and named-argument APIs often fail
at runtime instead.

```bash
git grep -n '<old symbol name>'        # before committing, across the whole tree
```

**Behavior changes under a stable signature** — the dangerous ones, because nothing fails to
compile. A default value changed, a fallback removed, an early return added, a filter applied to
a list every caller shares, ordering changed, a cache introduced, an error now swallowed instead
of thrown.

**Lifecycle and ownership.** Who creates and disposes a resource, whether a listener is removed,
whether a subscription is cancelled, whether an object is now shared where it used to be
per-instance. Leaks and use-after-dispose crashes live here.

**Data-shape changes.** A parsing change or renamed field means anything reading the old shape
now gets null. Check serializers, persisted state, and cached payloads — a model change can
break users who upgrade with old data on disk.

**Concurrency.** Work moved onto or off a background thread, an `await` added or removed,
parallel calls where there were sequential ones.

For each risk, state the concrete failure: which input, which caller, what goes wrong. "This
might break something" is not actionable; "`CardListCubit` now filters closed cards, so the
settings screen's card count drops by one for users with a cancelled card" is.

## Comment triage

The test is not length or style — it is whether removing the comment loses information that
isn't in the code.

**Remove** (safe to do without asking, since nothing changes at runtime):

| Pattern | Example |
|---|---|
| Restates the code | `// increment the counter` above `counter++` |
| Empty structural banner | `// ---------- helpers ----------` with one helper below |
| Stale — describes behavior that changed | `// returns null on error` above code that throws |
| Unfilled generator scaffolding | `// TODO: implement`, `// Your code here` |
| Redundant doc stub | `/** Gets the name. */` above `getName()` |
| Commented-out code with no explanation | a dead line nobody will ever restore |

**Keep** — deleting these is a real loss, and a much harder mistake to spot later:

| Pattern | Example |
|---|---|
| Explains *why* | `// swallowed on purpose: the screen falls back to the single card` |
| Encodes a non-obvious constraint | `// the toggle hangs 15px below the card, so budget for the overhang` |
| Documents an external quirk | `// the emboss pipeline has no dispatch state` |
| Warns about a trap | `// must run before init(), which reads this value` |
| Public API documentation | doc comments on exported symbols |
| Design or ticket reference | `// Figma 13780-13883`, `// see PROJ-4412` |
| TODO with an owner or ticket | `// TODO(phase-2): open the Report Issue sheet` |

When a comment sits between the two — vague but possibly meaningful — keep it and mention it in
the report. The cost of an unnecessary comment is small; the cost of deleting the one line that
explained a workaround is a future bug.

## Presenting findings

One message, grouped by category, most severe first. For each finding:

1. `file:line`
2. What it is, in one sentence
3. Why it matters — the concrete consequence
4. A specific proposal, not "consider reviewing this"

Then ask for the decision. `AskUserQuestion` fits discrete choices (commit as-is / fix first /
leave the file uncommitted); prose fits a list the user needs to react to item by item.

Two things to get right:

- **Do not block on everything.** If most findings are minor, say which ones actually need a
  decision and proceed with a stated assumption on the rest.
- **Their answer governs.** If the user says ship the mock, ship it, note it in the commit body
  so the next reader knows it was deliberate, and repeat it in the final report. Do not
  re-litigate a decision they already made.
