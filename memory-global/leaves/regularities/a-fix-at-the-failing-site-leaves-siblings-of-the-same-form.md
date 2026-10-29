---
name: a-fix-at-the-failing-site-leaves-siblings-of-the-same-form
description: When a defect is the instance of a form (a control asserting a shared resource absolutely, a spawn passing an ungranted flag), fixing the site where it failed leaves the other sites carrying the same form in place, and they fail later on their own.
type: reference
schema: regularity/v1
generality: 2
domain: planning
induced_from: [2026-07-20-agentctl-premise-gate-blocks-venue-refinement-replan, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan, 2026-07-16-onboarding-drift-after-crosscutting-refactor-needs-mechanical-guard, 2026-08-27-self-attested-identity-field-needs-declared-principal-check]
created: 2026-10-27
last_verified: 2026-10-27
---
# When a defect is the instance of a form (a control asserting a shared resource absolutely, a spawn passing an ungranted flag), fixing the site where it failed leaves the other sites carrying the same form in place, and they fail later on their own

## Regularity

When the defect that surfaced is an instance of a form — a control asserting a shared resource's health absolutely, a spawn passing a permission flag it was not granted, a document guarded by a pinned file list — the other sites carrying the same form exist whether or not they have failed yet. A fix applied at the site that failed leaves them in place, and they fail later, each on its own occasion, each looking like a new difficulty. A critique that names the form without the plan enumerating its sites leaves the enumeration undone.

## Generality

Level 2 — ranges over defects whose description names a form rather than a single site. It is not claimed for a defect that is unique to its site (a typo, a one-off value).

## Refutation

A run of form-level defects fixed only at their failing site after which no sibling site of the same form failed later would show the siblings do not persist for that kind of form.

## Induced from

- [[2026-07-20-agentctl-premise-gate-blocks-venue-refinement-replan]] — the replan-cycle mechanics
  this principle constrains: what a refinement carries, what the gates check, and what they let
  through.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the canonical instance:
  a catalogue of control-scoping vectors that grew one vector at a time, each discovered because the
  previous correction fixed only the site that failed.
