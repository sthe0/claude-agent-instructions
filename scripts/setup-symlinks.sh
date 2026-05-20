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

link "$REPO/agents" "$HOME/.claude/agents"
link "$REPO/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
link "$REPO/cursor-rules/claude-code-sync.mdc" "$HOME/.cursor/rules/claude-code-sync.mdc"
link "$REPO/memory-meta/INDEX.md" "$HOME/.claude/memory/INDEX.md"
link "$REPO/memory-meta/README.md" "$HOME/.claude/memory/README.md"

echo "Symlinks:"
ls -la "$HOME/.claude/agents" "$HOME/.claude/CLAUDE.md" "$HOME/.cursor/rules/claude-code-sync.mdc" \
  "$HOME/.claude/memory/INDEX.md" "$HOME/.claude/memory/README.md"
