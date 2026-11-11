#!/usr/bin/env bash
# Install the disk-pressure reaper timer (systemd --user), so the reapers run without a Claude session.
#
#   agent-reaper-pressure.timer / .service  every minute: hook-reaper.py --on-pressure
#   agent-reaper-pass.service               hook-reaper.py --force-run (every reaper due; --check uses it)
#   agent-reaper-only@.service              hook-reaper.py --only %i  (one reaper by name, e.g. @temp-residue)
#
# Usage: install-reaper-timer.sh [--dry-run] [--uninstall] [--status] [--check] [--tmpdir PATH]
#   (none)       write the units, daemon-reload, enable --now the timer
#   --dry-run    print the units and the commands; no systemctl call (install and --uninstall only)
#   --uninstall  disable the timer, remove the units
#   --status     installed (exit 0, with the timer's state) or not installed (exit 3)
#   --check      start the pass unit through systemd and judge the run from systemd's answer, the journal and
#                the observation the runner wrote under <agent home>/reaper/unit-pass/ in that run; the
#                manager's own environment is never read
#   --tmpdir P   the temp root frozen into the units (default: $TMPDIR, else the platform default, with a warning)
# Exit: 0 ok; 1 failed install or `check failed: <clause>`; 2 usage; 3 --status: not installed;
#       4 no systemd user manager.
#
# Every unit carries the same frozen TMPDIR and agent home (CLAUDE_CONFIG_DIR and CLAUDE_AGENT_HOME) and
# UnsetEnvironment of the ssh-agent and askpass variables; no removal logic lives in a unit.
# Env seams (tests): SYSTEMD_USER_DIR, SYSTEMCTL_BIN, LOGINCTL_BIN, JOURNALCTL_BIN.
set -euo pipefail
[[ "$(uname -s)" == "Linux" ]] || { echo 'reaper timer units: Linux only' >&2; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
HOOK="$REPO/scripts/hook-reaper.py"
UNIT_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
SYSTEMCTL="${SYSTEMCTL_BIN:-systemctl}"
LOGINCTL="${LOGINCTL_BIN:-loginctl}"
JOURNALCTL="${JOURNALCTL_BIN:-journalctl}"
UNITS=(agent-reaper-pressure.timer agent-reaper-pressure.service agent-reaper-pass.service agent-reaper-only@.service)
UNSET_LINE="UnsetEnvironment=SSH_AUTH_SOCK SSH_AGENT_PID GIT_ASKPASS SSH_ASKPASS"
DRY=0
MODE=install
TMPDIR_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --uninstall) MODE=uninstall ;;
    --status) MODE=status ;;
    --check) MODE=check ;;
    --tmpdir) TMPDIR_ARG="${2:?--tmpdir needs a path}"; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
if (( DRY )) && [[ "$MODE" == status || "$MODE" == check ]]; then
  echo "--dry-run applies to install and --uninstall only" >&2
  exit 2
fi

run() {
  if (( DRY )); then printf 'dry-run: %s\n' "$*"; else "$@"; fi
}

fail_check() {
  echo "check failed: $1: $2" >&2
  exit 1
}

require_manager() {
  local state
  state="$("$SYSTEMCTL" --user is-system-running 2>/dev/null || true)"
  case "$state" in
    running|degraded|starting|stopping|initializing|maintenance) ;;
    *)
      echo "no systemd user manager reachable (systemctl --user is-system-running said: ${state:-nothing}); nothing was changed" >&2
      exit 4
      ;;
  esac
}

