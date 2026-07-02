#!/usr/bin/env bash
# User systemd timer: pull every 10 min (fallback when crontab is denied).
#
# DEPRECATED: superseded by the daily explicit refresh OFFER
# (hook-instructions-refresh-due.py, a UserPromptSubmit hook that nudges the
# agent to ask before pulling, once per calendar day) — see
# docs/architecture/instruction-layering.md. This silent 10-min background
# pull can stash/rebase into a conflict on top of uncommitted local work with
# no one watching. To uninstall:
#   systemctl --user disable --now claude-agent-instructions-pull.timer
set -euo pipefail
[[ "$(uname -s)" == "Linux" ]] || { echo 'systemd timer: Linux only — use install-sync-cron.sh / launchd on macOS' >&2; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SYNC="$REPO/scripts/sync-instructions-repo.sh"
chmod +x "$SYNC" "$REPO/scripts/install-sync-systemd-timer.sh"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR" "$HOME/.local/log"
cat >"$UNIT_DIR/claude-agent-instructions-pull.service" <<EOF
[Unit]
Description=Pull claude-agent-instructions from origin

[Service]
Type=oneshot
ExecStart=%h/claude-agent-instructions/scripts/sync-instructions-repo.sh pull
StandardOutput=append:%h/.local/log/claude-agent-instructions-sync.log
StandardError=append:%h/.local/log/claude-agent-instructions-sync.log
EOF
cat >"$UNIT_DIR/claude-agent-instructions-pull.timer" <<EOF
[Unit]
Description=Pull claude-agent-instructions every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now claude-agent-instructions-pull.timer
systemctl --user list-timers --all | grep claude-agent-instructions || true
echo "Timer enabled. Log: ~/.local/log/claude-agent-instructions-sync.log"
