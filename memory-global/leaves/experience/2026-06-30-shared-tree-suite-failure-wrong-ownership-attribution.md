---
name: 2026-06-30-shared-tree-suite-failure-wrong-ownership-attribution
description: Difficulty — a full-suite or gate run goes red in a SHARED working tree and you attribute it to your change, but the root cause is UNRELATED parallel-session WIP (another session uncommitted files, conflict markers, a half-finished feature). Diagnose ownership first (git status; is the failing artifact in my changeset; was it dirty or red BEFORE I touched anything) before treating suite-red as your own regression. Recurs whenever several sessions share one tree.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (inherited from parent 2026-06-29-org-portable-core-internal-coupling-opt-in; re-confirmed live this session)"
refs: [2026-06-29-org-portable-core-internal-coupling-opt-in.md, 2026-06-29-resume-paused-task-isolated-worktree-pinned-sha.md]
created: 2026-06-30
last_verified: 2026-06-30
---

# Diagnose ownership before treating a shared-tree red suite as your own regression

## Difficulty
A shared-tree suite/gate failure is misattributed to your change when the real cause is unrelated parallel-session uncommitted WIP.

## Order & criterion
On any red full-suite/gate in a shared tree: (1) git status — list dirty + untracked; (2) is the failing artifact in MY changeset; (3) was it dirty/red BEFORE I touched anything (git stash my changes and re-run). Only if yes-mine treat as my regression; else isolate the foreign WIP and proceed on my own paths.

**Acceptance check:** git stash your own changes and re-run the suite: if still red with the same failures, the red is pre-existing/foreign, not yours.

## Contexts

### 2026-06-30 — 2026-06-29 — org-portability gate contamination
- Where it arose: claude-agent-instructions shared tree — org-portability final-suite gate hit a collection SyntaxError originating from an unrelated parallel-session dirty tree
- Working plan: Ran git status and asked is-this-file-in-my-changeset / was-it-dirty-before; confirmed foreign WIP; isolated it and proceeded on own paths.


### 2026-06-30 — 2026-06-30 — verify-all baseline contamination (this session)
- Where it arose: enforce-subdifficulty-extraction Stage 1: verify-all.py came back red (verify-readme + lint-hooks-executable) right after the gate change landed — looked like the new gate broke CI
- Working plan: git stash my two changed files and re-ran verify-all at HEAD: still red with the SAME 2 checks, caused by an unrelated parallel-session feature left untracked (scripts/enter-task.sh + scripts/project_entry/). Confirmed pre-existing/foreign, not my regression; committed only my own paths.

## Common core & variations
**Common:** A red shared-tree gate misattributed to your change; the stash-baseline re-run isolates foreign from own.

**Variations:** Here the contaminant was untracked files (not conflict markers) and the symptom was a check FAIL (not a collection error); same ownership diagnosis resolved it.

## Cost
~1 wrong-ownership detour before the ownership check cleared it.

## Self-critique of the agent system
Panicked at the SyntaxError before checking ownership; the 30-second ownership check must precede any the-suite-is-broken conclusion.

> Deterministic mechanism (2026-07-01): the shared-tree contamination behind this wrong-ownership detour is now surfaced proactively by the deterministic cross-session scope subsystem — a session-scope registry + online conflict detector that warns/blocks on a live cross-session overlap, plus a backend-blind `session-isolate.sh` router (git worktree / arc mount) to isolate instead of serialize. See `memory-global/leaves/system-knowledge/cross-session-scope-isolation.md` and `docs/operations/cross-session-scope-isolation.md`.
