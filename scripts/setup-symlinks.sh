#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEEPAGENT_ROOT="${DEEPAGENT_ROOT:-$HOME/arcadia/robot/deepagent}"

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

# Do not install org-yandex globally — it lives in robot/deepagent/.claude/rules/
rm -f "$HOME/.cursor/rules/org-yandex.mdc"
rm -rf "$HOME/.claude/scripts-local" 2>/dev/null || true

# Agents: global only in ~/.claude/agents/
if [[ -L "$HOME/.claude/agents" ]]; then
  rm "$HOME/.claude/agents"
fi
mkdir -p "$HOME/.claude/agents"
prune_dangling "$HOME/.claude/agents"

for file_path in "$REPO/agents/"*.md; do
  [[ -f "$file_path" ]] && link_agent_md "$file_path"
done

if [[ -d "$REPO/agents-local" ]]; then
  for file_path in "$REPO/agents-local/"*.md; do
    [[ -f "$file_path" ]] && link_agent_md "$file_path"
  done
fi

# Skills: global instruction skills only in ~/.claude/skills/
if [[ -L "$HOME/.claude/skills" ]]; then
  rm "$HOME/.claude/skills"
fi
mkdir -p "$HOME/.claude/skills"
prune_dangling "$HOME/.claude/skills"

# Remove legacy home symlinks to Arcadia artifacts or the0-agents
for entry in "$HOME/.claude/skills/"*; do
  [[ -e "$entry" ]] || continue
  base="$(basename "$entry")"
  [[ "$base" == "overcome-difficulty" || "$base" == "self-improvement" || "$base" == "README.md" ]] && continue
  if [[ -L "$entry" ]]; then
    target="$(readlink "$entry")"
    if [[ "$target" == *"/ai/artifacts/skills"* ]] || [[ "$target" == *"arcadia_the0-agents"* ]]; then
      rm -f "$entry"
    fi
  elif [[ -d "$entry" ]]; then
    rm -rf "$entry"
  fi
done

if [[ -d "$REPO/skills" ]]; then
  for dir_path in "$REPO/skills/"*/; do
    [[ -d "$dir_path" ]] || continue
    dir_path="${dir_path%/}"
    link_skill_dir "$dir_path"
  done
fi

if [[ -d "$REPO/skills-local" ]]; then
  for file_path in "$REPO/skills-local/"*.md; do
    [[ -f "$file_path" ]] || continue
    base="$(basename "$file_path")"
    [[ "$base" == "README.md" ]] && continue
    link "$file_path" "$HOME/.claude/skills/$base"
  done
fi

# Drop legacy per-agent symlinks to the0-agents / logos / deepagent project agents
for entry in "$HOME/.claude/agents/"*.md; do
  [[ -L "$entry" ]] || continue
  target="$(readlink "$entry")"
  if [[ "$target" == *"arcadia_the0-agents"* ]] || [[ "$target" == *"/logos/"* ]] || [[ "$target" == *"/robot/deepagent/.claude/agents"* ]]; then
    rm -f "$entry"
  fi
done

link "$HOME/.claude/agents" "$HOME/.cursor/agents"

"$REPO/scripts/install-git-hooks.sh"

# deepagent: project-local Cursor rules and CLAUDE.md (canonical in Arc)
if [[ -d "$DEEPAGENT_ROOT/.claude/rules" ]]; then
  mkdir -p "$DEEPAGENT_ROOT/.cursor/rules"
  rm -f "$DEEPAGENT_ROOT/.cursor/rules/claude-code-sync.mdc"
  link "$DEEPAGENT_ROOT/.claude/rules/project.mdc" "$DEEPAGENT_ROOT/.cursor/rules/deepagent-project.mdc"
  link "$DEEPAGENT_ROOT/.claude/rules/org-yandex.mdc" "$DEEPAGENT_ROOT/.cursor/rules/org-yandex.mdc"
  link "$DEEPAGENT_ROOT/.claude/CLAUDE.md" "$DEEPAGENT_ROOT/CLAUDE.md"
  if [[ -x "$DEEPAGENT_ROOT/.claude/scripts/link-skills.sh" ]]; then
    "$DEEPAGENT_ROOT/.claude/scripts/link-skills.sh" >/dev/null 2>&1 || true
  fi
fi

chmod +x "$REPO/scripts/verify-instructions-sync.sh" "$REPO/scripts/verify-layout-contract.sh" "$REPO/scripts/setup-project-memory.sh"
"$REPO/scripts/verify-layout-contract.sh" 2>/dev/null || true
"$REPO/scripts/verify-instructions-sync.sh" || true

echo "Symlinks:"
ls -la "$HOME/.claude/memory-global" "$HOME/.claude/skills" "$HOME/.claude/agents" 2>/dev/null || true
