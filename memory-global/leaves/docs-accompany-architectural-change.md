---
name: docs-accompany-architectural-change
description: An architectural change (ADR, new subsystem, changed coordination model) is not delivered until the canonical read-first surface (README) reflects it — symmetric to tests-accompany-code; a design record only the implementer can find does not count as documented
type: feedback
schema: principle/v1
converted_from: leaf/v1
grounded_in: [regularity:what-a-change-does-not-carry-ends-with-its-authors-context, regularity:a-change-outside-the-state-its-consumers-read-is-unseen-by-them]
grounding_judged: 664933488a6a30f299018b8a9c5ab178805b6aba4cce89171ce90a3de20a7b38
created: 2026-06-26
last_verified: 2026-06-26
---

## Difficulty

When "done" is gated on a *measurable* criterion (tests green, PR merged), the human-facing documentation falls outside the definition of done and is silently skipped — the implementer carries the new mental model in their head and forgets that a future reader of the repo has only the README. An ADR or design doc written *during* the work documents the **decision**, but if nothing links it from the canonical read-first surface, the implemented architecture is invisible to anyone who does not already know the file path. This recurs (the user's "опять забыл"): the same omission lands every time an architectural change ships its code without updating its overview.

## Principle

To keep an architectural change discoverable after its implementer's context is gone, update the canonical read-first surface (top-level README and the relevant docs index) in the same delivery, so that a reader of the README alone can reach the change and its ADR.

## Goal class

documented architecture reachable from the read-first surface

## Grounded in

- [[what-a-change-does-not-carry-ends-with-its-authors-context]]
- [[a-change-outside-the-state-its-consumers-read-is-unseen-by-them]]

## Guidance

**The rule is symmetric to [[tests-accompany-code]]: the user-facing overview accompanies the architectural change, in the same delivery.**

- **Author (developer / coordinator).** Any change that introduces or alters a *user-facing architecture* — a new subsystem, a changed coordination model, an accepted ADR — updates the canonical read-first surface (top-level `README.md`, and the relevant `docs/` index) in the same delivery. Writing the ADR is necessary but **not sufficient**: an ADR nobody links is not documented. The done-criterion for an architectural task includes "a reader of README alone can discover the change and reach its ADR".
- **Reviewer / resolution gate.** Before declaring an architectural change resolved, check the read-first surface reflects it; a green test suite verifies the *runtime* axis, not the *documentation* axis. "Tests pass" ≠ "documented".

**Named escape class** (architectural changes that legitimately ship without a README touch — name the reason, don't omit silently):

- Purely internal refactor with no change to the externally-described architecture or vocabulary.
- A change already fully covered by an existing README section (verify by reading it, don't assume).
- Work explicitly scoped as code-only with a follow-up doc task tracked elsewhere (cite the task).

## See also

- [[tests-accompany-code]] — the symmetric default on the runtime axis (output accompanies the artifact it describes)
- [[plan-activity-ontology]] — the README update is part of a stage's *result*/control-criterion, not optional polish
- `README.md` § Maintaining this README — "when the cooperation model changes, update the affected sections"
- regularity:what-a-change-does-not-carry-ends-with-its-authors-context — the regularity this rule rests on.
