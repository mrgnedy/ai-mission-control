#!/bin/sh
set -eu

plugin=$(cd "$(dirname "$0")/.." && pwd -P)
cursor="$plugin/scripts/setup_cursor.py"
claude="$plugin/scripts/setup.sh"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/learning-loop-cursor-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
repo() { mkdir -p "$1"; git -C "$1" init -q; }
id() { sed -n 's/^PLAN_ID: //p'; }
apply_cursor() {
  preview=$(python3 "$cursor" plan "$1")
  plan_id=$(printf '%s\n' "$preview" | id)
  [ -n "$plan_id" ] || fail 'Cursor preview omitted PLAN_ID'
  python3 "$cursor" apply "$1" "$plan_id" >/dev/null
}
apply_claude() {
  preview=$(sh "$claude" plan "$1")
  plan_id=$(printf '%s\n' "$preview" | id)
  [ -n "$plan_id" ] || fail 'Claude preview omitted PLAN_ID'
  sh "$claude" apply "$1" "$plan_id" >/dev/null
}

first="$tmp/cursor-first"
repo "$first"
apply_cursor "$first"
[ -f "$first/.claude/rules.md" ] || fail 'Cursor did not create shared rules'
[ -f "$first/.cursor/rules/learning-loop.mdc" ] || fail 'Cursor rule missing'
[ ! -e "$first/CLAUDE.md" ] || fail 'Cursor created CLAUDE.md'
rules_before=$(cksum "$first/.claude/rules.md" "$first/.claude/checks.tsv")
apply_claude "$first"
[ -f "$first/CLAUDE.md" ] || fail 'Claude second did not add its wiring'
rules_after=$(cksum "$first/.claude/rules.md" "$first/.claude/checks.tsv")
[ "$rules_before" = "$rules_after" ] || fail 'Claude second changed shared knowledge'
stable=$(cksum "$first/CLAUDE.md" "$first/.cursor/rules/learning-loop.mdc")
apply_cursor "$first"
apply_claude "$first"
[ "$stable" = "$(cksum "$first/CLAUDE.md" "$first/.cursor/rules/learning-loop.mdc")" ] || fail 'rerun changed wiring'

second="$tmp/claude-first"
repo "$second"
apply_claude "$second"
rules_before=$(cksum "$second/.claude/rules.md" "$second/.claude/checks.tsv")
apply_cursor "$second"
[ "$rules_before" = "$(cksum "$second/.claude/rules.md" "$second/.claude/checks.tsv")" ] || fail 'Cursor second changed shared knowledge'

custom="$tmp/custom"
repo "$custom"
mkdir -p "$custom/.Codex"
printf '# Existing custom rules\n' >"$custom/.Codex/rules.md"
printf 'ID\t*.txt\tbad\tbad line\n' >"$custom/.Codex/checks.tsv"
printf '# Existing Claude guidance\n' >"$custom/CLAUDE.md"
printf '# Existing agent guidance\n\n@.Codex/rules.md\n' >"$custom/AGENTS.md"
apply_cursor "$custom"
[ "$(sed -n '1p' "$custom/.learning-loop-rulebook-dir")" = '.Codex' ] || fail 'custom pointer wrong'
[ ! -e "$custom/.claude/rules.md" ] || fail 'created duplicate default rules'
apply_claude "$custom"
grep -Fq '@.Codex/rules.md' "$custom/CLAUDE.md" || fail 'Claude second did not import custom rules'
grep -Fq '# Existing Claude guidance' "$custom/CLAUDE.md" || fail 'Claude second lost existing guidance'

spaced="$tmp/spaced"
repo "$spaced"
mkdir -p "$spaced/Project Rules"
printf '# Existing\n' >"$spaced/Project Rules/rules.md"
printf 'ID\t*.txt\tbad\tbad line\n' >"$spaced/Project Rules/checks.tsv"
preview=$(LESSON_RULEBOOK_DIR='Project Rules' python3 "$cursor" plan "$spaced")
plan_id=$(printf '%s\n' "$preview" | id)
LESSON_RULEBOOK_DIR='Project Rules' python3 "$cursor" apply "$spaced" "$plan_id" >/dev/null
[ "$(python3 "$cursor" resolve-pointer "$spaced")" = "$(cd "$spaced/Project Rules" && pwd -P)" ] || fail 'spaced pointer did not resolve'

ambiguous="$tmp/ambiguous"
repo "$ambiguous"
mkdir -p "$ambiguous/.claude" "$ambiguous/.Codex"
printf '# A\n' >"$ambiguous/.claude/rules.md"
printf '# B\n' >"$ambiguous/.Codex/rules.md"
if python3 "$cursor" plan "$ambiguous" >/dev/null 2>&1; then
  fail 'ambiguous rulebooks were accepted'
fi

conflict="$tmp/cursor-rule-conflict"
repo "$conflict"
mkdir -p "$conflict/.cursor/rules"
printf '# User-owned rule\n' >"$conflict/.cursor/rules/learning-loop.mdc"
if python3 "$cursor" plan "$conflict" >/dev/null 2>&1; then
  fail 'different existing Cursor rule was accepted'
fi
[ ! -e "$conflict/.claude/rules.md" ] || fail 'conflict caused partial setup'

stale="$tmp/stale"
repo "$stale"
preview=$(python3 "$cursor" plan "$stale")
plan_id=$(printf '%s\n' "$preview" | id)
printf '# changed\n' >"$stale/AGENTS.md"
if python3 "$cursor" apply "$stale" "$plan_id" >/dev/null 2>&1; then
  fail 'stale Cursor plan was accepted'
fi
[ ! -e "$stale/.claude/rules.md" ] || fail 'stale apply wrote files'

unsafe="$tmp/unsafe"
repo "$unsafe"
ln -s "$first/.claude" "$unsafe/.claude"
if python3 "$cursor" plan "$unsafe" >/dev/null 2>&1; then
  fail 'symlinked target was accepted'
fi

printf 'PASS: Cursor setup and coexistence\n'
