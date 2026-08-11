---
name: shared-resource-health-asserted-relative-to-a-pinned-base
description: A control that asserts the health of a resource this delivery shares with others (trunk's position, trunk's test suite, any co-owned tree) must state that assertion RELATIVE to a measurement of the same resource pinned at a named base — subset-of-base-failures, not zero-failures; passed >= passed-at-base, not passed >= a constant.
type: reference
schema: principle/v1
generality: 2
domain: development
induced_from: [2026-06-30-shared-tree-suite-failure-wrong-ownership-attribution, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]
created: 2026-08-11
last_verified: 2026-08-11
---

# A shared resource's health is asserted relative to a pinned base

## Principle

To make a control answer the question the delivery actually owns — *did this change break
anything?* — **assert the health of any SHARED resource relatively, against a measurement of that
same resource taken at a named, pinned base**, never absolutely:

| Instead of | Write |
|---|---|
| the suite is green (0 failures) | the tip's failure set is a **subset** of the base's failure set |
| `passed >= <constant>` | `passed >= passed_at_base` |
| trunk is at `<sha>` | the merge-base with trunk is still the pinned base sha |

A resource is *shared* when someone other than this delivery can change it between the moment the
control was authored and the moment it runs: trunk's position, trunk's test suite, a co-owned
worktree, a machine's installed toolchain. An absolute assertion over such a resource is a claim
about **everyone's** state dressed as a claim about **yours**, and it fails in both directions —
red when a foreign regression lands (the delivery is blamed for work it never touched, and the
honest response is to weaken the control, which is how a gate becomes a formality), and, less
visibly, green when a foreign *improvement* masks a real local loss.

The base must be **pinned as a sha and measured once**, with the measurement committed as an
artifact (here: `{"base": <sha>, "passed": N, "failures": [...]}`). Pinning the sha without
recording the measurement leaves the comparison to be re-derived at run time against a moving
trunk, which is the absolute form again with extra steps.

## Generality

Level 2 — a class of tasks: authoring any automated control (a plan stage's `verify_command`, a
`final_check`, a CI job, a pre-commit gate, a release check) whose success predicate reads state
this delivery co-owns. It ranges over test suites, trunk position, lockfile/dependency state, and
shared fixtures. It is not claimed at level 3: the statement is specifically about *controls over
co-owned state*, and the sole-ownership case is genuinely exempt — a control over an artifact this
delivery alone produces should assert absolutely, because there the absolute value **is** the local
property.

## Induced from

- [[2026-06-30-shared-tree-suite-failure-wrong-ownership-attribution]] — the ownership fault in its
  first form: a suite red in a shared tree attributed to the session that happened to run it.
- [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] — the vector catalogue for
  mis-scoped engine-run controls; "whole-suite gate false-failing on pre-existing unrelated reds" is
  this principle's difficulty stated as one of that leaf's venue/scope vectors.

Third occurrence inside one task (2026-08, the advisor-timeout delivery): stage 1's merge-base
control, the stage-8 control caught in review, and stage 9's suite conjunct all carried the absolute
form — which is the `principle-promotion-threshold` recurrence that lifted it here.

## Refutation

The principle is refuted, or driven to a broader form, by a shared resource whose base measurement
is **not reproducible enough to compare against** — a suite with genuinely non-deterministic
membership, or a base whose failure set differs run-to-run by more than the delivery's own effect.
There the subset relation stops discriminating, and the requirement must widen from "compare to a
pinned base" to "compare to a distribution over the base", with a stated tolerance — at which point
the honest control is statistical, not a subset test.

A narrower, already-known limb the current form does **not** cover, and which would refute the
comfortable reading that a subset test is sufficient: a delivery-caused red whose node is *already*
in the base failure set is invisible to the subset relation, as is a passing→skipped transition.
The subset test bounds new reds only; closing those two needs a per-node diff and a `skipped <=
base` conjunct.

## See also

- [[result-checked-against-its-result-image]] — the general form: the control exists to compare a
  result to its declared image. This principle constrains what the *image* may legitimately be.
- [[verdict-covers-the-evidence-domain-it-claims]] — the sibling fault on the evidence axis: a
  verdict issued over evidence never actually read. Here the evidence is read but attributed to the
  wrong owner.
- [[a-judging-artefact-is-executed-before-it-judges]] — the same control, on the axis of whether it
  was ever run before being trusted.
