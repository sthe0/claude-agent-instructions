---
name: 2026-07-14-determinize-memory-org-principle-in-code
description: Requirement "maximally deterministic" + CLAUDE.md "separate rule from perception" demand the DECIDABLE part of a prose principle become a checkable code invariant; mechanize the FULL decidable set (not the obvious subset), keep the scanner flag-only (perception stays the model's), and model a new approved requirement surfaced at the resolution gate as reject→replan of the SAME task to preserve ONE delivery/ONE PR.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev@gmail.com"
refs: [memory-hierarchy.md reflexive-exit-is-base-activity-figure.md self-diagnose.py]
created: 2026-07-14
last_verified: 2026-07-14
---

# Determinize the full decidable set of a prose principle in code, and model resolution-gate scope-expansion as reject→replan

## Difficulty
A standing prose principle (memory-organization: two decomposition axes + generalize-and-group + reachability integrity) satisfies requirement (h) "maximally deterministic" only when its DECIDABLE part is a checkable code invariant, not prose. Two traps: (1) mechanizing only the obvious subset of the decidable rule (stage-3 shipped 3 of the detection classes; near-duplicate/orphan/broken-hook were still prose) — an instance of CLAUDE.md's mechanical-enumeration rule applied to one's OWN determinization; (2) at the resolution gate the user surfaced a NEW approved requirement ("the principle must be reflected in code"), which is scope-expansion of the SAME task, not a fresh task — modelling it as a fresh task would fork delivery into two PRs against the user's explicit "mechanization first, then ONE PR".

## Order & criterion
reject the under-delivering stage (honest: stage 3 under-delivered on req h) → full difficulty cycle (declare→investigate≥2H→critique with invariants-to-preserve read-only/fail-open/org-neutral + failure-address normative → normalize → plan-review --target new-plan → replan substantive) → ONE developer stage extending the existing scanner → verify measurable done criterion → keep ONE delivery / ONE PR.

**Acceptance check:** measurable: pytest scripts/tests/ green (1835 passed); dirty fixture exits 1 with all 4 classes present + both dangling-pointer false-positive shapes suppressed; clean fixture exits 0.

## Contexts

### 2026-07-14 — initial
- Where it arose: claude-agent-instructions Core, worktree /home/the0/cai-wt-memreorg branch memory-norms; scanner scripts/self-diagnose.py 229→485 lines.
- Working plan: self-diagnose-org-invariants: single spawn:developer stage — near-duplicate (Jaccard>=0.6 flag-only) + orphan (BFS reachability) + broken-hook-registration scans + dangling-pointer HTML-comment/placeholder fix + CLI wiring + fixture tests; rule detects/flags, model decides whether to merge/re-norm (NEVER auto-edit memory).

## Cost
1 planner + 1 thinker plan-review + 1 spawn:developer (dispatch timed out 9m20s exit143 -> marker-drop -> verified worktree directly + committed) ; ~4 engine difficulty-cycle transitions.

## Self-critique of the agent system
The rule/perception boundary is load-bearing: a self-diagnostic scanner over memory must be flag-only — auto-merging near-duplicates would destroy content a human should review. default_settings_paths must cover PROJECT settings (project/cwd .claude/), not just the user-level config root — the motivating broken hook lived in project-composed settings; a user-only default would miss its own motivating class in the automatic SessionStart path (caught by me, confirmed blocking by thinker plan-review).
