---
name: 2026-08-11-sync-stash-identity-not-position
description: sync-instructions-repo.sh pull stashed uncommitted work and restored it with a POSITIONAL 'git stash pop' — on a machine whose stash stack held nine entries belonging to other sessions, that restores a FOREIGN entry into the canonical checkout. Three compounding defects: (a) has_uncommitted() was identically true, so even a clean tree entered the stash path; (b) the pop was positional, not by the sha the run itself created; (c) a conflicting pop left the canonical tree with unmerged paths and conflict markers while the failure reached NEITHER stderr NOR the exit status — set -e is inert because main runs 'cmd_pull && …' and cmd_sync runs 'cmd_pull || true'. Fix (landed d793ecb): resolve our own entry by sha, keep the identity->position window instruction-free, add a post-condition that treats 'our sha still on the stack after a successful pop' as a concurrent-writer shift and fails loudly, restore the tree to HEAD on a conflicting pop leaving the work parked in the stash, and print the exact recovery command. Measured git facts that constrain any such fix: 'git stash pop <raw-sha>' is REJECTED by git (only 'git stash apply <sha>' takes a raw sha), so the position conversion is unavoidable and must instead be narrowed and detected; '<sha>^3' exists only on a -u entry; 'git stash push -u' on a clean tree creates zero entries and exits 0; git C-quotes non-ASCII paths unless '-c core.quotePath=false' is passed to EVERY path source feeding a comparison. Second, unrelated difficulty met the same task: the full-tree verify-all run scans TRACKED files only, so it passed green while the staged pre-commit run failed — a verification that could not see its own (still-untracked) delivery.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev"
refs: [2026-07-17-2026-07-17-core-pr-landing-with-fine-grained-pat, 2026-07-02-periodic-instruction-refresh-offer-not-silent-pull, 2026-06-30-2026-06-30-verify-spawned-developer-commit-scope-shared-tree, 2026-06-29-agentctl-verify-venue-worktree-needs-substantive-replan]
created: 2026-08-11
last_verified: 2026-08-11
---

# Addressing a shared stack by position instead of identity: sync-instructions-repo popped other sessions' stashes, silently

## Difficulty
A helper operating on a SHARED, machine-global stack (git stash) addressed its own entry by POSITION ('git stash pop' with no argument = top of stack) rather than by IDENTITY. On this machine the stack carried nine entries of other sessions' unfinished work, so the helper restored a foreign entry into the canonical checkout; when that conflicted it left the canonical tree unmerged with conflict markers, which broke an unrelated hook the next day. The failure announced itself to nobody: not stderr, not the exit status (set -e is inert under 'cmd_pull && …' and 'cmd_pull || true'), only a log file nobody reads. A fourth defect made it fire on every single run: the has_uncommitted() predicate was identically true, so even a clean tree entered the stash path.

## Order & criterion
Fix the stash-selection defect in scripts/sync-instructions-repo.sh: (a) restore by reference to OUR OWN stash entry, not the top of the global stack; (b) never leave the canonical checkout conflicted — on failure return the tree to HEAD and keep the work parked in the stash; (c) announce the failure outwards (stderr + non-zero exit), not only into a log file.

**Acceptance check:** measurable: the suite scripts/tests/test_sync_stash_pop_safety.py + test_sync_instructions_repo.py green (18/18), prove_stash_tests_discriminate.sh printing DISCRIMINATION PROVEN with the fully-broken variant red on ALL tests and the two partial variants red where it counts, the fix landed on origin/main, and a live pull on the canonical checkout leaving the nine foreign stash entries untouched.

## Contexts

### 2026-08-11 — 2026-08-11 — initial
- Where it arose: Core instruction repo (~/claude-agent-instructions), canonical checkout shared with a dozen live worktrees and nine foreign stash entries; delivery from an isolated worktree, landed by fast-forward.
- Working plan: sync-stash-pop-safety.toml — 4 stages: (1) author tests that go red on the defect and prove their own discriminating power against three deliberately-broken script variants; (2) fix the script, gated by an independent code-reviewer verdict; (3) documentation (the recovery rule in skills/self-improvement/policy.md, the scripts/README.md row); (4) land on origin/main and prove the canonical checkout carries it with the foreign stash stack intact.

## Cost
1 session (post-compaction continuation); 7 spawns (3 developer, 2 code-reviewer, 1 thinker plan-review, 1 planner); $28.14 list-price telemetry; 4831 s engine wall-clock; 2 review rounds (revise -> pass) plus one voluntary post-pass round of 4 fail-open nits.

## Self-critique of the agent system
Three things worth re-norming. (1) Verification scope: stage 3's full verify-all passed while the staged pre-commit run failed, because the two new test files were still untracked and therefore outside the full run's glob — a control that could not see its own delivery. (2) I launched a specialist with invented flags (--session/--budget/--task-file) instead of reading spawn-specialist.py's actual signature (--kind/--plan/--done-criterion/--criterion-type, brief via --constraints); the 1-lookup budget exists precisely for this. (3) I described a developer brief in a reply and launched nothing; the user had to point out there were no running processes. A brief that is written but not dispatched is not work in progress. Positively: every claim in every specialist report was re-measured by me before being reported to the user, and one such re-measurement (removing the third core.quotePath=false flag -> 1 failed) is what showed the coverage gap the reviewer had flagged was genuinely closed.
