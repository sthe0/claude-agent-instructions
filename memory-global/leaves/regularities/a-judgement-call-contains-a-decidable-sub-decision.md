---
name: a-judgement-call-contains-a-decidable-sub-decision
description: A judgement a specialist makes in prose almost always contains a sub-decision decidable from observable inputs — a classification, a boolean marker, a count against a threshold — and the irreducible perception is only the remainder.
type: reference
schema: regularity/v1
generality: 2
domain: coordination
induced_from: [regex-not-for-semantic-classification, partition-markers, 2026-06-27-critique-output-must-be-code-verified-into-downstream-artifact, 2026-06-30-policy-as-code-split-judgment-defer-blocked-layer, 2026-07-14-claim-provenance-ledger-reasoning-deliverable, 2026-07-14-determinize-memory-org-principle-in-code, 2026-07-22-semantic-gate-regex-to-judge]
created: 2026-10-27
last_verified: 2026-10-27
---
# A judgement a specialist makes in prose almost always contains a sub-decision decidable from observable inputs

## Regularity

A judgement call that a specialist currently makes from prose guidance almost always contains a sub-decision that is decidable from observable inputs — a classification by a known field, a boolean marker, a count against a threshold, a match against a known shape — beside a remainder that only a model or a human can judge. Left as one undivided call, the decidable part is re-derived on every occurrence and drifts with the reader; it is the remainder alone, not the whole call, that resists mechanization.

## Generality

Level 2 — ranges over recurring specialist judgements in the agent system. It is not claimed for a judgement that is perception throughout (whether a sentence reads clearly), where no observable input decides any part.

## Refutation

A recurring specialist judgement in which, after a deliberate search, no part could be decided from observable inputs would show the decomposition does not hold for that judgement.

## Induced from

- [[regex-not-for-semantic-classification]] — the rule half is a high-recall prefilter (cheap, precision-optional pattern match on text shape); the perception half is a fail-open model judge (`agentctl/advisor.py::judge_binary_ask`) that makes the actual semantic call; the extension seam is: any new semantic classifier mirrors `judge_binary_ask`'s contract (model, timeout, YES/NO protocol, exception → `False`) rather than inventing a new one.
- [[partition-markers]] — the rule half is the M1–M4 boolean-marker truth table (`scripts/agentctl/partition.py`), which derives the recommended/possible/not_required verdict mechanically; the perception half is the human/model judgment of whether each marker (independence, heterogeneity, blocking dependency, rollback risk) actually fires for this specific plan; the extension seam is the `--unit` materialization syntax, which lets new delivery-unit shapes (`inline`/`spawn`/`subtask`) attach to the same verdict machinery without changing the marker table.
