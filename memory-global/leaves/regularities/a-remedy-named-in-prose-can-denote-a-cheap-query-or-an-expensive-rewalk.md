---
name: a-remedy-named-in-prose-can-denote-a-cheap-query-or-an-expensive-rewalk
description: One natural-language remedy phrase can cover a read-only query and a full re-spawning re-walk whose costs differ by orders of magnitude, so a stated cost may belong to the other
type: reference
schema: regularity/v1
generality: 1
induced_from: [compose-query-cheaper-than-full-reissue]
rule_derived: "lifted from compose-query-cheaper-than-full-reissue; no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-remedy-named-in-prose-can-denote-a-cheap-query-or-an-expensive-rewalk

## Regularity

A plan author describing a remedy in natural language can use one phrase for two operations whose costs differ by orders of magnitude: a read-only query over state the engine already tracks, and a full re-walk that re-spawns every non-current unit. The phrase alone does not select between them, so a cost stated for the phrase can belong to the other operation.

## Generality

Level 1 — ranges over remedies named in plan prose where the engine offers both a lookup over recorded state and a full re-execution under similar wording. It is deliberately not claimed for remedies with a single implementation, where the stated cost always matches the code path.

## Induced from

- [[compose-query-cheaper-than-full-reissue]] — contributes the observed case behind the statement: "re-issue COMPOSE in full" was priced as the costly full walk although a read-only compose query costing nothing was available.

## Refutation

Plans whose remedy phrases always mapped to a single operation, so that the stated cost always matched the code path invoked, would show the claim false.
