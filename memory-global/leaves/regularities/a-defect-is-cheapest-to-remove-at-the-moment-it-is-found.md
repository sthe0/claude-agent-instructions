---
name: a-defect-is-cheapest-to-remove-at-the-moment-it-is-found
description: A localized defect costs least to fix at discovery, when localization is done and the tree is warm; filing it adds rediscovery and re-localization cost
type: reference
schema: regularity/v1
generality: 1
induced_from: [capability-before-offload]
rule_derived: "lifted from capability-before-offload (Deferral-offload section); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-defect-is-cheapest-to-remove-at-the-moment-it-is-found

## Regularity

At the moment a defect is found, its localization is done, the relevant files are read, the working tree is live and the test suite is warm. Filing the defect instead moves it into a queue and adds the cost of rediscovering and re-localizing it in a later session, so for a localized defect the removal cost at discovery is lower than the removal cost after filing.

## Generality

Level 1 — ranges over localized defects found by an agent while working on something else, outside the scope of its current plan. It is deliberately not claimed for defects that need another owner, a decision the finder does not hold, or a plan of their own.

## Induced from

- [[capability-before-offload]] — contributes the observed practice behind the statement: a defect found out of plan scope is compared as fix-now against file, rediscover, re-localize and fix-later, and filing is kept for what is not now-doable.

## Refutation

A set of localized defects found outside a plan's scope whose later fix, including rediscovery and re-localization, cost no more than fixing them at discovery would show the claim false for that class of defect.
