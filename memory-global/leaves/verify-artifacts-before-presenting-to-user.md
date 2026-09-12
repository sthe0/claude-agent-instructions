---
name: verify-artifacts-before-presenting-to-user
description: Personally re-verify concrete claims in a plan/essence/report BEFORE presenting it to the user for approval — not after, and not on trust in a chain of prior subagent review reports alone. User flagged this as an engine-mechanization proposal, not just a prose reminder, and pointed at a shared verification-code module as the shape.
type: feedback
schema: leaf/v1
created: 2026-09-04
last_verified: 2026-09-04
---

## Difficulty

On the `heredoc-body-spans` plan (GitHub claude-agent-instructions#108, agentctl session `13cc59ff-...`), rounds 4–6 of plan review (spawned `thinker` subagents) each independently verified fixes against the live plan file and quoted concrete evidence (grep output, line numbers, sha256 digests). I trusted that chain of reports and moved straight to `agentctl present-plan --kind essence` + the user-approval `AskUserQuestion` without personally re-running even the cheap confirming checks (`grep -c "22 added"` → 0, `grep -no "39 added"` → the 4 expected lines, reading the corrected `material_refs` text) myself first. The user caught this explicitly: *"проверки полноты плана... до того как сочтешь план... готовым... эти правки ты делал?"* — asking whether I had actually done the completeness check before deciding the plan was ready, not after. I ran the check only once asked, and it happened to confirm the reports were accurate — but the **order** was wrong regardless of the outcome.

## Guidance

Before any gate that presents an artifact to the user as ready (plan essence, a "done" report, a stage's `Expected result image` claim), **personally** re-run the cheapest form of the concrete verification the artifact rests on — a grep, a digest check, a direct read of the changed lines — even when a competent subagent already verified it and even when digests match end-to-end. Digest-matching proves the reviewer read the *same bytes*; it does not prove *I* checked the claim before asking the user to act on it. Trusting a subagent's verification is fine as the primary evidence; skipping your own cheap confirming pass before the user-facing gate is not.

**Why:** the existing rules (`Verify the right axis, report honestly`, `doubt-own-snapshot`) already state the general principle, but stating it in prose did not stop the lapse — I applied it *after* being asked instead of before presenting. The user explicitly reframed this as a request to **mechanize the gate**, not add more prose: *"вообще это предложение о правке движка: делать все проверки про предъявляемые тобой мне артефакты до предъявления"* — `agentctl` itself (likely the `present-plan --kind essence` / `approve` / `confirm-delivery` path in `scripts/agentctl/`) should require some recorded self-verification artifact before accepting a presentation, the way `stage-review` already binds a verdict to the sha256 of an `--observation` text. The user also recalled a related, previously-discussed idea worth folding into the design: **extract the verification code itself into one shared place**, since the same kind of check (confirm a concrete claim against a live artifact's current bytes) is needed at several different gates/times (plan-review rounds, `present-plan`, `approve`/`confirm-delivery`, `stage-review`, task resolution) — a single reusable verification module/helper in `scripts/agentctl/` that each gate calls, rather than each gate re-implementing its own ad hoc check.

This is its own **separate, substantive engine-code task** (routes through `planner` + full plan-approval per the self-improvement skill's own "substantive instruction change" carve-out) — **not** done inline in this leaf, and not bundled into whatever task surfaced the lapse, per the user's standing "каждую задачу отдельным планом" instruction. Pick it up as its own plan when there's room: read `scripts/agentctl/cli.py`'s `present-plan`/`approve`/`confirm-delivery` commands and `state.py`'s presentation-receipt shape first, survey every existing site that already does an ad hoc "check a claim against the live artifact" pass (plan-review, stage-review, resolution `verify-final`) to find the right shared shape, then design what a minimal "coordinator attests: I personally re-checked claim X against file Y at time T" recorded artifact would look like, gated the same way `StageReview`/`plan_review` already are, and backed by that one shared verification helper rather than N duplicated ones.

**How to apply (until the engine change lands):** at any point where you are about to run `agentctl present-plan`, write a "done"/"resolved" report, or otherwise hand the user an artifact whose credibility rests on a specific prior claim (a fix landed, a count is correct, a file was changed) — run the cheapest possible independent check of that specific claim yourself, in the same turn, before the presenting call. If the check was already run by a subagent minutes/rounds ago against the exact same file digest, a fresh `sha256sum` match plus re-running just the reviewer's own headline grep/assert is enough; it does not need to be a full re-review.

## See also

- `~/.claude-agent/memory-global/leaves/doubt-own-snapshot.md`
- `~/.claude-agent/CLAUDE.md` § Cognition the engine does NOT replace — "Verify the right axis, report honestly"
