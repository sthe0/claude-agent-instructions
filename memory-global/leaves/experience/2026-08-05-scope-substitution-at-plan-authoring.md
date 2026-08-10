---
name: 2026-08-05-scope-substitution-at-plan-authoring
description: A two-part order was answered by a plan covering only the second part, and nothing anywhere compared plan to order — CLAUDE.md's contraction rule catches only edits to an ALREADY-APPROVED plan and the replan-coverage gate compares a new plan to the critique — so all 19 verifiers, the thinker review and the approval itself were honestly green over a scope the user never asked for; fixed by making order elements records the plan_approval gate reads (covered-by-a-stage or cut-with-a-reason, empty bag itself a blocker — the opposite rule from the question bag) and by putting the coverage block, plan size included, inside the essence the user approves, re-derived from live state at gate time so a post-presentation cut cannot hide behind the plan_sha256 binding; the enumeration of the order stays unmechanized on purpose and is the largest residual.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
tier: 1
refs: [memory-global/leaves/question-provenance-gate.md, memory-global/leaves/principles/verdict-covers-the-evidence-domain-it-claims.md]
plan_file: /Users/the0/.claude-agent/plans/scope-coverage-at-approval.toml
created: 2026-08-05
last_verified: 2026-08-05
---

# A plan narrower than its order: every gate green over a domain nobody ordered

## Difficulty
The order was double ("the unclosed thing AND the deferred thing"); the plan covered only the second half, and the narrowing was presented as the plan entire. Every gate downstream then verified the PLAN — verify-all 19/19, thinker plan-review, the approval gate, the resolution gate — so the green was honest about a domain nobody had ordered. The comparison that would have caught it existed nowhere: CLAUDE.md § Substantive plan changes catches contraction only as an edit to an ALREADY-APPROVED plan, and gates.replan_coverage_blockers lives on replan and compares the new plan to the CRITIQUE, never to the original order. There was no primary plan-vs-order comparison at all. This is the same shape the task itself then wrote into the engine: a verdict over a domain the mechanism never looked at.

## Order & criterion
Close the hole at the place it opens — authoring — in three parts: (A) extend the premise plugin's seam so every element of the order is raised and, before approve, is either covered by a stage or explicitly cut with a reason, with approve refusing on an undispositioned element and the cut list visible in the presented essence; (B) one sentence in CLAUDE.md § Escalation extending the ask-options-span-the-set rule to the plan-against-the-order relation; (C) plan size visible in the presented essence BEFORE approval. Explicitly out of scope, named in advance under rule B itself: the root cause of the prior approve refusal, automating the escape-hatch inventory, and plan-review --verdict override.

**Acceptance check:** verify-final green on four checks in the delivery worktree: the two order-coverage test modules (46 tests, both gate directions, mutation-proved), verify-all.py 19/19, the full suite minus one pre-existing macOS path-length red, and a presence grep for both the pre-existing ask-options sentence and the new plan-against-the-order clause in CLAUDE.md.

## Contexts

### 2026-08-05 — Order elements as gate records, and the coverage block inside the approved essence
- Where it arose: agentctl engine, premise plugin (scripts/agentctl/premise.py, plugins_premise.py, cli.py) + CLAUDE.md § Escalation + memory-global/leaves/question-provenance-gate.md; branch scope-coverage-at-approval, worktree ~/claude-agent-instructions-scope
- Working plan: Three stages. (1) THE RECORD: OrderElement + VALID_ORDER_DISPOSITIONS + pure validate_order_elements + pure render_coverage_block in premise.py, the order_elements bag field, the three order-* verbs, and the fourth blocker family folded into plan_approval (commit 89bf200, independently code-reviewed pass). (2) THE VISIBILITY: present-plan --kind essence fast-fails when the rendering lacks the engine-generated block, stamping nothing and handing the block back verbatim; premise_blockers re-derives the block from LIVE state at gate time and requires the stored essence receipt to contain the CURRENT one (commit 441fed4, code-reviewed pass, both halves mutation-proved). (3) THE PROSE: the CLAUDE.md norm sentence, the agentctl README (three verbs, three->four blocker families, the essence-containment binding) and the leaf's order half plus four honest-ceiling residuals (commit d5165e9, in_thread).

- Mechanical traps paid for here: **`gen_crutch_registry.py` scans only git-TRACKED files**, so regenerating the registry *before* `git add` of a new test module gives a green standalone `verify-semantic-gates` and a RED pre-commit hook — correct order is `git add` the new files, then regenerate, then `git add` the registry. `verify-config-root-refs.py` reports `relocated … (content anchor still matches)` after a doc edit shifts pinned lines; `--repin` is the bookkeeping fix and refuses to re-anchor a genuinely stale entry. `spawn-specialist.py --budget` takes `small|medium|large`, never a dollar figure, and `--plan` takes a bare path — the `@<path>` convention is `--constraints`-only. Reading `plan-render` output with the Read tool costs enormous context (one-line JSON carrying the whole plan): extract `data.markdown` programmatically instead.

## Cost
$4.55 attributed to spawns over 1 dispatched stage (652 s), plus an unmeasured main-session share across two context windows; 3 commits, 1438 insertions across 15 files, 46 new tests.

## Self-critique of the agent system
Rated 4/5 by the user, matching my own proposal. Two named shortfalls. (1) The central residual — that nothing forces the order's elements to be enumerated from the ORDER rather than from the already-written plan — is closed by a DIRECTIVE in --element's help text plus a state.log timestamp, not by a mechanism; so the seam can be satisfied vacuously by exactly the failure it targets, and the argument that it compares plan to order is only as strong as the enumeration was independent. This was a conscious choice (the analogue would be a second advisor pass, and F3b already shows what a fail-open advisor is worth), but it is the thing to attack next. (2) The stage-1 developer spawn was killed with an empty output file AFTER doing the work and BEFORE committing; I verified all five method items by reading the diff and committed in-thread, which is the right recovery but means the dispatched actor did not close its own stage.
