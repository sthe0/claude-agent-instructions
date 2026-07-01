#!/usr/bin/env bash
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)}"
source "$REPO/scripts/lib/config-root.sh"
_realpath() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }

link() {
  local target="$1" linkpath="$2"
  if [[ -e "$linkpath" && ! -L "$linkpath" ]]; then
    echo "refuse: $linkpath exists and is not a symlink (move aside manually)" >&2
    exit 1
  fi
  ln -sfn "$target" "$linkpath"
}

prune_dangling() {
  local dir="$1"
  while IFS= read -r stale; do
    [[ -z "$stale" ]] && continue
    rm "$stale"
  done < <(find "$dir" -maxdepth 1 -type l ! -exec test -e {} \; -print 2>/dev/null || true)
}

link_cursor_agent_md() {
  local file_path="$1"
  local base
  base="$(basename "$file_path")"
  [[ "$base" == "README.md" ]] && return 0
  link "$file_path" "$HOME/.cursor/agents/$base"
}

mkdir -p "$HOME/.cursor/rules"

# Migrate away from legacy ~/.cursor/agents -> ~/.claude/agents symlink.
if [[ -L "$HOME/.cursor/agents" ]]; then
  legacy_target="$(_realpath "$HOME/.cursor/agents" 2>/dev/null || true)"
  claude_agents_target="$(_realpath "$CLAUDE_AGENT_HOME/agents" 2>/dev/null || true)"
  if [[ -n "$legacy_target" && -n "$claude_agents_target" && "$legacy_target" == "$claude_agents_target" ]]; then
    rm -f "$HOME/.cursor/agents"
  else
    echo "refuse: ~/.cursor/agents is a symlink to an unexpected target ($legacy_target)" >&2
    echo "move it aside manually, then rerun setup" >&2
    exit 1
  fi
fi
mkdir -p "$HOME/.cursor/agents"

prune_dangling "$HOME/.cursor/rules"
prune_dangling "$HOME/.cursor/agents"

link "$REPO/cursor/rules/claude-code-sync.mdc" "$HOME/.cursor/rules/claude-code-sync.mdc"

for file_path in "$REPO/cursor/agents/"*.md; do
  [[ -f "$file_path" ]] && link_cursor_agent_md "$file_path"
done

echo "Cursor links updated."
