---
name: 2026-06-25-state-gate-needs-acting-session-at-executing-via-toml
description: Production Edit/Write is denied unless the engine state of the session that issues the write is at an execution node (EXECUTING). Two non-obvious consequences: (1) markdown plans are structure-verified but do NOT populate state.stages, so next-stage/dispatch never reach EXECUTING — only a .toml plan populates stages; (2) a spawned developer runs under its OWN fresh, unclassified session, so the parent being at EXECUTING does not authorize the child — the child is denied every write and burns its whole budget before dying.
type: reference
schema: difficulty/v1
resolution_confirmed_by_user: "Да, решена (Recommended)"
refs: [2026-06-24-developer-marker-not-on-line-1-false-block.md, 2026-06-24-gate-exemption-is-category-error-for-result-images.md]
---

# hook-state-gate authorizes by the ACTING session's engine node, reached only via a .toml plan

## Difficulty
An in-thread or spawned production edit is blocked by hook-state-gate even though a plan was approved, because the ACTING session's engine is not at EXECUTING. Reaching EXECUTING requires populated state.stages, which a markdown plan does not create (only .toml does); and a spawned specialist has a separate session whose engine starts unclassified, so it inherits none of the parent's execution authority.

## Order & criterion
Before relying on in-thread carve-out or a spawned developer for gated writes: (a) submit a .toml plan (not just markdown) so stages populate; (b) drive the ACTING session to EXECUTING (classify -> ... -> dispatch/next-stage); for a spawn, the child must classify+reach EXECUTING in its own session, or the manager applies the reviewed code in-thread after driving ITS session to EXECUTING.

**Acceptance check:** measurable: agentctl status of the acting session shows node=EXECUTING with stages present; the gated Edit/Write then succeeds.

## Contexts

### 2026-06-25 — initial
- Where it arose: DEEPAGENT self-improvement: adding hook-self-improvement-reminder.py. Spawned developer denied all writes + hit $3 budget (engine not at EXECUTING; markdown plan -> no stages; spawned session unclassified). Recovered by switching to a .toml plan and applying the reviewed code in-thread after driving the manager session to EXECUTING.
- Working plan: Diagnose deny reason -> recognize stages empty -> author .toml plan with in_thread stages -> reset to CLASSIFIED to write the plan -> walk classify/plan/submit-plan/approve/next-stage/dispatch to EXECUTING -> apply reviewed code in-thread -> record-result per stage -> verify-final -> resolve.

### 2026-06-26 — recurrence (decompose 4 near-ceiling instruction surfaces)
- Where it arose: DEEPAGENT instruction-decomposition task (4 files -> sibling policy.md / leaf). A developer spawned for the file moves (ID byvunrgrr) was again denied every production Write to skills/** and burned its medium ($3) budget; only the gate-exempt memory-global leaf it wrote survived. Same root cause: the spawned session's engine was never classified/approved.
- Resolution: applied the whole decomposition in-thread (the manager session was already at EXECUTING via the .toml plan, so the gate was open). Net effect of the wasted spawn: ~$3 + ~23 min, zero usable production files. Confirms the rule: **instruction-repo refactors (production writes under skills/**, cursor/**, scripts/**) must run in-thread once the manager session is at EXECUTING — do not spawn a developer for them.**

### 2026-06-26 — positive application + the review refinement (agentctl drive/close wrapper)
- Where it arose: building the `agentctl drive`/`close` spine-orchestrator wrapper (production code under `scripts/agentctl/cli.py` + tests). Per this leaf's rule I did NOT spawn a developer for the gated `scripts/**` writes; I drove the manager session to EXECUTING via a .toml plan and applied all stages in-thread — zero wasted spawn budget this time.
- The tension this resolves: CLAUDE.md says "substantive production code → spawn developer, never write it yourself", but this leaf says spawning for `scripts/**` gets gate-denied. Resolution: when forced in-thread, get the independent-review value another way — spawn a **read-only `code-reviewer`** after writing the code. A reviewer makes NO production writes, so hook-state-gate never fires on it (its own session being unclassified is irrelevant). Here that surfaced 2 real test-coverage gaps + 1 docstring nit, all fixed before record-result. So "apply in-thread" and "get an independent specialist's eyes" are not in conflict — the executor must be in-thread, but the *controller* (reviewer ⊂ controller) can still be an independent spawn.
- Net: 0 wasted budget; ~$0.x on the read-only reviewer (small/medium), which returned actionable findings.

## Cost
~$3 wasted on a gate-denied spawned developer in the first TWO occurrences (2026-06-25 and 2026-06-26 decompose); each recovered in-thread. The 2026-06-26 wrapper task applied the rule proactively (0 waste) and added the read-only-reviewer refinement. The early repeat is direct evidence the spawn-for-gated-writes anti-pattern is easy to fall back into.

## Self-critique of the agent system
Should have checked the acting session's engine node + stages BEFORE spawning a developer for gated writes; spawning into an unclassified child session guaranteed denial. The markdown-vs-toml stages distinction is under-documented at the dispatch step.
