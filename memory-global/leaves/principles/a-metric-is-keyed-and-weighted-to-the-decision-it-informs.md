---
name: a-metric-is-keyed-and-weighted-to-the-decision-it-informs
description: A measurement whose unit, deduplication key or attribution does not match the decision it supports ranks the wrong remedies or detects nothing.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-08-20-transcript-token-accounting-must-be-price-weighted, 2026-06-29-cost-tracking-three-levels-attribution, 2026-07-03-quality-regression-tracking-instrument]
created: 2026-10-21
last_verified: 2026-10-21
---

# A metric is keyed and weighted to the decision it informs

## Principle

Before building or trusting a measurement, name the decision it feeds and check that its unit is the decision's unit (cost, not raw tokens), that each event is counted once, and that each observation is keyed to the thing the decision is about (stage, plan, revision); a metric with no such key cannot be attributed, however accurate its totals.

## Generality

Level 2 - a rule for any instrument used to choose a remedy or to detect a regression.

## Induced from

- [[2026-08-20-transcript-token-accounting-must-be-price-weighted]] - duplicate usage rows and unweighted token counts inverted the ranking of remedies.
- [[2026-06-29-cost-tracking-three-levels-attribution]] - cost records not keyed by stage or plan could not be folded upward.
- [[2026-07-03-quality-regression-tracking-instrument]] - quality ratings with no link to the instruction revision could not name a commit range to blame.

## Refutation

Refuted by an instrument whose raw, unkeyed totals led to the same remedy ranking as the weighted and keyed one in a case where the two were compared.
