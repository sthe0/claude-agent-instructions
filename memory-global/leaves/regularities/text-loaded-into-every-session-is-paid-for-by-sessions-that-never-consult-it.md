---
name: text-loaded-into-every-session-is-paid-for-by-sessions-that-never-consult-it
description: Always-loaded text costs tokens in every session, including those that never need a rule's rationale or enumeration; the directive is needed often, the elaboration rarely
type: reference
schema: regularity/v1
generality: 1
induced_from: [claude-md-elaboration]
rule_derived: "lifted from claude-md-elaboration (Difficulty section); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# text-loaded-into-every-session-is-paid-for-by-sessions-that-never-consult-it

## Regularity

A file loaded into every session costs tokens in every session, including the sessions that never consult a given rule's rationale, worked detail or enumeration. A rule's directive is needed in most sessions, while its elaboration is needed in few.

## Generality

Level 1 — ranges over rulebooks and other files loaded unconditionally into each session, where each rule has a short directive and a longer elaboration. It is deliberately not claimed for files loaded on demand, nor for the correctness of any particular rule.

## Induced from

- [[claude-md-elaboration]] — contributes the observed practice behind the statement: CLAUDE.md keeps each rule's directive while its rationale, worked detail and enumerations move to a leaf one link away.

## Refutation

Measuring sessions and finding that the size of the always-loaded file has no effect on per-session token cost, or that a rule's rationale and enumerations are consulted in most sessions, would show the claim false.
