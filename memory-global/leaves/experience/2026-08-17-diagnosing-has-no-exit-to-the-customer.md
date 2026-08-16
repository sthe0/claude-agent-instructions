---
name: 2026-08-17-diagnosing-has-no-exit-to-the-customer
description: A 12-day, 428 USD, 57-spawn, 11-replan task ended fully ACCEPTED on all eleven order requirements and rated 2 of 5: product right, process wrong. The effort-divergence trigger fired three times and each firing was answered by another replan, because DIAGNOSING's only exit edge is replan — the engine can re-author the plan but cannot hand the ORDER back to the customer, so 'is this plan right?' is the only question it can ask and 'is finishing this order worth what it is now costing?' is the one it cannot. On a SELF-SCOPED order (the agent's own critique set the scope) that missing exit is the whole difficulty: nothing compared the repair's growing cost against the critique's value, and the customer's process verdict arrived only at the very end, as a rating, in a ledger no planner reads.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (AskUserQuestion at the resolution gate, 2026-08-17: accepted all 11 requirements, rated 2/5)"
refs: [review-loop-cannot-measure-its-own-convergence, no-circuit-breaker-on-verification-effort, effort-divergence-trigger, scope-substitution-at-plan-authoring, coordinator-objective]
plan_file: /Users/the0/.claude-agent/plans/smd-act-defects-8.toml
created: 2026-08-17
last_verified: 2026-08-17
---

# The effort-divergence cycle has no exit that renegotiates the order with its customer

## Difficulty
An engine that detects effort divergence but can only answer it with another plan will keep re-planning a scope nobody re-authorised. agentctl routes a >=5x overrun (or 3 replans) into DIAGNOSING pre-framed and asking nothing, which is right — but DIAGNOSING closes through declare -> investigate -> critique -> normalize -> replan, and replan is its ONLY exit. Every one of the three firings on this task was on the replans-absolute scale, i.e. the engine observed 'you have replanned three times' and the only act it could offer was a fourth. The order was self-scoped (an open critique the agent itself resolved into 8 ranked defects), so the customer had never priced repairing all eight; by the time the price was visible the engine had no verb for showing it to them. A second half of the same gap: acceptance and quality are two different judgments collected in one moment — the user accepted the PRODUCT on all 11 requirements and rated the PROCESS 2/5 — and only the acceptance is durable and machine-read; the rating lands in claude-task-quality.jsonl, which no planner consults when estimating the next plan.

## Order & criterion
Read the activity-theory repository and the MMPK literature, critique the implementation and how adequately the SMD distinctions are reflected in the code and artifacts.

**Acceptance check:** acceptance-review: the user accepts on review, per-requirement, since no objective check decides whether a categorical distinction is adequately reflected

## Contexts

### 2026-08-17 — initial
- Where it arose: agentctl-driven SUBSTANTIVE task smd-act-defects-8: 13 stages, 8 categorical defects repaired, landed as 77 commits at e8d7eb9
- Working plan: /Users/the0/.claude-agent/plans/smd-act-defects-8.toml

## Cost
428.24 USD over 57 spawns (developer 36 / 354.21, code-reviewer 15 / 56.58, thinker 6 / 17.45), plus unattributed main-thread tokens; 12 days wall-clock (2026-08-04 to 2026-08-16); 11 replans; 164 user prompts. Orientation was 150-250 USD, so roughly 2x the top of it.

## Self-critique of the agent system
The trigger fired three times and I answered each with a replan without once asking whether the ORDER still warranted its cost — which is exactly what the trigger was telling me. The user had to say it twice in plain words before I stopped ('19 rounds, isn't that a lot', 'we have been two weeks on this'), and even then I kept the scope. Filing nine issues took one command once the token existed; that obligation had sat 'outstanding on the user' for days because I treated an unreachable channel as the end of the matter rather than as something to ask for.
