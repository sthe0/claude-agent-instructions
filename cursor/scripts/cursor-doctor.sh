#!/usr/bin/env bash
# Cursor host readiness preflight (read-only; mutates nothing).
#
# Usage:
#     ~/claude-agent-instructions/cursor/scripts/cursor-doctor.sh
#
# Exit 0 when every hard check passes; 1 if any hard check fails.
# Soft checks print [WARN] only.
set -uo pipefail

REPO="${CLAUDE_INSTRUCTIONS_REPO:-$HOME/claude-agent-instructions}"
source "$REPO/scripts/lib/config-root.sh"
FAIL=0

pass() { printf '  [ OK ] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; FAIL=1; }
warn() { printf '  [WARN] %s\n' "$1"; }
_realpath() { python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }

echo "Cursor readiness check (repo: $REPO)"
echo

if command -v python3 >/dev/null 2>&1; then
  pass "dependency 'python3' found ($(command -v python3))"
else
  fail "dependency 'python3' not on PATH"
fi

if command -v agent >/dev/null 2>&1; then
  pass "Cursor agent CLI found ($(command -v agent))"
elif command -v cursor-agent >/dev/null 2>&1; then
  pass "Cursor agent CLI found as cursor-agent ($(command -v cursor-agent))"
else
  fail "neither 'agent' nor 'cursor-agent' on PATH — install Cursor CLI (see cursor-agent-cli-spawn.md)"
fi

_rule="$HOME/.cursor/rules/claude-code-sync.mdc"
_rule_target="$REPO/cursor/rules/claude-code-sync.mdc"
if [[ -L "$_rule" ]] && [[ "$(_realpath "$_rule")" == "$(_realpath "$_rule_target")" ]]; then
  pass "$_rule -> repo cursor mirror"
else
  fail "$_rule does not symlink to $REPO/cursor/rules/claude-code-sync.mdc — run scripts/setup-symlinks.sh"
fi

_perm="$HOME/.cursor/permissions.json"
_perm_target="$REPO/cursor/config/permissions.json"
if [[ -L "$_perm" ]] && [[ "$(_realpath "$_perm")" == "$(_realpath "$_perm_target")" ]]; then
  pass "$_perm -> cursor/config/permissions.json"
else
  fail "$_perm does not symlink to $REPO/cursor/config/permissions.json — run cursor/scripts/apply-cursor-config.sh or setup-symlinks.sh"
fi

if (cd "$REPO/scripts" && python3 -m agentctl --help >/dev/null 2>&1); then
  pass "agentctl CLI runs from $REPO/scripts (python3 -m agentctl --help)"
else
  fail "python3 -m agentctl --help failed under $REPO/scripts"
fi

if [[ -f "$HOME/.cursor_api_key" ]] || [[ -n "${CURSOR_API_KEY:-}" ]]; then
  pass "Cursor API key present (~/.cursor_api_key or CURSOR_API_KEY)"
else
  warn "no ~/.cursor_api_key and CURSOR_API_KEY unset — agent -p spawns may fail"
fi

if [[ -f "$HOME/.cursor/cli-config.json" ]]; then
  pass "~/.cursor/cli-config.json present"
else
  warn "~/.cursor/cli-config.json missing — run cursor/scripts/apply-cursor-config.sh"
fi

warn "agent CLI smoke test skipped (non-destructive doctor only)"

echo
if [[ "$FAIL" -eq 0 ]]; then
  echo "Cursor host ready."
else
  echo "Cursor readiness: fix [FAIL] lines above."
fi
exit "$FAIL"
