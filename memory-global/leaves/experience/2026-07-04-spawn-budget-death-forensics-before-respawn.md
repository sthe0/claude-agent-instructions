---
name: 2026-07-04-spawn-budget-death-forensics-before-respawn
description: A claude -p specialist that dies at --max-budget-usd returns MALFORMED/no marker and the engine routes to BLOCKED — but the work on disk is often complete or mostly complete. 3 occurrences in one task (planner: artifact fully complete, zero respawn needed; stage-1 developer: ~70% done + red contract tests; a second developer nearly at budget but finished): forensic inspection (target artifact on disk, git status/diff, spawn transcript jsonl) before respawning saved a full budget each time. Partial work → continuation brief (done-work-is-correct-keep-it + exact remaining contract as red tests) respawned on a cheaper model finishes for a fraction of the original budget.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [memory-global/leaves/experience/2026-07-02-dead-spawn-scope-file-blocks-next-writer.md, memory-global/leaves/experience/2026-06-24-developer-marker-not-on-line-1-false-block.md]
plan_file: /home/the0/.claude-agent/plans/fix-scope-hook-lineage-and-replan-gaps-v1.toml
created: 2026-07-04
last_verified: 2026-07-04
---

# Spawn budget death: forensics on artifacts before any respawn; finish via continuation brief

## Difficulty
Desired: a budget-dead spawn's completed/partial work is recognized and reused. Actual: MALFORMED/no-marker + BLOCKED node reads as 'stage failed', inviting a from-scratch respawn that redoes done work and doubles cost — or worse, a 'fix' of code that was already correct.

## Order & criterion
1) On budget death, DO NOT respawn yet. Inspect: (a) the stage's target artifact on disk (plan file, commit, edited sources), (b) git status/diff in the work tree, (c) the spawn transcript ~/.claude-agent/projects/<spawn-cwd-hash>/<sid>.jsonl tail. 2) Classify: COMPLETE → verify yourself, unblock, record-result passed (no respawn); PARTIAL → write a continuation brief: list done work as keep-as-is, state the exact remaining contract (ideally the red tests the dead spawn already wrote), respawn a cheaper model (sonnet, medium). 3) Judge completeness against the real artifact schema (e.g. TOML [[final_check]] is top-level, not under meta — a wrong probe mis-reads a complete plan as truncated).

**Acceptance check:** Stage verify_command green without a second full-budget spawn; continuation spawn cost well under the original tier.

## Contexts

### 2026-07-04 — initial
- Where it arose: claude-agent-instructions, task fix-scope-hook-lineage-and-replan-gaps (agentctl-driven, dispatch-spawned planner+developers); 2 more occurrences in the preceding fix-agentctl-core-defects task
- Working plan: 4-stage TOML plan fix-scope-hook-lineage-and-replan-gaps-v1: (1) lineage-aware scope hook, (2) no_change replan refresh+snapshot backfill, (3) issue #16 content-hash review binding, (4) verify+smoke+gated landing. All landed as f47aaa3/f3b52af/00e8823 on origin/main.

## Cost
3 spawns attributed ~$7.9 total; forensics avoided ~2 full respawns (~$6 saved); continuation developer (sonnet/medium) finished stage 1 well under budget

## Self-critique of the agent system
First forensic probe misjudged the planner artifact as incomplete (checked meta.final_check instead of top-level [[final_check]]) — verify the artifact schema before judging completeness. Smoke simulated lineage env rather than end-to-end propagation (thinker N2), compensated by direct code verification of CLAUDE_CODE_SESSION_ID.
