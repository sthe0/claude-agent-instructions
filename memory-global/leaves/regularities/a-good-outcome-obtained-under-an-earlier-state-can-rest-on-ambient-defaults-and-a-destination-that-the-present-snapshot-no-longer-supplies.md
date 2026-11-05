---
name: a-good-outcome-obtained-under-an-earlier-state-can-rest-on-ambient-defaults-and-a-destination-that-the-present-snapshot-no-longer-supplies
description: A precedent's good outcome can rest on ambient defaults and a destination that the present snapshot no longer supplies
type: reference
schema: regularity/v1
generality: 1
induced_from: [investigate-cited-precedent]
rule_derived: "lifted from investigate-cited-precedent.md; the leaf cites dated incidents (2026-08-19, 2026-09-03, 2026-09-17) but no experience leaf stem, so no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-good-outcome-obtained-under-an-earlier-state-can-rest-on-ambient-defaults-and-a-destination-that-the-present-snapshot-no-longer-supplies

## Regularity

A precedent (a run that was green, a commit that landed, a routing decision that was made) happened under a state that may have diverged from the present one: an assert added since, a default changed, a refactor that moved the load-bearing line. The invariant that made the precedent succeed can rest on an ambient default that supplied a factor nobody set, so setting the one visible knob reproduces the setting but not the outcome; and a same-named parameter can be supplied by a different code path than the one inspected, so a default found by grep in one module is a decoy for the path that actually failed. A precedent also supplies a destination (a queue, a venue, a system) while the criterion that selected it may have been a driver other than the one at hand (a file-attachment constraint rather than the tier), and two systems that share vocabulary can differ in ownership. Reasoning from the present code alone therefore reaches conclusions that contradict what the cited run did then.

## Generality

Level 1 — ranges over precedents (green runs, landed commits, routing decisions) that are cited by the user, implied by the task or reached for unprompted, whose circumstances may have drifted from the present snapshot. It is deliberately not claimed for precedents whose state is verified identical to the present one.

## Induced from

- [[investigate-cited-precedent]] — contributes the dated incidents of a drifted ambient default, a same-named parameter on a different code path, and a precedent destination chosen by a different criterion than the tier.

## Refutation

A set of precedents for which the invariant, read from the present code and defaults alone, always matched what the precedent did at the time (no drifted default, no wrong-path default, no precedent whose destination was driven by a different criterion) would show the divergence does not arise.
