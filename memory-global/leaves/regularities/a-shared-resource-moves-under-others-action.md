---
name: a-shared-resource-moves-under-others-action
description: A resource other sessions or tasks also act on — a working tree, a trunk branch, a test suite's population, a session-state schema — changes between two observations by one task for reasons unrelated to that task.
type: reference
schema: regularity/v1
generality: 2
domain: development
induced_from: [2026-06-30-shared-tree-suite-failure-wrong-ownership-attribution, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan, 2026-07-03-foreign-wip-schema-leaks-into-session-state, 2026-10-12-stage-control-baseline-hostage]
created: 2026-10-27
last_verified: 2026-10-27
---
# A resource other sessions or tasks also act on

## Regularity

A resource that other sessions, tasks or people also act on — a shared working tree, a trunk branch, a test suite's population of tests, a session-state schema, a stash stack — changes between two observations made by one task for reasons that task did not cause: tests fail that its change never touched, foreign work-in-progress appears in its view, trunk advances past its base. An absolute observation of such a resource mixes the task's own effect with everyone else's, and nothing in the observation separates the two.

## Generality

Level 2 — ranges over resources outside one task's exclusive ownership. It is not claimed for a private, isolated resource nothing else writes (a task-owned scratch worktree).

## Refutation

A resource reputed shared that, observed across many tasks, never changed between one task's observations except through that task's own action would show it is in effect private, and absolute observation of it is sound.

## Induced from

- [[2026-06-30-shared-tree-suite-failure-wrong-ownership-attribution]] — the ownership fault in its
  first form: a suite red in a shared tree attributed to the session that happened to run it.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the vector catalogue for
  mis-scoped engine-run controls; "whole-suite gate false-failing on pre-existing unrelated reds" is
  this principle's difficulty stated as one of that leaf's venue/scope vectors.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the original difficulty from which the landed-state check kind was introduced in the engine (containment of the delivered head in the target rather than equality), and the kept contexts that fix what "landed" must mean once the delivery worktree no longer exists.
