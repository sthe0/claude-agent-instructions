---
name: 2026-06-30-shared-tree-suite-failure-wrong-ownership-attribution
description: Difficulty — a full-suite or gate run goes red in a SHARED working tree and you attribute it to your change, but the root cause is UNRELATED parallel-session WIP (another session uncommitted files, conflict markers, a half-finished feature). Diagnose ownership first (git status; is the failing artifact in my changeset; was it dirty or red BEFORE I touched anything) before treating suite-red as your own regression. Recurs whenever several sessions share one tree.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (inherited from parent 2026-06-29-org-portable-core-internal-coupling-opt-in; re-confirmed live this session)"
refs: [2026-06-29-org-portable-core-internal-coupling-opt-in.md, 2026-06-29-resume-paused-task-isolated-worktree-pinned-sha.md]
created: 2026-06-30
last_verified: 2026-07-02
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


### 2026-07-01 — A plausible PRIOR narrative reinforced the mis-attribution — 7 foreign failing tests were nearly taken as my blocker
- Where it arose: Core task complete-config-root-isolation (session 4445f907): finishing a 6-stage runtime-reader migration on the isolate-agent-config-root branch, shared tree with a live parallel session building auto-migrate-on-pull.
- Working plan: At the resolution gate, the full suite showed 7 NEW failures in test_sync_instructions_repo.py (incl. a broken plain push) plus modified sync-instructions-repo.sh, config-root.sh, doctor.sh, git-workflow.md, setup.md — none in my 6-stage file set. Because a REAL earlier fact held ('the stage-4 developer over-reached, doing stages 4+5'), I confidently mis-attributed this NEW foreign WIP to that same developer and spent tool-calls diagnosing it as my own broken deliverable (root-causing the push regression, weighing complete/exclude/revert). The user corrected the attribution in one line (paraphrased from Russian: "the auto-migration is happening in the neighboring session, not here"). Only then did ownership-diagnosis land: git status file set vs my plan's stages, timestamps (config-root.sh written 21:23 by the neighbor), and the foreign experience-leaf diff documenting THEIR task. Recovery: commit ONLY my explicit paths (git add <25 files>, never -A), leaving the neighbor's WIP + an unrelated Cloudflare leaf untouched; the 7 failures are the neighbor's incomplete feature, not my regression.

### 2026-07-02 — Destructive sync op (reset --hard) needlessly reverted a parallel session's uncommitted tracked WIP
- Where it arose: claude-agent-instructions shared tree — merging all remote branches into main trunk. To advance local main I ran 'git checkout main' + 'git reset --hard origin/main' before an ff-merge, while a parallel session held uncommitted WIP in the tracked scripts/hook-tracker-reminder.py (a new mount-hygiene feature).
- Working plan: The whole local-main dance was UNNECESSARY: I was already on the rebased working branch (= origin/main + the registry commit), so a single ref-only push — 'git push origin HEAD:main' — would have advanced trunk WITHOUT touching the working tree. Instead reset --hard reverted the neighbor's tracked-file WIP to the committed version (untracked files survive reset --hard; tracked modifications do not); it only reappeared because the neighbor's editor re-saved it — recovery by luck, not design. Rule: in a KNOWN-shared tree, advance/sync a branch with REF-ONLY ops ('git push <remote> HEAD:<b>', 'git branch -f <b> <ref>', 'git update-ref'), never working-tree-touching destructive ops ('git reset --hard', 'git checkout -f', 'git clean'). If a destructive op is truly unavoidable, 'git status' for foreign dirt and 'git stash' it first.
## Common core & variations
**Common:** A red shared-tree gate misattributed to your change; the stash-baseline re-run isolates foreign from own.

**Variations:** Here the contaminant was untracked files (not conflict markers) and the symptom was a check FAIL (not a collection error); same ownership diagnosis resolved it.

## Cost
~1 wrong-ownership detour before the ownership check cleared it.

## Self-critique of the agent system
Panicked at the SyntaxError before checking ownership; the 30-second ownership check must precede any the-suite-is-broken conclusion.

> Deterministic mechanism (2026-07-01): the shared-tree contamination behind this wrong-ownership detour is now surfaced proactively by the deterministic cross-session scope subsystem — a session-scope registry + online conflict detector that warns/blocks on a live cross-session overlap, plus a backend-blind `session-isolate.sh` router (git worktree / arc mount) to isolate instead of serialize. See `memory-global/leaves/system-knowledge/cross-session-scope-isolation.md` and `docs/operations/cross-session-scope-isolation.md`.
