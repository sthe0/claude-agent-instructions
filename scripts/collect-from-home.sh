#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

cp "$HOME/.claude/CLAUDE.md" "$REPO/CLAUDE.md"
cp "$HOME/.cursor/rules/claude-code-sync.mdc" "$REPO/cursor-rules/claude-code-sync.mdc"
mkdir -p "$REPO/agents"
cp -a "$HOME/.claude/agents/"*.md "$REPO/agents/"
cp "$HOME/.claude/memory/README.md" "$HOME/.claude/memory/INDEX.md" "$REPO/memory-meta/"
ls -la "$HOME/.claude/skills" > "$REPO/docs/skills-symlinks.txt" 2>/dev/null || true

echo "Collected from ~/.claude and ~/.cursor/rules"
