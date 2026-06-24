---
name: 2026-06-24-developer-marker-not-on-line-1-false-block
description: Recurring engine friction — a spawned specialist (developer) writes a summary paragraph before its COMPLETED:/PLAN-READY: return marker, but the spawn wrapper only accepts the marker on the first non-empty line, so it prefixes MALFORMED: and agentctl routes the stage to BLOCKED even though the work succeeded. The manager must then independently re-verify, unblock, and record-result. Fix lives in the wrapper (tolerate a marker on any line / scan the tail) and/or the developer SKILL (marker strictly on line 1).
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "давай закроем задачу"
refs: [2026-06-24-gate-exemption-is-category-error-for-result-images, 2026-05-26-agent-system-plan-vs-reality-drift]
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

## Cost
TODO — fill via cost-report.py / tool-usage-report.py

## Self-critique of the agent system
The manager (me) absorbed a recurring engine false-negative as manual recovery toil twice in one task instead of treating the first occurrence as the difficulty signal it was. The wrapper's first-line-marker contract is too strict for the developer's natural output shape; this is agent-system friction that should be fixed at the wrapper or SKILL level, not papered over per-occurrence. Triggers self-improvement the same turn.
