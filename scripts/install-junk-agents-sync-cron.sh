#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SYNC="$REPO/scripts/sync-junk-agents-arc.sh"
chmod +x "$SYNC" "$REPO/scripts/install-junk-agents-sync-cron.sh"
CRON_LINE="*/10 * * * * $SYNC pull >>$HOME/.local/log/junk-agents-arc-sync.log 2>&1"
MARKER="# junk-the0-agents arc sync"
(
  crontab -l 2>/dev/null | grep -v "$MARKER" | grep -v "sync-junk-agents-arc.sh pull" || true
  echo "$MARKER"
  echo "$CRON_LINE"
) | crontab - 2>/dev/null || {
  echo "WARN: crontab denied — use systemd timer or run pull manually" >&2
  exit 0
}
echo "Installed:"
crontab -l | grep -A1 "$MARKER"
