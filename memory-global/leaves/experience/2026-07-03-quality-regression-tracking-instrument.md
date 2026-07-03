---
name: 2026-07-03-quality-regression-tracking-instrument
description: Built the missing loop for detecting task-quality degradation caused by instruction/code edits: mandatory user-confirmed 1-5 rating at the resolution gate stamped with instructions_head, in-flight user-signal counters in the scorecard, degradation flags yielding a commit range, and a ranked-shortlist investigation helper + runbook leaf.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "Fedor (AskUserQuestion: 'Решена, оценка 3')"
refs: [policy-effectiveness-tracking, quality-regression-investigation, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]
created: 2026-07-03
last_verified: 2026-07-03
---

# Per-task quality tracking wired to instruction-commit history (resolve --quality + scorecard flags + investigate helper)

## Difficulty
<!-- Language exception: verbatim user evaluation quote, kept as the trigger evidence -->
A CLAUDE.md/skill/hook edit that degrades task-solving quality was invisible until the user complained ('после правок инструкций ты стал хуже решать задачи'): quality was assessed only as weekly-batch manual ratings + session proxies, with no per-task series and no link to the instructions-repo revision each task ran under, so no commit range could ever be blamed and fixes were guesswork.

## Order & criterion
4 stages: (1) agentctl resolve --quality 1-5 refused-if-absent, ledger row ~/.local/log/claude-task-quality.jsonl with instructions_head + engine counters; (2) policy-scorecard.py user-signal counters (corrections, questions, free-text AskUserQuestion answers, interrupts), Task-quality section, 2 degradation flags + instruction-commit range on fire; (3) quality-regression-investigate.py ranked commit shortlist (prose-removed/rule-moved/mechanized tags); (4) runbook leaf quality-regression-investigation (rating flow, rubric, fix ladder). Landed origin/main 153845e (commits 087c7be, 8114bc8, 7b275a6, 153845e).

**Acceptance check:** measurable: resolve refuses without --quality; full pytest 1296 green + verify-all 14/14; live scorecard --days 7 and live helper run clean; verify-leaf-structure + verify-memory-index green (engine verify-final PASSED all 4 stages)

## Contexts

### 2026-07-03 — initial
- Where it arose: quality-regression-tracking task, sessions 759b0d4b (plan v3 approved, v4 venue refinement), worktree /home/the0/claude-agent-instructions-quality-regression-tracking off base 94d2d95
- Working plan: ~/.claude-agent/plans/quality-regression-tracking-v4.toml (v3 + verify-venue moved to the worktree by refinement replan)

## Cost
~9h wall-clock across 2 days; spawns: stage-1 developer died 3x at budget-medium $3 (agentctl-touching stages need large), stage-2 $4.64 and stage-3 $3.60 on $8 large; plus sonnet thinker review $~0.2 for the venue replan gate

## Self-critique of the agent system
(1) Proposed rating 4, user adjusted DOWN to 3 (quality_by=user-adjusted) — first data point for the plan's own refutation watch: track the user-adjusted-downward rate for rubric inflation. (2) budget-medium repeatedly undersized for agentctl-touching developer stages — 3 dead spawns before switching to large; sizing rule candidate. (3) Live parallel-session collision in the shared tree consumed a large detour (hunk-level attribution of a mixed diff) — worktree isolation should have been the FIRST move, not the recovery.
