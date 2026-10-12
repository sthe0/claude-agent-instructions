---
name: 2026-01-05-sample-venue-difficulty
description: A sample difficulty used as a split fixture — a check runs where the commits are not and a fetch hangs the engine
type: reference
schema: difficulty/v1
generality: 0
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

### 2026-01-10 — first sighting
- Where it arose: the first plan that pinned a worktree.
- Working plan: one stage, one verify_command.

### 2026-01-20 — venue drift
- Where it arose: a plan pinned repo_root to the main checkout.
- Working plan: three stages; the worktree held every commit.

### 2026-02-01 — venue drift
- Where it arose: the same plan family, a second ticket.
- Working plan: two stages; repo_root fixed by a substantive replan.

### 2026-02-15 — hang in fetch
- Where it arose: a verify_command that ran a fetch.
- Working plan: the command was wrapped as below.

```
### not a context heading
## not a section either
git fetch origin main
```

- The check now reads the already-fetched local ref.

### 2026-03-01 — hang again
- Where it arose: a final_check that ran a network probe.
- Working plan: the probe moved out of the auto-run check.

## Common core & variations
**Common:** a control is executed where its subject lives, without the network.

**Variations:** venue drift versus a hung fetch.

## Cost
Fixture: no cost recorded.
