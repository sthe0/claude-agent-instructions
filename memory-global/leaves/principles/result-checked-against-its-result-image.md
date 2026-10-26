---
name: result-checked-against-its-result-image
description: Every produced result is critiqued against the result-image its plan declared before it is accepted or advanced past — the critique primitive (commonality/difference) applied to one stage. Skipping the check is itself a difficulty.
type: reference
schema: principle/v1
generality: 3
grounded_in: [regularity:a-step-that-ran-is-not-thereby-a-step-that-worked]
created: 2026-06-26
last_verified: 2026-06-26
---

# Every result is critiqued against its declared result-image

## Principle

Because a step that ran is not thereby a step that worked ([[a-step-that-ran-is-not-thereby-a-step-that-worked]]), for the goal of knowing that a step removed its difficulty, **compare the actual result to the `Expected result image` the plan declared, before accepting it or advancing**. This is the **critique** primitive of ADR-0001 — extract the *difference* between expected and actual — applied to a single stage: zero difference ⇒ pass; any difference ⇒ a difficulty, route to `overcome-difficulty`. A skipped comparison is itself a difficulty, because the observable that would prove success was never read.
## Goal class

Knowing, before accepting a step or advancing past it, that the step removed the difficulty it was planned against — for any plan stage, specialist delivery or fix whose success is a change of state in its material.

## Generality

Level 3 — a cross-domain invariant. It holds for a plan stage, for the final verification against the
done criterion, for conflict resolution (compare two edits), and for principle induction (compare an
example to a repeat). The single operation — compare two objects, extract the difference — is the same
across all of them, which is why this sits at the top of the gradient.

## Grounded in

- [[a-step-that-ran-is-not-thereby-a-step-that-worked]] — A step that executes to completion and a step that removed its difficulty are independent outcomes; nothing in the execution itself tells them apart, only a comparison of the actual state against a declared expected state does.

## Refutation

The principle fails its goal if steps accepted after a zero-difference comparison against their declared image are later found not to have removed their difficulty at a rate no lower than steps accepted without the comparison — the comparison then does not deliver the knowledge it is for, and the image or the comparison must be re-formed.

## See also

- `~/.claude-agent/CLAUDE.md` § Coordination spine; § On task resolution.
- [[coordinator-executes-through-specialists]] — the dispatch half of the same loop.
- `docs/adr/0001-consensus-architecture.md` § *The single primitive*.
