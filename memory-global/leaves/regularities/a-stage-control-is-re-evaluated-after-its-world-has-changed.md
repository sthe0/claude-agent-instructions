---
name: a-stage-control-is-re-evaluated-after-its-world-has-changed
description: A stage's control is run again at final verification and after replans, by which time landing, cleanup and later stages have removed its venue or changed the state it was written against.
type: reference
schema: regularity/v1
generality: 2
domain: development
induced_from: [2026-10-12-stage-control-venue-lifetime, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan, 2026-10-12-stage-control-phase-of-evaluation, 2026-07-03-engine-replan-artifact-discipline]
created: 2026-10-27
last_verified: 2026-10-27
---
# A stage's control is run again at final verification and after replans, by which time landing, cleanup and later stages have removed its venue or changed the state it was written against

## Regularity

A stage's control is not evaluated once: it is run again at final verification, after a replan, after landing, and by then the world it was written against has changed — the delivery worktree it named has been removed by landing, a later stage has extended or reversed its subject, the commit it probed for has become part of history. A clause true at the moment of the stage (a pre-action probe, an equality with the current head, a path in a temporary venue) is false at the re-run with no defect in the delivery.

## Generality

Level 2 — ranges over controls that a plan engine re-runs after the stage they belong to. It is not claimed for a control run exactly once and discarded.

## Refutation

Plans whose stage controls, written against the stage's own moment, all passed again at final verification after landing and cleanup with no venue or phase rewrite would show that, for those plans, the world did not change between the runs in any way the controls could see.

## Induced from

- [[2026-10-12-stage-control-venue-lifetime]] — fourteen contexts in which a control addressed a delivery worktree, a repo root pinned to it, a scaffold or a settings file that landing, cleanup or a later restoring stage had removed or replaced by the time verify-final re-ran it, so the check short-circuited or went green over the pre-change baseline; the venue's lifetime was a property no one had stated and the engine could not read from a `cd <path>` in command text.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the original difficulty that fixed the engine side (a declared check venue, refusal to resolve an unresolvable one, a landed-state check kind, a final-time venue) and that supplies the kept contexts showing why repairing the venue to match the old address is the wrong repair.
- [[2026-10-12-stage-control-phase-of-evaluation]] — six contexts in which a probe correct before the action was wired as a post-condition, a working-tree diff correct before the commit was re-run after it, an equality a later stage deliberately broke was kept as the control, and a literal frozen before the world changed was cited; in each the control could not go green for the right reason at verify-final and nothing in the plan said which phase it was written for.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the original difficulty that established the engine's final re-run of every measurable stage (the very mechanism that makes phase a property of the control) and the kept contexts that first showed a stage-time observable going stale at that re-run.
