#!/usr/bin/env bash
# Verify global instruction symlinks (git repo only).
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
SCRIPTS_DIR="$(dirname "${BASH_SOURCE[0]}")" || SCRIPTS_DIR="."
# shellcheck source=scripts/lib/config-root.sh
source "$SCRIPTS_DIR/lib/config-root.sh"  # exports CLAUDE_AGENT_HOME (system root — isolated or legacy)
FAIL=0
_realpath() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }

check_link() {
  local linkpath="$1" expected_target="$2"
  if [[ ! -L "$linkpath" ]]; then
    echo "FAIL: not a symlink: $linkpath"
    FAIL=1
    return
  fi
  local actual expected
  actual="$(_realpath "$linkpath")"
  expected="$(_realpath "$expected_target")"
  if [[ "$actual" != "$expected" ]]; then
    echo "FAIL: $linkpath -> $actual (expected $expected)"
    FAIL=1
  else
    echo "OK: $linkpath"
  fi
}

if [[ -x "$REPO/scripts/verify-layout-contract.sh" ]]; then
  echo "=== Layout contract ==="
  "$REPO/scripts/verify-layout-contract.sh" || FAIL=1
fi

# The contract's sibling: it asserts extracted skills are in the overlay, this one
# asserts the overlay copy actually resolves in the catalog. Nothing inside the repo
# can see that link, so an unrun check here means a silently uninvocable skill.
if [[ -x "$REPO/scripts/verify-extracted-skills-resolve.sh" ]]; then
  echo "=== Extracted skills ==="
  "$REPO/scripts/verify-extracted-skills-resolve.sh" || FAIL=1
fi

echo "=== Global symlinks ==="
check_link "$CLAUDE_AGENT_HOME/CLAUDE.md" "$REPO/CLAUDE.md"
check_link "$HOME/.cursor/rules/claude-code-sync.mdc" "$REPO/cursor/rules/claude-code-sync.mdc"
check_link "$CLAUDE_AGENT_HOME/memory-global" "$REPO/memory-global"

for f in "$REPO/agents/"*.md; do
  [[ -f "$f" ]] || continue
  base="$(basename "$f")"
  [[ "$base" == "README.md" ]] && continue
  check_link "$CLAUDE_AGENT_HOME/agents/$base" "$f"
done

for f in "$REPO/cursor/agents/"*.md; do
  [[ -f "$f" ]] || continue
  base="$(basename "$f")"
  [[ "$base" == "README.md" ]] && continue
  check_link "$HOME/.cursor/agents/$base" "$f"
done

if [[ -L "$HOME/.cursor/agents" ]]; then
  echo "FAIL: ~/.cursor/agents must be a directory, not a symlink"
  FAIL=1
fi

if [[ -d "$REPO/skills" ]]; then
  for d in "$REPO/skills/"*/; do
    [[ -d "$d" ]] || continue
    d="${d%/}"
    base="$(basename "$d")"
    # The specializations/ container is not symlinked; its contents are flattened.
    [[ "$base" == "specializations" ]] && continue
    check_link "$CLAUDE_AGENT_HOME/skills/$base" "$d"
  done
fi

if [[ -d "$REPO/skills/specializations" ]]; then
  for d in "$REPO/skills/specializations/"*/; do
    [[ -d "$d" ]] || continue
    d="${d%/}"
    check_link "$CLAUDE_AGENT_HOME/skills/$(basename "$d")" "$d"
  done
fi

# Stale agents that should no longer exist
for stale in manager.md memory.md self-improvement.md yandex-developer.md; do
  if [[ -L "$CLAUDE_AGENT_HOME/agents/$stale" || -f "$CLAUDE_AGENT_HOME/agents/$stale" ]]; then
    echo "FAIL: stale agent symlink $stale"
    FAIL=1
  fi
done

if [[ -f "$HOME/claude-agent-instructions/scripts/sync-junk-agents-arc.sh" ]]; then
  echo "FAIL: local arc scripts must not live in global scripts/ — use ~/.claude/scripts-local/"
  FAIL=1
fi

echo "=== Git (instructions repo) ==="
cd "$REPO"
git fetch origin main -q 2>/dev/null || true
behind="$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
ahead="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)"
if [[ "$behind" -gt 0 ]]; then
  echo "WARN: behind origin/main by $behind — sync-instructions-repo.sh pull"
  FAIL=1
fi
if [[ "$ahead" -gt 0 ]]; then
  echo "WARN: ahead of origin/main by $ahead — push"
fi
echo "git: behind=$behind ahead=$ahead"

if [[ "$FAIL" -eq 0 ]]; then
  echo "All checks passed."
else
  echo "Some checks failed. Run: $REPO/scripts/setup-symlinks.sh"
  exit 1
fi
