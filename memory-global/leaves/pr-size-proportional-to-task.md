---
name: pr-size-proportional-to-task
description: A PR's diff stays proportionate to what the task brings in (a ported prototype, the stated scope) — a diff more than 3x that size, or one carrying an unnamed deliverable, is justified addition by addition or split; tests and golden data stay proportionate to the original's.
schema: leaf/v1
type: feedback
created: 2026-10-27
last_verified: 2026-10-27
---

## Difficulty

A task ported a ~1.2k-line Python prototype (211 of them tests) into a Go service. The PR that went to human review was about 7x the source: tests plus golden testdata alone were ~5900 lines (97 golden files copying the prototype's whole output tree), and a local-run launcher plus a frontend UI rode along — added as a verification aid in one stage and grown through review rounds, never reconsidered as a separate deliverable. Neither the developer's self-review nor the independent code-reviewer compared the diff size with the size of the ported code, so the human reviewer was the first to see that the port was buried under everything around it. Each round of review on such a PR costs reviewer attention proportional to the whole diff, not to the task.

## Guidance

**Compare the diff with what the task brings in, before the PR goes to review.** The reference is the size of the thing being delivered — the ported prototype, the stated scope of the brief. A diff more than 3x that reference is a should-fix: either each addition is justified in the PR description (why this test, why this file is part of *this* change), or the extra part is split into its own PR.

**A deliverable the brief never named is a split candidate on its own**, whatever the ratio — a local runner, a UI, a docs page for a new mode. It may be worth building; it is rarely worth reviewing together with the port.

**Tests and golden data stay proportionate to the original's.** Parity with a prototype is shown on a few representative cases, not by committing the prototype's whole output tree.

**Split along the import boundary.** Check which packages the core change imports: when the core does not import the accreted part (here: the worker packages did not import the local runner), the split is clean and the second PR depends on the first, not the other way round.

This is not partition-markers' "volume alone is not a reason": there, a large but uniform change stays one PR. Here, the signal is disproportion *against the task* — a sign of scope that grew during execution.

## See also

- [[partition-markers]] — delivery partition decided before execution; this rule re-checks it at the PR boundary, after scope has had a chance to grow.
- [`skills/specializations/code-reviewer/SKILL.md`](../../skills/specializations/code-reviewer/SKILL.md) — Maintainability axis carries the rule; the developer's self-review applies the same lens.
