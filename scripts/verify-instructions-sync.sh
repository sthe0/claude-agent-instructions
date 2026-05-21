#!/usr/bin/env bash
# Verify Claude Code + Cursor read the same instruction files via symlinks.
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
THE0_AGENTS_MOUNT="${THE0_AGENTS_MOUNT:-$HOME/arcadia_the0-agents}"
JUNK_AGENTS_ROOT="${JUNK_AGENTS_ROOT:-$THE0_AGENTS_MOUNT/junk/the0/agents}"
FAIL=0

check_link() {
  local linkpath="$1" expected_target="$2"
  if [[ ! -L "$linkpath" ]]; then
    echo "FAIL: not a symlink: $linkpath"
    FAIL=1
    return
  fi
  local actual
  actual="$(readlink -f "$linkpath")"
  expected_target="$(readlink -f "$expected_target")"
  if [[ "$actual" != "$expected_target" ]]; then
    echo "FAIL: $linkpath -> $actual (expected $expected_target)"
    FAIL=1
  else
    echo "OK: $linkpath"
  fi
}

echo "=== Symlinks (Claude Code + Cursor) ==="
check_link "$HOME/.claude/CLAUDE.md" "$REPO/CLAUDE.md"
check_link "$HOME/.cursor/rules/claude-code-sync.mdc" "$REPO/cursor-rules/claude-code-sync.mdc"
check_link "$HOME/.claude/memory-global" "$REPO/memory-global"
check_link "$HOME/.claude/memory" "$JUNK_AGENTS_ROOT/memory-local"
if readlink -f "$HOME/.claude/memory" 2>/dev/null | grep -qE '/arcadia/junk/the0/agents'; then
  echo "FAIL: ~/.claude/memory still points at main ~/arcadia — run setup-symlinks.sh"
  FAIL=1
fi
if [[ ! -d "$THE0_AGENTS_MOUNT" ]] || ! arc mount --list 2>/dev/null | grep -qF "$THE0_AGENTS_MOUNT"; then
  echo "WARN: mount $THE0_AGENTS_MOUNT not listed — run scripts/setup-the0-agents-mount.sh"
fi
check_link "$HOME/.cursor/agents" "$HOME/.claude/agents"

if [[ -d "$HOME/.claude/agents" ]]; then
  for f in "$REPO/agents/"*.md; do
    [[ -f "$f" ]] || continue
    base="$(basename "$f")"
    check_link "$HOME/.claude/agents/$base" "$f"
  done
  if [[ -f "$JUNK_AGENTS_ROOT/agents-local/yandex-guru.md" ]]; then
    check_link "$HOME/.claude/agents/yandex-guru.md" "$JUNK_AGENTS_ROOT/agents-local/yandex-guru.md"
  fi
fi

if [[ -L "$HOME/.claude/agents/yandex-developer.md" ]]; then
  echo "FAIL: stale yandex-developer symlink — remove and run setup-symlinks.sh"
  FAIL=1
fi

echo "=== Stale copies (should not exist) ==="
DEEPAGENT_RULE="$HOME/arcadia/robot/deepagent/.cursor/rules/claude-code-sync.mdc"
if [[ -f "$DEEPAGENT_RULE" && ! -L "$DEEPAGENT_RULE" ]]; then
  size="$(wc -c <"$DEEPAGENT_RULE")"
  if [[ "$size" -lt 2000 ]]; then
    echo "WARN: $DEEPAGENT_RULE is a small regular file ($size bytes) — replace with overlay deepagent-project.mdc"
    FAIL=1
  fi
fi

if [[ -f "$DEEPAGENT_RULE" && -L "$DEEPAGENT_RULE" ]]; then
  echo "WARN: project still symlinks global rule — prefer deepagent-project.mdc overlay only"
fi

OVERLAY="$HOME/arcadia/robot/deepagent/.cursor/rules/deepagent-project.mdc"
if [[ -f "$OVERLAY" ]]; then
  if [[ -L "$OVERLAY" ]]; then
    check_link "$OVERLAY" "$REPO/cursor-rules/project-overlay-deepagent.mdc"
  else
    echo "WARN: $OVERLAY exists but is not symlink to repo template"
  fi
fi

echo "=== Git ==="
cd "$REPO"
git fetch origin main -q 2>/dev/null || true
behind="$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
ahead="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo 0)"
if [[ "$behind" -gt 0 ]]; then
  echo "WARN: repo behind origin/main by $behind commits — run sync-instructions-repo.sh pull"
  FAIL=1
fi
if [[ "$ahead" -gt 0 ]]; then
  echo "WARN: repo ahead of origin/main by $ahead commits — run push"
fi
echo "git: behind=$behind ahead=$ahead"

if [[ "$FAIL" -eq 0 ]]; then
  echo "All checks passed."
else
  echo "Some checks failed. Run: $REPO/scripts/setup-symlinks.sh"
  exit 1
fi
