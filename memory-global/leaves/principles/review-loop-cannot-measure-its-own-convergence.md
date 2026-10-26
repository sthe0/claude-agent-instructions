---
name: review-loop-cannot-measure-its-own-convergence
description: A review loop with a non-zero finding floor and a pass-required gate does not terminate by construction; a bounded round count is load-bearing — four mechanisms explain why no number of rounds alone moves the bound.
type: reference
schema: principle/v1
generality: 1
grounded_in: [regularity:incremental-repair-review-has-a-nonzero-finding-floor]
domain: coordination
created: 2026-08-25
last_verified: 2026-08-25
---

# A review loop with a non-zero finding floor does not terminate by construction

## Principle

Because an incremental-repair review loop has a nonzero finding floor ([[incremental-repair-review-has-a-nonzero-finding-floor]]), for the goal of a review loop that ends in a recorded decision at bounded cost, **bound the round count structurally** — as a hard element of the spec, not a heuristic. A loop with a pass-required gate and a non-zero finding floor has no reachable fixed point and terminates only when someone stops it; the bound is what makes "someone stopping it" a spec-compliant action rather than a surrender.

A review round that has not falsified any of the four non-convergence mechanisms below carries no
evidence its next round will converge. Instrumenting the loop (recording plan bytes, finding count,
finding text per round) is therefore a precondition to any hypothesis about WHY the loop is not
converging — without the record you cannot distinguish "volume problem" from "iatrogenic floor" from
"unanswerable study" from "artifact-machinery gap".

## Goal class

A plan-review, enumeration or publication loop that ends in a recorded decision at bounded cost, rather than cycling until someone abandons it.

## Generality

Level 1 — holds across a handful of sibling contexts in the agentctl review system: a 12-round
plan-review series, a 15-round independent-review series, the post-approval path where the valve
was wired only to the pre-approval path (so the counter stayed at zero on the path the difficulty
actually travels), and the enumeration axis that had the same non-terminating shape from a
structurally distinct cause. A single iteration-control pattern (pass-required gate, incremental
repair) produces the same non-termination property across each.

## Grounded in

- [[incremental-repair-review-has-a-nonzero-finding-floor]] — A review loop in which each round repairs the previous round's findings keeps producing findings at a floor above zero, because each repair is new material and each fresh reviewer applies a moving standard; the finding count does not trend to zero.

## Refutation

The principle fails its goal if loops with a structural bound still end without a recorded decision — abandoned, or overridden outside the bound — or if the bound is reached so routinely that the released decision is no better informed than a stop at round one.

## See also

- [[2026-08-11-review-loop-cannot-measure-its-own-convergence]] — the generality-0 experience leaf
  this principle is induced from (four contexts, occurrence count ≥ `principle-promotion-threshold`).
- [effort-divergence-trigger.md](../effort-divergence-trigger.md) — the companion overrun detector;
  fires on PASSING stages when cost overruns the estimate, not on BLOCKING stages in a review loop.
  The review-round scale was explicitly cut at closure; this principle records what that cut costs.
- `docs/adr/0001-consensus-architecture.md` § *Principle as a concept with a generality gradient*.
