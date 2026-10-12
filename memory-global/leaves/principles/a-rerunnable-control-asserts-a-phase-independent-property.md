---
name: a-rerunnable-control-asserts-a-phase-independent-property
description: A stage control is evaluated at record-result and again at verify-final, after the commit, the landing and every later stage; a pre-action probe, a working-tree diff, an exact-equality snapshot or a frozen literal is true in one phase only, so the control must assert a phase-independent end-state property, placed in the stage that owns it.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-10-12-stage-control-phase-of-evaluation, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]
domain: development
created: 2026-10-12
last_verified: 2026-10-12
---

# A re-runnable control asserts a property that holds in every phase it is re-run in

## Principle

Before freezing a control, name the phase of the plan in which each clause is true and ask whether it is still true after the commit, after landing and after every later stage that extends or reverses its subject; assert membership not equality, post-conditions not pre-action probes, and the durable end state in the stage that reaches it.

## Generality

Level 2 — ranges over any machine-evaluated check that is authored in one phase of a plan's lifecycle and evaluated again in others: per-stage result checks re-run at the closing gate, regression scripts replayed after a merge, acceptance checks replayed from a recorded plan. The class is "a snapshot written as if it were a law". It is not claimed for a control the engine evaluates exactly once at the moment it was written for, and it does not forbid phase-specific probes as evidence: a pre-action probe is legitimate diagnostic evidence, it is only unsound as the control that the closing gate re-runs.

## Induced from

- [[2026-10-12-stage-control-phase-of-evaluation]] — six contexts in which a probe correct before the action was wired as a post-condition, a working-tree diff correct before the commit was re-run after it, an equality a later stage deliberately broke was kept as the control, and a literal frozen before the world changed was cited; in each the control could not go green for the right reason at verify-final and nothing in the plan said which phase it was written for.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the original difficulty that established the engine's final re-run of every measurable stage (the very mechanism that makes phase a property of the control) and the kept contexts that first showed a stage-time observable going stale at that re-run.

## Refutation

A control that asserts a phase-specific state and is nonetheless sound at the closing re-run (because the engine freezes the evaluation snapshot, or re-runs it at the phase it was written for) would show the principle is a property of this engine's re-run discipline, not of controls in general. A re-run phase in which the end-state property itself is unobservable (the final state is destroyed or unreachable), so that no phase-independent assertion exists, would force a broader form: the control belongs in a stage-time-only gate and must be declared as such, not re-run.

## See also

- [[a-control-names-a-venue-that-outlives-its-last-rerun]] — the sibling axis of the same re-run: venue is the "where", phase the "when".
- [[a-judging-artefact-is-executed-before-it-judges]] — a probe wired as a post-condition would be caught by running it once in the phase it will judge; this principle names which phase to run it in.
- [[delivered-identity-is-established-by-content-or-ancestry]] — containment is a phase-independent landed-ness property where head equality is not.
- [Plan control-criterion hygiene](../plan-control-criterion-hygiene.md) — the authoring norms (no unverified current-behaviour fact, no self-rewrite) that apply this one.
