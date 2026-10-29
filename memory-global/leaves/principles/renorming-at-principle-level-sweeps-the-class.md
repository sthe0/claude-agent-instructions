---
name: renorming-at-principle-level-sweeps-the-class
description: A re-norming recorded at PRINCIPLE level is discharged by sweeping the whole class in the artefact being corrected — every site carrying the named form is fixed or explicitly exempted, as an executed step inside the corrected plan — not by fixing the single instance that failed.
type: reference
schema: principle/v1
generality: 2
domain: planning
grounded_in: [regularity:a-fix-at-the-failing-site-leaves-siblings-of-the-same-form]
created: 2026-08-11
last_verified: 2026-08-11
---

# A re-norming at principle level sweeps the class, not the instance

## Principle

Because a fix at the failing site leaves siblings of the same form ([[a-fix-at-the-failing-site-leaves-siblings-of-the-same-form]]), for the goal of a re-norming that holds, **discharge it at the generality it was recorded at**: when a critique names a *form* rather than a *site* — "a control asserting a property of a shared resource absolutely", "a spawn passing a permission flag it was not granted" — the corrected plan must **enumerate every site carrying that form** and either fix it or state why it is exempt, and that enumeration must be an **executed step inside the plan**, not a claim made in the critique text.

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

## Goal class

A re-norming that holds: once a form-level defect is corrected, no sibling site of the same form fails later as a fresh difficulty.

## Generality

Level 2 — a class of tasks: any correction to a repeated artefact, where the fault was diagnosed as
a form. It ranges over plan controls, hook and gate implementations, spawn templates, test
fixtures, and instruction prose. It is not claimed at level 3: the statement presupposes an
artefact with *enumerable* sites; a fault in a genuinely singular object (one config value, one
decision) has no class to sweep, and the note level is the right record there.

## Grounded in

- [[a-fix-at-the-failing-site-leaves-siblings-of-the-same-form]] — When a defect is the instance of a form (a control asserting a shared resource absolutely, a spawn passing an ungranted flag), fixing the site where it failed leaves the other sites carrying the same form in place, and they fail later on their own.

Observed in the 2026-08 advisor-timeout delivery: the shared-resource-control form
([[shared-resource-health-asserted-relative-to-a-pinned-base]]) was corrected three times at three
separate sites across four replans, because each correction addressed the failing control rather
than enumerating the form.

## Refutation

The principle fails its goal if, after a plan executed the sweep, a site of the same form that the sweep should have enumerated still fails later.

## See also

- [[a-norm-is-transformed-by-the-same-activity-figure-as-any-material]] — the figure this sits inside: re-norming as the base
  activity whose material is the norm. This principle constrains the *scope* of the resulting norm.
- [[result-checked-against-its-result-image]] — the sweep's own control: the enumeration step needs a
  declared result-image (the site list) like any other step.
- `~/.claude-agent/skills/overcome-difficulty/SKILL.md` § 4 — Normalization, where the level is
  chosen and this promise is made.
