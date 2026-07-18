---
name: 2026-07-18-plan-presentation-two-act-delivery-gate
description: Approval could fire without the user ever seeing the plan because 'show the plan' was agent perception, not a verifiable mechanism. Fix: split approval into two machine-enforced acts — (1) present-plan --kind essence registers a receipt and hook-plan-delivery-gate.py stamps it ONLY when the registered rendering actually landed as a completed turn's FINAL assistant text (pre-tool-call text never renders, so the final message is the only observable), (2) the approval AskUserQuestion must carry a [show-full-plan] option; plan_presentation_blockers denies cmd_approve until the stamp exists. Two non-obvious gotchas cost real debug time: the harness STRIPS the trailing newline from a delivered text message, so the registered rendering file must be rstrip('\n')ed or the delivered-vs-registered sha mismatches and the gate denies; and the ask must open in a CLEAN turn (no preceding tool call) or the turn-split gate drops it. The gate proved itself at runtime by gating this very session's own approval.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev"
refs: [2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan, 2026-07-03-engine-replan-artifact-discipline, 2026-07-09-gate-must-execute-what-it-attests, 2026-07-04-topological-gate-when-signal-unobservable]
created: 2026-07-18
last_verified: 2026-07-18
---

# Plan presentation as a machine-verified two-act delivery gate (not agent perception)

## Difficulty
A user could click 'approve' on a plan whose essence they never saw: 'presenting the plan' was left to agent judgement (perception), with no mechanism forcing the essence to be shown before the confirmation ask, and no separation between presenting and confirming.

## Order & criterion
classify -> submit-plan -> present-plan --kind essence (emit the registered rendering as the turn's FINAL message) -> [next turn] approval AskUserQuestion carrying [show-full-plan] -> hook stamps delivery -> approve (blocked until stamped). Presentation and confirmation are two SEPARATE acts by construction.

**Acceptance check:** measurable: 52 delivery-gate tests + 5 negative end-state checks green (same-turn ask denied; never-delivered ask denied+unstamped; delivered ask allowed+stamped; degraded-missing-transcript fail-open unstamped); AND the mechanism gated this session's own approval (runtime dogfood).

## Contexts

### 2026-07-18 — initial
- Where it arose: agentctl coordination engine (present-plan/plan_presentation_blockers in cli.py+gates.py, delivery.py sidecar, hook-plan-delivery-gate.py); CLAUDE.md § Approved plan; tech-writer charter widened Russian-only -> dialogue language.
- Working plan: 5 stages: (1) buy CLAUDE.md byte headroom by extracting elaboration to a leaf; (2) PlanPresentation receipt + delivery sidecar + present-plan cmd + plan_presentation_blockers wired into cmd_approve; (3) hook-plan-delivery-gate.py denies the approval ask until the registered essence landed as a completed turn's FINAL message and stamps at that moment; (4) widen tech-writer charter to dialogue language; (5) re-norm prose (Approved-plan definition, ask-user-question-split-turn overclaim, planner/coordinator procedure).

## Cost
Engine-attributed spawn cost ~$21 list-price (3 spawns; flat-Max telemetry, not real money — see [[flat-max-billing-cost-framing]]); ~69 min wall-clock; 5 stages, 2 failed stage-results, 3 replans, 3 difficulty records. The rework mass sat almost entirely in the verify-final false-fail cycle (Stage-1 execution-time-target trap + whole-repo final_check red on out-of-deliverable issues) and the delivery-gate self-debug (trailing-newline sha mismatch, turn-split denial), not in the mechanism design.

## Self-critique of the agent system
The verify-final false-fails I hit (Stage-1 verify_command re-running an execution-time byte-headroom sub-target that a LATER stage was DESIGNED to consume; whole-repo final_check red on 2 out-of-deliverable issues) are a 9th context of the already-richly-recorded verify-scoping family [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] / [[2026-07-03-engine-replan-artifact-discipline]] ('verify_commands must check DURABLE properties, not time-bound tree states'). Root planner defect: verify_commands/final_checks were authored from memory without dry-running against the post-all-stages baseline. The novel, code-invisible bits worth reading are the trailing-newline delivery-match gotcha and the two-act design rationale.
