---
name: ccgram-management
description: Operate the CCGram Telegram bridge that controls Claude Code (and other agent CLI) sessions on this user's machines. TRIGGER when the user mentions ccgram, the Telegram bridge, the Claude–Mac / Claude–Personal VPS / Claude–Work VPS groups, asks to restart / check / reinstall the bridge, debug a missing topic, or set up the bridge on a new machine. SKIP for unrelated Telegram bot work.
---

# CCGram management

CCGram bridges Telegram ↔ tmux on each of the user's machines. Each machine runs its own `ccgram` daemon, which connects to its own Telegram bot in its own forum-style group; tmux windows on the machine map to topics in the group.

## What lives where (per machine)

| Path | Purpose |
|---|---|
| `~/.ccgram/.env` | Bot token + allowed user ID + group ID. **Secret** — never log, copy, or commit. Per-machine: `TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`, `CCGRAM_GROUP_ID`. |
| `~/.ccgram/ccgram.log` | Stdout / stderr of the daemon. First place to look when something is wrong. |
| `~/.ccgram/events.jsonl` | Append-only event log written by hook events. |
| `~/.claude/settings.json` | Claude Code hook entries — installed by `ccgram hook --install`. Adds 9 events: `SessionStart`, `Notification`, `Stop`, `StopFailure`, `SessionEnd`, `SubagentStart`, `SubagentStop`, `TeammateIdle`, `TaskCompleted`. Does not touch existing `PreToolUse` / `PostToolUse` / `UserPromptSubmit` entries. |
| `~/.local/bin/ccgram` | The CLI binary, installed via `uv tool install ccgram`. |

## Per-machine setup at a glance

| Machine | Autostart | tmux | Notes |
|---|---|---|---|
| Mac (current laptop) | `launchd`: `~/Library/LaunchAgents/com.ccgram.daemon.plist`, label `com.ccgram.daemon` | brew-installed | `claude` from `/opt/homebrew/bin/claude` |
| Linux VPSes (`the0.fun`, `the0.klg.yp-c.yandex.net`) | `systemd --user`: `~/.config/systemd/user/ccgram.service`. Requires `sudo loginctl enable-linger the0` to persist past logout. | apt-installed | On `the0.fun`, `claude` is under `~/.nvm/...` — only available in interactive bash (`bash -ic` / `bash -lc`). The systemd unit explicitly adds the nvm bin to PATH. |

## Common operations

### Check status

- **Mac**: `launchctl print gui/$UID/com.ccgram.daemon | grep -E 'state|pid'`
- **Linux**: `systemctl --user status ccgram.service`
- Log tail (any machine): `tail -n 50 ~/.ccgram/ccgram.log`
- Doctor (any machine): `ccgram doctor` — checks tmux, agent binaries, env, hooks, orphaned windows.

### Restart

- **Mac**: `launchctl kickstart -k gui/$UID/com.ccgram.daemon`
- **Linux**: `systemctl --user restart ccgram.service`

### Stop / start

- **Mac**: `launchctl bootout gui/$UID/com.ccgram.daemon` / `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.ccgram.daemon.plist`
- **Linux**: `systemctl --user stop ccgram.service` / `systemctl --user start ccgram.service`

### Install / re-install Claude Code hooks

`ccgram hook --install` (idempotent — reports "N new, M already present"). To remove: `ccgram hook --uninstall`. Status: `ccgram hook --status`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `BadRequest: Not enough rights to create a topic` | Bot lacks `can_manage_topics` admin permission in the group | In Telegram: group → Administrators → bot → enable **Manage Topics** → save (the toggle is sometimes off-screen and easy to miss). Verify via `curl https://api.telegram.org/bot<TOKEN>/getChatMember?chat_id=<id>&user_id=<bot_id>`. |
| `getUpdates` returns empty `result: []` | Bot's Group Privacy is still ON, or bot was added before `/setprivacy → Disable` took effect | Re-add the bot to the group after toggling `/setprivacy → Disable` in BotFather. Privacy change does not apply retroactively. |
| `claude not found in PATH` from `ccgram doctor` | nvm not loaded in non-interactive shell | Use `bash -ic` or `bash -lc` to run doctor; the systemd unit on `the0.fun` already adds nvm bin to PATH. |
| Topics appear but no message routing | Hooks not installed | Run `ccgram hook --install`. |
| Daemon exits or restarts repeatedly | Look in `~/.ccgram/ccgram.log` first | Common: revoked bot token (re-issue in BotFather + update `.env`), or one ccgram daemon polling the same bot's `getUpdates` twice. |
| Bridge "отвалилась" but daemon shows `active` and `getMe` returns 200 | **Disk filled up (`OSError: [Errno 28] No space left on device`)** — background loops (`Status poll loop`, topic autoclose) crash and do **not** self-resurrect; the main process stays `active` so `status` looks healthy. Also: stale tmux window → topic binding after a session ended ("Could not probe same-name topic … not rebinding"). | `grep -c 'No space left' ~/.ccgram/ccgram.log` and check `df -h /` **first**. Free disk, then `systemctl --user restart ccgram.service` (Linux) / `launchctl kickstart -k` (Mac) — a restart is the only thing that revives the dead loops and re-binds windows. Transient `Reset Telegram polling … TimedOut` lines that self-recover are **not** this — they are flaky IPv6 egress to `api.telegram.org`, not a daemon bug. |

## Monitoring (the0.klg host)

`~/bin/ccgram-watchdog.sh` + `ccgram-watchdog.timer` (systemd --user, every 10 min) alert into the bridge's own Telegram group on: disk `/` ≥ 90%, `No space left` in the log, ≥ 10 polling timeouts/hour, or a dead daemon. Anti-spam: re-alerts a standing condition at most once per 2 h, sends a `✓` on recovery. Timeout count is parsed from `ccgram.log` (dateless `HH:MM:SS`, UTC) via `tail -n 2000` + awk windowing — the daemon's structlog output does **not** reach the systemd journal, so `journalctl` cannot be used for it. Not yet deployed to Mac / `the0.fun`.

## Set up a new machine

Use `scripts/setup-ccgram.sh` from this repo. It installs `uv` (if missing), `ccgram` via `uv tool install`, writes a placeholder `.env`, optionally installs the Claude Code hook, and registers the autostart unit (`launchd` on macOS, `systemd --user` on Linux).

The BotFather steps (create bot, configure `/setjoingroups Enable` and `/setprivacy Disable`, create forum group, promote bot with **Manage Topics** + **Pin Messages**) must be done manually in Telegram — Bot API does not allow programmatic bot creation. The script prints the exact checklist at the end.

## Files / facts NOT in memory

The mapping (machine → bot username → group ID) lives **only in each machine's `~/.ccgram/.env`** by user preference. Do not copy it into memory leaves. The bot tokens are secrets and must never leave that file. If you need the values, read them on the machine that owns them.
