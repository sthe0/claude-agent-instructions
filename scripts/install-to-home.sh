#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

install -m 0644 "$REPO/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
install -m 0644 "$REPO/cursor-rules/claude-code-sync.mdc" "$HOME/.cursor/rules/claude-code-sync.mdc"
mkdir -p "$HOME/.claude/agents"
for agent in "$REPO/agents"/*.md; do
  install -m 0644 "$agent" "$HOME/.claude/agents/$(basename "$agent")"
done

echo "Installed: CLAUDE.md, cursor-rules, $(ls "$REPO/agents"/*.md | wc -l) agents -> ~/.claude/agents/"
