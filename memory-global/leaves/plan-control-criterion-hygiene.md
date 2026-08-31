---
name: plan-control-criterion-hygiene
description: Four plan-authoring norms for a stage's control criterion — declare the venue a check observes instead of hard-coding a `cd` into the verify_command; never let a criterion assert an unverified fact about current behaviour; take a criterion's number from an explicitly bounded invocation; and never let a procedure step rewrite the criterion it is measured by.
type: feedback
schema: leaf/v1
created: 2026-08-31
last_verified: 2026-09-01
---

# Plan control-criterion hygiene

## Difficulty

To achieve a stage check that can actually go red for the right reason and
green for the right reason, the criterion has to survive the interval between
plan authoring and stage execution — and the executor has to be able to run it
without repairing it. Four distinct authoring habits break that, and all four
were observed live: three of them inside the very plan whose fourth stage
records this leaf.

The engine makes the cost concrete. `record-result` *runs* the stage's
`verify_command` itself and its exit code overrides the caller's `--status`, so
a defective check writes a fully-delivered stage FAILED and routes the session
into `DIAGNOSING`. And a plan may not be edited at `EXECUTING`
(`hook-state-gate.py`: "a plan is the result-image of active planning"), so the
executor cannot fix the criterion from where the failure is discovered. The
repair costs a full `declare → investigate → critique → normalize → replan`
cycle plus a fresh thinker review bound to the moved plan digest — and if the
review-round budget is already spent, a user override on top. That price is
paid for an authoring slip, every time.

The fourth norm is the one that makes the other three enforceable rather than
merely advisable: without it, an executor who notices a broken criterion
"fixes" it in flight, and the plan silently stops being the thing the work was
measured against.

## Guidance

### 1. Declare the venue a check observes; never hard-code a `cd`

A stage check that must observe the **delivery worktree** declares
`verify_venue = "delivery"`. One that must observe the **landed trunk**
declares `verify_venue_at_final = "repo_root"`, or is written as a typed
`kind = "landed"` check. Never prefix the `verify_command` with
`cd <absolute worktree path>`.

A hard-coded path is an address, not a declaration: the engine cannot know what
the check meant to observe, so it cannot re-point it when the venue moves or
adapt when `verify-final` re-runs every stage check after landing — and
`land-branch.py`'s cleanup removes the worktree the path names, at which point
`cli.py`'s `cd <repo_root> && <cmd>` short-circuits to a false FAILED.

**Provenance, and the false defect this norm replaces.** This was originally
filed as a Core *ordering* defect between `land` and `verify-final`. It is not
one. Re-verification against trunk showed `verify_venue_at_final` landed in
`ef1a786` (2026-07-29) and the typed `kind = "landed"` check in `fced7d0`
(2026-07-28) — both **before** the 2026-08-28 plan that hit the collision. The
engine already had both mechanisms; that plan simply did not use them and
hard-coded `cd <worktree>` instead. Recording the honest provenance is what
stops the same non-existent defect being re-derived from the same symptom.

### 2. A criterion never asserts an unverified fact about current behaviour

An `expected_result_image` describes the world the stage must **produce**. Every
clause it contains about a world that already exists is a claim the plan is
asserting on its own authority — and if that claim is wrong, a correct
execution cannot pass, because passing would ratify a falsehood.

This bites hardest on a **live quantity**. A count transcribed into a plan
starts decaying the moment it is written. State what must be cited and from
which live source; let the figure be measured at execution time and recorded in
the observation.

> **Observed, in this leaf's own plan.** Stage 3's criterion fixed "seven
> observed firings in session `3b0247b5`, six acknowledged". By execution the
> live record held **eleven**, ten acknowledged — because that `effort_fires`
> list is appended to by the very mechanism the stage was filing a report
> about. The executor verified against the live record and filed a body citing
> all eleven, *exceeding* the criterion; the acceptance judge blocked the pass
> anyway, correctly, and the repair cost the full difficulty cycle.

The **corollary on refs**: a fact the stage must *read* belongs in
`knowledge_refs`; an artifact the stage *produces or mutates* belongs in
`material_refs`. Putting a read-only fact in the material list turns background
knowledge into a deliverable the criterion then demands.

### 3. A criterion's number comes from an explicitly bounded invocation

Any counting command inside a criterion passes its bound explicitly — for
`gh issue list`, `--limit N`. A count read from a default-bounded listing is a
truncation, not a measurement, and must never enter a criterion.

Prefer a **content-keyed absolute** over a delta against a captured baseline: it
reads 0 when nothing was done, 2 when the stage ran twice, and 1 only for the
intended world; it survives a re-run without depending on a procedure step
remembering not to overwrite a scratch artifact, and it cannot fail open on a
missing one. Where a delta is genuinely required, capture the baseline within
the stage itself rather than trusting an artifact written elsewhere.

> **Observed.** A backlog count of "30" was an artifact of `gh issue list`'s
> default `--limit 30`. The true figure was **129**. Nothing in the command's
> output announces the truncation.

### 4. A procedure step never rewrites the criterion it is measured by

A procedure step re-measures and reports. It does not touch the
`expected_result_image` or `done_criterion` it is being judged against — even
when it can see that the criterion is wrong, and even when the correction would
be in the plan's favour.

The right move on a criterion discovered to be false is to **surface it as a
difficulty** and re-baseline through the gated path: `declare` → `investigate`
→ `critique` → `normalize` → `replan`, with the plan edit made at `DIAGNOSING`,
which is the one execution-side node where authoring a plan is legitimate, and
a fresh review bound to the moved digest. This is more expensive than an
in-flight edit, and that is the point: the extra cost buys an independent read
of the change, which is exactly what an executor editing its own success
condition does not have.

## See also

- [[experience/2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]]
  — the recurring venue/scope/interface class this leaf's norm 1 generalizes,
  including the venue-**lifetime** variant where landing and the stage checks
  carry mutually exclusive requirements.
- [[plan-cost-tier-empirical-stage-underestimate]] — the sibling authoring norm
  on the *effort* axis; distinct functional ground (pricing a stage) from this
  leaf's (stating a stage's control).
- [[plan-activity-ontology]] — the eight elements a plan must cover; norms 1–3
  sharpen the *control criterion* element, norm 4 protects it from its own
  executor.
