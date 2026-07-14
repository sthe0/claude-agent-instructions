---
name: 2026-07-14-claim-provenance-ledger-reasoning-deliverable
description: A reasoning/research (non-code) deliverable had no engine-level done-axis, so the agent under-investigated and presented fabricated claims as facts. Fix: a claim-provenance ledger in agentctl — typed claims (axiom/derivation/assumption) with a fail-closed CLOSURE check (the mechanized RULE), enumeration of load-bearing claims kept with the model (PERCEPTION), regex rejected as a formal proxy that types strings not claims; deliverable_kind at classify arms the resolution gate.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com"
refs: [formalization-ladder-l1-l3.md, 2026-07-14-determinize-memory-org-principle-in-code.md, 2026-07-09-gate-must-execute-what-it-attests.md]
created: 2026-07-14
last_verified: 2026-07-14
---

# Provenance is the done-axis for a reasoning deliverable: mechanize CLOSURE, keep enumeration as perception

## Difficulty
A reasoning/research deliverable had no equivalent of code's tests-green done-criterion, so 'reads confidently' passed for 'grounded' and fabricated decisions/judgments/proximity-guessed numbers shipped as facts. The done-axis (provenance) was left implicit, so under-investigation was invisible to the engine.

## Order & criterion
planner -> thinker plan-review -> user approval of the 8-stage plan -> Layer A (agentctl ledger subsystem) / Layer B (CLAUDE.md + planner discipline) / Layer C (formalization-ladder leaf) -> verify-final -> land to trunk. Mid-execution stage-7 difficulty (verify_command relative path) routed through the full engine declare->investigate>=2H->critique->normalize->plan-review->replan cycle.

**Acceptance check:** measurable: pytest scripts/tests/ 1920 passed/0 failed (15 hook_turn_end_gate failures confirmed a spawn-sandbox artifact, clean in root shell); verify-agentctl OK; runtime_check_ledger_gate OK; verify-final green; commit 45595de on origin/main (0/0 divergence).

## Contexts

### 2026-07-14 — initial
- Where it arose: claude-agent-instructions Core, main serving checkout; agentctl session 07db333d-f63c-44f2-95f4-6c9d54a674b8; commit 45595de.
- Working plan: claim-provenance-ledger.toml (8 stages): ledger.py/plugins_ledger.py typed claims + fail-closed CLOSURE; deliverable_kind at classify arms the resolution gate; advisor.enumerate_claims an independent semantic cross-check (recall-widener, NOT regex); Layer B references in CLAUDE.md + planner SKILL/policy; Layer C leaf formalization-ladder-l1-l3 (L1-L3 ladder + L3 refusal for empirics + honest residual recall<100%/DECOY/junk-dismiss).

## Cost
engine ~$20.5, 10 spawns, 7 attributed stages; one mid-execution difficulty cycle.

## Self-critique of the agent system
Two execution lessons. (1) A verify_command/final_check path to a file OUTSIDE [meta].repo_root must be ABSOLUTE — the engine runs every check from scripts/, so a relative skills/... path fails at record-time (stage-7 defect; normalized note-level; deeper fix = lint it in verify-plan-file.py, a carried follow-up). (2) When root legitimately completes a spawn:developer stage as self-improvement instruction-prose (a root-owned content class, here gated by the user's AskUserQuestion choice), record via direct record-result --status passed --control <self-review> rather than re-dispatching — re-dispatch would only re-hit the same byte-ceiling CLARIFY on already-done work; action=dispatch is the engine default, not a hard requirement, once the measurable verify_command passes.
