---
name: 2026-01-05-sample-venue-difficulty
description: A sample difficulty used as a split fixture — a check runs where the commits are not and a fetch hangs the engine (contexts split by memory-curator on 2026-10-12)
type: reference
schema: difficulty/v1
generality: 0
generalized_into: []
resolution_confirmed_by_user: "fixture (AskUserQuestion: 'Yes — resolved')"
created: 2026-01-05
last_verified: 2026-03-01
---

# A sample difficulty used as a split fixture

## Difficulty
A control runs in a checkout that lacks the commits it judges, and a network call in
the control wedges the engine. Two sub-families of the same difficulty accumulated here.

## Order & criterion
Run the control where the commits are; keep network calls out of it.

**Acceptance check:** the control exits as declared in the venue that holds the commits.

## Contexts

Moved by memory-curator split on 2026-10-12:

- [A control runs where the commits are not](2026-01-05-sample-venue-drift.md) — 2 contexts
- [A network call in a control hangs the engine](2026-02-15-sample-fetch-hang.md) — 2 contexts

### 2026-01-10 — first sighting
- Where it arose: the first plan that pinned a worktree.
- Working plan: one stage, one verify_command.

## Common core & variations
**Common:** a control is executed where its subject lives, without the network.

**Variations:** venue drift versus a hung fetch.

## Cost
Fixture: no cost recorded.
