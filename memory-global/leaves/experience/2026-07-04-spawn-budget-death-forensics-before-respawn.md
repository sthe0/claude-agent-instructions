---
name: 2026-07-04-spawn-budget-death-forensics-before-respawn
description: A claude -p specialist that dies at --max-budget-usd returns MALFORMED/no marker and the engine routes to BLOCKED — but the work on disk (or in the transcript) is often complete or mostly complete. 5 occurrences across two tasks (planner: artifact fully complete, zero respawn; stage-1 developer: ~70% done + red contract tests; a second developer nearly at budget but finished; plus a capture-plan planner whose full TOML was on disk, and a thinker plan-REVIEW whose verdict+findings were complete in the transcript despite MALFORMED): forensic inspection (target artifact on disk, git status/diff, spawn transcript jsonl) before respawning saved a full budget each time. For a review/analysis spawn that writes no file, the deliverable lives in the transcript text — extract it, don't respawn. Partial work → continuation brief (done-work-is-correct-keep-it + exact remaining contract as red tests) respawned on a cheaper model finishes for a fraction of the original budget.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [memory-global/leaves/experience/2026-07-02-dead-spawn-scope-file-blocks-next-writer.md, memory-global/leaves/experience/2026-06-24-developer-marker-not-on-line-1-false-block.md]
plan_file: /home/the0/.claude-agent/plans/fix-scope-hook-lineage-and-replan-gaps-v1.toml
created: 2026-07-04
last_verified: 2026-07-05
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

### 2026-07-05 — capture-absorb-program-durably (2 more occurrences, incl. a review-verdict variant)
- Where it arose: capture-absorb-program-durably task (agentctl-driven). (a) The **planner** for the capture plan died at the $3 medium cap (MALFORMED) but had written the COMPLETE 5-stage TOML to disk — used the artifact directly (only fix: two single-`"`→`"""` multiline-string typos), zero respawn. (b) The **thinker plan-review** died at the $1 small cap (MALFORMED) but had already emitted `PLAN-REVIEW: REVISE` + two well-formed blocking findings as the last text blocks in its transcript — extracted the verdict from `~/.claude-agent/projects/<hash>/<sid>.jsonl` (assistant text blocks) and recorded it, no respawn.
- New generalization: the pattern isn't limited to file-producing spawns. A **review/analysis** spawn's deliverable is its transcript text; budget-death truncates the *return-marker step*, not the reasoning already written. Parse the transcript's assistant text blocks before concluding "no verdict". The subsequent (cheaper, sonnet) re-review of the revised plan finished cleanly under budget — the continuation-on-cheaper-model half of the pattern held for a review too.

## Cost
3 spawns attributed ~$7.9 total; forensics avoided ~2 full respawns (~$6 saved); continuation developer (sonnet/medium) finished stage 1 well under budget. 2026-07-05: planner ($3) + thinker-review ($1) both budget-dead — forensics reused both (plan TOML from disk; REVISE verdict from transcript), avoiding ~$4 of respawns; the 5 capture-stage developers then ran $0.60–$1.95 each (all under budget).

## Self-critique of the agent system
First forensic probe misjudged the planner artifact as incomplete (checked meta.final_check instead of top-level [[final_check]]) — verify the artifact schema before judging completeness. Smoke simulated lineage env rather than end-to-end propagation (thinker N2), compensated by direct code verification of CLAUDE_CODE_SESSION_ID.
