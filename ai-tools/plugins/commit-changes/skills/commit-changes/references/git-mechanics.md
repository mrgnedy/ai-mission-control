# Git mechanics

The three techniques that make a clean split practical: putting one file's hunks into two
commits, checking every commit builds, and redoing commits safely when one doesn't.

## Contents

- [Splitting one file across commits](#splitting-one-file-across-commits)
- [Per-commit build verification](#per-commit-build-verification)
- [Project check commands](#project-check-commands)
- [Redoing commits safely](#redoing-commits-safely)

---

## Splitting one file across commits

A file often contains changes belonging to two different intents. `git add -p` and `git add -i`
need a TTY and are unavailable in most agent environments, so stage the subset through the index
with a trimmed patch instead.

```bash
# 1. Dump the file's diff to a scratch patch
git diff -- path/to/file.ext > /tmp/full.patch

# 2. Inspect hunk boundaries
grep -n '^@@' /tmp/full.patch

# 3. Keep the header (the 4 lines before the first @@) plus the wanted hunks
{ sed -n '1,4p' /tmp/full.patch; sed -n '<start>,<end>p' /tmp/full.patch; } > /tmp/subset.patch

# 4. Apply to the index only — the working tree keeps the full change
git apply --cached --whitespace=nowarn /tmp/subset.patch

# 5. Prove the split
git diff --cached --stat        # what this commit will contain
git diff --stat -- path/to/file.ext   # what is left for the later commit
```

Hunk line counts inside each `@@` header stay correct when you drop whole hunks, and `git apply`
locates hunks by their old-side context, so dropped hunks do not need renumbering. Never split a
hunk in half — that changes the counts and the patch will be rejected.

After the first commit, the later commit just stages the file normally with `git add`.

### When the default context merges hunks you need apart

`git diff` uses three lines of context, so two changed regions separated by fewer than six
unchanged lines land in one hunk. `-U1` separates them:

```bash
git diff -U1 -- path/to/file.ext > /tmp/full.patch
```

`git apply --cached` accepts the narrower context fine. If even `-U1` leaves them merged, the
regions are genuinely adjacent.

### When not to split

If a file's changes are one contiguous rewrite — a function body restructured such that two
concerns are interleaved line by line — splitting means *rewriting code*, which is beyond the
scope of organising commits. Merge the two commits instead and write a subject that honestly
covers both. A slightly broad commit is better than an invented intermediate state that never
existed.

## Per-commit build verification

Worth running whenever the split renamed an API, split a file's hunks, or produced more than a
handful of commits. It is the only thing that reliably catches a commit that compiles in the
final tree but not on its own.

Use a detached worktree so the user's working tree and any uncommitted files are untouched:

```bash
W=/tmp/verify-wt
git worktree add -q --detach $W HEAD
```

### Trap 1: missing generated and ignored files

A fresh worktree has no `.gitignore`d build output — no `.dart_tool/`, no `node_modules/`, no
`*.g.dart` / `*.freezed.dart` / `*.pb.go`. Without them the checker reports hundreds of errors
that have nothing to do with the commits, drowning the real signal.

Copy them in once. They are untracked, so `git checkout` between commits leaves them alone:

```bash
# package resolution
cp -R <main>/.dart_tool $W/.dart_tool          # or node_modules, target/, .venv…

# generated sources that live alongside the code
cd <main> && git ls-files --others --ignored --exclude-standard <src-dir> \
  | grep -E '\.(g|freezed|pb)\.(dart|go|ts)$' \
  | rsync -a --files-from=- . $W/<src-dir>/
```

### Trap 2: an error pattern that matches nothing

Counting errors with a wrong pattern reports zero for every commit — including broken ones — and
looks like success. Analyzer output is often indented (`  error • …`), so an anchored `^error`
silently matches nothing.

**Always validate the pattern against a commit you know is broken** before trusting a clean
sweep. If you have no known-bad commit, deliberately break one file in the worktree and confirm
your pattern catches it.

### The sweep

```bash
for c in $(git log --format=%h --reverse <base>..HEAD); do
  git -C $W checkout -q --detach $c
  OUT=$(cd $W/<src-dir> && <check command> 2>&1)
  N=$(printf '%s\n' "$OUT" | grep -c 'error')       # pattern validated above
  echo "$c  errors=$N  $(git log --format=%s -1 $c)"
  printf '%s\n' "$OUT" | grep 'error' | head -3
done
```

Compare against a baseline at the merge base — most repositories carry pre-existing warnings and
sometimes errors, so the target is *no new* errors, not zero.

Clean up when done:

```bash
git worktree remove --force $W && git worktree prune
```

## Project check commands

Detect from the manifest present in the repo. Prefer a type-check or analyze step over a full
build — it is far faster and catches the same class of breakage.

| Ecosystem | Marker | Command |
|---|---|---|
| Flutter | `pubspec.yaml` + `flutter` dep | `flutter analyze --no-pub <dirs>` |
| Dart | `pubspec.yaml` | `dart analyze <dirs>` |
| TypeScript | `tsconfig.json` | `npx tsc --noEmit` |
| JavaScript | `package.json` | the `typecheck`/`lint`/`build` script it defines |
| Rust | `Cargo.toml` | `cargo check --all-targets` |
| Go | `go.mod` | `go build ./...` |
| Python | `pyproject.toml` | `ruff check` and/or `mypy` |
| Kotlin/Java | `build.gradle` | `./gradlew compileKotlin` / `compileJava` |
| Swift | `Package.swift` / `.xcodeproj` | `swift build` / `xcodebuild -quiet` |

Scope to the directories the change touches where the tool allows it — a whole-repo check on a
large project is slow enough that it discourages running the sweep at all.

If the project defines its own check in `CLAUDE.md`, a Makefile, or a `scripts/` entry, use that
instead — it encodes flags the table cannot know.

## Redoing commits safely

When verification finds a broken commit, the fix is almost always regrouping — usually merging
the broken commit with the one that completes it — not editing code.

`git rebase -i` needs a TTY. Replay instead: the working tree already holds the final state, so
resetting and re-staging by path reproduces each commit exactly.

```bash
# 1. Safety net you can always return to
git tag split-backup HEAD

# 2. Mixed reset to the last good commit — HEAD and index move, working tree does not
git reset <last-good-commit>

# 3. Everything from the discarded commits is now unstaged. Re-stage and re-commit by group.
git add <paths for the merged commit>
git commit -F - <<'EOF'
feat: PROJ-123 combined subject covering both intents
EOF
# … repeat for the remaining groups

# 4. Prove the final tree is identical to before the redo
git diff split-backup HEAD --stat     # must print nothing

# 5. Only once that is clean
git tag -d split-backup
```

`git reset` without `--hard` is the important part: it preserves the working tree, including
files the user asked to leave uncommitted. `--hard` would destroy them.

Two things to check after any replay:

- `git status --porcelain` shows exactly the files that were meant to stay uncommitted, and
  nothing else
- `git diff <backup-tag> HEAD` is empty, proving the content is unchanged and only the commit
  boundaries moved

Files that appear in `git status` but were not in the original survey were created by something
else while you worked — the user's editor, a build step, an asset export. Leave them alone and
mention them in the report rather than folding them into a commit.
