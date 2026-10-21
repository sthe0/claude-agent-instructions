---
name: 2026-02-15-sample-fetch-hang
description: A sample cluster for the split fixture — a fetch inside an auto-run check wedges the engine
type: reference
schema: difficulty/v1
generality: 0
generalized_into: []
resolution_confirmed_by_user: "fixture (AskUserQuestion: 'Yes — resolved')"
created: 2026-10-12
last_verified: 2026-10-12
---

# A network call in a control hangs the engine

## Difficulty
A network call placed in an engine-run check never returns, and the engine, which runs it without a timeout, waits forever.

**Functional ground:** an auto-run check has no timeout, so any blocking call in it blocks the engine instead of failing

## Order & criterion
Split out of [2026-01-05-sample-venue-difficulty](2026-01-05-sample-venue-difficulty.md) by memory-curator split on 2026-10-12: the contexts below arose under that leaf's difficulty and cluster here by the functional ground named above. The order and acceptance check of the original stay in the source leaf.

## Contexts

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

## Cost
Not costed separately: the contexts keep the cost recorded in [2026-01-05-sample-venue-difficulty](2026-01-05-sample-venue-difficulty.md).
