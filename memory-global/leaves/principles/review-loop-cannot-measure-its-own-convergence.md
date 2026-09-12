---
name: review-loop-cannot-measure-its-own-convergence
description: A review loop with a non-zero finding floor and a pass-required gate does not terminate by construction; a bounded round count is load-bearing — four mechanisms explain why no number of rounds alone moves the bound.
type: reference
schema: principle/v1
generality: 1
induced_from: [2026-08-11-review-loop-cannot-measure-its-own-convergence]
domain: coordination
created: 2026-08-25
last_verified: 2026-08-25
---

# A review loop with a non-zero finding floor does not terminate by construction

## Principle

To prevent a plan-review or enumeration loop from cycling indefinitely, **bound the round count
structurally** — as a hard element of the spec, not a heuristic. A loop with a pass-required gate
and a non-zero finding floor has no reachable fixed point and terminates only when someone stops it;
the bound is what makes "someone stopping it" a spec-compliant action rather than a surrender.

A review round that has not falsified any of the four non-convergence mechanisms below carries no
evidence its next round will converge. Instrumenting the loop (recording plan bytes, finding count,
finding text per round) is therefore a precondition to any hypothesis about WHY the loop is not
converging — without the record you cannot distinguish "volume problem" from "iatrogenic floor" from
"unanswerable study" from "artifact-machinery gap".

## Generality

Level 1 — holds across a handful of sibling contexts in the agentctl review system: a 12-round
plan-review series, a 15-round independent-review series, the post-approval path where the valve
was wired only to the pre-approval path (so the counter stayed at zero on the path the difficulty
actually travels), and the enumeration axis that had the same non-terminating shape from a
structurally distinct cause. A single iteration-control pattern (pass-required gate, incremental
repair) produces the same non-termination property across each.

## Induced from

- [[2026-08-11-review-loop-cannot-measure-its-own-convergence]] — four contexts: measuring breach
  count (79% iatrogenic across 15 rounds), showing the terminator valve was wired only to the
  pre-approval path, and confirming the enumeration axis carries the same non-terminating shape.

Four mechanisms, each individually sufficient to defeat termination:

1. **Unanswerable hypothesis** — if the record retains round identity but not finding count text
   or plan bytes, any volume-law hypothesis is unanswerable, not refuted. The loop cannot tell you
   whether it is converging; only external instrumentation can. Instrument before theorizing.

2. **No verb for "study cannot be done"** — when the review vocabulary admits only *covered/cut*
   for orders and *revise/pass/override* for verdicts, a premise-invalidating finding (the study
   itself cannot proceed as specified) and an ordinary plan defect are indistinguishable: both
   arrive as BLOCKING and route to revise-and-re-review. A finding raised in round 2 was
   re-litigated for eight more rounds before being recorded as a cut.

3. **Iatrogenic floor** — repair is an authoring act at the density where authoring introduces
   defects. The finding count falls to a NON-ZERO FLOOR (measured: 79% of breakers from round 4
   onward were introduced by the immediately preceding round's own fix). A pass-required gate with
   a non-zero floor has no reachable exit. Remedy: freeze finished parts, not shrink the artifact.

4. **Artifact-to-machinery gap** — review reads the artifact; execution reads the machinery.
   Defects that exist only in the fit between them (a stage whose commit the pre-commit spine would
   refuse, a guard watching a constant rather than the live array) are invisible to review at any
   number of rounds; they surface only on first execution.

## Refutation

If a review loop is shown to reliably terminate without a round bound — a pass-required gate with
an incremental-repair process that reaches zero findings in practice — then mechanism 3 does not
hold for that process and the principle narrows to "measure the floor before relying on
termination." If a review vocabulary is extended with a distinct verb for "study cannot be done"
that routes separately from "plan has a defect," mechanism 2 no longer applies to loops using that
verb. Both narrowings drive the principle to a higher generality level (what invariant subsumes the
remainder?), not to abandonment.

## See also

- [[2026-08-11-review-loop-cannot-measure-its-own-convergence]] — the generality-0 experience leaf
  this principle is induced from (four contexts, occurrence count ≥ `principle-promotion-threshold`).
- [effort-divergence-trigger.md](../effort-divergence-trigger.md) — the companion overrun detector;
  fires on PASSING stages when cost overruns the estimate, not on BLOCKING stages in a review loop.
  The review-round scale was explicitly cut at closure; this principle records what that cut costs.
- `docs/adr/0001-consensus-architecture.md` § *Principle as a concept with a generality gradient*.
