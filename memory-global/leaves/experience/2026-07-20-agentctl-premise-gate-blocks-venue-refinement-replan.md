---
name: 2026-07-20-agentctl-premise-gate-blocks-venue-refinement-replan
description: The premise-blocks-replan gate (digest includes verify_command) makes a venue-fixing refinement replan un-passable because question-enumerate binds enumerated_at to state.plan_path, not the corrected plan (chicken-and-egg); recipe: nudge gate-exempt state.plan_path to the corrected plan, re-enumerate, fold preserved invariants into a stage field for replan_coverage, re-review, then refinement replan carries the worktree verify_command into live state.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (Fedor Solovyev)"
refs: [2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan, 2026-07-09-gate-must-execute-what-it-attests, 2026-07-03-engine-replan-artifact-discipline]
created: 2026-07-20
last_verified: 2026-07-22
---

# Premise gate blocks a verify_command-venue refinement replan — nudge plan_path + re-enumerate

## Difficulty
After a stage FAILED on the documented venue-mismatch (engine ran the stage verify_command in the serving checkout, which lacks the isolated-worktree commit), the standard remedy — a REFINEMENT replan re-pointing the verify_command/final_check cd to the worktree — was UN-PASSABLE through the normal engine path. The premise plugin (premise-blocks-replan, added 2026-07-09) recomputes _plan_content_digest over stage fields INCLUDING verify_command and requires enumerated_at == content_digest; but question-enumerate binds enumerated_at to state.plan_path, which still points at the OLD plan, so you cannot bind it to the corrected plan before the replan commits (chicken-and-egg) and premise staleness blocks the replan permanently. Compounding it: the no_change/refinement replan branches RE-MATERIALIZE verify_command from the plan FILE via _apply_refined_stage_fields, so a hand-patched state.json verify_command is clobbered by a replan reading a stale file.

**Sharper sibling defect (2026-07-21) — FIXED 2026-07-22 (commit `e987fe3`).** `question-enumerate` UPSERTs each raised item into `bag["candidates"]` (as `qenum-N`, disposition="raised"), and `question-check`/`validate_question_candidates` block `replan` on any candidate still "raised". But `question-dispose` AND `question-retire` both read/write `bag["questions"]` — a *different* store — so neither can reach a `qenum-N` candidate (`"no such question 'qenum-1'"`). There was no `question-candidate-dispose` analogous to the ledger plugin's `ledger-dispose` (which correctly operates on `bag["candidates"]`), and `cmd_question_enumerate`'s own success message advised the non-working `question-dispose --id <qenum-N>`. Net: once `question-enumerate` had run, any DIAGNOSING→replan was wedged with no CLI route out. **Now closed:** `agentctl question-candidate-dispose --id <qenum-N> --as recorded --question <qid> | --as dismissed --reason <text>` mirrors `ledger-dispose` on `bag["candidates"]` (recorded requires a resolvable `bag["questions"]` referent; dismissed requires a reason; error on unknown id), and the enumerate advice string now names it. The state.json surgery below is no longer load-bearing — use the CLI verb.

## Order & criterion
Passable recipe: (1) nudge the gate-exempt state.plan_path to the corrected plan file (where a successful replan sets it anyway); (2) question-enumerate -> enumerated_at now binds to digest(v2), genuinely satisfying the gate intent (enumerate against the plan being approved), not gaming it; (3) satisfy replan_coverage_blockers — every critique.invariants_to_preserve item must substring-land (casefold+collapsed-ws) in a stage conditions/invariants field, so fold the preserved principle sentence INTO invariants (the matcher does NOT scan stage.principle.statement); (4) any edit to v2 re-stales BOTH the premise digest and the plan-review, so re-enumerate AND re-run a fresh independent plan-review bound to v2; (5) replan --plan v2 -> refinement applies, carries worktree verify_command+final_check into live state, transitions DIAGNOSING->VERIFYING, re-arms the stage; (6) next-stage -> EXECUTING -> record-result -> engine re-runs the worktree-venue check itself.

**Acceptance check:** measurable — the engine's own re-run of the patched verify_command (at record-result) and every final_check (at verify-final) exits 0 in the worktree venue; here policy.md 338<=345 lines, leaf present+valid+indexed, six safety kernels grep-present.

