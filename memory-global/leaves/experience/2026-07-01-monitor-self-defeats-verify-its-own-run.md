---
name: 2026-07-01-monitor-self-defeats-verify-its-own-run
description: Recurring difficulty — a guard/monitor script crashes on every scheduled run and all its checks go silently dead while the guarded system and the monitor's mere existence both look healthy; the failure only shows in the monitor's own exit status, which nothing watches.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [2026-05-26-agent-system-plan-vs-reality-drift.md, macos-shell-portability-gotchas.md]
created: 2026-07-01
last_verified: 2026-07-05
---

# A watchdog that silently crashes defeats every check — verify the monitor's OWN run, not just its presence

## Difficulty
The ccgram watchdog had been exiting 1 on every 10-min systemd tick (for hours+), so all four checks (disk/ENOSPC/timeouts/daemon-down) were non-functional — yet the daemon status and the watchdog's presence both looked fine. Root cause: a 'grep | awk' pipeline under 'set -euo pipefail' aborts when grep finds ZERO matches (exit 1) — the normal healthy case — so the script died before any check ran. Invisible because the failure surface is the watchdog's OWN exit status, which nothing monitored (who-watches-the-watchdog gap).

## Order & criterion
Read the monitor's own run result BEFORE trusting its silence as 'all clear': systemctl --user show <svc> -p ExecMainStatus -p Result (or the timer's last-run status). Only then does absence-of-alert mean healthy. Shell fix for the class: guard every zero-match-legal grep in a pipe as '{ grep -E ... || true; } | ...' so a healthy zero-match doesn't kill the pipeline under pipefail.

**Acceptance check:** systemctl --user show ccgram-watchdog.service -p ExecMainStatus -p Result => Result=success ExecMainStatus=0 (was exit-code 1); bash -n + shellcheck clean; a healthy (zero-match) log no longer aborts the run.

## Contexts

### 2026-07-01 — initial
- Where it arose: ccgram watchdog (~/bin/ccgram-watchdog.sh) on klg Work VPS; bash 'set -euo pipefail'; grep zero-match in a pipe. Surfaced while adding a poll-loop-liveness check to the same script.
- Working plan: Hardened the crashing 'grep|awk' pipelines with '{ grep||true; }'; re-verified shellcheck/bash -n; confirmed the timer service now exits 0. Then added the intended new check on top.


### 2026-07-05 — systemd --user job silently aborts because minimal service PATH omits ~/.local/bin (ccgram)
- Where it arose: ccgram-topic-sweep timer (the0.klg): periodic orphan-topic prune
- Working plan: ccgram-topic-cleanup: stage1 one-time range sweep, stage2 persistent prune + timer

## Common core & variations
**Common:** A scheduled job that LOOKS installed (unit enabled, timer active) but whose every run dies before doing anything, visible only in an exit status nothing watches. Here the script's fail-safe guard (empty live-set -> abort, so it can never over-delete) fired on every run because 'ccgram status' produced empty output: systemd --user starts services with a minimal PATH that excludes ~/.local/bin where the uv-installed ccgram binary lives. The guard did its job (no damage), but the auto-clean would have silently never worked.

**Variations:** Detection that saved it: the plan required a MANUAL 'systemctl --user start <svc>' verify step, not just enable+timer — the manual run surfaced ExitStatus=1 + the 'ABORT: empty live set' log line immediately, instead of discovering months later that topics never auto-pruned. Fix: export PATH=HOME/.local/bin:HOME/bin:/usr/bin:/bin at the top of any script a systemd --user unit runs. Reusable rule: never trust enable+timer as proof a --user job works; trigger it once by hand and read its exit status + log. Twin lesson from the same task: Telegram Bot API has no list-forum-topics method, and the VPS egress to api.telegram.org is IPv6-only and flaky (~20% empty responses); a complete deleteForumTopic range sweep therefore needs bounded parallelism + empty-response retry, else ~20% of ids are silently unverified.

## Cost
~$5.76 developer spawn + ~30 min wall-clock incl. this fix (unplanned but blocking).

## Self-critique of the agent system
The SKILL.md Monitoring section described the watchdog as live protection, but its own liveness was never checked — a plan-vs-reality drift I'd have missed had the developer not read ExecMainStatus. Generalize: when a monitor IS the safety net, its liveness must itself be spot-checked, or silent self-defeat is undetectable.
