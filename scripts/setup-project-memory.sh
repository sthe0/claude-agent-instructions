#!/usr/bin/env bash
# Symlinks the per-cwd native auto-memory directory under ~/.claude/projects/
# to <project_cwd>/.claude/agent-memory/, so Claude Code reads and writes
# project memory through the native mechanism but the actual files live
# inside the project tree (versionable, shareable with other developers).
#
# Usage:
#   setup-project-memory.sh [project_cwd]
#
# Default project_cwd is $PWD. The script:
#   1. Creates <project_cwd>/.claude/agent-memory/ if missing (with a stub MEMORY.md).
#   2. Computes the Claude Code per-cwd hash directory name: leading "-" + cwd with "/" → "-".
#   3. Removes any existing ~/.claude/projects/<hash>/memory (file, dir, or symlink).
#   4. Replaces it with a symlink → <project_cwd>/.claude/agent-memory.
#
# Idempotent. Safe to rerun. Existing project memory files are preserved.

set -euo pipefail

PROJECT_CWD="${1:-$PWD}"
PROJECT_CWD="$(cd "$PROJECT_CWD" && pwd)"

if [[ "$PROJECT_CWD" == "$HOME" ]]; then
  echo "refuse: $HOME is not a project — native auto-memory at ~/.claude/projects/-Users-* stays as-is" >&2
  exit 1
fi

AGENT_MEMORY="$PROJECT_CWD/.claude/agent-memory"
mkdir -p "$AGENT_MEMORY" "$AGENT_MEMORY/experience" "$AGENT_MEMORY/system-knowledge"

if [[ ! -f "$AGENT_MEMORY/MEMORY.md" ]]; then
  cat >"$AGENT_MEMORY/MEMORY.md" <<EOF
# Project memory

Index of memories specific to this project. Versioned with the project (commit \`.claude/agent-memory/\` to the project's git).

Loaded into every Claude Code session in this project via the native auto-memory mechanism (symlinked from \`~/.claude/projects/<cwd-hash>/memory/\`).

## Entries

<!-- Add one-line pointers to leaf files in this directory as memories accumulate. -->
EOF
fi

# Claude Code per-cwd hash: absolute cwd with each "/" replaced by "-".
# Leading "/" already produces the leading "-", so no extra prefix.
# /home/x → -home-x  (NOT --home-x).
HASH="${PROJECT_CWD//\//-}"
PROJECTS_DIR="$HOME/.claude/projects/$HASH"
mkdir -p "$PROJECTS_DIR"

TARGET="$PROJECTS_DIR/memory"

if [[ -L "$TARGET" ]]; then
  current="$(readlink "$TARGET")"
  if [[ "$current" == "$AGENT_MEMORY" ]]; then
    echo "ok: $TARGET → $AGENT_MEMORY (already linked)"
    exit 0
  fi
  rm "$TARGET"
elif [[ -d "$TARGET" ]]; then
  if [[ -z "$(ls -A "$TARGET" 2>/dev/null)" ]]; then
    rmdir "$TARGET"
  else
    BACKUP="$TARGET.bak.$(date +%Y%m%d%H%M%S)"
    echo "warn: $TARGET is a non-empty directory — moving to $BACKUP" >&2
    mv "$TARGET" "$BACKUP"
  fi
elif [[ -e "$TARGET" ]]; then
  echo "refuse: $TARGET exists and is not a directory or symlink" >&2
  exit 1
fi

ln -s "$AGENT_MEMORY" "$TARGET"
echo "ok: $TARGET → $AGENT_MEMORY"

if [[ ! -f "$PROJECT_CWD/.gitignore" ]] || ! grep -qx ".claude/" "$PROJECT_CWD/.gitignore" 2>/dev/null; then
  echo
  echo "hint: commit .claude/agent-memory/ to the project's git so other developers inherit it."
fi
