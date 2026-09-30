---
name: plan-cost-tier-empirical-stage-underestimate
description: A plan's per-stage cost_tier (small/medium/large, driving the wall-clock estimate the effort-divergence trigger compares against) undercounts a stage whose material bundles several independently-costly units of work — an empirical measurement campaign, or a multi-file engine-rewrite spanning several independently-verifiable slices plus a from-scratch cross-cutting test suite. The empirical case is fixed by bumping the tier to "large"; the multi-slice engine-rewrite case is NOT fixed by any tier bump — it must be decomposed into separate stages at authoring time.
type: feedback
schema: leaf/v1
created: 2026-08-25
last_verified: 2026-09-30
---

# Plan cost_tier underestimates a multi-slice stage

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

## Second risk class: the multi-slice engine-rewrite stage — tier bump does not fix it

A different failure mode shares the same root cause (a stage's material
bundles several independently-costly units of work priced as one) but does
**not** respond to the empirical-stage remedy above. A stage whose
`Procedure:` enumerates several genuinely independent engineering slices —
each touching a different file/subsystem, each separately verifiable — plus
a from-scratch cross-cutting test suite, is not "one large stage"; it is
several stages wearing one `cost_tier` label. Bumping the label to `"large"`
buys a bigger budget window, but the work itself does not compress: a
one-shot dispatch still has to do the slices in some order, and whichever
slice is scoped/researched first consumes the window regardless of the
label above it.

Concretely observed (`review-prompt-fit`, session `core-wholeplan-review-fix`,
stage 2, "Engine: topo unit records … scoped discharge … read-only walk …
plan-review-compose"): the stage's own `Procedure:` already enumerated 9
steps across two files (`cli.py`: scope parsing, RECORDING, OVERRIDE,
PRIOR-PASS BRANCHING; `gates.py`: SCOPED DISCHARGE, WALK, COMPOSE; plus two
new subcommands, an `improvement-scan.py` detector, and a from-scratch test
file covering 22 named scenarios), tiered `"large"`. One full large-tier
dispatch (2373047 ms, $11.25 — already above the tier label) delivered only
step 1 of 9 (the STATE/data-model slice), correctly and completely, then
returned `INCOMPLETE:` with the developer's own explicit assessment that the
tier was underestimated for the scope. The tier was already the ceiling of
the escalation ladder in this leaf's first risk class — there was nowhere
higher to bump it.

**Guidance:** at authoring time, count the `Procedure:` steps and the files
they touch. When a stage's material spans **≥2 files/subsystems with ≥3
independently-verifiable slices**, or pairs such a rewrite with a
from-scratch test suite covering a double-digit number of named scenarios,
do not price it by tier at all — **decompose it into separate stages** along
the natural slice boundaries (one file/subsystem group per stage, tests
alongside the code that satisfies them), each independently dispatchable
and independently verifiable, with `depends_on` chaining them in sequence.
The tier ladder (small/medium/large) is calibrated for *one* coherent unit
of work; it has no rung for "several units bolted together," and treating a
bundle as if a bigger label could cover it just defers the discovery of the
mismatch to a live dispatch's `INCOMPLETE:` return instead of catching it on
paper.

## See also

- [[effort-divergence-trigger]] — the mechanism this heuristic keeps from
  firing on a stage that was never mispriced; § "the estimate is declared by
  the same actor it constrains" names this exact blind spot.
- [[policy-effectiveness-tracking]] — where estimate-vs-actual is supposed to
  be recalibrated across sessions, this leaf's actual feedback loop.
