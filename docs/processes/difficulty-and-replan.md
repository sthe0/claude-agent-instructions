# Difficulty and replan

> What happens when work is stuck: a difficulty is declared, investigated, and critiqued before the plan is revised. The cognition lives in the overcome-difficulty skill; this page describes when the process fires and how the engine shapes it.

Work does not always match the plan. When a stage's actual result diverges from the result image the plan declared — or when the check that would compare them cannot be performed at all — that divergence is a [difficulty](../concepts/difficulty.md), and it is handled by an explicit process rather than ad-hoc retries.

## When it fires

The difficulty process is the response to verification failure, a blocker, a repeated error, a surprising output, a plan mismatch, two or more process corrections in a row, or the same root-cause narrative repeated without new evidence. It runs before retrying an external workflow that has already failed once. The full trigger list and the cognition for each phase are in [the overcome-difficulty skill](../../skills/overcome-difficulty/SKILL.md).

## Declare, investigate, critique

When a stage fails, the engine routes the task into its DIAGNOSING node and enforces a fixed three-phase shape:

1. **Declare** — name the difficulty as a desired-vs-actual divergence and localize the moment things diverged.
2. **Investigate** — gather evidence, form at least two hypotheses, and test them.
3. **Critique** — challenge the diagnosis before acting on it.

The engine **blocks the replan step until the difficulty record is complete** — the three phases cannot be skipped on the way to revising the plan.

## Replan and re-arm

The output of the process is a concrete replanning task: a revision to the plan that removes the difficulty and lets the original user task resume on the new plan. A substantive replan re-arms the task at plan-ready, which means the revised plan passes back through the approval gate before execution continues — a plan is never edited in place past the planning node. The engine and its DIAGNOSING node are described in [the coordination engine and its state machine](../architecture/coordination-engine.md).

## See also

- [Difficulty](../concepts/difficulty.md) — the foundational object this process removes.
- [The overcome-difficulty skill](../../skills/overcome-difficulty/SKILL.md) — the cognition for each phase.
- [Planning](planning.md) — the plan a replan revises.
