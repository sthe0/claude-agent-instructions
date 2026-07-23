#!/usr/bin/env bash
# Symlinks the per-cwd native auto-memory directory under
# $CLAUDE_AGENT_HOME/projects/ (default ~/.claude-agent/projects/; see
# lib/config-root.sh) to <project_cwd>/.claude/agent-memory/, so Claude Code
# reads and writes project memory through the native mechanism but the actual
# files live inside the project tree (versionable, shareable with other
# developers).
#
# Usage:
#   setup-project-memory.sh [project_cwd]
#
# Default project_cwd is $PWD. The script:
#   1. Creates <project_cwd>/.claude/agent-memory/ (stub MEMORY.md only when there
#      is no native content to adopt — see step 3).
#   2. Computes the Claude Code per-cwd hash directory name: every non-alphanumeric char in the absolute cwd → "-" (matches the harness; "/" and "_" both map to "-").
#   3. If a native <root>/projects/<hash>/memory (isolated root, or a legacy
#      ~/.claude/projects/<hash>/memory not yet migrated) is a populated real dir
#      and the in-tree agent-memory is empty, MIGRATES the native content into
#      agent-memory (keeping a .premigrate.bak backup) instead of orphaning it;
#      refuses if BOTH sides already hold content.
#   4. Replaces the native path with a symlink → <project_cwd>/.claude/agent-memory.
#   5. Re-points a legacy ~/.claude/projects/<hash>/memory symlink (from a
#      pre-isolation install) at the same target, if one exists, so it keeps
#      resolving instead of dangling.
#
# Idempotent. Safe to rerun. Never orphans accumulated memory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/config-root.sh"

PROJECT_CWD="${1:-$PWD}"
PROJECT_CWD="$(cd "$PROJECT_CWD" && pwd)"

if [[ "$PROJECT_CWD" == "$HOME" ]]; then
  echo "refuse: $HOME is not a project — native auto-memory at $CLAUDE_AGENT_HOME/projects/-Users-* stays as-is" >&2
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
PROJECTS_DIR="$CLAUDE_AGENT_HOME/projects/$HASH"
mkdir -p "$PROJECTS_DIR"

TARGET="$PROJECTS_DIR/memory"

# A pre-isolation install may still have its native auto-memory anchored at
# the old root — track it so we can adopt its content and, once the isolated
# TARGET is in place, re-point it instead of leaving it dangling.
LEGACY_PROJECTS_DIR="$HOME/.claude/projects/$HASH"
LEGACY_TARGET="$LEGACY_PROJECTS_DIR/memory"
LEGACY_DIFFERS=1
[[ "$LEGACY_TARGET" == "$TARGET" ]] && LEGACY_DIFFERS=0

if [[ -L "$TARGET" && "$(readlink "$TARGET")" == "$AGENT_MEMORY" ]]; then
  echo "ok: $TARGET → $AGENT_MEMORY (already linked)"
else
  # Is there a populated native real directory we must preserve? Prefer the
  # isolated TARGET; fall back to a not-yet-migrated legacy location (a
  # pre-fix install wrote its accumulated memory there).
  NATIVE_SOURCE=""
  if [[ -d "$TARGET" && ! -L "$TARGET" && -n "$(ls -A "$TARGET" 2>/dev/null)" ]]; then
    NATIVE_SOURCE="$TARGET"
  elif [[ "$LEGACY_DIFFERS" -eq 1 && -d "$LEGACY_TARGET" && ! -L "$LEGACY_TARGET" && -n "$(ls -A "$LEGACY_TARGET" 2>/dev/null)" ]]; then
    NATIVE_SOURCE="$LEGACY_TARGET"
  fi
  NATIVE_POPULATED=0
  [[ -n "$NATIVE_SOURCE" ]] && NATIVE_POPULATED=1

  # Both sides hold content — refuse rather than silently merge or orphan either.
  if [[ "$NATIVE_POPULATED" -eq 1 && "$TREE_HAD_CONTENT" -eq 1 ]]; then
    echo "refuse: both $NATIVE_SOURCE and $AGENT_MEMORY hold content — merge manually, then rerun" >&2
    exit 1
  fi

  if [[ "$NATIVE_POPULATED" -eq 1 ]]; then
    # Populated native, empty tree: migrate the accumulated content INTO the tree
    # (never stub/orphan it), keeping a backup. cp's trailing /. copies dotfiles too.
    cp -a "$NATIVE_SOURCE/." "$AGENT_MEMORY/"
    BACKUP="$NATIVE_SOURCE.premigrate.bak.$(date +%Y%m%d%H%M%S)"
    mv "$NATIVE_SOURCE" "$BACKUP"
    echo "migrated native memory from $NATIVE_SOURCE into $AGENT_MEMORY (backup: $BACKUP)"
  else
    # No native content to adopt: stub a MEMORY.md if the tree lacks one, then clear
    # whatever empty/stale TARGET is in the way of the symlink.
    if [[ ! -f "$AGENT_MEMORY/MEMORY.md" ]]; then
      cat >"$AGENT_MEMORY/MEMORY.md" <<EOF
# Project memory

Index of memories specific to this project. Versioned with the project (commit \`.claude/agent-memory/\` to the project's git).

Loaded into every Claude Code session in this project via the native auto-memory mechanism (symlinked from \`$CLAUDE_AGENT_HOME/projects/<cwd-hash>/memory/\`).

## Entries

<!-- Add one-line pointers to leaf files in this directory as memories accumulate. -->
EOF
      python3 "$SCRIPT_DIR/edit-ledger.py" stamp --file "$AGENT_MEMORY/MEMORY.md" --tool "script:setup-project-memory" >/dev/null 2>&1 || true
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
fi

# Re-point a legacy in-place symlink at the OLD root, if one exists, so it
# keeps resolving instead of dangling at a moved target. Never touches a real
# (non-symlink) legacy dir here — that case was already adopted above.
if [[ "$LEGACY_DIFFERS" -eq 1 && -L "$LEGACY_TARGET" ]]; then
  ln -sfn "$AGENT_MEMORY" "$LEGACY_TARGET"
  echo "re-pointed legacy $LEGACY_TARGET → $AGENT_MEMORY"
fi

if [[ ! -f "$PROJECT_CWD/.gitignore" ]] || ! grep -qx ".claude/" "$PROJECT_CWD/.gitignore" 2>/dev/null; then
  echo
  echo "hint: commit .claude/agent-memory/ to the project's git so other developers inherit it."
fi
