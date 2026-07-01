---
name: 2026-07-01-monitor-self-defeats-verify-its-own-run
description: Recurring difficulty — a guard/monitor script crashes on every scheduled run and all its checks go silently dead while the guarded system and the monitor's mere existence both look healthy; the failure only shows in the monitor's own exit status, which nothing watches.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [2026-05-26-agent-system-plan-vs-reality-drift.md, macos-shell-portability-gotchas.md]
created: 2026-07-01
last_verified: 2026-07-01
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

## Cost
~$5.76 developer spawn + ~30 min wall-clock incl. this fix (unplanned but blocking).

## Self-critique of the agent system
The SKILL.md Monitoring section described the watchdog as live protection, but its own liveness was never checked — a plan-vs-reality drift I'd have missed had the developer not read ExecMainStatus. Generalize: when a monitor IS the safety net, its liveness must itself be spot-checked, or silent self-defeat is undetectable.
