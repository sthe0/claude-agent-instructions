#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$HOME/.claude" "$HOME/.claude/memory" "$HOME/.cursor/rules" "$HOME/.cursor/agents"

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
link "$HOME/.claude/agents" "$HOME/.cursor/agents"
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

# deepagent: project overlay only (не полная копия claude-code-sync.mdc)
DEEPAGENT_RULES="$HOME/arcadia/robot/deepagent/.cursor/rules"
if [[ -d "$DEEPAGENT_RULES" ]]; then
  mkdir -p "$DEEPAGENT_RULES"
  if [[ -f "$DEEPAGENT_RULES/claude-code-sync.mdc" && ! -L "$DEEPAGENT_RULES/claude-code-sync.mdc" ]]; then
    mv "$DEEPAGENT_RULES/claude-code-sync.mdc" \
      "$DEEPAGENT_RULES/claude-code-sync.mdc.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
  fi
  rm -f "$DEEPAGENT_RULES/claude-code-sync.mdc"
  link "$REPO/cursor-rules/project-overlay-deepagent.mdc" "$DEEPAGENT_RULES/deepagent-project.mdc"
  if [[ ! -L "$HOME/arcadia/robot/deepagent/CLAUDE.md" ]]; then
    link "$REPO/CLAUDE.md" "$HOME/arcadia/robot/deepagent/CLAUDE.md"
  fi
fi

chmod +x "$REPO/scripts/verify-instructions-sync.sh"
"$REPO/scripts/verify-instructions-sync.sh" || true

echo "Symlinks:"
ls -la "$HOME/.claude/agents" "$HOME/.claude/CLAUDE.md" "$HOME/.cursor/agents" \
  "$HOME/.cursor/rules/claude-code-sync.mdc" \
  "$HOME/.claude/memory/INDEX.md" "$HOME/.claude/memory/README.md"
