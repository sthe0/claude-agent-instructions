#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
THE0_AGENTS_MOUNT="${THE0_AGENTS_MOUNT:-$HOME/arcadia_the0-agents}"
JUNK_AGENTS_ROOT="${JUNK_AGENTS_ROOT:-$THE0_AGENTS_MOUNT/junk/the0/agents}"

mkdir -p "$HOME/.claude" "$HOME/.cursor/rules"

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

# Global memory (git repo)
link "$REPO/memory-global" "$HOME/.claude/memory-global"

# Local memory + agents (Arcadia junk on this machine)
if [[ ! -d "$JUNK_AGENTS_ROOT/memory-local" ]]; then
  echo "WARN: missing $JUNK_AGENTS_ROOT/memory-local — run scripts/setup-the0-agents-mount.sh" >&2
else
  link "$JUNK_AGENTS_ROOT/memory-local" "$HOME/.claude/memory"
fi

if [[ -L "$HOME/.claude/agents" ]]; then
  rm "$HOME/.claude/agents"
fi
mkdir -p "$HOME/.claude/agents"

for file_path in "$REPO/agents/"*.md; do
  [[ -f "$file_path" ]] && link_agent_md "$file_path"
done

if [[ -d "$JUNK_AGENTS_ROOT/agents-local" ]]; then
  for file_path in "$JUNK_AGENTS_ROOT/agents-local/"*.md; do
    [[ -f "$file_path" ]] && link_agent_md "$file_path"
  done
else
  echo "WARN: missing $JUNK_AGENTS_ROOT/agents-local" >&2
  if [[ -d "$REPO/agents-local" ]]; then
    for file_path in "$REPO/agents-local/"*.md; do
      [[ -f "$file_path" ]] && link_agent_md "$file_path"
    done
  fi
fi

link "$HOME/.claude/agents" "$HOME/.cursor/agents"

"$REPO/scripts/install-git-hooks.sh"
"$REPO/scripts/install-sync-cron.sh" 2>/dev/null || true
"$REPO/scripts/install-junk-agents-sync-cron.sh" 2>/dev/null || true
chmod +x "$REPO/scripts/sync-junk-agents-arc.sh" \
  "$REPO/scripts/junk-agents-arc-commit.sh" \
  "$REPO/scripts/setup-the0-agents-mount.sh" 2>/dev/null || true

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
  "$HOME/.claude/memory-global" "$HOME/.claude/memory" 2>/dev/null || true
