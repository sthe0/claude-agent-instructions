#!/usr/bin/env bash
# Stage junk/the0/agents, commit, push (after pull). Use instead of bare arc commit for local config.
set -euo pipefail

MOUNT="${THE0_AGENTS_MOUNT:-$HOME/arcadia_the0-agents}"
JUNK_REL="junk/the0/agents"
MSG="${1:-junk/the0/agents: update local agents and memory}"

REPO_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
"$REPO_SCRIPTS/sync-junk-agents-arc.sh" pull || true

cd "$MOUNT" || exit 1
arc checkout "${THE0_AGENTS_BRANCH:-the0-agents}"
arc add "$JUNK_REL"
if arc status "$JUNK_REL" | grep -q 'nothing to commit'; then
  echo "Nothing to commit under $JUNK_REL"
  exit 0
fi
arc commit -m "$MSG"
"$REPO_SCRIPTS/sync-junk-agents-arc.sh" push
