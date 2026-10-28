#!/bin/sh
set -eu

plugin_root=$(cd "$(dirname "$0")/.." && pwd -P)
checker="$plugin_root/hooks/rule_check.sh"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/learning-loop-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

repo="$tmp/repo"
mkdir -p "$repo/.claude" "$repo/src"
git -C "$repo" init -q
git -C "$repo" config user.name 'Learning Loop Test'
git -C "$repo" config user.email 'learning-loop@example.invalid'
printf '# Rules\n' >"$repo/.claude/rules.md"
printf 'T-001\tsrc/*.txt\tforbidden\tDo not add forbidden.\nT-002\tsrc/new-*.txt\t@new\tReview every new matching file.\n' >"$repo/.claude/checks.tsv"
printf 'baseline\n' >"$repo/src/existing.txt"
git -C "$repo" add .
git -C "$repo" commit -qm baseline

printf 'allowed\n' >>"$repo/src/existing.txt"
payload=$(printf '{"tool_name":"Edit","tool_input":{"file_path":"%s"}}' "$repo/src/existing.txt")
out=$(printf '%s' "$payload" | sh "$checker")
[ -z "$out" ] || fail 'compliant added line produced a report'

printf 'forbidden\n' >>"$repo/src/existing.txt"
out=$(printf '%s' "$payload" | sh "$checker")
printf '%s' "$out" | grep -q 'RULEBOOK: src/existing.txt' || fail 'violating edit did not report its path'
printf '%s' "$out" | grep -q 'T-001' || fail 'violating edit did not report its rule id'

printf 'content\n' >"$repo/src/new-example.txt"
payload=$(printf '{"tool_name":"Write","tool_input":{"file_path":"%s"}}' "$repo/src/new-example.txt")
out=$(printf '%s' "$payload" | sh "$checker")
printf '%s' "$out" | grep -q 'T-002' || fail '@new did not report an untracked Write'

plain="$tmp/plain"
mkdir -p "$plain"
git -C "$plain" init -q
printf 'anything\n' >"$plain/file.txt"
payload=$(printf '{"tool_name":"Write","tool_input":{"file_path":"%s"}}' "$plain/file.txt")
out=$(printf '%s' "$payload" | sh "$checker")
[ -z "$out" ] || fail 'an uninitialized repository should be silent'

miswired="$tmp/miswired"
mkdir -p "$miswired/.claude"
git -C "$miswired" init -q
printf '# Rules\n' >"$miswired/.claude/rules.md"
printf 'anything\n' >"$miswired/file.txt"
payload=$(printf '{"tool_name":"Write","tool_input":{"file_path":"%s"}}' "$miswired/file.txt")
out=$(printf '%s' "$payload" | sh "$checker")
printf '%s' "$out" | grep -q 'RULEBOOK CHECKER MISWIRED' || fail 'missing checks.tsv was not reported'

printf 'PASS: rule_check.sh\n'
