---
name: investigate-cited-precedent
description: When the user points at a specific prior success/precedent (ticket, run, commit, PR) as evidence, read HOW that precedent actually did it before theorizing from the current code snapshot — the present state may have diverged from the precedent's.
type: feedback
schema: leaf/v1
created: 2026-07-03
last_verified: 2026-07-03
---

# Investigate a user-cited precedent before theorizing from the current snapshot

## Difficulty

When the user cites a concrete precedent ("we measured this in DEEPAGENT-392", "this landed in commit X", "the green run did Y") as evidence that something is possible or was done differently, building a theory from the **current** code/state instead of reading how the precedent actually worked reaches wrong conclusions and wastes the user's attention. The present snapshot may have diverged from the precedent's state — an assert added since, a default changed, a refactor that moved the load-bearing line — so reasoning from "what the code says now" silently contradicts "what the cited run did then".

## Guidance

On a **user-cited precedent**, read *that precedent* first — its ticket comments, its run parameters, its code state at that time (`arc log`/`arc blame` by date+path, the PR diff, the run's global params) — **before** constructing a hypothesis from the present code. The user chose that exemplar deliberately; it is a load-bearing datum, and honoring it usually collapses the hypothesis space fast (e.g. a green run at the same params means the difference is in state, not in feasibility). Do not keep re-deriving from the current snapshot after the user has explicitly handed you a working example.

This is the positive-exemplar twin of [[doubt-own-snapshot]] (there the user asserts a requirement and you refresh your stale source; here the user hands you a prior success and you go study it). It is also the concrete form of the mini-OD **reference-baseline** pass ([[workflow-debug-investigation]] § Reference baseline): a known-good run *is* such a precedent — diff the failing run against it at block/param order before diving into infra logs.

## See also

- [[doubt-own-snapshot]] — the requirement-side twin (refresh your own source before doubting).
- [[workflow-debug-investigation]] — reference-baseline pass; a known-good run is a cited precedent.
- [[reasoning-and-task-solving]] — understand before acting.
