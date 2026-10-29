---
name: a-rerunnable-control-asserts-a-phase-independent-property
description: A stage control is evaluated at record-result and again at verify-final, after the commit, the landing and every later stage; a pre-action probe, a working-tree diff, an exact-equality snapshot or a frozen literal is true in one phase only, so the control must assert a phase-independent end-state property, placed in the stage that owns it.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:a-stage-control-is-re-evaluated-after-its-world-has-changed]
domain: development
created: 2026-10-12
last_verified: 2026-10-12
---

# A re-runnable control asserts a property that holds in every phase it is re-run in

## Principle

Because a stage control is re-evaluated after its world has changed ([[a-stage-control-is-re-evaluated-after-its-world-has-changed]]), for the goal of controls whose verdict does not depend on the phase they run in, before freezing a control, name the phase of the plan in which each clause is true and ask whether it is still true after the commit, after landing and after every later stage that extends or reverses its subject; assert membership not equality, post-conditions not pre-action probes, and the durable end state in the stage that reaches it.

## Goal class

Re-runnable controls whose verdict on a correct delivery does not depend on the plan phase in which they are evaluated.

## Generality

Level 2 — ranges over any machine-evaluated check that is authored in one phase of a plan's lifecycle and evaluated again in others: per-stage result checks re-run at the closing gate, regression scripts replayed after a merge, acceptance checks replayed from a recorded plan. The class is "a snapshot written as if it were a law". It is not claimed for a control the engine evaluates exactly once at the moment it was written for, and it does not forbid phase-specific probes as evidence: a pre-action probe is legitimate diagnostic evidence, it is only unsound as the control that the closing gate re-runs.

## Grounded in

- [[a-stage-control-is-re-evaluated-after-its-world-has-changed]] — A stage's control is run again at final verification and after replans, by which time landing, cleanup and later stages have removed its venue or changed the state it was written against.

## Refutation

The principle fails its goal if controls written this way still flip from pass to fail across phases on a delivery that did not change.

## See also

- [[a-control-names-a-venue-that-outlives-its-last-rerun]] — the sibling axis of the same re-run: venue is the "where", phase the "when".
- [[a-judging-artefact-is-executed-before-it-judges]] — a probe wired as a post-condition would be caught by running it once in the phase it will judge; this principle names which phase to run it in.
- [[delivered-identity-is-established-by-content-or-ancestry]] — containment is a phase-independent landed-ness property where head equality is not.
- [Plan control-criterion hygiene](../plan-control-criterion-hygiene.md) — the authoring norms (no unverified current-behaviour fact, no self-rewrite) that apply this one.
