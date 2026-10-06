---
name: committed-files-earn-their-place
description: A file earns a place in the product repository by being useful to OTHER developers (not by having an in-repo caller), and then must document why it exists and when/how to use it; useful-only-to-us-later goes to the personal junk tree; a one-shot self-check of a single delivery stays uncommitted in the task's evidence dir
type: feedback
schema: leaf/v1
created: 2026-07-13
last_verified: 2026-10-06
---

## Difficulty

Helper scripts, data dumps, run logs, and other task-produced files get committed into the product tree by reflex, with no in-repo caller and no usage doc beyond a docstring. Two symmetric failures follow: a genuinely reusable helper is dropped (or buried in an evidence dir) because "nothing calls it", and a one-shot self-check or pipeline intermediate is committed as permanent product clutter a future reader cannot interpret. The wrong keep-criterion is "does something in the repo call it" — a file with no caller can still be the most useful thing another developer finds, and a file with a caller can still be task-local noise. (Trigger 1: a past ticket committed a job-success-assertion script to an internal project's scripts directory with no in-repo caller and no when/how doc. Trigger 2, 2026-10-06: an internal project's judge-calibration pipeline wrote its cross-stage run state — three raw generation logs totaling ~52 MB, plus basket/pool construction intermediates and per-run judge-score dumps — directly into a VCS-tracked product library `resources/` directory, via a `RESOURCES_DIR`-style constant in the pipeline's own CLI that every run writes through by default; caught only when the user asked whether the directory's files belonged in the shared repo, and named explicitly as a *recurring* pattern, not a one-off — meaning the 2026-07-13 re-norming below did not actually change commit-time behavior.)

## Guidance

**The keep-criterion is usefulness to OTHER developers (or to durable future work), not the presence of an in-repo caller.** Classify every non-product file a task produces into exactly one of three homes:

- **Useful to other developers → commit into the product tree AND document it.** Commit it even when nothing calls it yet. Documentation is not optional: state *why it exists, when to reach for it, and how to run it* — a header in the file plus a pointer from the area's README / troubleshooting doc, so it is discoverable by someone who did not write it. An undocumented committed helper is an incomplete deliverable.
- **Useful only to us later, unlikely to help other developers → personal `junk/` tree, not the product tree.** Worth keeping so we do not re-invent it, but it should not add surface area to the product history other developers read.
- **One-shot self-check of a single delivery, no reuse value → do not commit.** Keep it in the task's evidence / scratch directory. It verified this task once; it is not part of the product.

**Author (developer or planner).** Before committing *any* task-produced file — a helper/script, but equally a data dump, run log, generated fixture, or pipeline intermediate — name its home by this test. This applies even when an existing code convention already pipes a pipeline's output into a committed directory by default (e.g. a `RESOURCES_DIR`-style constant): the convention existing is not evidence the destination is correct — it may itself be the uncorrected instance of this same difficulty. If committing, ship the documentation in the same change.

**Reviewer (code-reviewer).** A newly committed file — script or data — that is a one-shot self-verification/run artifact (belongs in evidence) or useful only to the author (belongs in `junk/`) is a **should-fix**; a committed file whose *why / when / how* a reader cannot determine is also a **should-fix** — ask for the doc or the move rather than approving. Treat an unusually large diff (many MB), a `.log` extension, or a filename carrying a run id/date as a prompt to re-check this test explicitly, not just a slow pageload to scroll past.

**Second occurrence, 2026-10-06 — this leaf alone did not hold.** Trigger 2 above landed after this leaf already existed: both the author-side test and the reviewer should-fix depend on someone applying them at the moment of commit or review, and a long-unreviewed PR removes the reviewer check entirely. Per [[recurring-normalize-factor-is-architecture-signal]], a second occurrence of the same factor means the prior re-norming was cosmetic, not structural — the durable fix moves part of this test to **plan-authoring time**, where it is decidable from the plan alone before any file exists: see `~/.claude-agent/skills/specializations/planner/policy.md` § Output-artifact placement review.

Extends [[tests-accompany-code]] (a test is one such accompanying artifact) and the developer rule "one-off experiments stay local, not committed duplicates".

## See also

- `~/.claude-agent/skills/specializations/developer/SKILL.md` § While developing (author side)
- `~/.claude-agent/skills/specializations/code-reviewer/SKILL.md` § What you review (reviewer side)
- `~/.claude-agent/skills/specializations/planner/policy.md` § Output-artifact placement review (plan-authoring-time check, added after the second occurrence)
- [[tests-accompany-code]] — the same accompany-your-commit discipline on the test axis
- [[docs-accompany-architectural-change]] — the documentation-projection invariant at the architecture scale
- [[recurring-normalize-factor-is-architecture-signal]] — why a second occurrence of the same factor requires a structural fix, not another prose patch
