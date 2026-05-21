#!/usr/bin/env bash
# Verify on-disk layout matches memory-global/agent-instructions/file-structure-contract.md
set -euo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
FAIL=0

fail() {
  echo "FAIL: $*"
  FAIL=1
}

ok() {
  echo "OK: $*"
}

require_dir() {
  [[ -d "$1" ]] || fail "missing directory $1"
}

require_file() {
  [[ -f "$1" ]] || fail "missing file $1"
}

require_absent() {
  [[ ! -e "$1" ]] || fail "must not exist: $1"
}

echo "=== Global repo tree ==="
require_file "$REPO/CLAUDE.md"
require_file "$REPO/README.md"
require_dir "$REPO/agents"
require_file "$REPO/agents/developer.md"
require_file "$REPO/memory-global/INDEX.md"
require_file "$REPO/memory-global/agent-instructions/file-structure-contract.md"
require_file "$REPO/memory-global/agent-instructions/runtime-layout.md"
require_file "$REPO/cursor-rules/claude-code-sync.mdc"
require_dir "$REPO/scripts"
require_file "$REPO/scripts/setup-symlinks.sh"
require_file "$REPO/scripts/verify-layout-contract.sh"
require_file "$REPO/scripts/sync-instructions-repo.sh"

for forbidden in sync-junk-agents-arc.sh junk-agents-arc-commit.sh setup-the0-agents-mount.sh install-junk-agents-sync-cron.sh; do
  require_absent "$REPO/scripts/$forbidden"
done
ok "no local arc scripts in global scripts/"

echo "=== Runtime symlinks ==="
if [[ -L "$HOME/.claude/CLAUDE.md" ]]; then ok "~/.claude/CLAUDE.md"; else fail "~/.claude/CLAUDE.md not symlink"; fi
if [[ -L "$HOME/.claude/memory-global" ]]; then ok "~/.claude/memory-global"; else fail "~/.claude/memory-global"; fi
if [[ -L "$HOME/.claude/memory" ]]; then
  if readlink -f "$HOME/.claude/memory" | grep -qE '/arcadia/junk/the0/agents'; then
    fail "~/.claude/memory points at main ~/arcadia"
  else
    ok "~/.claude/memory"
  fi
else
  fail "~/.claude/memory not symlink"
fi
if [[ -L "$HOME/.claude/scripts-local" ]]; then ok "~/.claude/scripts-local"; else fail "~/.claude/scripts-local (run setup-symlinks.sh)"; fi

if [[ -x "$HOME/.claude/scripts-local/verify-the0-agents-sync.sh" ]]; then
  echo "=== Local layer (delegate) ==="
  "$HOME/.claude/scripts-local/verify-the0-agents-sync.sh" || FAIL=1
else
  echo "WARN: skip local layer verify"
fi

if [[ "$FAIL" -eq 0 ]]; then
  echo "Layout contract checks passed."
  exit 0
fi
echo "Layout contract checks failed. See: $REPO/memory-global/agent-instructions/file-structure-contract.md"
exit 1
