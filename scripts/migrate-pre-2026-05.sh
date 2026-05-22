#!/usr/bin/env bash
# Migrate the runtime layer (~/.claude/) from the pre-2026-05 layout to the
# post-refactor one: drop the legacy ~/.claude/memory directory, re-apply
# symlinks (which prunes dangling agent symlinks and creates skills/), verify.
#
# Idempotent. Safe to run multiple times. See:
#   docs/migrations/2026-05-collapse-manager-memory.md
#
# Does NOT touch per-project memory — for that, run scripts/setup-project-memory.sh
# inside each project you want to set up.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Migration: pre-2026-05 → post-refactor ==="

if [[ -d "$HOME/.claude/memory" || -L "$HOME/.claude/memory" ]]; then
  if [[ -L "$HOME/.claude/memory" ]]; then
    target="$(readlink "$HOME/.claude/memory")"
    echo "Removing legacy ~/.claude/memory symlink (was → $target)"
    rm "$HOME/.claude/memory"
  else
    echo "Removing legacy ~/.claude/memory directory"
    if find "$HOME/.claude/memory" -mindepth 1 -not -type l | read -r; then
      backup="$HOME/.claude/memory.bak.$(date +%Y%m%d%H%M%S)"
      echo "  → contents include non-symlink files; moving to $backup for review"
      mv "$HOME/.claude/memory" "$backup"
    else
      rm -rf "$HOME/.claude/memory"
    fi
  fi
fi

for stale in manager.md memory.md self-improvement.md; do
  link="$HOME/.claude/agents/$stale"
  if [[ -L "$link" || -f "$link" ]]; then
    echo "Removing stale agent file: $stale"
    rm -f "$link"
  fi
done

echo "Re-applying symlinks…"
"$REPO/scripts/setup-symlinks.sh"

echo
echo "=== Verification ==="
"$REPO/scripts/verify-instructions-sync.sh"

echo
echo "Migration complete."
echo "Next step (per project where you want shared agent memory):"
echo "    cd <project_cwd>"
echo "    $REPO/scripts/setup-project-memory.sh"
