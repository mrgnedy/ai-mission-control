#!/bin/sh
set -eu

plugin_root=$(cd "$(dirname "$0")/.." && pwd -P)
setup="$plugin_root/scripts/setup.sh"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/learning-loop-setup-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

new_repo() {
  repo=$1
  mkdir -p "$repo"
  git -C "$repo" init -q
}

repo="$tmp/fresh"
new_repo "$repo"
printf '# Existing instructions\n' >"$repo/CLAUDE.md"
before=$(cksum <"$repo/CLAUDE.md")
plan=$(sh "$setup" plan "$repo")
after=$(cksum <"$repo/CLAUDE.md")
[ "$before" = "$after" ] || fail 'plan changed CLAUDE.md'
[ ! -e "$repo/.claude/rules.md" ] || fail 'plan created rules.md'
printf '%s' "$plan" | grep -q 'PREPEND: CLAUDE.md' || fail 'plan did not describe the prepend'
printf '%s' "$plan" | grep -q 'CREATE: .claude/rules.md' || fail 'plan did not describe rules.md'
plan_id=$(printf '%s\n' "$plan" | sed -n 's/^PLAN_ID: //p')
[ -n "$plan_id" ] || fail 'plan did not emit PLAN_ID'
sh "$setup" apply "$repo" "$plan_id" >/dev/null
[ -f "$repo/.claude/rules.md" ] || fail 'apply did not create rules.md'
[ -f "$repo/.claude/checks.tsv" ] || fail 'apply did not create checks.tsv'
first=$(sed -n '1p' "$repo/CLAUDE.md")
[ "$first" = '## Shared rulebook' ] || fail 'approved block was not prepended'
count=$(grep -Fc '@.claude/rules.md' "$repo/CLAUDE.md")
[ "$count" = 1 ] || fail 'rulebook import was duplicated'
grep -q '# Existing instructions' "$repo/CLAUDE.md" || fail 'existing CLAUDE.md content was lost'

stable_before=$(cksum "$repo/CLAUDE.md" "$repo/.claude/rules.md" "$repo/.claude/checks.tsv")
plan=$(sh "$setup" plan "$repo")
plan_id=$(printf '%s\n' "$plan" | sed -n 's/^PLAN_ID: //p')
sh "$setup" apply "$repo" "$plan_id" >/dev/null
stable_after=$(cksum "$repo/CLAUDE.md" "$repo/.claude/rules.md" "$repo/.claude/checks.tsv")
[ "$stable_before" = "$stable_after" ] || fail 'second apply was not idempotent'

preserve="$tmp/preserve"
new_repo "$preserve"
mkdir -p "$preserve/.claude"
printf 'custom rules\n' >"$preserve/.claude/rules.md"
printf 'custom checks\n' >"$preserve/.claude/checks.tsv"
printf '@.claude/rules.md\n\n# Existing\n' >"$preserve/CLAUDE.md"
preserve_before=$(cksum "$preserve/CLAUDE.md" "$preserve/.claude/rules.md" "$preserve/.claude/checks.tsv")
plan=$(sh "$setup" plan "$preserve")
plan_id=$(printf '%s\n' "$plan" | sed -n 's/^PLAN_ID: //p')
sh "$setup" apply "$preserve" "$plan_id" >/dev/null
preserve_after=$(cksum "$preserve/CLAUDE.md" "$preserve/.claude/rules.md" "$preserve/.claude/checks.tsv")
[ "$preserve_before" = "$preserve_after" ] || fail 'existing repository wiring was overwritten'

stale="$tmp/stale"
new_repo "$stale"
plan=$(sh "$setup" plan "$stale")
plan_id=$(printf '%s\n' "$plan" | sed -n 's/^PLAN_ID: //p')
printf '# Changed after preview\n' >"$stale/CLAUDE.md"
if sh "$setup" apply "$stale" "$plan_id" >/dev/null 2>&1; then
  fail 'apply accepted a stale plan'
fi
[ ! -e "$stale/.claude/rules.md" ] || fail 'stale apply made partial changes'

printf 'PASS: setup.sh\n'
