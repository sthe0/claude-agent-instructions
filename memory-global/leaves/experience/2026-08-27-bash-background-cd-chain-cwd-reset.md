---
name: 2026-08-27-bash-background-cd-chain-cwd-reset
description: The Bash tool's chained `cd /path && command &` pattern intermittently ran the backgrounded half against the session's ORIGINAL cwd rather than the cd-ed directory -- observed twice in one session: a `nohup python3 scripts/verify-all.py ...` failed with 'No such file or directory' because the relative script path resolved against the pre-cd directory, and a chained cd+nohup sequence silently lost the cd before the background command launched. The harness emits a 'Shell cwd was reset to <original>' system event between tool calls, so a background launch that assumes an earlier foreground cd persisted is wrong to assume so. Fix: never rely on `cd X && cmd &` chaining across a background boundary -- wrap as an explicit subshell, `(cd X && cmd) > log 2>&1 &`, or use absolute paths for every relative reference (script path, git -C target) inside the backgrounded command.
type: reference
schema: difficulty/v1
generality: 0
resolution_confirmed_by_user: "user, 2026-08-27, AskUserQuestion: accepted all 5 requirements and quality=4 for the session in which this was observed"
created: 2026-08-27
last_verified: 2026-08-27
---

# A backgrounded `cd DIR && cmd &` sometimes runs against the ORIGINAL session cwd, not the cd-ed one

## Difficulty
A backgrounded command silently executes against the wrong working directory when it relies on a cd from earlier in the same or a prior tool call rather than an explicit subshell, producing a hard-to-diagnose 'file not found' / 'not a git repository' failure that reads as a missing file rather than a cwd bug.

## Order & criterion
Run a plan stage's verify_command chain -- which needs the repo root as cwd for relative paths like scripts/verify-all.py and git status --porcelain -- as a backgrounded job long enough to exceed the foreground timeout.

**Acceptance check:** measurable: (cd /path && python3 scripts/verify-all.py) > log 2>&1 & completes with the expected exit code and correct relative-path resolution; the same command written as cd /path && nohup python3 scripts/verify-all.py ... & intermittently fails with ENOENT / 'not a git repository' because the background process's cwd is the pre-cd session directory.

## Contexts

### 2026-08-27 — initial
- Where it arose: Driving agentctl record-result / verify-final's verify_command chains as background jobs (nohup, ps-polled) in session baa1daea-e3fa-4fbe-80da-e756ed10313a
- Working plan: /home/the0/.claude-agent/plans/judge-escape-and-loop-guards.toml

## Cost
$83.13, 12 spawns (4 attributed to stages), ~183 min total session duration; quality 4 (user)

## Self-critique of the agent system
I hit this twice before adopting the subshell fix, losing time diagnosing what looked like a missing-file error rather than a cwd bug; the fix (explicit subshell) was available from the start and should have been the default background-launch idiom rather than something learned by repetition.