real() { python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"; }

unit_value() {
  # unit_value FILE KEY: the value of the first `Environment=KEY=value` line of FILE.
  sed -nE "s/^Environment=$2=(.*)$/\\1/p" "$1" | head -n 1
}

safe_path() {
  [[ "$2" =~ ^[A-Za-z0-9_./+@:,=-]+$ ]] || { echo "$1 '$2' has a character a unit file cannot carry unquoted" >&2; exit 2; }
}

all_installed() {
  local name
  for name in "${UNITS[@]}"; do
    [[ -f "$UNIT_DIR/$name" ]] || { MISSING="$name"; return 1; }
  done
}

if [[ "$MODE" == status ]]; then
  require_manager
  if all_installed; then
    echo "installed: agent-reaper-pressure.timer is-enabled=$("$SYSTEMCTL" --user is-enabled agent-reaper-pressure.timer 2>/dev/null || true) is-active=$("$SYSTEMCTL" --user is-active agent-reaper-pressure.timer 2>/dev/null || true)"
    exit 0
  fi
  echo "not installed (missing $MISSING)"
  exit 3
fi

if [[ "$MODE" == uninstall ]]; then
  (( DRY )) || require_manager
  run "$SYSTEMCTL" --user disable --now agent-reaper-pressure.timer || true
  for name in "${UNITS[@]}"; do run rm -f "$UNIT_DIR/$name"; done
  run "$SYSTEMCTL" --user daemon-reload
  exit 0
fi

if [[ "$MODE" == check ]]; then
  require_manager
  all_installed || fail_check units "$MISSING is not installed in $UNIT_DIR"
  pass_unit="$UNIT_DIR/agent-reaper-pass.service"
  only_unit="$UNIT_DIR/agent-reaper-only@.service"
  env_of() { grep -E '^(Environment|UnsetEnvironment)=' "$1" | sort || true; }
  [[ "$(env_of "$pass_unit")" == "$(env_of "$only_unit")" ]] \
    || fail_check environment "agent-reaper-only@.service carries a different environment than agent-reaper-pass.service"
  frozen_tmp="$(unit_value "$pass_unit" TMPDIR)"
  frozen_home="$(unit_value "$pass_unit" CLAUDE_CONFIG_DIR)"
  [[ -n "$frozen_tmp" && -n "$frozen_home" ]] || fail_check environment "agent-reaper-pass.service freezes no TMPDIR or CLAUDE_CONFIG_DIR"
  if [[ -n "${TMPDIR:-}" && "$(real "$TMPDIR")" != "$frozen_tmp" ]]; then
    echo "warning: this shell's TMPDIR ($TMPDIR) differs from the frozen $frozen_tmp" >&2
  fi

  started=0
  "$SYSTEMCTL" --user start --wait agent-reaper-pass.service || started=$?
  shown="$("$SYSTEMCTL" --user show -p Result -p ExecMainStatus -p InvocationID agent-reaper-pass.service)"
  result="$(sed -n 's/^Result=//p' <<<"$shown")"
  status="$(sed -n 's/^ExecMainStatus=//p' <<<"$shown")"
  invocation="$(sed -n 's/^InvocationID=//p' <<<"$shown")"
  [[ "$result" == success && "$status" == 0 ]] || fail_check result "Result=${result:-?} ExecMainStatus=${status:-?}"
  (( started == 0 )) || fail_check start "systemctl start --wait exited $started although Result=success"
  [[ -n "$invocation" ]] || fail_check observation "systemd reported no InvocationID"

  journal="$("$JOURNALCTL" --user "USER_INVOCATION_ID=$invocation" --no-pager -o cat 2>/dev/null || true)"
  error_line="$(grep -m 1 'reaper: runner error' <<<"$journal" || true)"
  [[ -z "$error_line" ]] || fail_check journal "$error_line"

  observation="$frozen_home/reaper/unit-pass/$invocation.json"
  [[ -f "$observation" ]] || fail_check observation "no $observation for InvocationID $invocation"
  verdict="$(python3 - "$observation" "$frozen_tmp" "$frozen_home" "$invocation" <<'PY'
import json, sys

path, tmp, home, invocation = sys.argv[1:5]
try:
    seen = json.load(open(path))
except (OSError, ValueError) as exc:
    print(f"observation|{path} unreadable: {exc}")
    sys.exit(0)
creds = seen.get("credentials_present") or {}
present = sorted(name for name, flag in creds.items() if flag)
for clause, bad, detail in (
    ("observation", seen.get("invocation_id") != invocation, f"invocation_id {seen.get('invocation_id')!r}"),
    ("tmpdir", seen.get("tmpdir") != tmp, f"observed {seen.get('tmpdir')!r}, frozen {tmp!r}"),
    ("agent_home", seen.get("agent_home") != home, f"observed {seen.get('agent_home')!r}, frozen {home!r}"),
    ("credentials", bool(present) or not creds, f"present: {', '.join(present) or 'flags missing'}"),
    ("reapers_run", not isinstance(seen.get("reapers_run"), int) or seen["reapers_run"] < 1, f"reapers_run={seen.get('reapers_run')!r}"),
    ("errors", seen.get("errors") != 0, f"errors={seen.get('errors')!r}"),
):
    if bad:
        print(f"{clause}|{detail}")
        break
PY
)"
  [[ -z "$verdict" ]] || fail_check "${verdict%%|*}" "${verdict#*|}"

  linger="$("$LOGINCTL" show-user "$(id -un)" -p Linger --value 2>/dev/null || true)"
  [[ "$linger" == yes ]] || fail_check linger "Linger=${linger:-unknown}; user units stop at logout (loginctl enable-linger $(id -un))"
  echo "check passed: InvocationID $invocation, tmpdir $frozen_tmp, agent home $frozen_home"
  exit 0
