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


### 2026-07-14 — second instance: reachability blind to the wikilink edge type
- Where it arose: claude-agent-instructions Core, worktree /tmp/wt-self-diagnose-wikilink branch self-diagnose-wikilink-reachability; scan_orphans BFS in scripts/self-diagnose.py
- Working plan: scan_orphans's mechanized reachability modeled only markdown path-link edges — blind to `[[wikilink]]` edges, the OTHER link type the memory actually uses (a leaf referenced from an index by frontmatter name: slug). Result: a wikilink-only-reachable leaf (smd-task-worktree-checkpoint.md) was FALSELY flagged orphan-leaf, forcing manual 'do not delete' triage and risking deletion of live-referenced content. Same trap #1 as the initial context (mechanize only the OBVIOUS subset of a decidable rule) recurring on the SAME scanner one week later. Fix (spawn:developer, flag-only preserved): _WIKILINK_RE + _build_name_index (frontmatter name:->path, first-wins) + follow [[slug]]/[[slug|alias]] from index files only; unknown slug ignored so real orphans still caught. Verified: pytest 1847 passed (1844+3); false orphan gone. Side-lesson: a dispatched developer reported 15 unrelated hook_turn_end_gate failures as 'pre-existing'; root independently re-ran and found them NOT reproducible (transient) — verify a spawned specialist's 'pre-existing failure' claim against a clean tree before folding it into the done-criterion narrative.

## Common core & variations
**Common:** mechanizing a decidable rule must enumerate the FULL domain of the rule (here: EVERY edge type a reachability check can traverse), not the first/obvious subset — the mechanical-enumeration discipline applied to one's own determinization

**Variations:** initial: near-duplicate/orphan/broken-hook still prose after stage-3 shipped 3-of-N detection classes. this instance: orphan reachability shipped 1-of-2 edge types (markdown, not wikilink).

## Cost
1 planner + 1 thinker plan-review + 1 spawn:developer (dispatch timed out 9m20s exit143 -> marker-drop -> verified worktree directly + committed) ; ~4 engine difficulty-cycle transitions.

## Self-critique of the agent system
The rule/perception boundary is load-bearing: a self-diagnostic scanner over memory must be flag-only — auto-merging near-duplicates would destroy content a human should review. default_settings_paths must cover PROJECT settings (project/cwd .claude/), not just the user-level config root — the motivating broken hook lived in project-composed settings; a user-only default would miss its own motivating class in the automatic SessionStart path (caught by me, confirmed blocking by thinker plan-review).
