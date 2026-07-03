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

### Reconstruct the invariant, not the surface knob (ambient defaults drift)

A precedent-type norm ("we did X at param P=v and it worked") often silently depends on an **ambient** parameter or default that was true then and has since drifted. Following the *surface knob* you remember reproduces the visible setting but not the *invariant* the precedent actually relied on. When you invoke a precedent, reconstruct the **invariant** — the relation over all the inputs that had to hold for the good outcome — and check whether it still holds under the **current** defaults, not just whether you set the one knob you recall.

Two traps make this fail. (a) An *ambient default* the precedent silently relied on may have **drifted** since — the invariant held then because a default supplied a factor you never set, and that default is no longer what it was. (b) A same-named parameter may be supplied by a **different code path** than the one you are inspecting: a default you found by grep in one module is not necessarily the default the failing path actually read (different loaders, config layers, or pipelines can each carry their own same-named field). So trace each factor of the invariant from the failing frame back to the real config source it read, and match it against the **observed values** — the error's own numbers — before naming a cause. Reproducing only the surface knob you remember reproduces the setting, not the invariant; and a plausible default on the wrong path is a decoy, not the cause.

## See also

- [[doubt-own-snapshot]] — the requirement-side twin (refresh your own source before doubting).
- [[workflow-debug-investigation]] — reference-baseline pass; a known-good run is a cited precedent.
- [[reasoning-and-task-solving]] — understand before acting.
