#!/usr/bin/env bash
# Idempotent: creates ~/.claude/agent-identity.local from a template if absent.
# Never overwrites an existing file — the operator's channel choice is preserved.
#
# Called by setup-symlinks.sh. Safe to re-run at any time.
set -euo pipefail

IDENTITY_FILE="$HOME/.claude/agent-identity.local"
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$IDENTITY_FILE" ]]; then
  echo "agent-identity.local: already present, skipping (channel: $(grep '^difficulty_channel' "$IDENTITY_FILE" || echo '(unset)'))"
  exit 0
fi

mkdir -p "$(dirname "$IDENTITY_FILE")"

# Detect difficulty channel from hardware signals; fall back to the org-neutral
# github default if detection fails (mirrors difficulty_channel.detect's default arm).
_detect_tmp="$(mktemp)"
_channel="github"
_detect_header="# detection failed, defaulted to github"
if _detect_out="$(cd "$SCRIPTS_DIR" && python3 -m difficulty_channel.detect 2>"$_detect_tmp")" \
   && [[ -n "$_detect_out" ]]; then
  _channel="$_detect_out"
  if [[ -s "$_detect_tmp" ]]; then
    _detect_header="$(sed 's/^/# detected: /' "$_detect_tmp")"
  else
    _detect_header="# detected: channel=${_channel}"
  fi
fi
rm -f "$_detect_tmp"

# Detect project-entry backends; fall back to git/none if detection fails.
_det_ws="git" _det_tr="none"
if _det_out="$(cd "$SCRIPTS_DIR" && python3 -m project_entry.detect_backend 2>/dev/null)"; then
  read -r _det_ws _det_tr <<<"$_det_out" || true
fi

cat > "$IDENTITY_FILE" <<EOF
# Per-machine identity for the claude-agent-instructions difficulty channel.
# This file is NOT committed (it is machine-local, never git-tracked).
#
${_detect_header}
#
# difficulty_channel — where non-author machines submit Core difficulties:
#   startrek  →  Yandex Tracker queue OOSEVENREPORT  (internal Yandex devs; requires ~/.tracker-token)
#   github    →  GitHub Issues sthe0/claude-agent-instructions  (external; requires GITHUB_TOKEN or gh auth)
#
# Authority (author vs. non-author) is NOT stored here — it is determined
# automatically via \`git push --dry-run\` capability on the instructions repo.
# If your machine can push to sthe0/claude-agent-instructions, you are an author.
#
# To switch channel, change the line below and save. No other file needs editing.
difficulty_channel=${_channel}
#
# project_backend — workspace backend for task entry (enter-task.sh):
#   git  → git worktree (org-neutral default)
#   arc  → Arcadia arc mount (internal Yandex; requires arc + ya on PATH)
# tracker_backend — tracker backend for task entry:
#   none     → no tracker integration
#   github   → GitHub Issues (requires gh CLI + auth)
#   startrek → Yandex Tracker (internal Yandex; requires ~/.tracker-token)
#
# To override, change the lines below and save.
project_backend=${_det_ws}
tracker_backend=${_det_tr}
EOF

echo "agent-identity.local: created at $IDENTITY_FILE (channel: ${_channel})"
echo "  → switch channel: change difficulty_channel= in $IDENTITY_FILE"
