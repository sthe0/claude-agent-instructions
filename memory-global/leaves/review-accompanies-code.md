---
name: review-accompanies-code
description: For review-gated work, a code-producing stage is not done until its change is an OPEN, self-justified review request — opening the review ≠ merging it, so a deferred trunk-land never licenses a deferred review — AND then driven by the author to MERGEABLE (CI green, reviewer comments answered, approval obtained) without being prompted, monitored via the long-job-monitoring mechanism; the change/review justifies each non-obvious field (rationale in commit/PR description where inline comments are impossible, e.g. JSON); symmetric author(developer)/reviewer
type: feedback
schema: leaf/v1
created: 2026-07-27
last_verified: 2026-07-27
---

## Difficulty

Three distinct failures, one root — a code-producing stage treated as "produce the artifact" instead of "produce a **reviewed, justified, mergeable** artifact":

1. **Review deferred behind landing.** The change was committed and pushed, but no review request was opened, on the reasoning that the trunk-land is a separate later decision. This silently drags the *review* along with the deferred *merge* — but opening a review is not merging it. A pushed-but-unreviewed commit is not a delivered code change; the reviewer never got the chance to catch anything, and the author's push-success read as a false done-signal. (User correction: *"А ревью с патчем где?"* / *"Почему сам сразу не предложил в ревью положить изменения?"*)
2. **Exemplar mirrored by surface, not semantics; non-obvious fields unexplained.** A config record was cloned from a sibling by *form* without verifying each field against the *target's* actual requirements — so a required field was missed (a routing header the target's quota needs) and an unrelated field was blind-copied. And because the format forbids inline comments (JSON), the change carried no rationale for its non-obvious fields, so a reviewer could not tell required-and-understood from copied-by-inertia. (User correction: the missing routing-pool header and the unexplained `max_stop_sequences_length`.)
3. **Review opened, then abandoned.** The review request was opened — and the author stopped watching it. A red CI check, reviewer comments, and the pending approval sat unattended until the user had to point at them. Opening the review is the *start* of the author's ownership, not the end; a review sitting red or unanswered is not progressing toward merge, and the open-review done-signal (failure 1's fix) silently degraded into a new false done-signal one step later. (User correction: *"В ревью тесты упали. Я хочу чтобы ты сам следил за статусом проверок … и старался самостоятельно довести ревью до состояния, когда его можно вливать."*)

Nothing mechanically couples a code change to *being under review*, to *justifying its non-obvious content*, or to *being driven to mergeable*; the discipline lives only in prose, so under load a change lands pushed-but-unreviewed, surface-cloned, or opened-then-abandoned.

## Guidance

**The rule is symmetric, and conditioned on the review gate.**

- **Author (developer / coordinator).** When the work is **review-gated** — a distinct human reviewer will look at it (not a sole-maintained fast-forward repo) — opening the review request (PR) is part of the **code-producing stage's done-criterion**, not a later step folded into landing. *Open ≠ merge:* deferring the trunk-land (a separate resolution-gate decision — see [[landing-discipline]]) does **not** license deferring the review. Push-success is not a done-signal; an **open** review request is. The default is to open the review yourself as the stage completes — do not wait for the user to ask *"where is the review?"*.
- **Author, on driving the review to mergeable.** Opening the review is the *start* of the author's ownership, not the end. Until the change is **mergeable** — CI green, every reviewer comment answered, required approval obtained — the author drives it there **unprompted**: watch the review's checks, new comments, and approval; fix what CI or reviewers flag; re-check. Do not wait for the user to say *"the tests failed in your review"*. Reuse the zero-token mechanism in [[long-job-monitoring]] § Generalization — a detached poller emitting `CHECK_FAILED` / `NEW_COMMENTS` / `MERGED`, closure gated on the merged (or mergeable-and-approval-obtained) marker: *"submitted" ≠ "mergeable"*. Only reading a review comment and fixing the code stays in the model; the monitoring cadence and marker-emission are the poller's job, not the user's.
- **Author, on content.** When cloning an exemplar record/config, justify **each field against the target's own requirements** (endpoint, upstream provider, quota/pool model, protocol) — not against surface similarity to the sibling. Pick the *right* exemplar (one whose requirements match the target's), not the nearest-looking one. State the rationale for each **non-obvious** field (why present, why this value) in the review — and where the format forbids inline comments (JSON, some manifests), that rationale goes in the **commit message / PR description**, which is then the review's explanation of record.
- **Reviewer (code-reviewer).** A review-gated code change with **no open review request** is not reviewable and not done. A cloned config record whose non-obvious fields carry **no stated rationale** is a **should-fix** — ask *why is this field here / why this value*, and check that each field is justified against the target's requirements, not merely present in the sibling. An **open review left with red CI or unanswered comments** is likewise not done — the author owns driving it to mergeable. Neither side waives silently.

**Named escape class** (changes that legitimately land without opening a separate review request):

- A sole-maintained repo with **no distinct human reviewer**, where policy lands direct via fast-forward (per [[landing-discipline]] — PR-vs-ff is set by *reviewer presence*, not surface type).
- Memory writes and gate-exempt scratch (not production).
- A trivial / mechanical change the coordinator is authorized to do in-thread under the small-change class — still stated as such, not assumed.

## See also

- [[landing-discipline]] — the **terminal** state (trunk/main at the resolution gate); this leaf is the **earlier** axis (an open review request at the code-producing stage). Open-review (here) and merged-to-trunk (there) are two distinct done-criteria on one change.
- [[long-job-monitoring]] — the **mechanism** for driving an open review to mergeable (§ Generalization: detached poller + `CHECK_FAILED`/`NEW_COMMENTS`/`MERGED` markers, closure gated on merged). This leaf carries the *policy* (author owns the drive); that leaf carries the *how* (zero-token monitoring without offloading the cadence to the user).
- [[tests-accompany-code]] — the symmetric author/reviewer template on the *test* axis; this leaf is its twin on the *review* axis.
- `~/.claude-agent/skills/specializations/developer/SKILL.md` § Self-review before COMPLETED (author side)
- `~/.claude-agent/skills/specializations/code-reviewer/SKILL.md` § What you review (reviewer side)
- [[mirror-working-caller-before-bypass]] — the runtime-context analogue of "mirror the *right* exemplar by understanding, not surface".
