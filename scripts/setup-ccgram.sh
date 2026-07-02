#!/usr/bin/env bash
# Bootstrap CCGram on a new machine (or pick up an existing install).
#
# Idempotent. Safe to rerun. Does NOT touch the bot token or other secrets in
# ~/.ccgram/.env — only creates a placeholder if missing.
#
# What it does:
#   1. Install uv (if not present).
#   2. Install ccgram via `uv tool install ccgram` (or upgrade if --upgrade).
#   3. Verify tmux is installed (does not install — apt/brew sudo prompts vary).
#   4. Create ~/.ccgram/ + a placeholder ~/.ccgram/.env if it doesn't exist.
#   5. Register autostart (launchd on macOS, systemd --user on Linux).
#   6. Print the BotFather + group manual-setup checklist.
#
# Manual steps the script CANNOT do (Telegram limitation):
#   - Create the bot via BotFather.
#   - Configure /setjoingroups Enable, /setprivacy Disable.
#   - Create the forum group and add the bot as admin with Manage Topics +
#     Pin Messages.
#   - Fetch the group ID (use https://api.telegram.org/bot<TOKEN>/getUpdates
#     after sending any message in the group).
#
# Usage:
#   setup-ccgram.sh             # bootstrap
#   setup-ccgram.sh --upgrade   # upgrade ccgram to the latest version
#
# See $CLAUDE_AGENT_HOME/skills/ccgram-management/SKILL.md for ongoing-ops reference.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib/config-root.sh"

UPGRADE=0
if [[ "${1:-}" == "--upgrade" ]]; then
  UPGRADE=1
fi

OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM=mac ;;
  Linux)  PLATFORM=linux ;;
  *) echo "unsupported OS: $OS" >&2; exit 2 ;;
esac

say() { echo "[setup-ccgram] $*"; }

# 1. uv
if ! command -v uv >/dev/null 2>&1; then
  if [[ "$PLATFORM" == "mac" ]] && command -v brew >/dev/null 2>&1; then
    say "installing uv via brew"
    brew install uv
  else
    say "installing uv via the official installer"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # The installer puts uv in ~/.local/bin
    export PATH="$HOME/.local/bin:$PATH"
  fi
else
  say "uv already installed: $(uv --version)"
fi

# 2. ccgram
if [[ "$UPGRADE" -eq 1 ]]; then
  say "upgrading ccgram"
  uv tool upgrade ccgram
elif command -v ccgram >/dev/null 2>&1 || [[ -x "$HOME/.local/bin/ccgram" ]]; then
  say "ccgram already installed: $("$HOME/.local/bin/ccgram" --version 2>/dev/null || ccgram --version)"
else
  say "installing ccgram via uv"
  uv tool install ccgram
fi
export PATH="$HOME/.local/bin:$PATH"

# 3. tmux check
if ! command -v tmux >/dev/null 2>&1; then
  cat <<'EOF' >&2

WARNING: tmux is not installed. CCGram needs tmux at runtime.

Install it with your OS package manager:
  macOS:  brew install tmux
  Ubuntu: sudo apt install tmux
  Yandex / RHEL-like: sudo yum install tmux

Re-run this script after.
EOF
  exit 3
fi
say "tmux: $(tmux -V)"

# 4. ~/.ccgram + .env placeholder
mkdir -p "$HOME/.ccgram"
if [[ ! -f "$HOME/.ccgram/.env" ]]; then
  cat > "$HOME/.ccgram/.env" <<'EOF'
# CCGram per-machine secrets. Edit before first run.
# Get TELEGRAM_BOT_TOKEN from @BotFather (/newbot).
# Get ALLOWED_USERS from @userinfobot (your numeric user id; comma-separated for many).
# Get CCGRAM_GROUP_ID via https://api.telegram.org/bot<TOKEN>/getUpdates after the bot is in the group.

TELEGRAM_BOT_TOKEN=
ALLOWED_USERS=
CCGRAM_GROUP_ID=
EOF
  chmod 600 "$HOME/.ccgram/.env"
  say "created placeholder ~/.ccgram/.env (mode 600) — fill in before running"
else
  say "~/.ccgram/.env already exists, leaving as-is"
fi

