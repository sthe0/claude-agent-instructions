---
name: 2026-06-29-org-portable-core-internal-coupling-opt-in
description: Difficulty — a tool (Core instruction repo) works in one org but is silently coupled to that org's INTERNAL-only facilities (Arcadia arc VCS, Startrek channel, hardcoded job-orchestrator names). The portability mistake is to ask 'what looks org-specific?' and strip it; the correct dividing line is REACHABILITY FROM OUTSIDE THE ORG BOUNDARY: neutralize only internal-only couplings, KEEP publicly-reachable services (yandex.cloud is public -> yandex-cloud-expert stays). Mechanism that preserves the working machine while defaulting clean: generic default in the shared/committed layer + internal-as-opt-in via a per-machine identity file (agent-identity.local, never committed) + auto-detection; an unconfigured (off-corp) machine is byte-identical to before. Side-difficulties met: (1) the full-suite resolution gate was contaminated by UNRELATED dirty-tree WIP (git conflict markers -> collection error) — diagnose ownership (whose files, were they dirty before I touched anything) before treating a suite-red as your own regression; (2) the just-fixed validator then surfaced the WIP author's OWN incomplete leaves (missing dates) — fixing a shared validator can newly-enforce latent defects elsewhere; (3) commit only your paths (git add explicit list) when the branch carries someone else's uncommitted WIP.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user"
refs: [2026-06-27-stub-mechanism-not-deployable-until-caller-cli-onboarding.md, 2026-06-26-guard-coupled-doc-relocation.md]
plan_file: /home/the0/.claude/plans/org-portable-core.toml
created: 2026-06-29
last_verified: 2026-06-29
last_accessed: 2026-06-30
---

# Make a single-org tool portable by the reachability dividing line: generic default in the shared layer, internal-only as per-machine opt-in

## Difficulty
A tool couples to one org's internal-only facilities; portability needs the right dividing line (external reachability) + generic-default/internal-opt-in mechanism, not removal of everything org-named.

## Order & criterion
Inventory couplings -> classify each by external reachability (internal-only vs public) -> flip shared-layer DEFAULT to the public equivalent -> move internal facility behind a per-machine opt-in (identity file + auto-detect) keeping the current machine working via override -> one-command onboarding wizard -> README/docs onboarding section -> full-suite + guards green.

**Acceptance check:** Fresh clone in a non-org environment with plain git works zero-edits; unconfigured machine byte-identical; full test suite + all repo guards green; onboarding is 3 commands.

## Contexts

### 2026-06-29 — org-portability
- Where it arose: claude-agent-instructions Core layer org-portability (CLAUDE.md/config.md prose + configure-identity.sh/hook-long-job-arm.py defaults + setup-org.sh wizard + README/docs)
- Working plan: /home/the0/.claude/plans/org-portable-core.toml
- Side-difficulties met here (now extracted, not inlined): [[shared-tree-suite-failure-wrong-ownership-attribution]] (a shared-tree full-suite red attributed to *your* change when the root cause is unrelated parallel-session WIP); [[validator-tightening-newly-enforces-latent-violations]] (the just-fixed shared validator then newly-enforced the WIP author's own latent date-less leaves). The third (commit only your paths via explicit `git add` list when the branch carries another session's WIP) is standard git hygiene — left in prose, same parallel-session context as the first.

## Cost
Moderate, and concentrated in the diagnosis, not the edits. The change itself is a 6-stage substantive plan — ~527 insertions / 19 deletions across 15 files in two commits (63e5fe3 difficulty-channel auto-detect, 0a0b52d the portability flip) — all small, mechanical prose/default/wizard edits each guarded by a new test. The real overhead came from the three side-difficulties: (1) a contaminated full-suite gate (an UNRELATED parallel-session dirty tree left git conflict markers → collection SyntaxError) cost a wrong-ownership detour before the "is this file in my changeset / was it dirty before I touched it?" check cleared it; (2) the just-fixed leaf validator then newly-enforced the WIP author's own latent date-less leaves, adding an unplanned fix-the-neighbours pass; (3) committing required an explicit `git add <paths>` list to avoid sweeping in the other session's uncommitted WIP. No precise per-task dollar figure is separable — the spawn cost log (`scripts/cost-report.py`) is a 7-day aggregate (~$186 over 79 spawns that week), and `agentctl resolve` never isolated a single-plan rollup for this task; the cost signal here is the ~1 ownership-misattribution detour + 1 neighbour-validator pass, not a billed figure.

## Self-critique of the agent system
Initial final-suite read panicked at a SyntaxError before checking file ownership; the 30-second 'is this file in my changeset / was it dirty before?' check should precede any 'the suite is broken' conclusion.
