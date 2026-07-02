#!/usr/bin/env bash
# DEPRECATED: superseded by the daily explicit refresh OFFER
# (hook-instructions-refresh-due.py, a UserPromptSubmit hook that nudges the
# agent to ask before pulling, once per calendar day) — see
# docs/architecture/instruction-layering.md. This silent 10-min background
# pull can stash/rebase into a conflict on top of uncommitted local work with
# no one watching. To uninstall: `crontab -e` and remove the
# "# claude-agent-instructions sync" line + the line below it (or `crontab -r`
# to clear the whole crontab).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SYNC="$REPO/scripts/sync-instructions-repo.sh"
chmod +x "$SYNC" "$REPO/scripts/install-sync-cron.sh"
CRON_LINE="*/10 * * * * $SYNC pull >>$HOME/.local/log/claude-agent-instructions-sync.log 2>&1"
MARKER="# claude-agent-instructions sync"
(
  crontab -l 2>/dev/null | grep -v "$MARKER" | grep -v "sync-instructions-repo.sh pull" || true
  echo "$MARKER"
  echo "$CRON_LINE"
) | crontab -
echo "Installed crontab entry:"
crontab -l | grep -A1 "$MARKER"
