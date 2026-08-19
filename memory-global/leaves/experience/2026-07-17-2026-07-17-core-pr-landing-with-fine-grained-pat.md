---
name: 2026-07-17-2026-07-17-core-pr-landing-with-fine-grained-pat
description: A fine-grained GitHub PAT that can create PRs and issues often CANNOT merge a PR (needs Contents:write), create a repo, or delete a branch — all return 403 'Resource not accessible by personal access token'. Land a Core PR git-natively instead: rebase the branch onto the (usually moved) origin/main, resolve conflicts, then fast-forward push branch:main via SSH — GitHub auto-closes the PR as merged when its commits land in main. Second trap met the same session: a guarded 'git stash list | grep -q . && git stash pop' will blindly pop a PRE-EXISTING unrelated stash left by another task/session (the list is global, not per-branch), conflicting into the tree; guard the intent, not just stash-non-emptiness.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev"
refs: [solved-by-007-marker-and-usage-stats, agent-usage-telemetry, 2026-07-09-landed-not-deployed-checkout-parked-on-feature-branch, 2026-06-29-org-portable-core-internal-coupling-opt-in, 2026-08-11-sync-stash-identity-not-position]
created: 2026-07-17
last_verified: 2026-08-19
---

# Landing a Core PR with a fine-grained PAT: SSH ff-push, not the API merge button; and the wrong-stash-pop trap

## Difficulty
Landing the solved_by_007+telemetry PR to Core main: the fine-grained PAT (login sthe0) created the PR fine but the merge API returned 403 (lacks Contents:write); it also can't create the private telemetry repo (deferred to backlog #37) nor delete the merged branch. Separately, a no-op 'git stash -u' (tree already committed) followed by a guarded 'git stash pop' popped an unrelated stash from a different task and conflicted README.md + an unrelated leaf.

## Order & criterion
1. Confirm main hasn't moved (it had: ee00a18->d764071). 2. Rebase branch onto origin/main; resolve additive conflicts in cli.py (merge new sibling imports + both cmd_classify branches) and the allowlist (recompute README line numbers via verify-config-root-refs). 3. Run verify-all + touched tests; distinguish a pre-existing trunk failure (benchmark-profile cross-ref) from your own regression. 4. force-with-lease the rebased branch, then 'git push origin branch:main' (SSH ff). 5. Verify PR merged+closed via API.

**Acceptance check:** acceptance-review: PR shows state=closed merged=true; origin/main == rebased commit; verify-all green except the pre-existing trunk red; feature tests 56/56.

## Contexts

### 2026-07-17 — initial
- Where it arose: Core instruction repo (github, public), landing from an isolated worktree off main; anchor mount shared with parallel live sessions.
- Working plan: solved-marker-and-007-stats.toml — solved_by_007 marker (engine-executed at resolve) + agent-stats.py local report + usage-digest.py opt-in cross-installation telemetry.

### 2026-08-11 — the same defect, in our own tooling, in production
- Where it arose: `scripts/sync-instructions-repo.sh`, `pop_stash_if_any` (:59-70) — the fleet's own sync script, not an ad-hoc command.
- What is new versus the 2026-07-17 instance: that one was a hand-written `git stash list | grep -q . && git stash pop` typed into a one-off landing command, where the blast radius was one tree during one landing. This one is the same shape **committed into the script every machine runs**. It ran on a CLEAN canon — so the run's own `git stash push -u` had created nothing — popped another session's 26-file rescue stash onto a fresh HEAD, left `scripts/agentctl/cli.py` unmerged, and took the coordination engine down machine-wide. Full account: [[2026-08-11-sync-stash-identity-not-position]].
- The carry-forward, which is the actual lesson: **the lesson was already recorded in this leaf and the defective code shipped anyway.** What was missing was never the lesson — it was a **mechanical consumer** of it. A rule that lives only as prose in a leaf is not read at the moment somebody writes `git stash pop` into a script. The corresponding control is a grep over this repo's own `scripts/` for a bare `git stash pop` (no sha, no `apply`-then-positional-`drop`), so the norm is enforced where the code is written rather than recalled where the leaf is read.
- Measured git facts this instance added (git 2.43, measured on this machine, not quoted from documentation), kept here because they cost real time to re-derive:
  - `git gc --prune=now` does **not** collect a stash commit — it stays reachable through the `refs/stash` reflog and `git cat-file -e <sha>` still succeeds afterwards. So a missing entry always means somebody dropped or popped it; gc is never the explanation.
  - `git stash apply <sha>` **does** restore untracked files from a `-u` entry, so an advertised `stash apply` recovery is not narrower than the loss it recovers from.
  - `post-merge` fires on `git merge --ff-only`, and `pre-rebase` fires on **both** rebase backends — the two fixture points a concurrent-writer test needs, one per branch of `cmd_pull`.

## Cost
1 session (post-compaction continuation); ~7 spawns prior; landing itself ~15 tool calls, 2 conflict resolutions, 1 self-corrected stash mistake.

## Self-critique of the agent system
The guarded stash-pop was a real self-inflicted error: 'git stash list | grep -q .' tests global stash presence, not whether a stash belongs to THIS task — I popped another session's stash. Lesson folded into the leaf. Also: I could have checked the project-state leaf and main-moved state before assuming my 3-day-old snapshot; doubt-own-snapshot caught it but only at report time.
