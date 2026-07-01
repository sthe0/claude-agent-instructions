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

## Cost
One extended session (main-thread opus). One `general-purpose`/sonnet Agent spawn for feature stages 2-5 (~96k subagent tokens, 36 tool calls). Engine `spawn-specialist.py` dispatch attempted once and crashed on macOS `/proc` (0 useful work; drove the route-via-Agent workaround). Extra cost: full incident recovery (memory reconstruction: 11 files) + the guard hook + its tests. Per-stage $ not split (main-session tokens unattributed; see scripts/cost-report.py).

## Self-critique of the agent system
The rm was avoidable — I built a destructive cleanup from $HASH derived from a command output that could be empty, and did not guard it. The guard hook + CLAUDE.md rule now prevent recurrence structurally.
