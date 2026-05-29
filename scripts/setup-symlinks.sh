#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$HOME/.claude" "$HOME/.cursor"

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
  local logfile="$HOME/.local/log/setup-symlinks-prune.log"
  mkdir -p "$(dirname "$logfile")"
  local stale now
  while IFS= read -r stale; do
    [[ -z "$stale" ]] && continue
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "$now prune-dangling: $stale" >&2
    echo "$now prune-dangling: $stale" >> "$logfile"
    rm "$stale"
  done < <(find "$dir" -maxdepth 1 -type l ! -exec test -e {} \; -print 2>/dev/null || true)
}

# Core global symlinks
link "$REPO/CLAUDE.md" "$HOME/.claude/CLAUDE.md"
link "$REPO/config.md" "$HOME/.claude/config.md"
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
    base="$(basename "$dir_path")"
    # skip the specializations container itself; iterate its contents below
    [[ "$base" == "specializations" ]] && continue
    link_skill_dir "$dir_path"
  done
fi

# Specializations live in skills/specializations/ but are symlinked flat into
# ~/.claude/skills/<name>/ so the Claude Code skill catalog sees them by name.
if [[ -d "$REPO/skills/specializations" ]]; then
  for dir_path in "$REPO/skills/specializations/"*/; do
    [[ -d "$dir_path" ]] || continue
    dir_path="${dir_path%/}"
    link_skill_dir "$dir_path"
  done
fi

if [[ -d "$REPO/skills-local" ]]; then
  for entry in "$REPO/skills-local/"*; do
    [[ -e "$entry" ]] || continue
    base="$(basename "$entry")"
    [[ "$base" == "README.md" ]] && continue
    if [[ -f "$entry" && "$entry" == *.md ]]; then
      # Single-file skill — link the .md directly.
      link "$entry" "$HOME/.claude/skills/$base"
    elif [[ -d "$entry" && -f "$entry/SKILL.md" ]]; then
      # Multi-file skill — link the whole directory (mirrors how skills/ are linked).
      link "$entry" "$HOME/.claude/skills/$base"
    fi
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

chmod +x "$REPO/scripts/verify-instructions-sync.sh" "$REPO/scripts/verify-layout-contract.sh" "$REPO/scripts/setup-project-memory.sh" "$REPO/scripts/apply-settings.sh" "$REPO/cursor/scripts/install-cursor-links.sh" "$REPO/cursor/scripts/link-project-cursor-agents.sh" "$REPO/cursor/scripts/migrate-cursor-namespace.sh"

"$REPO/cursor/scripts/install-cursor-links.sh"

# Merge versioned policy permissions (read-only allowlist + env) into the
# machine-local ~/.claude/settings.json; machine-specific keys are preserved.
"$REPO/scripts/apply-settings.sh"

"$REPO/scripts/install-git-hooks.sh"
"$REPO/scripts/verify-layout-contract.sh" 2>/dev/null || true
"$REPO/scripts/verify-instructions-sync.sh" || true

echo "Global symlinks ok. Per-project setup (run from each repo root):"
echo "  robot/deepagent  →  .claude/scripts/setup-local.sh"
echo "  logos            →  .claude/scripts/setup-local.sh"
ls -la "$HOME/.claude/memory-global" "$HOME/.claude/skills" "$HOME/.claude/agents" 2>/dev/null || true
