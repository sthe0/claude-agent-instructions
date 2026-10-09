#!/usr/bin/env bash
# Install the reboot-recovery units (systemd --user) and the `claude-recover` entry point.
#
#   claude-recover-snapshot.timer/.service  every minute: snapshot live claude sessions + FUSE mounts
#   claude-recover.service                  once per boot, after --after-unit: restore --auto
#
# Usage: install-claude-recover-systemd.sh [--dry-run] [--uninstall] [--after-unit UNIT]
#
# Env seams (tests): SYSTEMD_USER_DIR, LOCAL_BIN_DIR, SYSTEMCTL_BIN, LOGINCTL_BIN.
# The restore unit is only `enable`d here, never started: installing must not run a restore
# against a live system.
set -euo pipefail
[[ "$(uname -s)" == "Linux" ]] || { echo 'claude-recover units: Linux only' >&2; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
RECOVER="$REPO/scripts/claude-recover.py"
UNIT_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
BIN_DIR="${LOCAL_BIN_DIR:-$HOME/.local/bin}"
SYSTEMCTL="${SYSTEMCTL_BIN:-systemctl}"
LOGINCTL="${LOGINCTL_BIN:-loginctl}"
AFTER_UNIT="ccgram.service"
DRY=0
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --after-unit) AFTER_UNIT="${2:?--after-unit needs a unit name}"; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

run() {
  if (( DRY )); then printf 'dry-run: %s\n' "$*"; else "$@"; fi
}

if (( UNINSTALL )); then
  run "$SYSTEMCTL" --user disable --now claude-recover-snapshot.timer || true
  run "$SYSTEMCTL" --user disable claude-recover.service || true
  for f in claude-recover-snapshot.timer claude-recover-snapshot.service claude-recover.service; do
    run rm -f "$UNIT_DIR/$f"
  done
  run rm -f "$BIN_DIR/claude-recover"
  run "$SYSTEMCTL" --user daemon-reload
  exit 0
fi

# The unit's hard ceiling must exceed the kernel's own deadline, else systemd kills a healthy run.
DEADLINE_MIN="$(sed -nE 's/^DEADLINE_S = ([0-9]+) \* 60$/\1/p' "$RECOVER")"
[[ -n "$DEADLINE_MIN" ]] || { echo "cannot read DEADLINE_S from $RECOVER" >&2; exit 1; }
RUNTIME_MAX_MIN=$(( DEADLINE_MIN + 5 ))

path_dirs=""
for bin in claude tmux; do
  p="$(command -v "$bin" 2>/dev/null || true)"
  if [[ -n "$p" && "$p" == /* ]]; then
    path_dirs+="$(dirname "$p"):"
  else
    echo "warning: $bin not found on PATH; the units will not see it" >&2
  fi
done
UNIT_PATH="${path_dirs}$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

env_lines="Environment=PATH=$UNIT_PATH"
[[ -n "${CLAUDE_RECOVER_CONFIG_DIRS:-}" ]] && env_lines+=$'\n'"Environment=CLAUDE_RECOVER_CONFIG_DIRS=$CLAUDE_RECOVER_CONFIG_DIRS"
[[ -n "${TMUX_TMPDIR:-}" ]] && env_lines+=$'\n'"Environment=TMUX_TMPDIR=$TMUX_TMPDIR"

write_unit() {
  local name="$1" body
  body="$(cat)"
  if (( DRY )); then
    printf 'dry-run: write %s/%s\n' "$UNIT_DIR" "$name"
  else
    mkdir -p "$UNIT_DIR"
    printf '%s\n' "$body" >"$UNIT_DIR/$name"
  fi
}

write_unit claude-recover-snapshot.service <<EOF
[Unit]
Description=Snapshot live claude sessions and FUSE mounts for reboot recovery

[Service]
Type=oneshot
$env_lines
ExecStart=$RECOVER snapshot
EOF

write_unit claude-recover-snapshot.timer <<EOF
[Unit]
Description=Snapshot live claude sessions every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF

write_unit claude-recover.service <<EOF
[Unit]
Description=Restore mounts and claude sessions after a reboot
After=$AFTER_UNIT
Wants=$AFTER_UNIT

[Service]
Type=exec
$env_lines
ExecStart=$RECOVER restore --auto
RuntimeMaxSec=${RUNTIME_MAX_MIN}min

[Install]
WantedBy=default.target
EOF

if (( DRY )); then
  printf 'dry-run: symlink %s/claude-recover -> %s\n' "$BIN_DIR" "$RECOVER"
else
  mkdir -p "$BIN_DIR"
  chmod +x "$RECOVER"
  ln -sfn "$RECOVER" "$BIN_DIR/claude-recover"
fi

run "$SYSTEMCTL" --user daemon-reload
run "$SYSTEMCTL" --user enable --now claude-recover-snapshot.timer
run "$SYSTEMCTL" --user enable claude-recover.service

linger="$("$LOGINCTL" show-user "$(id -un)" -p Linger --value 2>/dev/null || true)"
[[ "$linger" == "yes" ]] || echo "warning: linger is off; user units will not start at boot (loginctl enable-linger $(id -un))" >&2
echo "claude-recover installed. Entry point: $BIN_DIR/claude-recover"
