---
name: recurring-normalize-factor-is-architecture-signal
description: Two or more normalize cycles naming factors in the same underlying category means the earlier re-norming was cosmetic (an instance patch, not a cause fix) — the replanning task must surface a structural alternative to the user as an explicit option, not another patch.
type: feedback
schema: leaf/v1
created: 2026-09-04
last_verified: 2026-09-04
---

# Recurring normalize-factor is an architecture signal

## Difficulty

`agentctl normalize` records a reproducible factor and blocks `replan` until it exists, but nothing checks whether the newly named factor is *actually new* — a fresh instance of an old, still-unaddressed category slips through the gate exactly as cleanly as a genuinely novel cause. Each cycle looks locally correct (a real factor, honestly named, duly recorded) while the series as a whole is diagnosing the wrong thing: not "this specific defect" but "this coupling keeps producing defects," and the gate has no way to see the series, only the single factor in front of it.

Concrete precedent: a monitoring-alert ticket for infrastructure availability. The plan's test procedures mutated real production state (an on-disk availability-state file, a live infrastructure-events list) and sent messages through the user's real messaging chat during test execution, wrapped in an ever-more-elaborate stash/simulate/restore/trap apparatus. One review round named and fixed a batch of point-defects in that apparatus. The next round found four new ones in the *same* apparatus, and its own closing note said explicitly that none of the four blockers required architectural reconsideration — all four were fixed with point patches. A further round found another defect in the same category: a false notification into the real chat, again via the same live-state-crosses-into-test-execution mechanism. Seventeen rounds in, the coordinator had never surfaced "isolate test state/fixtures from production state and the real chat entirely" as a named, user-facing alternative to "patch the apparatus again" — each round's own "one more round" authorization implicitly foreclosed it by framing continuation as the only live option.

## Guidance

Before naming `--factor` on a `normalize` call, check it against the factors already normalized earlier in the **same task** (the declare/critique/normalize history if the engine drove those cycles, or the conversation if it didn't). The check is a semantic one — same underlying coupling or mechanism, not the identical instance — so it stays a model judgment; do not try to regex-match factor strings. A structural example: two different defects both caused by shared mutable state crossing a test/production boundary count as the same category even though the concrete failure text differs every time.

If the new factor belongs to a category already normalized once before:

- The earlier normalize patched an instance, not the cause. Whatever keeps reproducing it is still there — say so plainly in the critique, don't just record another instance-level factor and move on.
- The replanning task must name the **structural alternative that removes the coupling itself** (e.g., isolated test fixtures replacing stash/restore around live state), not a further guard layered on top of the same apparatus.
- Put that alternative to the user as an **explicit, named option** alongside "patch again" — via `AskUserQuestion`, not folded into a continuation framing that only offers "one more round." This holds even when the user has already authorized further rounds in general terms; a general authorization to continue is not the same as a considered choice between "patch again" and "redesign," and only the latter actually resolves a category-level difficulty.
- Do not skip this because the review-round budget (`effort-replan-absolute`) hasn't fired yet — the round-count gate and the category-recurrence check are independent signals; a category can recur well within 3 rounds, and the count gate alone will not catch it (see [[effort-divergence-trigger]] below).

This stays deliberately unmechanized. The quantitative half — "how many rounds have run" — is already the engine's job via `effort-replan-absolute`. The qualitative half — "is this the same underlying category as before" — is a semantic classification only the model can make; per the root CLAUDE.md rule "separate rule from perception," this is the perception part, and forcing it into a regex or keyword match would just relocate the failure mode this leaf exists to prevent (see [[regex-not-for-semantic-classification]]).

## See also

- [[overcome-difficulty]] § 4 Normalization — the mechanism this leaf extends.
- [[effort-divergence-trigger]] — the quantitative sibling: round-count/spend/wall-clock divergence the engine detects by itself. This leaf covers the case that count alone misses: a small number of rounds, each finding a genuinely distinct-looking defect, that are nonetheless all instances of one uncorrected category.
- [[regex-not-for-semantic-classification]] — why the category check stays a model judgment, not a mechanized match.
