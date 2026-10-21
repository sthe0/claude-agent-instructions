---
name: 2026-01-05-sample-venue-drift
description: A sample cluster for the split fixture — the verification venue differs from the venue that holds the commits
type: reference
schema: difficulty/v1
generality: 0
generalized_into: []
resolution_confirmed_by_user: "fixture (AskUserQuestion: 'Yes — resolved')"
created: 2026-10-12
last_verified: 2026-10-12
---

# A control runs where the commits are not

## Difficulty
A stage's control is executed in a checkout that lacks the commits it judges, so a genuinely green stage is recorded as failed.

**Functional ground:** the venue a control runs in is chosen once and silently diverges from where the judged commits live

## Order & criterion
Split out of [2026-01-05-sample-venue-difficulty](2026-01-05-sample-venue-difficulty.md) by memory-curator split on 2026-10-12: the contexts below arose under that leaf's difficulty and cluster here by the functional ground named above. The order and acceptance check of the original stay in the source leaf.

## Contexts

### 2026-01-20 — venue drift
- Where it arose: a plan pinned repo_root to the main checkout.
- Working plan: three stages; the worktree held every commit.

## Cost
Not costed separately: the contexts keep the cost recorded in [2026-01-05-sample-venue-difficulty](2026-01-05-sample-venue-difficulty.md).
