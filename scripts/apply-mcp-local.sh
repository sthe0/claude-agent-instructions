#!/usr/bin/env bash
# Register the machine's MCP server definitions (mcp-local/*.json and an org
# layer's <config root>/mcp-plugins/*.json) into <config root>/.claude.json —
# the file Claude Code reads user-scope servers from. Idempotent.
#
#   apply-mcp-local.sh            register (needs a logged-in config root)
#   apply-mcp-local.sh --check    side-effect-free: list defined-but-unregistered servers
#
# Logic and exit codes: scripts/lib/mcp_registration.py.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/lib/config-root.sh
source "$REPO/scripts/lib/config-root.sh"  # exports CLAUDE_AGENT_HOME (system root)

case "${1:-}" in
  "")      mode=apply ;;
  --check) mode=check ;;
  *)       echo "usage: apply-mcp-local.sh [--check]" >&2; exit 2 ;;
esac

exec python3 "$REPO/scripts/lib/mcp_registration.py" "$mode"
