---
name: 2026-06-27-critique-output-must-be-code-verified-into-downstream-artifact
description: Difficulty — the overcome-difficulty critique already splits a divergence into similarities (what to preserve) and differences (what to change), but the engine discarded that split as prose: Critique stored only functional_ground/replanning_task, cmd_replan never read the critique, and diff_plans was blind to means/conditions changes. So the mapping similarity->preserved-condition / difference->changed-means was 100% model cognition with zero code enforcement, and a replan that removed a difference by changing means was silently classified no_change. Fix: structure the split as Critique fields, add a replan coverage gate (each similarity must substring-land in new-plan conditions/invariants; non-empty differences require a changed means/method multiset), and make diff_plans see+apply means/conditions. Key boundary: code VERIFIES coverage, it does not AUTHOR the item->field mapping (that stays cognition) — the realizable form of 'programmatically use the critique' is a verification gate, not codegen.
type: reference
created: 2026-06-27
last_verified: 2026-06-29
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [2026-06-26-critique-primitive-unifies-conflict-and-principle.md, 2026-06-25-state-gate-needs-acting-session-at-executing-via-toml.md]
last_accessed: 2026-06-29
---

# A structured diagnostic split is inert until code verifies its landing in the artifact it should shape

## Difficulty
A diagnostic step produces a structured split (similarity/difference) but the engine keeps only free-text and never feeds it into the downstream artifact (the replanned plan), so the intended mapping is unenforced and a means-only correction is invisible to the plan classifier.

## Order & criterion
Structure the split (Critique fields) -> capture at CLI -> coverage gate in replan -> fix diff_plans blindness -> sync prose. Criterion: pytest 677 green + verify-agentctl OK.

**Acceptance check:** measurable: full agentctl test battery green and verify-agentctl exits 0; coverage gate blocks an uncovered similarity and a means-unchanged difference, passes a correct plan and (back-compat) an empty split.

## Contexts

### 2026-06-27 — Wire critique split into replan (DEEPAGENT machine, claude-agent-instructions Core)
- Where it arose: agentctl engine (state.py Critique, cli.py cmd_critique/cmd_replan, gates.py, plan.py diff_plans) + overcome-difficulty SKILL
- Working plan: Approach A (structure + hard coverage gate) over advisory/prose-only: the user asked for programmatic use, and a verification gate is the form of that compatible with code=control-flow/prose=cognition. One developer spawn for the coupled ~320-line change; manager verified each stage's verify_command and the full battery independently before recording passed.


### 2026-06-27 — Bring all cognition residues to code — hybrid determinism + warn-only advisor
- Where it arose: agentctl engine (gates.py, plan.py, state.py schema 6->9, machine.py, cli.py, new advisor.py) + verify-plan-file.py + agentctl/README.md + doc-bindings.json
- Working plan: 8 stages, one PR-scope, serial (shared cli.py/plan.py/state.py). Each cognition residue reduced to EITHER a deterministic gate (block) OR — when the residue is semantic and has no deterministic oracle — a warn-only advisor that attaches list[str] to directive.data and NEVER flips directive.ok/node. New deterministic blocks: hypothesis distinctness + declaration anti-template (gates); substantive=>TOML-only + mandatory capability_required (plan/cli/verify-plan-file); verify_command mandatory on measurable + acceptance-review requires observation!=expected (plan/state.Criterion/cli); executable [[final_check]] at verify-final (plan/state.FinalCheck/cli._run_check); plan_stack push/pop sub-spine where no-auto-pop is structural via pop's RESOLVED source-node (state.PlanFrame/machine/cli). Manager drove each stage via record-result with the engine re-running verify_command as the gate; verify-final re-ran ALL stage commands defense-in-depth. 766 tests green, 0 skips.

## Common core & variations
**Common:** Same boundary as the base context: code VERIFIES, it does not AUTHOR. The realizable form of 'use cognition X in code' is a gate or a data-attachment, never codegen and never a model veto.

**Variations:** New corollary this context adds: when even VERIFICATION is non-deterministic (semantic judgement — was the weight class right, is the plan complete, is the observation real), the realizable form is a WARN-ONLY oracle (opt-in, fail-open, directive.data only) — it preserves the code=control-flow/prose=cognition canon precisely because it cannot block. Deterministic-where-possible (hard block immediately, no grandfather), advisory-where-not. Also: 8 stages sharing a file cluster ran serially as one PR (mirrors the base leaf's one-spawn-over-per-stage lesson) but here as 8 separate per-stage developer spawns + manager record-result gating — acceptable because each stage was a distinct file-set delta and the engine's verify_command gate caught regressions between them.

## Cost
1 developer spawn (budget large, complexity high); ~7min wall-clock; 677 tests; manager independent re-verify.

## Self-critique of the agent system
The 5 engine stages were one coherent PR to a single module cluster (shared cli.py) — decomposing into 5 dispatch-spawns would have conflicted and cost ~5x; collapsing to one developer spawn + manager-driven per-stage record-result (each verify_command actually run) kept the spine honest at lower cost. Generalizable: when plan stages share a file and the graph linearizes them, prefer one spawn over per-stage spawns.
