---
name: review-loop-shifting-grounds-never-converges
description: Several stages of one plan needed a manual override of an automated acceptance judge.
type: reference
schema: case/v1
origin_tier: project
source_digest: sha256:5c05c562b38afe6702fb1c44424b52e7425ce8b1c6c6fe911d50983924687745
generalized_into: [review-loop-cannot-measure-its-own-convergence]
created: 2026-10-21
last_verified: 2026-10-21
---

# Case: review-loop-shifting-grounds-never-converges

## Aspect

Successive revise verdicts rested on changing grounds, so repeating the loop did not bring the stage closer to acceptance.

## Expected

The loop measures its own convergence and exits when it stalls.

## Actual

Roughly half of the stages ended in a manual override.

## Mismatch

Nothing in the loop could tell that repeated rounds were not converging.
