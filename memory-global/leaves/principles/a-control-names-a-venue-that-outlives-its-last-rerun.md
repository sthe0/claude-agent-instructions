---
name: a-control-names-a-venue-that-outlives-its-last-rerun
description: A stage check's venue (worktree, repo_root, settings file, scaffold) is a lifetime-bound resource: the engine re-runs the check at verify-final after landing and cleanup, so the control must declare a venue that survives every re-run, or a distinct surviving venue for the re-run; a hard-coded address both misses the lifetime and silences the lint that would flag it.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-10-12-stage-control-venue-lifetime, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]
domain: development
created: 2026-10-12
last_verified: 2026-10-12
---

# A control names a venue that outlives its last re-run

## Principle

Declare, for every control, the venue it observes and how long that venue lives; if the delivery's own landing, cleanup or a later stage can remove or restore it, name a surviving venue for the re-run instead of repairing the venue to make the old address true.

## Generality

Level 2 — ranges over any check that is authored once and executed again later by a mechanism the author does not drive: stage verify commands and final checks re-run after landing, scheduled or CI re-validations, acceptance scripts replayed from a recorded plan. The class is "a re-runnable check whose observed place is created or destroyed by the work it certifies". It is not claimed for one-shot checks run by hand in the moment, nor for venues the delivery neither creates nor can alter (a read-only external system), where the lifetime question has a trivial answer.

## Induced from

- [[2026-10-12-stage-control-venue-lifetime]] — fourteen contexts in which a control addressed a delivery worktree, a repo root pinned to it, a scaffold or a settings file that landing, cleanup or a later restoring stage had removed or replaced by the time verify-final re-ran it, so the check short-circuited or went green over the pre-change baseline; the venue's lifetime was a property no one had stated and the engine could not read from a `cd <path>` in command text.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the original difficulty that fixed the engine side (a declared check venue, refusal to resolve an unresolvable one, a landed-state check kind, a final-time venue) and that supplies the kept contexts showing why repairing the venue to match the old address is the wrong repair.

## Refutation

An observation of a re-run control that is both correct and cheaper when it is pinned to a venue whose lifetime was never declared, across a delivery whose landing and cleanup did destroy that venue, would show the declaration is not what carries the correctness (and the real driver is something else, such as the check's content). A venue that must be preserved for the re-run to be meaningful, where naming a distinct surviving venue is impossible (the artifact exists only in the destroyed place), would force a broader form: keep the venue alive as part of the plan rather than re-point the check.

## See also

- [[a-rerunnable-control-asserts-a-phase-independent-property]] — the sibling axis: the venue is the "where", the phase is the "when" of the same re-run.
- [[delivered-identity-is-established-by-content-or-ancestry]] — what a landed-state check may bind to once the venue has changed.
- [[a-judging-artefact-is-executed-before-it-judges]] — run the control once against the world it will judge before relying on it.
- [Plan control-criterion hygiene](../plan-control-criterion-hygiene.md) — the authoring norms (declared venue, no unverified current-behaviour fact) that apply this principle.
