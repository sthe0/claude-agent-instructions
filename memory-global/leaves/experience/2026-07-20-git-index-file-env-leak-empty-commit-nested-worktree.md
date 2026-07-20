---
name: 2026-07-20-git-index-file-env-leak-empty-commit-nested-worktree
description: A Python library that shells out to git and can run inside a pre-commit hook silently produced EMPTY commits: git exports GIT_INDEX_FILE (+GIT_DIR/GIT_PREFIX/... per githooks(5)) to the hook, pointing at the very index being committed; a nested `git worktree add` inherited it and reset THAT index to HEAD, discarding the caller's staged changes. `git -C <root>` does NOT override an inherited GIT_INDEX_FILE. It slipped every manual test because running the verifier directly (no GIT_INDEX_FILE) does not reproduce — running the check != committing through it.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user (2026-07-20)"
refs: [2026-07-17-2026-07-17-core-pr-landing-with-fine-grained-pat, 2026-07-20-stage-verify-command-narrower-than-final-check]
created: 2026-07-20
last_verified: 2026-07-20
---

# A git-shelling library reachable from a pre-commit hook makes EMPTY commits via inherited GIT_INDEX_FILE

## Difficulty
baseline_diff.new_violations calls the caller's finder (which runs `git ls-files --cached`) and baseline_worktree (`git worktree add --detach <tmp> HEAD`), both plain subprocess.run inheriting os.environ. During a real commit the inherited GIT_INDEX_FILE made the nested worktree add rewrite the caller's commit index to HEAD -> empty commit (reproduced: staged count 3->0 with the var set; unaffected without it).

## Order & criterion
Add a _clean_git_env() context manager that pops the leaking git location/index vars (GIT_INDEX_FILE/GIT_DIR/GIT_WORK_TREE/GIT_OBJECT_DIRECTORY/GIT_COMMON_DIR/GIT_PREFIX/GIT_NAMESPACE/GIT_INDEX_VERSION) from os.environ on entry and restores on finally; wrap BOTH baseline_worktree AND new_violations so a caller-supplied finder's git calls run under the clean env too. Nested entry is a safe no-op. Verify by committing through the REAL pre-commit hook (running the check standalone does not reproduce).

**Acceptance check:** measurable: with GIT_INDEX_FILE set to the repo index, new_violations does NOT reset the staged index; a real commit through the pre-commit hook is non-empty; os.environ is byte-identical after the call.

## Contexts

### 2026-07-20 — initial
- Where it arose: scripts/lib/baseline_diff.py (claude-agent-instructions); discovered while landing PR #39, which fixed it end-to-end (its own commit ran the fixed code via verify-config-root-refs.py --staged and stayed non-empty).
- Working plan: baseline-diff-git-index-leak-fix.toml: (1) _clean_git_env scrub + GIT_INDEX_FILE-leak regression test; (2) land Partition A (#38) and Partition B (#39, incl. the fix) as two disjoint-file PRs off origin/main, each committing through the real pre-commit gate without --no-verify.

## Cost
TODO — fill from the figure surfaced by `agentctl resolve` (see also scripts/cost-report.py)

## Self-critique of the agent system
The original plan's final_check ran verify-all --staged DIRECTLY, which cannot reproduce the leak (no GIT_INDEX_FILE) — the verification axis was wrong: 'run the check' != 'commit through the check'. A hook-reachable git library must be verified by an actual hook-driven commit.
