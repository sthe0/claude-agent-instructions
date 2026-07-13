---
name: committed-files-earn-their-place
description: A file earns a place in the product repository by being useful to OTHER developers (not by having an in-repo caller), and then must document why it exists and when/how to use it; useful-only-to-us-later goes to the personal junk tree; a one-shot self-check of a single delivery stays uncommitted in the task's evidence dir
type: feedback
schema: leaf/v1
created: 2026-07-13
last_verified: 2026-07-13
---

## Difficulty

Helper scripts written to verify or support one task get committed into the product tree by reflex, with no in-repo caller and no usage doc beyond a docstring. Two symmetric failures follow: a genuinely reusable helper is dropped (or buried in an evidence dir) because "nothing calls it", and a one-shot self-check is committed as permanent product clutter a future reader cannot interpret. The wrong keep-criterion is "does something in the repo call it" — a file with no caller can still be the most useful thing another developer finds, and a file with a caller can still be task-local noise. (Trigger: DEEPAGENT-449 committed `assert_nirvana_run_succeeded.py` to `robot/deepagent/scripts/` with no in-repo caller and no when/how doc.)

## Guidance

**The keep-criterion is usefulness to OTHER developers (or to durable future work), not the presence of an in-repo caller.** Classify every non-product file a task produces into exactly one of three homes:

- **Useful to other developers → commit into the product tree AND document it.** Commit it even when nothing calls it yet. Documentation is not optional: state *why it exists, when to reach for it, and how to run it* — a header in the file plus a pointer from the area's README / troubleshooting doc, so it is discoverable by someone who did not write it. An undocumented committed helper is an incomplete deliverable.
- **Useful only to us later, unlikely to help other developers → personal `junk/` tree, not the product tree.** Worth keeping so we do not re-invent it, but it should not add surface area to the product history other developers read.
- **One-shot self-check of a single delivery, no reuse value → do not commit.** Keep it in the task's evidence / scratch directory. It verified this task once; it is not part of the product.

**Author (developer).** Before committing a helper/script, name its home by this test. If committing, ship the documentation in the same change.

**Reviewer (code-reviewer).** A newly committed helper that is a one-shot self-verification (belongs in evidence) or useful only to the author (belongs in `junk/`) is a **should-fix**; a committed file whose *why / when / how* a reader cannot determine is also a **should-fix** — ask for the doc or the move rather than approving.

Extends [[tests-accompany-code]] (a test is one such accompanying artifact) and the developer rule "one-off experiments stay local, not committed duplicates".

## See also

- `~/.claude-agent/skills/specializations/developer/SKILL.md` § While developing (author side)
- `~/.claude-agent/skills/specializations/code-reviewer/SKILL.md` § What you review (reviewer side)
- [[tests-accompany-code]] — the same accompany-your-commit discipline on the test axis
- [[docs-accompany-architectural-change]] — the documentation-projection invariant at the architecture scale
