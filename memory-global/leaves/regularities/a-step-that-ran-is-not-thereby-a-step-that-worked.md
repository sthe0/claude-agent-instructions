---
name: a-step-that-ran-is-not-thereby-a-step-that-worked
description: A step that executes to completion and a step that removed its difficulty are independent outcomes; nothing in the execution itself tells them apart, only a comparison of the actual state against a declared expected state does.
type: reference
schema: regularity/v1
generality: 3
induced_from: [coordinator-pitfalls, 2026-06-24-gate-exemption-is-category-error-for-result-images, static-checks-green-live-run-fails, 2026-05-26-agent-system-plan-vs-reality-drift, 2026-06-30-2026-06-30-verify-spawned-developer-commit-scope-shared-tree, 2026-07-23-expected-image-stale-vs-codereview-normative-reconcile]
created: 2026-10-27
last_verified: 2026-10-27
---
# A step that executes to completion and a step that removed its difficulty are independent outcomes

## Regularity

A step's execution and its effect are independent: a command exits 0, a specialist returns `COMPLETED:`, a plan's stages all report done, and the difficulty they were meant to remove can still be present, because the execution reports on the act and not on the state the act was meant to change. The difference becomes observable only where the actual state is compared against an expected state that was declared before the step ran; where no such image was declared, a finished step and a working step look identical.

## Generality

Level 3 — ranges over every transformation whose success is a change of state in its material rather than the completion of the act itself: a plan stage, a spawned specialist's delivery, a fix to a running system, a migration. It is not claimed for an act whose completion is its whole content (sending a message whose receipt is the goal).

## Refutation

An observed class of steps whose execution status reliably predicted the effect — no case in which a completed run left the difficulty in place over a run of independent occurrences — would show the independence does not hold for that class, and the comparison against a declared image would be redundant there.

## Induced from

- [[coordinator-pitfalls]] — "Advanced to the next stage … without comparing the actual outcome to the
  stage's `Expected result image:`."
- [[2026-06-24-gate-exemption-is-category-error-for-result-images]] — the result-image is the output of
  a process the same gate governs; mutating it unchecked is exactly what the comparison catches.
