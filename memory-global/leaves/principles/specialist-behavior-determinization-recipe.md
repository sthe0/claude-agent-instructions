---
name: specialist-behavior-determinization-recipe
description: A reusable four-step recipe for turning a specialist's ad-hoc judgment call into a structural mechanism (prefilter/truth-table/typed contract) that owns the decidable rule half, keeping the model only for the irreducible perception half. Grounded in two independent instances (semantic-classification hard blocks; delivery-partition sizing) that converged on the same shape.
type: reference
schema: principle/v1
generality: 2
domain: coordination
grounded_in: [regularity:a-judgement-call-contains-a-decidable-sub-decision]
created: 2026-09-01
last_verified: 2026-09-01
---

# Determinizing a specialist's behavior: a four-step recipe

## Principle

Because a judgement call contains a decidable sub-decision ([[a-judgement-call-contains-a-decidable-sub-decision]]), for the goal of turning a specialist's currently-prose-guided judgment call into something reliable and reviewable, do not try to mechanize the whole call. Split it: (a) find the sub-decision inside the call that is actually **decidable from observable inputs** — a classification, a boolean marker, a count against a threshold, a match against a known shape; (b) build a structural mechanism that owns that sub-decision outright — a prefilter, a boolean-marker truth table, a typed contract, a gate; (c) leave the **irreducible perception** — the part only a model or a human can judge — behind that mechanism, never in front of it, so the mechanism decides *whether* perception is even invoked and the model never re-derives the structural part; (d) name an explicit **extension seam** so the next specialist or the next case is added to the existing mechanism, not bolted on as a second one-off. Apply this recipe *before* proposing a brand-new bespoke gate for a specialist's behavior — check whether an existing determinization already has an extension seam that covers the new case.

## Goal class

Turning a specialist's prose-guided judgement call into something reliable and reviewable — the same inputs giving the same decision, and the residual judgement isolated where a reviewer can see it.

## Generality

Level 2 — a class-of-tasks rule: "when determinizing any specialist's judgment call, split rule from perception this way." It is not scoped to one specialist or one review type; it names the general shape that a level-1 or level-0 determinization instance (a specific reviewer, a specific gate) should be checked against.

## Grounded in

- [[a-judgement-call-contains-a-decidable-sub-decision]] — A judgement a specialist makes in prose almost always contains a sub-decision decidable from observable inputs — a classification, a boolean marker, a count against a threshold — and the irreducible perception is only the remainder.

Two independent instances (a natural-language semantic-classification gate; a delivery-partition sizing decision) solving unrelated problems converged on the same rule/perception split and the same "name an extension seam" closing move — the recurrence signal this leaf generalizes from, at the same order of evidence `principle-promotion-threshold`'s Rule-of-Three basis relies on, applied here across domains rather than within one functional-ground cluster (hence recorded at medium confidence, below the full threshold — see Refutation).

## Refutation

The principle fails its goal if a call split this way still yields different decisions on the same observable inputs, or if its residual perception becomes no easier to review than the undivided call.

## See also

- [[review-object-requirements-contract]] — a seed checklist of properties any reviewed object must satisfy; this recipe is the general method for turning a reviewer's own judgment about those properties into a structural check, once enough concrete difficulties justify it.
- `CLAUDE.md` preamble, "Separate rule from perception; determinize the rule at its proper structural level" — the root principle both precedents already cite as their shared ground, and that this leaf lifts to an explicit, reusable four-step procedure.
- [[determinize-required-specialist-dispatch]] — a sibling application (proactively pairing a trigger with an existing reactive gate) of the same root principle, at a narrower scope than this leaf's general recipe.
- `skills/self-improvement/SKILL.md` § "Structural form before prose" — the tie-breaker (extend an existing mechanism before inventing a new one) that step (d) of this recipe operationalizes.
