---
name: 2026-09-03-published-text-writer-gate-effort-divergence-deadlock
description: Delivering GitHub issue #125 (mechanize tech-writer usage before publishing to tickets) surfaced two engine gaps in agentctl's effort-divergence machinery: fire-acknowledge does not exit DIAGNOSING by itself, and the replans-absolute scale creates an unbreakable deadlock once its counter crosses the threshold, because the very replan call required to exit DIAGNOSING increments the same counter the trigger compares.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [effort-divergence-trigger, agentctl-acceptance-judge-gate, landing-discipline]
created: 2026-09-03
last_verified: 2026-09-03
---

# Publication-gate task landed; discovered a replans-absolute effort-divergence deadlock at DIAGNOSING exit

## Difficulty
verify-final kept failing at the resolution gate via a replans-scale absolute effort-divergence fire (threshold 3). Acknowledging the fire (fire-acknowledge --decision continue) read as a full unblock but the very next verify-final still refused, because DIAGNOSING-exit is a separate precondition gated on a complete declare->investigate->critique->replan cycle, not on the fire being acknowledged. Worse, each exit replan itself incremented the same non-resettable replans counter the absolute-scale trigger compares (no baseline subtraction on this scale), so every remediation attempt guaranteed the next verify-final would re-fire -- a mathematically provable infinite loop, confirmed empirically as the counter rose 8->9 across two consecutive exit attempts.

## Order & criterion
Deliver the publication-time tech-writer gate (issue #125) to origin/main, verified green end to end.

**Acceptance check:** On origin/main: the gate denies unwitnessed publication text and allows witnessed text, the new test suite passes, verify-all names no failing check other than verify-terms (which names none of this change's files) -- and agentctl verify-final/resolve reach RESOLVED.

## Contexts

### 2026-09-03 — initial
- Where it arose: claude-agent-instructions repo, agentctl engine, effort-divergence trigger (replans absolute scale, config effort-replan-absolute=3)
- Working plan: /home/the0/.claude-agent/plans/published-text-writer-gate.toml

## Cost
13 spawns, ~$59 attributed spend, ~15.5k s duration across 6 attributed stages (per verify-final cost-log); plus this session's own DIAGNOSING/deadlock recovery on top

## Self-critique of the agent system
The deadlock was discovered empirically (by hitting it) rather than derived from reading gates.py before the first exit-replan attempt; the third-gap leaf entry (baseline not snapshotted for the replans scale) already existed and, read closely beforehand, would have predicted this exact failure mode one exit-cycle earlier. Also: the kill-switch bypass (AGENTCTL_EFFORT=0 on the exit replan) was attempted twice before realizing the persisted-DIAGNOSING-state timing nuance, costing an extra round.
