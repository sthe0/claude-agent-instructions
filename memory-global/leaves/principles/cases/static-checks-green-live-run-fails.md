---
name: static-checks-green-live-run-fails
description: A review-driven refactor passed import checks, unit tests and a build diff.
type: reference
schema: case/v1
origin_tier: project
source_digest: sha256:158d0cb3c08b0c7b09af067801f0f85ea5da7b72032682ef7c0e77ece985ecf0
generalized_into: [a-step-that-ran-is-not-thereby-a-step-that-worked]
created: 2026-10-21
last_verified: 2026-10-21
---

# Case: static-checks-green-live-run-fails

## Aspect

The deciding stage of the change was observable only in a live run, which was not performed before success was reported.

## Expected

The result is checked against the declared result image at the deciding stage.

## Actual

A live run later failed at the block that the static checks never reached.

## Mismatch

Success was inferred from partial progress and from checks on a different axis than the one that mattered.
