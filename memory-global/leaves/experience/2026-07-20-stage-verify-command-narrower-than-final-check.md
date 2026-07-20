---
name: 2026-07-20-stage-verify-command-narrower-than-final-check
description: An agentctl plan stage's verify_command must cover the FULL material the stage touches, not a subset. Twice in one plan a stage was recorded PASSED while harboring a red that only the broader final_check later surfaced: stage 1 added a committed script (scripts/semantic_judge.py) but its verify_command ran only pytest, never verify-readme, so the missing scripts/README.md entry survived (docs-accompany-code); stage 2 edited a hook (hook-escalation-diagnosis-gate.py, moved to a deliberate prefilter-only design) but its verify_command did not include that hook's test file, so 3 stale deny-tests encoding the retired regex behavior survived. Both turned routine completion into a late overcome-difficulty cycle at verify-final. Same family as [[2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]] (verify mis-scoped in venue/scope/suite) — this vector is: stage verify_command SUBSET of final_check.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com"
refs: [/home/the0/.claude-agent/plans/lang-agnostic-judge-and-baseline-diff.toml, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]
created: 2026-07-20
last_verified: 2026-07-20
---

# A stage verify_command narrower than the plan's final_check lets a PASSED stage hide a red

## Difficulty
verify-final failed on two completeness gaps that already-PASSED stages harbored, because each stage's verify_command was narrower than the plan's final_check (full pytest suite + verify-all --staged). A green stage gave false confidence; the reds only appeared at the end.

## Order & criterion
Deliver a language-agnostic semantic judge (partition A, stages 1-2) + a reusable baseline-diff violation-granularity primitive (partition B, stages 3-4), each stage verified via record-result then the plan's final_check at verify-final.

**Acceptance check:** measurable: full suite pytest scripts/tests -q green AND verify-all.py --staged exits 0. A stage verify_command must be a superset (for that stage's material) of what final_check re-checks: a stage adding a committed script runs verify-readme; a stage editing hook/module X runs test_X.

## Contexts

### 2026-07-20 — initial
- Where it arose: claude-agent-instructions engine-driven substantive plan (lang-agnostic-judge-and-baseline-diff.toml); worktree /home/the0/cai-wt-judge, repo_root pinned there per the stages' worktree-authoring mandate.
- Working plan: Ran the full overcome-difficulty cycle (declare/investigate with 3 hypotheses/critique failure-address=normative/normalize) + mandatory thinker review (pass) + refinement replan that widened stage 1 verify_command (&& verify-readme) and stage 2 verify_command (+test_hook_escalation_diagnosis_gate.py +test_hook_self_improvement_reminder.py); then fixed the 3 stale tests by adding an HTTP-503/unreachable protocol token to the shared ESC_BODY (preserving the deny-matrix intent, NOT flipping to allow, because the gate is prefilter-only by design) and registered semantic_judge.py in scripts/README.md (which shifted a line-pinned config-root-refs-allowlist entry 106->107, a small coupled fix).

## Cost
Developer work ran in an isolated worktree (`/home/the0/cai-wt-judge`); ≥1 thinker spawn for the mandatory refinement-replan review, plus the overcome-difficulty cycle. Under a flat-Max subscription the token spend is list-price telemetry, not real money (≈$0). The real cost was wall-clock: the two completeness gaps surfaced only at `verify-final` — a stage `verify_command` narrower than the plan's `final_check` gave two false-green PASSED records and forced a late overcome-difficulty + refinement-replan cycle at landing instead of at record-result time.

## Self-critique of the agent system
I recorded stages 1-2 PASSED trusting the stage verify_command's named test set, without cross-checking that it covered every file the stage modified (hook-escalation-diagnosis-gate.py, hook-self-improvement-reminder.py were edited but their tests unlisted). The engine's own final_check caught it — the spine worked — but a superset-check at record-result time would have caught it a cycle earlier.
