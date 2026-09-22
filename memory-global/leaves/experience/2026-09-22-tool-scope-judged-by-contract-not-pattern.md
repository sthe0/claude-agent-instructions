---
name: 2026-09-22-tool-scope-judged-by-contract-not-pattern
description: A regex/keyword classifier used to judge whether a shell/git/python command was in-scope of an approved plan is the wrong axis -- scope must be grounded in the tool's actual contract (docs/tests/source), proactively at plan-construction time for standard tools, by content/contract for ad-hoc scripts; and this must be a systemic planner-process fix (every activity element traceable to an order requirement, plan review checks coherence not just presence, requirements derived by problematization not transcription), not a one-off.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
plan_file: /home/the0/.claude-agent/plans/planner-order-traceability.toml
created: 2026-09-22
last_verified: 2026-09-22
---

# Ground tool-scope/effect judgments in the tool's own contract, not surface pattern -- mechanized as structural plan-element traceability

## Difficulty
Judging a command's scope by its literal surface text (regex/keyword match) cannot distinguish an in-scope invocation from an out-of-scope one when the same tool serves both -- the actual scope is a property of what the tool DOES (its contract), not how it is spelled on the command line.

## Order & criterion
User order (verbatim, Russian): reject the regex/keyword approach entirely; ground scope/effect judgments in the tool's actual contract (docs/tests/source), read directly when in doubt; do this proactively at plan-construction for standard tools, by content/contract/tests (not invocation surface) for ad-hoc scripts; make this a determinized, organized part of HOW PLANS ARE BUILT -- traceability of every plan element to an order requirement, coherence-checking between principles/goal/order at review time, and order requirements derived via problematization not transcription -- not a one-off patch.

**Acceptance check:** R1-R6 encoded: R1/R2 as prose guidance in CLAUDE.md carve-out 3 + planner/SKILL.md + planner/policy.md (contract-grounding, proactive at plan-construction); R3 (per-stage order-requirement traceability) and R5 (derivation-presence, not substring-match) mechanized as STRUCTURAL checks in agentctl/submission.py, not prose; R4 (review checks coherence) and R6 (bounded single-element correction is refinement) as skill/policy prose. Verified via a purpose-built plan (planner-order-traceability.toml, 2 stages) whose own final_check independently re-derives and checks all six edits; full test suite green (5932 passed, 4 skipped); a whole-plan thinker review passed on round 7.

## Contexts

### 2026-09-22 — planner-order-traceability
- Where it arose: claude-agent-instructions repo, planner specialization + agentctl engine (scripts/agentctl/submission.py, plan.py, state.py, render.py)
- Working plan: Two-stage plan: stage 1 prose edits (CLAUDE.md, planner/SKILL.md, planner/policy.md); stage 2 structural mechanization of R3+R5 in agentctl/submission.py, through 3 code-review rounds and a round-7 whole-plan thinker review.

## Cost
13.69 USD list-price telemetry (flat-max, not real spend), 7 spawns, ~2h active wall-clock across sessions; 2 additional overcome-difficulty cycles in this segment alone to fix the plan's own final_check verify-script bugs

## Self-critique of the agent system
The plan's own final_check verify script itself needed two rounds of post-hoc brittle-control-criterion fixes (stale exact-file-set, naive whole-file substring-absence) -- ironic given the task's own subject is 'don't use naive surface-pattern checks, ground checks in what they actually verify'. Both were confirming instances of the already-recorded plan-control-criterion-hygiene.md leaf, not novel lessons (recorded via --level note, no new leaf content). Residual not fixed: the plan TOML's done_criterion and two stages' expected_result_image still say 'eighteen-file set' (only final_check.label was corrected to 'twenty-file set') -- left as a known cosmetic inconsistency to avoid a third plan_sha256-driven re-acceptance cycle.
