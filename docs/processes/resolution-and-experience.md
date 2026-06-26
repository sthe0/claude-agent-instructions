# Resolution and experience

> How a task is closed: the resolution gate, the criterion type that shapes the confirmation, and the decision of whether to record the experience. The binding detail lives in CLAUDE.md and the recording-experience leaf; this page describes the flow.

A substantive task is not over when the work looks done. It is closed by an explicit, gated sequence that ends in user confirmation, after which the system decides whether the task taught it anything worth keeping.

## The resolution gate

The engine holds the task behind a non-skippable **resolution gate**. Two things must happen in order:

1. **Verify-final** — every stage must be passed and the plan's final verification must succeed against the overall done criterion. A failed verification routes to the difficulty process, not to closing.
2. **Resolve** — the user's explicit confirmation that the task is resolved is recorded; an empty confirmer is refused.

The gate mechanism is part of [the coordination engine and its state machine](../architecture/coordination-engine.md).

## Criterion type shapes the question

How the task is confirmed depends on how its done criterion is verified:

- **Measurable** — there is an objective check (a test, a command's output, a file's presence). The check is run, and the confirmation question can be generic.
- **Acceptance-review** — there is no objective check, so the user accepts on review. The confirmation question must name the specific observation the user just performed, so that "yes" cannot quietly mean "the explanation sounded right".

The confirmation itself is a one-line recap of requested-versus-delivered followed by a structured question, asked in the user's language. The full rule is in [CLAUDE.md](../../CLAUDE.md) § On task resolution.

## Recording the experience

Once the task is confirmed resolved, the system decides whether to record it. The quality bar: record only if a future similar task would want to read it first — a non-obvious choice invisible from the code or commits, a reusable difficulty overcome, a revealed gap in tooling or memory, or a meaningful amount of saved rediscovery. Otherwise nothing is recorded; memory bloat is worse than a gap. When an experience is worth keeping it is written as a difficulty-schema leaf, after searching for an analogous leaf to extend rather than duplicate. The how-to is in [the recording-experience leaf](../../memory-global/leaves/recording-experience.md), and the leaf shape itself in [the experience leaf schema](../../memory-global/leaves/experience-leaf-schema.md).

## See also

- [The task lifecycle](task-lifecycle.md) — where resolution sits in the end-to-end flow.
- [The recording-experience leaf](../../memory-global/leaves/recording-experience.md) — the quality bar and search-before-record discipline.
- [Self-improvement](self-improvement.md) — the sibling process for behavioural corrections.
