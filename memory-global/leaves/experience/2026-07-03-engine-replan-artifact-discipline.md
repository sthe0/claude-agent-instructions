---
name: 2026-07-03-engine-replan-artifact-discipline
description: Engine-driven task hit three coupled coordinator errors around replan/dispatch: (1) editing the plan file at state.plan_path in place makes replan a self-diff (no_change) that silently drops the correction; (2) a substantive replan resets already-PASSED stages to PENDING, forcing re-verification of delivered work — verify_commands must therefore check durable properties, not time-bound tree states; (3) agentctl dispatch SPAWNS the specialist itself — running spawn-specialist.py on top double-spawns and burns budget.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (AskUserQuestion at the resolution gate: «Да, решена»)"
refs: [agentctl/cli.py, 2026-06-25-claude-md-reduction-floor.md]
created: 2026-07-03
last_verified: 2026-07-22
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


### 2026-07-03 — config-root-tails sweep (claude-agent-instructions, session 1c913d96, same day, second independent context)
- Where it arose: Substantive 5-stage sweep of legacy config-root prose refs (411 occurrences -> 151 converted, 91 allowlisted, standing enumerator in verify-all); agentctl drove the full spine plan->approve->partition->execute->verify-final->resolve
- Working plan: Independently re-hit all three failure shapes and confirmed the remedies live: (1) post-thinker refinements applied to the plan file IN PLACE were never re-registered - engine ran the frozen pre-refinement verify_command and false-FAILED Stage 3; remedy: copy to a NEW path (v2) with one genuine prose refinement, plan-review --target v2, replan -> kind=refinement copies verify mechanics into state. Wrinkle: the replan coverage gate blocked this no-op re-registration (no means changed) - honest bypass is --coverage-waiver naming the engine snapshot (not the plan artifact) as the divergent thing. (2) Stage 3 count invariant was expressed against the LIVE tree (live grep total == worklist rows) and false-failed at verify-final regression replay after the Stage 4 sweep removed 151 occurrences; remedy: re-anchor to the frozen Stage 3 commit (git grep -onP <pat> 3e48c4f minus generated artifacts == worklist rows) - re-runnable forever; applied as a v3 refinement replan directly from VERIFYING (difficulty commands are rejected at that node). (3) called agentctl dispatch AFTER already spawning via spawn-specialist.py - dispatch is itself a spawner and launched a duplicate developer into the SAME worktree; killed the duplicate process tree within a minute. Rule: dispatch XOR manual spawn, never both.

### 2026-07-04 — legacy pre-snapshot session: no_change replan leaves stale stage materialization
- Where it arose: fix-agentctl-core-defects stage 6, session ce4f6071, 2026-07-04
- Working plan: Symptom: after an in-place verify_command refinement, replan said 'plan unchanged — retry the re-armed stage' and the engine then ran the OLD verify_command from state (failing on out-of-scope issue #16) despite the plan file carrying the reviewed new one. Root cause chain: the session was approved BEFORE the #8 snapshot fix shipped, so state.plan_snapshot_path is empty; the documented fallback diffs plan_path against args.plan — the same file — so ANY in-place edit degrades to kind=no_change, and the no_change branch re-arms the FAILED stage WITHOUT refreshing stage definitions from the plan file. Remedy that works: author the correction as a NEW plan file (v8 = reviewed content; restore v7 on disk to its as-approved content as the diff baseline), re-bind the thinker review to v8, replan --plan v8 -> kind=refinement -> stage definitions refresh with PASSED carry. Guard: for any session whose state predates the snapshot mechanism, never correct a plan in place — always a new file.
### 2026-07-22 — a cosmetic edit inside a classifier-watched structural field flips a typo-refinement into a destructive substantive replan
- Where it arose: behavioral-conformance-control task, session f8a7f77a, agentctl current at origin/main ~19c3fe1; the failing stage's verify_command named a non-existent test module (`test_cli.py`), a genuine typo-only fix
- Working plan: The fix was a single `replace_all` of the wrong module name across the plan TOML. `replace_all` touched 6 sites — one of which was an ILLUSTRATIVE "Green:" example string sitting INSIDE the structural field `meta.done_criterion`. The engine classifies refinement-vs-substantive by BYTE-diff of structural fields, so a purely cosmetic change to a classifier-watched field re-classified the replan as **substantive**, re-armed the PLAN_READY hard gate, and reset already-PASSED stages 4–5 to PENDING — even though intent/scope/means were unchanged. Recovery cost a full re-approval cycle (present-plan essence → user re-approval) plus re-recording the two lost stages (their code was committed and green, so re-record, not re-do). Rule: when fixing a plan file, scope the edit to the field that actually needs it — NEVER let a `replace_all` or cosmetic touch land inside a classifier-watched structural field (`meta.*`, executor, done_criterion, means), or accept that the engine will destroy PASSED state and force re-approval. A substantive replan is a destructive operation on delivered work; treat any structural-field byte-change as substantive-by-construction. Same family as the initial context's "substantive replan resets PASSED stages" — this vector is the ACCIDENTAL trigger of that reset via an unscoped string edit.
- Secondary (landing tail, same session): after both delivery branches were reviewed PASS at their pre-rebase HEAD, rebasing onto a concurrently-advancing origin/main was byte-clean but broke `verify-config-root-refs` — a LINE-pinned allowlist entry (the harness-ref pin at MEMORY.md line 64) drifted to line 65 because this branch's own new MEMORY.md pointer stacked on top of a parallel session's MEMORY.md addition. A clean rebase is not a green rebase: re-run the FULL `verify-all` (not just the targeted suites) after every rebase onto a moved trunk, and expect line-pinned governance to drift under concurrent same-file edits. Land with `land-branch.py --remote-only` when a parallel session has local trunk checked out, so you never disturb its working tree.

## Common core & variations
**Common:** engine consumes plan fields literally; a value outside the typed vocabulary must fail loudly at load, not degrade silently at dispatch — and a byte-change to any classifier-watched structural field is substantive-by-construction, so an unscoped edit there silently destroys PASSED state

**Variations:** prior contexts: replan self-diff / stage reset / double-spawn; executor vocabulary fallthrough; this one: an unscoped `replace_all` touching an illustrative string inside `meta.done_criterion` accidentally triggers the destructive substantive reset, plus a post-rebase line-pin verify drift on the landing tail

## Cost
Wall-clock ~1.5h across the recovery; $4.5 attributed to stage 5 dispatch spawn + ~$1.9 wasted on the duplicate manual spawn; 2 extra difficulty cycles

## Self-critique of the agent system
The double-spawn was the SECOND dispatch-semantics mistake this task (a --dry-run BLOCKED transition earlier) — the coordinator should have read dispatch's behavior after the first surprise instead of re-modeling it from assumption. Engine-side fixes worth filing: snapshot the approved plan at approve-time; make dispatch's spawning explicit in its Directive detail.
