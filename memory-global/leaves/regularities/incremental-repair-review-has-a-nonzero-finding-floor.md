---
name: incremental-repair-review-has-a-nonzero-finding-floor
description: A review loop in which each round repairs the previous round's findings keeps producing findings at a floor above zero, because each repair is new material and each fresh reviewer applies a moving standard; the finding count does not trend to zero.
type: reference
schema: regularity/v1
generality: 1
domain: coordination
induced_from: [2026-08-11-review-loop-cannot-measure-its-own-convergence, review-loop-shifting-grounds-never-converges, 2026-08-04-no-circuit-breaker-on-verification-effort, 2026-08-17-diagnosing-has-no-exit-to-the-customer, 2026-09-03-published-text-writer-gate-effort-divergence-deadlock, 2026-10-06-season2-metric-instruction-tuning]
created: 2026-10-27
last_verified: 2026-10-27
---
# A review loop in which each round repairs the previous round's findings keeps producing findings at a floor above zero, because each repair is new material and each fresh reviewer applies a moving standard

## Regularity

A review loop in which every round repairs the previous round's findings does not converge to zero findings: each repair is new, unreviewed material; a fresh reviewer reads the whole object again against a standard that shifts from reviewer to reviewer; an enumeration of open questions grows as the plan it enumerates is edited. The finding count settles at a floor above zero, so a gate that requires a clean pass has no reachable fixed point, and the loop's own output — the latest finding list — carries no signal of whether the next round will be the last.

## Generality

Level 1 — ranges over iterative review or enumeration loops gated on a pass verdict — plan reviews, question enumerations, publication judges, metric-tuning rounds. It is not claimed for a review against a fixed, closed checklist whose items, once satisfied, cannot be re-opened by the repair of another item.

## Refutation

A run of incremental-repair review loops whose finding counts reached zero by themselves, without a bound or a stop decision, in most observed cases would show the floor is not structural for that kind of loop.

## Induced from

- [[2026-08-11-review-loop-cannot-measure-its-own-convergence]] — four contexts: measuring breach
  count (79% iatrogenic across 15 rounds), showing the terminator valve was wired only to the
  pre-approval path, and confirming the enumeration axis carries the same non-terminating shape.
