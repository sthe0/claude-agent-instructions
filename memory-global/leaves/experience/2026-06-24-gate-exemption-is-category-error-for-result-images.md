---
name: 2026-06-24-gate-exemption-is-category-error-for-result-images
description: Difficulty — a path is blanket-exempted from a process-gate because it 'isn't task output', but the artifact is in fact the result-image of a process the same gate governs (a plan is the output of planning). The unconditional exemption is then a category error: it lets the artifact be mutated in states where mutating it is exactly the thing the gate exists to catch. Fix: gate it with a phase/node-aware rule (writable only in the producing phase, frozen after), and resist weakening the gate to preserve an existing escape hatch — route the escape through the difficulty-overcoming path instead.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "Решена (Recommended)"
refs: [2026-06-24-prose-to-code-migration-consumer-and-superset, 2026-05-26-agent-system-plan-vs-reality-drift]
---

# A process-gate exemption is suspect when the exempted artifact is itself a gated result-image

## Difficulty
When codifying the rule 'all state-changing tasks flow through the coordination spine', a developer blanket-exempted ~/.claude/plans/ ('plans are authored before EXECUTING, gating them would break the planner'). But a plan IS the result-image of planning — the spine's own output. Exempting it unconditionally means a plan can be silently rewritten mid-execution, which is precisely a plan-vs-reality divergence the engine should treat as a difficulty. A second trap: the strict rule 'deny plan-writes outside the planning phase' appears to conflict with existing escape hatches (replan_refine: EXECUTING->BLOCKED->EXECUTING; CLAUDE.md in-thread plan refinements). The tempting fix is to weaken the gate (allow plan writes in the active stage too).

## Order & criterion
1) When about to exempt a path from a process-gate, ask: is this artifact the result-image of a process this gate governs? If yes, an unconditional exemption is wrong — gate it with a phase/node-aware rule (mutable only in the producing phase, frozen after). 2) When a strict gate rule seems to conflict with an existing escape hatch, do NOT weaken the gate to fit the hatch. Check whether the hatch should itself route through the difficulty-overcoming path. Here: changing a plan mid-execution is a difficulty, overcome reflexively (overcome-difficulty -> replan re-arms at PLAN_READY), so the deny IS the enforcement, and the allow-set is exactly the producing phase {CLASSIFIED,ROUTED,PLANNING,PLAN_READY} (spawned planners auto-start at CLASSIFIED, so the set extends below PLANNING).

**Acceptance check:** measurable — gate denies a plan write at EXECUTING/RESOLVED and allows at CLASSIFIED/ROUTED/PLANNING/PLAN_READY; non-plan production gating unchanged; tests + runtime check green

## Contexts

### 2026-06-24 — node-aware plan gating in agentctl
- Where it arose: agentctl coordination engine: hook-state-gate.py + exempt_paths.py, after Phase 1 made gating uniform across the agent's own config/instructions
- Working plan: exempt_paths.py: drop /.claude/plans/ from _EXEMPT_SUBSTRINGS, add is_plan_file() to identify a plan path. hook-state-gate.py: PLAN_MUTABLE_NODES={CLASSIFIED,ROUTED,PLANNING,PLAN_READY}; gate_decision(weight,node,is_plan) applies the node-aware branch BEFORE the ALLOW_NODES check (EXECUTING is in ALLOW_NODES but a plan write there must deny, with a replan/overcome-difficulty hint). CLAUDE.md line 39 reframed plans as node-aware not flat-exempt. 287 tests green; 7/7 runtime gate outcomes match; verify-all 12/12. Commit 3de14fa.

## Cost
manager in-thread (engine-driven 2-stage plan); no spawn. 1 commit 3de14fa.

## Self-critique of the agent system
I first proposed a weaker design (A-prime: allow plan writes in the active stage too) to avoid breaking replan_refine and in-thread refinements. That was solving the conflict by eroding the gate. The user's principle — 'changing a plan during execution means a difficulty arose; overcoming it means stepping into a reflexive position and the changed plan IS the result of removing the difficulty' — showed the strict deny was correct and the conflict dissolves because the escape hatch (replan) already exists. Lesson: when a new gate rule fights an escape hatch, suspect the hatch should route through the principled path, not that the gate is too strict.
