---
name: root-direct-edits-accumulate-context-and-skip-review
description: Production work done directly by the root coordinator accumulates file reads and tool output in the one context every later turn pays for, and passes through no separate actor who could review it.
type: reference
schema: regularity/v1
generality: 2
domain: coordination
induced_from: [coordinator-pitfalls]
rule_derived: "Seed regularity lifted from a coordinator-pitfalls row (ADR-0001 S2.5); no experience leaf recorded."
created: 2026-10-27
last_verified: 2026-10-27
---
# Production work done directly by the root coordinator accumulates file reads and tool output in the one context every later turn pays for, and passes through no separate actor who could review it

## Regularity

Production work done directly from the root coordinator's thread leaves its file reads, diffs and tool output in the one context that every later turn re-reads and pays for, and it passes through no actor other than the one that wrote it: there is no fresh context to review it and no return marker to verify against. Work dispatched to a specialist leaves only its conclusion in the root's context and arrives through a marker the root can check.

## Generality

Level 2 — ranges over substantive production work in a coordinator-and-specialists system whose root context is carried across turns. It is not claimed for a change small enough that its context cost and review risk are below the cost of a dispatch.

## Refutation

Sessions in which substantive root-direct edits left the root's context no larger than dispatched work would have, and showed no more unreviewed defects than dispatched work, would show the accumulation and the missing review do not occur.

## Induced from

- [[coordinator-pitfalls]] — "Root does most edits via Bash/Edit/Write → `Task → developer` for
  code; invoke `overcome-difficulty` when stuck." The recurring symptom this principle generalizes.