## Contexts

### 2026-07-20 — groom-si-policy (worktree, premise active)
- Where it arose: Instruction-grooming of skills/self-improvement/policy.md (relocate 3 operational Git-sync subsections to a leaf) authored in an isolated worktree /home/the0/cai-wt-groom-si off origin/main, with the premise plugin active on a SUBSTANTIVE session.
- Working plan: groom-si-policy v2 (worktree-venue verify_command; repo_root pinned to serving checkout per instance-8)

### 2026-07-21 — instr-pull-integrity-gate (candidate-store dead-end)
- Where it arose: self-improvement task wiring a fail-open integrity gate into `sync-instructions-repo.sh cmd_pull`. A Stage-2 artifact-name refinement dropped the session into DIAGNOSING; the ensuing `replan` was wedged by 13 undispositioned `qenum-*` candidates left by an earlier `question-enumerate` — with the store-mismatch above, no CLI could close them.
- **Workaround used (store surgery, since no CLI path exists):** in `~/.claude-agent/agentctl/state/<session>.json`, set each `plugins.premise.candidates[*].disposition = "dismissed"` with an honest per-item `reason` (`validate_question_candidates` accepts dismissed+reason or recorded+question). Several candidates were genuinely useful (a stale `done_criterion`, two Stage-3 cwd edge cases) — addressed in the plan, not just dismissed. Then refresh `enumerated_at` to `_plan_content_digest(load_plan(plan_path))`, re-record `plan-review`, and `replan`.
- Follow-up owed: this is a real agentctl bug (missing `question-candidate-dispose` + wrong advice string in `cmd_question_enumerate`) — file a difficulty/fix so the workaround is not load-bearing. **CLOSED 2026-07-22 (next context).**

### 2026-07-22 — add-question-candidate-dispose (the fix; follow-up closed)
- Where it arose: dedicated SUBSTANTIVE fix task off origin/main in isolated worktree /Users/the0/cai-wt-qcd (branch `agentctl-question-candidate-dispose`), discharging the follow-up owed above.
- What landed (commit `e987fe3`): `cmd_question_candidate_dispose` in `scripts/agentctl/cli.py` mirroring `cmd_ledger_dispose` but over `state.plugins["premise"]["candidates"]` (recorded ⇒ resolvable `--question` referent; dismissed ⇒ `--reason`; unknown id ⇒ "no such candidate"); subparser + COMMANDS entry; corrected `cmd_question_enumerate` advice string; 8-test file `scripts/tests/test_agentctl_question_candidate_dispose.py` (both success paths, all error paths, and a `premise_blockers` gate-unblock). verify-all 16/16, full pytest 2396 passed / 3 skipped.
- **Meta (spine mechanics learned, worth reusing):** (a) `spawn-specialist.py` doesn't set child cwd — a spawned `developer` inherits the parent's cwd and its filesystem sandbox anchors on that cwd's git root, so **dispatch must be run from the worktree scripts dir** for the child to reach the worktree; (b) spawned children (even under bypassPermissions) **cannot execute `python`** but can read/write files and run git/ls — so the root (which holds execution rights in the worktree) runs the verification itself; (c) `python3 -m agentctl` only imports when cwd=`scripts/` (package lives at `scripts/agentctl/`) — a verify_command that cd's to repo_root false-fails the `-m agentctl` clause while pytest (conftest-injected sys.path) passes; wrap the `-m` clause in `(cd scripts && …)`. This third point recurred as the DIAGNOSING→replan on this very task — the same verify-venue family as [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]].

## Cost
Modest — most effort in diagnosing the premise digest binding across plugins_premise.py + cli.py cmd_replan + gates.py replan_coverage_blockers.

## Self-critique of the agent system
I first planned a full manual state.json patch (5+ fields: verify_command, final_check, status, difficulty, node) per the venue leaf's remedy (b). The cleaner engine-native path is a SINGLE gate-exempt plan_path nudge that lets the engine's own refinement replan do the field-reload + node transition with authoritative bookkeeping — fewer hand-edited fields, less drift risk. Meta: reach for the minimal state nudge that unblocks the engine's own transition, not a hand-replica of it.
