---
name: result-checked-against-its-result-image
description: Every produced result is critiqued against the result-image its plan declared before it is accepted or advanced past — the critique primitive (commonality/difference) applied to one stage. Skipping the check is itself a difficulty.
type: reference
schema: principle/v1
generality: 3
induced_from: [coordinator-pitfalls, 2026-06-24-gate-exemption-is-category-error-for-result-images]
created: 2026-06-26
last_verified: 2026-06-26
---

# Every result is critiqued against its declared result-image

## Principle

To know a step actually removed its difficulty (and not merely ran), **compare the actual result to
the `Expected result image` the plan declared, before accepting it or advancing**. This is the
**critique** primitive of ADR-0001 — extract the *difference* between expected and actual — applied to
a single stage: zero difference ⇒ pass; any difference ⇒ a difficulty, route to `overcome-difficulty`.
A skipped comparison is itself a difficulty, because the observable that would prove success was never
read.

## Generality

Level 3 — a cross-domain invariant. It holds for a plan stage, for the final verification against the
done criterion, for conflict resolution (compare two edits), and for principle induction (compare an
example to a repeat). The single operation — compare two objects, extract the difference — is the same
across all of them, which is why this sits at the top of the gradient.

## Induced from

- [[coordinator-pitfalls]] — "Advanced to the next stage … without comparing the actual outcome to the
  stage's `Expected result image:`."
- [[2026-06-24-gate-exemption-is-category-error-for-result-images]] — the result-image is the output of
  a process the same gate governs; mutating it unchecked is exactly what the comparison catches.

## Refutation

If a stage is found whose correctness genuinely cannot be expressed as any observable comparison
(no test, no command, no artifact, not even acceptance-review by a human) then the "always compare to
a result-image" form is too strong and must admit an explicit "irreducibly unobservable" class — at
which point the engine's measurable-vs-acceptance split is where that exception lives.

## See also

- `~/.claude-agent/CLAUDE.md` § Coordination spine; § On task resolution.
- [[coordinator-executes-through-specialists]] — the dispatch half of the same loop.
- `docs/adr/0001-consensus-architecture.md` § *The single primitive*.
