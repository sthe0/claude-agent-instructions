---
name: a-repeated-hand-rolled-action-is-seen-by-no-trigger-that-reads-only-corrections-and-resolutions
description: Repetition of a hand-rolled multi-step action is seen by no trigger that fires only on user corrections or on task resolution
type: reference
schema: regularity/v1
generality: 1
induced_from: [propose-skill-on-repeated-workaround]
rule_derived: "lifted from propose-skill-on-repeated-workaround.md (Difficulty); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-repeated-hand-rolled-action-is-seen-by-no-trigger-that-reads-only-corrections-and-resolutions

## Regularity

When the same complex multi-step action is carried out by hand more than once with no skill covering it, repetition of the action is visible to no existing trigger: the self-improvement trigger fires only on an explicit user correction, and the resolution-time pattern sweep fires only at task resolution and only if the closing self-critique was written. Each failure is therefore corrected as a point-fix into a narrow reference leaf and read as a local mistake, a ticket can close with no experience leaf, and the action stays hand-rolled until a later repetition forces a tool; a reference leaf that does a skill's job without the reuse is the strongest sign of it.

## Generality

Level 1 — ranges over complex reusable actions repeated by hand under triggers that read only user corrections and task resolution. It is deliberately not claimed for one-line actions, for which formalizing costs more than repeating.

## Induced from

- [[propose-skill-on-repeated-workaround]] — contributes the observed case behind the statement: a deployment action hand-rolled repeatedly, each failure point-fixed into a reference leaf, and the ticket closed with no experience leaf.

## Refutation

Repeated hand-rolled actions found to be promoted to a skill after the second occurrence without any new trigger; or a ticket closing without a self-critique found to be swept for the repetition by the resolution-time pattern scan.
