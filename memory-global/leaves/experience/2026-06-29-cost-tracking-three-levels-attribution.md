---
name: 2026-06-29-cost-tracking-three-levels-attribution
description: A declared multi-level metric (per-stage/plan/task execution cost) was only partially and manually captured; auto-capture required keying the cost record by the unit's identity at write time, then folding stage->plan->task at the finalize node, while naming the axis that genuinely cannot be split.
type: reference
created: 2026-06-29
last_verified: 2026-06-29
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [scripts/agentctl/cost.py, scripts/agentctl/state.py, memory-global/leaves/system-knowledge/MEMORY.md, 2026-05-26-agent-system-plan-vs-reality-drift.md]
plan_file: /home/the0/.claude/plans/cost-tracking-three-levels.toml
last_accessed: 2026-06-29
---

# Multi-level cost tracking: stamp the unit's identity at the write source, fold upward at finalize

## Difficulty
agentctl tracked NO execution cost at stage/plan/task level: Outcome/SessionState had no cost field, resolve/verify-final captured none. The only auto-captured source (spawn-cost JSONL) was not keyed by stage_index/plan_path, so it could not be attributed. The experience-leaf ## Cost section existed but was manual and its verifier only checked section presence, not that the TODO placeholder was replaced — so the 'task is tracked' claim silently diverged from reality.

## Order & criterion
Data model (Outcome cost fields + SessionState.CostRollup, backward-compatible defaults) -> stamp identity at source (dispatch passes --stage-index; spawn-specialist writes stage_index+plan_path into each log row) -> attribute at record-result (cost.py folds matching rows onto the spawn stage's Outcome; in-thread stays None) -> aggregate at verify-final/resolve (fold per-stage outcomes into SessionState.cost, surface in Directive + history) -> retire the placeholder (verifier rejects unreplaced TODO in ## Cost). Each stage gated on its own pytest run.

**Acceptance check:** measurable: full pytest suite green (814) + verify-agentctl exit 0 + the live verify-final surfaced a real rollup ($5.07 across the 3 stages dispatched after the --stage-index feature shipped).

## Contexts

### 2026-06-29 — Cost attribution in agentctl
- Where it arose: agentctl coordination engine (scripts/agentctl/) + experience-leaf verifier
- Working plan: 5-stage TOML plan; spawn:developer per stage; medium budget/complexity each

## Cost
planner $3.30 (hit medium cap, artifact complete) + 5 developer spawns $8.99 = ~$12.3; the feature self-measured $5.07 for the 3 attributable stages

## Self-critique of the agent system
Attribution only covers spawned stages; main-session/in-thread tokens are not split per stage (named honestly in the rollup note, points at cost-report.py). Stages 1-2 of this very task were not attributed because --stage-index shipped only in stage 2 — a bootstrapping artifact, not a defect. Also surfaced (not fixed): diff_plans ignores criterion.verify_command, so a verify_command-only TOML change is silently dropped on no_change replan.
