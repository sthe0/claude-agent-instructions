---
name: effort-divergence-trigger
description: The engine enters the difficulty cycle by itself when actual effort runs past the current plan's re-derived estimate by the configured multiple — four scales, a window opening at plan approval, and a terminal that diagnoses rather than asking.
type: reference
schema: leaf/v1
created: 2026-08-05
last_verified: 2026-08-20
---

# The effort-divergence trigger

The one-clause rule lives in CLAUDE.md § When the work is stuck; the engine-side mechanics are in `scripts/agentctl/README.md` § Effort divergence. This leaf carries what a coordinator needs when the trigger fires at them.

## Difficulty

A task can run many times over its own plan and **nothing in the system notices**. The only detector is the user's attention — the resource the whole coordination machinery exists to spend less of. So the failure is self-concealing in exactly the wrong direction: the more the work overruns, the more supervision it silently demands, and the overrun that most needs a re-plan is the one nobody is counting.

A plan is a **norm**. Running far past it is not merely expensive; it is evidence that the norm is missing something essential about the real situation. That is a [[function-place-difficulty]] — a symptom whose cause is a broken functional place, not a number to push back into range by working harder.

## Guidance

**Four scales, one question.** `spend` and `wall_clock` are ratios: accumulated actual over the estimate the current plan derives from its own stages (plus the reviews the engine itself mandates — one thinker review per plan version, one code review per `spawn:developer` stage). `replans` is an absolute count, because a plan's *intended* replan count is structurally zero. `interactions` ships accounting-only: a plan's intended interaction count is not derivable at all, since driving the same plan more autonomously changes it wildly, so any threshold now would be a guess dressed as a derivation. Values live in `config.md`, each row carrying its own basis; never quote them from memory.

**The window opens at plan approval, not at session start.** Every actual is compared as `actual − baseline`, where the baseline is snapshotted once, at `approve`. That is the only window the estimate describes, since every stage it prices executes after approval. Two consequences worth holding: a session that never passes `approve` — every small change — is inert by construction, not by a weight-class test; and **an overrun spent while planning is invisible to the mechanism**. This feature's own planning phase ran three full review rounds and would not have fired on itself.

**Re-derivation at every replan is the point, not an implementation detail.** The comparison is always against the plan being executed *now*. A plan that contracts under a difficulty lowers its estimate while the actual stands — which is precisely the case where the trigger should be sharpest, and would be blunted by comparing against the original plan forever.

**The terminal is diagnose, never a question.** At the fire site the engine transitions to `DIAGNOSING`, seeds the difficulty and hands you a pre-framed declaration carrying the numbers. It asks nothing, because a confirmation there would reinstate the very supervision the trigger removes. Honestly: the *downstream* consequence can still reach the user, since a substantive replan out of that cycle meets the ordinary approval gate like any other. What is removed is the need for the user to **notice**; their authority over a changed plan is untouched.

**When it fires at you.** Do not treat it as an engine fault and do not work around it. Run the ordinary `declare → investigate → critique → replan`, and let the declaration's numbers be the evidence — the question to answer is *what about the real situation did this plan not know*, not *how do I get back under the number*. Re-arming is deliberate: the baseline rebases onto the actual at each firing, and a further fire additionally requires a `replan` since the last one, so the trigger is silent until a new norm exists to violate.

**The honest weakness: the estimate is declared by the same actor it constrains.** Nothing structural prevents a comfortable estimate. The only mitigation is that estimate and actual both land on the quality-ledger row, so both the thresholds and the estimating habit can be recalibrated against what they actually caught — see [[policy-effectiveness-tracking]] for the Flags-fire → self-improvement → record-movement procedure that is supposed to consume them. Secondary blind spots: the spend scale reads only what the cost ledger attributes to this plan's path, so a task whose overrun is entirely in the main thread under-reads there and is covered only by wall-clock; and the hook-stamped accumulators are best-effort. A third blind spot is structural rather than best-effort: a review round is **not one of its four scales**, so a cycle whose cost concentrates in review passes rather than replans runs unmeasured — measured while this mechanism was being extended, fifteen thinker verdicts across five replans with the trigger never firing on review cost — and the quality-ledger row the closing sequence writes carries the same four ratios and no fifth, so those rounds leave nothing to recalibrate against either. What partially covers the gap is a separate mechanism, the round-release valve on `plan_review_rounds`, which routes a review loop to the user at `effort-replan-absolute` rounds; the counter it reads was long advanced only on the pre-approval path, leaving the valve unreachable from `replan` — exactly where post-approval review cycles recur — until the counter was extended to advance there too. And on the scales that are counted, `record_fire` rebases the baseline onto the current actuals, so one firing consumes that scale's window: the next fire needs a fresh overrun measured from the new baseline, not the continuation of the old one.

## See also

- [[function-place-difficulty]] — why an overrun is a symptom of a broken functional place rather than a number to force back into range.
- [[policy-effectiveness-tracking]] — the quality-ledger loop the fires and both effort vectors feed, and the only calibration path for a self-set estimate.
- [[recording-experience]] — the resolution-time record where a firing's lesson is supposed to land.
