---
name: 2026-08-27-self-attested-identity-field-needs-declared-principal-check
description: cmd_plan_review --verdict override accepted any free-text --reviewer with no binding to who actually decided, letting the coordinator self-record the user's own deadlock-escape decision; the same gap existed silently on the WRITE side too (cmd_replan editing an approved plan with no proof a human saw the diff, and pop-subplan re-deriving venue from a plan file that could have been edited in the same silent way) until the user explicitly widened the order to cover editability, not just executability.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [memory-global/leaves/question-provenance-gate.md, memory-global/leaves/acting-without-asking.md, memory-global/leaves/ask-user-question-split-turn.md]
plan_file: /home/the0/.claude-agent/plans/plan-review-override-customer-id-fix-v2.toml
created: 2026-08-27
last_verified: 2026-08-27
---

# A self-recorded identity/verdict field (--reviewer, --author) is worthless without a declared-principal check, on BOTH the read gate and every sibling write gate

## Difficulty
A gate that checks WHETHER an action is authorized (plan-review override, an acceptance review, any --by/--reviewer/--author field) is trivially defeated if the value is free text nobody validates against a declared principal (order.customer_id or equivalent) — the coordinator can type any name, including the user's own designation, and the gate passes. The same organizational gap recurs on the WRITE side of the same object: whatever makes an approved plan's EXECUTION require a genuine user act (delivery-gated approval) has a sibling that makes the plan's EDIT require the same, and a third that makes an artifact DERIVED from the plan file (a sub-plan's restored venue) inherit the same protection — fixing only the first leaves the other two open as an equivalent bypass.

## Order & criterion
User: fix cmd_plan_review's override path so it cannot be self-attested under an arbitrary --reviewer string; the user then explicitly broadened scope mid-task: «Я хочу чтобы план не только нельзя было "молча исполнять после правки", но чтобы его и "молча поправить" после одобрения и до возникновения затруднения тоже нельзя было» — i.e. close the read-side AND the write-side of the same silent-edit hole, and audit for further siblings (pop-subplan's venue re-derivation surfaced during the plan's own thinker review, not from the original problematization).

**Acceptance check:** cmd_plan_review --verdict override refuses when --reviewer != order.customer_id (R1-R3, tested); cmd_replan outside DIAGNOSING refuses a refinement/no_change diff without a delivered+certified replan_diff receipt bound to the exact candidate bytes (R4-R7, tested); pop-subplan trusts a PlanFrame's captured venue over a stale/edited parent file read (R9, tested, found during plan review not at authoring time); full agentctl suite green (5320 passed), verify-agentctl OK, and a genuine AcceptanceReview recorded by the user (not self-recorded by the coordinator under the 'user' customer_id) before verify-final's resolution gate opened.

## Contexts

### 2026-08-27 — initial
- Where it arose: Any coordination engine (agentctl-shaped or not) that lets a free-text field stand in for 'a human decided this' — review verdicts, approval bypasses, acceptance sign-off, escape hatches with a --by/--reviewer/--note shape. Check the SAME question at every sibling gate touching the same protected object: read (execute), write (edit), and derived-state (an artifact rebuilt from the object, which can silently re-absorb an edit the object-level gate never sees).
- Working plan: an approved plan (or equivalent authorized-state object) must remain both un-silently-executable AND un-silently-editable after approval, with narrow, explicitly-reasoned exemptions (a legitimate difficulty-repair cycle) rather than an open self-attestation escape

## Cost
total_cost_usd=21.90, wall_clock~85min, spawn_count=9 (developer x mostly, code-reviewer, thinker), 8 plan stages + this resolution gate

## Self-critique of the agent system
Near-miss avoided, not made: at this same session's own resolution gate, verify-final demanded an AcceptanceReview bound to order.customer_id='user' — recording it under my own authorship would have been the exact same self-attestation bug this task fixes, one layer up. Caught it structurally (customer_id check made it a hard refusal, not just a judgment call) and ran a genuine AskUserQuestion instead of typing 'agentctl accept --author user' myself. Worth recording BECAUSE this pattern is easy to miss when the acting party and the described principal share a name ('user') and there is no live human turn immediately preceding the command — the temptation is to treat 'the user asked for this whole task' as standing consent for every downstream identity-bearing record, which is exactly the conflation the fix under R1-R3 exists to prevent.
