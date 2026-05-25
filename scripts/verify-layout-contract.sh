#!/usr/bin/env bash
# Verify on-disk layout matches skills/self-improvement/policy.md § File structure.
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
require_file "$REPO/config.md"
require_file "$REPO/README.md"
require_dir "$REPO/agents"
require_file "$REPO/agents/README.md"
# No shipped Task-spawned subagents currently — these specialists moved to skills/specializations/
require_absent "$REPO/agents/developer.md"
require_absent "$REPO/agents/planner.md"
require_absent "$REPO/agents/thinker.md"
require_absent "$REPO/agents/yandex-cloud-expert.md"
require_absent "$REPO/agents/manager.md"
require_absent "$REPO/agents/memory.md"
require_absent "$REPO/agents/self-improvement.md"

require_dir "$REPO/skills"
require_file "$REPO/skills/overcome-difficulty/SKILL.md"
require_file "$REPO/skills/self-improvement/SKILL.md"
require_file "$REPO/skills/self-improvement/policy.md"
require_file "$REPO/skills/tracker-management/SKILL.md"

require_dir "$REPO/skills/specializations"
require_file "$REPO/skills/specializations/planner/SKILL.md"
require_file "$REPO/skills/specializations/developer/SKILL.md"
require_file "$REPO/skills/specializations/thinker/SKILL.md"
require_file "$REPO/skills/specializations/yandex-cloud-expert/SKILL.md"

require_dir "$REPO/memory-global"
require_file "$REPO/memory-global/MEMORY.md"
require_dir "$REPO/memory-global/leaves"
require_dir "$REPO/memory-global/leaves/experience"
require_dir "$REPO/permissions"
require_file "$REPO/permissions/global.json"
require_file "$REPO/permissions/README.md"
require_file "$REPO/scripts/permissions-cli.py"
require_file "$REPO/scripts/lint-permissions.py"
require_file "$REPO/scripts/spawn-specialist.py"
require_file "$REPO/scripts/verify-cross-refs.py"
require_file "$REPO/scripts/lint-cursor-mirror.py"
require_file "$REPO/scripts/cost-report.py"
require_file "$REPO/scripts/memory-audit.py"
require_absent "$REPO/memory-global/INDEX.md"
require_absent "$REPO/memory-global/agent-instructions"
require_absent "$REPO/memory-global/development"
require_absent "$REPO/memory-meta"

require_file "$REPO/cursor-rules/claude-code-sync.mdc"
require_dir "$REPO/scripts"
require_file "$REPO/scripts/setup-symlinks.sh"
require_file "$REPO/scripts/setup-project-memory.sh"
require_file "$REPO/scripts/verify-layout-contract.sh"
require_file "$REPO/scripts/sync-instructions-repo.sh"
require_file "$REPO/scripts/apply-mcp-local.sh"
require_file "$REPO/scripts/verify-all.py"
require_file "$REPO/scripts/verify-language.py"
require_file "$REPO/githooks/pre-commit"
require_file "$REPO/githooks/post-commit"
require_file "$REPO/agents-local/README.md"
require_file "$REPO/skills-local/README.md"
require_file "$REPO/mcp-local/README.md"

for forbidden in sync-junk-agents-arc.sh junk-agents-arc-commit.sh setup-the0-agents-mount.sh install-junk-agents-sync-cron.sh; do
  require_absent "$REPO/scripts/$forbidden"
done
ok "no local arc scripts in global scripts/"

echo "=== Runtime symlinks ==="
if [[ -L "$HOME/.claude/CLAUDE.md" ]]; then ok "~/.claude/CLAUDE.md"; else fail "~/.claude/CLAUDE.md not symlink"; fi
if [[ -L "$HOME/.claude/config.md" ]]; then ok "~/.claude/config.md"; else fail "~/.claude/config.md not symlink"; fi
if [[ -L "$HOME/.claude/memory-global" ]]; then ok "~/.claude/memory-global"; else fail "~/.claude/memory-global"; fi
if [[ -d "$HOME/.claude/skills" ]]; then ok "~/.claude/skills"; else fail "~/.claude/skills"; fi

# ~/.claude/memory (old local memory dir) must be gone in the new model
if [[ -e "$HOME/.claude/memory" ]]; then
  fail "~/.claude/memory exists — superseded by ~/.claude/memory-global and <project>/.claude/agent-memory. Remove it."
fi

if [[ -L "$HOME/.claude/scripts-local" ]]; then
  ok "~/.claude/scripts-local"
fi

if [[ "$FAIL" -eq 0 ]]; then
  echo "Layout contract checks passed."
  exit 0
fi
echo "Layout contract checks failed. See: $REPO/skills/self-improvement/policy.md § File structure"
exit 1
