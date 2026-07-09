---
name: 2026-07-09-gate-must-execute-what-it-attests
description: Difficulty — two independent defects of one family in the agentctl engine, both invisible because the engine reported green. (a) ATTEST-VS-EXECUTE: 'approve' snapshots and hashes the plan FILE, but dispatch runs the stage/final_check copy cached in state at 'submit-plan'; a legal edit at PLAN_READY (exactly how a REVISE plan-review verdict is answered) therefore lands in the artifact and in the approval hash but never in the executed copy, while plan_snapshot_hash matches the live file and reports green. Fix: re-load the plan from plan_path into the caches immediately before stamping the approval GateRecord, reading the carry-key BEFORE mutating (a refreshed stage whose carry-key changed must fall back to PENDING). (b) REFUSE-WITHOUT-A-ROUTE: a failing [[final_check]] left the session parked at VERIFYING, where declare/investigate/critique all refuse ('difficulty commands run only in the DIAGNOSING cycle'), so the only escape was 'reset --force' — the gate taught operators to bypass it. Fix: fire the already-existing VERIFYING->DIAGNOSING 'diagnose' transition from cmd_verify_final's failure branch, exactly as cmd_record_result does for a failed stage. General rule: a gate's authority is only as real as the bytes it actually executes, and a refusal with no reachable resolution path is a bypass trainer. Control criterion for both: MUTATION PROOF — a verify_command is not trusted until it is shown to FAIL against a deliberately mutated implementation; and pin EXACT pytest node ids, never 'pytest -k <substr>', which substring-matches an unrelated pre-existing test and passes vacuously.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
tier: 1
refs: [2026-07-04-topological-gate-when-signal-unobservable.md, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan.md, 2026-06-24-gate-exemption-is-category-error-for-result-images.md]
plan_file: /home/the0/.claude-agent/plans/hook-ask-defer-timer-block.v4.toml
created: 2026-07-09
last_verified: 2026-07-09
---

# A gate must execute the artifact it attests, and every refusal it can issue must reach a resolution cycle

## Difficulty
A gate reports green while governing bytes it does not execute; and a gate that can refuse offers no transition into the cycle that resolves a refusal.

## Order & criterion
Notice the executed spec differs from the attested artifact (dispatch handed a stale stage after a legal PLAN_READY edit) -> read WHERE each cache is populated (submit-plan) vs WHERE the hash is taken (approve) -> refresh caches from plan_path inside approve, before the GateRecord, carry-key read before mutation -> separately, notice a refused final_check has no outgoing edge, confirm the 'diagnose' edge already exists in machine.py and only cli.py fails to fire it -> pin both with exact pytest node ids -> mutation-proof each pinned test against the reverted hunk.

**Acceptance check:** measurable: both pinned node-id tests FAIL against the un-patched cli.py and PASS with it (mutation proof run, not assumed); whole scripts/tests/ suite stays green (1508 passed).

## Contexts

### 2026-07-09 — 2026-07-09 — initial
- Where it arose: claude-agent-instructions: scripts/agentctl/cli.py (cmd_approve _refresh_caches_from_plan_path, cmd_verify_final failure branch, cmd_replan no_change final_check), scripts/tests/test_cli_directives.py, scripts/tests/test_verify_execution.py. Surfaced while landing scripts/hook-ask-defer-timer.py; the stale-spec defect bit the very plan that was fixing the hook.
- Working plan: /home/the0/.claude-agent/plans/hook-ask-defer-timer-block.v4.toml


### 2026-07-09 — 2026-07-09 — the same family, two more instances: a verifier's domain and a directive's announcement
- Where it arose: claude-agent-instructions: scripts/verify-config-root-refs.py (_iter_repo_files, find_ungoverned), scripts/config-root-refs-allowlist.txt, scripts/agentctl/cli.py (cmd_critique). Filed as GitHub issues #25 and #26, fixed by 4b24b29 + e956f28 on main.
- Working plan: /home/the0/.claude-agent/plans/fix-verifier-and-critique-gate.toml

## Common core & variations
**Common:** One functional ground with the two engine defects above: a gate or verifier is only as truthful as the thing it actually reads. Attest what you execute; announce only what you read; scan only what is under version control. Each instance reported GREEN (or a confident directive) while grounded in something other than the artifact it governed.

**Variations:** Three new instances. (1) VERIFIER DOMAIN: _iter_repo_files walked repo_root.rglob('*') though its own docstring promised 'tracked-shape' files, so the verdict was a property of one machine's scratch — an allowlist entry pinned a file that commit 77ce06c added to the allowlist but never committed: stale (red) in a clean checkout, matched (green) in a tree where the file existed untracked. Fixed with git ls-files plus a narrow fallback for non-git domains. The subtle part (caught by plan-review, not by me): find_ungoverned re-derives the domain with a SECOND independent rglob, so fixing only find_occurrences would have traded the stale-entry red for an ungoverned red. Induced rule: an allowlist entry ships in the SAME commit as the file it governs. (2) EXEMPTION KEY: five allowlist entries were pinned by line number into an append-only, machine-generated index; prepending one pointer shifted all five, and the mechanical re-pin would have carried zero review value. Key an exemption by the identity of the thing exempted, not by a coordinate that shifts for unrelated reasons. The grammar already supported a whole-file entry — no parser change, only data. Accepted residual risk recorded in the plan: a whole-file exemption is wider than its justification. (3) UNREAD ANNOUNCEMENT: cmd_critique asserted 'replan is now unblocked' from the presence of three record sections while gates.difficulty_blockers had since grown a shape check (>=2 distinct hypotheses, anti-template declaration) the announcement never learned about. Fixed by consulting the gate — and deliberately NOT firing it, since cmd_replan logs the same gate and test_gate_telemetry asserts an exact gates_fired list.

## Cost
Moderate: 4 spawns, $7.14 total (thinker r1 hit the $1 cap -> MALFORMED, verdict recovered from the transcript jsonl; stage-3 developer hit the $3 cap -> MALFORMED -> BLOCKED though the work was complete; stage 4 dispatched at --budget large, $4.13, clean marker). Two full overcome-difficulty cycles. The design insight (attest == execute) was load-bearing; the patch is ~20 lines.

## Self-critique of the agent system
I wrote a vacuous control criterion twice in one task (shell backtick substitution inside a double-quoted python -c; then 'pytest -k verify_final' substring-selecting an unrelated pre-existing test). Both times the verify_command COULD NOT FAIL, so it was not a control. Mutation proof must be the standing discipline, not a remedy applied after a reviewer catches it. Separately: a budget-cap exit prints MALFORMED and the engine routes to BLOCKED even when the work is complete — the manager then re-verifies by hand; the wrapper should distinguish 'no marker because capped' from 'no marker because failed'.