# 5. Autostart
if [[ "$PLATFORM" == "mac" ]]; then
  PLIST="$HOME/Library/LaunchAgents/com.ccgram.daemon.plist"
  if [[ ! -f "$PLIST" ]]; then
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ccgram.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>$HOME/.local/bin/ccgram</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key>
    <string>$HOME/.ccgram/ccgram.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.ccgram/ccgram.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
EOF
    say "wrote $PLIST"
  else
    say "$PLIST already exists, leaving as-is"
  fi
  if launchctl print "gui/$UID/com.ccgram.daemon" >/dev/null 2>&1; then
    say "launchd unit already loaded; kickstarting"
    launchctl kickstart -k "gui/$UID/com.ccgram.daemon" || true
  else
    say "loading launchd unit"
    launchctl bootstrap "gui/$UID" "$PLIST"
    launchctl enable "gui/$UID/com.ccgram.daemon"
    launchctl kickstart -k "gui/$UID/com.ccgram.daemon" || true
  fi
else
  # Linux: systemd --user
  UNIT_DIR="$HOME/.config/systemd/user"
  UNIT="$UNIT_DIR/ccgram.service"
  mkdir -p "$UNIT_DIR"
  if [[ ! -f "$UNIT" ]]; then
    # Detect nvm path for the0.fun-style nvm-installed Claude Code.
    NVM_BIN=""
    for candidate in "$HOME/.nvm/versions/node/"*/bin; do
      [[ -d "$candidate" ]] && NVM_BIN="$candidate" && break
    done
    PATH_LINE="$HOME/.local/bin"
    [[ -n "$NVM_BIN" ]] && PATH_LINE="$PATH_LINE:$NVM_BIN"
    PATH_LINE="$PATH_LINE:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    cat > "$UNIT" <<EOF
[Unit]
Description=CCGram Telegram bridge for Claude Code
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$HOME/.local/bin/ccgram
Restart=on-failure
RestartSec=10
StandardOutput=append:%h/.ccgram/ccgram.log
StandardError=append:%h/.ccgram/ccgram.log
Environment=PATH=$PATH_LINE

[Install]
WantedBy=default.target
EOF
    say "wrote $UNIT"
  else
    say "$UNIT already exists, leaving as-is"
  fi
  systemctl --user daemon-reload
  systemctl --user enable --now ccgram.service
  say "systemd unit enabled and started"
  linger="$(loginctl show-user "$USER" 2>/dev/null || true)"
  if ! printf '%s' "$linger" | grep -q '^Linger=yes'; then
    cat <<EOF >&2

NOTE: linger is not enabled for this user. Without linger, the systemd-user
instance stops on last logout and ccgram will not survive a reboot. Run once:

    sudo loginctl enable-linger $USER

This requires sudo and only needs to be done once per machine.
EOF
  fi
fi

# 6. Hook install
if [[ -f "$CLAUDE_AGENT_HOME/settings.json" || -f "$HOME/.claude/settings.json" ]] || command -v claude >/dev/null 2>&1; then
  say "installing Claude Code hooks (idempotent)"
  ccgram hook --install || say "hook install failed — non-fatal, run manually later"
else
  say "Claude Code not detected — skip hook install (run 'ccgram hook --install' after installing Claude Code)"
fi

# 7. BotFather checklist
cat <<'EOF'

============================================================
Bootstrap done. Manual Telegram steps (per machine / per bot):

  1. @BotFather → /newbot → set name, get TELEGRAM_BOT_TOKEN.
  2. @BotFather → /setjoingroups → pick bot → Enable.
  3. @BotFather → /setprivacy   → pick bot → Disable.
  4. Create a Telegram group, enable Topics (Group settings → Topics).
  5. Add the bot to the group; promote to Administrator with:
        - Manage Topics  (must be on)
        - Pin Messages
  6. Get the group id:
        curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
     (send any message in the group first; look for chat.id starting with -100).
  7. Fill ~/.ccgram/.env with the three values.
  8. Restart the daemon:
        macOS:  launchctl kickstart -k gui/$UID/com.ccgram.daemon
        Linux:  systemctl --user restart ccgram.service
  9. Open the group in Telegram, create a new topic, send a message —
     ccgram should reply with a directory browser.
============================================================
EOF
