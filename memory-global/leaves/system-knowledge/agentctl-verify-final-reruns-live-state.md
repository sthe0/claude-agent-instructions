---
name: agentctl-verify-final-reruns-live-state
description: agentctl verify-final re-executes every stage's own verify_command against CURRENT live state, not a historical snapshot from when that stage originally ran — a stage-level exact-count/fact assertion goes permanently stale once later, legitimate, out-of-plan activity changes that fact.
type: reference
schema: leaf/v1
created: 2026-09-22
last_verified: 2026-09-22
---

## Difficulty

A plan's stage passed cleanly when it originally executed, asserting some exact
fact about the world at that moment (e.g. "exactly 12 comments exist on this
ticket"). Later, further legitimate activity outside that stage's own scope
(here: the user explicitly requesting one more, genuine final comment) makes
that fact literally change (12 → 13). `agentctl verify-final` then starts
failing FOREVER, with no way to make it pass short of editing the plan —
because `cmd_verify_final` (see `cli.py:4870`) re-invokes each stage's
`verify_command` fresh, against whatever the live system looks like *right
now*, not against a recorded historical result. This is not obvious from the
plan-authoring surface (TOML, `plan-render`) and cost a full
declare→investigate→critique→replan cycle to discover (a project ticket-
comment-cleanup task, 2026-09-22): a first
"just bump the expected count" fix and a second "just revert to the old
count" fix were both wrong, and the actual re-run-against-live-state
behavior was found only by directly `exec()`-ing the extracted verify_command
body against the live ticket.

## Guidance

- **Author stage `verify_command`s so they assert only facts strictly within
  that stage's own causal control** — facts the stage alone produces, that
  will stay true indefinitely once true (e.g. "these N specific ids are gone"
  is stable forever; "the ticket has exactly N comments in total" is not,
  the moment anything outside the plan can add one).
- **Any invariant about the plan's true FINAL state — including an exact
  total count — belongs solely in `final_check`**, which is meant to reflect
  reality at resolution time, not in an intermediate stage's `verify_command`.
- If a stage's own postcondition legitimately includes a count at the moment
  it ran, say so in prose (`expected_result_image` / `done_criterion`) but do
  not encode it as a machine-checked assertion that `verify-final` will
  re-run later against a world that has moved on.
- When `verify-final` fails on what looks like a scope-growth mismatch (the
  live system now has *more* of something than a stage once asserted), that
  is `--failure-address нормативное` (the plan's own verification method was
  inadequate) — not "the plan/goal changed" and not a resource problem.

## See also

- [agentctl stage-vector staleness](agentctl-stage-vector-staleness.md) — a
  different plan/engine staleness trap (stage-count edits after `PLAN_READY`),
  same family of "the engine's cached/re-derived view of a plan can silently
  diverge from what you'd expect."
