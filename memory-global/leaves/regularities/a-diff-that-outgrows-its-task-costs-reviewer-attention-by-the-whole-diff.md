---
name: a-diff-that-outgrows-its-task-costs-reviewer-attention-by-the-whole-diff
description: Review cost scales with the whole diff, not the task; scope grown during execution escapes self-review and code review until the human reviewer sees it
type: reference
schema: regularity/v1
generality: 1
induced_from: [pr-size-proportional-to-task]
rule_derived: "lifted from pr-size-proportional-to-task.md (Difficulty); no experience leaf backs it"
introduced_by: grounding-norm
created: 2026-10-30
last_verified: 2026-10-30
---

# a-diff-that-outgrows-its-task-costs-reviewer-attention-by-the-whole-diff

## Regularity

Each round of review on a PR costs reviewer attention proportional to the whole diff, not to the task it delivers. Scope added during execution (tests, golden data copying a prototype's whole output tree, a local launcher, a UI added as a verification aid and grown through review rounds) accretes without being reconsidered as a separate deliverable, because the developer's self-review and an independent code review each read the diff's content and neither compares its size with the size of what the task brings in. The human reviewer is then the first to see that the delivery is buried under everything around it.

## Generality

Level 1 — ranges over PRs whose task has a definite size reference (a ported prototype, a stated scope) and whose diff grew during execution. It is deliberately not claimed for large changes that are uniform and in scope, where size alone is not disproportion.

## Induced from

- [[pr-size-proportional-to-task]] — contributes the observed case behind the statement: a small port reached human review buried under tests, golden data, a local runner and a UI that no earlier review compared with the task's size.

## Refutation

Reviewer effort per round found to be unrelated to diff size; or self-review and independent review found to flag diff-to-task disproportion before human review in a sample of oversized PRs.
