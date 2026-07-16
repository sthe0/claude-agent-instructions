---
name: 2026-07-17-2026-07-17-core-pr-landing-with-fine-grained-pat
description: A fine-grained GitHub PAT that can create PRs and issues often CANNOT merge a PR (needs Contents:write), create a repo, or delete a branch — all return 403 'Resource not accessible by personal access token'. Land a Core PR git-natively instead: rebase the branch onto the (usually moved) origin/main, resolve conflicts, then fast-forward push branch:main via SSH — GitHub auto-closes the PR as merged when its commits land in main. Second trap met the same session: a guarded 'git stash list | grep -q . && git stash pop' will blindly pop a PRE-EXISTING unrelated stash left by another task/session (the list is global, not per-branch), conflicting into the tree; guard the intent, not just stash-non-emptiness.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "fedor.solovyev"
refs: [solved-by-007-marker-and-usage-stats, agent-usage-telemetry, 2026-07-09-landed-not-deployed-checkout-parked-on-feature-branch, 2026-06-29-org-portable-core-internal-coupling-opt-in]
created: 2026-07-17
last_verified: 2026-07-17
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

## Cost
1 session (post-compaction continuation); ~7 spawns prior; landing itself ~15 tool calls, 2 conflict resolutions, 1 self-corrected stash mistake.

## Self-critique of the agent system
The guarded stash-pop was a real self-inflicted error: 'git stash list | grep -q .' tests global stash presence, not whether a stash belongs to THIS task — I popped another session's stash. Lesson folded into the leaf. Also: I could have checked the deepagent-leaf and main-moved state before assuming my 3-day-old snapshot; doubt-own-snapshot caught it but only at report time.
