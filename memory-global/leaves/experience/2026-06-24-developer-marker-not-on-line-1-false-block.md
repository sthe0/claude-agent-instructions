---
name: 2026-06-24-developer-marker-not-on-line-1-false-block
description: Recurring engine friction — a spawned specialist (developer) writes a summary paragraph before its COMPLETED:/PLAN-READY: return marker, but the spawn wrapper only accepts the marker on the first non-empty line, so it prefixes MALFORMED: and agentctl routes the stage to BLOCKED even though the work succeeded. The manager must then independently re-verify, unblock, and record-result. Fix lives in the wrapper (tolerate a marker on any line / scan the tail) and/or the developer SKILL (marker strictly on line 1).
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "давай закроем задачу"
refs: [2026-06-24-gate-exemption-is-category-error-for-result-images, 2026-05-26-agent-system-plan-vs-reality-drift]
created: 2026-06-24
last_verified: 2026-07-11
---

# Spawned developer's return marker after a prose preamble false-BLOCKs the engine

## Difficulty
The spawn-wrapper return-marker contract requires the marker (COMPLETED: / PLAN-READY: / INCOMPLETE: / …) on the FIRST non-empty line of the specialist's final message. A developer subagent's natural output shape is a short result summary FIRST, then the marker — so the wrapper prefixes MALFORMED: and `agentctl dispatch` records the stage as BLOCKED. The work itself passed; the block is a pure false-negative on output FORMATTING. It recurred TWICE in one task (stage 1 verifier + stage 2 migration), each time forcing the manager into a manual recovery loop: independently re-verify the deliverable, `agentctl unblock`, then `record-result --status passed`. The divergence is plan-vs-reality at the dispatch step: expected marker→PASS, actual MALFORMED→BLOCKED, with no defect in the underlying result image.

## Order & criterion
Expected at dispatch: a successful stage returns its marker and the engine records PASSED. Acceptance criterion: the stage's deliverable meets its done_criterion AND the engine advances without a manual unblock.

**Acceptance check:** A spawned developer stage that meets its done_criterion advances the engine to the next stage with no manual unblock/record-result recovery by the manager.

## Contexts

### 2026-06-24 — memory-leaf-rigid-structure stages 1 & 2
- Where it arose: agentctl-driven substantive task; spawn-specialist.py developer spawns for the verifier implementation (stage 1) and the system-knowledge migration (stage 2)
- Working plan: Both times: confirm the deliverable independently (pytest + verify-all + content spot-check), then `agentctl unblock` followed by `record-result --status passed`. This unblocks the engine but is manual toil. Durable fix is a self-improvement change: either (a) the spawn wrapper scans the whole final message (or its tail) for a return marker instead of requiring line 1, or (b) the developer SKILL.md mandates the marker as the strict first line with an example, or both. Route through self-improvement (this turn).


### 2026-06-25 — 2026-06-25 — DEEPAGENT-436 validate_config YT_TOKEN fix (MALFORMED via budget cut)
- Where it arose: agentctl stage 2 spawn:developer (budget small) for a 2-line fix + 1 unit test; spawn returned error_max_budget_usd with no marker → MALFORMED
- Working plan: Same recovery shape, different MALFORMED cause: spawn died on error_max_budget_usd ($1 small budget; cost_usd=1.014 — the developer static prefix alone is ~$1.01 in cache: 2.0M cache_read + 79k cache_create before any work), result also listed a stray Edit permission_denial. The working tree was source of truth: arc status/log/pr showed edits + unit test already committed (3e7e0be0) AND pushed to the ticket branch. So I did NOT re-spawn/retry — read the actual diff, re-ran pytest myself (10 passed) to verify the runtime axis, created the still-missing draft PR (14091442) in-thread (my engine at EXECUTING so the prod-edit gate was open), then record-result passed. Reusable: (1) on ANY MALFORMED/errored developer spawn, treat arc status/log/diff/pr as ground truth and re-verify what landed before assuming failure — partial success is common; finish only residual steps in-thread at the open gate. (2) BUDGET CALIBRATION: --budget small ($1) is insufficient for ANY developer spawn in this project — skill+context static prefix burns ~$1 of cache before the first edit; use --budget medium minimum even for trivial fixes.


### 2026-07-11 — cleanup-worktrees-audit stage-1 sweeper spawn
- Where it arose: agentctl dispatch of a spawn:developer stage building hook-orphan-worktree-sweep.py + its 23 unit tests
- Working plan: Developer completed BOTH artifacts (315-line sweeper + 176-line test file, all 23 tests green) but emitted no COMPLETED: marker line at all, so spawn-specialist.py validate_marker() wrapped the output MALFORMED:(ok=False) and cli.py parked the stage BLOCKED (spawn_count=0, rc=1) — the exact false-block the wrapper comment at spawn-specialist.py:218 warns about. Recovery: healthcheck (claude -p works) -> read the working tree (both files present, untracked) -> run the stage verify_command (23/23 unit + 7/7 integration green) -> agentctl unblock + record-result --status passed --control <attestation>. No respawn, no lost work.
## Common core & variations
**Common:** Spawn returns a non-marker terminal line (MALFORMED) and agentctl would route to BLOCKED, but the underlying work fully or partially succeeded; manager must independently re-verify the artifact (arc status/log/diff/pr, re-run tests) rather than trust the marker, then finish only the residual steps.

**Variations:** Cause of the missing marker differs: 2026-06-24 = prose preamble before the marker (formatting); 2026-06-25 = budget exhaustion / mid-flight error so no marker emitted at all. Recovery is identical (verify artifact → finish residual in-thread → record-result); the budget case adds a calibration fix (raise small-spawn budget to medium) on top of the marker-contract fix.

## Cost
Ticket-driven leaf — per-session token/$ figures live in DEEPAGENT-436, not separately recaptured here. The qualitative cost this leaf records: two manual unblock + independent-re-verify + record-result recovery loops forced on the manager within one task by the same false-negative.

## Self-critique of the agent system
The manager (me) absorbed a recurring engine false-negative as manual recovery toil twice in one task instead of treating the first occurrence as the difficulty signal it was. The wrapper's first-line-marker contract is too strict for the developer's natural output shape; this is agent-system friction that should be fixed at the wrapper or SKILL level, not papered over per-occurrence. Triggers self-improvement the same turn.
