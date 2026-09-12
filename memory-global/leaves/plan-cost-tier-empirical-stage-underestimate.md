---
name: plan-cost-tier-empirical-stage-underestimate
description: A plan's per-stage cost_tier (small/medium/large, driving the wall-clock estimate the effort-divergence trigger compares against) undercounts a stage that runs a real empirical measurement campaign plus its own acceptance-judge/plan-review rounds — default such a stage to "large", not "medium", at authoring time.
type: feedback
schema: leaf/v1
created: 2026-08-25
last_verified: 2026-08-25
---

# Plan cost_tier underestimates an empirical-measurement stage

## Difficulty

To achieve an effort-divergence trigger that fires only on a genuinely wrong
norm (not on every ordinary stage), a plan's `cost_tier` must price what a
stage actually costs in active wall-clock. A stage whose material work is a
real empirical measurement — running a multi-variant campaign against a live
or in-process harness, waiting on results, then clearing its own
acceptance-judge or plan-review rounds — costs far more than a stage that
only edits code and runs a fast unit test, even though both look like one
plan step. Pricing both at `"medium"` (`effort-stage-minutes-medium` = 25
active minutes) understates the empirical stage by an order of magnitude and
manufactures a spurious effort-divergence firing once the real number lands,
forcing a `declare → investigate → critique → replan` cycle to correct an
estimate that a slightly more careful authoring pass would have gotten right
the first time.

## Guidance

At plan-authoring time (`planner`, or the root when refining a plan
in-thread), tier a stage `"large"` — not `"medium"` — when its material work
is any of: a multi-variant or multi-repeat measurement campaign (prompt
variants, model comparisons, A/B-style runs) against a real harness (live
HTTP or in-process replay); a stage whose `criterion_type` is
`acceptance_review` and therefore carries an automatic judge-verdict gate
that can bounce and require a strengthened observation; or a stage expected
to accumulate its own mandatory `plan_review` rounds. `"medium"` stays
correct for an ordinary code-change-plus-test stage, even a substantial one.

Concretely observed this session (`de495-fix-codeact-multiblock`,
`ed1e2dd0-8dec-4a05-a802-710612808849`): a stage testing 6 prompt-wording
variants × 3 repeats × 30 in-flight requests, gated by an acceptance-judge
verdict, was priced `"medium"` (25 min) and actually cost most of a 647-minute
session — triggering the effort-divergence mechanism twice on the
`wall_clock` scale before the tier was corrected to `"large"` for it and for
the two other not-yet-run stages sharing the same risk class (a real
external-API/in-process empirical measurement). A pure code+test stage in
the same plan, correctly priced `"medium"`, passed with no divergence at all
— the gap is specifically the empirical-measurement-plus-gate shape, not
plan size in general.

This is a **forward-looking authoring heuristic**, not a retroactive fix: a
stage that already passed keeps its recorded tier (its actual cost is
already folded into the session's measured actuals, not into a forward
estimate) — only not-yet-executed stages of the same risk class get bumped.

## See also

- [[effort-divergence-trigger]] — the mechanism this heuristic keeps from
  firing on a stage that was never mispriced; § "the estimate is declared by
  the same actor it constrains" names this exact blind spot.
- [[policy-effectiveness-tracking]] — where estimate-vs-actual is supposed to
  be recalibrated across sessions, this leaf's actual feedback loop.
