---
name: 2026-07-03-foreign-wip-schema-leaks-into-session-state
description: An unfinished foreign feature (plan_review gate) sitting uncommitted in the shared instructions repo both broke the stage verify suite and captured its own field into this session's engine state; when the WIP later vanished from the tree (owner session), the engine could no longer load the session. Isolation, not deletion: verify under the feature's own off-switch, migrate state with a backup.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "the0"
refs: [memory-global/leaves/experience/2026-07-03-engine-replan-artifact-discipline.md, memory-global/leaves/experience/2026-06-29-org-portable-core-internal-coupling-opt-in.md]
created: 2026-07-03
last_verified: 2026-07-03
---

# Foreign uncommitted WIP schema leaks into live session state

## Difficulty
Stage-1 verify_command failed with 8 test failures that were NOT the stage's fault: a foreign uncommitted plan_review WIP in the shared claude-agent-instructions tree auto-activated its thinker-review gate (advisor-mode=substantive) inside partition tests. Worse, the WIP's PlanReview field had been recorded into THIS session's state.json, so after the WIP's owner removed it from the tree mid-task, SessionState init crashed on the orphaned plan_review key and the engine could not load the session at all.

## Order & criterion
1) Attribute suite-red before treating it as your regression: run the suite at the stage's clean commit; green there means the tree, not the stage. 2) Never delete a parallel session's WIP; neutralize it for YOUR measurement via the feature's own documented off-switch (here AGENTCTL_PLAN_REVIEW=0 in verify_commands, applied as a refinement replan with the full declare, investigate, critique record). 3) When a removed WIP orphans a key in engine state: back up state.json, drop the key, verify status loads. 4) Write the revised plan to a NEW path; replan diffs old (state.plan_path) vs new, so in-place edits self-diff to no_change.

**Acceptance check:** agentctl status loads the session; stage verify_command green at the stage's own commit; foreign WIP bytes untouched by us (backup patch retained)

## Contexts

### 2026-07-03 — initial
- Where it arose: partition-materialization task in claude-agent-instructions; engine session 636b526e; WIP owner is a parallel live session on the same machine
- Working plan: ~/.claude-agent/plans/partition-materialization-v5.toml

## Cost
about 15 USD in spawns total; about 3.11 USD of it burned by dev spawn 1 (medium) partly on discovering the foreign WIP; one full difficulty cycle plus refinement replan

## Self-critique of the agent system
Rediscovered the in-place-plan-edit self-diff pitfall already recorded the same day in engine-replan-artifact-discipline (that leaf sat as uncommitted foreign WIP); should have run record-experience search at the FIRST difficulty, not at resolution
