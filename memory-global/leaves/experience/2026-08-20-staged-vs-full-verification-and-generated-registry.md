---
name: 2026-08-20-staged-vs-full-verification-and-generated-registry
description: verify-all ran green over the whole tree while the pre-commit hook refused the same commit: the full-tree run never saw the still-untracked new test file, whose sites verify-semantic-gates then found unregistered once staged. The registry is generated, so the remedy is gen_crutch_registry.py, never a hand edit — and regenerating drags in catch-up rows for already-landed files, which belong in the commit message rather than in a silent diff.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "«Да, решено — оценка 4» — the user (fedor.solovyev) at the resolution gate 2026-08-20, choosing to land into trunk in the same ask"
refs: [memory-global/leaves/experience/2026-07-29-crutch-registry-standing-prevention.md, memory-global/leaves/experience/2026-08-11-sync-stash-identity-not-position.md, memory-global/leaves/experience/2026-06-26-guard-coupled-doc-relocation.md]
plan_file: /home/the0/.claude-agent/plans/record-experience-hint-path.toml
created: 2026-08-20
last_verified: 2026-08-20
---

# Full-tree verification is blind to an untracked new file; its staged twin is not

## Difficulty
A repository that runs the same verifier suite in two modes — full-tree and staged (pre-commit) — scans two different file sets, and the new file a change ADDS is exactly the one the full-tree mode cannot see while it is still untracked. So a reported-green whole-repo verify-all does not predict the pre-commit verdict for the very delivery it was run on, and the stage's own "run verify-all and report" step produces a green number that is structurally blind to the half of the change that is new: here verify-semantic-gates counted 1219 code sites and passed, then counted 1225 and refused the commit over the new test file's 6 unregistered sites.

Third occurrence of this ground, and the first where it is the primary difficulty rather than a footnote: [[2026-08-11-sync-stash-identity-not-position]] met it in this same direction (full-tree green, staged red, untracked delivery) and [[2026-06-26-guard-coupled-doc-relocation]] in the inverse (verify-cross-refs staged mode scans only tracked files, so a full-mode pass masks a pre-commit failure). Norm: when a change ADDS files, `git add` them before the full-tree verification run, or treat that run's verdict as covering only the pre-existing tree and say so.

Corollary on the artifact the gate demanded, already recorded elsewhere and not re-derived here: crutch_registry.toml is generated, so the remedy is `gen_crutch_registry.py` and never a hand edit ([[2026-07-29-crutch-registry-standing-prevention]] for why the registry exists, [[2026-07-31-generated-config-fix-the-generator-not-the-artifact]] for the general rule). What this context adds is that the regeneration is not scoped to the change: it also emits catch-up rows for prose files landed earlier without one, so the commit carries a generated diff far larger than the change and that excess must be declared in the commit message rather than left for a reviewer to discover.

## Order & criterion
Land a small producer-side fix to record-experience.py's extend hints, in-thread, in a linked worktree.

**Acceptance check:** The commit is accepted by the pre-commit hook with staged-mode verify-all 21/21, and every row of the generated-file diff is either attributable to this change or declared as catch-up in the commit message.

## Contexts

### 2026-08-20 — initial
- Where it arose: claude-agent-instructions @ 465c094; scripts/verify-semantic-gates.py, scripts/gen_crutch_registry.py, scripts/crutch_registry.toml; 2026-08-20
- Working plan: 1. Read the gate's own instruction rather than working around it: regenerate, never hand-edit. 2. Confirm where the new sites land in the generated partition table (scripts/tests/** -> not-a-gate here), so no manual disposition is owed. 3. Re-run the verifier alone before re-attempting the commit. 4. Name the catch-up rows and their cause in the commit message, so the oversized generated diff is declared rather than discovered by a reviewer.

## Cost
Implementation and this recording ran in-thread: 0 spawns attributed to the stage. The session's 7 thinker spawns total $9.25 list-price / ~24 min (`~/.local/log/claude-spawn-costs.jsonl`, session 28328be1) — five of them the plan-review rounds for this plan, the rest the abandoned resolver plan that preceded it. Under flat Max those dollars are telemetry, not money; the real cost was the five review rounds of wall-clock before a 26-line change. The difficulty recorded here cost one refused commit plus one regeneration, ~5 min.
