---
name: committed-files-earn-their-place
description: Raw production / personal data is never committed to a shared repo, junk/ included — only derived aggregates ship; past that, a file earns a place in the product repository by being useful to OTHER developers (not by having an in-repo caller), and then must document why it exists and when/how to use it; useful-only-to-us-later goes to the personal junk tree; a one-shot self-check of a single delivery stays uncommitted in the task's evidence dir
type: feedback
schema: leaf/v1
created: 2026-07-13
last_verified: 2026-09-01
---

## Difficulty

Helper scripts written to verify or support one task get committed into the product tree by reflex, with no in-repo caller and no usage doc beyond a docstring. Two symmetric failures follow: a genuinely reusable helper is dropped (or buried in an evidence dir) because "nothing calls it", and a one-shot self-check is committed as permanent product clutter a future reader cannot interpret. The wrong keep-criterion is "does something in the repo call it" — a file with no caller can still be the most useful thing another developer finds, and a file with a caller can still be task-local noise. (Trigger: a past ticket committed a job-success-assertion script to an internal project's scripts directory with no in-repo caller and no when/how doc.)

A second trigger exposes a worse failure of the same reflex: the three homes sort candidates on **one axis only** — who finds the file useful — so a file whose *content* is raw production data sorts cleanly into the personal `junk/` tree and gets committed there, legitimately by the rule as written. That is what happened: a measurement task committed its artifacts under a personal `junk/` path, six of them dumps of real end-user chats (users' own first messages, model replies, chat identifiers). A security monitor (SECALERTS-1154361) flagged them, and remediation meant deleting the data from trunk history. Usefulness is the wrong first question for such a file: no answer to it makes committing the data acceptable.

## Guidance

**Content first: raw production / personal data is never committed to a shared repository — `junk/` included.** This test runs *before* the usefulness test and overrides all three homes below; a file that fails it has no home in the repository at all. Raw data means end-user message text, model responses to real user input, chat / user / session identifiers, and anything else from which a real person's content can be reconstructed. Only **derived aggregates** ship — counts, quantiles, scores, lengths, rates. Raw rows stay in the task's uncommitted evidence directory, and the deliverable cites the query that regenerates them instead of carrying them.

**Past that test, the keep-criterion is usefulness to OTHER developers (or to durable future work), not the presence of an in-repo caller.** Classify every non-product file a task produces into exactly one of three homes:

- **Useful to other developers → commit into the product tree AND document it.** Commit it even when nothing calls it yet. Documentation is not optional: state *why it exists, when to reach for it, and how to run it* — a header in the file plus a pointer from the area's README / troubleshooting doc, so it is discoverable by someone who did not write it. An undocumented committed helper is an incomplete deliverable.
- **Useful only to us later, unlikely to help other developers → personal `junk/` tree, not the product tree.** Worth keeping so we do not re-invent it, but it should not add surface area to the product history other developers read.
- **One-shot self-check of a single delivery, no reuse value → do not commit.** Keep it in the task's evidence / scratch directory. It verified this task once; it is not part of the product.

**Author (developer).** Before committing any file a task produced, apply the content test first — a file carrying raw production / personal data is not committed anywhere, whatever its usefulness. Only then name its home by the usefulness test. If committing, ship the documentation in the same change.

**Reviewer (code-reviewer).** A committed file carrying raw production / personal data is **blocking**, not should-fix — ask for the data to be replaced by a derived aggregate or removed from the change entirely. A newly committed helper that is a one-shot self-verification (belongs in evidence) or useful only to the author (belongs in `junk/`) is a **should-fix**; a committed file whose *why / when / how* a reader cannot determine is also a **should-fix** — ask for the doc or the move rather than approving.

Extends [[tests-accompany-code]] (a test is one such accompanying artifact) and the developer rule "one-off experiments stay local, not committed duplicates".

## See also

- `~/.claude-agent/skills/specializations/developer/SKILL.md` § While developing (author side)
- `~/.claude-agent/skills/specializations/code-reviewer/SKILL.md` § What you review (reviewer side)
- [[tests-accompany-code]] — the same accompany-your-commit discipline on the test axis
- [[docs-accompany-architectural-change]] — the documentation-projection invariant at the architecture scale
