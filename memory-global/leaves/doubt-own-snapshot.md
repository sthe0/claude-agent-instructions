---
name: doubt-own-snapshot
description: When a user's stated requirement appears to contradict what you observe, first suspect your OWN source is stale or incomplete and refresh it before doubting the requirement or asking a clarifying question built on a false premise.
type: feedback
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-02
---

# Before you doubt a requirement, doubt your own snapshot

The short rule lives in CLAUDE.md § Escalation to the user; this leaf carries the full narrative.

## Difficulty

Challenging a **correct** requirement from an out-of-date local view wastes the user's attention and erodes trust; the apparent contradiction is far more often your staleness than the user's error.

## Guidance

When a user's stated requirement appears to contradict what you observe (a command / file / flag the user says exists that you don't see), first suspect your OWN source is stale or incomplete — `pull` / `fetch` / re-read the authoritative source (fresh state may live on another branch or machine) **before** doubting the requirement or asking a clarifying question built on a false premise ("X doesn't exist"). A stale local snapshot is not ground truth.

Critically evaluate every clarified requirement for adequacy **and** non-contradiction, but resolve a perceived contradiction to root — self-staleness included — before escalating.

## See also

- `~/.claude-agent/CLAUDE.md` § Escalation to the user — the short pointer that loads this leaf.
- [[capability-before-offload]] — the acting-side twin: doubt your own claim of *"can't"*, not the user's expectation that you can.
