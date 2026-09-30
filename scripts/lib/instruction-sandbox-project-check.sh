#!/usr/bin/env bash
# Structural check of the project composed into an instruction-sandbox root.
# Usage: instruction-sandbox-project-check.sh <root>
# Prints `CHECK project:structure PASS|FAIL <detail>`; exit 0 on PASS, 1 on FAIL.
#
# Reads only ISB_PROJECT_MOUNT and ISB_PROTECTED (colon-joined) from
# <root>/sandbox.env, so it knows nothing about any particular project. FAIL when:
#   - <root>/project/.claude is missing or a symlink, or CLAUDE.md is not a regular file
#   - a symlink under <root>/project dangles, resolves outside both the mount and
#     <root>, or resolves under a protected path
#   - a settings*.json under <root>/project/.claude (symlinks followed) names a
#     protected path: a hook wired there by absolute path would run the real
#     project's hooks instead of the candidate's, and could write into it
set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" || $# -ne 1 ]]; then
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
  [[ $# -eq 1 ]] && exit 0 || exit 2
fi

root="$(readlink -f "$1")"
project="$root/project"

fail() { echo "CHECK project:structure FAIL $*"; exit 1; }
inside() { [[ "$1" == "$2" || "$1" == "$2"/* ]]; }
env_value() { sed -n "s/^$1=//p" "$root/sandbox.env" | head -n 1; }

[[ -f "$root/sandbox.env" ]] || fail "no sandbox.env in $root"
mount="$(env_value ISB_PROJECT_MOUNT)"
[[ -n "$mount" ]] || fail "sandbox.env has no ISB_PROJECT_MOUNT"
protected=()
IFS=: read -r -a protected <<< "$(env_value ISB_PROTECTED)"

[[ -d "$project/.claude" && ! -L "$project/.claude" ]] || fail "$project/.claude is not a real directory"
[[ -f "$project/CLAUDE.md" && ! -L "$project/CLAUDE.md" ]] || fail "$project/CLAUDE.md is not a regular file"

while IFS= read -r -d '' link; do
  target="$(readlink -f "$link")"
  [[ -e "$target" ]] || fail "dangling link $link"
  if ! inside "$target" "$mount" && ! inside "$target" "$root"; then
    fail "link $link resolves outside the mount and the sandbox: $target"
  fi
  for p in ${protected[@]+"${protected[@]}"}; do
    [[ -n "$p" ]] || continue
    ! inside "$target" "$p" || fail "link $link resolves under protected $p"
  done
done < <(find "$project" -type l -print0)

while IFS= read -r -d '' settings; do
  for p in ${protected[@]+"${protected[@]}"}; do
    [[ -n "$p" ]] || continue
    escaped="$(printf '%s' "$p" | sed 's/[][\.^$*+?(){}|/]/\\&/g')"
    if grep -Eq -- "(^|[^A-Za-z0-9_./-])${escaped}(\$|[/\"'[:space:]])" "$settings"; then
      fail "$settings names protected path $p"
    fi
  done
done < <(find -L "$project/.claude" -type f -name 'settings*.json' -print0)

echo "CHECK project:structure PASS"
