---
name: pr-size-proportional-to-task
description: A PR's diff stays proportionate to what the task brings in (a ported prototype, the stated scope) — a diff more than 3x that size, or one carrying an unnamed deliverable, is justified addition by addition or split; tests and golden data stay proportionate to the original's.
schema: principle/v1
converted_from: leaf/v1
grounded_in: [regularity:a-diff-that-outgrows-its-task-costs-reviewer-attention-by-the-whole-diff, regularity:a-change-outside-the-state-its-consumers-read-is-unseen-by-them, regularity:what-a-change-does-not-carry-ends-with-its-authors-context]
grounding_judged: 99e5852bcfeaead9b746e4bfbb863eebb5543a0036fceca8f6b4d36409fe6f05
grounding_override: The leaf bundles several clauses resting on different regularities; across two judge rounds the judge alternately names a missing ground and then an irrelevant one among the added grounds, so no single grounded_in satisfies both; the repair is a per-clause split of the leaf, a content change outside this in-place conversion.
type: feedback
created: 2026-10-27
last_verified: 2026-10-27
---

## Difficulty

A task ported a ~1.2k-line Python prototype (211 of them tests) into a Go service. The PR that went to human review was about 7x the source: tests plus golden testdata alone were ~5900 lines (97 golden files copying the prototype's whole output tree), and a local-run launcher plus a frontend UI rode along — added as a verification aid in one stage and grown through review rounds, never reconsidered as a separate deliverable. Neither the developer's self-review nor the independent code-reviewer compared the diff size with the size of the ported code, so the human reviewer was the first to see that the port was buried under everything around it. Each round of review on such a PR costs reviewer attention proportional to the whole diff, not to the task.

## Principle

To keep a port or a scoped task from being buried under its surroundings in review, compare the PR's diff with what the task brings in (the ported prototype, the stated scope) before it goes to review; justify each addition in the description or split the extra part into its own PR when the diff exceeds 3x that reference or carries a deliverable the brief never named (a local runner, a UI, a docs page); keep tests and golden data proportionate to the original's; and split along the import boundary so the second PR depends on the first.

## Goal class

PRs whose review cost is proportional to the task they deliver

## Grounded in

- [[a-diff-that-outgrows-its-task-costs-reviewer-attention-by-the-whole-diff]]
- [[a-change-outside-the-state-its-consumers-read-is-unseen-by-them]]
- [[what-a-change-does-not-carry-ends-with-its-authors-context]]

## Guidance

**Compare the diff with what the task brings in, before the PR goes to review.** The reference is the size of the thing being delivered — the ported prototype, the stated scope of the brief. A diff more than 3x that reference is a should-fix: either each addition is justified in the PR description (why this test, why this file is part of *this* change), or the extra part is split into its own PR.

**A deliverable the brief never named is a split candidate on its own**, whatever the ratio — a local runner, a UI, a docs page for a new mode. It may be worth building; it is rarely worth reviewing together with the port.

**Tests and golden data stay proportionate to the original's.** Parity with a prototype is shown on a few representative cases, not by committing the prototype's whole output tree.

**Split along the import boundary.** Check which packages the core change imports: when the core does not import the accreted part (here: the worker packages did not import the local runner), the split is clean and the second PR depends on the first, not the other way round.

This is not partition-markers' "volume alone is not a reason": there, a large but uniform change stays one PR. Here, the signal is disproportion *against the task* — a sign of scope that grew during execution.

## See also

- [[partition-markers]] — delivery partition decided before execution; this rule re-checks it at the PR boundary, after scope has had a chance to grow.
- [`skills/specializations/code-reviewer/SKILL.md`](../../skills/specializations/code-reviewer/SKILL.md) — Maintainability axis carries the rule; the developer's self-review applies the same lens.
