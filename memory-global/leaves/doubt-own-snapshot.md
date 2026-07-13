---
name: doubt-own-snapshot
description: When a user's stated requirement appears to contradict what you observe, first suspect your OWN source is stale or incomplete and refresh it before doubting the requirement or asking a clarifying question built on a false premise.
type: feedback
schema: leaf/v1
created: 2026-07-02
last_verified: 2026-07-13
---

# Before you doubt a requirement, doubt your own snapshot

The short rule lives in CLAUDE.md § Escalation to the user; this leaf carries the full narrative.

## Difficulty

Challenging a **correct** requirement from an out-of-date local view wastes the user's attention and erodes trust; the apparent contradiction is far more often your staleness than the user's error.

## Guidance

When a user's stated requirement appears to contradict what you observe (a command / file / flag the user says exists that you don't see), first suspect your OWN source is stale or incomplete — `pull` / `fetch` / re-read the authoritative source (fresh state may live on another branch or machine) **before** doubting the requirement or asking a clarifying question built on a false premise ("X doesn't exist"). A stale local snapshot is not ground truth.

Critically evaluate every clarified requirement for adequacy **and** non-contradiction, but resolve a perceived contradiction to root — self-staleness included — before escalating.

The snapshot to refresh is not only your view of the stated order but the functional place behind it — a stale order can be literally accurate yet already fill the wrong position in its organizedness; see [[function-place-difficulty]] for reconstructing the function an order serves before optimizing it at face value.

### The planning direction: before planning potentially-already-done work, refresh the authoritative source

The same stale-snapshot difficulty has a *planning-side* twin. Before you plan work that another session, machine, or collaborator **could already have produced** — a merge, a hook deploy, a refactor, a migration — refresh the authoritative source (`git fetch origin` + re-read the live state / branch tips / deployed config) **before** committing the plan. Planning against a stale local view silently re-plans already-done work; the wasted effort surfaces only when the plan is executed or, worse, when someone checks the live state you should have checked first.

Concrete instance (2026-07-11): a whole plan plus **four** thinker-review rounds were spent against a `main` snapshot **19 commits stale**, on work (a "loops" feature) that had *already been merged and deployed* — discovered only when the user asked "сверься со свежим main". Personal memory was independently wrong too (it claimed the primary checkout sat on a feature branch when it was on `main`). Two separate stale snapshots, one avoidable `fetch` away from being caught at plan time. So: for any task whose result is the kind of thing that gets done once and shared, a fetch-and-re-read is a **plan-time precondition**, not a nicety.

### The outage direction: doubt your own probe before declaring a service down

The same difficulty has an *external-failure* twin. When a service appears to fail, your bare probe is the stale snapshot — a `curl`/one-shot call can fail for a dozen reasons that are not "the service is down" (wrong client, missing ambient context, expired token, a transient, the wrong endpoint). Before you declare it down or escalate the outage to the user, reproduce the failure with the **real client** the working path uses, and actively seek a **counter-example** (open the UI, try a second access path) — a genuine outage survives both, a false premise does not. Never launder the unverified premise into a sub-agent question ("the endpoint is down — how do I get access?"): the sub-agent inherits the premise and **circularly confirms** it. Route it through overcome-difficulty (reproduce → ≥2 hypotheses, each with a cheap falsifier) instead. Enforced pre-emptively by `hook-escalation-diagnosis-gate.py` (denies the un-diagnosed AskUserQuestion) and, as a Stop backstop, by the `escalation_without_diagnosis` turn guardian.

## See also

- `~/.claude-agent/CLAUDE.md` § Escalation to the user — the short pointer that loads this leaf.
- [[mirror-working-caller-before-bypass]] — the same "use the real working path, not a bypass" instinct on the ambient-context axis.
- [[capability-before-offload]] — the acting-side twin: doubt your own claim of *"can't"*, not the user's expectation that you can.
