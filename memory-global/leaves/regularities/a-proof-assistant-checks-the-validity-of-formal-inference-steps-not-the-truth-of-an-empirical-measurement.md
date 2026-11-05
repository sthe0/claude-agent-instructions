---
name: a-proof-assistant-checks-the-validity-of-formal-inference-steps-not-the-truth-of-an-empirical-measurement
description: A proof assistant checks that inference steps are valid, not that an empirical measurement is true; an unsourced claim is a free variable in a closed term
type: reference
schema: regularity/v1
generality: 1
induced_from: [formalization-ladder-l1-l3]
rule_derived: "lifted from formalization-ladder-l1-l3 (L3 refusal criterion and the Curry-Howard framing of L1); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-proof-assistant-checks-the-validity-of-formal-inference-steps-not-the-truth-of-an-empirical-measurement

## Regularity

A proof engine (LEAN, calculus of constructions, combinatory logic) checks that each inference step of a formal derivation is valid; it does not check the truth of an empirical axiom, which rests on the measurement itself, so formalizing a measured load, cost or latency figure adds ceremony without adding grounding, whereas a claim presented as fact without a recorded source, derivation or assumption mark is a free variable in a term asserted to be closed.

## Generality

Level 1 — ranges over formalizations of claims in a proof assistant (LEAN, calculus of constructions, combinatory logic) where an empirical figure such as a load, a cost or a latency enters as an axiom. It is deliberately not claimed for the validity of purely formal derivations, which the proof engine does check.

## Induced from

- [[formalization-ladder-l1-l3]] — contributes the L3 refusal criterion and the Curry-Howard framing of L1: a measured figure gains no grounding from formalization, and an unsourced claim is a free variable.

## Refutation

A case where lifting an empirical measurement (a measured quality score, a cost estimate) into a proof assistant changes whether the measured value is true, or where a claim with no recorded ground nonetheless type-checks as closed in the ledger.
