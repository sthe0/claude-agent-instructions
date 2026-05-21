#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$HOME/.claude" "$HOME/.claude/memory" "$HOME/.cursor/rules"

link() {
  local target="$1" linkpath="$2"
  if [[ -e "$linkpath" && ! -L "$linkpath" ]]; then
    echo "refuse: $linkpath exists and is not a symlink (move aside manually)" >&2
    exit 1
  fi
  ln -sfn "$target" "$linkpath"
}

link_agent_md() {
  local file_path="$1"
  local base
  base="$(basename "$file_path")"
  [[ "$base" == "README.md" ]] && return 0
  link "$file_path" "$HOME/.claude/agents/$base"
}

link "$REPO/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
link "$REPO/cursor-rules/claude-code-sync.mdc" "$HOME/.cursor/rules/claude-code-sync.mdc"
link "$REPO/memory-meta/INDEX.md" "$HOME/.claude/memory/INDEX.md"
link "$REPO/memory-meta/README.md" "$HOME/.claude/memory/README.md"

if [[ -L "$HOME/.claude/agents" ]]; then
  rm "$HOME/.claude/agents"
fi
mkdir -p "$HOME/.claude/agents"

for file_path in "$REPO/agents/"*.md; do
  [[ -f "$file_path" ]] && link_agent_md "$file_path"
done

if [[ -d "$REPO/agents-local" ]]; then
  for file_path in "$REPO/agents-local/"*.md; do
    [[ -f "$file_path" ]] && link_agent_md "$file_path"
  done
fi

"$REPO/scripts/install-git-hooks.sh"
"$REPO/scripts/install-sync-cron.sh" 2>/dev/null || true

echo "Symlinks:"
ls -la "$HOME/.claude/agents/" "$HOME/.claude/CLAUDE.md" "$HOME/.cursor/rules/claude-code-sync.mdc" \
  "$HOME/.claude/memory/INDEX.md" "$HOME/.claude/memory/README.md"
