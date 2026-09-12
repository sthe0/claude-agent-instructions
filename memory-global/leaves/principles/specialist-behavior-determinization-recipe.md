---
name: specialist-behavior-determinization-recipe
description: A reusable four-step recipe for turning a specialist's ad-hoc judgment call into a structural mechanism (prefilter/truth-table/typed contract) that owns the decidable rule half, keeping the model only for the irreducible perception half. Induced from two independent instances (semantic-classification hard blocks; delivery-partition sizing) that converged on the same shape.
type: reference
schema: principle/v1
generality: 2
domain: coordination
induced_from: [regex-not-for-semantic-classification, partition-markers]
created: 2026-09-01
last_verified: 2026-09-01
---

# Determinizing a specialist's behavior: a four-step recipe

## Principle

To turn a specialist's currently-prose-guided judgment call into something reliable and reviewable, do not try to mechanize the whole call. Split it: (a) find the sub-decision inside the call that is actually **decidable from observable inputs** — a classification, a boolean marker, a count against a threshold, a match against a known shape; (b) build a structural mechanism that owns that sub-decision outright — a prefilter, a boolean-marker truth table, a typed contract, a gate; (c) leave the **irreducible perception** — the part only a model or a human can judge — behind that mechanism, never in front of it, so the mechanism decides *whether* perception is even invoked and the model never re-derives the structural part; (d) name an explicit **extension seam** so the next specialist or the next case is added to the existing mechanism, not bolted on as a second one-off. Apply this recipe *before* proposing a brand-new bespoke gate for a specialist's behavior — check whether an existing determinization already has an extension seam that covers the new case.

## Generality

Level 2 — a class-of-tasks rule: "when determinizing any specialist's judgment call, split rule from perception this way." It is not scoped to one specialist or one review type; it names the general shape that a level-1 or level-0 determinization instance (a specific reviewer, a specific gate) should be checked against.

## Induced from

- [[regex-not-for-semantic-classification]] — the rule half is a high-recall prefilter (cheap, precision-optional pattern match on text shape); the perception half is a fail-open model judge (`agentctl/advisor.py::judge_binary_ask`) that makes the actual semantic call; the extension seam is: any new semantic classifier mirrors `judge_binary_ask`'s contract (model, timeout, YES/NO protocol, exception → `False`) rather than inventing a new one.
- [[partition-markers]] — the rule half is the M1–M4 boolean-marker truth table (`scripts/agentctl/partition.py`), which derives the recommended/possible/not_required verdict mechanically; the perception half is the human/model judgment of whether each marker (independence, heterogeneity, blocking dependency, rollback risk) actually fires for this specific plan; the extension seam is the `--unit` materialization syntax, which lets new delivery-unit shapes (`inline`/`spawn`/`subtask`) attach to the same verdict machinery without changing the marker table.

Two independent instances (a natural-language semantic-classification gate; a delivery-partition sizing decision) solving unrelated problems converged on the same rule/perception split and the same "name an extension seam" closing move — the recurrence signal this leaf generalizes from, at the same order of evidence `principle-promotion-threshold`'s Rule-of-Three basis relies on, applied here across domains rather than within one functional-ground cluster (hence recorded at medium confidence, below the full threshold — see Refutation).

## Refutation

Two precedents is one short of the Rule-of-Three `principle-promotion-threshold` ordinarily requires before a cluster is flagged for induction; this leaf is recorded at generality 2 anyway because the convergence is across otherwise-unrelated domains, which is stronger evidence per-instance than two occurrences within one functional-ground cluster — but it is still recorded at **medium**, not high, confidence for exactly this reason. Apply the recipe to a third, genuinely new specialist-determinization case (a concrete candidate: point 4's proposed scope-localized re-review verb for plan-review, tracked as a follow-on in the Core issue this task files). If the resulting structure does **not** fit the four-step shape — no clean rule/perception split exists, or no useful extension seam can be named — record that outcome as a new context under this leaf rather than silently abandoning it: either the principle narrows to explicitly name what the two known instances share that a third case lacks, or the recipe itself needs revision. A third case that DOES fit raises confidence to high and satisfies the ordinary Rule-of-Three bar retroactively.

## See also

- [[review-object-requirements-contract]] — a seed checklist of properties any reviewed object must satisfy; this recipe is the general method for turning a reviewer's own judgment about those properties into a structural check, once enough concrete difficulties justify it.
- `CLAUDE.md` preamble, "Separate rule from perception; determinize the rule at its proper structural level" — the root principle both induced precedents already cite as their shared ground, and that this leaf lifts to an explicit, reusable four-step procedure.
- [[determinize-required-specialist-dispatch]] — a sibling application (proactively pairing a trigger with an existing reactive gate) of the same root principle, at a narrower scope than this leaf's general recipe.
- `skills/self-improvement/SKILL.md` § "Structural form before prose" — the tie-breaker (extend an existing mechanism before inventing a new one) that step (d) of this recipe operationalizes.
