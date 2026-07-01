---
name: 2026-07-01-2026-07-01-engine-spawn-proc-tree-macos-and-rm-empty-var
description: Two macOS-surfaced difficulties in one substantive task. (1) agentctl dispatch auto-spawns via spawn-specialist.py, whose proc_tree.py cleanup reads Linux-only /proc; on macOS kill_tree raises FileNotFoundError so every spawn returns rc=1 with no marker and the engine mis-reports a completed stage as a spawn failure. (2) A live-verification cleanup rm -rf built with an interpolated $HASH that turned out empty collapsed to rm -rf $HOME/.claude/projects/ and deleted the personal auto-memory + all transcripts.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "Да, решена"
refs: [scripts/hook-guard-destructive-rm.py, scripts/proc_tree.py, 2026-06-24-developer-marker-not-on-line-1-false-block.md]
created: 2026-07-01
last_verified: 2026-07-01
---

# Engine specialist-spawn breaks on macOS (proc_tree /proc), and destructive rm from an empty var wiped ~/.claude

## Difficulty
On macOS the engine's spawn path (spawn-specialist.py -> proc_tree.py) is broken (/proc absent) so specialist stages appear to fail though the work completes; and a destructive command interpolating a variable that can be empty silently deletes agent-critical dirs.

## Order & criterion
Verify a stage's actual work independently when the spawn wrapper reports failure without a marker; route specialist work through the in-harness Agent tool as the working spawn path on macOS; never build rm -rf (or any destructive cmd) from an interpolated path var without a non-empty guard.

**Acceptance check:** measurable: hermetic tests + isolated live e2e green for the feature; guard hook denies the exact incident command and allows legit temp deletes (19/19).

## Contexts

### 2026-07-01 — macOS engine spawn + destructive-rm cleanup
- Where it arose: macOS; ~/claude-agent-instructions engine (agentctl dispatch), spawn-specialist.py, proc_tree.py; live verification of claude-task --init.
- Working plan: Recorded stage results manually after independent verification; routed stages 2-5 via Agent tool; recovered memory (index verbatim + reconstructed leaves, Time Machine for full bodies); codified an rm guard (hook-guard-destructive-rm.py + CLAUDE.md rule); filed proc_tree.py as a follow-up.


### 2026-07-01 — Full macOS-portability audit + fix of Core (follow-up resolving the /proc crash)
- Where it arose: ~/claude-agent-instructions; 8 audited defects: proc_tree.py, sync-instructions-repo.sh, doctor.sh, verify-instructions-sync.sh, cursor/scripts/{migrate-cursor-namespace,install-cursor-links}.sh, install-sync-systemd-timer.sh, setup-ccgram.sh, hook-arc-mount-search-guard.py
- Working plan: Explore-agent audit (grep BSD/GNU/bash-3.2/proc idioms) -> planner TOML (4 stages) -> approval -> stage1 via harness Agent (spawn-specialist.py itself broken on macOS = bootstrapping), stages 2-4 parallel via Agent on disjoint files -> per-stage independent verify + engine record-result -> final checks incl. live reap.

## Common core & variations
**Common:** The reliable macOS fix is a single portable primitive over a platform branch: replace the /proc PPID walk with one 'ps -Ao pid=,ppid=,pgid=' snapshot (identical columns macOS+Linux, no psutil) serving _all_pids/_ppid/_group_members. The engine's dispatch spawns via spawn-specialist.py -> proc_tree.kill_tree (the thing being fixed), so stage 1 must run through the harness Agent tool not engine spawn (bootstrapping). Verify the RUNTIME axis: a live launch_supervised()+kill_tree() of a real subtree must leave 0 survivors — static grep/imports insufficient.

**Variations:** The live reap test surfaced a second macOS-only bug invisible to static review: after SIGTERM the group leader briefly becomes a zombie that answers os.kill(pid,0) as ALIVE but makes os.killpg(pgid,SIGKILL) raise EPERM (not ESRCH) on macOS, crashing the reap and spinning the full grace_s (~4s). Fix: swallow PermissionError in send() and treat defunct (ps stat 'Z') pids as not-alive. Other idioms: date -Is -> date -u +format; readlink -f -> python3 realpath helper; mapfile -> while-read under bash 3.2; cmd|grep -q under pipefail -> capture-then-test; Linux-only installer -> uname guard; swallowed /proc-read flipping a guard to allow-all -> explicit greppable os.path.exists guard.

## Cost
One extended session (main-thread opus). One `general-purpose`/sonnet Agent spawn for feature stages 2-5 (~96k subagent tokens, 36 tool calls). Engine `spawn-specialist.py` dispatch attempted once and crashed on macOS `/proc` (0 useful work; drove the route-via-Agent workaround). Extra cost: full incident recovery (memory reconstruction: 11 files) + the guard hook + its tests. Per-stage $ not split (main-session tokens unattributed; see scripts/cost-report.py).

## Self-critique of the agent system
The rm was avoidable — I built a destructive cleanup from $HASH derived from a command output that could be empty, and did not guard it. The guard hook + CLAUDE.md rule now prevent recurrence structurally.
