---
name: review-object-requirements-contract
description: Seed checklist of properties any reviewed object (plan, code, artifact, or future review target) must satisfy regardless of type — a starter, explicitly-refinable universal contract, not yet an induced principle.
type: reference
schema: leaf/v1
created: 2026-09-01
last_verified: 2026-09-01
---

# Universal requirements for a reviewed object

## Difficulty

Today, what makes a reviewed object "adequate" is left to per-invocation prose judgment, re-derived independently by every reviewer (thinker on a plan, code-reviewer on a diff, a future reviewer of some other artifact type) with no standing, cross-object-type checklist to anchor against. This is a source of avoidable stochasticity: two review passes over the same object can reasonably disagree not because the object changed but because the reviewer's ad-hoc notion of "adequate" drifted. Individual object types already have their own partial, type-specific instances of such a checklist (a plan's 8-element activity ontology, plan-review's materiality test, the M1-M4 partition markers), but nothing states the properties common to *any* reviewed object, independent of type — so each new review context reinvents the checklist from scratch, or skips stating it at all.

## Guidance

This leaf is a **SEED**, not a finished contract. It states a starter set of properties any reviewed object should satisfy, explicitly meant to be refined incrementally — via `self-improvement`/`overcome-difficulty` — as concrete review difficulties on other object types (code review, documentation review, other future review targets) are recorded. Individual items may later graduate into their own `principle/v1` leaves once enough accumulated difficulties satisfy that schema's `induced_from` requirement ([[principle-leaf-schema]] — "a principle is never rootless"). Until then, this leaf carries the seed as a `leaf/v1` reference, which carries no induction requirement.

The starter checklist:

1. **Internal consistency.** The object does not contradict itself — a stated goal, a stated scope, and the concrete steps/claims that follow from them agree. *Type-specific instance:* a plan's `[meta] goal` / `done_criterion` / per-stage `method` fields are checked for mutual consistency during plan-review (see [[question-provenance-gate]]).

2. **Every step or claim rests on a resource of verifiable, named origin.** A claim without a traceable source (a number, a deadline, a "this is how X works" assertion) is either grounded explicitly or flagged as unverified — never silently asserted. *Type-specific instance:* the planner's "Numbers and deadlines without a source" rule (`skills/specializations/planner/policy.md`) and the reasoning-deliverable claim-provenance ledger ([[formalization-ladder-l1-l3]]).

3. **Every named control is actually reachable and executable.** A stated verification step, gate, or check must be one that can genuinely run and produce a verdict — not a control that is aspirational, unreachable in practice, or that already trivially passes before any work happens. *Type-specific instance:* the plan-authoring "green-at-submit" advisory and the `verify_command` green-reachability lint (`agentctl/plan.py`).

4. **Scope is explicit, and any narrowing is visible rather than silently absorbed.** What the object does NOT cover must be stated, not left to be discovered later by omission. *Type-specific instance:* the plan-vs-order coverage block (`order-raise`/`order-dispose`, [[question-provenance-gate]]) and the rule that an ask's options must span the full set the analysis enumerated (`CLAUDE.md` § Escalation to the user).

Each item above is deliberately abstract enough to apply to a plan, a code diff, a document, or an object type not yet named — a future reviewer instantiates it the way the four type-specific examples already do for plans.

## See also

- [[plan-activity-ontology]] — the plan-specific 8-element instance of "what a reviewed object must state" (order, material/result, control criterion, means, method, conditions/invariants, actor/capability, refutable principle).
- [[question-provenance-gate]] — the plan-review materiality test and the order-coverage gate, two concrete mechanizations of properties 1, 2, and 4 above, scoped to plans.
- [[partition-markers]] — the M1-M4 per-object-type marker checklist, a worked example of a type-specific checklist this seed generalizes from.
- [[review-loop-cannot-measure-its-own-convergence]] — the companion difficulty on the review *process* axis (how many rounds, when does it terminate) rather than the review *content* axis (what makes an object adequate) this leaf addresses.
- [[specialist-behavior-determinization-recipe]] — the general recipe for turning a reviewer's ad-hoc judgment (including checks against this seed) into a structural gate where the judgment is decidable.
