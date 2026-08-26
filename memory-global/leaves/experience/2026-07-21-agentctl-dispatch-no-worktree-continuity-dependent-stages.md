---
name: 2026-07-21-agentctl-dispatch-no-worktree-continuity-dependent-stages
description: A dependent spawn stage (depends_on a prior stage) forks a FRESH worktree off origin/main because agentctl dispatch passes only the plan + stage-index to spawn-specialist.py — no active-worktree/branch context. So stage 2, whose method assumed stage 1's committed constant, found it absent from origin/main and RE-DID stage 1 identically in its own branch. Net result was correct (the whole change ends up on one branch = one PR) but cost a redundant ~half-spawn and left a dangling stage-1-only branch. Detection: watch the developer's worktree choice right after dispatch (git worktree list); a fresh branch off origin/main for a dependent stage is the signal. Coordinator can't inject into the frozen dispatch prompt, so the mitigations are: (a) verify-hard the combined branch carries BOTH stages (single-source, all final_checks) and fix-forward, or (b) fix in the engine — dispatch should thread the prior dependent stage's worktree/branch.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "the0 (user)"
refs: [2026-06-24-agentctl-verify-venue-worktree-needs-substantive-replan.md, 2026-07-03-engine-replan-artifact-discipline.md, 2026-07-20-agentctl-premise-gate-blocks-venue-refinement-replan.md]
created: 2026-07-21
last_verified: 2026-07-21
---

# agentctl dispatch threads no worktree/branch continuity across dependent spawn stages

## Difficulty
Dispatch of a dependent spawn stage has no channel to tell the spawned developer to build on the prior stage's (un-landed, worktree-only) branch; the developer's natural 'worktree off origin/main' action skips the prior stage's work.

## Order & criterion
classify->plan->approve->partition->stage1 dispatch (developer makes branch B1, commits)->stage2 dispatch (developer makes FRESH branch B2 off origin/main, re-does stage1 + does stage2)->verify combined B2->land B2 as PR, delete redundant B1

**Acceptance check:** measurable: the landed branch contains BOTH stages' changes with a single source of the shared constant and all owned tests + final_checks green; exactly one delivery branch survives (the redundant per-stage branch deleted).

## Contexts

### 2026-07-21 — initial
- Where it arose: agentctl multi-stage spawn:developer plans in Core (~/claude-agent-instructions), worktree-delivered changes where stage N+1 depends_on stage N's committed-but-un-landed work.
- Working plan: 2 stages (single-source marker constant; enrich essence Directive), 1 PR; dispatch synchronous; verify each stage's intent-diff in the worktree; net one combined branch landed via PR #42.

## Cost
2 developer spawns, $3.72 list-price telemetry (flat Max); ~half of spawn 2 was redundant stage-1 redo

## Self-critique of the agent system
I FORESAW the continuity gap before dispatching stage 2 but had no injection channel into the frozen dispatch prompt, so I dispatched-and-verified rather than pre-fixing. Net-correct but wasteful; the durable fix is engine-side (dispatch threads the dependent worktree) — filed as a self-improvement follow-up.
