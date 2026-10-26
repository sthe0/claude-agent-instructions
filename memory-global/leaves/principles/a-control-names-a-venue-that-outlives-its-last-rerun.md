---
name: a-control-names-a-venue-that-outlives-its-last-rerun
description: A stage check's venue (worktree, repo_root, settings file, scaffold) is a lifetime-bound resource: the engine re-runs the check at verify-final after landing and cleanup, so the control must declare a venue that survives every re-run, or a distinct surviving venue for the re-run; a hard-coded address both misses the lifetime and silences the lint that would flag it.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:a-stage-control-is-re-evaluated-after-its-world-has-changed]
domain: development
created: 2026-10-12
last_verified: 2026-10-12
---

# A control names a venue that outlives its last re-run

## Principle

Because a stage control is re-evaluated after its world has changed ([[a-stage-control-is-re-evaluated-after-its-world-has-changed]]), for the goal of controls that pass again at every re-run of a correct delivery, declare, for every control, the venue it observes and how long that venue lives; if the delivery's own landing, cleanup or a later stage can remove or restore it, name a surviving venue for the re-run instead of repairing the venue to make the old address true.
## Goal class

Stage controls that pass again at every re-run of a correct delivery — final verification after landing and cleanup included — without rewriting a venue to make an old address true.

## Generality

Level 2 — ranges over any check that is authored once and executed again later by a mechanism the author does not drive: stage verify commands and final checks re-run after landing, scheduled or CI re-validations, acceptance scripts replayed from a recorded plan. The class is "a re-runnable check whose observed place is created or destroyed by the work it certifies". It is not claimed for one-shot checks run by hand in the moment, nor for venues the delivery neither creates nor can alter (a read-only external system), where the lifetime question has a trivial answer.

## Grounded in

- [[a-stage-control-is-re-evaluated-after-its-world-has-changed]] — A stage's control is run again at final verification and after replans, by which time landing, cleanup and later stages have removed its venue or changed the state it was written against.

## Refutation

The principle fails its goal if controls with a declared surviving venue still refuse or false-fail at final verification of a correct delivery because their venue was gone.

## See also

- [[a-rerunnable-control-asserts-a-phase-independent-property]] — the sibling axis: the venue is the "where", the phase is the "when" of the same re-run.
- [[delivered-identity-is-established-by-content-or-ancestry]] — what a landed-state check may bind to once the venue has changed.
- [[a-judging-artefact-is-executed-before-it-judges]] — run the control once against the world it will judge before relying on it.
- [Plan control-criterion hygiene](../plan-control-criterion-hygiene.md) — the authoring norms (declared venue, no unverified current-behaviour fact) that apply this principle.
