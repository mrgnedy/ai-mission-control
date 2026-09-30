#!/bin/sh
set -eu

usage() {
  printf 'Usage: %s plan|apply [project-directory] [plan-id]\n' "$0" >&2
  exit 2
}

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

[ "$#" -ge 1 ] || usage
action=$1
requested=${2:-${CLAUDE_PROJECT_DIR:-$PWD}}
expected_plan=${3:-}

[ -d "$requested" ] || fail "project directory does not exist: $requested"
git_root=$(git -C "$requested" rev-parse --show-toplevel 2>/dev/null) || \
  fail "learning-loop setup requires a git repository"
root=$(cd "$git_root" && pwd -P) || fail "cannot resolve repository root"

script_dir=$(cd "$(dirname "$0")" && pwd -P) || fail "cannot resolve plugin scripts directory"
plugin_root=$(dirname "$script_dir")
assets="$plugin_root/skills/lesson/assets"
rules_template="$assets/rules.template.md"
checks_template="$assets/checks.template.tsv"
[ -f "$rules_template" ] || fail "missing bundled template: $rules_template"
[ -f "$checks_template" ] || fail "missing bundled template: $checks_template"

claude_file="$root/CLAUDE.md"
rule_dir="$root/.claude"
rules_file="$rule_dir/rules.md"
checks_file="$rule_dir/checks.tsv"

[ ! -L "$rule_dir" ] || fail ".claude is a symbolic link; use the manual wiring guide"
for target in "$claude_file" "$rules_file" "$checks_file"; do
  [ ! -L "$target" ] || fail "${target#"$root"/} is a symbolic link; use the manual wiring guide"
done

has_rulebook_import() {
  [ -f "$claude_file" ] && grep -Fqx '@.claude/rules.md' "$claude_file"
}

print_claude_block() {
  printf '%s\n' \
    '## Shared rulebook' \
    '' \
    '@.claude/rules.md' \
    '' \
    'When you discover a durable, non-obvious project insight that would help future agents avoid' \
    'mistakes or repeated investigation, consider invoking the `learning-loop:lesson` skill and' \
    'propose capturing it.'
}

state_fingerprint() {
  {
    for target in "$claude_file" "$rules_file" "$checks_file"; do
      rel=${target#"$root"/}
      if [ -f "$target" ]; then
        printf 'FILE %s ' "$rel"
        cksum <"$target"
      else
        printf 'MISSING %s\n' "$rel"
      fi
    done
  } | cksum | awk '{ print $1 "-" $2 }'
}

print_creation() {
  rel=$1
  source=$2
  printf '\nCREATE: %s\n' "$rel"
  printf '%s\n' '----- exact content -----'
  cat "$source"
  printf '%s\n' '----- end content -----'
}

print_plan() {
  plan_id=$(state_fingerprint)
  printf 'LEARNING LOOP SETUP PLAN\n'
  printf 'PROJECT: %s\n' "$root"
  printf 'PLAN_ID: %s\n' "$plan_id"

  if [ -f "$rules_file" ]; then
    printf '\nNO CHANGE: .claude/rules.md already exists and will be preserved.\n'
  else
    print_creation '.claude/rules.md' "$rules_template"
  fi

  if [ -f "$checks_file" ]; then
    printf '\nNO CHANGE: .claude/checks.tsv already exists and will be preserved.\n'
  else
    print_creation '.claude/checks.tsv' "$checks_template"
  fi

  if has_rulebook_import; then
    printf '\nNO CHANGE: CLAUDE.md already imports @.claude/rules.md.\n'
  else
    if [ -f "$claude_file" ]; then
      printf '\nPREPEND: CLAUDE.md\n'
    else
      printf '\nCREATE: CLAUDE.md\n'
    fi
    printf '%s\n' '----- exact block -----'
    print_claude_block
    printf '%s\n' '----- end block -----'
  fi

  printf '\nOPTIONAL: .claude/rulecheck.config is not created automatically. See wiring.md for cross-module checks.\n'
  printf 'No files were changed. Apply only after the user approves this exact plan.\n'
}

prepend_claude_block() {
  tmp=$(mktemp "$root/.CLAUDE.md.learning-loop.XXXXXX") || fail "cannot create temporary file"
  trap 'rm -f "$tmp"' EXIT HUP INT TERM
  print_claude_block >"$tmp"
  if [ -s "$claude_file" ]; then
    printf '\n\n' >>"$tmp"
    cat "$claude_file" >>"$tmp"
  fi
  if [ -f "$claude_file" ]; then
    mode=$(stat -f '%Lp' "$claude_file" 2>/dev/null || stat -c '%a' "$claude_file" 2>/dev/null || printf '')
    [ -z "$mode" ] || chmod "$mode" "$tmp"
  fi
  mv "$tmp" "$claude_file"
  trap - EXIT HUP INT TERM
}

apply_plan() {
  [ -n "$expected_plan" ] || fail "apply requires the PLAN_ID printed by a prior plan"
  current_plan=$(state_fingerprint)
  [ "$current_plan" = "$expected_plan" ] || \
    fail "repository setup files changed after preview; run plan again and request fresh approval"

  changed=0
  if [ ! -f "$rules_file" ] || [ ! -f "$checks_file" ]; then
    mkdir -p "$rule_dir"
  fi
  if [ ! -f "$rules_file" ]; then
    cp "$rules_template" "$rules_file"
    printf 'CREATED: .claude/rules.md\n'
    changed=1
  else
    printf 'PRESERVED: .claude/rules.md\n'
  fi
  if [ ! -f "$checks_file" ]; then
    cp "$checks_template" "$checks_file"
    printf 'CREATED: .claude/checks.tsv\n'
    changed=1
  else
    printf 'PRESERVED: .claude/checks.tsv\n'
  fi
  if has_rulebook_import; then
    printf 'PRESERVED: CLAUDE.md already imports @.claude/rules.md\n'
  else
    prepend_claude_block
    printf 'UPDATED: CLAUDE.md with the approved block at the top\n'
    changed=1
  fi
  if [ "$changed" = 1 ]; then
    printf 'SETUP COMPLETE\n'
  else
    printf 'SETUP ALREADY COMPLETE\n'
  fi
}

case "$action" in
  plan) print_plan ;;
  apply) apply_plan ;;
  *) usage ;;
esac
