---
name: a-metric-is-keyed-and-weighted-to-the-decision-it-informs
description: A measurement whose unit, deduplication key or attribution does not match the decision it supports ranks the wrong remedies or detects nothing.
type: reference
schema: principle/v1
generality: 2
grounded_in: [regularity:an-unkeyed-or-unweighted-total-misranks-the-decision-it-feeds]
created: 2026-10-21
last_verified: 2026-10-21
---

# A metric is keyed and weighted to the decision it informs

## Principle

Because an unkeyed or unweighted total misranks the decision it feeds ([[an-unkeyed-or-unweighted-total-misranks-the-decision-it-feeds]]), for the goal of measurements that rank and attribute correctly, before building or trusting a measurement, name the decision it feeds and check that its unit is the decision's unit (cost, not raw tokens), that each event is counted once, and that each observation is keyed to the thing the decision is about (stage, plan, revision); a metric with no such key cannot be attributed, however accurate its totals.

## Goal class

Measurements that rank the alternatives of the decision they feed the way the decision's real unit ranks them, and attribute their totals to those alternatives.

## Generality

Level 2 - a rule for any instrument used to choose a remedy or to detect a regression.

## Grounded in

- [[an-unkeyed-or-unweighted-total-misranks-the-decision-it-feeds]] — A measurement counted in a unit other than the decision's (raw tokens for a cost decision), counting events more than once, or lacking a key to the thing decided about ranks the alternatives differently from the decision's real unit, however accurate its totals.

## Refutation

The principle fails its goal if a measurement checked this way still ranks the decision's alternatives differently from the decision's real unit, or still cannot attribute its totals.
