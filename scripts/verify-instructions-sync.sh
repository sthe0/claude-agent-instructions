#!/usr/bin/env bash
# Verify global instruction symlinks (git repo only).
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
FAIL=0

check_link() {
  local linkpath="$1" expected_target="$2"
  if [[ ! -L "$linkpath" ]]; then
    echo "FAIL: not a symlink: $linkpath"
    FAIL=1
    return
  fi
  local actual expected
  actual="$(readlink -f "$linkpath")"
  expected="$(readlink -f "$expected_target")"
  if [[ "$actual" != "$expected" ]]; then
    echo "FAIL: $linkpath -> $actual (expected $expected)"
    FAIL=1
  else
    echo "OK: $linkpath"
  fi
}

echo "=== Global symlinks ==="
check_link "$HOME/.claude/CLAUDE.md" "$REPO/CLAUDE.md"
check_link "$HOME/.cursor/rules/claude-code-sync.mdc" "$REPO/cursor-rules/claude-code-sync.mdc"
check_link "$HOME/.claude/memory-global" "$REPO/memory-global"
check_link "$HOME/.cursor/agents" "$HOME/.claude/agents"

for f in "$REPO/agents/"*.md; do
  [[ -f "$f" ]] || continue
  check_link "$HOME/.claude/agents/$(basename "$f")" "$f"
done

if [[ -L "$HOME/.claude/agents/yandex-developer.md" ]]; then
  echo "FAIL: stale yandex-developer symlink"
  FAIL=1
fi

if [[ -f "$HOME/claude-agent-instructions/scripts/sync-junk-agents-arc.sh" ]]; then
  echo "FAIL: local arc scripts must not live in global scripts/ — use ~/.claude/scripts-local/"
  FAIL=1
fi

echo "=== Local (the0-agents) ==="
if [[ -x "$HOME/.claude/scripts-local/verify-the0-agents-sync.sh" ]]; then
  "$HOME/.claude/scripts-local/verify-the0-agents-sync.sh" || FAIL=1
else
  echo "WARN: skip local verify — ~/.claude/scripts-local/ missing"
fi

echo "=== Stale copies ==="
DEEPAGENT_RULE="$HOME/arcadia/robot/deepagent/.cursor/rules/claude-code-sync.mdc"
if [[ -f "$DEEPAGENT_RULE" && ! -L "$DEEPAGENT_RULE" ]]; then
  size="$(wc -c <"$DEEPAGENT_RULE")"
  if [[ "$size" -lt 2000 ]]; then
    echo "WARN: $DEEPAGENT_RULE is a small regular file ($size bytes)"
    FAIL=1
  fi
fi

OVERLAY="$HOME/arcadia/robot/deepagent/.cursor/rules/deepagent-project.mdc"
if [[ -L "$OVERLAY" ]]; then
  check_link "$OVERLAY" "$REPO/cursor-rules/project-overlay-deepagent.mdc"
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
