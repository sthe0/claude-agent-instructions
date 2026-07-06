---
name: 2026-07-01-monitor-self-defeats-verify-its-own-run
description: Recurring difficulty — a guard/monitor script crashes on every scheduled run and all its checks go silently dead while the guarded system and the monitor's mere existence both look healthy; the failure only shows in the monitor's own exit status, which nothing watches.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [2026-05-26-agent-system-plan-vs-reality-drift.md, macos-shell-portability-gotchas.md]
created: 2026-07-01
last_verified: 2026-07-06
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

### 2026-07-06 — "user still sees stale topics" was CLIENT CACHE, not a server miss; and hardening the periodic sweep
- Where it arose: same ccgram-topic-cleanup task. After Stage 1's exhaustive sweep the user still reported "вижу кучу старых топиков". I re-swept, widened the id ceiling (probe current-max-msg-id via send+delete, covered 2..29048) — server was provably clean (only ever 1 deletable topic; all else TOPIC_ID_INVALID). Root cause: **Telegram caches the forum-topic list on the client**; deleted topics keep showing until a hard refresh. Decisive move was NOT another server sweep but a **user-side tap-test** (tap a ghost topic → it reports "deleted" and vanishes / Clear Cache). Reconciliation rule: when the API says clean but the user sees stale, suspect the **client view**, not the server — get a cheap user observation before more server work. The user confirmed the ghosts cleared on tap.
- Working plan (Stage 2 replan): hardened the periodic sweep after verification exposed two real defects. (1) It re-probed all ~109 logged ids every 45-min tick over flaky IPv6 → **4m17s runs**; fix = a persisted **DONE set** of confirmed-gone ids (thread ids are never reused, so skip forever) → steady-state **0.68s**, live ids never enter DONE so a later-orphaned window is still caught. (2) A slow run **outlasted the timer period → two concurrent runs** hammering the API and racing the DONE file; fix = non-blocking **flock** (second run logs SKIP, exits 0) + `TimeoutStartSec=1800` backstop. Also removed a **secret leak**: the bot token was interpolated into the curl URL → visible in `ps`/`/proc/<pid>/cmdline`; fix = feed it via `printf ... | curl -K -` (config on stdin; printf is a builtin so no argv either). Verified empirically: live curl argv = `curl -K -`; flock SKIP path exercised; guards intact (skipped_live=6 == live set == tmux windows).
- Coordination lesson: at `replan` the engine re-armed BOTH a fresh **plan-review** (bound to the new plan hash) and a **critique-coverage** gate (the critique's `invariants_to_preserve` must appear near-verbatim in a stage's invariants). Pitfall hit: I first told the plan-reviewer to check **plan-vs-code match**, but the new script is intentionally **not yet written** (gated behind that very approval) → false "revise" on by-design mismatch. Rule: at replan-time plan-review, judge the **plan design only**; implementation follows approval.

## Common core & variations
**Common:** A scheduled job that LOOKS installed (unit enabled, timer active) but whose every run dies before doing anything, visible only in an exit status nothing watches. Here the script's fail-safe guard (empty live-set -> abort, so it can never over-delete) fired on every run because 'ccgram status' produced empty output: systemd --user starts services with a minimal PATH that excludes ~/.local/bin where the uv-installed ccgram binary lives. The guard did its job (no damage), but the auto-clean would have silently never worked.

**Variations:** Detection that saved it: the plan required a MANUAL 'systemctl --user start <svc>' verify step, not just enable+timer — the manual run surfaced ExitStatus=1 + the 'ABORT: empty live set' log line immediately, instead of discovering months later that topics never auto-pruned. Fix: export PATH=HOME/.local/bin:HOME/bin:/usr/bin:/bin at the top of any script a systemd --user unit runs. Reusable rule: never trust enable+timer as proof a --user job works; trigger it once by hand and read its exit status + log. Twin lesson from the same task: Telegram Bot API has no list-forum-topics method, and the VPS egress to api.telegram.org is IPv6-only and flaky (~20% empty responses); a complete deleteForumTopic range sweep therefore needs bounded parallelism + empty-response retry, else ~20% of ids are silently unverified.

## Cost
~$5.76 developer spawn + ~30 min wall-clock incl. this fix (unplanned but blocking).

## Self-critique of the agent system
The SKILL.md Monitoring section described the watchdog as live protection, but its own liveness was never checked — a plan-vs-reality drift I'd have missed had the developer not read ExecMainStatus. Generalize: when a monitor IS the safety net, its liveness must itself be spot-checked, or silent self-defeat is undetectable.
