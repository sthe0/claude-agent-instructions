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

link_skill_dir() {
  local dir_path="$1"
  local base
  base="$(basename "$dir_path")"
  link "$dir_path" "$HOME/.claude/skills/$base"
}

prune_dangling() {
  local dir="$1"
  find "$dir" -maxdepth 1 -type l ! -exec test -e {} \; -delete 2>/dev/null || true
}

# Core global symlinks
link "$REPO/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
link "$REPO/cursor-rules/claude-code-sync.mdc" "$HOME/.cursor/rules/claude-code-sync.mdc"
link "$REPO/memory-global" "$HOME/.claude/memory-global"

# Local arc gates / scripts (optional, present only on Arcadia machines)
if [[ -f "$JUNK_AGENTS_ROOT/cursor-rules/org-yandex.mdc" ]]; then
  link "$JUNK_AGENTS_ROOT/cursor-rules/org-yandex.mdc" "$HOME/.cursor/rules/org-yandex.mdc"
fi

if [[ -d "$JUNK_AGENTS_ROOT/scripts" ]]; then
  link "$JUNK_AGENTS_ROOT/scripts" "$HOME/.claude/scripts-local"
  chmod +x "$JUNK_AGENTS_ROOT/scripts"/*.sh 2>/dev/null || true
  "$JUNK_AGENTS_ROOT/scripts/install-junk-agents-sync-cron.sh" 2>/dev/null || true
fi

# Agents: ~/.claude/agents/ as a regular directory with per-file symlinks
if [[ -L "$HOME/.claude/agents" ]]; then
  rm "$HOME/.claude/agents"
fi
mkdir -p "$HOME/.claude/agents"
prune_dangling "$HOME/.claude/agents"

for file_path in "$REPO/agents/"*.md; do
  [[ -f "$file_path" ]] && link_agent_md "$file_path"
done

if [[ -d "$JUNK_AGENTS_ROOT/agents-local" ]]; then
  for file_path in "$JUNK_AGENTS_ROOT/agents-local/"*.md; do
    [[ -f "$file_path" ]] && link_agent_md "$file_path"
  done
fi

if [[ -d "$REPO/agents-local" ]]; then
  for file_path in "$REPO/agents-local/"*.md; do
    [[ -f "$file_path" ]] && link_agent_md "$file_path"
  done
fi

# Skills: ~/.claude/skills/ as a regular directory with per-skill directory symlinks
if [[ -L "$HOME/.claude/skills" ]]; then
  rm "$HOME/.claude/skills"
fi
mkdir -p "$HOME/.claude/skills"
prune_dangling "$HOME/.claude/skills"

if [[ -d "$REPO/skills" ]]; then
  for dir_path in "$REPO/skills/"*/; do
    [[ -d "$dir_path" ]] || continue
    dir_path="${dir_path%/}"
    link_skill_dir "$dir_path"
  done
fi

# Machine-local skills (gitignored single-file skills)
if [[ -d "$REPO/skills-local" ]]; then
  for file_path in "$REPO/skills-local/"*.md; do
    [[ -f "$file_path" ]] || continue
    base="$(basename "$file_path")"
    [[ "$base" == "README.md" ]] && continue
    link "$file_path" "$HOME/.claude/skills/$base"
  done
fi

link "$HOME/.claude/agents" "$HOME/.cursor/agents"

"$REPO/scripts/install-git-hooks.sh"
"$REPO/scripts/install-sync-cron.sh" 2>/dev/null || true

chmod +x "$REPO/scripts/verify-instructions-sync.sh" "$REPO/scripts/verify-layout-contract.sh" "$REPO/scripts/setup-project-memory.sh"
"$REPO/scripts/verify-layout-contract.sh" 2>/dev/null || true
"$REPO/scripts/verify-instructions-sync.sh" || true

echo "Symlinks:"
ls -la "$HOME/.claude/memory-global" "$HOME/.claude/skills" "$HOME/.claude/agents" 2>/dev/null || true
