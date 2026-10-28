#!/bin/sh
#
# Advisory tier-2 rulebook checker for the learning-loop Claude Code plugin.
# Repository knowledge remains in <project>/.claude/{rules.md,checks.tsv}.
# The plugin owns this mechanism; consuming repositories must not copy it.
#
# Only lines added against HEAD are inspected. An untracked file is inspected
# in full, and @new fires only when the hook reports a Write operation.
#
set -u
set -f

input=$(cat)
field() {
  printf '%s' "$input" | sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" | head -n 1
}
f=$(field file_path)
tool=$(field tool_name)
[ -n "$f" ] && [ -f "$f" ] || exit 0

dir=$(cd "$(dirname "$f")" && pwd -P) || exit 0
root=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) || exit 0
root=$(cd "$root" && pwd -P) || exit 0
abs="$dir/$(basename "$f")"
case "$abs" in "$root"/*) ;; *) exit 0 ;; esac
rel=${abs#"$root"/}

if [ -n "${LESSON_RULEBOOK_DIR:-}" ]; then
  case "$LESSON_RULEBOOK_DIR" in
    /*) here=$LESSON_RULEBOOK_DIR ;;
    *) here="$root/$LESSON_RULEBOOK_DIR" ;;
  esac
else
  here="$root/.claude"
fi
checks="$here/checks.tsv"
rules="$here/rules.md"

# The plugin may be enabled globally, so an uninitialized repository is normal.
[ -f "$checks" ] || {
  [ -f "$rules" ] || exit 0
  printf '%s' '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"RULEBOOK CHECKER MISWIRED: rules.md exists but checks.tsv is missing beside it, so no tier-2 rule was checked. Restore the repository checks file or remove the stale rulebook wiring."}}'
  exit 0
}

MODULES_DIR=''
IMPORT_PREFIX=''
SHARED_MODULES=''
ALLOWED_IMPORT_REGEX=''
[ -f "$here/rulecheck.config" ] && . "$here/rulecheck.config"

new=0
if git -C "$root" ls-files --error-unmatch -- "$rel" >/dev/null 2>&1; then
  added=$(git -C "$root" diff -U0 HEAD -- "$rel" | sed -n -e '/^+++ /d' -e 's/^+//p')
else
  added=$(cat "$abs")
  [ "$tool" = Write ] && new=1
fi

cross_module() {
  [ -n "$MODULES_DIR" ] && [ -n "$IMPORT_PREFIX" ] || return 0
  module=$(printf '%s' "$rel" | sed -n "s|^$MODULES_DIR/\([^/]*\)/.*|\1|p")
  [ -n "$module" ] || return 0
  allowed="$module"
  if [ -n "$SHARED_MODULES" ]; then
    allowed="$module|$(printf '%s' "$SHARED_MODULES" | tr ',' '|')"
  fi
  matches=$(printf '%s\n' "$added" \
    | awk -v p="$IMPORT_PREFIX" '{ s = $0; sub(/^[ \t]+/, "", s); if (index(s, p) == 1) print }' \
    | grep -vE -- "$IMPORT_PREFIX($allowed)/")
  [ -n "$ALLOWED_IMPORT_REGEX" ] && matches=$(printf '%s\n' "$matches" | grep -vE -- "$ALLOWED_IMPORT_REGEX")
  printf '%s' "$matches" | grep -v '^$' | head -n 1
}

clean() { printf '%s' "$1" | tr -d '"\\' | tr '\t' ' ' | sed 's/^ *//' | cut -c1-90; }

TAB=$(printf '\t')
IFS_DEFAULT=$IFS
hits=''
while IFS="$TAB" read -r id globs re msg || [ -n "${id:-}" ]; do
  case "${id:-}" in '' | \#*) continue ;; esac
  [ -n "${re:-}" ] || continue

  in_scope=0
  IFS=,
  for glob in $globs; do
    case "$rel" in $glob) in_scope=1 ;; esac
  done
  IFS=$IFS_DEFAULT
  [ "$in_scope" = 1 ] || continue

  case "$re" in
    @new)
      [ "$new" = 1 ] || continue
      where='new file'
      ;;
    @cross-module | @cross-feature)
      sample=$(cross_module)
      [ -n "$sample" ] || continue
      where="added: $(clean "$sample")"
      ;;
    @file:*)
      grep -qE -- "${re#@file:}" "$abs" || continue
      where='in this file'
      ;;
    *)
      sample=$(printf '%s\n' "$added" | grep -E -m 1 -- "$re") || continue
      n=$(printf '%s\n' "$added" | grep -cE -- "$re")
      where="$n added line(s), e.g. $(clean "$sample")"
      ;;
  esac
  hits="${hits}\\n  - ${id} (${where}): $(printf '%s' "$msg" | tr -d '"\\')"
done <"$checks"

[ -n "$hits" ] || exit 0
here_rel=${here#"$root"/}
rulebook='the rulebook'
[ -f "$rules" ] && rulebook="$here_rel/rules.md"
out="RULEBOOK: ${rel}${hits}\\n\\nFix what this change added. Legacy lines elsewhere in the file are out of scope unless the task says otherwise. If a rule is wrong or too broad, say so and run the lesson skill to amend it. Full text: ${rulebook}"
printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"%s"}}' "$out"
exit 0
