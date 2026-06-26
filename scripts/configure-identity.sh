#!/usr/bin/env bash
# Idempotent: creates ~/.claude/agent-identity.local from a template if absent.
# Never overwrites an existing file — the operator's channel choice is preserved.
#
# Called by setup-symlinks.sh. Safe to re-run at any time.
set -euo pipefail

IDENTITY_FILE="$HOME/.claude/agent-identity.local"

if [[ -f "$IDENTITY_FILE" ]]; then
  echo "agent-identity.local: already present, skipping (channel: $(grep '^difficulty_channel' "$IDENTITY_FILE" || echo '(unset)'))"
  exit 0
fi

mkdir -p "$(dirname "$IDENTITY_FILE")"

cat > "$IDENTITY_FILE" <<'EOF'
# Per-machine identity for the claude-agent-instructions difficulty channel.
# This file is NOT committed (it is machine-local, never git-tracked).
#
# difficulty_channel — where non-author machines submit Core difficulties:
#   startrek  →  Yandex Tracker queue OOSEVENREPORT  (internal Yandex devs; requires ~/.tracker-token)
#   github    →  GitHub Issues sthe0/claude-agent-instructions  (external; requires GITHUB_TOKEN or gh auth)
#
# Authority (author vs. non-author) is NOT stored here — it is determined
# automatically via `git push --dry-run` capability on the instructions repo.
# If your machine can push to sthe0/claude-agent-instructions, you are an author.
#
# To switch channel, change the line below and save. No other file needs editing.
difficulty_channel=startrek
EOF

echo "agent-identity.local: created at $IDENTITY_FILE (channel: startrek)"
echo "  → switch to 'github' for GitHub Issues: set difficulty_channel=github"
