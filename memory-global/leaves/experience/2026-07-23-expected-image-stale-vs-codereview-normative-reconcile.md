---
name: 2026-07-23-expected-image-stale-vs-codereview-normative-reconcile
description: When a code-review decision changes the delivered material (here: removed the --dry-run exemption from a dispatch-gate hook), the plan's expected-image can still assert the OLD behavior and the measurable verification then FALSELY FAILS a correct, merged artifact. Route the fix as normative (the verification NORM went stale, not the code) and reconcile the expected-image to the delivered bytes — never mask it as a pass. Encode it as invariants-to-preserve ONLY, NOT a --difference-to-remove: the engine's replan_coverage_blockers ties any declared difference to a means/method change, which honestly must NOT happen in a pure normative reconciliation (the delivered activity is correct and unchanged), so a difference-to-remove hard-blocks replan with 'no stage means/method changed'.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev"
refs: [arc-land-dispatch-determinize.toml, merge-commit 11ba9400, r20430905]
created: 2026-07-23
last_verified: 2026-07-23
---

# Stale expected-image vs a code-review decision: reconcile as an invariants-only normative replan

## Difficulty
Expected: stage-2 live verification passes — every synthetic-payload decision matches the plan's expected image. Actual: 15/16 matched, but 'arc pr create --dry-run' was DENIED by the delivered+merged hook while the plan expected ALLOW. Mismatch: the plan's verification norm (expected-image) predicted pre-code-review behavior; the code-review had removed the --dry-run exemption (arc pr create has no --dry-run flag, so a bare one is a publish-less create → correctly denied). A stale norm that would have FAILED a correct artifact.

## Order & criterion
declare (Expected/Actual/Mismatch) → investigate with 2 hypotheses (H1 code false-deny FALSIFIED via 'arc pr create --help' showing no --dry-run flag + is_exempt() has no dry-run branch; H2 expected-image stale CONFIRMED) → critique routing failure-address normative → normalize → replan (refinement re-classified substantive by the engine because the control criterion changed → re-approval).

**Acceptance check:** measurable — verify_command (merge-commit pin + both hermetic suites) green on a clean trunk checkout; plus a 16/16 synthetic-payload exercise of the composed hook (7 DENY / 9 ALLOW after reconciliation).

## Contexts

### 2026-07-23 — initial
- Where it arose: agentctl-driven self-improvement task (arc-land-dispatch-determinize), post-merge verification stage; the delivered code was already trunk-merged and independently code-reviewed.
- Working plan: 1) Reconcile the expected-image at EVERY site (5 here: context, stage-1 done_criterion, stage-1 method DENY-branch, stage-1 test-case description, stage-2 ALLOW→DENY list + counts) — a universally-quantified norm change needs all instances, not just the one that broke. 2) failure-address normative. 3) In the critique, declare the delivered behavior as --invariant-to-preserve and declare NO --difference-to-remove (a normative reconciliation re-selects the CONTROL CRITERION, not means/method; the coverage gate blocks a difference-to-remove that no means/method change backs). 4) Re-spawn thinker bound to the NEW plan bytes (any edit re-stales the review). 5) A substantive replan resets PASSED stages to PENDING — re-record them against the durable merged artifact (record-result honors a spawn:developer stage without re-dispatch; do NOT re-spawn → duplicate PR).

## Cost
1 developer spawn (delivery, prior session, $4.53 list-price telemetry); this session in-thread engine driving. Flat Max — cost is telemetry, not money.

## Self-critique of the agent system
The stale expected-image should have been caught when the code-review removed the exemption — the load-bearing-axis sweep (change a decision → sweep all its representations) applies to the PLAN's verification norm too, not only to product code. Honest routing (full DIAGNOSING sub-spine) was correct; the multi-round premise/plan-review/coverage-gate friction is inherent to editing an approved plan, not a defect.
