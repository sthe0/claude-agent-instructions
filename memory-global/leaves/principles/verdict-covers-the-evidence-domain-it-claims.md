---
name: verdict-covers-the-evidence-domain-it-claims
description: A mechanism that issues a verdict must actually cover the evidence domain its verdict claims — a gate demanding proof whose prover is absent, and a checker enumerating from one source while reading another, are the same fault. Escape hatches must stay reachable, diagnosed, typed and counted.
type: reference
schema: principle/v1
generality: 2
domain: development
induced_from: [ask-user-question-split-turn, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]
created: 2026-08-04
last_verified: 2026-08-04
---

# A verdict covers the evidence domain it claims

## Principle

To make a mechanism's verdict mean what it says, **make it cover the evidence domain the verdict
claims — and where it cannot, say so instead of answering.** A judging mechanism has three possible
honest answers, not two: *satisfied*, *not satisfied*, and *I could not look*. Collapsing the third
into either of the first two is the fault; which of the two it collapses into decides who it hurts.

This task's two forms of the same fault:

**The gate form — the absent prover.** A fail-closed gate demands proof produced by something else.
When that producer is not installed in the root the session actually loads from, the gate demands
proof that cannot exist and blames whoever hit it. So an engine gate backed by an external
satisfier must:

1. keep an escape **reachable** — without one, a dead prover bricks the spine, and a gate that
   cannot be satisfied trains its users to route around gates in general;
2. **diagnose** before refusing — distinguish a missing PROOF from an absent PROVER, mechanically
   wherever the engine can observe the difference, and say which one it found;
3. record a **typed** reason for each escape, not free text alone — a note explains one escape, a
   token counts all of them;
4. be **counted**, with a standing report — an escape nobody counts stops being the exception, and
   the change is invisible precisely because it is gradual.

**The checker form — the unread domain.** A checker that enumerates its domain from one source (the
git index) while reading content from another (the working tree) cannot see a new artifact at all,
so it answers OK *for the wrong reason*: not "I looked and it was fine" but "I did not look." A
green that means *not looked at* is worse than a red, because a red gets investigated. The fix
belongs in the producing step — stage the artifact before you verify — not in the checker, which is
behaving exactly as designed.

**The corollary, which is where this is most often got wrong:** a checker can be
index-ENUMERATED yet manifest-SATISFIED. Making an artifact **visible** to a checker and
**satisfying** that checker are two different acts. Surfacing an obligation never discharges it, and
a plan that treats them as one step produces a stage that is green precisely because nobody has
looked yet.

## Generality

Level 2 — a class of tasks: the design of any mechanism that issues a verdict over evidence it does
not itself produce. It ranges over engine gates whose satisfier is an out-of-process hook, over
repo verifiers that enumerate one source and read another, and over CI checks that report on a
subset they never state. It is not claimed at level 3: the statement is about *judging mechanisms*,
and lifting it to "every mechanism reports its own coverage" would need instances outside that
class. Finding one is what would promote it.

## Induced from

- [[ask-user-question-split-turn]] — the plan-presentation delivery gate and its residuals. The
  unreachable-prover case is one of those residuals made concrete: `approve` refused for "no
  delivery proof recorded" on a machine where the hook that writes the proof was not registered in
  the root the session loads from, and nothing in the refusal said so.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the verifier form, met
  first as a green stage followed by a red pre-commit: a checker enumerating from the git index over
  a working tree that had moved on.

## Refutation

The principle is refuted, or driven to a broader form, by a judging mechanism that provably cannot
distinguish "not satisfied" from "could not look" — where the coverage question is undecidable to
the mechanism itself rather than merely unimplemented. At that point the requirement is not
"diagnose" but "declare the undecidability at the boundary", and the statement must widen to admit
a mechanism whose honest output is a permanent third state rather than a diagnosis.

The narrower escape-hatch half carries its own refutation: if escapes turn out to be genuinely rare,
the typed reason is overhead. The measurement is what refutes that — 3 of the first 5 delivery
stamps on this machine were overrides — and `scripts/escape-hatch-report.py` is what will show
whether the ratio falls once the gate names the real cause.

## See also

- [[result-checked-against-its-result-image]] — the same discipline one level up: a result is not
  accepted until compared to its declared image. This principle is about the comparison's *evidence*
  being real.
- [[regex-not-for-semantic-classification]] — a related determinization fault: a mechanism deciding
  something it cannot actually observe (meaning) and issuing a hard verdict anyway.
- `scripts/agentctl/README.md` § Two config roots — the engine-author-facing form of the gate half.
- `scripts/lib/hook_wiring.py` — the WIRED / ABSENT / UNKNOWN probe, where the third answer is
  first-class by construction.
