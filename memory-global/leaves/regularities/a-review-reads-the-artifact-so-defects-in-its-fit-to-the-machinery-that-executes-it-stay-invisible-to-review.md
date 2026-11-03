---
name: a-review-reads-the-artifact-so-defects-in-its-fit-to-the-machinery-that-executes-it-stay-invisible-to-review
description: Review reads the artifact while execution reads the machinery, so artifact-to-machinery fit defects survive any number of review rounds and surface at the first execution attempt.
type: reference
schema: regularity/v1
generality: 1
induced_from: [2026-08-11-review-loop-cannot-measure-its-own-convergence]
domain: planning
created: 2026-11-03
last_verified: 2026-11-03
---

# A review that reads the artifact cannot see defects in the artifact's fit to the machinery that executes it

## Regularity

A review of an artifact (a plan, a spec) reads the artifact; execution reads the machinery the artifact is fed to. A defect that lies in the fit of the artifact to the machinery (a field the machinery ignores, a name it resolves differently) is therefore invisible to review at any number of rounds and is found by the first execution attempt.

## Generality

Level 1 — independent review of a plan or specification whose consumer is a separate piece of machinery. It is not claimed for reviews that also run the artifact through that machinery.

## Induced from

- [[2026-08-11-review-loop-cannot-measure-its-own-convergence]] — two artifact-to-machinery fit defects survived 11 and 12 review rounds and were found by the first execution attempt each time.

## Refutation

Held-out check: the single source is the only context, so the claim is held at level 1 on it alone. A fit defect found by a review that only read the artifact, in its first round, refutes it.