fi

# install
if [[ -n "$TMPDIR_ARG" ]]; then
  FROZEN_TMP="$(real "$TMPDIR_ARG")"
elif [[ -n "${TMPDIR:-}" ]]; then
  FROZEN_TMP="$(real "$TMPDIR")"
else
  FROZEN_TMP="$(env -u TMPDIR python3 -c 'import os, tempfile; print(os.path.realpath(tempfile.gettempdir()))')"
  echo "warning: neither --tmpdir nor TMPDIR is set; freezing the platform default $FROZEN_TMP, which may not be the sessions' temp root" >&2
fi
FROZEN_HOME="$(PYTHONPATH="$REPO/scripts" python3 -c 'import os; from lib import config_root; print(os.path.realpath(config_root.agent_home()))')"
safe_path "temp root" "$FROZEN_TMP"
safe_path "agent home" "$FROZEN_HOME"
safe_path "repository" "$REPO"
[[ -d "$FROZEN_TMP" ]] || echo "warning: $FROZEN_TMP is not a directory; a run would fall back to another temp root" >&2
PYTHON="$(command -v python3)"
(( DRY )) || require_manager

ENV_BLOCK="Environment=TMPDIR=$FROZEN_TMP
Environment=CLAUDE_CONFIG_DIR=$FROZEN_HOME
Environment=CLAUDE_AGENT_HOME=$FROZEN_HOME
$UNSET_LINE"

write_unit() {
  local name="$1" body
  body="$(cat)"
  if (( DRY )); then
    printf '# %s/%s\n%s\n\n' "$UNIT_DIR" "$name" "$body"
  else
    mkdir -p "$UNIT_DIR"
    printf '%s\n' "$body" >"$UNIT_DIR/$name"
  fi
}

write_unit agent-reaper-pressure.service <<EOF
[Unit]
Description=Run the reapers whose filesystems are short of free space

[Service]
Type=oneshot
$ENV_BLOCK
ExecStart=$PYTHON $HOOK --on-pressure
EOF

write_unit agent-reaper-pressure.timer <<EOF
[Unit]
Description=Check disk pressure and reap abandoned residue every minute

[Timer]
OnCalendar=minutely
Persistent=false
AccuracySec=10s
Unit=agent-reaper-pressure.service

[Install]
WantedBy=timers.target
EOF

write_unit agent-reaper-pass.service <<EOF
[Unit]
Description=One pass of every reaper, due regardless of free space

[Service]
Type=oneshot
$ENV_BLOCK
ExecStart=$PYTHON $HOOK --force-run
EOF

write_unit agent-reaper-only@.service <<EOF
[Unit]
Description=One pass of the reaper %i

[Service]
Type=oneshot
$ENV_BLOCK
ExecStart=$PYTHON $HOOK --only %i
EOF

run "$SYSTEMCTL" --user daemon-reload
run "$SYSTEMCTL" --user enable --now agent-reaper-pressure.timer

if (( ! DRY )); then
  linger="$("$LOGINCTL" show-user "$(id -un)" -p Linger --value 2>/dev/null || true)"
  [[ "$linger" == yes ]] || echo "warning: linger is off; the timer stops at logout (loginctl enable-linger $(id -un))" >&2
  echo "reaper timer installed. Check it: $0 --check"
fi
