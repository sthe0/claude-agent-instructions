#!/usr/bin/env bash
# Install the nightly background debt cycle (systemd --user): agent-debt-cycle.timer runs
# scripts/mandate-cycle.py once a day at 01:00 under the standing mandate.
#
# Usage: install-mandate-timer.sh [--write-env] [--uninstall]
#   (none)       write the env file and both units, then enable and start the timer
#   --write-env  write only ~/.config/agent-debt-cycle/env (no unit, no systemctl)
#   --uninstall  disable the timer and remove both units (the env file and the log stay)
#
# The env file carries PATH and the agent config-root / gh config-dir variables when they are
# set in this shell -- never a token. The cycle reads its own credentials from the gh config dir.
# Env seams (tests): SYSTEMD_USER_DIR, SYSTEMCTL_BIN; the rest hangs off $HOME.
set -euo pipefail
[[ "$(uname -s)" == "Linux" ]] || { echo 'agent-debt-cycle timer: Linux only' >&2; exit 1; }

UNIT_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
SYSTEMCTL="${SYSTEMCTL_BIN:-systemctl}"
ENV_DIR="$HOME/.config/agent-debt-cycle"
ENV_FILE="$ENV_DIR/env"
UNITS=(agent-debt-cycle.service agent-debt-cycle.timer)
WRITE_ENV=0
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --write-env) WRITE_ENV=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if (( UNINSTALL )); then
  "$SYSTEMCTL" --user disable --now agent-debt-cycle.timer || true
  for unit in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$unit"; done
  "$SYSTEMCTL" --user daemon-reload
  echo "agent-debt-cycle timer removed. Env file and log are left in place."
  exit 0
fi

# The unit's PATH is ordered by priority: the interpreter's directory first, so the cycle runs
# under the same python3 (and its site-packages) this shell resolves, then the other tools, then
# the usual fallbacks -- each directory once. ExecStart names that interpreter outright, so the
# one binary that matters never depends on PATH order.
path_dirs=()
add_dir() {
  local d
  for d in ${path_dirs[@]+"${path_dirs[@]}"}; do [[ "$d" == "$1" ]] && return 0; done
  path_dirs+=("$1")
}
PYTHON3=""
for bin in python3 gh git claude; do
  p="$(command -v "$bin" 2>/dev/null || true)"
  if [[ -n "$p" && "$p" == /* ]]; then
    [[ "$bin" == python3 ]] && PYTHON3="$p"
    add_dir "$(dirname "$p")"
  else
    echo "warning: $bin not found on PATH; the cycle will not see it" >&2
  fi
done
[[ -n "$PYTHON3" ]] || { echo 'agent-debt-cycle timer: python3 not found on PATH; cannot pin ExecStart' >&2; exit 1; }
for d in "$HOME/.local/bin" /usr/local/bin /usr/bin /bin; do add_dir "$d"; done
UNIT_PATH="$(IFS=:; printf '%s' "${path_dirs[*]}")"

mkdir -p "$ENV_DIR"
{
  printf 'PATH=%s\n' "$UNIT_PATH"
  for var in CLAUDE_AGENT_HOME CLAUDE_CONFIG_DIR GH_CONFIG_DIR; do
    [[ -n "${!var:-}" ]] && printf '%s=%s\n' "$var" "${!var}"
  done
} >"$ENV_FILE"
chmod 600 "$ENV_FILE"
echo "Wrote $ENV_FILE"
(( WRITE_ENV )) && exit 0

mkdir -p "$UNIT_DIR" "$HOME/.local/log"
cat >"$UNIT_DIR/agent-debt-cycle.service" <<EOF
[Unit]
Description=Background debt cycle under the standing mandate

[Service]
Type=oneshot
WorkingDirectory=%h/claude-agent-instructions
EnvironmentFile=%h/.config/agent-debt-cycle/env
ExecStart=$PYTHON3 %h/claude-agent-instructions/scripts/mandate-cycle.py run
StandardOutput=append:%h/.local/log/agent-debt-cycle.log
StandardError=append:%h/.local/log/agent-debt-cycle.log
EOF
cat >"$UNIT_DIR/agent-debt-cycle.timer" <<'EOF'
[Unit]
Description=Nightly background debt cycle

[Timer]
OnCalendar=*-*-* 01:00:00
Persistent=false

[Install]
WantedBy=timers.target
EOF
"$SYSTEMCTL" --user daemon-reload
"$SYSTEMCTL" --user enable --now agent-debt-cycle.timer
echo "Timer enabled. Log: ~/.local/log/agent-debt-cycle.log. Kill switch: agentctl mandate-stop"
