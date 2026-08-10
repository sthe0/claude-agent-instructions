---
name: renorming-at-principle-level-sweeps-the-class
description: A re-norming recorded at PRINCIPLE level is discharged by sweeping the whole class in the artefact being corrected — every site carrying the named form is fixed or explicitly exempted, as an executed step inside the corrected plan — not by fixing the single instance that failed.
type: reference
schema: principle/v1
generality: 2
domain: planning
induced_from: [2026-07-20-agentctl-premise-gate-blocks-venue-refinement-replan, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]
created: 2026-08-11
last_verified: 2026-08-11
---

# A re-norming at principle level sweeps the class, not the instance

## Principle

To make a re-norming actually hold, **discharge it at the generality it was recorded at**: when a
critique names a *form* rather than a *site* — "a control asserting a property of a shared resource
absolutely", "a spawn passing a permission flag it was not granted" — the corrected plan must
**enumerate every site carrying that form** and either fix it or state why it is exempt, and that
enumeration must be an **executed step inside the plan**, not a claim made in the critique text.

The level a re-norming is recorded at is a promise about its scope. `--level note` promises this
site; `--level principle` promises the class. Fixing the one instance that happened to fail while
recording the lesson at principle level leaves the promise unkept in the most expensive way: the
record says the class is closed, so the surviving siblings are now protected from being looked at
again.

This is mechanically checkable and therefore belongs in the plan as a step with an observable —
`grep`/AST enumeration of the form, the site list as the step's output artifact, a per-site verdict
of fixed-or-exempt. Stating it as prose in the critique is exactly the failure mode it names.

**Why the engine does not catch this.** `agentctl replan` runs a coverage gate over the critique's
`--difference-to-remove` items: each named difference must land in some stage's changed
means/method. That checks the *dataflow* — every difference you named goes somewhere. It never
checks that the **siblings** of the named difference were swept, because it cannot: the class is a
semantic property of the artefact, not a field on the record. The sweep is the coordinator's
cognition, and its determinized half is the enumeration step you put in the plan.

## Generality

Level 2 — a class of tasks: any correction to a repeated artefact, where the fault was diagnosed as
a form. It ranges over plan controls, hook and gate implementations, spawn templates, test
fixtures, and instruction prose. It is not claimed at level 3: the statement presupposes an
artefact with *enumerable* sites; a fault in a genuinely singular object (one config value, one
decision) has no class to sweep, and the note level is the right record there.

## Induced from

- [[2026-07-20-agentctl-premise-gate-blocks-venue-refinement-replan]] — the replan-cycle mechanics
  this principle constrains: what a refinement carries, what the gates check, and what they let
  through.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the canonical instance:
  a catalogue of control-scoping vectors that grew one vector at a time, each discovered because the
  previous correction fixed only the site that failed.

Observed in the 2026-08 advisor-timeout delivery: the shared-resource-control form
([[shared-resource-health-asserted-relative-to-a-pinned-base]]) was corrected three times at three
separate sites across four replans, because each correction addressed the failing control rather
than enumerating the form.

## Refutation

The principle is refuted, or driven to a broader form, by a class whose enumeration is not
decidable cheaply enough to be a plan step — where finding every site carrying the form costs more
than the expected damage from the un-swept siblings. At that point the requirement must widen from
"sweep the class now" to "sweep the class **or** install a standing detector for the form", and the
detector (a verifier, a lint rule, a registry) becomes the discharge. That widening is already the
better answer whenever the form can recur in code not yet written, so the refutation is likely to
arrive as a strengthening rather than a defeat.

## See also

- [[reflexive-exit-is-base-activity-figure]] — the figure this sits inside: re-norming as the base
  activity whose material is the norm. This principle constrains the *scope* of the resulting norm.
- [[result-checked-against-its-result-image]] — the sweep's own control: the enumeration step needs a
  declared result-image (the site list) like any other step.
- `~/.claude-agent/skills/overcome-difficulty/SKILL.md` § 4 — Normalization, where the level is
  chosen and this promise is made.
