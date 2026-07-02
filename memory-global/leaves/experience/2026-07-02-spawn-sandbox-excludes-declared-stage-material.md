---
name: 2026-07-02-spawn-sandbox-excludes-declared-stage-material
description: A dispatched developer spawn is sandboxed to scripts/, but the plan stage declared docs/operations/cross-session-scope-isolation.md as material and its method required editing it; the spawn burned its $3 budget on 13 permission denials (doc reads via Read AND cat, pytest retries in several phrasings) and died on error_max_budget_usd after writing the code but before the report marker and the doc edit. The plan's own 'manager fallback for out-of-scope actions' condition existed but the spawn was not told which material was out of its reach, so it discovered the wall by burning budget. Rule: when authoring a stage for a sandboxed spawn, partition the material list explicitly — anything outside the sandbox is assigned to the manager in the stage method, and the spawn prompt names it as off-limits.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "Fedor (2026-07-02: clicked 'Да, решена (Recommended)' at the ghost-scope-fix resolution gate)"
refs: [scripts/spawn-specialist.py, ~/.claude/plans/ghost-scope-fix.toml]
created: 2026-07-02
last_verified: 2026-07-02
---

# Spawn budget burned on permission denials when declared stage material lies outside the spawn sandbox

## Difficulty
Desired: a dispatched spawn spends its budget on the stage's work product. Actual: the stage's material list included a file outside the spawn's sandbox, so the spawn spent a large share of its budget probing denied actions (13 denials, multiple phrasings of the same read) and died on budget before completing its report.

## Order & criterion
Execute plan ghost-scope-fix stage 2 via a dispatched developer spawn sandboxed to scripts/

**Acceptance check:** Spawn returns a line-initial marker within budget; no budget spent probing actions the plan already knows are denied

## Contexts

### 2026-07-02 — initial
- Where it arose: ~/claude-agent-instructions; agentctl dispatch -> spawn-specialist.py (budget medium $3, sonnet); plan ~/.claude/plans/ghost-scope-fix.toml stage 2; spawn session 209d493f
- Working plan: Recovery applied: manager salvaged the delivered working-tree code (full diff review), wrote the out-of-sandbox doc section per the plan's manager-fallback condition, ran all verification (147 scope/spawn, full 1149), recorded the stage passed with an honest control note. Prevention: at plan-authoring/dispatch time, diff each stage's material list against the spawn sandbox root; move out-of-sandbox items into an explicit manager step in the stage method and state them as off-limits in the spawn prompt.

## Cost
$3.23 spawn budget (code+tests delivered, marker and doc edit lost) + one manager salvage cycle; net loss vs a clean run ~$0.3 and one diagnosis round
