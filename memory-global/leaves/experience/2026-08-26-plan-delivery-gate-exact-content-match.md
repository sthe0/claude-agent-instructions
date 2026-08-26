---
name: 2026-08-26-plan-delivery-gate-exact-content-match
description: Composing the chat message that delivers a plan essence by retyping/reformatting it (e.g. wrapping a file path in markdown backticks) instead of copying the registered rendering-file bytes verbatim broke hook-plan-delivery-gate.py's exact/normalized content match on the first attempt, denying the plan-approval AskUserQuestion.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "Принять — всё выполнено (Recommended)"
created: 2026-08-26
last_verified: 2026-08-26
---

# Plan-delivery gate needs byte-exact essence text -- markdown decoration breaks it

## Difficulty
present-plan --rendering-file registers rendering_text verbatim; hook-plan-delivery-gate.py (PreToolUse on AskUserQuestion, node PLAN_READY) later checks that this exact text (or a whitespace/casefold/invisible-Cf-character-normalized version of it) appears as a completed turn's final message. Deliberately composing the delivery message by hand -- in this case adding markdown backticks around a plan path that were not in the registered file -- inserts a genuine visible-character difference, which the gate's tolerance class explicitly does NOT cover (only whitespace/casefold/Cf drift is tolerated). Root cause was only found by reading the full 617-line hook source; the fix was to re-Read the scratchpad rendering file and reproduce it byte-for-byte as the turn's final message, with zero markdown added.

## Order & criterion
When present-plan has registered a rendering file, deliver it by reading that exact file and reproducing its bytes as the turn's final text -- never retype, reformat, or add markdown emphasis around any part of it (including file paths). If the delivery is denied, read hook-plan-delivery-gate.py's current source rather than guessing, since its tolerance class (whitespace/casefold/Cf-only) is narrow and explicit.

**Acceptance check:** measurable: the AskUserQuestion for plan approval is allowed by hook-plan-delivery-gate.py on the first attempt after emitting the rendering-file's bytes verbatim as the turn's final message (no PreToolUse deny for '...has not landed as a completed turn's final message...').

## Contexts

### 2026-08-26 — initial
- Where it arose: agentctl coordination spine, PLAN_READY node, present-plan -> deliver -> approve gate cycle for any substantive plan or replan submission using hook-plan-delivery-gate.py.
- Working plan: /home/the0/.claude-agent/plans/spawn-effort-required.toml

## Cost
Session 321ce6af-6ce4-4aa9-921c-7559328245db, per `agentctl resolve`: total_cost_usd=8.4607648, total_duration_ms=2506508 (~41.8 min), spawn_count=3, attributed_stages=1 (spawn-stage costs only; main-session/in-thread tokens not split per stage — see scripts/cost-report.py for the whole-session estimate). 2 stages, 1 replan, quality=5 (user-confirmed).

## Self-critique of the agent system
I composed the delivery message from memory/summary instead of copying the registered rendering file's bytes directly, which is exactly the kind of small transcription drift the gate exists to catch; going forward, Read the rendering file immediately before emitting it as the final message and paste its content unmodified.
