---
name: what-a-change-does-not-carry-ends-with-its-authors-context
description: What a change does not itself carry is lost with its author's context
type: reference
schema: regularity/v1
generality: 1
induced_from: [tests-accompany-code, docs-accompany-architectural-change, review-accompanies-code]
rule_derived: "written from rule leaves, grounding-norm seed"
introduced_by: grounding-norm
created: 2026-10-29
last_verified: 2026-10-29
---

# what-a-change-does-not-carry-ends-with-its-authors-context

## Regularity

At the moment of a change its author holds what the change's next readers need in order to trust and use it — what it was checked against, how the system now works, whether another pair of eyes has seen it. What the change does not itself carry — a test, a description on the surface read first, a review record — ends with the author's context, and the next reader meets a change whose correctness and intent they can only reconstruct.

## Generality

Level 1 — ranges over code and documentation changes handed from their author to later readers or maintainers in this agent system. It is deliberately not claimed for changes read only by their author, nor for the content of a test, description or review record, only for their presence.

## Induced from

- [[tests-accompany-code]] — contributes what a change was checked against: the test is the part of that context the change itself carries.
- [[docs-accompany-architectural-change]] — contributes how the system now works: the description on the surface read first is the part of that context the change itself carries.
- [[review-accompanies-code]] — contributes whether another pair of eyes has seen the change: the review record is the part of that context the change itself carries.

## Refutation

Refuted by a change that carried no test, no description and no review record, yet whose next readers could trust and use it without reconstructing its correctness or intent; or by a carried test, description or review record that readers could not use either, which would force a narrower form.
