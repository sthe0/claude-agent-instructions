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
#   1. Creates <project_cwd>/.claude/agent-memory/ (stub MEMORY.md only when there
#      is no native content to adopt — see step 3).
#   2. Computes the Claude Code per-cwd hash directory name: every non-alphanumeric char in the absolute cwd → "-" (matches the harness; "/" and "_" both map to "-").
#   3. If the native ~/.claude/projects/<hash>/memory is a populated real dir and the
#      in-tree agent-memory is empty, MIGRATES the native content into agent-memory
#      (keeping a .premigrate.bak backup) instead of orphaning it; refuses if BOTH
#      sides already hold content.
#   4. Replaces the native path with a symlink → <project_cwd>/.claude/agent-memory.
#
# Idempotent. Safe to rerun. Never orphans accumulated memory.

set -euo pipefail

PROJECT_CWD="${1:-$PWD}"
PROJECT_CWD="$(cd "$PROJECT_CWD" && pwd)"

if [[ "$PROJECT_CWD" == "$HOME" ]]; then
  echo "refuse: $HOME is not a project — native auto-memory at ~/.claude/projects/-Users-* stays as-is" >&2
  exit 1
fi

AGENT_MEMORY="$PROJECT_CWD/.claude/agent-memory"
# Whether the in-tree memory already held real content before we touched it —
# decides stub-vs-migrate when the native auto-memory is also populated.
TREE_HAD_CONTENT=0
[[ -f "$AGENT_MEMORY/MEMORY.md" ]] && TREE_HAD_CONTENT=1
mkdir -p "$AGENT_MEMORY" "$AGENT_MEMORY/experience" "$AGENT_MEMORY/system-knowledge"

# Claude Code per-cwd hash: every non-alphanumeric char in the absolute cwd → "-"
# (the harness sanitizes "/" AND "_" — and any other non-alnum — to "-"). The
# leading "/" already produces the leading "-", so no extra prefix.
# /home/x → -home-x ; /home/arcadia_X → -home-arcadia-X  (underscore → dash too).
HASH="$(printf '%s' "$PROJECT_CWD" | sed 's/[^A-Za-z0-9]/-/g')"
PROJECTS_DIR="$HOME/.claude/projects/$HASH"
mkdir -p "$PROJECTS_DIR"

TARGET="$PROJECTS_DIR/memory"

if [[ -L "$TARGET" && "$(readlink "$TARGET")" == "$AGENT_MEMORY" ]]; then
  echo "ok: $TARGET → $AGENT_MEMORY (already linked)"
  exit 0
fi

# Is the native auto-memory a populated real directory we must preserve?
NATIVE_POPULATED=0
[[ -d "$TARGET" && ! -L "$TARGET" && -n "$(ls -A "$TARGET" 2>/dev/null)" ]] && NATIVE_POPULATED=1

# Both sides hold content — refuse rather than silently merge or orphan either.
if [[ "$NATIVE_POPULATED" -eq 1 && "$TREE_HAD_CONTENT" -eq 1 ]]; then
  echo "refuse: both $TARGET and $AGENT_MEMORY hold content — merge manually, then rerun" >&2
  exit 1
fi

if [[ "$NATIVE_POPULATED" -eq 1 ]]; then
  # Populated native, empty tree: migrate the accumulated content INTO the tree
  # (never stub/orphan it), keeping a backup. cp's trailing /. copies dotfiles too.
  cp -a "$TARGET/." "$AGENT_MEMORY/"
  BACKUP="$TARGET.premigrate.bak.$(date +%Y%m%d%H%M%S)"
  mv "$TARGET" "$BACKUP"
  echo "migrated native memory into $AGENT_MEMORY (backup: $BACKUP)"
else
  # No native content to adopt: stub a MEMORY.md if the tree lacks one, then clear
  # whatever empty/stale TARGET is in the way of the symlink.
  if [[ ! -f "$AGENT_MEMORY/MEMORY.md" ]]; then
    cat >"$AGENT_MEMORY/MEMORY.md" <<EOF
# Project memory

Index of memories specific to this project. Versioned with the project (commit \`.claude/agent-memory/\` to the project's git).

Loaded into every Claude Code session in this project via the native auto-memory mechanism (symlinked from \`~/.claude/projects/<cwd-hash>/memory/\`).

## Entries

<!-- Add one-line pointers to leaf files in this directory as memories accumulate. -->
EOF
  fi
  if [[ -L "$TARGET" ]]; then
    rm "$TARGET"
  elif [[ -d "$TARGET" ]]; then
    rmdir "$TARGET"          # guaranteed empty here (NATIVE_POPULATED==0)
  elif [[ -e "$TARGET" ]]; then
    echo "refuse: $TARGET exists and is not a directory or symlink" >&2
    exit 1
  fi
fi

ln -s "$AGENT_MEMORY" "$TARGET"
echo "ok: $TARGET → $AGENT_MEMORY"

if [[ ! -f "$PROJECT_CWD/.gitignore" ]] || ! grep -qx ".claude/" "$PROJECT_CWD/.gitignore" 2>/dev/null; then
  echo
  echo "hint: commit .claude/agent-memory/ to the project's git so other developers inherit it."
fi
