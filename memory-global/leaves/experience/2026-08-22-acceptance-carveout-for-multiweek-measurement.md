---
name: 2026-08-22-acceptance-carveout-for-multiweek-measurement
description: An order requirement (R8) demanded a measured, statistically-significant delta over live traffic needing ~11 days / 396 post-fix rows to reach significance — a real-world duration no single delivery session can span. Both the engine's plan-level acceptance gate (every declared requirement id must carry a 'pass' verdict, with no deferred/pending option, before agentctl resolve) and the plan's own done_criterion initially bound this multi-week measurement to one session's own passability, which the effort-divergence trigger caught only after 3 replans. Resolution: split the requirement into the delivered code fix plus a standing, self-reporting async instrument (a detached 6-hourly poller + a tracked GitHub issue that receives the real significance result once enough data exists), then have the order's customer explicitly accept — at the plan-level acceptance-review step, author-matched to [meta.order].customer_id, never assumed by the agent — that shipping the tracked instrument satisfies the requirement for this delivery, with the actual measurement continuing to run and self-report afterward.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev"
refs: [doubt-own-snapshot, effort-divergence-trigger, question-provenance-gate]
created: 2026-08-22
last_verified: 2026-08-22
---

# A calendar-bound acceptance criterion is carved into a standing instrument, not fabricated or left blocking

## Difficulty
A requirement's acceptance criterion rests on calendar time the delivery session cannot itself span, but the acceptance/resolution gate demands a same-session pass/fail verdict on every declared requirement with no deferred state — forcing either a fabricated premature pass or a permanently blocked resolution.

## Order & criterion
At plan-authoring time, recognize a calendar-bound acceptance criterion (multi-day/week significance test, soak test, slow-burn trend) as categorically different from a same-session check; carve it into a standing, self-reporting async instrument (poller + tracked issue) as part of the delivered plan, and let the order's customer decide — at the acceptance-review step, not before — whether shipping the instrumented-but-unmeasured delivery satisfies the requirement.

**Acceptance check:** Verified live: a running detached poller process + its append-only log, a GitHub issue that will receive the async significance result, and an agentctl accept --verdict record for the calendar-bound requirement explicitly authored by the order's customer (author matched against [meta.order].customer_id) rather than assumed by the agent.

## Contexts

### 2026-08-22 — initial
- Where it arose: Any substantive plan whose order (or done_criterion) rests a pass/fail on a duration longer than one delivery session — statistical significance over live traffic, a multi-day soak, a slow-accruing cost/latency trend. Recognize and carve it out AT PLAN-AUTHORING TIME, not after the effort-divergence trigger forces a replan; then route the accept verdict through the actual customer, since only they can judge whether the instrumented delivery satisfies the order for now.
- Working plan: /home/the0/.claude-agent/plans/spawn-outcome-typing.toml

## Cost
9 stages, 12 spawns, $46.41 total, quality 4/5 (user-confirmed)

## Self-critique of the agent system
The carve-out was reached only via the engine's effort-divergence trigger after 3 replans on this plan; the calendar-bound nature of R8 was checkable from data already in hand (spawn-rate-derived sample-size math) at initial plan-authoring time, and should have been carved out then rather than discovered as a costly correction — this is the same generalizable authoring-snapshot gap recorded in doubt-own-snapshot.md's new 'authoring direction' section.
