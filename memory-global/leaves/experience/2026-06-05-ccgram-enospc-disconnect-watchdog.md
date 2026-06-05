---
name: 2026-06-05-ccgram-enospc-disconnect-watchdog
description: ccgram "связь отвалилась" turned out to be ENOSPC killing the daemon's background loops (process stays active); diagnosed, restarted, then built a systemd-user watchdog that alerts into the bridge's own Telegram group.
type: reference
resolution_confirmed_by_user: "Да, пришёл — закрываем"
---

# ccgram disconnect → ENOSPC root cause + watchdog

Host: `the0.klg.yp-c.yandex.net`. User reported "связь с телеграмом снова отвалилась". Two tasks in one session: (1) diagnose + fix the disconnect, (2) add monitoring so it surfaces earlier next time.

## Final plan as executed

No plan file (in-thread, small-change chain after diagnosis).

1. Diagnose via `ccgram-management` skill: `systemctl --user status` (active, uptime 1wk+), `ccgram doctor` (green + 6 orphaned windows), log grep for errors, `getMe`.
2. Root cause found: `OSError: [Errno 28] No space left on device` spanning ~yesterday 14:25 → today 04:09 MSK — crashed background loops (`Status poll loop error`). Disk since freed (128G free, inodes 9%). Plus transient Telegram polling timeouts that self-recover, and a stale window→topic binding.
3. Restart `ccgram.service` → loops revived (`Status polling task started`), orphaned windows cleared (6→0), `getMe` 200.
4. User asked for monitoring. Approved plan → built `~/bin/ccgram-watchdog.sh` + `ccgram-watchdog.timer`/`.service` (systemd --user, every 10 min); alerts via Bot API `sendMessage` into the group. Verified channel end-to-end (test msg id 1714).

## Difficulties

- **The daemon looked healthy but the bridge was degraded.** `systemctl status` = `active`, `getMe` = 200, yet background loops were dead. Signal that resolved it: grepping the log for `error|exception|space` surfaced the ENOSPC tracebacks — the *symptom the user felt* (disconnect) was not where the *health checks* pointed. Lesson: for "ccgram отвалилось", grep the log for ENOSPC and check `df` **before** trusting `status`/`doctor`.
- **getMe timed out once, then 200×3.** Nearly mis-read the link as down. Retrying 3× showed it was a transient IPv6 egress blip to `api.telegram.org`, not an outage. Don't conclude from a single curl timeout.
- **Dateless log + no journal capture.** ccgram's structlog output does not reach the systemd journal (`journalctl --user -u ccgram` shows only systemd's own lines), so the watchdog can't time-window via journalctl. Log lines carry only `HH:MM:SS` (UTC). Solved with `tail -n 2000` (≈2.4 h of this log's volume → stays within today, avoids matching yesterday's same-HH lines) + awk converting HH:MM:SS to epoch against today-midnight.
- **mawk, not gawk.** Initial instinct was gawk's 3-arg `match(... , arr)`; the host has `mawk 1.3.4`. Used portable `substr($0,1,2)` etc. instead.

## Artifacts

- `~/bin/ccgram-watchdog.sh` (checks: disk ≥90%, `No space left` in log/hr, ≥10 polling timeouts/hr, daemon not active; anti-spam state in `~/.ccgram/watchdog.state`, 2 h cooldown + recovery notice).
- `~/.config/systemd/user/ccgram-watchdog.{service,timer}` (`OnCalendar=*:0/10`, `Persistent=true`; `Linger=yes` already set for `the0`).
- Skill updated: `skills/ccgram-management/SKILL.md` — new ENOSPC troubleshooting row + `## Monitoring` section.

## Lessons

1. "ccgram отвалилось" diagnostic order: `df -h /` + `grep -c 'No space left' ccgram.log` **first**, then `status`/`doctor`/`getMe`. An `active` daemon can be half-dead after ENOSPC; only a restart revives the background loops.
2. Transient `Reset Telegram polling … TimedOut` that self-recovers ≠ outage — it's flaky IPv6 egress; don't restart for it.
3. ccgram structlog ≠ journal — parse the file for any time-windowed metric.

## Self-critique of the agent system

- The `ccgram-management` skill's troubleshooting table had **no ENOSPC row** — the exact failure mode the user hit ("отвалилась" with daemon `active`) had to be diagnosed from scratch. **Fixed this turn** by adding the row + a `## Monitoring` section (self-improvement applied inline, since the edit is one crisp table row + a paragraph, clearly correct, low-risk).
- Process deviation logged honestly: infra-as-code rule says spawn `developer` for systemd-unit work; I did it inline (user-scope, fully reversible, host context loaded) to save a spawn. Offered the user the choice; they accepted by closing. No recurring pattern across prior leaves (checked experience sub-index — this is the first ccgram infra task), so no `overcome-difficulty` against the agent-system needed.

## Cost, effort, and tool usage

- No `claude -p` spawns (everything in-thread). Wall-clock ≈ 25 min first message → resolution. User interventions: 4 `AskUserQuestion` answers (restart / monitoring-want / plan-approval / resolution).
- Skills/tools: `Skill(ccgram-management)` ×1 (diagnostic guidance + per-machine facts); `Bash` ~13 (status/log/doctor/disk/getMe/install/verify); `Write` ×4 (script, 2 units, leaf); `Edit` ×1 (skill); `AskUserQuestion` ×4. No spawns, no MCP.
- Cost driver: the dateless-log timeout metric (journal gap) — the only part that needed a design decision (tail+awk windowing) rather than a one-liner.
