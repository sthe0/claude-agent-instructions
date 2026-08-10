---
name: a-judging-artefact-is-executed-before-it-judges
description: An artefact that will JUDGE other work — a plan's controls, a CI job, a lint rule, a review checklist — is executed read-only against the real environment before the work it judges is reviewed or approved. Reading it is not verifying it; a control defect found downstream costs a whole replan cycle.
type: reference
schema: principle/v1
generality: 2
induced_from: [2026-07-09-gate-must-execute-what-it-attests, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan, 2026-07-20-stage-verify-command-narrower-than-final-check]
created: 2026-08-11
last_verified: 2026-08-11
---

# A judging artefact is executed before it judges

## Principle

To keep a control's defects from being paid for by the work it judges, **execute the judging
artefact read-only against the real environment before that work is reviewed or approved.** Reading
a control is not verifying it. A `verify_command`, a `final_check`, a CI job definition, a lint
rule, a generated review checklist — each is *code that will issue verdicts*, and shipping it
unexecuted is shipping untested code into the position of judge.

The operational form: run every candidate control read-only **before** the plan is submitted, and
**before** spawning the reviewer — the two most expensive gates on the spine sit between authoring
and first execution, so a control defect discovered after them is repaid at their price.

**Why this is not caught today.** Every gate on the `submit_plan` path — structure, coverage,
premise provenance, order coverage, the thinker review — reads a `verify_command` as **text**; none
runs it. So the earliest a control defect can surface is `record-result`, downstream of both
expensive gates, where the cheapest repair is a whole replan cycle. On the 2026-08 advisor-timeout
plan that mechanism alone produced **four** consecutive replans, six plan versions and nine thinker
reviews, without the deliverable ever being at fault.

The structural home is an `agentctl` read-only pre-flight sibling to `check-coverage`: given a
candidate plan, execute each stage's control in the venue the engine will use and report
exit-status and stderr, without recording a stage result. Until that exists, materialize the
controls to a scratch directory and run them by hand before `submit_plan` — a stopgap, and one that
should be named as such rather than normalized.

## Generality

Level 2 — a class of tasks: authoring any artefact whose purpose is to render a verdict on other
work. It ranges over plan controls, CI/pipeline definitions, verifier and lint scripts, hook
guards, and generated test harnesses. It is not claimed at level 3: the statement is about
*judging* artefacts specifically, where the asymmetry is that the judge's own defects are charged to
the judged. An ordinary deliverable is of course also tested, but there the cost of a defect falls
on the deliverable itself, which is a different economics.

## Induced from

- [[2026-07-09-gate-must-execute-what-it-attests]] — the same fault one level in: a gate that
  attests to a property it never executed the check for.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the vector catalogue of
  controls that are wrong in venue, scope, interface, suite or lifetime. Every vector in it is a
  defect that one read-only execution before `submit_plan` would have surfaced.
- [[2026-07-20-stage-verify-command-narrower-than-final-check]] — a control whose text looked right
  and whose executed scope was narrower than the final check it was supposed to anticipate.

## Refutation

The principle is refuted, or driven to a broader form, by a control whose read-only execution is
**not** cheaper than the cycle it protects — a check that is inherently destructive, that cannot run
before the work it judges exists (a genuine post-condition over an artefact not yet produced), or
whose single run costs more than a replan. The first two are the real class: a post-condition
control has nothing to run against at authoring time, and forcing one produces a *different* fault
(a pre-action probe wired as a post-condition, which is structurally unsatisfiable). So the honest
widened form is: execute what can be executed now, and for the rest verify the **shape** — venue,
scope, interface — against a dry-run harness, declaring which conjuncts stayed unexecuted.

## See also

- [[verdict-covers-the-evidence-domain-it-claims]] — the sibling on the evidence axis: a verdict
  over evidence never read. This principle is upstream of it — the judge itself was never run.
- [[shared-resource-health-asserted-relative-to-a-pinned-base]] — a specific control defect class
  that a pre-flight execution would surface immediately (an absolute assertion goes red on a base
  that was already red).
- [[renorming-at-principle-level-sweeps-the-class]] — how a control defect found this way should be
  discharged once it is diagnosed as a form.
- `scripts/agentctl/README.md` — `check-coverage`, the existing read-only pre-flight this one would
  sit beside.
