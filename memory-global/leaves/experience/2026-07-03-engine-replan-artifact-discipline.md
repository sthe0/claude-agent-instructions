---
name: 2026-07-03-engine-replan-artifact-discipline
description: Engine-driven task hit three coupled coordinator errors around replan/dispatch: (1) editing the plan file at state.plan_path in place makes replan a self-diff (no_change) that silently drops the correction; (2) a substantive replan resets already-PASSED stages to PENDING, forcing re-verification of delivered work — verify_commands must therefore check durable properties, not time-bound tree states; (3) agentctl dispatch SPAWNS the specialist itself — running spawn-specialist.py on top double-spawns and burns budget.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (AskUserQuestion at the resolution gate: «Да, решена»)"
refs: [agentctl/cli.py, 2026-06-25-claude-md-reduction-floor.md]
created: 2026-07-03
last_verified: 2026-07-03
---

# agentctl replan/dispatch artifact discipline: never edit the plan in place, dispatch already spawns

## Difficulty
Desired: an engine correction cycle (declare→investigate→critique→replan) applies a fixed verify_command and execution resumes once. Actual: three divergences — (a) replan --plan pointing at the SAME path as state.plan_path after an in-place edit diffed the file against itself → no_change → stale command re-ran and failed again; (b) the earlier substantive replan (executor/done_criterion delta) reset stages 1-3 from PASSED to PENDING although their work was committed; (c) dispatch itself spawned a developer while the coordinator, believing dispatch merely records routing, manually spawned a second one for the same stage (~$1.9 burned before the second stood down). Root cause common to all three: the engine keeps no snapshot of the approved plan and treats commands as effectful primitives, while the coordinator modeled them as bookkeeping.

## Order & criterion
When correcting a plan under agentctl: author every revision at a NEW version-suffixed path (plans are compared old=state.plan_path vs new=--plan — same path = self-diff no-op); write stage verify_commands as durable-property checks (mechanism exists + suite green), never observations of the current tree that later stages are designed to falsify; before spawning for a stage, know that dispatch already spawns — spawn-specialist.py is only for manual/out-of-engine spawns; expect a substantive replan to reset PASSED stages and re-record them against those durable checks.

**Acceptance check:** measurable — replan returns kind=refinement and the re-armed stage passes on retry with the corrected command; no duplicate specialist process for the stage (single spawn-costs.jsonl entry per stage).

## Contexts

### 2026-07-03 — initial
- Where it arose: instruction-offload task, session 759b0d4b, agentctl engine v. current at commits 8bb18b6..801ac90; agentctl/cli.py cmd_replan (old = _load(state.plan_path)) and cmd_dispatch
- Working plan: 1. Reproduce: stage 1 re-fail after in-place edit + replan no_change. 2. Read cmd_replan: old loaded from state.plan_path at call time — no stored snapshot. 3. Restore the baseline content at the old path, write the correction to /tmp/instruction-offload-v3.toml, replan → refinement applied, stage re-armed and passed. 4. Re-record stages 2-3 against durable checks. 5. Double-spawn: sibling developer discovered by the manual spawn, which stood down; let the dispatch-spawned one finish and land (801ac90).


### 2026-07-03 — unknown executor token silently falls through to in-thread
- Where it arose: si-mechanize-gates stage 1 dispatch, session 759b0d4b, 2026-07-03
- Working plan: Plan TOML stage had executor = "developer" (bare specialization name); agentctl/plan.py:23 recognizes only "in_thread" and "spawn:<kind>", and an unknown token silently resolves to in-thread — dispatch answered 'stage 1 is in-thread; no spawn' with no warning, contradicting the approved plan's intent. Correction cost a full difficulty cycle (record-result failed -> declare/investigate/critique -> substantive replan v2 at a NEW path -> user re-approval), because changing the executor is a substantive plan change. Rule: author executor tokens as the exact typed values; structural fix queued for backlog — plan.py should REJECT an unknown executor token at load (validation, not fallthrough).

## Common core & variations
**Common:** engine consumes plan fields literally; a value outside the typed vocabulary must fail loudly at load, not degrade silently at dispatch

**Variations:** prior contexts: replan self-diff / stage reset / double-spawn; this one: executor vocabulary fallthrough

## Cost
Wall-clock ~1.5h across the recovery; $4.5 attributed to stage 5 dispatch spawn + ~$1.9 wasted on the duplicate manual spawn; 2 extra difficulty cycles

## Self-critique of the agent system
The double-spawn was the SECOND dispatch-semantics mistake this task (a --dry-run BLOCKED transition earlier) — the coordinator should have read dispatch's behavior after the first surprise instead of re-modeling it from assumption. Engine-side fixes worth filing: snapshot the approved plan at approve-time; make dispatch's spawning explicit in its Directive detail.
